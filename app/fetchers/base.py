"""数据源抓取基础设施。

提供：
- httpx 异步 HTTP 客户端（统一 UA / 超时）
- RSS/Atom 解析（feedparser）
- RSSHub 多实例故障转移（参考 WhatsHot 的 M3 机制）
- Bilibili WBI 签名（参考 WhatsHot 的 M5 机制）
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import feedparser
import httpx

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# RSSHub 公共实例（多地址故障转移）
DEFAULT_RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.fatpandac.com",
    "https://rsshub.liumingye.cn",
    "https://rsshub.moeyy.xyz",
    "https://rss.rssforever.com",
]


class FetchError(Exception):
    """数据源抓取失败。"""


# ============ HTTP 客户端 ============

async def http_get(
    url: str,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    """GET 请求，统一浏览器 UA，超时控制。"""
    req_headers = {"User-Agent": BROWSER_UA}
    if headers:
        req_headers.update(headers)
    owns = client is None
    c = client or httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=req_headers)
    try:
        resp = await c.get(url, headers=req_headers, params=params)
        resp.raise_for_status()
        return resp
    finally:
        if owns:
            await c.aclose()


async def http_post(
    url: str,
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    req_headers = {"User-Agent": BROWSER_UA, "Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    owns = client is None
    c = client or httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=req_headers)
    try:
        resp = await c.post(url, json=body, headers=req_headers)
        resp.raise_for_status()
        return resp
    finally:
        if owns:
            await c.aclose()


# ============ RSS/Atom 解析 ============

def parse_feed(xml_text: str, limit: int) -> list[dict[str, Any]]:
    """解析 RSS 2.0 / Atom 为通用条目。参考 WhatsHot utils/feed.py 的字段。"""
    feed = feedparser.parse(xml_text)
    entries: list[dict[str, Any]] = []
    for entry in feed.entries:
        if len(entries) >= limit:
            break
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        link = entry.get("link") or ""
        # 发布时间：published / updated / created
        published = None
        for key in ("published_parsed", "updated_parsed", "created_parsed"):
            ts = entry.get(key)
            if ts:
                try:
                    published = int(time.mktime(ts) * 1000)
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
            "title": title,
            "url": link,
            "publishedAt": published,
            "summary": summary,
            "extra": {
                "author": entry.get("author", ""),
                "tags": [t.get("term", "") for t in entry.get("tags", []) if t.get("term")],
            },
        })
    return entries


# ============ RSSHub 多实例故障转移 ============

async def fetch_rsshub(
    path: str,
    limit: int,
    timeout: float = 20.0,
    instances: list[str] | None = None,
) -> list[dict[str, Any]]:
    """依次尝试各 RSSHub 实例，首个成功返回即停止。参考 WhatsHot utils/rsshub.py。

    实例尝试顺序（需求：上次成功实例优先）：
    1. 路由级"成功实例"记忆（该 path 上次测试联通/获取成功的实例，来自 rsshub_manager）
    2. 已标记 online 的全局实例
    3. 全部全局实例（或配置指定的 instances）
    记忆实例抓取成功则刷新记忆；失败则清除该条记忆并继续尝试其他实例。
    另跳过返回 "# Looks like something went wrong" 等错误页的实例（实例在线但路由无数据）。
    """
    from ..rsshub_manager import rsshub_instances

    candidates = _rsshub_instance_order(path, instances)
    last_error = ""
    for base in candidates:
        base = base.rstrip("/")
        url = f"{base}{path}?format=json&limit={limit}"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            if _is_rsshub_error_page(data):
                last_error = f"[RSSHub] 实例 {base} 路由无数据（错误页）"
                continue
            items = _rsshub_items(data, limit)
            if items:
                # 抓取成功：记录该路由的成功实例
                rsshub_instances.set_success_instance(path, base)
                return items
            last_error = f"[RSSHub] 实例 {base} 解析结果为空"
        except Exception as exc:  # noqa: BLE001 - 故障转移需要吞掉单个实例错误
            last_error = f"[RSSHub] {base} 失败: {exc}"
    # 全部实例失败：若记忆实例是最后尝试的（且失败），清除记忆避免下次重复
    remembered = rsshub_instances.get_success_instance(path)
    if remembered:
        rsshub_instances.clear_success_instance(path)
    raise FetchError(last_error or "[RSSHub] 所有实例均失败")


def _rsshub_instance_order(path: str, instances: list[str] | None) -> list[str]:
    """构建实例尝试顺序：记忆实例 → online 实例 → 全部实例（去重）。"""
    from ..rsshub_manager import rsshub_instances

    if instances:
        ordered: list[str] = []
        remembered = rsshub_instances.get_success_instance(path)
        if remembered:
            ordered.append(remembered)
        for inst in instances:
            inst = inst.rstrip("/")
            if inst not in ordered:
                ordered.append(inst)
        return ordered

    ordered = []
    remembered = rsshub_instances.get_success_instance(path)
    if remembered:
        ordered.append(remembered)
    for inst in rsshub_instances.online_urls() + rsshub_instances.list_instance_urls():
        inst = inst.rstrip("/")
        if inst not in ordered:
            ordered.append(inst)
    return ordered


def _is_rsshub_error_page(data: Any) -> bool:
    """识别 RSSHub 返回的"Looks like something went wrong"错误页。

    此时实例本身可用，但该路由在该实例上无数据或出错；
    返回 JSON 格式（?format=json）时可能表现为 items 为空 + 特定 title，
    或 HTML 错误页文本。
    """
    if isinstance(data, dict):
        title = str(data.get("title") or "")
        message = str(data.get("message") or "")
        text = f"{title} {message}"
        return any(marker in text for marker in (
            "Looks like something went wrong",
            "Something went wrong",
            "Internal Server Error",
            "404 Not Found",
        ))
    if isinstance(data, str):
        return "Looks like something went wrong" in data
    return False


def _rsshub_items(data: Any, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in data.get("items", []) if isinstance(data, dict) else []:
        if len(items) >= limit:
            break
        title = (item.get("title") or "").strip()
        if not title:
            continue
        published = None
        for key in ("date_published", "date_modified", "pubDate"):
            raw = item.get(key)
            if raw:
                try:
                    parsed = time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ"))
                    published = int(parsed * 1000)
                    break
                except (ValueError, TypeError):
                    try:
                        parsed = time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
                        published = int(parsed * 1000)
                        break
                    except (ValueError, TypeError):
                        continue
        items.append({
            "title": title,
            "url": item.get("url") or item.get("link") or "",
            "publishedAt": published,
            "summary": item.get("content_html") or item.get("content_text") or item.get("summary") or "",
        })
    return items


# ============ Bilibili WBI 签名 ============

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]


def _get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _enc_wbi(params: dict[str, str], img_key: str, sub_key: str) -> str:
    mixin = _get_mixin_key(img_key + sub_key)
    signed = dict(params)
    signed["wts"] = str(int(time.time()))
    query = "&".join(
        f"{_filter_param(k)}={_filter_param(v)}"
        for k, v in sorted(signed.items())
    )
    w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={w_rid}"


def _filter_param(value: str) -> str:
    return re.sub(r"[!'()*]", "", value)


async def fetch_bilibili_wbi(
    url: str,
    params: dict[str, str],
    timeout: float = 15.0,
) -> dict[str, Any]:
    """B站接口（WBI 签名）。img_key/sub_key 由 nav 接口获取并缓存。"""
    global _WBI_KEYS, _WBI_KEYS_AT
    if _WBI_KEYS is None or time.time() - _WBI_KEYS_AT > 3600:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
            headers={"User-Agent": BROWSER_UA, "Referer": "https://www.bilibili.com/"},
        ) as client:
            nav = await client.get("https://api.bilibili.com/x/web-interface/nav")
            nav.raise_for_status()
            nav_data = nav.json()
            wbi = nav_data.get("data", {}).get("wbi_img", {})
            img_url = wbi.get("img_url", "")
            sub_url = wbi.get("sub_url", "")
            _WBI_KEYS = (
                img_url[img_url.rfind("/") + 1: img_url.rfind(".")],
                sub_url[sub_url.rfind("/") + 1: sub_url.rfind(".")],
            )
            _WBI_KEYS_AT = time.time()
    img_key, sub_key = _WBI_KEYS
    query = _enc_wbi(params, img_key, sub_key)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout,
        headers={"User-Agent": BROWSER_UA, "Referer": "https://www.bilibili.com/ranking/all"},
    ) as client:
        resp = await client.get(f"{url}?{query}")
        resp.raise_for_status()
        return resp.json()


_WBI_KEYS: tuple[str, str] | None = None
_WBI_KEYS_AT: float = 0.0
