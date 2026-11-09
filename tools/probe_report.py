#!/usr/bin/env python3
"""Summarise a probe-mode log (firmware built with CSI_PROBE=1).

    python probe_report.py probe.log

Answers the bring-up questions for ACK ("Polite Wi-Fi") CSI on your board:
  * do ACKs from the AP produce CSI callbacks at all?
    (pokes use a made-up sender address per AP, see poke_ta(); ACKs to it,
    or to the board's own MAC for older firmware, count as ours)
  * what MAC / destination MAC does the driver report for them?
  * how long after the poke do they arrive (to tune ACK_WIN_US)?
  * how well does the firmware's timing-based classifier match reality?
An ACK seen by the sniffer and a CSI callback with the same radio timestamp
(rx_ts) are the same frame, which gives ground truth for the last question.
"""
import sys
from collections import Counter

import numpy as np

VERDICT = {-1: 'dropped', 0: 'AP frame', 1: 'ACK'}
MAX_NODES = 8                         # keep in sync with main/csi_core.h


def poke_ta(me, node):
    """Made-up poke sender for `node`; mirrors poke_ta() in csi_core.h."""
    b = [int(x, 16) for x in me.split(':')]
    b[0] = (b[0] ^ 0x02) & 0xFE
    b[5] = (b[5] + node) & 0xFF
    return ':'.join(f'{x:02x}' for x in b)


def our_addrs(me):
    """Receiver addresses whose ACKs answer our pokes."""
    if not me:
        return set()
    return {me} | {poke_ta(me, n) for n in range(MAX_NODES)}


def parse(lines):
    me, csi, acks, stats = None, [], [], None
    for line in lines:
        f = line.strip().split(',')
        try:
            if f[0] == 'ME' and len(f) == 2:
                me = f[1]
            elif f[0] == 'STATS' and len(f) in (8, 9):
                stats = dict(zip(('poke', 'tx_err', 'ack', 'ap', 'drop', 'qfull'),
                                 map(int, f[2:8])))
            elif f[0] == 'PROBE' and len(f) == 13:
                r = dict(what=f[1], t=int(f[2]), mac=f[3], dmac=f[4], sig=int(f[5]),
                         rate=int(f[6]), len=int(f[7]), rssi=int(f[8]), dt=int(f[9]),
                         pending=int(f[10]), verdict=int(f[11]), rx_ts=int(f[12]))
                (csi if r['what'] == 'c' else acks).append(r)
        except ValueError:
            continue
    return me, csi, acks, stats


def report(me, csi, acks, stats, out=print):
    out(f"board MAC: {me or 'unknown (no ME line)'}")
    if stats:
        out(f"last STATS: {stats['poke']} pokes, {stats['tx_err']} tx errors, "
            f"{stats['ack']} ACK CSI, {stats['ap']} AP CSI, {stats['drop']} dropped, "
            f"{stats['qfull']} queue full")
        if stats['poke'] == 0:
            out("  !! no pokes sent - is POKE_HZ > 0 and was the AP found?")
        elif stats['tx_err'] > stats['poke'] * 0.1:
            out("  !! many tx errors - esp_wifi_80211_tx is rejecting the frame")
    ours = our_addrs(me)
    foreign = [r for r in acks if r['dmac'] not in ours]
    acks = [r for r in acks if r['dmac'] in ours]
    out(f"CSI callbacks: {len(csi)}   ACKs to us seen by sniffer: {len(acks)}"
        f"   (ACKs to other devices: {len(foreign)})")
    if not csi:
        out("  !! no PROBE lines - was the firmware built with CSI_PROBE=1?")
        return {}

    out("classifier verdicts: " + ", ".join(
        f"{VERDICT[v]} {n}" for v, n in sorted(Counter(r['verdict'] for r in csi).items())))

    ack_ts = {r['rx_ts'] for r in acks}
    truth = [r for r in csi if r['rx_ts'] in ack_ts]
    res = {'acks_seen': len(acks), 'ack_csi': len(truth), 'foreign_acks': len(foreign)}
    out("")
    if not acks:
        out("No ACKs addressed to us were sniffed. Either the AP isn't answering, the")
        out("pokes aren't going out, or control frames aren't delivered on this chip.")
        if foreign:
            out("Other devices' ACKs ARE sniffed, so control frames do get through.")
        return res
    out(f"Ground truth: {len(truth)} of {len(acks)} sniffed ACKs "
        f"({len(truth) / len(acks):.0%}) also produced a CSI callback.")
    if not truth:
        out("=> ACKs do NOT produce CSI here. Set POKE_HZ 0 (beacons only) or try a")
        out("   different poke frame / rate.")
        return res

    macs = Counter(r['mac'] for r in truth).most_common(3)
    dmacs = Counter(r['dmac'] for r in truth).most_common(3)
    out("  reported mac : " + ", ".join(f"{m} x{n}" for m, n in macs))
    out("  reported dmac: " + ", ".join(f"{m} x{n}" for m, n in dmacs))
    if me and dmacs[0][0] == me:
        out("  => dmac is our own MAC: ACKs could be matched exactly by dmac.")
    elif me and dmacs[0][0] in ours:
        out("  => dmac is our made-up poke address: ACKs are matched exactly by dmac.")
    sigs = Counter((r['sig'], r['rate'], r['len']) for r in truth).most_common(3)
    out("  (sig_mode, rate, len): " + ", ".join(f"{s} x{n}" for s, n in sigs))

    dts = np.array([r['dt'] for r in truth if r['pending'] >= 0])
    if dts.size:
        p50, p95, p99 = np.percentile(dts, [50, 95, 99])
        out(f"  delay after poke (us): median {p50:.0f}, p95 {p95:.0f}, p99 {p99:.0f}, "
            f"max {dts.max()}")
        res['suggest_win'] = int(max(500, p99 * 1.5))
        out(f"  => suggested ACK_WIN_US: {res['suggest_win']}")

    tagged = [r for r in csi if r['verdict'] == 1]
    correct = sum(r['rx_ts'] in ack_ts for r in tagged)
    missed = sum(r['verdict'] != 1 for r in truth)
    res.update(tagged=len(tagged), correct=correct, missed=missed)
    out(f"  classifier: {correct}/{len(tagged)} tagged ACKs were real ACKs, "
        f"{missed} real ACKs missed")
    return res


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], errors='ignore') as f:
        report(*parse(f))


if __name__ == '__main__':
    main()
