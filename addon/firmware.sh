#!/bin/sh
# Copies the built firmware (idf.py build) to Home Assistant for the add-on to
# flash on the hub, then restarts the add-on. Usage: addon/firmware.sh user@host [ssh options]
set -e
cd "$(dirname "$0")/.."
[ -f build/flash_args ] || { echo "Build first: . ~/esp/esp-idf-v5.3.2/export.sh && idf.py build"; exit 1; }
DEST=$1; shift
FILES="flash_args $(grep -o '[^ ]*\.bin' build/flash_args | tr '\n' ' ')"
tar -C build -czf - $FILES | ssh "$@" "$DEST" 'sudo rm -rf /share/ackwave/firmware && sudo mkdir -p /share/ackwave/firmware && sudo tar -C /share/ackwave/firmware -xzf - && sudo touch /share/ackwave/firmware/FLASH_ME && sudo chmod -R go-rwx /share/ackwave/firmware && bash -lc "ha apps restart local_ackwave"'
echo "Sent; the add-on flashes the hub as it starts (see its log)."
