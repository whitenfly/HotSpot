"""流水线编排与后台调度。

流程：获取数据 -> 清洗 -> AI 整理 -> 存储备份 -> 发布 API。
后台定时任务按 scheduler 配置周期执行；间隔未到自动复用缓存（防风控）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from . import ai_organizer, cleaner
from .config import config_manager
from .fetchers import fetch_all_sources, fetch_source
from .models import PublishPayload, RawItem, RawSnapshot, now_ms
from .storage import storage

logger = logging.getLogger("hotspot.pipeline")

# 全局运行状态（Web UI 轮询展示）
_run_state: dict = {
    "running": False,
    "stage": "idle",
    "startedAt": 0,
    "finishedAt": 0,
    "message": "",
    "lastError": None,
    "runs": {},  # stage -> runId
}


def run_state() -> dict:
    return {**_run_state, "runs": dict(_run_state["runs"])}


def _set_stage(stage: str, message: str = "") -> None:
    _run_state["stage"] = stage
    _run_state["message"] = message
    logger.info("[%s] %s", stage, message)


# ============ 单步执行 ============

async def run_fetch(
    force: bool = False,
    reuse_window_seconds: float = 0,
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> RawSnapshot:
    """执行数据获取（后台任务化，progress_cb 上报进度 0-100）。

    reuse_window_seconds > 0 时，若最近一次获取在此窗口内则直接复用（需求五-2）。
    force=True 忽略数据源间隔强制请求。快照写入经 snapshot_lock 串行。
    """
    from .tasks import task_manager

    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        if not force and reuse_window_seconds > 0:
            cached = storage.latest_within("raw", reuse_window_seconds)
            if cached:
                if progress_cb:
                    await progress_cb(100, "获取数据", "复用窗口内缓存（未发起请求）")
                _set_stage("fetch", "复用窗口内缓存（未发起请求）")
                snapshot = RawSnapshot.model_validate(cached)
                _run_state["runs"]["raw"] = snapshot.runId
                _run_state["finishedAt"] = now_ms()
                return snapshot

        _set_stage("fetch", "开始获取数据源…")
        sources_cfg = config_manager.get_sources(enabled_only=True)

        async def _progress(p: int, stage: str | None, msg: str) -> None:
            if progress_cb:
                await progress_cb(p, "获取数据", msg)

        # 并发抓取（网络段不持锁），完成后在快照锁内写盘
        snapshot = await fetch_all_sources(sources_cfg, force=force, progress_cb=_progress)
        async with task_manager.snapshot_lock:
            storage.save_snapshot("raw", snapshot)
        _run_state["runs"]["raw"] = snapshot.runId
        update_source_availability(snapshot)
        ok = sum(1 for s in snapshot.sources if s.connected)
        msg = f"获取完成：{ok}/{len(snapshot.sources)} 个源正常，共 {len(snapshot.items)} 条"
        _set_stage("fetch", msg)
        if progress_cb:
            await progress_cb(100, "获取数据", msg)
        return snapshot
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("fetch", f"获取失败：{exc}")
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


async def run_fetch_source(
    source_id: str,
    force: bool = True,
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> RawSnapshot:
    """单源单独获取，并与最近一次获取快照部分合并（需求：只更新该数据源数据部分）。

    后台任务化：progress_cb 上报进度；支持多个数据源并行单独获取——
    网络请求段并行执行，快照合并写经 task_manager.snapshot_lock 串行，避免互相覆盖。
    """
    from .tasks import task_manager

    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        source_cfg = config_manager.get_source(source_id)
        if source_cfg is None:
            raise RuntimeError(f"数据源不存在: {source_id}")

        name = source_cfg.get("name", source_id)
        if progress_cb:
            await progress_cb(5, "获取数据", f"单独获取数据源 {name}…")
        new_items, status = await fetch_source(source_cfg, force=force)
        if progress_cb:
            await progress_cb(70, "获取数据", f"{name} 抓取完成（{len(new_items)} 条），合并快照…")

        # 读改写合并段持快照锁（多源并行时串行，保证不覆盖其他源的并行合并结果）
        async with task_manager.snapshot_lock:
            latest = storage.load_latest("raw")
            if latest is None:
                snapshot = RawSnapshot(items=new_items, sources=[status])
            else:
                base = RawSnapshot.model_validate(latest)
                kept_items = [it for it in base.items if it.source != source_id]
                kept_statuses = [s for s in base.sources if s.source != source_id]
                snapshot = RawSnapshot(
                    items=kept_items + new_items,
                    sources=kept_statuses + [status],
                    extra={"partial": True, "mergedSource": source_id, "baseRunId": base.runId},
                )
            storage.save_snapshot("raw", snapshot)
        _run_state["runs"]["raw"] = snapshot.runId
        update_source_availability(snapshot)
        msg = (
            f"单源获取完成：{name} {len(new_items)} 条，"
            f"合并后共 {len(snapshot.items)} 条（其他源数据保留）"
        )
        _set_stage("fetch", msg)
        if progress_cb:
            await progress_cb(100, "获取数据", msg)
        return snapshot
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("fetch", f"单源获取失败：{exc}")
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


def update_source_availability(snapshot: RawSnapshot, status: SourceStatus | None = None) -> dict:
    """更新独立源可用性快照（latest/availability.json）。

    单源测试 / 单源获取后调用：以最近一次可用性快照为基础（无则用最近 raw/ai 快照），
    更新/合并指定源的状态后写回，使「数据源可用性」列表始终反映最新单源操作结果。
    """
    from .models import SourceStatus as _Status

    # 基准：availability 优先，回退 raw，再回退 ai
    availability = storage.load_latest("availability")
    base_sources: list[dict] = []
    base_fetched = 0
    if availability and availability.get("sources"):
        base_sources = availability["sources"]
        base_fetched = availability.get("updatedAt", 0)
    else:
        for kind in ("raw", "ai"):
            snap = storage.load_latest(kind)
            if snap and snap.get("sources"):
                base_sources = snap["sources"]
                base_fetched = snap.get("fetchedAt") or snap.get("generatedAt") or 0
                break

    merged = {s["source"]: dict(s) for s in base_sources if isinstance(s, dict) and s.get("source")}
    if status is not None:
        merged[status.source] = status.model_dump()
    elif snapshot is not None:
        for s in snapshot.sources:
            merged[s.source] = s.model_dump()

    now = now_ms()
    payload = {
        "type": "availability",
        "updatedAt": now,
        "sources": list(merged.values()),
    }
    storage.save_raw_extra("availability", payload)
    return payload


def exclude_source(source_id: str) -> dict | None:
    """禁用数据源：从最新原始快照中剔除该源数据并备份，使其不再进入后续清洗/AI。

    备份内容含该源全部条目与获取时间（fetchedAt），存于 data/disabled/{source_id}/，
    供重新启用时恢复（需求：保留备份，看数据获取时间决定是否复用）。
    """
    latest = storage.load_latest("raw")
    if not latest:
        return None
    base = RawSnapshot.model_validate(latest)
    removed_items = [it for it in base.items if it.source == source_id]
    if removed_items:
        fetched_at = max(it.fetchedAt for it in removed_items)
        storage.save_disabled_source(source_id, {
            "sourceId": source_id,
            "sourceName": removed_items[0].sourceName,
            "fetchedAt": fetched_at,
            "backedUpAt": now_ms(),
            "items": [it.model_dump() for it in removed_items],
        })
    kept_items = [it for it in base.items if it.source != source_id]
    kept_statuses = [s for s in base.sources if s.source != source_id]
    extra = dict(base.extra)
    extra["excludedSource"] = source_id
    snapshot = RawSnapshot(items=kept_items, sources=kept_statuses, extra=extra)
    storage.save_snapshot("raw", snapshot)
    update_source_availability(snapshot)
    return snapshot.model_dump()


def restore_source(source_id: str) -> dict | None:
    """重新启用数据源：从备份恢复该源数据并合并回最新原始快照。

    仅当备份数据的获取时间不早于当前快照中该源数据时复用（需求：看数据获取时间）；
    无备份时返回 None（等待下一次正常抓取）。
    """
    backup = storage.load_disabled_source(source_id)
    if not backup or not backup.get("items"):
        return None
    backup_items = [RawItem.model_validate(it) for it in backup["items"]]
    backup_fetched = backup.get("fetchedAt") or max(it.fetchedAt for it in backup_items)

    latest = storage.load_latest("raw")
    if not latest:
        snapshot = RawSnapshot(items=backup_items)
    else:
        base = RawSnapshot.model_validate(latest)
        existing = [it for it in base.items if it.source == source_id]
        existing_fetched = max(it.fetchedAt for it in existing) if existing else 0
        if existing and existing_fetched >= backup_fetched:
            return base.model_dump()  # 现有数据更新，不覆盖
        kept_items = [it for it in base.items if it.source != source_id]
        kept_statuses = [s for s in base.sources if s.source != source_id]
        snapshot = RawSnapshot(
            items=kept_items + backup_items,
            sources=kept_statuses,
            extra=dict(base.extra),
        )
    storage.save_snapshot("raw", snapshot)
    update_source_availability(snapshot)
    return snapshot.model_dump()


async def run_clean(
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> dict:
    """基于最新原始数据执行清洗（后台任务化，progress_cb 上报进度）。"""
    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        if progress_cb:
            await progress_cb(10, "清洗数据", "读取原始数据…")
        raw = storage.load_latest("raw")
        if not raw:
            raise RuntimeError("无原始数据，请先执行【获取数据】")
        if progress_cb:
            await progress_cb(40, "清洗数据", f"清洗 {len(raw.get('items', []))} 条原始数据…")
        _set_stage("clean", f"清洗 {len(raw.get('items', []))} 条原始数据…")
        cleaned = cleaner.clean_snapshot(raw)
        storage.save_snapshot("cleaned", cleaned)
        _run_state["runs"]["cleaned"] = cleaned.runId
        _set_stage("clean", f"清洗完成：{len(cleaned.items)} 条")
        if progress_cb:
            await progress_cb(100, "清洗数据", f"清洗完成：{len(cleaned.items)} 条")
        return cleaned.model_dump()
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("clean", f"清洗失败：{exc}")
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


async def run_ai(
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> dict:
    """基于最新清洗数据执行 AI 整理（后台任务化，progress_cb 上报批进度）。

    每次整理生成一条过程追踪（run trace，runId 为时间 ID），记录每批输入/发送/返回/
    解析及终稿，成功与失败均落盘供详情页可视化调试。
    """
    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    trace: dict = {
        "runId": time.strftime("%Y%m%d-%H%M%S"),
        "startedAt": now_ms(),
        "status": "running",
    }
    try:
        cleaned = storage.load_latest("cleaned")
        if not cleaned:
            raise RuntimeError("无清洗后数据，请先执行【数据清洗】")
        ai_cfg = config_manager.get_ai()
        trace["sourceItemCount"] = len(cleaned.get("items", []))
        if not ai_cfg.get("enabled", False):
            logger.warning("AI 整理未在配置中启用，仍按手动触发执行")
        if progress_cb:
            await progress_cb(5, "AI 整理", f"准备整理 {len(cleaned.get('items', []))} 条数据…")
        _set_stage("ai", f"AI 整理 {len(cleaned.get('items', []))} 条清洗数据…（模型 {ai_cfg.get('model')}）")
        snapshot = await ai_organizer.organize(cleaned, progress_cb=progress_cb, trace=trace)
        storage.save_snapshot("ai", snapshot)
        _run_state["runs"]["ai"] = snapshot.runId
        publish_latest()  # 同步发布到对外 API
        _set_stage("ai", f"AI 整理完成：{snapshot.total} 条，{len(snapshot.categories)} 个领域")
        if progress_cb:
            await progress_cb(100, "AI 整理", f"AI 整理完成：{snapshot.total} 条")
        trace["status"] = "done"
        trace["total"] = snapshot.total
        trace["categories"] = snapshot.categories
        trace["finishedAt"] = now_ms()
        trace["runId"] = snapshot.runId  # 与 ai 快照 runId 对齐
        storage.save_ai_run(trace)
        return snapshot.model_dump()
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("ai", f"AI 整理失败：{exc}")
        # 失败现场也保存（含已完成批次与错误），供详情页调试
        trace["status"] = "failed"
        trace["error"] = f"{type(exc).__name__}: {exc}"
        trace["finishedAt"] = now_ms()
        storage.save_ai_run(trace)
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


async def run_ai_batch_retry(
    run_id: str,
    batch_index: int,
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> dict:
    """单批重试（需求：某批请求失败/想重新生成时，只重跑该批，不必全部重来）。

    以 run trace 中该批记录的 inputItems 重新调用 AI，成功后原地更新该批
    （aiResponse/parsedGroups/status=ok/error 清除），并保存 trace。
    """
    trace = storage.load_ai_run(run_id)
    if trace is None:
        raise RuntimeError(f"AI 整理记录不存在: {run_id}")
    entry = next((b for b in trace.get("batches", []) if b.get("batchIndex") == batch_index), None)
    if entry is None:
        raise RuntimeError(f"批次 {batch_index} 不存在")
    batch = ai_organizer.batch_input_from_trace(entry)
    if not batch:
        raise RuntimeError(f"批次 {batch_index} 无输入条目可重试")

    if progress_cb:
        await progress_cb(10, "AI 重试", f"重试批次 {batch_index}…")
    ai_cfg = config_manager.get_ai()
    index_offset = int(entry.get("inputOffset", 0))
    # groupId 偏移：该批之前所有成功批的组数（保持重试前编号稳定）
    group_base = ai_organizer.count_ok_groups(trace, up_to_batch_index=batch_index)
    entry["status"] = "running"
    entry["error"] = None
    entry.pop("parsedGroups", None)
    storage.save_ai_run(trace)
    try:
        groups = await ai_organizer.process_single_batch(batch, index_offset, group_base, ai_cfg, entry)
        # 该批之后的成功批组数可能因重试改变，统一重排后续批次的 groupId，保证全局唯一
        renumber_groups_from(trace, batch_index)
        if progress_cb:
            await progress_cb(100, "AI 重试", f"批次 {batch_index} 重试成功（{len(groups)} 组）")
        storage.save_ai_run(trace)
        return {"ok": True, "batchIndex": batch_index, "groups": len(groups),
                "trace": storage.load_ai_run(run_id)}
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        storage.save_ai_run(trace)
        raise


def renumber_groups_from(trace: dict[str, Any], from_batch_index: int) -> None:
    """重排 trace 中从某批起的全部成功批组的 groupId（重试改变组数后保证全局唯一）。

    从 from_batch_index 批开始，先统计其之前成功批的组数作为起始编号，逐批连续编号。
    """
    base = ai_organizer.count_ok_groups(trace, up_to_batch_index=from_batch_index)
    for b in trace.get("batches", []):
        if b.get("batchIndex", 1) < from_batch_index:
            continue
        if b.get("status") != "ok":
            continue
        for g in b.get("parsedGroups") or []:
            g["groupId"] = f"g{base}"
            base += 1


async def run_ai_finalize(
    run_id: str,
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> dict:
    """续跑终稿合并（需求：批次部分失败/重试后，基于全部成功批重新做跨批合并并产出榜单）。

    收集 run trace 中全部 status=ok 批次的解析组 → 终稿合并 → 生成 AiSnapshot →
    存储 ai 快照并发布到 API，同时更新该 run trace 为 done。
    """
    trace = storage.load_ai_run(run_id)
    if trace is None:
        raise RuntimeError(f"AI 整理记录不存在: {run_id}")
    groups = ai_organizer.collect_ok_groups(trace)
    if not groups:
        raise RuntimeError("没有成功批次可合并（请先重试失败批次）")
    ai_cfg = config_manager.get_ai()
    if progress_cb:
        await progress_cb(20, "AI 续跑", f"基于 {len(groups)} 组做终稿合并…")
    finalize_trace = dict(trace.get("finalize") or {})
    finals = await ai_organizer.finalize_groups(groups, ai_cfg, finalize_trace)

    # 构造 cleaned 元信息（沿用原 run 的清洗来源与源状态）
    cleaned_src = storage.load_latest("cleaned") or {}
    from .models import CleanedSnapshot

    cleaned = CleanedSnapshot(
        runId=str(trace.get("cleanedFromRunId") or cleaned_src.get("runId") or "resumed"),
        cleanedFromRunId=str(trace.get("cleanedFromRunId") or cleaned_src.get("runId") or ""),
        sources=cleaned_src.get("sources", []),
    )
    snapshot = ai_organizer.build_snapshot(cleaned, ai_cfg, finals)
    storage.save_snapshot("ai", snapshot)
    _run_state["runs"]["ai"] = snapshot.runId
    publish_latest()
    if progress_cb:
        await progress_cb(90, "AI 续跑", f"榜单已生成：{snapshot.total} 条")

    # 更新 run trace：写入续跑终稿、标 done
    trace["status"] = "done"
    trace["finalize"] = finalize_trace
    trace["total"] = snapshot.total
    trace["categories"] = snapshot.categories
    trace["finishedAt"] = now_ms()
    trace["resumedFrom"] = trace.get("runId")
    storage.save_ai_run(trace)
    if progress_cb:
        await progress_cb(100, "AI 续跑", f"续跑完成：{snapshot.total} 条（runId {snapshot.runId}）")
    return {"ok": True, "runId": snapshot.runId, "total": snapshot.total}


async def run_all(
    force: bool = False,
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
) -> dict:
    """全流程：获取 -> 清洗 -> AI 整理（后台任务化，阶段加权进度 0-100）。"""
    await run_fetch(force=force, progress_cb=_map_progress(progress_cb, 0, 50))
    cleaned = await run_clean(progress_cb=_map_progress(progress_cb, 50, 60))
    await run_ai(progress_cb=_map_progress(progress_cb, 60, 100))
    return cleaned


def _map_progress(
    outer: Callable[[int, str | None, str], Awaitable[None]] | None,
    lo: int,
    hi: int,
) -> Callable[[int, str | None, str], Awaitable[None]] | None:
    """把子任务进度 [0,100] 线性映射到 [lo, hi]（全流程阶段加权）。"""
    if outer is None:
        return None

    async def cb(p: int, stage: str | None, msg: str) -> None:
        await outer(lo + int((hi - lo) * p / 100), stage, msg)

    return cb


pipeline_run_all = run_all


# ============ 对外发布（Firefly 消费） ============

def publish_latest() -> None:
    """将最新 AI 数据组装为 PublishPayload 并落盘 latest/publish.json。"""
    ai = storage.load_latest("ai")
    if not ai:
        return
    scheduler = config_manager.get_scheduler()
    interval = float(scheduler.get("fetchIntervalMinutes", 15))
    generated = ai.get("generatedAt") or ai.get("fetchedAt") or now_ms()
    payload = PublishPayload(
        generatedAt=generated,
        updatedAt=now_ms(),
        nextRefresh=generated + int(interval * 60 * 1000),
        refreshIntervalHours=interval / 60,
        sourceItemCount=ai.get("sourceItemCount", 0),
        total=ai.get("total", 0),
        categories=ai.get("categories", []),
        items=ai.get("items", []),
        ranking=ai.get("ranking", {}),
        sources=ai.get("sources", []),
        sourcesOfItems=ai.get("sources", []),
    )
    storage.save_raw_extra("publish", payload.model_dump())


# ============ 后台调度 ============

async def scheduler_loop() -> None:
    """周期执行获取 / AI 整理（间隔未到自动跳过）。

    定时触发的任务经 task_manager 提交，与其他任务一样出现在任务列表并展示进度
    （label 带「定时」前缀便于区分）；若已有流水线任务（手动或定时）在运行则本轮跳过，
    避免重复提交与排队堆积。
    """
    from .tasks import task_manager

    while True:
        scheduler = config_manager.get_scheduler()
        if scheduler.get("enabled", True):
            try:
                now = time.time()
                fetch_interval = float(scheduler.get("fetchIntervalMinutes", 15)) * 60
                ai_interval = float(scheduler.get("aiIntervalMinutes", 30)) * 60
                ai_cfg = config_manager.get_ai()

                # 已有流水线任务在运行（含排队中的）→ 本轮跳过，交给任务队列自行完成
                running = await task_manager.running_pipeline()
                if running is not None:
                    _set_stage("idle", f"已有任务在运行（{running.get('label', '')}），定时轮次跳过")
                    await asyncio.sleep(60)
                    continue

                submitted = False
                last_fetch = _latest_time("raw")
                fetch_due = last_fetch is None or (now - last_fetch) >= fetch_interval
                ai_enabled = ai_cfg.get("enabled", False)
                run_ai_after = scheduler.get("runAiAfterFetch", False)

                if fetch_due:
                    # 定时全量获取；runAiAfterFetch 且 AI 已启用时一步完成 获取+清洗+AI
                    if run_ai_after and ai_enabled:
                        await task_manager.submit(
                            "run_all", "定时全流程（获取+清洗+AI）",
                            lambda cb: run_all(force=False, progress_cb=cb),
                        )
                    else:
                        await task_manager.submit(
                            "fetch_all", "定时获取数据",
                            lambda cb: run_fetch(force=False, progress_cb=cb),
                        )
                    submitted = True
                    _set_stage("idle", "定时获取已提交后台任务")
                elif not fetch_due:
                    _set_stage("idle", "获取间隔未到，跳过本轮")

                # AI 独立周期（仅当本轮未提交含 AI 的组合任务且 AI 已启用）
                last_ai = _latest_time("ai")
                ai_due = last_ai is None or (now - last_ai) >= ai_interval
                if not submitted and ai_enabled and ai_due:
                    cleaned = storage.load_latest("cleaned")
                    if cleaned:
                        await task_manager.submit(
                            "ai", "定时 AI 整理",
                            lambda cb: run_ai(progress_cb=cb),
                        )
                        _set_stage("idle", "定时 AI 整理已提交后台任务")
                elif not ai_enabled:
                    _set_stage("idle", "AI 整理未启用（在配置中填写 API 密钥并启用）")
            except Exception as exc:  # noqa: BLE001
                _run_state["lastError"] = f"调度异常: {exc}"
                logger.exception("scheduler error")
        await asyncio.sleep(60)


def _latest_time(kind: str) -> float | None:
    latest = storage.load_latest(kind)
    if not latest:
        return None
    ts = latest.get("fetchedAt") or latest.get("generatedAt") or 0
    return ts / 1000 if ts else None
