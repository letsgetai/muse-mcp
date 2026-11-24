"""飞书卡片 2.0 构造器 + 发送前自检。

四类卡：决策（要人拍板）、摘要（信息密度）、结果（做完回报）、告知。
硬规则：一屏一焦点；结论在前；全文折叠；按钮只携带 {op, aid}。
"""
from __future__ import annotations

import json

MAX_ELEMENTS = 200


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _btn(label: str, value: dict, kind: str = "default", width: str | None = None) -> dict:
    btn = {"tag": "button",
           "text": {"tag": "plain_text", "content": label},
           "type": kind,
           "behaviors": [{"type": "callback", "value": value}]}
    if width:
        btn["width"] = width
    return btn


def _columns(*groups: list[dict], weights: list[str] | None = None) -> dict:
    cols = []
    for i, els in enumerate(groups):
        col = {"tag": "column", "elements": els}
        col["width"] = weights[i] if weights else "auto"
        cols.append(col)
    return {"tag": "column_set", "flex_mode": "none",
            "horizontal_spacing": "8px", "columns": cols}


def _panel(title: str, elements: list[dict], expanded: bool = False) -> dict:
    return {"tag": "collapsible_panel", "expanded": expanded,
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def base(title: str, *, subtitle: str = "", template: str = "blue",
         tags: list[str] | None = None, width_mode: str = "default",
         elements: list[dict] | None = None) -> dict:
    header = {"title": {"tag": "plain_text", "content": title}, "template": template}
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    if tags:
        palette = ["blue", "green", "orange", "grey", "turquoise", "violet"]
        header["text_tag_list"] = [
            {"tag": "text_tag", "text": {"tag": "plain_text", "content": t},
             "color": palette[i % len(palette)]} for i, t in enumerate(tags)
        ]
    return {"schema": "2.0",
            "config": {"update_multi": True, "width_mode": width_mode},
            "header": header,
            "body": {"direction": "vertical", "elements": elements or []}}


def decision_card(aid: str, title: str, summary: str, *, points=None,
                  diff: str | None = None, risk: str = "L2",
                  ttl_hint: str = "30 分钟内有效") -> dict:
    els: list[dict] = [_md(summary)]
    if points:
        els.append(_md("\n".join(f"- {p}" for p in points)))
    if diff:
        els.append(_panel("将发生什么", [_md(diff)]))
    els.append(_columns(
        [_btn("批准", {"op": "approve", "aid": aid}, "primary_filled")],
        [_btn("改一下", {"op": "edit", "aid": aid}, "default")],
        [_btn("拒绝", {"op": "reject", "aid": aid}, "danger_text")],
    ))
    card = base(title, subtitle=f"{risk} · {ttl_hint}", template="blue",
                tags=[risk], elements=els)
    check(card)
    return card


def digest_card(title: str, items: list[str], *, subtitle: str = "",
                metrics: list[tuple[str, str]] | None = None) -> dict:
    els: list[dict] = []
    if metrics:
        els.append(_columns(*[[_md(f"**{v}**\n<font color='grey'>{k}</font>")]
                             for k, v in metrics]))
    head, rest = items[:5], items[5:]
    if head:
        els.append(_md("\n".join(f"{i + 1}. {x}" for i, x in enumerate(head))))
    if rest:
        els.append(_panel(f"还有 {len(rest)} 条", [_md("\n".join(f"- {x}" for x in rest))]))
    card = base(title, subtitle=subtitle, template="turquoise", tags=["摘要"], elements=els)
    check(card)
    return card


def result_card(title: str, body: str, *, ok: bool = True,
                detail: str | None = None) -> dict:
    els = [_md(body)]
    if detail:
        els.append(_panel("详情", [_md(detail)]))
    card = base(title, template="green" if ok else "red",
                tags=["已完成" if ok else "失败"], elements=els)
    check(card)
    return card


def notice_card(title: str, body: str, *, footer: str = "") -> dict:
    els = [_md(body)]
    if footer:
        els.append(_md(f"<font color='grey'>{footer}</font>"))
    card = base(title, template="grey", elements=els)
    check(card)
    return card


def check(card: dict) -> None:
    problems: list[str] = []
    if card.get("schema") != "2.0":
        problems.append("schema 必须是 '2.0'")
    if not (card.get("header") or {}).get("title", {}).get("content"):
        problems.append("header.title.content 为空")
    els = (card.get("body") or {}).get("elements") or []
    if not els:
        problems.append("body.elements 为空")
    if len(els) > MAX_ELEMENTS:
        problems.append(f"元素超过 {MAX_ELEMENTS}")
    buttons = _walk(els, "button")
    if len(buttons) == 1 and buttons[0].get("type") != "primary_filled":
        problems.append("单按钮卡必须是 primary_filled")
    if len(buttons) > 1:
        if buttons[0].get("type") != "primary_filled":
            problems.append("多按钮卡第一个必须 primary_filled")
        if sum(1 for b in buttons if b.get("type") == "primary_filled") > 1:
            problems.append("多按钮卡只能有一个 primary_filled")
    if len(els) > 6:
        problems.append(f"视觉块 {len(els)} 个，超过 6 个（建议 2–5）")
    if problems:
        raise ValueError("卡片自检未通过: " + "; ".join(problems))


def _walk(elements, tag: str):
    out = []
    for el in elements or []:
        if el.get("tag") == tag:
            out.append(el)
        for key in ("elements", "columns"):
            if isinstance(el.get(key), list):
                out.extend(_walk(el[key], tag))
    return out


def dumps(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))