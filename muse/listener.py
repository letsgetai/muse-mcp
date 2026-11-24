"""卡片回调节点：把一次按钮点击变成状态机的一次状态跳转。

被 server.py 的 daemon 线程和 approve_listener.py 共用。
"""
from __future__ import annotations

import json
import sys

from . import approvals, cards, feishu


def handle_event(ev: dict, store) -> dict:
    """纯函数式处理一条 card.action.trigger。返回处理结果，便于测试。"""
    value = ev.get("action_value") or "{}"
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {"ok": False, "reason": "action_value 不是 JSON"}

    aid = payload.get("aid", "")
    op = payload.get("op", "")
    operator = ev.get("operator_id", "")
    token = ev.get("token", "")
    verdict = {"approve": "approve", "reject": "reject", "edit": "edit"}.get(op)
    if not aid or verdict is None:
        return {"ok": False, "reason": f"无效按钮 {op}"}

    result = approvals.decide(store, aid, operator, verdict)
    row = store.get_pending(aid)

    if not result["ok"]:
        _update(token, cards.notice_card("这张卡片不能再操作", result["reason"]))
        return result

    if verdict == "reject":
        _update(token, cards.result_card("已拒绝", f"不会执行：{row['title']}"))
    elif verdict == "edit":
        _update(token, cards.notice_card(
            "需要改哪里？", f"回复一句，我改完再发你确认。\n\n当前动作：{row['title']}"))
    else:
        _update(token, cards.notice_card(
            "已批准", f"{row['title']}\n\n执行时会再核一次（card_claim），不会重复做。"))
    return result


def _update(token: str, card: dict) -> None:
    if not token:
        return
    try:
        feishu.update_card(token, card)
    except Exception as exc:  # noqa: BLE001 卡片更新失败不影响状态机
        print(f"[listener] 更新卡片失败: {exc}", file=sys.stderr, flush=True)


def run(store) -> None:
    """常驻：消费 card.action.trigger。"""
    stream = feishu.EventStream("card.action.trigger")
    print("[listener] 等待卡片回调…", file=sys.stderr, flush=True)
    for ev in stream:
        res = handle_event(ev, store)
        if res.get("ok"):
            print(f"[listener] {res.get('aid')} {res.get('status')}",
                  file=sys.stderr, flush=True)