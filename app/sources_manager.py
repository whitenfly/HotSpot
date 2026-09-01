"""数据源单文件配置管理（需求：数据源配置与其他配置分开，单文件独立管理）。

每个数据源独立存储为 data/sources/{id}.json，支持增删改查。
首次启动时自动从默认源列表生成单文件，并迁移 config.json 中的旧 sources 数组。
"""

from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from typing import Any

from .config import DATA_DIR, DEFAULT_SOURCES

SOURCES_DIR = os.path.join(DATA_DIR, "sources")
# 镜像内置模板目录（Docker 镜像中），首次启动时复制到数据卷
TEMPLATE_SOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "data_templates", "sources")

# 允许的 source id 字符集（用于文件名安全）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 数据源字段白名单（校验时剔除未知字段）
ALLOWED_FIELDS = {
    "id", "name", "type", "domain", "enabled", "limit",
    "minIntervalMinutes", "timeoutSeconds", "config", "template",
}


class SourceManagerError(Exception):
    pass


class SourceManager:
    """数据源单文件配置管理器，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        os.makedirs(SOURCES_DIR, exist_ok=True)
        self._ensure_defaults()

    # ---------- 路径与初始化 ----------

    def _path(self, source_id: str) -> str:
        return os.path.join(SOURCES_DIR, f"{source_id}.json")

    def _ensure_defaults(self) -> None:
        """首次启动/升级时保证数据源完整：内置默认源 + 镜像模板源增量补写。

        场景：
        - 全新部署（sources/ 为空）：写入 内置默认源 + 模板路由源（合并去重）
        - 升级部署（sources/ 已有旧源）：增量补写缺失的模板源（如 42 个 RSSHub 路由源），
          已存在的源不覆盖，用户自定义源不受影响
        - 旧 config.json 迁移：沿用旧 sources 数组
        """
        with self._lock:
            existing_ids = set(self._list_files())
            if not existing_ids:
                legacy = self._read_legacy_sources()
                if legacy:
                    sources = legacy
                else:
                    merged: dict[str, dict[str, Any]] = {}
                    for src in deepcopy(DEFAULT_SOURCES) + self._read_template_sources():
                        merged[src["id"]] = src
                    sources = list(merged.values())
                for source in sources:
                    self._write(source)
                return
            # 升级场景：补写模板源中缺失的源
            for src in self._read_template_sources():
                if src["id"] not in existing_ids:
                    self._write(src)

    def _read_template_sources(self) -> list[dict[str, Any]]:
        """读取镜像内置模板数据源（Docker 部署时 42 个 RSSHub 路由源）。"""
        if not os.path.isdir(TEMPLATE_SOURCES_DIR):
            return []
        sources: list[dict[str, Any]] = []
        for f in sorted(os.listdir(TEMPLATE_SOURCES_DIR)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(TEMPLATE_SOURCES_DIR, f), "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict) and data.get("id"):
                    sources.append(data)
            except Exception:  # noqa: BLE001
                continue
        return sources

    def _read_legacy_sources(self) -> list[dict[str, Any]]:
        """读取 config.json 中的旧 sources 数组（若存在则迁移）。"""
        config_file = os.path.join(DATA_DIR, "config.json")
        if not os.path.exists(config_file):
            return []
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:  # noqa: BLE001
            return []
        sources = cfg.get("sources")
        if isinstance(sources, list) and sources:
            return sources
        return []

    def _list_files(self) -> list[str]:
        if not os.path.isdir(SOURCES_DIR):
            return []
        return [f[:-5] for f in os.listdir(SOURCES_DIR) if f.endswith(".json")]

    def _read(self, source_id: str) -> dict[str, Any] | None:
        path = self._path(source_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None

    def _write(self, source: dict[str, Any]) -> None:
        path = self._path(source["id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(source, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ---------- CRUD ----------

    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            sources = []
            for sid in sorted(self._list_files()):
                data = self._read(sid)
                if data:
                    sources.append(data)
        if enabled_only:
            sources = [s for s in sources if s.get("enabled", True)]
        return sources

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(source_id)

    def save_source(self, source: dict[str, Any]) -> dict[str, Any]:
        """新增或更新数据源。返回规范化后的配置。"""
        with self._lock:
            source = self._normalize(source)
            sid = source["id"]
            if not _ID_RE.match(sid):
                raise SourceManagerError(f"非法数据源 id: {sid}（仅允许字母/数字/-/_）")
            if not source.get("name"):
                raise SourceManagerError("数据源名称不能为空")
            self._write(source)
            return deepcopy(source)

    def delete_source(self, source_id: str) -> bool:
        with self._lock:
            path = self._path(source_id)
            if os.path.exists(path):
                os.remove(path)
                return True
            return False

    def get_template(self, source_id: str) -> dict[str, Any] | None:
        source = self.get_source(source_id)
        return source.get("template") if source else None

    # ---------- 规范化 ----------

    @staticmethod
    def _normalize(source: dict[str, Any]) -> dict[str, Any]:
        """校验并规范化配置：仅保留白名单字段，补齐默认值。"""
        if not isinstance(source, dict):
            raise SourceManagerError("数据源配置必须是 JSON 对象")
        sid = source.get("id")
        if not sid:
            raise SourceManagerError("数据源缺少 id")
        sid = str(sid)
        allowed_types = {"rss", "json_get", "json_post", "html", "rsshub", "wbi_json", "google_news"}
        source_type = source.get("type")
        if source_type not in allowed_types:
            raise SourceManagerError(f"不支持的数据源类型: {source_type}（可选 {sorted(allowed_types)}）")
        normalized: dict[str, Any] = {}
        for key, value in source.items():
            if key in ALLOWED_FIELDS:
                normalized[key] = value
        normalized["id"] = sid
        normalized.setdefault("name", sid)
        normalized.setdefault("type", source_type)
        normalized.setdefault("domain", "综合")
        normalized.setdefault("enabled", True)
        normalized.setdefault("limit", 30)
        normalized.setdefault("minIntervalMinutes", 10)
        normalized.setdefault("timeoutSeconds", 15)
        normalized.setdefault("config", {})
        if "template" not in normalized:
            normalized["template"] = None
        return normalized

    def mask_api_keys(self, source: dict[str, Any]) -> dict[str, Any]:
        """返回给前端时隐藏 config 中的敏感字段（token/key/secret）。"""
        masked = deepcopy(source)
        cfg = masked.get("config")
        if isinstance(cfg, dict):
            for key in list(cfg.keys()):
                lowered = key.lower()
                if any(flag in lowered for flag in ("key", "token", "secret", "cookie", "password")):
                    value = str(cfg[key])
                    cfg[key] = value[:4] + "…" + value[-4:] if len(value) > 8 else "***"
        return masked


sources_manager = SourceManager()
