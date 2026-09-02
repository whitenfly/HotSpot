"""AI 数据整理模块（需求四、八）。

将清洗后的数据喂给 OpenAI 兼容接口（DeepSeek/OpenAI 等），完成：
1) 分类：条目归类到领域（可多领域）
2) 对比合并：不同源报道同一事件的条目合并，数据源取并集
3) 摘要：简明扼要总结（信息量小 ≤50 字；信息量大分点，每点 ≤50 字）
4) 热度对比：不同源/领域热度标准不同，AI 统一给出可比热度并分榜
5) 结构化 JSON 输出

配置（需求八）：
- AI 接口地址 / 密钥 / 模型 / 提示词 均可通过 Web UI 与配置文件查看修改
- 自动预估输入输出 tokens，防止 maxTokens 配置过低导致输出截断、JSON 解析失败
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

import httpx

from .config import config_manager
from .models import AiItem, AiSnapshot, CleanedSnapshot, HeatLabel, now_ms

# ============ Tokens 预估（需求八：防止输出截断） ============

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def estimate_tokens(text: str) -> int:
    """粗略预估 tokens：CJK 约 1 token/字，拉丁约 1 token/4 字符，取保守系数。"""
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk * 0.9 + other * 0.28) + 8


# ============ OpenAI 兼容客户端 ============

class AIError(Exception):
    pass


async def chat_completion(
    system_prompt: str,
    user_payload: str,
    ai_cfg: dict[str, Any],
    return_raw: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], str]:
    """调用 OpenAI 兼容 /chat/completions，返回解析后的 JSON dict。

    return_raw=True 时返回 (parsed_dict, raw_content_text)，供过程追踪记录原始返回。
    """
    base_url = (ai_cfg.get("baseUrl") or "https://api.deepseek.com/v1").rstrip("/")
    api_key = ai_cfg.get("apiKey") or ""
    if not api_key:
        raise AIError("未配置 AI API 密钥，请在 Web 配置或 data/config.json 中填写")
    if any(ord(c) > 127 for c in api_key):
        raise AIError(
            "AI API 密钥包含非 ASCII 字符（如省略号 …）。"
            "这通常是因为 Web 配置页返回的是脱敏掩码值，原样保存覆盖了真实密钥。"
            "请在配置中重新粘贴完整密钥后保存。"
        )
    url = f"{base_url}/chat/completions"

    body: dict[str, Any] = {
        "model": ai_cfg.get("model", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        "temperature": float(ai_cfg.get("temperature", 0.3)),
        "max_tokens": int(ai_cfg.get("maxTokens", 8192)),
        "stream": False,
    }
    if ai_cfg.get("jsonMode", True):
        body["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = float(ai_cfg.get("timeoutSeconds", 600))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code != 200:
        raise AIError(f"AI 接口返回 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    # 解析 JSON：容忍 markdown 代码块包裹
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AIError(f"AI 输出 JSON 解析失败（可能是输出被截断）：{exc}\n---\n{content[:300]}") from exc
    if return_raw:
        return parsed, content
    return parsed


# ============ 输入构造 ============

def build_batch_input(items: list[dict], index_offset: int, enable_content: bool) -> str:
    """构造单批条目的 JSON 输入。"""
    payload: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        entry: dict[str, Any] = {
            "inputIndex": index_offset + i,
            "title": it["title"],
            "url": it.get("url", ""),
            "source": it["source"],
            "domain": it.get("domain") or "",
            "heat": it.get("heat"),
            "publishedAt": it.get("publishedAt"),
            "summary": (it.get("summary") or "")[:200],
        }
        if enable_content and not entry["summary"]:
            content = it.get("_content", "")
            if content:
                entry["content"] = content[:500]
        payload.append(entry)
    return json.dumps(payload, ensure_ascii=False)


def parse_batch_output(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """解析单批 AI 输出为 [{inputIndexes, title, url, categories, summary, heat}]。"""
    groups: list[dict[str, Any]] = []
    for item in raw.get("items", []) or []:
        indexes = item.get("inputIndexes") or item.get("input_indexes") or []
        if not isinstance(indexes, list):
            indexes = [indexes]
        indexes = [int(x) for x in indexes]
        if not indexes:
            continue
        heat = item.get("heat")
        try:
            heat = float(heat) if heat is not None else None
        except (TypeError, ValueError):
            heat = None
        groups.append({
            "inputIndexes": indexes,
            "title": (item.get("title") or "").strip(),
            "url": item.get("url") or "",
            "categories": [str(c).strip() for c in (item.get("categories") or []) if str(c).strip()],
            "summary": (item.get("summary") or "").strip(),
            "heat": heat,
        })
    return groups


def enrich_groups(
    parsed: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    index_offset: int,
    group_id_base: int,
) -> list[dict[str, Any]]:
    """为解析出的组补充 groupId/sources/rawItemIds 等（在原位 enrich）。

    group_id_base：本批第一个组的全局编号（= 已有组总数），保证跨批 groupId 全局唯一。
    """
    for g in parsed:
        g["groupId"] = f"g{group_id_base}"
        group_id_base += 1
        picked = [batch[i - index_offset] for i in g["inputIndexes"]
                  if index_offset <= i < index_offset + len(batch)]
        g["sources"] = sorted({p["source"] for p in picked})
        g["sourceNames"] = _source_names(g["sources"])
        g["rawItemIds"] = [p["id"] for p in picked]
        g["rawHeats"] = {p["source"]: p["heat"] for p in picked if p["heat"] is not None}
        published_times = [p["publishedAt"] for p in picked if p.get("publishedAt")]
        g["publishedAt"] = min(published_times) if published_times else None
    return parsed


async def process_single_batch(
    batch: list[dict[str, Any]],
    index_offset: int,
    group_id_base: int,
    ai_cfg: dict[str, Any],
    trace_entry: dict[str, Any],
) -> list[dict[str, Any]]:
    """独立重跑单个批次（需求：单批失败可单独重试）。

    - batch：该批输入条目（与第一次送入时一致的字段结构）
    - index_offset：该批在整份输入中的起始索引（保证 inputIndexes 全局编号一致）
    - group_id_base：该批第一组的全局编号（= 重试前整次整理已成功组的数量，保证 groupId 唯一）
    - trace_entry：该批的 trace 记录（原地更新 status/aiResponse/parsedGroups/error）
    """
    system_prompt = config_manager.get_prompt("system_prompt") or "你是热点数据整理专家。"
    enable_content = bool(ai_cfg.get("enableFetchContent", False))
    user_payload = build_batch_input(batch, index_offset, enable_content)
    trace_entry["sentPayload"] = user_payload
    raw, raw_text = await chat_completion(system_prompt, user_payload, ai_cfg, return_raw=True)
    parsed = parse_batch_output(raw)
    if not parsed:
        trace_entry["status"] = "error"
        trace_entry["error"] = "AI 输出中没有有效条目"
        trace_entry["aiResponse"] = raw_text
        raise AIError(f"批次 {trace_entry.get('batchIndex', '?')} AI 输出中没有有效条目")
    enrich_groups(parsed, batch, index_offset, group_id_base)
    trace_entry.update({
        "status": "ok",
        "aiResponse": raw_text,
        "parsedGroups": parsed,
        "error": None,
    })
    return parsed


def build_finalize_input(groups: list[dict[str, Any]]) -> str:
    """构造终稿合并请求：把各批结果压缩为紧凑摘要喂给 AI 做跨批合并与最终热度。"""
    payload = [
        {
            "groupId": g["groupId"],
            "title": g["title"],
            "sources": g["sources"],
            "categories": g["categories"],
            "summary": g["summary"][:120],
            "heat": g["heat"],
        }
        for g in groups
    ]
    return json.dumps(payload, ensure_ascii=False)


def parse_finalize_output(raw: dict[str, Any], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析终稿输出：合并组、最终标题/分类/摘要/热度。"""
    group_by_id = {g["groupId"]: g for g in groups}
    finals: list[dict[str, Any]] = []
    for item in raw.get("finalItems", []) or []:
        group_ids = item.get("groupIds") or []
        ids = [str(x) for x in group_ids]
        picked = [g for g in groups if g["groupId"] in ids]
        if not picked:
            continue
        heat = item.get("heat")
        try:
            heat = float(heat) if heat is not None else None
        except (TypeError, ValueError):
            heat = None
        finals.append({
            "groups": picked,
            "title": (item.get("title") or picked[0]["title"]).strip(),
            "url": item.get("url") or picked[0]["url"],
            "categories": [str(c).strip() for c in (item.get("categories") or []) if str(c).strip()]
            or _union_categories(picked),
            "summary": (item.get("summary") or picked[0]["summary"]).strip(),
            "heat": heat,
        })
    return finals


def _union_categories(groups: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for g in groups:
        for c in g.get("categories", []):
            if c not in seen:
                seen.append(c)
    return seen


# ============ 主流程 ============

async def organize(
    cleaned: CleanedSnapshot | dict,
    progress_cb: Callable[[int, str | None, str], Awaitable[None]] | None = None,
    trace: dict[str, Any] | None = None,
) -> AiSnapshot:
    """AI 整理主流程：分批 -> 批内分类/合并/摘要 -> 终稿跨批合并/统一热度 -> 榜单。

    progress_cb(progress, stage, message) 上报批处理进度（0-100），供后台任务进度条展示。
    trace（可选 dict）为过程追踪收集器：记录每批的输入条目/发送payload/AI原始返回/解析结果，
    以及终稿阶段输入输出；由调用方（pipeline）负责落盘供详情页可视化。
    """
    if trace is not None:
        trace.setdefault("status", "running")
        trace.setdefault("model", config_manager.get_ai().get("model", ""))
        trace.setdefault("batches", [])
    if isinstance(cleaned, dict):
        cleaned = CleanedSnapshot.model_validate(cleaned)
    ai_cfg = config_manager.get_ai()
    if trace is not None:
        trace["cleanedFromRunId"] = cleaned.runId
        trace.setdefault("sourceItemCount", len(cleaned.items))

    # 输入裁剪
    items = cleaned.items
    max_items = int(ai_cfg.get("maxItems", 300))
    if len(items) > max_items:
        # 按热度保留靠前者（无热度按原顺序）
        items = sorted(items, key=lambda it: it.heat if it.heat is not None else -1, reverse=True)[:max_items]

    if not items:
        if progress_cb:
            await progress_cb(100, "AI 整理", "无条目可整理")
        return AiSnapshot(
            cleanedFromRunId=cleaned.runId,
            model=ai_cfg.get("model", ""),
            sources=cleaned.sources,
        )

    system_prompt = config_manager.get_prompt("system_prompt") or "你是热点数据整理专家。"
    finalize_prompt = config_manager.get_prompt("finalize_prompt") or (
        "请对给定热点条目做跨批合并与热度归一化，输出 finalItems。"
    )
    # 记录发送给 AI 的系统提示词（每批都携带同一份 system prompt，trace 顶层存一份即可）
    if trace is not None:
        trace["systemPrompt"] = system_prompt
        trace["finalizePrompt"] = finalize_prompt

    # 分批（自适应 batchSize 防止超上下文窗口）
    batch_size = int(ai_cfg.get("batchSize", 60))
    context_window = int(ai_cfg.get("contextWindow", 65536))
    max_tokens = int(ai_cfg.get("maxTokens", 8192))
    batches: list[list[dict]] = []
    for start in range(0, len(items), batch_size):
        batch = [
            {"title": it.title, "url": it.url, "source": it.source, "domain": it.domain,
             "heat": it.heat, "summary": it.summary, "id": it.id, "publishedAt": it.publishedAt}
            for it in items[start: start + batch_size]
        ]
        batches.append(batch)

    # 校验输出 tokens 配置是否足以支撑每批（需求八：防截断）。
    # 按实际批次条目数估算，末批条目少于 batchSize 时不需要满额输出。
    est_output_per_item = 120
    max_batch_len = max(len(b) for b in batches) if batches else 0
    needed = max_batch_len * est_output_per_item
    if max_tokens < needed:
        raise AIError(
            f"AI maxTokens 配置过低：当前 {max_tokens}，估算每批最多需 {needed}。"
            f"请调大 maxTokens（建议 ≥ {int(needed * 1.3)}）或调小 batchSize。"
        )

    enable_content = bool(ai_cfg.get("enableFetchContent", False))

    groups: list[dict[str, Any]] = []
    index_offset = 0
    total_batches = len(batches)
    for batch_index, batch in enumerate(batches):
        if progress_cb:
            await progress_cb(
                int(batch_index / total_batches * 80),
                "AI 整理",
                f"分批处理 {batch_index + 1}/{total_batches}（每批 ≤{batch_size} 条）",
            )
        # 调用前先占位 trace 条目（含该批 inputOffset/sentPayload），失败也留现场供单批重试
        trace_entry: dict[str, Any] | None = None
        if trace is not None:
            trace_entry = {
                "batchIndex": batch_index + 1,
                "inputOffset": index_offset,
                "inputItemCount": len(batch),
                "inputItems": [{k: item[k] for k in ("id", "title", "url", "source", "domain", "heat", "summary", "publishedAt")
                                if k in item} for item in batch],
                "status": "running",
            }
            trace["batches"].append(trace_entry)

        user_payload = build_batch_input(batch, index_offset, enable_content)
        if trace_entry is not None:
            trace_entry["sentPayload"] = user_payload
        est_input = estimate_tokens(system_prompt) + estimate_tokens(user_payload)
        if est_input + max_tokens > context_window:
            if trace_entry is not None:
                trace_entry["status"] = "error"
                trace_entry["error"] = f"预估输入 {est_input} tokens 超过上下文窗口 {context_window}"
            raise AIError(
                f"批次 {batch_index + 1} 预估输入 {est_input} tokens + 输出 {max_tokens} "
                f"超过上下文窗口 {context_window}。请调大 contextWindow 或调小 batchSize。"
            )
        raw, raw_text = await chat_completion(system_prompt, user_payload, ai_cfg, return_raw=True)
        parsed = parse_batch_output(raw)
        if not parsed:
            if trace_entry is not None:
                trace_entry["status"] = "error"
                trace_entry["error"] = "AI 输出中没有有效条目"
                trace_entry["aiResponse"] = raw_text
            raise AIError(f"批次 {batch_index + 1} AI 输出中没有有效条目，请检查提示词与输入。")
        group_base = len(groups)
        enrich_groups(parsed, batch, index_offset, group_base)
        groups.extend(parsed)
        if trace_entry is not None:
            trace_entry.update({
                "status": "ok",
                "aiResponse": raw_text,
                "parsedGroups": parsed,
            })
        index_offset += len(batch)
        await asyncio.sleep(0.2)  # 批次间轻微间隔

    # 终稿：跨批合并 + 统一热度
    if progress_cb:
        await progress_cb(85, "AI 整理", "跨批合并与热度归一化…")
    if len(groups) > 1:
        finalize_input = build_finalize_input(groups)
        est_in = estimate_tokens(finalize_prompt) + estimate_tokens(finalize_input)
        if est_in + max_tokens > context_window:
            raise AIError(f"终稿请求预估超上下文窗口（{est_in + max_tokens} > {context_window}），请调小 maxItems 或调大 contextWindow。")
        raw_final, raw_final_text = await chat_completion(finalize_prompt, finalize_input, ai_cfg, return_raw=True)
        finals = parse_finalize_output(raw_final, groups)
        if not finals:
            raise AIError("终稿合并 AI 输出为空，请检查 finalize_prompt。")
        if trace is not None:
            trace["finalize"] = {
                "sentPayload": finalize_input,
                "aiResponse": raw_final_text,
                "parsedFinals": finals,
            }
    else:
        finals = [{
            "groups": groups,
            "title": groups[0]["title"],
            "url": groups[0]["url"],
            "categories": groups[0]["categories"],
            "summary": groups[0]["summary"],
            "heat": groups[0]["heat"],
        }]
        if trace is not None:
            trace["finalize"] = {"note": "单批处理，无需跨批合并终稿", "parsedFinals": finals}
    if progress_cb:
        await progress_cb(95, "AI 整理", "生成榜单…")
    if trace is not None:
        trace["batchCount"] = total_batches

    return build_snapshot(cleaned, ai_cfg, finals)


def build_snapshot(cleaned: CleanedSnapshot, ai_cfg: dict[str, Any], finals: list[dict[str, Any]]) -> AiSnapshot:
    """将终稿结果组装为 AiSnapshot：榜单排序、分领域榜单、热度等级。"""
    now = now_ms()

    def normalize_heat(items: list[dict]) -> None:
        heats = [f["heat"] for f in items if f["heat"] is not None]
        if not heats:
            for i, f in enumerate(items):
                f["_heat"] = max(0.0, 1.0 - i / max(len(items), 1))
            return
        lo, hi = min(heats), max(heats)
        span = hi - lo or 1.0
        for f in items:
            f["_heat"] = (f["heat"] - lo) / span if f["heat"] is not None else 0.0

    normalize_heat(finals)
    finals.sort(key=lambda f: f["_heat"], reverse=True)

    heat_values = [f["_heat"] for f in finals]

    def label(h: float) -> HeatLabel:
        pct = sum(1 for v in heat_values if v > h) / max(len(heat_values), 1)
        if pct <= 0.05:
            return "viral"
        if pct <= 0.2:
            return "top"
        if pct <= 0.4:
            return "hot"
        if pct <= 0.7:
            return "trending"
        return "normal"

    ai_items: list[AiItem] = []
    all_categories: list[str] = []
    for rank, f in enumerate(finals, start=1):
        groups = f["groups"]
        sources = sorted({s for g in groups for s in g.get("sources", [])})
        source_names = _source_names(sources)
        categories = f["categories"] or _union_categories(groups)
        raw_heats: dict[str, float] = {}
        published: list[int] = []
        raw_ids: list[str] = []
        for g in groups:
            raw_heats.update(g.get("rawHeats", {}))
            if g.get("publishedAt"):
                published.append(g["publishedAt"])
            raw_ids.extend(g.get("rawItemIds", []))
        for c in categories:
            if c not in all_categories:
                all_categories.append(c)
        ai_items.append(AiItem(
            id=f"cluster-{rank:04d}",
            title=f["title"],
            url=f["url"],
            sources=sources,
            sourceNames=source_names,
            categories=categories,
            summary=f["summary"],
            heat=round(f["_heat"], 4),
            heatLabel=label(f["_heat"]),
            rank=rank,
            rawHeats={k: round(v, 4) for k, v in raw_heats.items()},
            publishedAt=min(published) if published else None,
            updatedAt=now,
            rawItemIds=raw_ids,
            sourceCount=len(sources),
        ))

    # 榜单：总榜 + 各领域榜
    ranking: dict[str, list[str]] = {"overall": [it.id for it in ai_items]}
    category_ranks: dict[str, int] = {}
    for cat in all_categories:
        cat_items = sorted(
            [it for it in ai_items if cat in it.categories],
            key=lambda it: it.heat, reverse=True,
        )
        ranking[cat] = [it.id for it in cat_items]
        for rank, it in enumerate(cat_items, start=1):
            category_ranks[f"{it.id}:{cat}"] = rank

    for it in ai_items:
        it.categoryRanks = {cat: category_ranks.get(f"{it.id}:{cat}", 0) for cat in it.categories}

    return AiSnapshot(
        cleanedFromRunId=cleaned.runId,
        fetchedAt=now,
        generatedAt=now,
        model=ai_cfg.get("model", ""),
        total=len(ai_items),
        sourceItemCount=len(cleaned.items),
        categories=all_categories,
        items=ai_items,
        ranking=ranking,
        sources=cleaned.sources,
    )


def _source_names(source_ids: list[str]) -> list[str]:
    """将源 id 映射为展示名。"""
    sources = config_manager.get_sources()
    by_id = {s["id"]: s.get("name", s["id"]) for s in sources}
    return [by_id.get(sid, sid) for sid in source_ids]


def batch_input_from_trace(trace_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """从 trace 批次记录恢复重试所需的输入条目（字段结构与原 batch 一致）。"""
    batch: list[dict[str, Any]] = []
    for item in trace_entry.get("inputItems", []):
        batch.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "source": item.get("source", ""),
            "domain": item.get("domain"),
            "heat": item.get("heat"),
            "summary": item.get("summary"),
            "id": item.get("id", ""),
            "publishedAt": item.get("publishedAt"),
        })
    return batch


def count_ok_groups(trace: dict[str, Any], up_to_batch_index: int | None = None) -> int:
    """统计 trace 中 status=ok 批次解析出的组总数（用于重试批次的 groupId 偏移）。

    up_to_batch_index 指定时仅统计该批之前的成功批（单批重试需保持重试前编号稳定）。
    """
    total = 0
    for b in trace.get("batches", []):
        if up_to_batch_index is not None and b.get("batchIndex", 1) >= up_to_batch_index:
            continue
        if b.get("status") == "ok":
            total += len(b.get("parsedGroups") or [])
    return total


def collect_ok_groups(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """按批次顺序收集 trace 中所有成功批的解析组（供续跑终稿合并）。"""
    groups: list[dict[str, Any]] = []
    for b in trace.get("batches", []):
        if b.get("status") != "ok":
            continue
        for g in b.get("parsedGroups") or []:
            groups.append(g)
    return groups


async def finalize_groups(
    groups: list[dict[str, Any]],
    ai_cfg: dict[str, Any],
    trace_finalize: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """对已有组做终稿跨批合并（需求：重试失败批次后/对成功批次重新合并时复用）。

    trace_finalize 可选：非空时原地记录 发送payload/AI返回/解析结果 供续跑 trace。
    """
    finalize_prompt = config_manager.get_prompt("finalize_prompt") or (
        "请对给定热点条目做跨批合并与热度归一化，输出 finalItems。"
    )
    finals: list[dict[str, Any]]
    if len(groups) > 1:
        finalize_input = build_finalize_input(groups)
        max_tokens = int(ai_cfg.get("maxTokens", 8192))
        context_window = int(ai_cfg.get("contextWindow", 65536))
        est_in = estimate_tokens(finalize_prompt) + estimate_tokens(finalize_input)
        if est_in + max_tokens > context_window:
            raise AIError(f"终稿请求预估超上下文窗口（{est_in + max_tokens} > {context_window}），请调小 maxItems 或调大 contextWindow。")
        raw_final, raw_final_text = await chat_completion(finalize_prompt, finalize_input, ai_cfg, return_raw=True)
        finals = parse_finalize_output(raw_final, groups)
        if not finals:
            raise AIError("终稿合并 AI 输出为空，请检查 finalize_prompt。")
        if trace_finalize is not None:
            trace_finalize.update({
                "sentPayload": finalize_input,
                "aiResponse": raw_final_text,
                "parsedFinals": finals,
            })
    else:
        finals = [{
            "groups": groups,
            "title": groups[0]["title"],
            "url": groups[0]["url"],
            "categories": groups[0]["categories"],
            "summary": groups[0]["summary"],
            "heat": groups[0]["heat"],
        }]
        if trace_finalize is not None:
            trace_finalize.update({"note": "单组处理，无需跨批合并终稿", "parsedFinals": finals})
    return finals
