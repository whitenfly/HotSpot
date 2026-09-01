"""流水线编排与后台调度。

流程：获取数据 -> 清洗 -> AI 整理 -> 存储备份 -> 发布 API。
后台定时任务按 scheduler 配置周期执行；间隔未到自动复用缓存（防风控）。
"""

from __future__ import annotations

import asyncio
import logging
import time

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

async def run_fetch(force: bool = False, reuse_window_seconds: float = 0) -> RawSnapshot:
    """执行数据获取。

    reuse_window_seconds > 0 时，若最近一次获取在此窗口内则直接复用（需求五-2）。
    force=True 忽略数据源间隔强制请求。
    """
    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        if not force and reuse_window_seconds > 0:
            cached = storage.latest_within("raw", reuse_window_seconds)
            if cached:
                _set_stage("fetch", "复用窗口内缓存（未发起请求）")
                snapshot = RawSnapshot.model_validate(cached)
                _run_state["runs"]["raw"] = snapshot.runId
                _run_state["finishedAt"] = now_ms()
                return snapshot

        _set_stage("fetch", "开始获取数据源…")
        sources_cfg = config_manager.get_sources(enabled_only=True)
        snapshot = await fetch_all_sources(sources_cfg, force=force)
        storage.save_snapshot("raw", snapshot)
        _run_state["runs"]["raw"] = snapshot.runId
        ok = sum(1 for s in snapshot.sources if s.connected)
        _set_stage("fetch", f"获取完成：{ok}/{len(snapshot.sources)} 个源正常，共 {len(snapshot.items)} 条")
        return snapshot
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("fetch", f"获取失败：{exc}")
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


async def run_fetch_source(source_id: str, force: bool = True) -> RawSnapshot:
    """单源单独获取，并与最近一次获取快照部分合并（需求：只更新该数据源数据部分）。

    场景：全量获取后某源失败/需单独刷新时，手动对该源重新获取，
    新数据替换最近快照中该源的数据，其他源数据保留，生成新的 raw 快照
    （覆盖 latest/raw），从而后续 清洗/AI 流程基于合并后的完整数据继续。
    """
    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        source_cfg = config_manager.get_source(source_id)
        if source_cfg is None:
            raise RuntimeError(f"数据源不存在: {source_id}")

        _set_stage("fetch", f"单独获取数据源 {source_cfg.get('name', source_id)}…")
        new_items, status = await fetch_source(source_cfg, force=force)

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
            f"单源获取完成：{source_cfg.get('name', source_id)} {len(new_items)} 条，"
            f"合并后共 {len(snapshot.items)} 条（其他源数据保留）"
        )
        _set_stage("fetch", msg)
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


async def run_clean() -> dict:
    """基于最新原始数据执行清洗。"""
    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        raw = storage.load_latest("raw")
        if not raw:
            raise RuntimeError("无原始数据，请先执行【获取数据】")
        _set_stage("clean", f"清洗 {len(raw.get('items', []))} 条原始数据…")
        cleaned = cleaner.clean_snapshot(raw)
        storage.save_snapshot("cleaned", cleaned)
        _run_state["runs"]["cleaned"] = cleaned.runId
        _set_stage("clean", f"清洗完成：{len(cleaned.items)} 条")
        return cleaned.model_dump()
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("clean", f"清洗失败：{exc}")
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


async def run_ai() -> dict:
    """基于最新清洗数据执行 AI 整理。"""
    _run_state["running"] = True
    _run_state["startedAt"] = now_ms()
    _run_state["lastError"] = None
    try:
        cleaned = storage.load_latest("cleaned")
        if not cleaned:
            raise RuntimeError("无清洗后数据，请先执行【数据清洗】")
        ai_cfg = config_manager.get_ai()
        if not ai_cfg.get("enabled", False):
            # 未启用时给出明确提示（仍可手动触发）
            logger.warning("AI 整理未在配置中启用，仍按手动触发执行")
        _set_stage("ai", f"AI 整理 {len(cleaned.get('items', []))} 条清洗数据…（模型 {ai_cfg.get('model')}）")
        snapshot = await ai_organizer.organize(cleaned)
        storage.save_snapshot("ai", snapshot)
        _run_state["runs"]["ai"] = snapshot.runId
        publish_latest()  # 同步发布到对外 API
        _set_stage("ai", f"AI 整理完成：{snapshot.total} 条，{len(snapshot.categories)} 个领域")
        return snapshot.model_dump()
    except Exception as exc:  # noqa: BLE001
        _run_state["lastError"] = f"{type(exc).__name__}: {exc}"
        _set_stage("ai", f"AI 整理失败：{exc}")
        raise
    finally:
        _run_state["running"] = False
        _run_state["finishedAt"] = now_ms()


async def run_all(force: bool = False) -> dict:
    """全流程：获取 -> 清洗 -> AI 整理。"""
    await run_fetch(force=force)
    cleaned = await run_clean()
    await run_ai()
    return cleaned


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
    """周期执行获取 / AI 整理（间隔未到自动跳过）。"""
    while True:
        scheduler = config_manager.get_scheduler()
        if scheduler.get("enabled", True):
            try:
                now = time.time()
                fetch_interval = float(scheduler.get("fetchIntervalMinutes", 15)) * 60
                ai_interval = float(scheduler.get("aiIntervalMinutes", 30)) * 60

                last_fetch = _latest_time("raw")
                if last_fetch is None or (now - last_fetch) >= fetch_interval:
                    await run_fetch(force=False)
                    if scheduler.get("runAiAfterFetch", False):
                        await run_clean()
                        await run_ai()
                else:
                    _set_stage("idle", "获取间隔未到，跳过本轮")

                last_ai = _latest_time("ai")
                ai_cfg = config_manager.get_ai()
                if ai_cfg.get("enabled", False) and (last_ai is None or (now - last_ai) >= ai_interval):
                    cleaned = storage.load_latest("cleaned")
                    if cleaned:
                        await run_ai()
                elif not ai_cfg.get("enabled", False):
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
