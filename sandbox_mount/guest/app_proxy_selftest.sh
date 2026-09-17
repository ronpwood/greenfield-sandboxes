#!/usr/bin/env bash
# usage: app_proxy_selftest.sh <bun-binary> <app-dir>
# Boots the app's dev server on loopback and app_proxy.ts in front of it, then
# asks for / and the first bundle chunk with a foreign Host header. Exit 0 only
# on 200/200. Run it against any bun binary to prove the proxy on that version
# without a VM — this is how a bun release is vetted before it floats in.
set -euo pipefail
BUN="$1"; APP="$2"; HERE="$(cd "$(dirname "$0")" && pwd)"
UP=4702; PUB=4701; T="$(mktemp -d)"
( cd "$APP" && PORT=$UP nohup "$BUN" index.html >"$T/dev.log" 2>&1 </dev/null & echo $! >"$T/dev.pid" )
( PORT=$PUB UPSTREAM=$UP nohup "$BUN" "$HERE/app_proxy.ts" >"$T/proxy.log" 2>&1 </dev/null & echo $! >"$T/proxy.pid" )
trap 'kill $(cat "$T"/*.pid) 2>/dev/null || true; rm -rf "$T"' EXIT
sleep 3
H='Host: vm-abc.exe.xyz'
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$H" "http://127.0.0.1:$PUB/")
js=$(curl -s -H "$H" "http://127.0.0.1:$PUB/" | grep -o -E 'src="[^"]+\.js"' | head -1 | sed 's/src="//;s/"//')
jscode=$(curl -s -o /dev/null -w '%{http_code}' -H "$H" "http://127.0.0.1:$PUB${js:-/missing.js}")
echo "bun $("$BUN" --version): / -> $code   ${js:-<no chunk>} -> $jscode"
if [ "$code" != 200 ] || [ "$jscode" != 200 ]; then
  echo "--- dev.log ---"; tail -n 20 "$T/dev.log"; echo "--- proxy.log ---"; tail -n 20 "$T/proxy.log"
  exit 1
fi
