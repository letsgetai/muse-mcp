#!/usr/bin/env bash
# muse-mcp 安装自检：不改系统、不装依赖、不启动服务。
set -euo pipefail

cd "$(dirname "$0")"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo "muse-mcp 自检"
echo

command -v python3 >/dev/null || die "找不到 python3"
python3 - <<'PY' || die "python3 版本过低（需要 3.9+）"
import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)"

if command -v lark-cli >/dev/null; then
  ok "lark-cli $(lark-cli --version 2>&1 | head -1 | awk '{print $NF}')"
else
  warn "没有 lark-cli：记忆可用，卡片类工具会报错"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  warn "已生成 .env，请填写 MUSE_OWNER_OPEN_ID 和 FEISHU_CHAT_ID 后重跑"
  exit 0
fi

set -a; . ./.env; set +a
[ -n "${MUSE_OWNER_OPEN_ID:-}" ] || die ".env 里缺 MUSE_OWNER_OPEN_ID"
ok "owner = ${MUSE_OWNER_OPEN_ID}"

if [ -n "${FEISHU_CHAT_ID:-}" ] || [ -n "${FEISHU_USER_ID:-}" ]; then
  ok "卡片目标已配置"
else
  warn "没有 FEISHU_CHAT_ID / FEISHU_USER_ID：卡片类工具不可用"
fi

echo
echo "配置检查："
python3 server.py --selftest

echo
echo "下一步："
echo "  1) 跑测试：   python3 -m unittest discover -s tests -v"
echo "  2) 起监听器： python3 approve_listener.py        # 必须先起，否则按钮点不动"
echo "  3) 让它开口： python3 proactive.py --dry-run       # 先看内容"
echo "                python3 proactive.py --loop         # 到点自己发"
echo "  4) 接宿主：   python3 server.py                  # stdio"
echo "                python3 server.py --http 8765      # HTTP"
echo
echo "systemd 常驻（可选，不需要 sudo）："
echo "  mkdir -p ~/.config/systemd/user"
echo "  # 写 ~/.config/systemd/user/muse-approve.service，ExecStart 指向 approve_listener.py"
echo "  systemctl --user enable --now muse-approve"