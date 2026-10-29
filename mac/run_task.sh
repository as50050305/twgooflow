#!/bin/sh
# launchd 排程用（週一～五 18:45、21:45）：抓盤後資料，不停下來等按鍵。Windows 的 run_task.bat。
cd "$(dirname "$0")/.." || exit 1
. ./mac/common.sh
mkdir -p logs
if ! find_py; then echo "$(date '+%Y-%m-%d %H:%M:%S') run_task: no working Python 3.7+ found" >> logs/task_console.log; exit 1; fi
"$PY" fetch.py >> logs/task_console.log 2>&1
