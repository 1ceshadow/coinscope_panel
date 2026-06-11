#!/usr/bin/env bash
# 组合系统一键启动: panel(信号桥) -> freqtrade(交易引擎) -> 打开网页
# 用法: ./start_all.sh        启动并打开网页
#       ./start_all.sh stop   停止全部
set -u

PANEL_DIR="$HOME/coinscope_panel"
FT_DIR="$HOME/ft_userdata"
PANEL_LOG="/tmp/panel_run.log"
PANEL_URL="http://127.0.0.1:8090"
FT_URL="http://127.0.0.1:8080"

c_green=$'\e[32m'; c_yellow=$'\e[33m'; c_red=$'\e[31m'; c_reset=$'\e[0m'
ok(){ echo "${c_green}✓${c_reset} $*"; }
warn(){ echo "${c_yellow}!${c_reset} $*"; }
err(){ echo "${c_red}✗${c_reset} $*"; }

# ---------- 停止 ----------
if [ "${1:-}" = "stop" ]; then
  echo "停止组合系统..."
  (cd "$FT_DIR" && docker compose stop) && ok "freqtrade 已停" || warn "freqtrade 停止异常"
  pkill -f panel.py && ok "panel 已停" || warn "panel 未在运行"
  exit 0
fi

# ---------- 前置检查 ----------
echo "=== 组合系统启动 ==="

# xray 代理 (freqtrade 连币安要用)
if pgrep -f xray >/dev/null; then
  ok "xray 代理运行中"
else
  warn "xray 代理未运行，freqtrade 可能连不上币安。请先启动 xray"
fi

# ---------- 第1步: 启动 panel ----------
if pgrep -f panel.py >/dev/null; then
  ok "panel 已在运行 (跳过)"
else
  echo "启动 panel ..."
  cd "$PANEL_DIR" || { err "找不到 $PANEL_DIR"; exit 1; }
  setsid python3 panel.py > "$PANEL_LOG" 2>&1 < /dev/null &
  # 等待 panel 就绪 (最多 20 秒)
  for i in $(seq 1 20); do
    if curl -s -o /dev/null "$PANEL_URL/api/data" 2>/dev/null; then break; fi
    sleep 1
  done
  if curl -s -o /dev/null "$PANEL_URL/api/data" 2>/dev/null; then
    ok "panel 已就绪 ($PANEL_URL)"
  else
    err "panel 启动超时，看日志: tail $PANEL_LOG"
    exit 1
  fi
fi

# 检查白名单是否有币 (cookie 是否有效)
PAIRS=$(curl -s "$PANEL_URL/api/pairlist" 2>/dev/null)
if echo "$PAIRS" | grep -q "USDT"; then
  ok "panel 白名单正常"
else
  warn "panel 白名单为空 — 可能 cookie 失效，请在 Chrome 登录 sanhe6.com"
fi

# ---------- 第2步: 启动 freqtrade ----------
echo "启动 freqtrade ..."
cd "$FT_DIR" || { err "找不到 $FT_DIR"; exit 1; }
docker compose up -d && ok "freqtrade 容器已启动" || { err "freqtrade 启动失败"; exit 1; }

# 等待 freqtrade API 就绪 (最多 40 秒)
echo "等待 freqtrade 就绪 ..."
for i in $(seq 1 40); do
  if curl -s -o /dev/null "$FT_URL/api/v1/ping" 2>/dev/null; then break; fi
  sleep 1
done
if curl -s "$FT_URL/api/v1/ping" 2>/dev/null | grep -q pong; then
  ok "freqtrade 已就绪 ($FT_URL)"
else
  warn "freqtrade API 未就绪，可能仍在加载市场数据。稍后手动刷新网页"
fi

# ---------- 第3步: 打开网页 ----------
echo "打开网页 ..."
for url in "$FT_URL" "$PANEL_URL"; do
  xdg-open "$url" >/dev/null 2>&1 &
  sleep 1
done
ok "已在浏览器打开两个面板"

echo ""
echo "=== 启动完成 ==="
echo "  信号面板:   $PANEL_URL"
echo "  交易界面:   $FT_URL  (用户名 freqtrader)"
echo "  停止系统:   ./start_all.sh stop"
