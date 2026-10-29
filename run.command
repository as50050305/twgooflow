#!/bin/sh
# 手動抓資料（盤後）：雙擊執行。Windows 的 run.bat。
cd "$(dirname "$0")" || exit 1
. ./mac/common.sh
mkdir -p logs
if ! find_py; then no_py_msg; printf "按 Enter 關閉…"; read -r _; exit 1; fi
echo "使用 Python：$PY"
"$PY" --version
"$PY" fetch.py "$@"
echo
echo "完成。雙擊「開啟儀表板.command」（或 dashboard.html）查看；紀錄在 logs 資料夾。"
printf "按 Enter 關閉…"; read -r _
