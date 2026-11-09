#!/usr/bin/env python3
"""Generates realistic fake firmware output for 2 nodes:
beacons at ~10 Hz (kind 0) and ACKs to our pokes at 40 Hz (kind 1, weaker),
following a scenario (default: still while calibrating -> motion -> still ->
channel hop 6->11 -> still -> motion -> still), plus junk lines. STATE and
CALIB lines come from the same detector the board runs.

    python fake_csi.py              # print to stdout
    python fake_csi.py --realtime   # paced like the real board (for gui.py)
"""
import argparse
import math
import sys
import time

import numpy as np

K = np.arange(64)
NULLS = [0] + list(range(27, 38))
BEACON_S = 0.1024               # 100 TU
ACK_HZ = 40                     # per node
ACK_GAIN = 0.6                  # ACKs come back weaker than beacons here

JUNK = [
    "ESP-ROM:esp32s3-20210327",
    "I (312) wifi:mode : null",
    "",
    "CSI,garbage",
    "CSI,1,2,3",
    "CSI,100,0,6,-40,0,0,128,1 2 3",                    # old format (no kind)
    "CSI,100,0,0,6,-40,0,0,128,1 2 3",                  # length mismatch
    "CSI,100,0,7,6,-40,0,0,128," + " ".join(["0"] * 128),   # unknown kind
    "CSI,100,0,0,6,-40,0,0,128," + " ".join(["300"] * 128),  # out of int8 range
    "CHANNEL,abc,6,scan",
    "STATE,5,dancing,100",
    "\x00\xffline noise",
]


def static_channel(rng, paths=4):
    """Static multipath frequency response over 64 subcarriers."""
    h = np.zeros(64, complex)
    for _ in range(paths):
        a = rng.uniform(0.3, 1.0)
        delay = rng.uniform(0.0, 0.1)
        ph = rng.uniform(0, 2 * np.pi)
        h += a * np.exp(-1j * (2 * np.pi * K * delay + ph))
    return h


def to_line(ts, src, kind, ch, h, scale, rssi):
    h = h * scale
    h[NULLS] = 0
    buf = np.empty(128, dtype=int)
    buf[0::2] = np.clip(np.round(h.imag), -128, 127)   # imag first
    buf[1::2] = np.clip(np.round(h.real), -128, 127)
    return f"CSI,{ts},{src},{kind},{ch},{rssi},0,0,128," + " ".join(map(str, buf))


def schedule(t_end):
    """(t, src, kind) for every packet, in time order."""
    ev = []
    for src in (0, 1):
        t = 0.013 + 0.037 * src
        while t < t_end:
            ev.append((t, src, 0))
            t += BEACON_S
        t = 0.005 + src / (2 * ACK_HZ)
        while t < t_end:
            ev.append((t, src, 1))
            t += 1 / ACK_HZ
    return sorted(ev)


# Scenario steps: ('still', s) | ('motion', s) | ('hop',) |
# ('relocate', rssi_change_db, noise_factor): the board is moved (new multipath) |
# ('glitch', s): the router briefly switches to a very different channel shape
# and snaps back (seen on a real ISP gateway every ~20 min); not motion
DEFAULT = [('still', 15), ('motion', 6), ('still', 8), ('hop',),
           ('still', 16), ('motion', 4), ('still', 10)]


def timeline(scenario):
    """[(t_start, t_end, step)] for timed steps, plus instant steps at their time."""
    out, t = [], 0.0
    for step in scenario:
        if step[0] in ('still', 'motion'):
            out.append((t, t + step[1], step))
            t += step[1]
        else:
            out.append((t, t, step))
    return out, t


def _mac(*b):
    return ':'.join(f"{x:02x}" for x in b)


# (MAC, signal dBm, frames/s, signal wobble dB): a smart plug and a TV that
# sit still, a phone with a private MAC, and a console that sleeps
DEMO_DEVICES = [(_mac(0x50, 0x02, 0x91, 0x10, 0x20, 0x30), -62, 2.0, 0.5),
                (_mac(0x3c, 0x22, 0xfb, 0x40, 0x50, 0x60), -55, 6.0, 1.0),
                (_mac(0xda, 0xa1, 0x19, 0x70, 0x80, 0x90), -48, 9.0, 6.0),
                (_mac(0xa4, 0xc1, 0xe8, 0xa0, 0xb0, 0xc0), 0, 0.0, 0.0)]


def fake_devices(t):
    """DEV lines as the board prints them every 5 s (no randomness, so the
    demo and the tests stay reproducible)."""
    out = []
    for i, (m, rssi, rate, wob) in enumerate(DEMO_DEVICES):
        r = rssi + (round(wob * math.sin(t * 0.7 + i)) if rssi else 0)
        out.append(f"DEV,{m},{r},{int(rate * t)},{int(t / 3)},{0 if rate else 4000}")
    return out


def generate(seed=0, scenario=DEFAULT):
    """Yields (line, kind, label, t). kind: junk|learned|channel|csi|stats|state|calib|other.
    label is 'still'/'motion' for CSI lines."""
    from analyze import MotionDetector, parse_line      # the board's twin
    board = MotionDetector()
    board.take_cal_event()                              # boot CALIB printed below
    rng = np.random.default_rng(seed)
    for j in JUNK:
        yield j, 'junk', None, None
    yield "ME,24:0a:c4:11:22:33", 'other', None, None
    yield "BUZZER,off", 'other', None, None
    yield "BUTTON,gpio0", 'other', None, None
    yield "LEARNED,0,aa:bb:cc:dd:ee:f0", 'learned', None, None
    yield "LEARNED,1,aa:bb:cc:dd:ee:f1", 'learned', None, None
    yield board.calib_line(0), 'calib', None, 0.0

    def new_links():
        hs = {s: static_channel(rng) for s in (0, 1)}
        return hs, {s: 20.0 / np.abs(h).max() for s, h in hs.items()}

    links, scales = new_links()
    ch, rssi_off, noise = 6, 0.0, 0.03
    steps, t_end = timeline(scenario)
    instant = [(t0, st) for t0, t1, st in steps if t0 == t1]
    moving_spans = [(t0, t1) for t0, t1, st in steps if st[0] == 'motion']
    glitch_spans = [(t0, t0 + st[1]) for t0, _t1, st in steps if st[0] == 'glitch']
    glitch_shape = np.where(K % 2, 1.7, 0.3)    # alternate strong/weak subcarriers
    next_stats, next_calib, pokes, acks, aps = 5.0, 5.0, 0, 0, 0
    for t, src, kind in schedule(t_end):
        while instant and t >= instant[0][0]:
            t0, st = instant.pop(0)
            if st[0] == 'hop':
                ch = 11 if ch == 6 else 6
                links, scales = new_links()      # new frequency = new response
                board.on_channel(int(t * 1e6))
                yield f"CHANNEL,{int(t * 1e6)},{ch},csa", 'channel', None, t
            elif st[0] == 'relocate':
                links, scales = new_links()      # new spot = new multipath
                rssi_off += st[1]
                noise *= st[2]
        if t >= next_stats:
            yield "ME,24:0a:c4:11:22:33", 'other', None, t
            yield "LEARNED,0,aa:bb:cc:dd:ee:f0", 'learned', None, t
            yield "LEARNED,1,aa:bb:cc:dd:ee:f1", 'learned', None, t
            for line in fake_devices(t):         # devices on the network (demo)
                yield line, 'dev', None, t
            yield (f"STATS,{int(next_stats * 1e6)},{pokes},0,{acks},{aps},3,0",
                   'stats', None, t)
            next_stats += 5.0
        h = links[src].copy()
        if any(a <= t < b for a, b in glitch_spans):
            h = h * glitch_shape
        moving = any(a <= t < b for a, b in moving_spans)
        if moving:                               # a moving reflector
            fd = 2.0 + 0.7 * src
            h += 0.5 * np.exp(-1j * (2 * np.pi * K * 0.05 + 2 * np.pi * fd * t))
        h += rng.normal(0, noise, 64) + 1j * rng.normal(0, noise, 64)
        gain = rng.uniform(0.9, 1.1) * (ACK_GAIN if kind else 1.0)   # AGC jitter
        rssi = int(rng.integers(-50, -40)) - (6 if kind else 0) + int(rssi_off)
        if kind:
            pokes += 1
            acks += 1
        else:
            aps += 1
        line = to_line(int(t * 1e6), src, kind, ch, h, scales[src] * gain, rssi)
        yield line, 'csi', 'motion' if moving else 'still', t
        d = parse_line(line)[1]
        board.on_csi(d)
        change = board.evaluate(d['ts'])
        if board.take_cal_event() or t >= next_calib:  # as print_task does
            next_calib = t + 5.0
            yield board.calib_line(d['ts']), 'calib', None, t
        if change:
            ms, ml = board.max_scores(d['ts'])
            yield (f"STATE,{d['ts']},{'motion' if change > 0 else 'still'},"
                   f"{int((ms or 0) * 1000)},{int((ml or 0) * 1000)}"), 'state', None, t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--realtime', action='store_true', help='pace output in real time')
    ap.add_argument('--loop', action='store_true', help='repeat forever (with --realtime)')
    ap.add_argument('--seed', type=int, default=0)
    a = ap.parse_args()
    seed = a.seed
    while True:
        start = time.monotonic()
        for line, _kind, _label, t in generate(seed):
            if a.realtime and t is not None:
                delay = start + t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            print(line, flush=a.realtime)
        if not a.loop:
            break
        seed += 1


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):  # reader exited early (e.g. | head)
        sys.stderr.close()
