"""记忆：磁盘上的文件，不是数据库里的黑盒。

    $MUSE_HOME/
      MEMORY.md              每轮读进上下文的那份（自动生成，可直接手改）
      memory/<entity>.md     按实体分页，人可读
      log/YYYY-MM-DD.md      流水，只追加
      index.json             检索索引（可随时重建）

设计取舍：检索用"关键词 + CJK 二元组 + 稀有词加权 + 时间衰减"，不引向量库。
个人记忆量级（几千条）下够用，而且每一行都能被 cat 出来看。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid

LATIN = re.compile(r"[a-zA-Z][a-zA-Z0-9_.+-]{1,}")
CJK = re.compile(r"[\u4e00-\u9fff]+")
PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】<>《》/\\|+*=~`-]+")


def now_ms() -> int:
    return int(time.time() * 1000)


def _norm(text: str) -> str:
    return PUNCT.sub("", (text or "").lower())


def tokens(text: str) -> list[str]:
    """拉丁词 + 中文二元组。中文不切词，二元组召回足够用。"""
    out: list[str] = []
    for w in LATIN.findall(text or ""):
        out.append(w.lower())
    for run in CJK.findall(text or ""):
        if len(run) == 1:
            out.append(run)
        for i in range(len(run) - 1):
            out.append(run[i:i + 2])
    return out


class Memory:
    def __init__(self, home: str | None = None):
        self.home = os.path.expanduser(
            home or os.environ.get("MUSE_HOME") or "~/.muse")
        os.makedirs(os.path.join(self.home, "memory"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "log"), exist_ok=True)
        self.index_path = os.path.join(self.home, "index.json")
        self.entries = self._load()

    # ---------- 存储 ----------

    def _load(self) -> list[dict]:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_path)
        self._render()

    def _render(self) -> None:
        """把索引渲染成 MEMORY.md：按实体分组，只保留最近 200 行。"""
        groups: dict[str, list[dict]] = {}
        for e in self.entries:
            groups.setdefault(e.get("entity") or "未分类", []).append(e)

        lines = ["# MEMORY", "", "> 自动生成；可以直接手改，改完下次 remember 会覆盖同名实体页。", ""]
        budget = 200
        for entity, items in sorted(groups.items(), key=lambda kv: -max(x["ts"] for x in kv[1])):
            if budget <= 0:
                break
            lines.append(f"## {entity}")
            page = []
            for e in sorted(items, key=lambda x: -x["ts"]):
                if budget <= 0:
                    break
                day = time.strftime("%Y-%m-%d", time.localtime(e["ts"] / 1000))
                lines.append(f"- {e['text']}  _({day})_")
                page.append(f"- {e['text']}")
                budget -= 1
            lines.append("")
            path = os.path.join(self.home, "memory", _safe(entity) + ".md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {entity}\n\n" + "\n".join(page) + "\n")

        with open(os.path.join(self.home, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")

    # ---------- 写 ----------

    def remember(self, text: str, *, entity: str | None = None,
                 source: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("空记忆不写")
        norm = _norm(text)
        for e in self.entries:
            if _norm(e["text"]) == norm:
                e["ts"] = now_ms()
                self._save()
                return {"id": e["id"], "dup": True, "text": e["text"]}

        entry = {
            "id": uuid.uuid4().hex[:10],
            "text": text[:500],
            "entity": (entity or "未分类").strip(),
            "source": (source or "")[:200] or None,
            "ts": now_ms(),
        }
        self.entries.append(entry)
        self._save()
        self._log(entry)
        return {"id": entry["id"], "dup": False, "text": entry["text"]}

    def _log(self, entry: dict) -> None:
        day = time.strftime("%Y-%m-%d")
        line = (f"- {time.strftime('%H:%M')} **{entry['entity']}** {entry['text']}"
                + (f"  ← {entry['source']}" if entry.get("source") else "") + "\n")
        with open(os.path.join(self.home, "log", f"{day}.md"), "a", encoding="utf-8") as f:
            f.write(line)

    def forget(self, key: str) -> dict:
        """按 id 或原文子串删除。返回删掉几条。"""
        before = len(self.entries)
        self.entries = [e for e in self.entries
                        if not (e["id"] == key or key in e["text"])]
        removed = before - len(self.entries)
        if removed:
            self._save()
        return {"removed": removed}

    # ---------- 读 ----------

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        terms = tokens(query)
        if not terms:
            return self._recent(limit)
        n = max(1, len(self.entries))
        df: dict[str, int] = {}
        for e in self.entries:
            for t in set(tokens(e["text"] + " " + (e.get("entity") or ""))):
                df[t] = df.get(t, 0) + 1

        scored = []
        for e in self.entries:
            bag = set(tokens(e["text"] + " " + (e.get("entity") or "")))
            score = sum(math.log(1 + n / (1 + df.get(t, 0))) for t in terms if t in bag)
            if score <= 0:
                continue
            age_days = max(0.0, (now_ms() - e["ts"]) / 86400_000)
            score *= 1 + 0.2 * math.exp(-age_days / 30)
            scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [{**e, "score": round(s, 3)} for s, e in scored[:limit]]

    def _recent(self, limit: int) -> list[dict]:
        return [{**e, "score": 0.0}
                for e in sorted(self.entries, key=lambda x: -x["ts"])[:limit]]

    def profile(self, limit: int = 40) -> str:
        if not self.entries:
            return ""
        lines = []
        for e in sorted(self.entries, key=lambda x: -x["ts"])[:limit]:
            lines.append(f"- **{e['entity']}**：{e['text']}")
        return "\n".join(lines)

    def stats(self) -> dict:
        return {
            "home": self.home,
            "entries": len(self.entries),
            "entities": len({e.get("entity") for e in self.entries}),
            "memory_md": os.path.join(self.home, "MEMORY.md"),
        }


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff.-]+", "_", name)[:60] or "misc"