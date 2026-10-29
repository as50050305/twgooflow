#!/bin/sh
# TWFlow（Mac）共用：找一個能用的 Python 3.7 以上，結果放在 $PY，並記在 .pypath（下次直接用）。
# 用法：先 cd 到 twflow 資料夾，再 . ./mac/common.sh && find_py
py_ok() {
  [ -n "$1" ] && [ -x "$1" ] || return 1
  "$1" -c "import sys;sys.exit(sys.version_info<(3,7))" >/dev/null 2>&1
}
find_py() {
  PY=""
  if [ -f .pypath ]; then
    p=$(head -n 1 .pypath)
    if py_ok "$p"; then PY="$p"; return 0; fi
  fi
  # 順序：python.org 安裝版（新到舊）→ Homebrew → PATH → 系統內建 /usr/bin/python3
  for p in $(ls -d /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 2>/dev/null | sort -t. -k3,3nr) \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           "$(command -v python3 2>/dev/null)" /usr/bin/python3; do
    if py_ok "$p"; then
      PY="$p"
      printf '%s\n' "$PY" > .pypath 2>/dev/null
      return 0
    fi
  done
  return 1
}
no_py_msg() {
  echo
  echo "找不到可用的 Python 3.7 以上。"
  echo "請到 https://www.python.org/downloads/macos/ 下載安裝 Python 3，再重新執行。"
}
