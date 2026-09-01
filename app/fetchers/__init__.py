"""数据源分发器：按配置抓取 -> 统一包装为 RawItem + SourceStatus。

负责：
- 按源配置选择适配器（专用处理器优先，否则按 type 路由）
- 支持 template 解析模板（单文件配置中的 template 字段，参考 template_engine）
- 每个源独立 try/except，保证单个源失败不影响整体
- 记录数据源可用性（连通性/数据量/耗时/数据更新时间）
- 源间请求间隔（防风控，需求一）
- 单源手动测试联通（test_source）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..models import RawItem, RawSnapshot, SourceStatus, now_ms
from ..template_engine import extract_entries, validate_template
from .sources import REGISTRY, TYPE_ROUTER

# 上一次真实请求时间（用于源间间隔）
_last_request_at: dict[str, float] = {}


async def _fetch_entries(source_cfg: dict) -> list[dict[str, Any]]:
    """按配置抓取并解析为通用条目列表。

    - 若配置含 template：请求原始数据后按模板提取（可用于新增自定义源）
    - 否则走内置适配器（专用处理器 > type 路由）
    """
    template = source_cfg.get("template")
    if template:
        validate_template(template)
        return await _fetch_with_template(source_cfg, template)
    handler = REGISTRY.get(source_cfg["id"]) or TYPE_ROUTER.get(source_cfg.get("type", ""))
    if handler is None:
        raise ValueError(f"未注册的数据源类型/适配器: {source_cfg.get('type')}")
    return await handler(source_cfg)


async def _fetch_with_template(source_cfg: dict, template: dict[str, Any]) -> list[dict[str, Any]]:
    """按 template 抓取：先请求原始数据，再按模板提取条目。"""
    from ..fetchers.base import http_get, http_post, parse_feed

    cfg = source_cfg.get("config") or {}
    timeout = float(source_cfg.get("timeoutSeconds", 15))
    url = cfg.get("url", "")
    ttype = template.get("type", "json")

    if not url:
        raise ValueError("template 类型数据源必须配置 config.url")

    if source_cfg.get("type") == "json_post":
        response = await http_post(url, body=cfg.get("body"), timeout=timeout, headers=cfg.get("headers"))
        raw_json = response.json()
        return extract_entries(raw_json, template, int(source_cfg.get("limit", 30)))

    response = await http_get(
        url, timeout=timeout, headers=cfg.get("headers"),
        params=cfg.get("params"),
    )
    content_type = response.headers.get("content-type", "")
    if ttype == "rss":
        xml = response.text
        # 需要原始 XML：直接用 extract_entries 的 rss 分支（raw_xml）
        return extract_entries(None, template, int(source_cfg.get("limit", 30)), raw_xml=xml)
    if ttype == "html":
        return extract_entries(None, template, int(source_cfg.get("limit", 30)), raw_html=response.text)
    return extract_entries(response.json(), template, int(source_cfg.get("limit", 30)))


def _entries_to_items(entries: list[dict[str, Any]], source_cfg: dict) -> list[RawItem]:
    source_id = source_cfg["id"]
    source_name = source_cfg.get("name", source_id)
    limit = int(source_cfg.get("limit", 30))
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
    return items


async def fetch_source(source_cfg: dict, force: bool = False) -> tuple[list[RawItem], SourceStatus]:
    """抓取单个数据源，返回 (条目列表, 可用性状态)。永不抛出异常。"""
    source_id = source_cfg["id"]
    source_name = source_cfg.get("name", source_id)
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
        entries = await _fetch_entries(source_cfg)
        _last_request_at[source_id] = time.time()
        items = _entries_to_items(entries, source_cfg)

        status.connected = True
        status.itemCount = len(items)
        status.durationMs = int((time.time() - started) * 1000)
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


async def test_source(source_cfg: dict) -> dict[str, Any]:
    """单源手动测试联通（需求：数据源可手动单独测试联通、测试数据接收）。

    返回结构化结果供页面可视化：
    - connected / error：联通性
    - rawPreview：原始响应摘要（便于调试）
    - itemsPreview：按模板/适配器解析出的前几条条目
    - durationMs / fetchedAt
    """
    source_id = source_cfg["id"]
    started = time.time()
    result: dict[str, Any] = {
        "source": source_id,
        "sourceName": source_cfg.get("name", source_id),
        "connected": False,
        "error": None,
        "rawPreview": None,
        "itemsPreview": [],
        "durationMs": 0,
        "fetchedAt": now_ms(),
        "template": source_cfg.get("template"),
    }
    try:
        # 独立请求原始数据做预览（复用适配器内部逻辑会丢失原始响应，因此按 type 直接请求）
        raw = await _fetch_raw(source_cfg)
        result["rawPreview"] = raw
        entries = await _fetch_entries(source_cfg)
        items = _entries_to_items(entries, source_cfg)
        result["connected"] = True
        result["itemsPreview"] = [
            {"title": it.title, "url": it.url, "heat": it.heat,
             "publishedAt": it.publishedAt, "summary": it.summary, "extra": it.extra}
            for it in items[:10]
        ]
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["durationMs"] = int((time.time() - started) * 1000)
    return result


async def _fetch_raw(source_cfg: dict) -> dict[str, Any] | str | None:
    """获取原始响应摘要（测试可视化用），失败返回 None。

    rsshub 类型源复用 fetch_rsshub 的容错逻辑（优先 online 实例、跳过错误页），
    保证「测试联通」的原始响应预览与实际抓取行为一致。
    """
    from ..fetchers.base import fetch_rsshub, http_get, http_post

    cfg = source_cfg.get("config") or {}
    timeout = float(source_cfg.get("timeoutSeconds", 15))

    if source_cfg.get("type") == "rsshub":
        path = cfg.get("path", "")
        if not path:
            return None
        try:
            items = await fetch_rsshub(path, 5, timeout=timeout, instances=cfg.get("instances"))
            return _summarize_rsshub(items)
        except Exception as exc:  # noqa: BLE001
            return f"[RSSHub 抓取失败] {type(exc).__name__}: {exc}"

    url = cfg.get("url", "")
    if not url:
        return None
    try:
        if source_cfg.get("type") == "json_post":
            response = await http_post(url, body=cfg.get("body"), timeout=timeout, headers=cfg.get("headers"))
        else:
            response = await http_get(url, timeout=timeout, headers=cfg.get("headers"), params=cfg.get("params"))
        text = response.text
        if len(text) > 2000:
            text = text[:2000] + "…(已截断)"
        return text
    except Exception:  # noqa: BLE001
        return None


def _summarize_rsshub(items: list[dict[str, Any]]) -> str:
    """把 RSSHub 解析出的条目整理为可读的原始预览摘要。"""
    if not items:
        return "[RSSHub] 抓取成功但无条目"
    lines = [f"[RSSHub] 抓取成功，共 {len(items)} 条（预览前 5 条）："]
    for it in items[:5]:
        lines.append(f"- {it.get('title', '')[:60]}  {it.get('url', '')[:50]}")
    return "\n".join(lines)


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
