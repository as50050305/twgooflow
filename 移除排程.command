#!/bin/sh
# 移除 mac_install.sh 建立的自動排程（tw.twflow.fetch、tw.twflow.live）。程式與資料不會刪除。
UIDN=$(id -u)
for L in tw.twflow.fetch tw.twflow.live; do
  P="$HOME/Library/LaunchAgents/$L.plist"
  if [ -f "$P" ]; then
    launchctl bootout "gui/$UIDN" "$P" >/dev/null 2>&1 || launchctl unload -w "$P" >/dev/null 2>&1
    rm -f "$P"
    echo "已移除排程 $L"
  else
    echo "沒有排程 $L"
  fi
done
printf "按 Enter 關閉…"; read -r _
