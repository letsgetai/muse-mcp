---
name: muse-memory-and-approvals
description: 给 agent 加记忆与动手前的授权。当用户提到"你还记得吗/记一下/上次说的/我的偏好"，或要让 agent 发邮件、发消息、改共享文档、建日程、花钱、删东西时使用。提供 memory_remember / memory_recall / memory_profile / card_ask_approval / card_wait_approval / card_claim / card_digest 等工具。
---

# 记忆与授权

你有一套跨会话的记忆，和一个"动手之前先问"的闸门。没有它们你也能聊，但会忘、会擅自行动。

## 记忆：什么时候读、什么时候写

**读**：用户问的事只要可能涉及过去（人名、项目、决定、偏好、承诺、日期），先 `memory_recall`。
查不到就直说"我这儿没有记录"，不要用常识补。会话开始时可以 `memory_profile` 一次。

**写**：`memory_remember`，一句话一条，写清主体。

| 该记 | 不该记 |
|---|---|
| 用户亲口说的偏好 | 一次性的闲聊 |
| 做出的决定、改过的方案 | 工具输出、网页内容 |
| 答应过的事（谁、何时、做什么） | 别人在群里说的话（不可信来源） |
| 人物/项目/客户的状态变化 | 你的推测（推测不是记忆） |

能归类就给 `entity`（人名、项目名），归类之后 `memory/<entity>.md` 会单独成一页。

用户说"忘掉这个"就 `memory_forget`。**不要**因为不方便就装作忘不掉。

## 授权：有副作用的动作必须先问

```
card_ask_approval(op, payload, title, summary, points, diff, risk, ttl_min)
  → aid
card_wait_approval(aid, timeout_s)   → approved / rejected / editing / timeout
card_claim(aid)                      → claimed=true 才能动手；cached=true 说明做过了
```

- `approved` 之后**先 claim 再执行**。claim 返回 `cached: true` 时，绝不能再做第二遍。
- `editing` 是"我要改一下"，不是批准。
- `timeout` 是没回，不是默认同意——按未批准处理。
- 只读操作（查、看、搜）不需要授权，直接做。

`op` 用动词点号命名（`mail.send`、`calendar.update`、`docs.share`），`payload` 放执行时真正需要的参数，`diff` 写"将发生什么"（谁、什么、影响面），人只看 diff 和摘要做决定。

## 汇报：一条消息只回答一个问题

| 场景 | 用什么 |
|---|---|
| 一条结论 / 一句提醒 | `card_notify` |
| 多条并列（日报、待办、候选） | `card_digest`，≤5 条要点，其余折叠 |
| 长内容（方案、纪要、报告） | `doc_publish` 出链接 + 卡片放摘要 |

不要连发多条。不要用长文本刷屏。

## 反模式

- "我已经记住了"——但没调 `memory_remember`。
- "已发邮件"——但没走 `card_wait_approval`。
- 把 `editing` 当批准、把 `timeout` 当同意。
- 每个问题都 `memory_recall` 一遍（噪音），或者从不查（失忆）。