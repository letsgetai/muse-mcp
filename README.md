# muse-mcp

给你的 **现有 agent** 加一层「另一个我」：**记忆 · 卡片 · 授权**。

它不是一个 agent，不跑模型，不做规划。它是四个东西：

```
MCP server（记忆 + 卡片）  +  审批监听器（接卡片回调）
 +  主动层（没人说话时自己开口）  +  一段提示词（行为规则）
```

装上之后，你的 agent 会记事、会把要动手的事先做成卡片问你、会用结构化卡片汇报，
并且会在早上八点半自己找你。

## 它补的是哪一件事

| 能力 | 谁提供 |
|---|---|
| 记忆的读写 | 本仓库（文件 + 检索） |
| 高密度展示 | 本仓库（飞书卡片 2.0） |
| 动手前的授权 | 本仓库（待批准动作 + 卡片按钮 + 幂等执行） |
| **主动**（没人说话时自己开口） | `proactive.py`：定时 + 静默时段 + 日预算 + 去重，内容可交给内核生成 |

## 60 秒跑通

```bash
git clone https://github.com/letsgetai/muse-mcp && cd muse-mcp
cp .env.example .env && $EDITOR .env        # 填 owner 和 chat
set -a && . ./.env && set +a

python3 server.py --selftest                # 看配置对不对
python3 -m unittest discover -s tests -v    # 22 个离线单测，不联网
python3 proactive.py --dry-run               # 看它早上会说什么（不发）
```

真让它开口（会往飞书发一张卡）：

```bash
python3 proactive.py --once      # 立刻发一次（演示）
python3 proactive.py --loop      # 常驻，每分钟检查是不是到点了
```

不写代码直接试一条：

```bash
printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_remember","arguments":{"text":"Dana 的报价是 48k","entity":"Dana"}}}' \
'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"card_notify","arguments":{"title":"通了","body":"这条卡片来自 MCP 工具"}}}' \
| python3 server.py
```

依赖：`python3`（只用标准库）+ `lark-cli`（发卡片用，已登录 bot 身份）。

## 11 个工具

| 工具 | 干什么 |
|---|---|
| `memory_remember` / `memory_recall` / `memory_profile` / `memory_forget` | 记、查、画像、忘 |
| `card_ask_approval` | 登记一条待批准动作并发决策卡，返回 `aid` |
| `card_approval_status` / `card_wait_approval` | 查状态 / 阻塞等拍板 |
| `card_claim` | 执行前最后一道闸：确认已批准且没执行过 |
| `card_notify` / `card_digest` | 告知卡 / 信息密度卡 |
| `doc_publish` | 长内容写云文档，卡片里只放摘要 |

## 授权是怎么走的

```
agent 想发邮件
   └─ card_ask_approval  → 待批准动作入库 + 发卡片 → 返回 aid
        └─ card_wait_approval(aid)          ← 阻塞，等你点
             ├─ 拒绝 → 结束，什么也没发生
             └─ 批准 → card_claim(aid)
                          ├─ claimed=true  → 现在可以真发
                          └─ cached=true   → 已经发过了，别再发
```

三条不变量：

1. **只有登记时指定的 open_id 能拍板**（卡片可以被转发，所以必须校验）；
2. **过期即作废**（默认 30 分钟）；
3. **同一个 `aid` 只放行一次**——重复回调、网络重试都不会发第二封邮件。

`approve_listener.py` 负责接住按钮点击。**它必须常驻**，否则按钮只是装饰：

```bash
python3 approve_listener.py        # 建议 systemd / docker / supervisord
```

## 记忆长什么样

```
$MUSE_HOME/                 # 默认 ~/.muse
├── MEMORY.md               每轮读进上下文的那份（自动生成，可直接手改）
├── memory/<entity>.md      按实体分页，人可读
├── log/2026-11-24.md       流水，只追加
└── index.json              检索索引，删了会自动重建
```

检索是「拉丁词 + 中文二元组 + 稀有词加权 + 时间衰减」，不引向量库。几千条量级够用，而且每一行都能 `cat` 出来看。要更强就把它换成 Graphiti / mem0，接口只有 `remember/recall/profile` 三个。

## 接进不同的宿主

**Aily / 任何支持自定义 MCP 的平台**：stdio 命令 `python3 /path/to/server.py`，或 HTTP `python3 server.py --http 8765`。

**Claude Code / Codex**：

```json
{"mcpServers": {"muse": {"command": "python3",
  "args": ["/path/to/server.py"],
  "env": {"MUSE_HOME": "/home/me/.muse", "MUSE_OWNER_OPEN_ID": "ou_xxx",
          "FEISHU_CHAT_ID": "oc_xxx"}}}}
```

**行为规则**：把 [`skill/SKILL.md`](skill/SKILL.md) 塞进宿主的技能/人设；
Aily 用户直接用 [`prompt/aily.md`](prompt/aily.md)，让它自己配。

## 主动：它自己开口

```bash
python3 proactive.py --loop      # 常驻
```

判据（都可用环境变量改）：到点（默认 08:30，5 分钟窗口）→ 不在静默时段（22–08）
→ 今日未超预算（3 次）→ 距上次 ≥90 分钟。任一条不过就不发，并说明原因。

**内容从哪来**，三档可选：

| 配置 | 行为 |
|---|---|
| 什么都不配 | 确定性摘要：最近记忆 + 今日新增 + 待批数量。零依赖，立刻能跑 |
| `MUSE_WAKER_CMD="claude -p"` | 把摘要喂给宿主跑一轮，用它的输出当正文（真正"它自己在想"） |
| `MUSE_WAKER_URL=http://...` | POST `{"prompt": ...}` 到宿主的唤醒地址，取响应当正文 |

```bash
MUSE_BRIEF_AT=08:30 MUSE_BUDGET=3 MUSE_QUIET_START=22 MUSE_QUIET_END=8 python3 proactive.py --loop
```

## 已知限制

- **主动的"内容"是摘要，不是思考**，除非配了 waker。摘要能让你确认链路通，但真正有用的是 `MUSE_WAKER_CMD` 那一档。
- **授权是"协作式"的**。它靠 agent 自觉在动手前调 `card_ask_approval`；提示词写得不好，agent 可能绕过。真要硬约束，得在宿主层做工具白名单。
- **卡片绑定飞书**。换 Slack/微信要重写 `muse/cards.py`，状态机可以复用。
- **记忆是单人的**。多用户部署要按 open_id 分目录（`MUSE_HOME/<open_id>/`），目前没做。

## License

MIT。飞书卡片规范来自飞书开放平台文档；`lark-cli` 是外部依赖，不随本仓库分发。