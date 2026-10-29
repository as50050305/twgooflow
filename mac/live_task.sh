#!/bin/sh
# launchd 排程用（週一～五 08:40）：在背景執行盤中服務（收盤後 30 分鐘自己結束）。Windows 的 live_task.bat。
# 要停止：雙擊 stop_live.command；想在視窗裡看：雙擊 live.command（會接手）。
cd "$(dirname "$0")/.." || exit 1
. ./mac/common.sh
mkdir -p logs
if ! find_py; then echo "$(date '+%Y-%m-%d %H:%M:%S') live_task: no working Python 3.7+ found" >> logs/task_console.log; exit 1; fi
exec "$PY" live.py --no-browser >> logs/live_console.log 2>&1
