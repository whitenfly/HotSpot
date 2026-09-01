"""数据清洗模块（需求三）。

基于原始数据，做文本清洗与冗余信息去除：
- 去除 HTML 标签、转义、连续空白
- 标题归一化：去除源附加的冗余后缀/前缀（如 [r/xxx]、【视频】、更新时间戳等）
- 去重（源内按归一化标题）
- 清洗失败/无效条目剔除
输出结构化 CleanedSnapshot，为 AI 整理打基础。
"""

from __future__ import annotations

import html as html_lib
import re

from .config import config_manager
from .models import CleanedItem, CleanedSnapshot, RawSnapshot, SourceStatus, now_ms

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
# 常见冗余标记：括号内的小尾巴（来源/时间/类型标注）
BRACKET_SUFFIX_RE = re.compile(
    r"\s*[【\[\(（\[]\s*(?:视频|图文|图|多图|直播|组图|原创|转载|快讯|更新|首发|独家|荐|荐读|热)\s*[】\]\)）\]]\s*$"
)
# 标题末尾被附加的 [r/xxx] 等社区来源标记
COMMUNITY_SUFFIX_RE = re.compile(r"\s*\[r\/[^\]]+\]\s*$")
# 标题中常见噪声：URL、连续标点
URL_RE = re.compile(r"https?://\S+")
CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
CJK_PUNCT = r"\u3000-\u303f\uff00-\uffef"


def strip_html(text: str) -> str:
    text = html_lib.unescape(text)
    text = HTML_TAG_RE.sub(" ", text)
    return text


def collapse_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def clean_title(title: str) -> str:
    """标题清洗：去冗余标记与噪声，保留核心信息。"""
    t = strip_html(title)
    t = collapse_whitespace(t)
    t = COMMUNITY_SUFFIX_RE.sub("", t)
    t = BRACKET_SUFFIX_RE.sub("", t)
    # 去除纯 URL 标题
    t = URL_RE.sub("", t).strip()
    # 去除只包含标点的残留
    if re.fullmatch(f"[{CJK_PUNCT}\\s]+", t):
        return ""
    return t


def clean_summary(text: str, max_len: int) -> str | None:
    if not text:
        return None
    s = strip_html(text)
    s = collapse_whitespace(s)
    s = URL_RE.sub("", s).strip()
    if not s:
        return None
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def normalize_key(title: str) -> str:
    """归一化键：仅保留字母数字与 CJK，其余字符折叠为空格（用于去重/合并）。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf]+", " ", title).lower().strip()


def clean_snapshot(raw: RawSnapshot | dict) -> CleanedSnapshot:
    """将原始快照清洗为清洗后快照。"""
    cfg = config_manager.get_cleaning()
    if isinstance(raw, dict):
        raw = RawSnapshot.model_validate(raw)

    cleaned_items: list[CleanedItem] = []
    seen_keys: set[str] = set()

    for item in raw.items:
        title = clean_title(item.title)
        if not title and cfg.get("dropNoTitle", True):
            continue
        if len(title) > int(cfg.get("maxTitleLength", 200)):
            title = title[: int(cfg.get("maxTitleLength", 200))] + "…"

        if cfg.get("normalizeTitle", True):
            key = normalize_key(title)
            if not key:
                continue
            if cfg.get("dedupeWithinSource", True) and key in seen_keys:
                continue
            seen_keys.add(key)

        summary = clean_summary(item.summary or "", int(cfg.get("maxSummaryLength", 500)))
        cleaned_items.append(CleanedItem(
            id=item.id,
            title=title,
            url=item.url,
            source=item.source,
            sourceName=item.sourceName,
            domain=item.domain,
            heat=item.heat,
            heatUnit=item.heatUnit,
            publishedAt=item.publishedAt,
            summary=summary,
            fetchedAt=item.fetchedAt,
        ))

    return CleanedSnapshot(
        cleanedFromRunId=raw.runId,
        fetchedAt=now_ms(),
        items=cleaned_items,
        sources=raw.sources,
    )
