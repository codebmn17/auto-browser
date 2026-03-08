#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99
WIDTH="${BROWSER_WIDTH:-1600}"
HEIGHT="${BROWSER_HEIGHT:-900}"
START_URL="${BROWSER_URL:-about:blank}"
WS_ENDPOINT_FILE="${BROWSER_WS_ENDPOINT_FILE:-/data/profile/browser-ws-endpoint.txt}"

mkdir -p /data/profile /data/downloads /tmp/runtime
rm -f "$WS_ENDPOINT_FILE"
rm -f /data/profile/SingletonLock /data/profile/SingletonSocket /data/profile/SingletonCookie

Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -nopw -xkb >/tmp/x11vnc.log 2>&1 &
/usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 &
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 >/tmp/socat.log 2>&1 &

CHROMIUM_PATH="$(python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)
PY
)"

cleanup() {
  if [[ -n "${CHROME_PID:-}" ]] && kill -0 "$CHROME_PID" >/dev/null 2>&1; then
    kill "$CHROME_PID" >/dev/null 2>&1 || true
    wait "$CHROME_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

"$CHROMIUM_PATH" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-background-networking \
  --user-data-dir=/data/profile \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --window-size="${WIDTH},${HEIGHT}" \
  "$START_URL" >/tmp/chromium.log 2>&1 &

CHROME_PID=$!

python - "$WS_ENDPOINT_FILE" <<'PY'
import json
import sys
import time
from urllib.request import urlopen

out_path = sys.argv[1]

for _ in range(120):
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=1) as response:
            payload = json.load(response)
        ws_url = payload["webSocketDebuggerUrl"].replace(
            "ws://127.0.0.1:9222/",
            "ws://browser-node:9223/",
        )
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(ws_url)
        print(f"wrote {out_path}: {ws_url}")
        break
    except Exception:
        time.sleep(0.5)
else:
    raise SystemExit("failed to discover chromium websocket endpoint")
PY

wait "$CHROME_PID"
