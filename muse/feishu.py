"""lark-cli 适配：发卡、更新卡、消费事件。不直连 HTTP，token 与重连交给 CLI。"""
from __future__ import annotations

import json
import os
import subprocess
import threading

LARK = os.environ.get("LARK_CLI") or "lark-cli"


class FeishuError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 90, as_: str = "bot") -> dict:
    cmd = [LARK, *args, "--as", as_]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    raw = (p.stdout or "").strip()
    start = raw.find("{")
    data = {}
    if start >= 0:
        try:
            data = json.loads(raw[start:])
        except json.JSONDecodeError:
            pass
    if p.returncode != 0 or not data.get("ok", False):
        raise FeishuError(f"lark-cli {' '.join(args)} 失败 rc={p.returncode}\n"
                          f"stdout={raw[:300]}\nstderr={(p.stderr or '')[:300]}")
    return data


def send_card(card: dict, *, chat_id: str | None = None, user_id: str | None = None,
              idempotency_key: str | None = None) -> str:
    chat_id = chat_id or os.environ.get("FEISHU_CHAT_ID")
    user_id = user_id or os.environ.get("FEISHU_USER_ID")
    if bool(chat_id) == bool(user_id):
        raise FeishuError("需要且只需要 FEISHU_CHAT_ID 或 FEISHU_USER_ID 之一")
    args = ["im", "+messages-send", "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False)]
    args += ["--chat-id", chat_id] if chat_id else ["--user-id", user_id]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key[:50]]
    data = _run(args).get("data") or {}
    mid = data.get("message_id")
    if not mid:
        raise FeishuError("发送成功但没拿到 message_id")
    return mid


def update_card(token: str, card: dict) -> None:
    """回调 token 延迟更新：30 分钟、最多 2 次。"""
    payload = json.dumps({"token": token, "card": card}, ensure_ascii=False)
    _run(["api", "POST", "/open-apis/interactive/v1/card/update", "--data", payload])


def patch_card(message_id: str, card: dict) -> None:
    """按 message_id 更新，14 天内不限次数。"""
    payload = json.dumps({"content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)
    _run(["im", "messages", "patch", "--message-id", message_id, "--data", payload])


def publish_doc(title: str, markdown: str) -> dict:
    """建云文档（bot 身份），返回 document_id 与 url。"""
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".md", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(markdown)
        data = _run(["docs", "+create", "--doc-format", "markdown",
                     "--content", f"@{path}", "--title", title])
    finally:
        os.unlink(path)
    doc = ((data.get("data") or {}).get("document")) or {}
    return {"document_id": doc.get("document_id"), "url": doc.get("url")}


class EventStream:
    """一个 EventKey 一个子进程，stderr 就绪标记后开始吐 NDJSON。"""

    READY = "[event] ready"

    def __init__(self, event_key: str, *, as_: str = "bot"):
        self.event_key = event_key
        self.ready = threading.Event()
        self.errors: list[str] = []
        self._proc = subprocess.Popen(
            [LARK, "event", "consume", event_key, "--as", as_],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._lines: list[str] = []
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        if not self.ready.wait(10):
            raise FeishuError(f"{event_key} 未就绪: {self.error_summary()}")

    def _read_out(self):
        for line in self._proc.stdout:
            if line.strip():
                self._lines.append(line)

    def _read_err(self):
        for line in self._proc.stderr:
            if self.READY in line:
                self.ready.set()
            elif line.strip():
                self.errors.append(line.rstrip())

    def error_summary(self) -> str:
        raw = "\n".join(self.errors)
        start = raw.find("{")
        if start >= 0:
            try:
                env = json.loads(raw[start:])
                err = env.get("error") or {}
                hint = err.get("hint") or ""
                return err.get("message", "未知错误") + (f"\n  处理: {hint}" if hint else "")
            except json.JSONDecodeError:
                pass
        return (raw.strip().splitlines() or ["无输出"])[-1][:300]

    def __iter__(self):
        """逐条产出事件。跳过 ready 阶段之前就在缓冲里的行。"""
        while True:
            if not self._lines:
                if self._proc.poll() is not None and not self._lines:
                    return
                threading.Event().wait(0.05)
                continue
            line = self._lines.pop(0)
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def close(self):
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            pass