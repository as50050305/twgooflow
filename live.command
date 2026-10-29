#!/bin/sh
# 盤中即時：在這個終端機視窗執行盤中服務，並打開 http://localhost:8765。Windows 的 live.bat。
# 開盤時間請讓這個視窗開著；關掉視窗（或按 Ctrl+C）就停止。
# 如果 08:40 排程已經在背景開了服務，會請它結束、改由這個視窗接手。
cd "$(dirname "$0")" || exit 1
. ./mac/common.sh
mkdir -p logs
printf '\033]0;TWFlow live - 關掉這個視窗就停止\007'
if ! find_py; then no_py_msg; printf "按 Enter 關閉…"; read -r _; exit 1; fi
echo "使用 Python：$PY"
"$PY" --version
"$PY" live.py --foreground "$@"
echo
echo "盤中服務已停止，可以關掉這個視窗。"
printf "按 Enter 關閉…"; read -r _
