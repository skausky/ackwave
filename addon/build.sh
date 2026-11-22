#!/bin/sh
# Assembles the add-on folder (addon/ackwave + the dashboard code) in build/addon/ackwave,
# ready to copy to Home Assistant's /addons/ackwave.
set -e
cd "$(dirname "$0")/.."
OUT=build/addon/ackwave
rm -rf "$OUT" && mkdir -p "$OUT/tools"
cp addon/ackwave/config.yaml addon/ackwave/Dockerfile addon/ackwave/run.sh addon/ackwave/DOCS.md "$OUT/"
cp tools/*.py tools/ml_motion.json "$OUT/tools/"
rm -f "$OUT"/tools/test_*.py "$OUT"/tools/fake_csi.py
cp -r tools/web "$OUT/tools/web"
echo "$OUT"
