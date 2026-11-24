#!/usr/bin/env python3
"""审批回调监听：把飞书卡片上的按钮变成状态机的一次状态跳转。

    python3 approve_listener.py

它会一直挂着 `lark-cli event consume card.action.trigger`。没有它，卡片上的按钮
只是装饰——点了没人接。建议用 systemd / supervisord / docker 让它常驻。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from muse import approvals, cards, feishu  # noqa: E402
from muse.store import Store  # noqa: E402

DB = os.environ.get("MUSE_DB") or os.path.join(
    os.environ.get("MUSE_HOME") or os.path.expanduser("~/.muse"), "muse.db")


def main() -> None:
    store = Store(DB)
    stream = feishu.EventStream("card.action.trigger")
    print("[listener] 等待卡片回调…", file=sys.stderr, flush=True)

    for ev in stream:
        try:
            value = ev.get("action_value") or "{}"
            payload = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            continue

        aid = payload.get("aid", "")
        op = payload.get("op", "")
        operator = ev.get("operator_id", "")
        token = ev.get("token", "")
        verdict = {"approve": "approve", "reject": "reject", "edit": "edit"}.get(op)
        if not aid or verdict is None:
            continue

        result = approvals.decide(store, aid, operator, verdict)
        row = store.get_pending(aid)

        if not result["ok"]:
            _update(token, cards.notice_card("这张卡片不能再操作", result["reason"]))
            continue

        if verdict == "reject":
            _update(token, cards.result_card("已拒绝", f"不会执行：{row['title']}"))
        elif verdict == "edit":
            _update(token, cards.notice_card(
                "需要改哪里？", f"回复一句，我改完再发你确认。\n\n当前动作：{row['title']}"))
        else:
            _update(token, cards.notice_card(
                "已批准", f"{row['title']}\n\n执行时会再核一次（card_claim），不会重复做。"))

        print(f"[listener] {aid} {verdict} -> {result}", file=sys.stderr, flush=True)


def _update(token: str, card: dict) -> None:
    if not token:
        return
    try:
        feishu.update_card(token, card)
    except Exception as exc:  # noqa: BLE001 卡片更新失败不影响状态机
        print(f"[listener] 更新卡片失败: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()