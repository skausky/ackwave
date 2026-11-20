#!/bin/sh
# Starts the ackwave dashboard for Home Assistant's sidebar (ingress only).
# Data and settings live in /share/ackwave (the board-key secret too, owner-only).
OUT=/share/ackwave
mkdir -p "$OUT" && chmod 700 "$OUT"
opt() { python3 -c "import json,sys; v=json.load(open('/data/options.json')).get(sys.argv[1]); print(v if v not in (None, False) else '')" "$1"; }
PORT=$(opt port)
NORAW=--no-raw
[ "$(opt keep_raw)" = "True" ] && NORAW=
while :; do
    if [ -n "$PORT" ]; then PORTS=$PORT; [ -e "$PORT" ] || PORTS=; else PORTS=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null); fi
    [ -n "$PORTS" ] && break
    echo "No board on USB yet: plug the hub into this machine (checking every 10 s)."
    sleep 10
done
echo "Boards on: $PORTS"
# shellcheck disable=SC2086
exec python3 -u /app/tools/web.py $PORTS --ingress --port 8765 --out "$OUT" $NORAW
