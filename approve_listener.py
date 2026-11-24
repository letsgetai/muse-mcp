#!/usr/bin/env python3
"""审批回调监听（独立进程版）。

如果你用 `python3 server.py --daemon --http 8765`，这个进程不需要单独起——
daemon 已经把调度和回调都放在一个进程里了。这个脚本留给"只想接回调"的场景。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from muse import listener  # noqa: E402
from muse.store import Store  # noqa: E402

if __name__ == "__main__":
    db = os.environ.get("MUSE_DB") or os.path.join(
        os.environ.get("MUSE_HOME") or os.path.expanduser("~/.muse"), "muse.db")
    listener.run(Store(db))
