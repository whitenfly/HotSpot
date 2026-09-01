"""HotSpot 服务入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 3456
"""

from __future__ import annotations

import os

import uvicorn

from .api import app  # noqa: F401  (uvicorn 通过模块路径加载)

if __name__ == "__main__":
    port = int(os.environ.get("HOTSPOT_PORT", "3456"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
