#!/usr/bin/env python3
"""Muse MCP — 给任何 agent 装上"另一个我"的四件事。

    python3 server.py                 # stdio（Aily / Claude Code / Codex / Cursor…）
    python3 server.py --http 8765     # HTTP JSON-RPC（远端宿主）

只做四件事，不做 agent：
    memory_*    记忆：写、查、画像、忘
    card_*      展示与授权：决策卡、摘要卡、告知卡，以及"批准了才执行"
    doc_publish 长内容写成云文档，卡片里只放摘要

没有这些环境变量时，记忆照常工作，卡片类工具会返回明确的错误而不是静默失败：
    MUSE_HOME              记忆目录，默认 ~/.muse
    MUSE_DB                审批账本，默认 $MUSE_HOME/muse.db
    MUSE_OWNER_OPEN_ID     只有这个人能拍板
    FEISHU_CHAT_ID         或 FEISHU_USER_ID，二选一
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from muse import approvals, cards, feishu  # noqa: E402
from muse.memory import Memory  # noqa: E402
from muse.store import Store  # noqa: E402

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "muse-mcp", "version": "0.1.0"}

_mem: Memory | None = None
_store: Store | None = None


def mem() -> Memory:
    global _mem
    if _mem is None:
        _mem = Memory()
    return _mem


def store() -> Store:
    global _store
    if _store is None:
        path = os.environ.get("MUSE_DB") or os.path.join(mem().home, "muse.db")
        _store = Store(path)
    return _store


def _owner() -> str:
    who = os.environ.get("MUSE_OWNER_OPEN_ID", "")
    if not who:
        raise RuntimeError("缺少 MUSE_OWNER_OPEN_ID：只有这个人能批准动作")
    return who


def _chat_configured() -> bool:
    return bool(os.environ.get("FEISHU_CHAT_ID") or os.environ.get("FEISHU_USER_ID"))


TOOLS = [
    {
        "name": "memory_remember",
        "description": (
            "记下一条关于用户的事实。只记稳定、以后还会用到的东西：偏好、决定、承诺、"
            "关键人物与项目的状态。不要记一次性闲聊。重复内容会自动去重。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "一句话事实，写清主体"},
                "entity": {"type": "string", "description": "归属：人名/项目/主题，默认「未分类」"},
                "source": {"type": "string", "description": "出处：哪条消息、哪次会议"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_recall",
        "description": "按关键词找相关记忆。回答用户涉及既往事实的问题前先调用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_profile",
        "description": "用户画像：最近记住的若干条事实。适合在会话开始或用户问「你记得我什么」时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 40}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_forget",
        "description": "忘掉一条记忆（按 id 或原文片段）。用户说「忘掉这个」时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "card_ask_approval",
        "description": (
            "有外部副作用的动作（发邮件、改共享文档、建日程、发群消息、花钱）先调这个。"
            "它会发一张带按钮的飞书卡片，返回 aid；用 card_wait_approval 等结果，"
            "批准后先 card_claim 拿到执行权，再真正动手。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "description": "动作名，例如 mail.send"},
                "payload": {"type": "object", "description": "执行时需要的参数"},
                "title": {"type": "string", "description": "一句话问题"},
                "summary": {"type": "string", "description": "结论式摘要，1–3 句"},
                "points": {"type": "array", "items": {"type": "string"},
                           "description": "要点，最多 3 条"},
                "diff": {"type": "string", "description": "将发生什么，折叠展示"},
                "risk": {"type": "string", "enum": ["L1", "L2", "L3"], "default": "L2"},
                "ttl_min": {"type": "integer", "default": 30},
            },
            "required": ["op", "payload", "title", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "card_approval_status",
        "description": "查一条待批准动作的状态：pending/approved/rejected/editing/executed/expired。",
        "inputSchema": {
            "type": "object",
            "properties": {"aid": {"type": "string"}},
            "required": ["aid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "card_wait_approval",
        "description": "阻塞等待用户拍板。返回 approved 才允许执行副作用；timeout 表示还没回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "aid": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 300},
            },
            "required": ["aid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "card_claim",
        "description": (
            "执行前的最后一道闸：确认已批准，并且这条动作没被执行过。"
            "claimed=true 才动手；cached=true 表示已经做过，不要再做。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"aid": {"type": "string"}},
            "required": ["aid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "card_notify",
        "description": "发一张纯告知卡（不需要用户操作）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "footer": {"type": "string"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "card_digest",
        "description": "发一张信息密度卡：一行指标 + ≤5 条要点，其余折叠。用于日报、周报、摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
                "subtitle": {"type": "string"},
                "metrics": {"type": "array", "items": {
                    "type": "array", "items": {"type": "string"}}},
            },
            "required": ["title", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "doc_publish",
        "description": "把长内容写成飞书云文档，返回链接。卡片里只放摘要，正文放文档。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "markdown": {"type": "string"},
            },
            "required": ["title", "markdown"],
            "additionalProperties": False,
        },
    },
]


def _need_chat() -> None:
    if not _chat_configured():
        raise RuntimeError("缺少 FEISHU_CHAT_ID 或 FEISHU_USER_ID，无法发卡片")


def call_tool(name: str, args: dict) -> dict:
    if name == "memory_remember":
        return {"ok": True, **mem().remember(
            args["text"], entity=args.get("entity"), source=args.get("source"))}

    if name == "memory_recall":
        hits = mem().recall(args["query"], int(args.get("limit", 5)))
        return {"ok": True, "count": len(hits), "hits": hits}

    if name == "memory_profile":
        text = mem().profile(int(args.get("limit", 40)))
        return {"ok": True, "profile": text or "（还没有记忆）", **mem().stats()}

    if name == "memory_forget":
        return {"ok": True, **mem().forget(args["key"])}

    if name == "card_ask_approval":
        _need_chat()
        owner = _owner()
        aid = approvals.propose(
            store(), op=args["op"], payload=args.get("payload") or {},
            title=args["title"], summary=args["summary"], operator=owner,
            diff=args.get("diff"), risk=args.get("risk", "L2"),
            ttl_min=int(args.get("ttl_min", 30)),
        )
        card = cards.decision_card(
            aid, args["title"], args["summary"], points=args.get("points"),
            diff=args.get("diff"), risk=args.get("risk", "L2"),
            ttl_hint=f"{int(args.get('ttl_min', 30))} 分钟内有效")
        mid = feishu.send_card(card, idempotency_key=aid)
        store().set_pending(aid, message_id=mid)
        return {"ok": True, "aid": aid, "message_id": mid,
                "next": "card_wait_approval 等结果，批准后 card_claim"}

    if name == "card_approval_status":
        cur = approvals.get(store(), args["aid"])
        return {"ok": True, **cur} if cur else {"ok": False, "reason": "没有这条动作"}

    if name == "card_wait_approval":
        return approvals.wait(store(), args["aid"], int(args.get("timeout_s", 300)))

    if name == "card_claim":
        return approvals.claim(store(), args["aid"])

    if name == "card_notify":
        _need_chat()
        mid = feishu.send_card(cards.notice_card(
            args["title"], args["body"], footer=args.get("footer", "")))
        return {"ok": True, "message_id": mid}

    if name == "card_digest":
        _need_chat()
        metrics = [tuple(m) for m in (args.get("metrics") or [])]
        mid = feishu.send_card(cards.digest_card(
            args["title"], args["items"], subtitle=args.get("subtitle", ""),
            metrics=metrics))
        return {"ok": True, "message_id": mid}

    if name == "doc_publish":
        return {"ok": True, **feishu.publish_doc(args["title"], args["markdown"])}

    raise ValueError(f"未知工具 {name}")


# ---------------- JSON-RPC ----------------

def handle(req: dict):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            payload = call_tool(name, args)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": not payload.get("ok", True)}}
        except Exception as exc:  # noqa: BLE001
            print(f"[muse-mcp] {name} 失败: {exc}", file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"未知方法 {method}"}}


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def serve_http(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok":true,"server":"muse-mcp"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            resp = handle(req)
            body = json.dumps(resp or {}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"[muse-mcp] http://127.0.0.1:{port}", file=sys.stderr, flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    p = argparse.ArgumentParser(description="Muse MCP server")
    p.add_argument("--http", type=int, default=0, help="HTTP 端口，0 表示 stdio")
    p.add_argument("--selftest", action="store_true", help="不跑 server，检查环境")
    args = p.parse_args()

    if args.selftest:
        print(json.dumps({
            "memory": mem().stats(),
            "approvals": store().stats(),
            "feishu_configured": _chat_configured(),
            "owner": bool(os.environ.get("MUSE_OWNER_OPEN_ID")),
        }, ensure_ascii=False, indent=2))
        return

    if args.http:
        serve_http(args.http)
    else:
        serve_stdio()


if __name__ == "__main__":
    main()