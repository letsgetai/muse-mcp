"""待批准动作的账本：SQLite，只放状态，不放记忆。

记忆是给人看的文件（见 memory.py）；这里放的是机器状态：
一条动作等谁批、批了没有、执行过没有。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pending_actions (
  aid          TEXT PRIMARY KEY,
  created_at   INTEGER NOT NULL,
  operator     TEXT NOT NULL,          -- 只允许这个 open_id 拍板
  chat_id      TEXT,
  title        TEXT NOT NULL,
  summary      TEXT NOT NULL,
  op           TEXT NOT NULL,
  payload      TEXT NOT NULL,
  diff         TEXT,
  risk         TEXT NOT NULL DEFAULT 'L2',
  status       TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|executed|expired|failed
  expires_at   INTEGER NOT NULL,
  message_id   TEXT,
  card_updates INTEGER NOT NULL DEFAULT 0,
  result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_actions(status, created_at);

CREATE TABLE IF NOT EXISTS operations (
  op_key      TEXT PRIMARY KEY,
  aid         TEXT,
  result      TEXT,
  executed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

MAX_TOKEN_UPDATES = 2


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.path = path

    def close(self) -> None:
        self.db.close()

    # ---------- pending actions ----------

    def create_pending(self, **kw) -> None:
        self.db.execute(
            "INSERT INTO pending_actions(aid, created_at, operator, chat_id, title, summary,"
            " op, payload, diff, risk, status, expires_at)"
            " VALUES (:aid,:created_at,:operator,:chat_id,:title,:summary,:op,:payload,"
            ":diff,:risk,'pending',:expires_at)",
            kw,
        )
        self.db.commit()

    def get_pending(self, aid: str):
        return self.db.execute("SELECT * FROM pending_actions WHERE aid=?", (aid,)).fetchone()

    def set_pending(self, aid: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE pending_actions SET {cols} WHERE aid=?", (*fields.values(), aid))
        self.db.commit()

    def open_pending(self, operator: str):
        return self.db.execute(
            "SELECT * FROM pending_actions WHERE operator=? AND status='pending'"
            " AND expires_at>? ORDER BY created_at DESC",
            (operator, now_ms()),
        ).fetchall()

    def expire_pending(self) -> int:
        cur = self.db.execute(
            "UPDATE pending_actions SET status='expired'"
            " WHERE status='pending' AND expires_at<=?",
            (now_ms(),),
        )
        self.db.commit()
        return cur.rowcount

    # ---------- exactly once ----------

    def get_operation(self, op_key: str):
        return self.db.execute("SELECT * FROM operations WHERE op_key=?", (op_key,)).fetchone()

    def put_operation(self, op_key: str, aid: str | None, result: dict) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO operations(op_key, aid, result, executed_at) VALUES (?,?,?,?)",
            (op_key, aid, json.dumps(result, ensure_ascii=False), now_ms()),
        )
        self.db.commit()

    # ---------- kv ----------

    def get_kv(self, key: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_kv(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )
        self.db.commit()

    def stats(self) -> dict:
        def one(sql):
            return self.db.execute(sql).fetchone()[0]

        return {
            "pending": one("SELECT COUNT(*) FROM pending_actions WHERE status='pending'"),
            "decided": one("SELECT COUNT(*) FROM pending_actions WHERE status IN"
                           " ('approved','rejected','executed','failed')"),
            "operations": one("SELECT COUNT(*) FROM operations"),
        }