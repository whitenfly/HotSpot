"""数据源分发器：按配置抓取 -> 统一包装为 RawItem + SourceStatus。

负责：
- 按源配置选择适配器（专用处理器优先，否则按 type 路由）
- 每个源独立 try/except，保证单个源失败不影响整体
- 记录数据源可用性（连通性/数据量/耗时/数据更新时间）
- 源间请求间隔（防风控，需求一）
"""

from __future__ import annotations

import asyncio
import time

from ..models import RawItem, RawSnapshot, SourceStatus, now_ms
from .sources import REGISTRY, TYPE_ROUTER

# 上一次真实请求时间（用于源间间隔）
_last_request_at: dict[str, float] = {}


async def fetch_source(source_cfg: dict, force: bool = False) -> tuple[list[RawItem], SourceStatus]:
    """抓取单个数据源，返回 (条目列表, 可用性状态)。永不抛出异常。"""
    source_id = source_cfg["id"]
    source_name = source_cfg.get("name", source_id)
    limit = int(source_cfg.get("limit", 30))
    timeout = float(source_cfg.get("timeoutSeconds", 15))
    started = time.time()
    status = SourceStatus(source=source_id, sourceName=source_name, fetchedAt=now_ms())

    min_interval = float(source_cfg.get("minIntervalMinutes", 10)) * 60
    last = _last_request_at.get(source_id, 0.0)
    if not force and last and (time.time() - last) < min_interval:
        status.skipped = True
        status.connected = True
        status.itemCount = 0
        status.durationMs = 0
        status.error = "间隔未到，复用缓存（本次未请求）"
        return [], status

    try:
        handler = REGISTRY.get(source_id) or TYPE_ROUTER.get(source_cfg.get("type", ""))
        if handler is None:
            raise ValueError(f"未注册的数据源类型/适配器: {source_cfg.get('type')}")
        entries = await handler(source_cfg)
        _last_request_at[source_id] = time.time()

        items: list[RawItem] = []
        for entry in entries[:limit]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            heat = entry.get("heat")
            items.append(RawItem(
                id=f"{source_id}-{_stable_id(title, entry.get('url'))}",
                title=title,
                url=entry.get("url") or "",
                source=source_id,
                sourceName=source_name,
                domain=source_cfg.get("domain"),
                heat=heat,
                heatUnit="热度值" if heat is not None else None,
                publishedAt=entry.get("publishedAt"),
                summary=entry.get("summary"),
                extra=entry.get("extra", {}),
                fetchedAt=now_ms(),
            ))

        status.connected = True
        status.itemCount = len(items)
        status.durationMs = int((time.time() - started) * 1000)
        # 数据源热度：取条目热度的平均值（若有）
        heats = [it.heat for it in items if it.heat is not None]
        if heats:
            status.heat = round(sum(heats) / len(heats), 2)
            status.heatUnit = "平均热度"
        return items, status
    except Exception as exc:  # noqa: BLE001 - 单源失败不允许中断整体
        _last_request_at[source_id] = time.time()
        status.connected = False
        status.itemCount = 0
        status.durationMs = int((time.time() - started) * 1000)
        status.error = f"{type(exc).__name__}: {exc}"
        return [], status


def _stable_id(title: str, url: str) -> str:
    """生成稳定的条目 id：优先用 url 路径/查询中稳定片段，否则用标题哈希前缀。"""
    import hashlib
    import re

    if url:
        m = re.search(r"/(?:p|question|post|item|topic|video|newsDetail_forward_|t/)?(\d{4,})", url)
        if m:
            return m.group(1)
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:10]


async def fetch_all_sources(sources_cfg: list[dict], force: bool = False) -> RawSnapshot:
    """按配置抓取全部启用的数据源，返回一次完整快照。"""
    items: list[RawItem] = []
    statuses: list[SourceStatus] = []
    delay = float(_scheduler_delay())

    for source_cfg in sources_cfg:
        if not source_cfg.get("enabled", True):
            continue
        src_items, status = await fetch_source(source_cfg, force=force)
        items.extend(src_items)
        statuses.append(status)
        if status.durationMs > 0 and delay > 0:
            await asyncio.sleep(delay)  # 源间间隔，防止同一时段集中请求被风控

    return RawSnapshot(items=items, sources=statuses)


def _scheduler_delay() -> float:
    from ..config import config_manager

    return float(config_manager.get_scheduler().get("requestDelaySeconds", 0.5))
