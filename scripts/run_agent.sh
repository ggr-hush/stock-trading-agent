#!/bin/bash
# launchd wrapper: 拉起 stock-trading-agent supervisor
# launchd 会用 plist 里 ProgramArguments 调 /bin/bash scripts/run_agent.sh

PROJECT_DIR="/Users/alice/Documents/Codex/stock-trading-agent"
LOG_FILE="/tmp/agent_launchd.log"

# 切到项目目录 (用绝对路径, 避免 launchd 给的 cwd 异常)
cd "$PROJECT_DIR" || { echo "cd failed: $PROJECT_DIR" >> "$LOG_FILE"; exit 1; }

# 导出 venv 路径 + 项目路径
export PYTHONPATH="$PROJECT_DIR"
export PATH="$PROJECT_DIR/.venv/bin:$PATH"

# .env 加载 (curl/lark/minimax 等)
if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  . "$PROJECT_DIR/.env"
  set +a
fi

# 写一行启动 log
echo "[$(date '+%Y-%m-%d %H:%M:%S')] launchd: start agent (pid=$$ cwd=$(pwd))" >> "$LOG_FILE"

# exec 替换为 supervisor
exec "$PROJECT_DIR/.venv/bin/python" -m stock_trading_agent.agent start >> "$LOG_FILE" 2>&1
