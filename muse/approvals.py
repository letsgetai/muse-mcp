"""待批准动作的状态机。

不变量：
  1. 只有登记时指定的 open_id 能拍板；
  2. 过期就作废，迟到的一律拒绝；
  3. 同一个 aid 真正执行只放行一次（claim 返回 cached 表示别再做一遍）。
"""
from __future__ import annotations

import json
import time
import uuid

MAX_CARD_UPDATES = 2


def now_ms() -> int:
    return int(time.time() * 1000)


def new_aid() -> str:
    return "act_" + uuid.uuid4().hex[:12]


def propose(store, *, op: str, payload: dict, title: str, summary: str,
            operator: str, chat_id: str | None = None, diff: str | None = None,
            risk: str = "L2", ttl_min: int = 30) -> str:
    aid = new_aid()
    store.create_pending(
        aid=aid, created_at=now_ms(), operator=operator, chat_id=chat_id,
        title=title, summary=summary, op=op,
        payload=json.dumps(payload, ensure_ascii=False),
        diff=diff, risk=risk, expires_at=now_ms() + int(ttl_min) * 60_000,
    )
    return aid


def get(store, aid: str) -> dict | None:
    row = store.get_pending(aid)
    if row is None:
        return None
    return {
        "aid": row["aid"], "status": row["status"], "title": row["title"],
        "op": row["op"], "risk": row["risk"],
        "expires_in_s": max(0, int((row["expires_at"] - now_ms()) / 1000)),
        "message_id": row["message_id"],
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def decide(store, aid: str, operator: str, verdict: str) -> dict:
    row = store.get_pending(aid)
    if row is None:
        return {"ok": False, "reason": "找不到这条动作"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"当前状态 {row['status']}，不能再处理"}
    if row["operator"] != operator:
        return {"ok": False, "reason": "不是发起人，无权处理"}
    if row["expires_at"] <= now_ms():
        store.set_pending(aid, status="expired")
        return {"ok": False, "reason": "已过期，请重新发起"}

    status = {"approve": "approved", "reject": "rejected", "edit": "editing"}.get(verdict)
    if status is None:
        return {"ok": False, "reason": f"未知动作 {verdict}"}
    store.set_pending(aid, status=status)
    return {"ok": True, "aid": aid, "status": status}


def claim(store, aid: str) -> dict:
    """执行前调用。同一个 aid 只有第一次返回 claimed=True。"""
    row = store.get_pending(aid)
    if row is None:
        return {"ok": False, "reason": "找不到这条动作"}
    if row["status"] not in ("approved", "executed"):
        return {"ok": False, "reason": f"状态 {row['status']} 不可执行"}
    op_key = f"{row['op']}:{aid}"
    done = store.get_operation(op_key)
    if done is not None:
        return {"ok": True, "claimed": False, "cached": True,
                "result": json.loads(done["result"])}
    store.put_operation(op_key, aid, {"claimed": True, "at": now_ms()})
    store.set_pending(aid, status="executed", card_updates=0)
    return {"ok": True, "claimed": True, "op_key": op_key}


def claim_card_update(store, aid: str) -> None:
    row = store.get_pending(aid)
    used = int(row["card_updates"] or 0) if row else 0
    if used >= MAX_CARD_UPDATES:
        raise RuntimeError("回调 token 更新次数用尽，改用消息 patch")
    store.set_pending(aid, card_updates=used + 1)


def wait(store, aid: str, timeout_s: int = 300, poll_s: float = 2.0) -> dict:
    deadline = time.time() + max(1, int(timeout_s))
    while time.time() < deadline:
        cur = get(store, aid)
        if cur is None:
            return {"ok": False, "reason": "找不到这条动作"}
        if cur["status"] != "pending":
            return {"ok": True, **cur}
        time.sleep(poll_s)
    cur = get(store, aid) or {}
    return {"ok": True, **cur, "aid": aid, "status": "timeout"}


def sweep(store) -> int:
    return store.expire_pending()