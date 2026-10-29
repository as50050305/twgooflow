#!/bin/sh
# 打開儀表板：盤中服務有在跑就開 http://localhost:埠號（即時），沒有就直接開 dashboard.html。
cd "$(dirname "$0")" || exit 1
PORT=$(sed -n 's/.*"live_port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' config.json 2>/dev/null | head -n 1)
[ -n "$PORT" ] || PORT=8765
if curl -s -m 3 --noproxy '*' "http://127.0.0.1:$PORT/api/live" 2>/dev/null | grep -q provider; then
  open "http://localhost:$PORT/"
else
  open "dashboard.html"
fi
