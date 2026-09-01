"""RSSHub 全局实例管理器（需求：RSSHub 全局实例源管理）。

- 公共实例列表持久化于 data/rsshub_instances.json
- 每个实例含 url/name/location/maintainer/online
- online 由 Web 页面的「测试」操作写入（探测实例根路径，返回 2xx 且可访问即 online）
- 抓取 RSSHub 路由时，优先使用 online 的实例，并按序故障转移
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

from .config import DATA_DIR

INSTANCES_FILE = os.path.join(DATA_DIR, "rsshub_instances.json")
# 路由级"成功实例"记忆：{path: {"instance": url, "updatedAt": ms}}
SUCCESS_FILE = os.path.join(DATA_DIR, "rsshub_success.json")
# 镜像内置模板（Docker 镜像中 19 个公共实例清单），首次启动复制到数据卷
TEMPLATE_INSTANCES_FILE = os.path.join(os.path.dirname(__file__), "..", "data_templates", "rsshub_instances.json")

# 默认实例（首次启动写入；official 实例列表可被 Web 页面编辑替换）
DEFAULT_INSTANCES: list[dict[str, Any]] = [
    {"url": "https://rsshub.app", "name": "官方实例", "location": "美国", "maintainer": "RSSHub 团队", "online": False},
    {"url": "https://rsshub.rssforever.com", "name": "rssforever", "location": "美国", "maintainer": "RSSHub 团队", "online": False},
    {"url": "https://rsshub.fatpandac.com", "name": "FatPanda", "location": "香港", "maintainer": "RSSHub 团队", "online": False},
    {"url": "https://rsshub.liumingye.cn", "name": "Liumingye", "location": "香港", "maintainer": "RSSHub 团队", "online": False},
    {"url": "https://rsshub.moeyy.xyz", "name": "Moeyy", "location": "中国大陆", "maintainer": "RSSHub 团队", "online": False},
]


class RsshubInstanceManager:
    """RSSHub 实例配置管理器，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not os.path.exists(INSTANCES_FILE):
            template = self._read_template()
            self.save(template if template else deepcopy(DEFAULT_INSTANCES))
            return
        # 升级场景：现有文件存在，若实例数少于模板则增量补齐（保留已有 online 状态）
        template = self._read_template()
        if not template:
            return
        current = self.list_instances()
        if len(current) >= len(template):
            return
        current_urls = {i.get("url") for i in current}
        merged = []
        for t in template:
            existing = next((c for c in current if c.get("url") == t["url"]), None)
            if existing:
                merged.append({**t, "online": existing.get("online", False),
                               "lastError": existing.get("lastError")})
            else:
                merged.append(t)
        for c in current:
            if c.get("url") not in {t["url"] for t in template}:
                merged.append(c)  # 保留用户自定义实例
        self.save(merged)

    def _read_template(self) -> list[dict[str, Any]] | None:
        if not os.path.exists(TEMPLATE_INSTANCES_FILE):
            return None
        try:
            with open(TEMPLATE_INSTANCES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("instances", [])
            return data if isinstance(data, list) and data else None
        except Exception:  # noqa: BLE001
            return None

    def save(self, instances: list[dict[str, Any]]) -> None:
        with self._lock:
            tmp = INSTANCES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(instances, f, ensure_ascii=False, indent=2)
            os.replace(tmp, INSTANCES_FILE)

    def list_instances(self) -> list[dict[str, Any]]:
        with self._lock:
            if not os.path.exists(INSTANCES_FILE):
                return deepcopy(DEFAULT_INSTANCES)
            try:
                with open(INSTANCES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data = data.get("instances", [])
                return data if isinstance(data, list) else []
            except Exception:  # noqa: BLE001
                return deepcopy(DEFAULT_INSTANCES)

    def update_instances(self, instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """整体更新实例列表（Web 页面编辑）。规范化字段并落盘。"""
        normalized: list[dict[str, Any]] = []
        for inst in instances:
            if not isinstance(inst, dict) or not inst.get("url"):
                continue
            normalized.append({
                "url": str(inst["url"]).rstrip("/"),
                "name": inst.get("name", str(inst["url"]).rstrip("/")),
                "location": inst.get("location", ""),
                "maintainer": inst.get("maintainer", ""),
                "online": bool(inst.get("online", False)),
            })
        self.save(normalized)
        return normalized

    def set_online(self, url: str, online: bool, error: str | None = None) -> None:
        """更新单实例 online 状态（测试后写回）。"""
        with self._lock:
            instances = self.list_instances()
            for inst in instances:
                if inst.get("url") == url.rstrip("/"):
                    inst["online"] = online
                    if error:
                        inst["lastError"] = error
                    else:
                        inst.pop("lastError", None)
            self.save(instances)

    def online_urls(self) -> list[str]:
        return [i["url"] for i in self.list_instances() if i.get("online")]

    def list_instance_urls(self) -> list[str]:
        """全部实例 URL（含未测试的），按配置顺序。"""
        return [i["url"] for i in self.list_instances()]

    # ---------- 路由级成功实例记忆 ----------

    def get_success_instance(self, path: str) -> str | None:
        """返回某路由上次成功抓取的实例 URL；无记忆或实例已被删除时返回 None。"""
        with self._lock:
            if not os.path.exists(SUCCESS_FILE):
                return None
            try:
                with open(SUCCESS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001
                return None
            entry = data.get(path) if isinstance(data, dict) else None
            if not entry or not entry.get("instance"):
                return None
            url = str(entry["instance"]).rstrip("/")
            # 记忆的实例若已不在实例清单中则失效
            if url not in {i["url"] for i in self.list_instances()}:
                return None
            return url

    def set_success_instance(self, path: str, instance_url: str) -> None:
        """记录某路由成功抓取的实例（测试联通 / 单独获取 / 全量获取成功后写入）。"""
        import time

        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(SUCCESS_FILE):
                try:
                    with open(SUCCESS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:  # noqa: BLE001
                    data = {}
            if not isinstance(data, dict):
                data = {}
            data[path] = {"instance": instance_url.rstrip("/"), "updatedAt": int(time.time() * 1000)}
            tmp = SUCCESS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SUCCESS_FILE)

    def clear_success_instance(self, path: str) -> None:
        """清除某路由的成功实例记忆（该实例抓取失败时调用，避免每次都先打失败实例）。"""
        with self._lock:
            if not os.path.exists(SUCCESS_FILE):
                return
            try:
                with open(SUCCESS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001
                return
            if isinstance(data, dict) and path in data:
                data.pop(path, None)
                tmp = SUCCESS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, SUCCESS_FILE)


rsshub_instances = RsshubInstanceManager()
