"""兼容启动入口（v1 服务已归档到 legacy/server_v1.py）。

工作台现在只有一套后端：app.py（FastAPI）。
保留本文件是为了不破坏 `python server.py` 这个旧命令。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import runtime

runtime.bootstrap()


def main() -> None:
    print("[deprecated] server.py 是 v1 兼容入口，实际启动的是 app.py（FastAPI）。")
    from app import app
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8900")),
                log_level="warning")


if __name__ == "__main__":
    main()
