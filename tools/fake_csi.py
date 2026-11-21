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
import base64
import math
import random
import sys
import time

import numpy as np

from analyze import PACKED_AT

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
    return csi_line(ts, src, kind, ch, rssi, buf)


def csi_line(ts, src, kind, ch, rssi, buf, text=False):
    """A CSI line as the firmware sends it (format_csi_line in csi_core.h), or
    with text=True in the first format (decimal numbers), which recordings
    made before the change hold."""
    buf = np.asarray(buf, dtype=int)
    if text:
        return f"CSI,{ts},{src},{kind},{ch},{rssi},0,0,{buf.size}," + " ".join(map(str, buf))
    if buf.size == 128 and not np.delete(buf, PACKED_AT).any():   # as lltf_pack
        buf = buf[PACKED_AT]
    data = base64.b64encode(buf.astype(np.int8).tobytes()).decode()
    return f"CSI,{ts},{src},{kind},{ch},{rssi},0,0,{buf.size},{data}"


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
# ('rssi', db): the level steps but the room doesn't change (someone in the path) |
# ('glitch', s): the router briefly switches to a very different channel shape
# and snaps back (seen on a real ISP gateway every ~20 min); not motion |
# ('scan', s): router 0 scans other channels (router 1 carries on): no packets
# from it for 175 ms of every 0.5 s, and every other burst comes back with a
# milder (~0.3) shape change. Seen every 20 min 2 s on another gateway
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
DEMO_PEER = _mac(0x24, 0x0a, 0xc4, 0x12, 0x34, 0x56)    # a second ackwave board
DEMO_ME = _mac(0x24, 0x0a, 0xc4, 0x11, 0x22, 0x33)      # the demo's own board (ME)


def fake_devices(t):
    """DEV lines as the board prints them every 5 s (no randomness, so the
    demo and the tests stay reproducible)."""
    out = []
    for i, (m, rssi, rate, wob) in enumerate(DEMO_DEVICES):
        r = rssi + (round(wob * math.sin(t * 0.7 + i)) if rssi else 0)
        out.append(f"DEV,{m},{r},{int(rate * t)},{int(t / 3)},{0 if rate else 4000},{i % 2}")  # its router
    out.append(f"PEER,{DEMO_PEER},-58,{int(t)},300,6,1")   # hellos, once a second
    mv = int(t // 40) % 3 == 1                                # its own verdict: motion now and then
    out.append(f"SAT,{DEMO_PEER},-58,{int(t)},{3 if mv else 2},{80 if mv else 18},{60 if mv else 14},"
               f"95,-60,1,{int(t)},{_mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)},6")   # its router: the demo's 2nd
    k = 3.2 if mv else 1.0                                   # its links: router, a plug, the hub
    out.append(f"SATL,{DEMO_PEER},{_mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)}:3:{int(22 * k)}:{int(21 * k)},"
               f"{DEMO_DEVICES[0][0]}:4:{int(30 * (1.1 if mv else 1))}:{int(28 * (1.1 if mv else 1))},"
               f"{DEMO_ME}:4:{int(25 * k)}:{int(24 * k)}")
    if int(t) % 10 < 5:                                      # every 10 s: who it hears
        out.append(f"SURV,{DEMO_PEER},{_mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)}:0:-60,"
                   f"{DEMO_ME}:1:-58,{DEMO_DEVICES[0][0]}:2:-55")
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
    scan_spans = [(t0, t0 + st[1]) for t0, _t1, st in steps if st[0] == 'scan']
    scan_shape = np.where(K % 2, 1.3, 0.7)
    next_stats, next_calib, pokes, acks, aps = 5.0, 5.0, 0, 0, 0
    for t, src, kind in schedule(t_end):
        while instant and t >= instant[0][0]:
            t0, st = instant.pop(0)
            if st[0] == 'hop':
                ch = 11 if ch == 6 else 6
                links, scales = new_links()      # new frequency = new response
                board.on_channel(int(t * 1e6))
                yield f"CHANNEL,{int(t * 1e6)},{ch},csa", 'channel', None, t
            elif st[0] == 'rssi':                # a body in the path: level only, same room
                rssi_off += st[1]
            elif st[0] == 'relocate':
                links, scales = new_links()      # new spot = new multipath
                rssi_off += st[1]
                noise *= st[2]
        if t >= next_stats:
            yield "ME,24:0a:c4:11:22:33", 'other', None, t
            yield "LEARNED,0,aa:bb:cc:dd:ee:f0", 'learned', None, t
            yield "LEARNED,1,aa:bb:cc:dd:ee:f1", 'learned', None, t
            for line in fake_devices(t):         # devices on the network (demo)
                yield line, line.split(',', 1)[0].lower(), None, t
            yield (f"STATS,{int(next_stats * 1e6)},{pokes},0,{acks},{aps},3,0",
                   'stats', None, t)
            next_stats += 5.0
        h = links[src].copy()
        if any(a <= t < b for a, b in glitch_spans):
            h = h * glitch_shape
        scan = [a for a, b in scan_spans if a <= t < b] if src == 0 else []
        if scan:
            cycle, phase = divmod(t - scan[0], 0.5)
            if phase < 0.175:                    # off channel: nothing heard
                pokes += kind
                continue
            if cycle % 2:
                h = h * scan_shape
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


# Other boards for --boards: each its own MAC, boot time (clock offset),
# crystal error (drift) and scene. Against DEFAULT (motion 15-21 s and 45-49 s):
# board 2 sees the first one late and the second one, board 3 only its own.
OTHER_BOARDS = [
    (_mac(0xc4, 0x9e, 0x7e, 0x10, 0x20, 0x31), 7.3, 15e-6,
     [('still', 18), ('motion', 6), ('still', 20), ('motion', 4), ('still', 11)]),
    (_mac(0x68, 0x9d, 0xd2, 0x10, 0x20, 0x32), 31.9, -12e-6,
     [('still', 33), ('motion', 4), ('still', 22)]),
]
TS_KINDS = ('CSI,', 'STATE,', 'CALIB,', 'STATS,', 'CHANNEL,')
OWN_ONLY = ('DEV,', 'PEER,', 'SAT,', 'POKING,')        # the demo's devices: the first board's


def generate_boards(n, seed=0):
    """n boards in one stream, as the hub will send them: the first board's
    lines as they are, the others' tagged '@<mac>,' with their own clocks.
    Yields (line, t) in time order."""
    import heapq

    def other(k):
        mac, boot, drift, scene = OTHER_BOARDS[k]
        for line, _kind, _label, t in generate(seed + 100 * (k + 1), scene):
            if line.startswith(OWN_ONLY):
                continue
            if line.startswith('ME,'):
                line = 'ME,' + mac
            elif line.startswith(TS_KINDS):
                p = line.split(',', 2)
                if len(p) > 1 and p[1].isdigit():        # junk lines stay junk
                    p[1] = str(int((int(p[1]) / 1e6 + boot) * (1 + drift) * 1e6))
                    line = ','.join(p)
            yield (t if t is not None else -1.0), f'@{mac},{line}'

    first = (((t if t is not None else -1.0), line) for line, _k, _l, t in generate(seed))
    streams = [first] + [other(k) for k in range(min(n, len(OTHER_BOARDS) + 1) - 1)]
    for t, line in heapq.merge(*streams, key=lambda x: x[0]):
        yield line, (t if t >= 0 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--realtime', action='store_true', help='pace output in real time')
    ap.add_argument('--loop', action='store_true', help='repeat forever (with --realtime)')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--house', action='store_true', help='a whole house for the demo: boards, a person walking, the light')
    ap.add_argument('--layout', metavar='FILE', help='with --house: write the Map layout (JSON) here and exit')
    ap.add_argument('--boards', type=int, default=1, help=f'boards in the stream (1-{len(OTHER_BOARDS) + 1}); '
                    "the others' lines are tagged @<mac>,")
    a = ap.parse_args()
    seed = a.seed
    while True:
        start = time.monotonic()
        if a.house and a.layout:                # the Map's layout, and the boards' and routers' names
            import json
            import os
            with open(a.layout, 'w') as f:
                json.dump(house_layout(), f)
            with open(os.path.join(os.path.dirname(os.path.abspath(a.layout)), 'dashboard.json'), 'w') as f:
                json.dump(dict(board_names={m: n for m, n, _p, _l, _j in HOUSE_SATS},
                               router_names={HOUSE_ROUTERS['ap:0'][0]: 'Living room', HOUSE_ROUTERS['ap:1'][0]: 'Upstairs'},
                               per_link=True), f)
            return
        rows = (house_lines(seed=seed) if a.house else
                ((line, t) for line, _k, _l, t in generate(seed)) if a.boards <= 1 else generate_boards(a.boards, seed))
        for line, t in rows:
            if a.realtime and t is not None:
                delay = start + t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            print(line, flush=a.realtime)
        if not a.loop:
            break
        seed += 1


# ---------- A whole house for the demo (./csi demo-web) ----------
# One person walks a route; every link of every board sees motion exactly
# when the person crosses that link's sensitive zone (the Map's model). The
# hub's own raw signal follows its links; the satellites send what a real hub
# prints for them (SAT, SATL, SURV). A device joins and a satellite comes
# online partway through, so the dashboard shows them arriving.
HOUSE_ROUTERS = {'ap:0': (_mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf0), (7.0, 1.0)),
                 'ap:1': (_mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1), (-1.5, 7.5))}
HOUSE_SATS = [  # MAC, name, position, its links' far ends (router key, device MAC, 'hub'), joins at (s)
    (_mac(0xc4, 0x9e, 0x7e, 0x10, 0x20, 0x31), 'Kitchen', (10.0, 6.5), ['ap:0', 'dev0', 'hub'], 0),
    (_mac(0x68, 0x9d, 0xd2, 0x10, 0x20, 0x32), 'Bedroom', (2.5, 11.0), ['ap:1', 'dev1', 'hub'], 0),
    (_mac(0x24, 0x0a, 0xc4, 0x10, 0x20, 0x33), 'Office', (13.0, -1.0), ['ap:0', 'dev2'], 25),
]
HOUSE_DEVS = {'dev0': (DEMO_DEVICES[0][0], (7.0, 9.0)), 'dev1': (DEMO_DEVICES[1][0], (5.5, 12.5)),
              'dev2': (DEMO_DEVICES[3][0], (11.0, -3.0))}
HOUSE_NEW_DEV = (_mac(0x50, 0x02, 0x91, 0x77, 0x66, 0x55), 40)   # a device that joins at 40 s
HOUSE_LIGHT = (8.5, 8.0)
HOUSE_ROUTE = [(1.0, 3.0), (6.0, 4.0), (9.5, 8.5), (6.0, 11.5), (2.0, 10.0), (1.0, 3.0), (11.0, 1.0), (12.5, -2.0), (1.0, 3.0)]
HOUSE_SPEED = 0.9                    # m/s, a slow walk
HOUSE_PAUSE = 6.0                    # s standing still at each stop
LAMBDA = 0.1235                      # m, channel 6


def house_layout():
    """The positions the demo seeds the Map with (metres from the hub)."""
    nodes = [dict(key=k, x=p[0], y=p[1], placed=True, name=f'Router {int(k[3:]) + 1}') for k, (_m, p) in HOUSE_ROUTERS.items()]
    nodes += [dict(key='sat:' + m, x=p[0], y=p[1], placed=True, name=n) for m, n, p, _l, _j in HOUSE_SATS]
    nodes += [dict(key=m, x=p[0], y=p[1], placed=True, name=m) for m, p in HOUSE_DEVS.values()]
    nodes.append(dict(key='light', x=HOUSE_LIGHT[0], y=HOUSE_LIGHT[1], placed=True, name='Light'))
    return dict(unit='m', env='home', nodes=nodes)


def house_person(t):
    """Where the person is at t s: walking the route, pausing at each stop."""
    legs = []
    for a, b in zip(HOUSE_ROUTE, HOUSE_ROUTE[1:]):
        legs.append((a, b, math.dist(a, b) / HOUSE_SPEED))
    loop = sum(d + HOUSE_PAUSE for _a, _b, d in legs)
    t %= loop
    for a, b, d in legs:
        if t < HOUSE_PAUSE:
            return a, False
        t -= HOUSE_PAUSE
        if t < d:
            f = t / d
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), True
        t -= d
    return HOUSE_ROUTE[0], False


def house_sens(p, a, b):
    """How strongly a person at p changes link a-b (the Map's estimate)."""
    d = math.dist(a, b)
    d1, d2 = math.dist(p, a), math.dist(p, b)
    zones = (d1 + d2 - d) / (LAMBDA / 2)
    along = 1 / (1 + 0.22 * zones)
    return min(1.0, along * along + 0.55 * math.exp(-min(d1, d2) / 0.7))


def house_end(key):
    if key == 'hub':
        return (0.0, 0.0)
    if key in HOUSE_ROUTERS:
        return HOUSE_ROUTERS[key][1]
    return HOUSE_DEVS[key][1]


def house_lines(duration=240.0, seed=0):
    """(line, t) in time order: the hub's own data plus the satellites' reports."""
    import heapq
    step = 0.5
    # the hub sees motion while the walking person is near one of its router links
    hub_links = [house_end(k) for k in HOUSE_ROUTERS]
    states, t = [], 0.0
    while t < duration:
        p, walking = house_person(t)
        on = walking and max(house_sens(p, (0.0, 0.0), e) for e in hub_links) > 0.3
        states.append('motion' if on else 'still')
        t += step
    scenario, run = [], 1
    for prev, cur in zip(states, states[1:] + [None]):
        if cur == prev:
            run += 1
        else:
            scenario.append((prev, run * step))
            run = 1
    hub = ((tt if tt is not None else -1.0, line) for line, _k, _l, tt in generate(seed, scenario)
           if DEMO_PEER not in line)            # the plain demo's extra board: not in this house

    def sats():
        t, seq = 0.0, 0
        rng = random.Random(seed)
        while t < duration:
            p, walking = house_person(t)
            seq += 1
            yield t, f"DEMO,person,{p[0]:.2f},{p[1]:.2f},{1 if walking else 0}"   # the truth, for the Map (demo only)
            for mac, _name, pos, ends, joins in HOUSE_SATS:
                if t < joins:
                    continue
                links, any_mv = [], False
                for e in ends:
                    v = house_sens(p, pos, house_end(e)) if walking else 0.0
                    mv = v > 0.3
                    any_mv |= mv
                    k = 1.0 + 4.5 * v + rng.uniform(-0.15, 0.15)   # in proportion, as a real link reacts
                    far = (HOUSE_ROUTERS[e][0] if e in HOUSE_ROUTERS else DEMO_ME if e == 'hub' else HOUSE_DEVS[e][0])
                    flags = 3 if e in HOUSE_ROUTERS else 4
                    links.append(f"{far}:{flags}:{int(20 * k)}:{int(19 * k)}")
                rssi = -45 - int(3 * math.dist(pos, (0, 0)))
                ks = int(50 * (1.6 if any_mv else 0.35))
                yield t, (f"SAT,{mac},{rssi},{seq},{3 if any_mv else 2},{ks},{int(ks * 0.8)},95,-58,{len(ends)},"
                          f"{int(t)},{HOUSE_ROUTERS[ends[0]][0]},6")
                yield t, f"SATL,{mac}," + ','.join(links)
                if seq % 20 == 1:
                    yield t, (f"SURV,{mac}," + ','.join(f"{HOUSE_ROUTERS[k][0]}:0:{-40 - int(4 * math.dist(pos, HOUSE_ROUTERS[k][1]))}"
                                                        for k in HOUSE_ROUTERS))
            if HOUSE_NEW_DEV[1] <= t < HOUSE_NEW_DEV[1] + step:
                yield t, f"DEV,{HOUSE_NEW_DEV[0]},-61,5,1,200,0"
            t += step

    for t, line in heapq.merge(hub, sats(), key=lambda x: x[0]):
        yield line, (t if t >= 0 else None)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):  # reader exited early (e.g. | head)
        sys.stderr.close()
