#!/bin/sh
# 停止正在執行的盤中服務（不管是排程在背景開的，還是 live.command 的視窗）。Windows 的 stop_live.bat。
cd "$(dirname "$0")" || exit 1
. ./mac/common.sh
mkdir -p logs
if ! find_py; then no_py_msg; printf "按 Enter 關閉…"; read -r _; exit 1; fi
"$PY" live.py --stop
sleep 3
