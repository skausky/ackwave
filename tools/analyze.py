#!/usr/bin/env python3
"""Live CSI motion analyzer (text output). For the graphical view use gui.py.
Usage: python analyze.py /dev/ttyUSB0       (or COM5)
       python fake_csi.py | python analyze.py -
"""
import argparse
import math
import sys
from collections import defaultdict

import numpy as np

# LLTF subcarriers 0..63; drop DC (0) and guards (27-37) -> 52 values.
# Must match lltf_amplitudes() in main/csi_core.h.
VALID = np.r_[1:27, 38:64]
KIND_NAMES = {0: 'ap', 1: 'ack'}
MAX_APS = 8          # nodes 0..7 are APs, 8.. are devices we poke (csi_core.h)

# Defaults shared with the firmware: md_cfg_default() in main/csi_core.h.
# Keep them in sync (test_pipeline.py checks C and Python agree).
CFG = dict(
    tau_lp=0.032, tau_hp=0.5,            # band-pass ~0.3-5 Hz
    tau_short=0.5, tau_long=4.0,         # energy windows (s)
    warm_s=2.0, min_pkts=8, stale_s=2.0,
    settle_us=1_000_000,                 # ignore data after a channel change
    on_short=2.8, off_short=2.0,         # thresholds, multiples of the floor
    on_long=1.4, off_long=1.2,
    long_cap=3.0,                        # long window input cap, x floor
    sample_us=100_000, floor_every_us=1_000_000,
    floor_min_n=100, floor_pct=10, floor_up_per_s=0.01,
    floor_lo=0.005, floor_hi=0.2,
    rssi_tau_fast=5.0, rssi_tau_slow=60.0, rssi_step_db=4.0,
    rssi_min_age_us=10_000_000,
    glitch_shape=0.4,                    # ignore packets whose shape jumps this far...
    glitch_hold_us=5_000_000,            # ...unless the new shape lasts this long
    tau_ref=1.0,                         # shape reference average (s)
)
SETTLE_US = CFG['settle_us']
TOP_K = 8
RING = 1200                              # floor samples: 120 s at 10 Hz
PHASES = ('wait', 'collect', 'fast', 'track')
WHYS = ('boot', 'button', 'rssi', 'channel')


def parse_line(line):
    """Returns (kind, dict) for the board's lines (CSI, CHANNEL, LEARNED, STATE,
    STATS, CALIB, GATE, DEV, POKING, UNPOKED, REFUSED), else None."""
    line = line.strip()
    try:
        if line.startswith('CSI,'):
            p = line.split(',', 9)
            if len(p) != 10:
                return None
            ts, src, kind, ch, rssi, sig, cwb, n = map(int, p[1:9])
            raw = np.fromiter(map(int, p[9].split()), dtype=np.int16)
            if n < 128 or raw.size != n or kind not in KIND_NAMES:
                return None
            if raw.min() < -128 or raw.max() > 127:
                return None
            lltf = raw[:128].astype(float)               # [imag, real] pairs
            amp = np.hypot(lltf[1::2], lltf[0::2])
            return 'csi', dict(ts=ts, src=src, kind=kind, ch=ch, rssi=rssi,
                               sig_mode=sig, amp=amp)
        if line.startswith('CHANNEL,'):
            p = line.split(',')
            return 'channel', dict(ts=int(p[1]), ch=int(p[2]),
                                   why=p[3] if len(p) > 3 else '')
        if line.startswith('LEARNED,'):
            p = line.split(',')
            return 'learned', dict(idx=int(p[1]), mac=p[2])
        if line.startswith('GATE,'):                     # per-link poke health
            p = line.split(',')
            if len(p) != 6 or p[5] not in ('ok', 'weak', 'silent'):
                return None
            return 'gate', dict(idx=int(p[1]), rssi=int(p[2]), yield_pct=int(p[3]),
                                sent=int(p[4]), state=p[5])
        if line.startswith('DEV,'):                      # a device heard on our network
            p = line.split(',')
            if len(p) != 6 or len(p[1]) != 17:
                return None
            return 'dev', dict(mac=p[1].lower(), rssi=int(p[2]), tx=int(p[3]),
                               rx=int(p[4]), age_ms=int(p[5]))
        if line.startswith('POKING,'):
            p = line.split(',')
            return 'poking', dict(idx=int(p[1]), mac=p[2].lower())
        if line.startswith('UNPOKED,'):
            return 'unpoked', dict(mac=line.split(',')[1].lower())
        if line.startswith('REFUSED,'):
            p = line.split(',')
            return 'refused', dict(mac=p[1].lower() if len(p) > 2 else '',
                                   why=p[-1])
        if line.startswith('STATS,'):                    # firmware counters
            p = line.split(',')
            if len(p) not in (8, 9):                     # 9th field: newer firmware
                return None
            ts, poke, tx_err, ack, ap, drop, qfull = map(int, p[1:8])
            return 'stats', dict(ts=ts, poke=poke, tx_err=tx_err, ack=ack, ap=ap,
                                 drop=drop, qfull=qfull,
                                 reply=int(p[8]) if len(p) > 8 else 0)
        if line.startswith('STATE,'):                    # firmware's own verdict
            p = line.split(',')
            if p[2] not in ('motion', 'still'):
                return None
            return 'state', dict(ts=int(p[1]), motion=p[2] == 'motion',
                                 score=int(p[3]) / 1000,
                                 score_long=int(p[4]) / 1000 if len(p) > 4 else None)
        if line.startswith('CALIB,'):                    # firmware's quiet level
            p = line.split(',')
            if len(p) != 7 or p[2] not in PHASES or p[3] not in WHYS:
                return None
            return 'calib', dict(ts=int(p[1]), phase=p[2], why=p[3],
                                 floor=int(p[4]) / 1e6, on_short=int(p[5]) / 1e6,
                                 on_long=int(p[6]) / 1e6)
    except (ValueError, IndexError):
        return None
    return None


class Stream:
    __slots__ = ('n', 'n_glitch', 't_start', 't_last', 'rssi_t0', 'score_s', 'score_l',
                 'rssi_f', 'rssi_s', 'lp', 'hp', 'es', 'el', 'ref')

    def __init__(self):
        self.n = self.n_glitch = 0
        self.t_start = self.t_last = self.rssi_t0 = 0
        self.score_s = self.score_l = None
        self.rssi_f = self.rssi_s = 0.0
        self.lp = self.hp = self.es = self.el = self.ref = None


def _alpha(dt, tau):
    return 1.0 - math.exp(-dt / tau)


class MotionDetector:
    """Python twin of motion_det_t in csi_core.h (same maths, float64).
    on_csi() then evaluate() for every packet, on_channel() on a hop."""

    def __init__(self, cfg=None, now_us=0):
        self.c = dict(CFG, **(cfg or {}))
        self.settle_until = 0
        self.motion = self.m_short = self.m_long = False
        self.streams = {}
        self.external = False            # floor supplied by the board (CALIB lines)
        self.recalibrate(now_us, 0, 'boot')

    # ---- floor / calibration ----
    def recalibrate(self, now_us, delay_us=0, why='button'):
        self.phase, self.why, self.cal_event = 'wait', why, True
        self.collect_from = self.next_sample = self.next_floor = now_us + delay_us
        self.ring = []                   # oldest first, at most RING
        self.floor = 0.0
        for s in self.streams.values():  # current RSSI is the new normal
            s.rssi_s, s.rssi_t0 = s.rssi_f, now_us

    def set_external(self, floor, phase, why):
        """Use the board's floor instead of measuring our own."""
        self.external = True
        self.floor, self.phase, self.why = floor, phase, why

    def on_channel(self, ts):
        self.streams.clear()             # old baselines are invalid
        self.settle_until = ts + self.c['settle_us']
        if not self.external:
            booting = self.why == 'boot' and self.floor <= 0   # e.g. the start-up scan
            self.recalibrate(ts, 0, 'boot' if booting else 'channel')

    def thresholds(self):
        f = self.floor
        return (self.c['on_short'] * f, self.c['on_long'] * f) if f > 0 else (None, None)

    # ---- per packet ----
    def on_csi(self, d):
        """Returns this stream's short-window score, or None if not ready."""
        c, ts = self.c, d['ts']
        if ts < self.settle_until:
            return None
        a = d['amp'][VALID]
        mean = a.mean()
        if mean <= 0:
            return None
        x = a / mean
        s = self.streams.setdefault((d['src'], d['kind']), Stream())
        if s.n > 0:
            # Glitch check (see md_update): ignore a sudden big shape change
            # unless it lasts glitch_hold
            since = ts - s.t_last
            if (np.abs(x - s.ref).mean() > c['glitch_shape']
                    and since < c['glitch_hold_us']):
                s.n_glitch += 1
                return None
            a_r = 1.0 if since >= c['glitch_hold_us'] else _alpha(max(0, since) * 1e-6, c['tau_ref'])
            s.ref += a_r * (x - s.ref)
        if s.n == 0:
            s.lp, s.hp, s.ref = x.copy(), x.copy(), x.copy()
            s.es, s.el = np.zeros_like(x), np.zeros_like(x)
            s.t_start = s.rssi_t0 = ts
            s.rssi_f = s.rssi_s = float(d['rssi'])
        else:
            dt = max(0.0, (ts - s.t_last) * 1e-6)
            s.lp += _alpha(dt, c['tau_lp']) * (x - s.lp)
            s.hp += _alpha(dt, c['tau_hp']) * (x - s.hp)
            y2 = (s.lp - s.hp) ** 2
            s.es += _alpha(dt, c['tau_short']) * (y2 - s.es)
            lcap = (c['long_cap'] * self.floor) ** 2 if self.floor > 0 else math.inf
            s.el += _alpha(dt, c['tau_long']) * (np.minimum(y2, lcap) - s.el)
            np.minimum(s.el, lcap, out=s.el)   # e.g. built up before a floor existed
            inv_n = 1.0 / (s.n + 1)          # running mean first: no start-up bias
            a_f = max(_alpha(dt, c['rssi_tau_fast']), inv_n)
            a_sl = max(_alpha(dt, c['rssi_tau_slow']), inv_n)
            if ts - s.rssi_t0 < c['rssi_min_age_us']:
                a_sl = a_f                   # settle at the current level first
            s.rssi_f += a_f * (d['rssi'] - s.rssi_f)
            s.rssi_s += a_sl * (d['rssi'] - s.rssi_s)
        s.n += 1
        s.t_last = ts
        s.score_s = s.score_l = None
        if (not self.external and ts - s.rssi_t0 >= c['rssi_min_age_us']
                and abs(s.rssi_f - s.rssi_s) > c['rssi_step_db']):
            self.recalibrate(ts, 0, 'rssi')
        if s.n < c['min_pkts'] or (ts - s.t_start) * 1e-6 < c['warm_s']:
            return None
        s.score_s = math.sqrt(np.sort(s.es)[-TOP_K:].mean())
        s.score_l = math.sqrt(np.sort(s.el)[-TOP_K:].mean())
        return s.score_s

    def max_scores(self, now_us):
        """(short, long): highest scores among live streams, None if none."""
        stale = self.c['stale_s'] * 1e6
        def alive(key):
            s = self.streams.get(key)
            return s is not None and now_us - s.t_last <= stale
        # As on the board: APs decide, and an AP's own frames only while it
        # has no live ACK stream (they're sparse mixed traffic)
        live = [s for (src, k), s in self.streams.items()
                if src < MAX_APS and s.score_s is not None and now_us - s.t_last <= stale
                and not (k == 0 and alive((src, 1)))]
        if not live:
            return None, None
        return max(s.score_s for s in live), max(s.score_l for s in live)

    def max_score(self, now_us):
        return self.max_scores(now_us)[0]

    def _floor_step(self, ts, ms):
        c = self.c
        if self.external or ts < self.collect_from:
            return
        if self.phase == 'wait':
            self.phase, self.cal_event = 'collect', True
        if ms is not None and ts >= self.next_sample:
            self.ring.append(ms)
            if len(self.ring) > RING:
                del self.ring[0]
            self.next_sample = ts + c['sample_us']
        n = len(self.ring)
        if ts < self.next_floor or n < c['floor_min_n']:
            return
        self.next_floor = ts + c['floor_every_us']
        q = sorted(self.ring)[n * c['floor_pct'] // 100]
        if self.phase == 'track':
            q = min(q, self.floor * (1 + c['floor_up_per_s'] * c['floor_every_us'] * 1e-6))
        else:
            nxt = 'track' if n == RING else 'fast'
            if self.phase == 'collect':      # first floor: restart the long window
                for st in self.streams.values():
                    if st.el is not None:
                        st.el[:] = 0
                    if st.score_l is not None and st.score_l > 0:
                        st.score_l = 0.0
            if nxt != self.phase:
                self.phase, self.cal_event = nxt, True
        self.floor = min(max(q, c['floor_lo']), c['floor_hi'])

    def evaluate(self, now_us):
        """+1 motion started, -1 motion ended, 0 no change."""
        c = self.c
        ms, _ = self.max_scores(now_us)
        self._floor_step(now_us, ms)
        ms, ml = self.max_scores(now_us)     # the floor step may restart the long window
        ms = -1.0 if ms is None else ms
        ml = -1.0 if ml is None else ml
        f = self.floor
        if f <= 0:
            self.m_short = self.m_long = False
        else:
            if not self.m_short and ms > c['on_short'] * f:
                self.m_short = True
            elif self.m_short and ms < c['off_short'] * f:
                self.m_short = False
            if not self.m_long and ml > c['on_long'] * f:
                self.m_long = True
            elif self.m_long and ml < c['off_long'] * f:
                self.m_long = False
        m = self.m_short or self.m_long
        change = 0 if m == self.motion else (1 if m else -1)
        self.motion = m
        return change

    def take_cal_event(self):
        e, self.cal_event = self.cal_event, False
        return e

    def calib_line(self, ts):
        """The CALIB line the firmware prints for this state."""
        on_s, on_l = self.thresholds()
        return (f"CALIB,{ts},{self.phase},{self.why},{int(self.floor * 1e6)},"
                f"{int((on_s or 0) * 1e6)},{int((on_l or 0) * 1e6)}")


def open_port(src, baud):
    """(lines, send): an iterator of text lines, and send(text) to write a
    command to the board (None for stdin / a file)."""
    if src == '-':
        return sys.stdin, None
    import serial                            # pip install pyserial
    port = serial.Serial(src, baud, timeout=1)
    lines = (b.decode('utf-8', 'ignore') for b in iter(port.readline, None))
    return lines, lambda text: port.write(text.encode('ascii') + b'\n')


def open_source(src, baud):
    return open_port(src, baud)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('source', help="serial port (/dev/ttyUSB0, COM5) or '-' for stdin")
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--every', type=int, default=25, help='print every N packets per stream')
    a = ap.parse_args()

    det = MotionDetector()
    count = defaultdict(int)
    nodes = {}
    for line in open_source(a.source, a.baud):
        ev = parse_line(line)
        if ev is None:
            continue
        kind, d = ev
        if kind == 'learned':
            if nodes.get(d['idx']) != d['mac']:      # re-sent every 5 s; print once
                nodes[d['idx']] = d['mac']
                print(f"node {d['idx']} = {d['mac']}")
        elif kind == 'channel':
            print(f"--- channel -> {d['ch']} ({d['why']})")
            det.on_channel(d['ts'])
        elif kind == 'stats':
            print(f"--- board: {d['poke']} pokes, {d['ack']} ACKs, {d['ap']} AP frames, "
                  f"{d['tx_err']} tx errors, {d['qfull']} queue drops")
        elif kind == 'calib':
            print(f"--- board quiet level {d['floor']:.4f} ({d['phase']}, {d['why']})")
        elif kind == 'state':
            print(f"*** board says {'MOTION' if d['motion'] else 'still'} "
                  f"(score {d['score']:.3f})")
        else:
            score = det.on_csi(d)
            change = det.evaluate(d['ts'])
            if det.take_cal_event():
                print(f"--- PC calibration: {det.phase} ({det.why})")
            if change:
                print(f">>> {'MOTION' if change > 0 else 'still'}")
            key = (d['src'], d['kind'])
            count[key] += 1
            if score is not None and count[key] % a.every == 0:
                state = 'MOTION' if det.motion else 'still'
                ms, ml = det.max_scores(d['ts'])
                rel = (f"x{ms / det.floor:4.1f} short  x{ml / det.floor:4.1f} long"
                       if det.floor > 0 else "calibrating")
                print(f"node {d['src']} {KIND_NAMES[d['kind']]:3s} ch {d['ch']:2d} "
                      f"rssi {d['rssi']:4d} score {score:.3f}  {rel}  {state}")


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        sys.stderr.close()                   # quiet exit on Ctrl-C or closed pipe
