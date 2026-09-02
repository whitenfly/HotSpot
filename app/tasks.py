"""后台任务管理器（需求：数据获取/清洗/AI整理后台化 + 进度量化 + 多源并行）。

- 任务类型：fetch_all(全量获取) / clean / ai / run_all / fetch_source(单源获取)
- 提交后立即返回 taskId，协程在后台执行，期间用户可浏览/操作其他内容
- 进度量化：每类任务上报 0-100 进度 + 阶段/消息（Web 进度条轮询）
- 并发规则：
  - fetch_all/clean/ai/run_all 属流水线任务，同一时刻仅一个运行（pipeline_lock）
  - 单源获取 fetch_source 可多个并行；同一 source_id 仅一个（source_locks）
  - 所有写/读-改-写 raw 快照的操作经 snapshot_lock 串行，避免并发合并覆盖
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger("hotspot.tasks")

# 任务状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# 流水线任务（互斥：同一时刻只跑一个）
PIPELINE_KINDS = {"fetch_all", "clean", "ai", "run_all"}

# 任务保留数量
TASK_RETENTION = 50


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._source_locks: dict[str, asyncio.Lock] = {}
        # asyncio.Lock 需在运行中的事件循环内创建（3.10+ 在无循环时构造会异常），
        # 故惰性初始化：模块导入期不建锁，首次任务运行时必有事件循环
        self._pipeline_lock: asyncio.Lock | None = None
        self._snapshot_lock: asyncio.Lock | None = None
        self._lock: asyncio.Lock | None = None

    def _get_pipeline_lock(self) -> asyncio.Lock:
        if self._pipeline_lock is None:
            self._pipeline_lock = asyncio.Lock()
        return self._pipeline_lock

    def _get_snapshot_lock(self) -> asyncio.Lock:
        if self._snapshot_lock is None:
            self._snapshot_lock = asyncio.Lock()
        return self._snapshot_lock

    @property
    def snapshot_lock(self) -> asyncio.Lock:
        """raw 快照读改写串行锁（供 pipeline 合并写段使用）。"""
        return self._get_snapshot_lock()

    async def _get_meta_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ---------- 状态 ----------

    async def _register(self, kind: str, label: str, progress_cb: Callable | None) -> dict[str, Any]:
        lock = await self._get_meta_lock()
        async with lock:
            task_id = uuid.uuid4().hex[:12]
            entry: dict[str, Any] = {
                "taskId": task_id,
                "kind": kind,
                "label": label,
                "state": PENDING,
                "progress": 0,
                "stage": "排队中",
                "message": "",
                "startedAt": 0,
                "finishedAt": 0,
                "error": None,
                "detail": None,
            }
            self._tasks[task_id] = entry
            # 精简保留
            if len(self._tasks) > TASK_RETENTION:
                for old_id in list(self._tasks.keys())[: len(self._tasks) - TASK_RETENTION]:
                    if self._tasks[old_id]["state"] in (DONE, FAILED, CANCELLED):
                        del self._tasks[old_id]
            return entry

    def update(self, task_id: str, **fields: Any) -> None:
        entry = self._tasks.get(task_id)
        if entry:
            entry.update(fields)

    def _cb(self, task_id: str, extra: dict[str, Any] | None = None):
        """构造进度回调，返回可 await 的更新函数。"""
        async def update(progress: int, stage: str | None = None, message: str = "", **kw: Any) -> None:
            fields: dict[str, Any] = {"progress": int(progress), "message": message}
            if stage:
                fields["stage"] = stage
            fields.update(kw)
            if extra:
                fields.update(extra)
            self.update(task_id, **fields)
        return update

    # ---------- 运行包装 ----------

    async def _run(self, task_id: str, coro_factory: Callable[[Callable], Awaitable[Any]]) -> None:
        entry = self._tasks.get(task_id)
        if not entry:
            return
        self.update(task_id, state=RUNNING, startedAt=int(time.time() * 1000),
                    stage="运行中", error=None)
        cb = self._cb(task_id)
        try:
            result = await coro_factory(cb)
            self.update(task_id, state=DONE, progress=100, stage="完成",
                        finishedAt=int(time.time() * 1000), detail=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("task %s failed", task_id)
            self.update(task_id, state=FAILED, stage="失败",
                        error=f"{type(exc).__name__}: {exc}",
                        finishedAt=int(time.time() * 1000))
        finally:
            entry = self._tasks.get(task_id)
            if entry and entry["state"] in (PENDING, RUNNING):
                self.update(task_id, state=CANCELLED, finishedAt=int(time.time() * 1000))

    # ---------- 提交入口 ----------

    async def submit(
        self,
        kind: str,
        label: str,
        coro_factory: Callable[[Callable], Awaitable[Any]],
        *,
        source_id: str | None = None,
    ) -> str:
        """提交后台任务，立即返回 taskId。

        coro_factory 接收 progress_cb(progress, stage, message)，执行实际工作。
        并发规则：流水线任务整体互斥（pipeline_lock）；同一数据源的单源获取互斥
        （source_locks）；不同源的单源获取可并行——raw 快照的读改写由 pipeline 内部
        在合并写段获取 task_manager.snapshot_lock 串行（网络请求段不持锁）。
        """
        entry = await self._register(kind, label, None)
        task_id = entry["taskId"]

        async def guarded(cb: Callable) -> Any:
            if kind in PIPELINE_KINDS:
                async with self._get_pipeline_lock():
                    return await coro_factory(cb)
            if kind == "fetch_source" and source_id:
                lock = self._source_locks.setdefault(source_id, asyncio.Lock())
                async with lock:
                    return await coro_factory(cb)
            return await coro_factory(cb)

        asyncio.create_task(self._run(task_id, guarded))
        return task_id

    # ---------- 查询 ----------

    async def list_tasks(self, limit: int = 30) -> list[dict[str, Any]]:
        lock = await self._get_meta_lock()
        async with lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda t: t.get("startedAt") or t.get("finishedAt") or 0,
                reverse=True,
            )
            return tasks[:limit]

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        lock = await self._get_meta_lock()
        async with lock:
            return self._tasks.get(task_id)

    async def running_pipeline(self) -> dict[str, Any] | None:
        """返回正在运行的流水线任务（供前端禁用重复提交）。"""
        lock = await self._get_meta_lock()
        async with lock:
            for t in self._tasks.values():
                if t["kind"] in PIPELINE_KINDS and t["state"] in (PENDING, RUNNING):
                    return t
        return None

    async def running_source_fetch(self, source_id: str) -> dict[str, Any] | None:
        lock = await self._get_meta_lock()
        async with lock:
            for t in self._tasks.values():
                if (t["kind"] == "fetch_source" and t["state"] in (PENDING, RUNNING)
                        and t.get("detail") and isinstance(t["detail"], dict)
                        and t["detail"].get("sourceId") == source_id):
                    return t
        return None


task_manager = TaskManager()
