"""FastAPI 应用：对外 API + Web UI + 后台调度。

对外 API（供 Firefly 热点榜单模块消费）：
  GET  /api/hotspot/ai           最新 AI 整理数据（含榜单）
  GET  /api/hotspot/raw          最新原始数据预览
  GET  /api/hotspot/cleaned      最新清洗后数据预览
  GET  /api/hotspot/sources      数据源可用性
  GET  /api/hotspot/publish      对外发布载荷（Firefly 直接消费）
  GET  /api/hotspot/health       健康检查
  GET  /api/hotspot/status       运行状态

控制 API（Web UI 调试使用）：
  POST /api/hotspot/fetch / clean / ai / run-all
  GET/PUT /api/hotspot/config
  GET/PUT /api/hotspot/prompts/{name}
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import config_manager
from .models import now_ms
from .pipeline import pipeline_run_all, run_ai, run_clean, run_fetch, run_state, scheduler_loop
from .storage import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("hotspot.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="HotSpot 热点数据服务", version="1.0.0")

# 允许跨域：供 Firefly 前端直接调用（需求六）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ 数据查询 API ============

@app.get("/api/hotspot/health")
async def health():
    return {
        "status": "ok",
        "service": "hotspot",
        "version": "1.0.0",
        "time": __import__("time").time(),
    }


@app.get("/api/hotspot/status")
async def status():
    return run_state()


@app.get("/api/hotspot/raw")
async def get_raw():
    data = storage.load_latest("raw")
    if not data:
        raise HTTPException(404, "暂无原始数据，请先在 Web 页面点击【获取数据】")
    return JSONResponse(data)


@app.get("/api/hotspot/cleaned")
async def get_cleaned():
    data = storage.load_latest("cleaned")
    if not data:
        raise HTTPException(404, "暂无清洗后数据，请先执行【数据清洗】")
    return JSONResponse(data)


@app.get("/api/hotspot/ai")
async def get_ai():
    """最新 AI 整理数据（含总榜 + 各领域榜单）。Firefly 可消费此接口。"""
    data = storage.load_latest("ai")
    if not data:
        raise HTTPException(404, "暂无 AI 整理数据，请先在配置中填写 API 密钥并点击【AI 整理】")
    return JSONResponse(data)


@app.get("/api/hotspot/publish")
async def get_publish():
    """对外发布载荷（Firefly 热点榜单模块直接消费）。"""
    data = storage.load_latest("publish") or storage.load_latest("ai")
    if not data:
        raise HTTPException(404, "暂无已发布数据")
    return JSONResponse(data)


@app.get("/api/hotspot/sources")
async def get_sources():
    """数据源可用性（需求二）。

    优先返回独立可用性快照（latest/availability.json，由单源测试/获取实时更新），
    无则回退到最近 ai/raw 快照中的 sources。
    """
    availability = storage.load_latest("availability")
    if availability and availability.get("sources"):
        return JSONResponse({"updatedAt": availability.get("updatedAt", 0), "sources": availability["sources"]})
    latest = storage.load_latest("ai") or storage.load_latest("raw")
    sources = (latest or {}).get("sources", [])
    if not sources:
        sources = []
    return JSONResponse({"updatedAt": (latest or {}).get("fetchedAt", 0), "sources": sources})


# ============ 控制 API ============

@app.post("/api/hotspot/fetch")
async def api_fetch(force: bool = Body(default=False, embed=True)):
    try:
        snapshot = await run_fetch(force=force)
        return {"ok": True, "runId": snapshot.runId,
                "items": len(snapshot.items), "sources": len(snapshot.sources)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/hotspot/clean")
async def api_clean():
    try:
        cleaned = await run_clean()
        return {"ok": True, "runId": cleaned.get("runId"), "items": len(cleaned.get("items", []))}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/hotspot/ai")
async def api_ai():
    """触发 AI 整理：自动存储 + 发布到 API（需求六-2）。"""
    try:
        result = await run_ai()
        return {"ok": True, "runId": result.get("runId"), "total": result.get("total")}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/hotspot/run-all")
async def api_run_all(force: bool = Body(default=False, embed=True)):
    try:
        await pipeline_run_all(force=force)
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ============ 配置与提示词 API ============

# 密钥脱敏占位符：GET /config 返回该值，PUT 时检测到则保留原有密钥，避免掩码回写破坏密钥
MASKED_KEY_SENTINEL = "__HOTSPOT_MASKED__"


def _mask_config(cfg: dict) -> dict:
    masked = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    ai = masked.get("ai")
    if ai and ai.get("apiKey"):
        key = str(ai["apiKey"])
        ai["apiKey"] = key[:4] + "…" + key[-4:] if len(key) > 8 else "***"
    return masked


def _preserve_real_key(base: dict, incoming: dict) -> dict:
    """PUT 配置时保护 API 密钥：incoming 为脱敏值/占位符时，沿用已保存的真实密钥。

    修复：Web 页面「配置」标签页展示的是打码后的密钥，若用户原样保存，掩码值
    会覆盖真实密钥，导致 AI 请求携带含 U+2026（…）的 Authorization 头而触发
    ascii 编码错误。
    """
    ai_in = incoming.get("ai")
    if not isinstance(ai_in, dict) or not isinstance(base.get("ai"), dict):
        return incoming
    incoming_key = ai_in.get("apiKey")
    real_key = base["ai"].get("apiKey", "")
    if incoming_key in (MASKED_KEY_SENTINEL, "") or (isinstance(incoming_key, str) and "…" in incoming_key):
        ai_in["apiKey"] = real_key
    return incoming


@app.get("/api/hotspot/config")
async def get_config():
    cfg = config_manager.get()
    ai = cfg.get("ai", {})
    # 返回脱敏配置（密钥打码，便于页面展示）
    return JSONResponse(_mask_config(cfg))


@app.put("/api/hotspot/config")
async def put_config(payload: dict = Body(...)):
    try:
        # 保护密钥：若提交的是脱敏掩码值，则沿用已保存的真实密钥
        current = config_manager.get()
        payload = _preserve_real_key(current, payload)
        cfg = config_manager.update(payload)
        return {"ok": True, "config": _mask_config(cfg)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/hotspot/prompts")
async def list_prompts():
    return {"prompts": config_manager.list_prompts()}


@app.get("/api/hotspot/prompts/{name}")
async def get_prompt(name: str):
    content = config_manager.get_prompt(name)
    if content is None or (not content and name not in ("system_prompt", "finalize_prompt")):
        raise HTTPException(404, "提示词不存在")
    return PlainTextResponse(content)


@app.put("/api/hotspot/prompts/{name}")
async def put_prompt(name: str, request: Request):
    content = (await request.body()).decode("utf-8")
    try:
        config_manager.save_prompt(name, content)
        return {"ok": True}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ============ 数据源管理 API（单文件配置 / 测试 / 单独获取） ============

from .sources_manager import SourceManagerError, sources_manager


@app.get("/api/hotspot/sources/config")
async def list_source_configs():
    """数据源配置列表（含解析模板；config 中敏感字段脱敏）。"""
    sources = sources_manager.list_sources()
    return {"sources": [sources_manager.mask_api_keys(s) for s in sources]}


@app.get("/api/hotspot/sources/config/{source_id}")
async def get_source_config(source_id: str):
    source = sources_manager.get_source(source_id)
    if source is None:
        raise HTTPException(404, "数据源不存在")
    return sources_manager.mask_api_keys(source)


@app.post("/api/hotspot/sources/config")
async def create_source_config(payload: dict = Body(...)):
    """新增数据源（单文件配置）。"""
    try:
        source = sources_manager.save_source(payload)
        return {"ok": True, "source": sources_manager.mask_api_keys(source)}
    except SourceManagerError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.put("/api/hotspot/sources/config/{source_id}")
async def update_source_config(source_id: str, payload: dict = Body(...)):
    """更新数据源配置。"""
    try:
        existing = sources_manager.get_source(source_id)
        if existing is None:
            raise HTTPException(404, "数据源不存在")
        # 保留 id 一致；config 中 token/密钥脱敏值回写时沿用原值
        payload = _restore_source_secrets(existing, payload)
        source = sources_manager.save_source(payload)
        return {"ok": True, "source": sources_manager.mask_api_keys(source)}
    except SourceManagerError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.delete("/api/hotspot/sources/config/{source_id}")
async def delete_source_config(source_id: str):
    if sources_manager.delete_source(source_id):
        return {"ok": True}
    raise HTTPException(404, "数据源不存在")


@app.post("/api/hotspot/sources/config/{source_id}/toggle")
async def toggle_source_config(source_id: str):
    """即时启用/禁用数据源（需求3：卡片上的开关，不进编辑表单）。

    禁用：从最新原始快照剔除该源数据（不进入后续清洗/AI）并备份该源数据；
    启用：从备份恢复该源数据（若备份获取时间不早于现有数据则复用）。
    """
    from .pipeline import exclude_source, restore_source

    source = sources_manager.get_source(source_id)
    if source is None:
        raise HTTPException(404, "数据源不存在")
    was_enabled = source.get("enabled", True)
    new_enabled = not was_enabled
    source["enabled"] = new_enabled
    updated = sources_manager.save_source(source)

    result: dict = {"ok": True, "enabled": updated.get("enabled", True),
                    "source": sources_manager.mask_api_keys(updated)}
    try:
        if new_enabled and not was_enabled:
            restored = restore_source(source_id)
            result["restored"] = True if restored else False
            result["restoredFromBackup"] = restored is not None
        elif was_enabled and not new_enabled:
            excluded = exclude_source(source_id)
            result["excluded"] = True if excluded else False
            result["backedUp"] = bool(
                storage.load_disabled_source(source_id) if excluded else False
            )
    except Exception as exc:  # noqa: BLE001 - 数据剔除/恢复失败不阻断启停
        result["dataError"] = f"{type(exc).__name__}: {exc}"
    return result


@app.post("/api/hotspot/sources/test/{source_id}")
async def test_source_endpoint(source_id: str):
    """手动测试数据源联通与数据接收（返回结构化预览供页面可视化）。

    测试完成后同步更新数据源可用性列表（需求：单独测试后也要更新源可用性）。
    """
    from .fetchers import test_source

    source = sources_manager.get_source(source_id)
    if source is None:
        raise HTTPException(404, "数据源不存在")
    result = await test_source(source)
    # 将测试结果写入可用性快照
    from .models import SourceStatus as _Status

    status = _Status(
        source=source_id,
        sourceName=source.get("name", source_id),
        connected=result.get("connected", False),
        itemCount=len(result.get("itemsPreview", [])),
        fetchedAt=now_ms(),
        durationMs=result.get("durationMs", 0),
        skipped=False,
        error=result.get("error"),
    )
    from .pipeline import update_source_availability

    update_source_availability(None, status=status)
    return result


@app.post("/api/hotspot/sources/fetch/{source_id}")
async def fetch_source_endpoint(source_id: str, force: bool = Body(default=True, embed=True)):
    """单源单独获取，并与最近一次快照部分合并（只更新该源数据部分）。"""
    from .pipeline import run_fetch_source

    try:
        snapshot = await run_fetch_source(source_id, force=force)
        return {
            "ok": True,
            "runId": snapshot.runId,
            "items": len(snapshot.items),
            "mergedSource": source_id,
            "partial": snapshot.extra.get("partial", False),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def _restore_source_secrets(existing: dict, incoming: dict) -> dict:
    """更新数据源配置时保护密钥类字段：incoming 为脱敏掩码值则沿用原值。"""
    if not isinstance(incoming, dict):
        return incoming
    incoming_cfg = incoming.get("config")
    existing_cfg = existing.get("config") or {}
    if isinstance(incoming_cfg, dict) and isinstance(existing_cfg, dict):
        for key, new_value in list(incoming_cfg.items()):
            if isinstance(new_value, str) and ("…" in new_value or new_value == "***"):
                if key in existing_cfg:
                    incoming_cfg[key] = existing_cfg[key]
    return incoming


# ============ RSSHub 全局实例管理 API（需求2） ============

from .rsshub_manager import rsshub_instances


@app.get("/api/hotspot/rsshub/instances")
async def list_rsshub_instances():
    return {"instances": rsshub_instances.list_instances()}


@app.put("/api/hotspot/rsshub/instances")
async def update_rsshub_instances(payload: dict = Body(...)):
    """整体更新实例列表（Web 页面编辑导入官方实例清单）。"""
    instances = payload.get("instances", [])
    if not isinstance(instances, list):
        return JSONResponse({"ok": False, "error": "instances 必须是数组"}, status_code=400)
    updated = rsshub_instances.update_instances(instances)
    return {"ok": True, "instances": updated}


@app.post("/api/hotspot/rsshub/test/{url:path}")
async def test_rsshub_instance(url: str):
    """测试单个 RSSHub 实例的 online 状态（探测根路径连通性）。"""
    import httpx as _httpx

    base = url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    result = {"url": base, "online": False, "error": None, "durationMs": 0}
    import time as _time

    started = _time.time()
    try:
        async with _httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 HotSpot/1.0"},
        ) as client:
            resp = await client.get(base)
        ok = resp.status_code < 500  # 2xx/3xx/4xx 均可视为在线（4xx 说明服务器在响应）
        result["online"] = ok
        result["statusCode"] = resp.status_code
        if not ok:
            result["error"] = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["durationMs"] = int((_time.time() - started) * 1000)
    rsshub_instances.set_online(base, result["online"], result["error"])
    return result


# ============ 历史快照 API ============

@app.get("/api/hotspot/history/{kind}")
async def history(kind: str):
    if kind not in ("raw", "cleaned", "ai"):
        raise HTTPException(400, "kind 必须为 raw/cleaned/ai")
    return {"snapshots": storage.list_snapshots(kind)}


# ============ Web UI ============

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ============ 启动 ============

@app.on_event("startup")
async def startup():
    config_manager.load()
    # 同步一次最新发布（若已有 AI 数据）
    from .pipeline import publish_latest

    publish_latest()
    asyncio.create_task(scheduler_loop())
