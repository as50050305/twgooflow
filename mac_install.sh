#!/bin/sh
# TWFlow（Mac）安裝／更新
# 用法：打開「終端機」，輸入 sh 和一個空格，把這個檔案拖進視窗，按 Enter。
# 會安裝到 ~/twflow（不放 OneDrive／桌面：避免兩台電腦同時寫同一份資料、避免 macOS 資料夾權限問題）。
# 再執行一次就是更新程式：config.json、sectors.csv 已經有的話不會覆蓋，data 資料夾不會動。
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/twflow"

ask() {  # ask "問題" 預設(y/n) → 回傳 0＝是
  printf "%s " "$1"
  read -r a
  [ -z "$a" ] && a="$2"
  case "$a" in [Yy]*|是*) return 0 ;; *) return 1 ;; esac
}
clean_path() {  # 拖進終端機的路徑：去掉前後空白、引號、反斜線跳脫
  printf '%s' "$1" | sed -e 's/\\\(.\)/\1/g' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^'\(.*\)'\$/\1/" -e 's/^"\(.*\)"$/\1/'
}

echo "== TWFlow 台股盤後法人資金流向（Mac 版）安裝 =="
echo "來源：$SRC"
echo "安裝到：$DEST"
if [ ! -f "$SRC/fetch.py" ] || [ ! -f "$SRC/live.py" ] || [ ! -f "$SRC/dashboard.html" ]; then
  echo "！這個資料夾裡找不到 fetch.py／live.py／dashboard.html，請把整個 mac版 資料夾一起放好再執行。"
  exit 1
fi
mkdir -p "$DEST/mac" "$DEST/logs" "$DEST/data" || exit 1

# 1. 複製程式（新版覆蓋舊版）
for f in fetch.py live.py dashboard.html 使用說明.txt "使用說明（Mac）.txt" run.command live.command stop_live.command 開啟儀表板.command 移除排程.command mac_install.sh; do
  [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$DEST/$f"
done
for f in common.sh run_task.sh live_task.sh; do cp -f "$SRC/mac/$f" "$DEST/mac/$f"; done
for f in config.json sectors.csv; do
  d="${f%.*}.default.${f##*.}"            # Releases 壓縮檔裡是 config.default.json、sectors.default.csv
  [ -f "$SRC/$d" ] && cp -f "$SRC/$d" "$DEST/$d"
  if [ -f "$DEST/$f" ]; then echo "  保留你原本的 $f"
  elif [ -f "$SRC/$f" ]; then cp "$SRC/$f" "$DEST/$f"
  elif [ -f "$SRC/$d" ]; then cp "$SRC/$d" "$DEST/$f"; fi
done
[ -f "$SRC/VERSION.txt" ] && cp -f "$SRC/VERSION.txt" "$DEST/VERSION.txt"
rm -f "$DEST/.pypath"
chmod +x "$DEST"/*.command "$DEST"/mac/*.sh "$DEST/mac_install.sh" 2>/dev/null
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null   # 移除「從網路下載」的隔離標記，雙擊 .command 才不會被擋
echo "1. 程式已複製。"

# 2. Python
cd "$DEST" || exit 1
. ./mac/common.sh
if ! find_py; then no_py_msg; exit 1; fi
echo "2. 使用 Python：$PY（$("$PY" --version 2>&1)）"

# 3. 測試 https 連線（證交所）
echo "3. 測試連線證交所…（約 5 秒）"
if "$PY" -c "import fetch,urllib.request as u;u.urlopen(u.Request('https://www.twse.com.tw/zh/',headers={'User-Agent':'Mozilla/5.0'}),timeout=30,context=fetch.make_ssl_context()).read(100)" >/dev/null 2>logs/install_ssl.log; then
  echo "   連線正常。"
else
  echo "   ！連線失敗（說明在 logs/install_ssl.log）。可以試試：打開 應用程式 → Python 3.x → 雙擊 Install Certificates.command，再執行一次安裝。"
fi

# 4. 時區（排程時間用 Mac 的時區）
TZNOW=$(date +%z)
if [ "$TZNOW" != "+0800" ]; then
  echo "！這台 Mac 的時區不是台北（目前 $TZNOW）：排程時間會跟著 Mac 的時區。建議到 系統設定 → 一般 → 日期與時間 改成台北。"
fi

# 5. 可選：從 Windows 複製已經抓好的資料（省下第一次 1～2 小時）
if [ ! -d "$DEST/data/days" ] && ask "4. 要從 Windows 複製已經抓好的資料嗎？(y/N)" n; then
  printf "   把 Windows 的 twflow 資料夾（或裡面的 data 資料夾）拖進來，按 Enter："
  read -r P
  P=$(clean_path "$P")
  [ -d "$P/data" ] && P="$P/data"
  if [ -d "$P/days" ]; then
    for d in days raw; do [ -d "$P/$d" ] && cp -R "$P/$d" "$DEST/data/"; done
    for f in "$P"/*.js "$P/calendar.json" "$P/universe.json" "$P/live_settings.json"; do [ -f "$f" ] && cp -f "$f" "$DEST/data/"; done
    echo "   已複製 data。"
  else
    echo "   ！$P 裡沒有 days 資料夾，跳過。"
  fi
fi

# 6. 可選：自動排程（launchd）
UIDN=$(id -u)
LA="$HOME/Library/LaunchAgents"
plist() {  # plist 標籤 腳本 時:分...
  L="$1"; S="$2"; shift 2
  P="$LA/$L.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$L</string>"
    echo "  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$DEST/$S</string></array>"
    echo "  <key>WorkingDirectory</key><string>$DEST</string>"
    echo "  <key>StandardOutPath</key><string>$DEST/logs/launchd_$L.log</string>"
    echo "  <key>StandardErrorPath</key><string>$DEST/logs/launchd_$L.log</string>"
    echo "  <key>EnvironmentVariables</key><dict><key>PYTHONIOENCODING</key><string>utf-8</string><key>LANG</key><string>zh_TW.UTF-8</string></dict>"
    echo "  <key>StartCalendarInterval</key><array>"
    for t in "$@"; do
      for wd in 1 2 3 4 5; do
        echo "    <dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>${t%%:*}</integer><key>Minute</key><integer>${t##*:}</integer></dict>"
      done
    done
    echo "  </array>"
    echo '</dict></plist>'
  } > "$P"
  launchctl bootout "gui/$UIDN" "$P" >/dev/null 2>&1 || launchctl unload "$P" >/dev/null 2>&1
  launchctl bootstrap "gui/$UIDN" "$P" >/dev/null 2>&1 || launchctl load -w "$P"
}
if ask "5. 要建立自動排程嗎？平日 18:45／21:45 抓盤後資料、08:40 開盤中服務 (Y/n)" y; then
  mkdir -p "$LA"
  plist tw.twflow.fetch mac/run_task.sh 18:45 21:45
  plist tw.twflow.live mac/live_task.sh 8:40
  echo "   已建立排程（Mac 睡眠時錯過的，喚醒後會補跑；關機錯過的不會補）。要移除：雙擊 移除排程.command"
fi

# 7. 可選：現在就抓一次
if ask "6. 要現在就抓一次資料嗎？（第一次約 1.5～2.5 小時，可以中斷，下次接著抓）(y/N)" n; then
  "$PY" fetch.py
fi

echo
echo "完成。程式在 $DEST"
echo "  抓資料：雙擊 run.command　盤中即時：雙擊 live.command　看儀表板：雙擊 開啟儀表板.command"
echo "  建議用 Chrome 看儀表板。詳細說明：使用說明（Mac）.txt"
open "$DEST" 2>/dev/null
