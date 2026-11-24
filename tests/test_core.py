"""离线单测：不碰飞书，不联网。

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from muse import approvals, cards  # noqa: E402
import proactive  # noqa: E402
from muse.memory import Memory  # noqa: E402
from muse.store import Store  # noqa: E402

OWNER = "ou_owner"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()


class MemoryTest(Base):
    def test_remember_and_dedupe(self):
        m = Memory(self.home)
        a = m.remember("Dana 是 Acme 的采购负责人", entity="Dana")
        b = m.remember("Dana 是 Acme 的采购负责人", entity="Dana")
        self.assertFalse(a["dup"])
        self.assertTrue(b["dup"])
        self.assertEqual(len(m.entries), 1)

    def test_recall_chinese(self):
        m = Memory(self.home)
        m.remember("报价按 200 席位 48k 走，周五前回复", entity="Acme")
        m.remember("我喜欢周五下午做复盘", entity="偏好")
        hits = m.recall("Acme 报价多少")
        self.assertTrue(hits)
        self.assertIn("48k", hits[0]["text"])

    def test_recall_ranks_rare_terms(self):
        m = Memory(self.home)
        m.remember("周五交付", entity="计划")
        m.remember("Dana 要求报价改成 48k", entity="Dana")
        hits = m.recall("Dana 48k")
        self.assertEqual(hits[0]["entity"], "Dana")

    def test_profile_and_md(self):
        m = Memory(self.home)
        m.remember("预算是 48k", entity="Acme")
        self.assertIn("48k", m.profile())
        md = Path(self.home, "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Acme", md)
        self.assertTrue(Path(self.home, "memory", "Acme.md").exists())

    def test_forget(self):
        m = Memory(self.home)
        rid = m.remember("临时信息", entity="杂项")["id"]
        self.assertEqual(m.forget(rid)["removed"], 1)
        self.assertEqual(len(m.entries), 0)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            Memory(self.home).remember("   ")


class CardTest(unittest.TestCase):
    def test_decision_card_shape(self):
        card = cards.decision_card("act_1", "要发吗", "正文", points=["a"], diff="> d")
        self.assertEqual(card["schema"], "2.0")
        buttons = cards._walk(card["body"]["elements"], "button")
        self.assertEqual([b["type"] for b in buttons][0], "primary_filled")
        self.assertEqual(buttons[0]["behaviors"][0]["value"]["aid"], "act_1")

    def test_single_button_must_be_filled(self):
        bad = cards.base("t", elements=[{"tag": "button", "type": "default"}])
        with self.assertRaises(ValueError):
            cards.check(bad)

    def test_digest_overflow_folds(self):
        card = cards.digest_card("日报", [f"第{i}条" for i in range(9)])
        self.assertEqual(len(cards._walk(card["body"]["elements"], "collapsible_panel")), 1)


class ApprovalTest(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(self.home, "t.db"))

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def _propose(self, **kw):
        return approvals.propose(self.store, op="mail.send", payload={"to": "x"},
                                 title="要发吗", summary="发一封邮件",
                                 operator=OWNER, **kw)

    def test_approve_then_claim_once(self):
        aid = self._propose()
        self.assertTrue(approvals.decide(self.store, aid, OWNER, "approve")["ok"])
        first = approvals.claim(self.store, aid)
        second = approvals.claim(self.store, aid)
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertTrue(second.get("cached"))

    def test_cannot_claim_before_approval(self):
        aid = self._propose()
        res = approvals.claim(self.store, aid)
        self.assertFalse(res["ok"])

    def test_wrong_operator(self):
        aid = self._propose()
        self.assertFalse(approvals.decide(self.store, aid, "ou_stranger", "approve")["ok"])

    def test_expired(self):
        aid = self._propose(ttl_min=-1)
        res = approvals.decide(self.store, aid, OWNER, "approve")
        self.assertFalse(res["ok"])
        self.assertIn("过期", res["reason"])

    def test_reject_cannot_execute(self):
        aid = self._propose()
        approvals.decide(self.store, aid, OWNER, "reject")
        self.assertFalse(approvals.claim(self.store, aid)["ok"])

    def test_wait_timeout(self):
        aid = self._propose()
        res = approvals.wait(self.store, aid, timeout_s=1)
        self.assertEqual(res["status"], "timeout")


class ServerTest(Base):
    """真跑一遍 stdio 握手，确认 stdout 是纯 JSON-RPC。"""

    def _rpc(self, payloads):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MUSE_", "FEISHU_", "LARK_"))}
        env.update(MUSE_HOME=self.home, MUSE_DB=os.path.join(self.home, "t.db"))
        proc = subprocess.run([sys.executable, str(ROOT / "server.py")],
                              input="\n".join(json.dumps(p) for p in payloads) + "\n",
                              capture_output=True, text=True, env=env, timeout=30)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_handshake_and_memory_roundtrip(self):
        out = self._rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "memory_remember",
                        "arguments": {"text": "Dana 的报价是 48k", "entity": "Dana"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "memory_recall", "arguments": {"query": "报价"}}},
        ])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "muse-mcp")
        self.assertGreaterEqual(len(out[1]["result"]["tools"]), 10)
        saved = json.loads(out[2]["result"]["content"][0]["text"])
        self.assertTrue(saved["ok"])
        hits = json.loads(out[3]["result"]["content"][0]["text"])
        self.assertEqual(hits["count"], 1)

    def test_card_tool_without_config_fails_cleanly(self):
        out = self._rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "card_notify",
                                     "arguments": {"title": "t", "body": "b"}}}])
        self.assertTrue(out[0]["result"]["isError"])
        self.assertIn("FEISHU_CHAT_ID", out[0]["result"]["content"][0]["text"])


class ProactiveTest(Base):
    """主动层：只测判断，不发卡片。"""

    def _store(self):
        return Store(os.path.join(self.home, "t.db"))

    def cfg(self):
        return {"brief_at": "08:30", "budget": 3, "quiet_start": 22,
                "quiet_end": 8, "min_gap_min": 90}

    def test_sends_at_brief_time(self):
        from datetime import datetime
        res = proactive.plan(datetime(2026, 11, 25, 8, 31), self.cfg(),
                             last_sent_ts=None, sent_today=0)
        self.assertEqual(res["action"], "send")

    def test_skips_quiet_hours(self):
        from datetime import datetime
        res = proactive.plan(datetime(2026, 11, 25, 23, 0), self.cfg(),
                             last_sent_ts=None, sent_today=0)
        self.assertEqual(res["reason"], "静默时段")

    def test_skips_when_budget_used(self):
        from datetime import datetime
        res = proactive.plan(datetime(2026, 11, 25, 8, 31), self.cfg(),
                             last_sent_ts=None, sent_today=3)
        self.assertEqual(res["action"], "skip")

    def test_skips_when_too_soon(self):
        from datetime import datetime
        last = int(datetime(2026, 11, 25, 9, 30).timestamp() * 1000)
        res = proactive.plan(datetime(2026, 11, 25, 10, 0), self.cfg(),
                             last_sent_ts=last, sent_today=1, forced=True)
        self.assertIn("间隔", res["reason"])

    def test_digest_contains_memory_and_pending(self):
        from datetime import datetime
        m = Memory(self.home)
        m.remember("Dana 的报价是 48k", entity="Dana")
        st = self._store()
        try:
            d = proactive.build_digest(m, st, datetime(2026, 11, 25, 8, 30))
            self.assertTrue(any("48k" in str(x) for x in d["items"]))
            self.assertEqual(d["metrics"][0][0], "记忆")
        finally:
            st.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)