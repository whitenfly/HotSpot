"""各数据源的抓取与解析适配器。

每个适配器负责：请求该源接口/页面 -> 提取通用条目 dict（title/url/heat/publishedAt/summary）。
条目最终由 dispatcher 统一包装为 RawItem。

参考 WhatsHot（alisen39/WhatsHot）的源与解析方式，并结合 Firefly 既有实现。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from bs4 import BeautifulSoup

from .base import (
    FetchError,
    fetch_bilibili_wbi,
    fetch_rsshub,
    http_get,
    http_post,
    parse_feed,
)

# 通用条目 dict 字段：title, url, heat(float|None), publishedAt(int|None), summary(str|None), extra(dict)
Entry = dict[str, Any]
FetcherFn = Callable[[dict[str, Any]], Awaitable[list[Entry]]]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if not s:
            return None
        s = s.replace(",", "").replace("万", "0000").replace("亿", "00000000")
        return float(s)
    except (ValueError, TypeError):
        return None


def _rank_heat(index: int, total: int) -> float:
    """榜单型源没有显式热度时，按名次赋热（第 1 名最高）。"""
    return float(max(1, total - index))


# ============ 微博热搜 ============

async def weibo(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"], headers=cfg.get("headers"))).json()
    band = data.get("data", {}).get("band_list", []) or []
    entries: list[Entry] = []
    for item in band[: source["limit"]]:
        word = _clean_text(item.get("word") or "")
        scheme = _clean_text(item.get("word_scheme") or "")
        title = word or scheme
        if not title:
            continue
        heat = _num(item.get("num"))
        entries.append({
            "title": title,
            "url": f"https://s.weibo.com/weibo?q={title}",
            "heat": heat,
            "publishedAt": None,
            "summary": _clean_text(item.get("note") or "") or None,
            "extra": {"rank": item.get("realpos", 0), "category": item.get("category", "")},
        })
    return entries


# ============ 百度热搜 ============

async def baidu(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for card in data.get("data", {}).get("cards", []) or []:
        for group in card.get("content", []) or []:
            for index, item in enumerate(group.get("content", []) or []):
                if len(entries) >= source["limit"]:
                    break
                title = _clean_text(item.get("word") or "")
                if not title:
                    continue
                raw_url = item.get("rawUrl") or item.get("url") or ""
                url = f"https://www.baidu.com{raw_url}" if raw_url.startswith("/") else raw_url
                heat = _num(item.get("hotScore") or item.get("hotTag"))
                entries.append({"title": title, "url": url,
                                "heat": heat if heat is not None else _rank_heat(index, 50),
                                "publishedAt": None, "summary": item.get("desc") or None, "extra": {}})
            if len(entries) >= source["limit"]:
                break
        if len(entries) >= source["limit"]:
            break
    return entries


# ============ 今日头条 ============

async def toutiao(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for item in data.get("data", []) or []:
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("Title") or "")
        if not title:
            continue
        raw_url = item.get("Url") or ""
        url = f"https://www.toutiao.com{raw_url}" if raw_url.startswith("/") else raw_url
        entries.append({"title": title, "url": url, "heat": _num(item.get("HotValue")),
                        "publishedAt": None, "summary": item.get("Label") or None, "extra": {}})
    return entries


# ============ 知乎热榜 ============

async def zhihu(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for item in data.get("data", []) or []:
        if len(entries) >= source["limit"]:
            break
        target = item.get("target") or {}
        title = _clean_text(target.get("title") or "")
        if not title:
            continue
        qid = target.get("id")
        url = (target.get("url") or f"https://www.zhihu.com/question/{qid}").replace(
            "api.zhihu.com/questions", "www.zhihu.com/question"
        )
        detail = item.get("detail_text") or ""
        m = re.match(r"([\d.]+)", detail)
        heat = float(m.group(1)) * 10000 if m else None
        created = target.get("created")
        entries.append({
            "title": title, "url": url, "heat": heat,
            "publishedAt": created * 1000 if created else None,
            "summary": _clean_text(target.get("excerpt") or "") or None,
            "extra": {},
        })
    return entries


# ============ 36氪热榜 ============

async def kr36(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    body = dict(cfg["body"])
    body["timestamp"] = int(__import__("time").time() * 1000)
    data = (await http_post(cfg["url"], body=body, timeout=source["timeoutSeconds"])).json()
    rank_list = data.get("data", {}).get("hotRankList", []) or []
    entries: list[Entry] = []
    for index, item in enumerate(rank_list):
        if len(entries) >= source["limit"]:
            break
        material = item.get("templateMaterial") or {}
        title = _clean_text(material.get("widgetTitle") or "")
        item_id = item.get("itemId")
        if not title or item_id is None:
            continue
        collect = _num(material.get("statCollect"))
        publish = item.get("publishTime") or material.get("publishTime")
        published = None
        if publish:
            published = int(publish) if int(publish) > 1e11 else int(publish) * 1000
        entries.append({
            "title": title,
            "url": f"https://www.36kr.com/p/{item_id}",
            "heat": collect if collect else _rank_heat(index, len(rank_list)),
            "publishedAt": published,
            "summary": _clean_text(material.get("widgetContent") or "") or None,
            "extra": {},
        })
    return entries


# ============ 掘金热榜 ============

async def juejin(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_post(
        cfg["url"], body=cfg["body"], timeout=source["timeoutSeconds"],
        headers={"Origin": "https://juejin.cn", "Referer": "https://juejin.cn/"},
    )).json()
    entries: list[Entry] = []
    for item in data.get("data", []) or []:
        if len(entries) >= source["limit"]:
            break
        info = item.get("article_info", {}) or {}
        title = _clean_text(info.get("title") or "")
        if not title:
            continue
        heat = _num(info.get("hot_index") or info.get("view_count"))
        published = info.get("ctime")
        entries.append({
            "title": title,
            "url": f"https://juejin.cn/post/{info.get('article_id')}",
            "heat": heat,
            "publishedAt": published * 1000 if published else None,
            "summary": _clean_text(info.get("brief_content") or "") or None,
            "extra": {},
        })
    return entries


# ============ 澎湃新闻 ============

async def thepaper(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    hot_news = data.get("data", {}).get("hotNews", []) or []
    entries: list[Entry] = []
    for index, item in enumerate(hot_news):
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("name") or "")
        cont_id = item.get("contId")
        if not title or not cont_id:
            continue
        pub = item.get("pubTimeLong")
        entries.append({
            "title": title,
            "url": f"https://www.thepaper.cn/newsDetail_forward_{cont_id}",
            "heat": _rank_heat(index, len(hot_news)),
            "publishedAt": pub if pub else None,
            "summary": None,
            "extra": {"channelName": item.get("channelName") or ""},
        })
    return entries


# ============ V2EX ============

async def v2ex(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for item in data if isinstance(data, list) else []:
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("title") or "")
        if not title:
            continue
        entries.append({
            "title": title,
            "url": item.get("url") or f"https://www.v2ex.com/t/{item.get('id')}",
            "heat": _num(item.get("replies")),
            "publishedAt": item.get("created") * 1000 if item.get("created") else None,
            "summary": _clean_text(item.get("content") or "") or None,
            "extra": {"node": (item.get("node") or {}).get("title", "")},
        })
    return entries


# ============ 虎扑 ============

async def hupu(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for item in data.get("data", {}).get("topicThreads", []) or []:
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("title") or "")
        tid = item.get("tid")
        if not title or not tid:
            continue
        entries.append({
            "title": title,
            "url": f"https://bbs.hupu.com/{tid}.html",
            "heat": _num(item.get("replies")),
            "publishedAt": item.get("createTime") * 1000 if item.get("createTime") else None,
            "summary": None,
            "extra": {"forumName": item.get("forumName") or ""},
        })
    return entries


# ============ Hacker News ============

async def hackernews(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    top = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    ids = (top if isinstance(top, list) else [])[: source["limit"]]
    entries: list[Entry] = []
    for hn_id in ids:
        try:
            item = (await http_get(
                f"https://hacker-news.firebaseio.com/v0/item/{hn_id}.json",
                timeout=source["timeoutSeconds"],
            )).json()
        except Exception:  # noqa: BLE001
            continue
        title = _clean_text(item.get("title") or "")
        if not title:
            continue
        entries.append({
            "title": title,
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id')}",
            "heat": _num(item.get("score")),
            "publishedAt": item.get("time") * 1000 if item.get("time") else None,
            "summary": _clean_text(item.get("text") or "") or None,
            "extra": {"by": item.get("by", ""), "comments": item.get("descendants", 0)},
        })
    return entries


# ============ GitHub ============

async def github(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    headers = {"Accept": "application/vnd.github+json"}
    token = source.get("config", {}).get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    import datetime

    week_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    url = cfg["url"].replace("{since}", week_ago)
    data = (await http_get(url, timeout=source["timeoutSeconds"], headers=headers)).json()
    entries: list[Entry] = []
    for item in data.get("items", []) or []:
        if len(entries) >= source["limit"]:
            break
        full_name = item.get("full_name") or ""
        if not full_name:
            continue
        entries.append({
            "title": full_name,
            "url": item.get("html_url") or f"https://github.com/{full_name}",
            "heat": _num(item.get("stargazers_count")),
            "publishedAt": None,
            "summary": _clean_text(item.get("description") or "") or None,
            "extra": {"language": item.get("language") or "", "forks": item.get("forks_count") or 0},
        })
    return entries


# ============ B站排行榜（WBI 签名） ============

async def bilibili_rank(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = await fetch_bilibili_wbi(cfg["url"], cfg.get("params", {}), timeout=source["timeoutSeconds"])
    if data.get("code") != 0:
        raise FetchError(f"[BILIBILI] 接口返回错误 code={data.get('code')} {data.get('message', '')}")
    entries: list[Entry] = []
    for item in data.get("data", {}).get("list", []) or []:
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("title") or "")
        bvid = item.get("bvid") or ""
        if not title or not bvid:
            continue
        stat = item.get("stat") or {}
        entries.append({
            "title": title,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "heat": _num(stat.get("view")),
            "publishedAt": item.get("pubdate") * 1000 if item.get("pubdate") else None,
            "summary": _clean_text(item.get("desc") or "") or None,
            "extra": {"author": item.get("owner", {}).get("name", "")},
        })
    return entries


# ============ 少数派 ============

async def sspai(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for item in data.get("data", []) or []:
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("title") or "")
        if not title:
            continue
        entries.append({
            "title": title,
            "url": f"https://sspai.com/post/{item.get('id')}",
            "heat": _num(item.get("liked_count")),
            "publishedAt": item.get("released_time") * 1000 if item.get("released_time") else None,
            "summary": _clean_text(item.get("abstract") or "") or None,
            "extra": {},
        })
    return entries


# ============ OpenAI / Anthropic 官网新闻（HTML） ============

async def _html_links(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    html = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).text
    soup = BeautifulSoup(html, "lxml")
    import re as _re

    href_filter = cfg.get("hrefFilter") or ""
    href_re = _re.compile(href_filter) if href_filter else None
    links: list[tuple[str, str]] = []
    for a in soup.select(cfg.get("selector", "a[href]")):
        title = _clean_text(a.get_text(" ", strip=True))
        href = a.get("href") or ""
        if not title or len(title) < 8 or not href:
            continue
        if href_re and not href_re.search(href):
            continue
        if href.startswith("/"):
            base = _re.match(r"(https?://[^/]+)", cfg["url"])
            href = f"{base.group(1)}{href}" if base else href
        links.append((title, href))
    seen: set[str] = set()
    entries: list[Entry] = []
    for title, href in links:
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        if len(entries) >= source["limit"]:
            break
        entries.append({"title": title, "url": href, "heat": None,
                        "publishedAt": None, "summary": None, "extra": {}})
    return entries


# ============ 通用 RSS（含新增数据源 daheiai） ============

async def rss_feed(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    xml = (await http_get(cfg["url"], timeout=source["timeoutSeconds"],
                          headers={"Accept": "application/rss+xml, application/atom+xml, application/xml"})).text
    entries: list[Entry] = []
    for item in parse_feed(xml, source["limit"]):
        entries.append({
            "title": item["title"],
            "url": item["url"],
            "heat": None,
            "publishedAt": item["publishedAt"],
            "summary": _clean_text(item["summary"]) or None,
            "extra": item["extra"],
        })
    return entries


# ============ RSSHub 源（抖音热搜等） ============

async def rsshub_feed(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    items = await fetch_rsshub(cfg["path"], source["limit"], timeout=source["timeoutSeconds"],
                               instances=cfg.get("instances"))
    entries: list[Entry] = []
    for item in items:
        entries.append({
            "title": item["title"],
            "url": item["url"],
            "heat": None,
            "publishedAt": item["publishedAt"],
            "summary": _clean_text(item["summary"]) or None,
            "extra": {},
        })
    return entries


# ============ Google News RSS 搜索代理（无官方 feed 的站点，参考 WhatsHot） ============

async def google_news_feed(source: dict[str, Any]) -> list[Entry]:
    import urllib.parse

    cfg = source["config"]
    query = cfg.get("query", "")
    locales = [(cfg.get("hl", "zh-CN"), cfg.get("gl", "CN"), cfg.get("ceid", "CN:zh-Hans"))]
    if cfg.get("hl") != "en-US":
        locales.append(("en-US", "US", "US:en"))
    for hl, gl, ceid in locales:
        url = (
            f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}"
            f"&hl={hl}&gl={gl}&ceid={ceid}"
        )
        xml = (await http_get(url, timeout=source["timeoutSeconds"])).text
        entries: list[Entry] = []
        for item in parse_feed(xml, source["limit"]):
            entries.append({
                "title": item["title"],
                "url": item["url"],
                "heat": None,
                "publishedAt": item["publishedAt"],
                "summary": _clean_text(item["summary"]) or None,
                "extra": item["extra"],
            })
        if entries:
            return entries
    return []


# ============ 网易新闻 ============

async def netease(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"])).json()
    entries: list[Entry] = []
    for item in data.get("data", {}).get("list", []) or []:
        if len(entries) >= source["limit"]:
            break
        title = _clean_text(item.get("title") or "")
        if not title:
            continue
        docid = item.get("docid") or item.get("postid") or ""
        url = item.get("url") or (f"https://www.163.com/dy/article/{docid}.html" if docid else "")
        published = None
        pub = item.get("publishTime") or item.get("ptime")
        if pub:
            try:
                import datetime

                published = int(datetime.datetime.strptime(str(pub), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            except ValueError:
                published = None
        entries.append({
            "title": title,
            "url": url,
            "heat": _num(item.get("pv") or item.get("commentCount")),
            "publishedAt": published,
            "summary": _clean_text(item.get("digest") or "") or None,
            "extra": {"sourceName": item.get("source") or ""},
        })
    return entries


# ============ 腾讯新闻 ============

async def qq_news(source: dict[str, Any]) -> list[Entry]:
    cfg = source["config"]
    data = (await http_get(cfg["url"], timeout=source["timeoutSeconds"],
                           headers={"Referer": "https://news.qq.com/"})).json()
    entries: list[Entry] = []
    for id_list in data.get("idlist", []) or []:
        for item in id_list.get("newslist", []) or []:
            if len(entries) >= source["limit"]:
                break
            title = _clean_text(item.get("title") or "")
            if not title:
                continue
            url = item.get("url") or f"https://view.inews.qq.com/a/{item.get('id')}"
            heat = _num(item.get("hotEvent") and item.get("hotEvent", {}).get("hotScore"))
            entries.append({
                "title": title,
                "url": url,
                "heat": heat,
                "publishedAt": item.get("publish_time") * 1000 if item.get("publish_time") else None,
                "summary": _clean_text(item.get("abstract") or "") or None,
                "extra": {"media": item.get("media_name") or ""},
            })
    return entries


# ============ 注册表 ============

def _mk(name: str, fn: FetcherFn) -> FetcherFn:
    return fn


REGISTRY: dict[str, FetcherFn] = {
    # 类型 -> 通用处理器
    "rss": rss_feed,
    "rsshub": rsshub_feed,
    "html": _html_links,
    # 具体源 -> 专用处理器
    "weibo": weibo,
    "baidu": baidu,
    "toutiao": toutiao,
    "zhihu": zhihu,
    "36kr": kr36,
    "netease": netease,
    "qq-news": qq_news,
    "thepaper": thepaper,
    "v2ex": v2ex,
    "hupu": hupu,
    "hackernews": hackernews,
    "github": github,
    "bilibili-rank": bilibili_rank,
    "sspai": sspai,
}

# 源 id 若在 REGISTRY 有专用处理器则使用，否则按 type 分发
TYPE_ROUTER: dict[str, FetcherFn] = {
    "rss": rss_feed,
    "rsshub": rsshub_feed,
    "html": _html_links,
    "google_news": google_news_feed,
}
