#!/usr/bin/env python3
"""主动层：在没人说话的时候，让它自己开口。

它做的事很少，但缺了它，"个人助理"就退化成"问答窗口"：

    tick() → 判断该不该说话（时间/静默/预算/去重）
           → 构建摘要（记忆 + 待批 + 今天流水）
           → 可选：先叫醒宿主跑一轮，用它的输出当正文
           → 发 card_digest

三种 waker：
    无              直接用确定性摘要（默认，零依赖，能跑）
    webhook         POST 到宿主的唤醒地址（通用）
    command         跑本地命令（claude -p / dsh / 任何 CLI）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from muse import cards, feishu  # noqa: E402
from muse.memory import Memory  # noqa: E402
from muse.store import Store  # noqa: E402

DEFAULTS = {
    "brief_at": "08:30",        # 早报时间，HH:MM
    "budget": 3,                # 每天最多主动几次
    "quiet_start": 22,          # 静默时段
    "quiet_end": 8,
    "min_gap_min": 90,          # 两次之间至少隔多久
}


def config() -> dict:
    cfg = dict(DEFAULTS)
    if os.environ.get("MUSE_BRIEF_AT"):
        cfg["brief_at"] = os.environ["MUSE_BRIEF_AT"]
    for key, env in (("budget", "MUSE_BUDGET"), ("min_gap_min", "MUSE_MIN_GAP_MIN"),
                     ("quiet_start", "MUSE_QUIET_START"),
                     ("quiet_end", "MUSE_QUIET_END")):
        if os.environ.get(env):
            cfg[key] = int(os.environ[env])
    return cfg


def in_quiet(now: datetime, cfg: dict) -> bool:
    h = now.hour
    if cfg["quiet_start"] <= cfg["quiet_end"]:
        return cfg["quiet_start"] <= h < cfg["quiet_end"]
    return h >= cfg["quiet_start"] or h < cfg["quiet_end"]


def plan(now: datetime, cfg: dict, *, last_sent_ts: int | None,
         sent_today: int, forced: bool = False) -> dict:
    """该不该说话。forced=True 是手动触发（演示用），绕过时间但保留静默/预算。"""
    if in_quiet(now, cfg) and not forced:
        return {"action": "skip", "reason": "静默时段"}

    if sent_today >= cfg["budget"]:
        return {"action": "skip", "reason": "今日主动次数已用完"}

    if last_sent_ts:
        gap_min = (now.timestamp() * 1000 - last_sent_ts) / 60_000
        if gap_min < cfg["min_gap_min"]:
            return {"action": "skip", "reason": f"距上次 {int(gap_min)} 分钟，不足间隔"}

    if not forced:
        target = _at(now, cfg["brief_at"])
        if not (target <= now < target + timedelta(minutes=5)):
            return {"action": "skip", "reason": f"还没到 {cfg['brief_at']}"}

    return {"action": "send", "reason": "手动触发" if forced else "到点"}


def _at(now: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def build_digest(memory: Memory, store: Store, now: datetime) -> dict:
    """确定性摘要：不调模型也能看，调了模型就换成它的话。"""
    recent = sorted(memory.entries, key=lambda e: -e["ts"])[:5]
    today = now.strftime("%Y-%m-%d")
    today_count = sum(1 for e in memory.entries
                      if time.strftime("%Y-%m-%d", time.localtime(e["ts"] / 1000)) == today)
    pending = store.db.execute(
        "SELECT COUNT(*) c FROM pending_actions WHERE status='pending'").fetchone()["c"]

    items: list[str] = []
    if pending:
        items.append(f"**{pending} 个动作**等你拍板（发卡后可点批准/拒绝）")
    for e in recent:
        items.append(f"{e['entity']}：{e['text'][:80]}")
    if not items:
        items = ["今天还没有新记忆。可以先跟我说说今天要推进的事。"]

    return {
        "title": "早上好" if now.hour < 12 else "今天到现在",
        "subtitle": now.strftime("%m月%d日 %H:%M"),
        "items": items,
        "metrics": [("记忆", str(len(memory.entries))),
                    ("今日新增", str(today_count)),
                    ("待批", str(pending))],
    }


# ---------------- wakers ----------------

def wake(prompt: str) -> dict:
    """叫醒宿主跑一轮，拿它的输出当摘要正文。没配置就返回空。"""
    cmd = os.environ.get("MUSE_WAKER_CMD")
    url = os.environ.get("MUSE_WAKER_URL")

    if cmd:
        try:
            out = subprocess.run(cmd, shell=True, input=prompt, capture_output=True,
                                 text=True, timeout=int(os.environ.get("MUSE_WAKER_TIMEOUT", "120")))
            text = (out.stdout or "").strip()
            return {"ok": bool(text), "text": text,
                    "error": None if text else (out.stderr or "命令没有输出").strip()[:300]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}"}

    if url:
        import urllib.request
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"prompt": prompt}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode()
            return {"ok": True, "text": body.strip(), "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": False, "text": "", "error": None}


BRIEF_PROMPT = """你是我的个人助理。现在没有人跟你说话，是你在主动开口。
基于下面的记忆摘要，用三句话告诉我：① 今天最该推进的一件事；② 有没有我答应过但还没做的事；③ 需要我拍板的。
不要复述我给你的内容，不要客套。

{memory}
"""


def tick(*, forced: bool = False, dry_run: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    cfg = config()
    db = os.environ.get("MUSE_DB") or os.path.join(
        os.environ.get("MUSE_HOME") or os.path.expanduser("~/.muse"), "muse.db")
    store = Store(db)
    memory = Memory()

    last_sent = store.get_kv("last_sent_ts")
    last_sent = int(last_sent) if last_sent else None
    day_start = int(_at(now, "00:00").timestamp() * 1000)
    sent_today = int(store.get_kv(f"sent:{now.strftime('%Y-%m-%d')}") or 0)

    decision = plan(now, cfg, last_sent_ts=last_sent, sent_today=sent_today, forced=forced)
    if decision["action"] != "send":
        return {"action": "skip", "reason": decision["reason"]}

    digest = build_digest(memory, store, now)
    woken = wake(BRIEF_PROMPT.format(memory=memory.profile(limit=20)))
    if woken.get("ok"):
        digest["items"] = [woken["text"][:1500]]
        digest["metrics"] = [("来源", "内核"), ("记忆", str(len(memory.entries)))]

    if dry_run:
        return {"action": "would_send", "card": digest, "waker": woken}

    mid = feishu.send_card(
        cards.digest_card(digest["title"], digest["items"],
                          subtitle=digest["subtitle"], metrics=digest["metrics"]),
        idempotency_key=f"brief:{now.strftime('%Y-%m-%d-%H%M')}")

    store.set_kv("last_sent_ts", int(now.timestamp() * 1000))
    store.set_kv(f"sent:{now.strftime('%Y-%m-%d')}", sent_today + 1)
    return {"action": "sent", "message_id": mid, "waker": bool(woken.get("ok"))}


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="主动层：定时让它自己开口")
    p.add_argument("--once", action="store_true", help="立刻跑一次（演示用）")
    p.add_argument("--dry-run", action="store_true", help="只打印，不发")
    p.add_argument("--loop", action="store_true", help="常驻，每分钟检查一次")
    args = p.parse_args()

    if args.once or args.dry_run:
        print(json.dumps(tick(forced=True, dry_run=args.dry_run), ensure_ascii=False, indent=2))
        return

    if args.loop:
        print("[proactive] 常驻中，每分钟检查一次", file=sys.stderr, flush=True)
        while True:
            try:
                res = tick()
                if res.get("action") == "sent":
                    print(f"[proactive] sent {res}", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[proactive] 失败: {exc}", file=sys.stderr, flush=True)
            time.sleep(60)

    p.print_help()


if __name__ == "__main__":
    main()