#!/bin/bash
# 安装 stock-trading-agent 为 macOS launchd 服务
# 用法: bash deploy/install_launchd.sh [install|uninstall|status|start|stop|restart|tail]

set -e

LABEL="com.stockagent"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$PROJECT_DIR/deploy/com.stockagent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
ENV_FILE="$PROJECT_DIR/.env"
PID_FILE="$PROJECT_DIR/data/agent.pid"

action="${1:-install}"

# ──── 生成 plist + 注入 .env ────
# 用 awk 替换 EnvironmentVariables 段 (避开 Python 嵌 bash 的转义噩梦)
gen_plist_with_env() {
  cp "$PLIST_SRC" "$PLIST_DST"

  # 拼 env dict 内容 (用临时文件, 避免引号/转义坑)
  local envtmp
  envtmp=$(mktemp)
  cat > "$envtmp" <<ENVHEAD
        <key>PYTHONPATH</key><string>${PROJECT_DIR}</string>
        <key>PATH</key><string>${PROJECT_DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
ENVHEAD

  if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key val; do
      [[ -z "$key" || "$key" =~ ^# ]] && continue
      [[ -z "$val" ]] && continue
      # XML 转义
      esc_val=$(printf '%s' "$val" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g")
      printf '        <key>%s</key><string>%s</string>\n' "$key" "$esc_val" >> "$envtmp"
    done < "$ENV_FILE"
  fi

  # 用 awk 找 <key>EnvironmentVariables</key> 到下一个 </dict> 之间替换
  awk -v envfile="$envtmp" '
    /<key>EnvironmentVariables<\/key>/ {
      print
      print "<dict>"
      while ((getline line < envfile) > 0) print line
      close(envfile)
      in_env = 1
      next
    }
    in_env && /<\/dict>/ {
      print
      in_env = 0
      next
    }
    in_env { next }
    { print }
  ' "$PLIST_DST" > "$PLIST_DST.tmp"
  mv "$PLIST_DST.tmp" "$PLIST_DST"
  rm -f "$envtmp"
  local n=$(grep -c '<key>' "$envtmp" 2>/dev/null || echo 0)
  echo "  ✓ env vars 注入完成"
}

case "$action" in
  install)
    # 杀干净所有老进程 + 清 plist + 清 pid
    pkill -9 -f "stock_trading_agent.agent" 2>/dev/null || true
    rm -f "$PID_FILE"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    sleep 1
    mkdir -p "$HOME/Library/LaunchAgents"
    gen_plist_with_env
    launchctl load -w "$PLIST_DST"
    echo "✓ 已安装, agent 5s 内启动"
    echo "  状态:  bash $0 status"
    echo "  日志:  bash $0 tail"
    ;;

  uninstall)
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    pkill -9 -f "stock_trading_agent.agent" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✓ 已卸载"
    ;;

  status)
    echo "[plist]"
    ls -la "$PLIST_DST" 2>&1 | head -1
    echo
    echo "[launchctl]"
    launchctl list | grep -i stockagent || echo "  (没找到)"
    echo
    echo "[pid file]"
    cat "$PID_FILE" 2>/dev/null || echo "  (无)"
    echo
    echo "[out log 末 5 行]"
    tail -5 /tmp/agent_launchd.out.log 2>/dev/null || echo "  (无)"
    echo
    echo "[err log 末 5 行]"
    tail -5 /tmp/agent_launchd.err.log 2>/dev/null || echo "  (无)"
    ;;

  start)
    launchctl load -w "$PLIST_DST" 2>/dev/null || true
    launchctl start "$LABEL"
    echo "✓ start"
    ;;

  stop)
    launchctl stop "$LABEL" 2>/dev/null || true
    sleep 3
    pkill -9 -f "stock_trading_agent.agent" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✓ stop"
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    sleep 2
    "$0" status
    ;;

  tail)
    tail -f /tmp/agent_launchd.out.log
    ;;

  *)
    echo "用法: $0 {install|uninstall|status|start|stop|restart|tail}"
    exit 1
    ;;
esac
