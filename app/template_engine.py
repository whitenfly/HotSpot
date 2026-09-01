"""解析模板引擎（需求：数据源单文件配置中的解析模板）。

模板描述"如何从原始响应中提取结构化条目"，支持三类数据格式：
- json：JSON 点路径定位条目数组 + 字段映射（可含额外嵌套路径）
- rss：RSS/Atom 通用解析（feedparser），字段映射基于条目 dict
- html：CSS 选择器定位链接条目 + 字段映射

模板结构示例（写入 data/sources/{id}.json 的 template 字段）：
{
  "type": "json",
  "itemsPath": "data.list",
  "fields": {
    "title": "title",
    "url": "url",
    "heat": "hot_value",
    "heatUnit": null,
    "publishedAt": "publish_time",
    "summary": "summary",
    "extra": { "author": "user.name" }
  }
}
"""

from __future__ import annotations

import re
from typing import Any

import feedparser
from bs4 import BeautifulSoup


class TemplateError(Exception):
    """模板配置或解析错误。"""


# ============ JSON 点路径 ============

def get_path(data: Any, path: str | None) -> Any:
    """按点路径取 JSON 值：支持 dict 键与数组下标（如 data.list.0.title）。"""
    if path is None:
        return data
    current = data
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def json_items(raw: Any, items_path: str | None) -> list[Any]:
    items = get_path(raw, items_path)
    if items is None:
        return []
    if isinstance(items, dict):
        # 兼容：条目为对象（如 {data: {...}} 无数组）时包装为单条
        return [items]
    if isinstance(items, list):
        return items
    return []


# ============ RSS ============

def _rss_entries(xml_text: str, limit: int) -> list[dict[str, Any]]:
    feed = feedparser.parse(xml_text)
    entries: list[dict[str, Any]] = []
    for entry in feed.entries:
        if len(entries) >= limit:
            break
        published = None
        for key in ("published_parsed", "updated_parsed", "created_parsed"):
            ts = entry.get(key)
            if ts:
                try:
                    import time as _time

                    published = int(_time.mktime(ts) * 1000)
                    break
                except (ValueError, OverflowError):
                    continue
        summary = (
            entry.get("summary")
            or entry.get("description")
            or (entry.get("content", [{}])[0].get("value") if entry.get("content") else "")
            or ""
        )
        entries.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "url": entry.get("link", ""),
            "publishedAt": published,
            "summary": summary,
            "author": entry.get("author", ""),
        })
    return entries


# ============ HTML ============

def html_items(html: str, selector: str, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    links: list[dict[str, Any]] = []
    for a in soup.select(selector):
        if len(links) >= limit:
            break
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        href = a.get("href") or ""
        if not title or not href:
            continue
        links.append({"title": title, "url": href})
    return links


# ============ 字段提取 ============

def _extract_field(item: Any, field_cfg: Any, base: dict[str, Any]) -> Any:
    """从单个原始条目提取一个字段值。

    field_cfg 可为：
    - 字符串：点路径（RSS/HTML 中直接用键名）
    - dict：{path: ..., default: ...}
    - None：跳过
    """
    if field_cfg is None:
        return None
    if isinstance(field_cfg, dict):
        path = field_cfg.get("path")
        default = field_cfg.get("default")
        value = get_path(item, path) if path is not None else None
        return value if value is not None else default
    return get_path(item, str(field_cfg))


def _extract_extra(item: Any, extra_cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(extra_cfg, dict):
        return {}
    extra: dict[str, Any] = {}
    for key, path in extra_cfg.items():
        value = get_path(item, str(path)) if path is not None else None
        if value is not None:
            extra[key] = value
    return extra


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except ValueError:
        return None


# ============ 主入口 ============

def extract_entries(
    raw: Any,
    template: dict[str, Any],
    limit: int,
    *,
    raw_html: str | None = None,
    raw_xml: str | None = None,
) -> list[dict[str, Any]]:
    """按模板从原始数据提取通用条目列表。

    返回每个条目包含 title/url/heat/publishedAt/summary/extra 的 dict。
    """
    ttype = template.get("type", "json")
    fields = template.get("fields") or {}
    limit = max(1, int(limit))

    if ttype == "rss":
        xml = raw_xml if raw_xml is not None else (raw if isinstance(raw, str) else "")
        raw_items: list[Any] = _rss_entries(xml, limit)
    elif ttype == "html":
        html = raw_html if raw_html is not None else (raw if isinstance(raw, str) else "")
        selector = template.get("selector", "a[href]")
        raw_items = html_items(html, selector, limit)
    else:
        raw_items = json_items(raw, template.get("itemsPath"))

    entries: list[dict[str, Any]] = []
    for item in raw_items:
        if len(entries) >= limit:
            break
        title = _extract_field(item, fields.get("title"), {})
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not title:
            continue
        url = _extract_field(item, fields.get("url"), {})
        if ttype in ("rss",) and not url:
            url = _extract_field(item, fields.get("link"), {})
        heat = _num(_extract_field(item, fields.get("heat"), {}))
        published = _extract_field(item, fields.get("publishedAt"), {})
        published_at = None
        if published is not None:
            try:
                import datetime

                if isinstance(published, (int, float)) and published > 0:
                    published_at = int(float(published) if float(published) > 1e11 else float(published) * 1000)
                else:
                    parsed = datetime.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                    published_at = int(parsed.timestamp() * 1000)
            except (ValueError, TypeError):
                published_at = None
        summary = _extract_field(item, fields.get("summary"), {})
        entries.append({
            "title": title,
            "url": str(url or ""),
            "heat": heat,
            "publishedAt": published_at,
            "summary": re.sub(r"\s+", " ", str(summary or "")).strip() or None,
            "extra": _extract_extra(item, fields.get("extra", {})),
        })
    return entries


def validate_template(template: Any) -> None:
    """校验模板结构，非法时抛出 TemplateError。"""
    if not isinstance(template, dict):
        raise TemplateError("template 必须是 JSON 对象")
    ttype = template.get("type", "json")
    if ttype not in ("json", "rss", "html"):
        raise TemplateError(f"不支持的模板类型: {ttype}")
    fields = template.get("fields")
    if not isinstance(fields, dict) or not fields.get("title"):
        raise TemplateError("模板必须包含 fields.title 字段映射")
    if ttype == "json" and not template.get("itemsPath"):
        raise TemplateError("json 类型模板必须包含 itemsPath")
    if ttype == "html" and not template.get("selector"):
        raise TemplateError("html 类型模板必须包含 selector")
