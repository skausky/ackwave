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
# New firmware for the hub (copied to $OUT/firmware by the PC, with a FLASH_ME
# marker): flashed on the first port before the dashboard starts, then the
# marker is renamed so it runs once.
FW="$OUT/firmware"
if [ -f "$FW/FLASH_ME" ] && [ -f "$FW/flash_args" ]; then
    HUBPORT=${PORTS%% *}
    echo "Flashing new firmware on $HUBPORT ..."
    if (cd "$FW" && python3 -m esptool --chip esp32s3 -p "$HUBPORT" -b 460800 \
            --before default_reset --after hard_reset write_flash "@flash_args"); then
        mv "$FW/FLASH_ME" "$FW/flashed-$(date +%Y%m%d-%H%M%S)"
        echo "Firmware flashed."
        sleep 4                                   # the board restarts
    else
        mv "$FW/FLASH_ME" "$FW/failed-$(date +%Y%m%d-%H%M%S)"
        echo "Flashing failed: the board keeps its old firmware."
    fi
fi
# shellcheck disable=SC2086
exec python3 -u /app/tools/web.py $PORTS --ingress --port 8765 --out "$OUT" --give-up 60 $NORAW
