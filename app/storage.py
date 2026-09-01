"""存储与备份模块（需求五）。

- 每次数据获取/清洗/AI 整理后，按时间戳备份快照：data/{kind}/{runId}.json
- 最新数据单独存放：data/latest/{kind}.json，供实时调用（需求五-3）
- 复用窗口：间隔未到时读取最近一次快照，避免重复请求被风控（需求五-2）
- 快照保留策略：每种保留最近 N 份，超出自动清理
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .config import DATA_DIR, config_manager

RETENTION_PER_KIND = int(os.environ.get("HOTSPOT_RETENTION", "50"))


class Storage:
    """基于 JSON 文件的快照存储，线程安全。"""

    def __init__(self, data_dir: str = DATA_DIR) -> None:
        self.data_dir = data_dir
        self._lock = threading.RLock()
        for kind in ("raw", "cleaned", "ai", "latest", "statuses"):
            os.makedirs(os.path.join(data_dir, kind), exist_ok=True)

    # ---------- 基础读写 ----------

    def _path(self, kind: str, run_id: str) -> str:
        return os.path.join(self.data_dir, kind, f"{run_id}.json")

    def _latest_path(self, kind: str) -> str:
        return os.path.join(self.data_dir, "latest", f"{kind}.json")

    def _write(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _read(self, path: str) -> Any | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None

    # ---------- 快照保存 ----------

    def save_snapshot(self, kind: str, snapshot: Any, publish: bool = True) -> str:
        """保存快照（时间戳备份）+ 更新 latest 副本。返回 runId。"""
        with self._lock:
            data = snapshot.model_dump() if hasattr(snapshot, "model_dump") else snapshot
            run_id = data.get("runId") or time.strftime("%Y%m%d-%H%M%S")
            self._write(self._path(kind, run_id), data)
            if publish:
                self._write(self._latest_path(kind), data)
            self._prune(kind)
            return run_id

    def save_raw_extra(self, key: str, data: Any, latest: bool = True) -> None:
        """保存附加数据（如数据源可用性独立快照、对外发布载荷、运行日志）。

        latest=True 时同时写入 latest/ 目录（供实时读取）。
        """
        with self._lock:
            self._write(os.path.join(self.data_dir, "statuses", f"{key}.json"), data)
            if latest:
                self._write(self._latest_path(key), data)

    def _prune(self, kind: str) -> None:
        if kind in ("latest",):
            return
        kind_dir = os.path.join(self.data_dir, kind)
        try:
            files = sorted(
                f for f in os.listdir(kind_dir)
                if f.endswith(".json") and not f.endswith(".tmp")
            )
        except OSError:
            return
        for old in files[:-RETENTION_PER_KIND]:
            try:
                os.remove(os.path.join(kind_dir, old))
            except OSError:
                pass

    # ---------- 读取 ----------

    def load_latest(self, kind: str) -> Any | None:
        return self._read(self._latest_path(kind))

    def load_snapshot(self, kind: str, run_id: str) -> Any | None:
        return self._read(self._path(kind, run_id))

    def list_snapshots(self, kind: str) -> list[dict[str, Any]]:
        """列出某类快照（按时间倒序），含 runId/fetchedAt/itemCount。"""
        kind_dir = os.path.join(self.data_dir, kind)
        result: list[dict[str, Any]] = []
        if not os.path.isdir(kind_dir):
            return result
        for f in sorted(os.listdir(kind_dir), reverse=True):
            if not f.endswith(".json"):
                continue
            data = self._read(os.path.join(kind_dir, f))
            if not data:
                continue
            result.append({
                "runId": data.get("runId", f[:-5]),
                "fetchedAt": data.get("fetchedAt", 0),
                "itemCount": len(data.get("items", [])),
                "sources": len(data.get("sources", [])),
            })
        return result

    # ---------- 复用窗口 ----------

    def latest_within(self, kind: str, max_age_seconds: float) -> Any | None:
        """返回 max_age_seconds 内最近一次快照；超龄或缺失返回 None。

        需求五-2：一定时间内可复用，防止多次请求被风控。
        """
        data = self.load_latest(kind)
        if not data:
            return None
        fetched_at = data.get("fetchedAt") or data.get("generatedAt") or 0
        if fetched_at and (time.time() * 1000 - fetched_at) > max_age_seconds * 1000:
            return None
        return data


storage = Storage()
