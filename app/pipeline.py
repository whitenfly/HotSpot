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
from .fetchers import fetch_all_sources
from .models import PublishPayload, RawSnapshot, now_ms
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
