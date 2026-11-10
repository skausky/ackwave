#!/usr/bin/env python3
"""End-to-end tests: firmware-format lines -> parser -> motion detector.

Synthetic scenarios (fake_csi.py) cover calibration, motion during
calibration, a moved board (RSSI step), a room that got noisier without one,
and channel hops. Two real captures from an ESP32-S3 (test/data) check the
detector on real noise. Every run is also fed to the firmware's C detector
(build-host/score_lines) and both must agree on scores, verdicts and floor."""
import gzip
import math
import os
import subprocess
import sys

import numpy as np

from analyze import MotionDetector, SETTLE_US, parse_line
from fake_csi import generate

HERE = os.path.dirname(os.path.abspath(__file__))
C_SCORER = os.path.join(HERE, '..', 'build-host', 'score_lines')
DATA = os.path.join(HERE, '..', 'test', 'data')
DETECT_S = 1.0          # motion must be flagged within this of its start
HOLD_S = 10.0           # ...and cleared within this of its end


# ---------------------------------------------------------------- runners
def run_python(lines, cfg=None):
    """Feeds lines like the board would. Returns a dict of everything observed."""
    det = MotionDetector(cfg)
    det.take_cal_event()
    r = dict(scores={}, states=[], calib=[], floors=[], verdict=[], n_csi=0, n_bad=0,
             hops=[], det=det)
    last_floor = 0.0
    for line in lines:
        ev = parse_line(line)
        if ev is None:
            if line.startswith('CSI,'):
                r['n_bad'] += 1
            continue
        kind, d = ev
        if kind == 'channel':
            det.on_channel(d['ts'])
            r['hops'].append(d['ts'])
            continue
        if kind != 'csi':
            continue
        r['n_csi'] += 1
        s = det.on_csi(d)
        if s is not None:
            st = det.streams[(d['src'], d['kind'])]
            r['scores'][(d['ts'], d['src'], d['kind'])] = (s, st.score_l)
        change = det.evaluate(d['ts'])
        if det.take_cal_event():
            r['calib'].append((d['ts'], det.phase, det.why))
        if change:
            r['states'].append((d['ts'], 'motion' if change > 0 else 'still'))
        if det.floor != last_floor:
            r['floors'].append((d['ts'], det.floor))
            last_floor = det.floor
        r['verdict'].append((d['ts'], det.motion, det.floor > 0))
    return r


def run_c(lines):
    if not os.path.exists(C_SCORER):
        return None
    out = subprocess.run([C_SCORER], input='\n'.join(lines) + '\n', text=True,
                         capture_output=True, check=True).stdout
    r = dict(scores={}, states=[], calib=[], floors=[])
    for row in out.splitlines():
        f = row.split()
        if f[0] == 'SCORE':
            r['scores'][(int(f[1]), int(f[2]), int(f[3]))] = (float(f[4]), float(f[5]))
        elif f[0] == 'STATE':
            r['states'].append((int(f[1]), f[2]))
        elif f[0] == 'CALIB':
            r['calib'].append((int(f[1]), f[2], f[3]))
        elif f[0] == 'FLOOR':
            r['floors'].append((int(f[1]), float(f[2])))
    return r


def step_value(steps, ts):
    v = 0.0
    for t, x in steps:
        if t > ts:
            break
        v = x
    return v


def check_c_agrees(name, py, lines):
    c = run_c(lines)
    if c is None:
        print(f"  [{name}] (C scorer not built - run 'make test-py' for the C/Python check)")
        return
    assert c['scores'].keys() == py['scores'].keys(), \
        f"{name}: C scored {len(c['scores'])} packets, Python {len(py['scores'])}"
    ds = max((abs(c['scores'][k][0] - v[0]) for k, v in py['scores'].items()), default=0)
    dl = max((abs(c['scores'][k][1] - v[1]) for k, v in py['scores'].items()), default=0)
    times = sorted({t for t, _ in py['floors']} | {t for t, _ in c['floors']})
    df = max((abs(step_value(c['floors'], t) - step_value(py['floors'], t))
              / max(step_value(py['floors'], t), 1e-9) for t in times), default=0)
    print(f"  [{name}] C vs Python: {len(py['scores'])} scores, max diff short {ds:.1e} "
          f"long {dl:.1e}, floor {df:.1e} relative; {len(py['states'])} state changes")
    assert ds < 1e-4 and dl < 1e-4, f"{name}: C and Python scores disagree"
    assert df < 1e-3, f"{name}: C and Python floors disagree"
    assert c['states'] == py['states'], f"{name}: states differ: C {c['states']} Py {py['states']}"
    assert c['calib'] == py['calib'], f"{name}: calibration differs: C {c['calib']} Py {py['calib']}"


# ---------------------------------------------------------------- helpers
def spans(meta, label):
    """[(t0, t1)] of consecutive CSI lines with this label (seconds)."""
    out, cur = [], None
    for _line, kind, lab, t in meta:
        if kind != 'csi':
            continue
        if lab == label:
            cur = [t, t] if cur is None else [cur[0], t]
        elif cur is not None:
            out.append(tuple(cur))
            cur = None
    if cur is not None:
        out.append(tuple(cur))
    return out


def motion_times(states):
    """[(start_s, end_s or inf)] from STATE changes."""
    out = []
    for ts, s in states:
        if s == 'motion':
            out.append([ts / 1e6, math.inf])
        elif out:
            out[-1][1] = ts / 1e6
    return [tuple(x) for x in out]


def check_motion_found(name, py, meta, allow=()):
    """Every motion segment detected quickly and cleared in time; no other
    motion except inside the (t0, t1) windows in `allow`. Motion that is over
    before the first floor exists can't be judged and is skipped."""
    got = motion_times(py['states'])
    first_floor = py['floors'][0][0] / 1e6 if py['floors'] else math.inf
    for a, b in spans(meta, 'motion'):
        if b < first_floor:
            continue
        hit = [m for m in got if m[0] <= a + DETECT_S and m[1] >= a]
        assert hit, f"{name}: motion at {a:.1f}-{b:.1f}s not detected within {DETECT_S}s: {got}"
        assert hit[0][1] <= b + HOLD_S, f"{name}: motion at {a:.1f}s held until {hit[0][1]:.1f}s"
    motion = spans(meta, 'motion')
    for m0, m1 in got:
        ok = any(a - 0.1 <= m0 <= b for a, b in motion) or \
            any(a <= m0 <= b for a, b in allow)
        assert ok, f"{name}: false motion at {m0:.1f}-{m1:.1f}s"


def scenario(name, steps, seed=3):
    meta = list(generate(seed=seed, scenario=steps))
    lines = [line for line, *_ in meta]
    py = run_python(lines)
    return meta, lines, py


def floor_at(py, t_s):
    return step_value(py['floors'], t_s * 1e6)


# ---------------------------------------------------------------- tests
def test_default():
    meta = list(generate(seed=1))
    lines = [line for line, *_ in meta]
    py = run_python(lines)
    n_junk = sum(1 for _l, k, *_ in meta if k == 'junk')
    print(f"default: parsed {py['n_csi']} CSI lines, rejected {n_junk} junk lines")
    for line, kind, _lab, _t in meta:                    # every board line type parses
        ev = parse_line(line)
        if kind == 'junk':
            assert ev is None, f"junk line was parsed: {line!r}"
        elif kind in ('learned', 'stats', 'state', 'calib', 'channel', 'csi', 'dev'):
            assert ev and ev[0] == kind, f"failed to parse {line!r}"

    # Separation of short scores on both streams, and beacon/ACK alike (time-based)
    still, motion = spans(meta, 'still'), spans(meta, 'motion')
    med = {}
    for k in (0, 1):
        for lab, sp in (('still', still), ('motion', motion)):
            v = [s for (ts, _src, kind), (s, _l) in py['scores'].items() if kind == k and
                 any(a + 1.5 <= ts / 1e6 <= b for a, b in sp)]
            assert v, f"no {lab} scores for kind {k}"
            med[(lab, k)] = float(np.median(v))
        print(f"  kind {k}: short score median still {med[('still', k)]:.4f}, "
              f"motion {med[('motion', k)]:.4f}")
        assert med[('motion', k)] > 3 * med[('still', k)], f"kind {k}: motion vs still"
    ratio = med[('motion', 1)] / med[('motion', 0)]
    assert 0.7 < ratio < 1.4, f"ACK and beacon motion scores differ too much ({ratio:.2f})"

    # Calibration: collect at boot, a floor after ~10 s of samples, recalibrate on the hop
    cal = py['calib']
    print("  calibration: " + ", ".join(f"{p}/{w} @ {ts / 1e6:.1f}s" for ts, p, w in cal))
    assert cal[0][1:] == ('collect', 'boot')
    first = next(ts for ts, p, _w in cal if p == 'fast') / 1e6
    assert 11 < first < 14, f"first floor at {first:.1f}s"
    assert any(w == 'channel' for _ts, _p, w in cal), "no recalibration after the hop"
    # No verdict without a floor; no scores during the post-hop settle
    assert not any(m and not has for _ts, m, has in py['verdict'])
    hop = py['hops'][0]
    assert not any(hop <= ts < hop + SETTLE_US for ts, *_ in py['scores'])
    det = py['det']
    assert det.streams and all(s.t_start >= hop + SETTLE_US for s in det.streams.values())

    check_motion_found('default', py, meta)
    print("  state changes: " + ", ".join(f"{s} @ {ts / 1e6:.2f}s" for ts, s in py['states']))
    check_c_agrees('default', py, lines)


def test_motion_while_calibrating():
    steps = [('motion', 8), ('still', 25), ('motion', 4), ('still', 12)]
    meta, lines, py = scenario('busy-cal', steps)
    _m, _l, clean = scenario('clean-cal', [('still', 33), ('motion', 4), ('still', 12)])
    f_busy, f_clean = floor_at(py, 32.5), floor_at(clean, 32.5)
    print(f"motion while calibrating: floor {f_busy:.4f} vs {f_clean:.4f} calibrated in a "
          f"still room ({f_busy / f_clean:.2f}x)")
    assert 0.8 < f_busy / f_clean < 1.3, "floor not recovered from a busy calibration"
    first = py['floors'][0][0] / 1e6
    got = motion_times(py['states'])
    print(f"  first floor at {first:.1f}s; motion verdicts {[(round(a, 1), round(b, 1)) for a, b in got]}")
    # Leftover from the movement may show right after the floor appears, briefly
    check_motion_found('busy-cal', py, meta, allow=[(first - 0.1, first + 0.1)])
    leftover = [m for m in got if m[0] < 30]
    assert all(b < first + 8 for _a, b in leftover), "leftover motion held too long"
    check_c_agrees('busy-cal', py, lines)


def test_board_moved():
    steps = [('still', 20), ('relocate', -8, 1.0), ('still', 30), ('motion', 4), ('still', 12)]
    meta, lines, py = scenario('moved', steps)
    rssi = [ts / 1e6 for ts, p, w in py['calib']               # recalibration starts
            if w == 'rssi' and p in ('wait', 'collect')]
    print(f"board moved at 20 s (RSSI -8 dB): recalibrated at {rssi}")
    assert len(rssi) == 1 and 20 < rssi[0] < 28, "RSSI step didn't trigger one recalibration"
    check_motion_found('moved', py, meta, allow=[(20, rssi[0] + 0.5)])
    check_c_agrees('moved', py, lines)


def test_router_glitch():
    """Brief router glitches (0.2 s and 1.2 s) must not count as motion; a real
    movement afterwards must still be found."""
    steps = [('still', 25), ('glitch', 0.2), ('still', 15), ('glitch', 1.2), ('still', 15),
             ('motion', 4), ('still', 12)]
    meta, lines, py = scenario('glitch', steps)
    got = motion_times(py['states'])
    ignored = sum(s.n_glitch for s in py['det'].streams.values())
    print(f"router glitches at 25 s and 40 s (motion from 55 s): motion {got}, {ignored} packets ignored")
    assert ignored > 20, "glitch packets weren't ignored"
    assert not [m for m in got if m[0] < 54.9], f"glitch flagged as motion: {got}"
    check_motion_found('glitch', py, meta)
    check_c_agrees('glitch', py, lines)


def test_router_scan():
    """A router's background scan (175 ms gaps, a ~0.3 shape change in every
    other burst, 3.5 s from 25 s) must not count as motion; real movement after
    it (from 45 s: scan steps take no scenario time) must."""
    steps = [('still', 25), ('scan', 3.5), ('still', 20), ('motion', 4), ('still', 12)]
    meta, lines, py = scenario('scan', steps)
    got = motion_times(py['states'])
    def ignored(t0_s, t1_s, cfg=None):       # glitches per stream between t0 and t1
        def ts(line):
            try:
                return int(line.split(',')[1])
            except (IndexError, ValueError):
                return 0
        def upto(t_s):
            r = run_python([l for l in lines if ts(l) < t_s * 1e6], cfg)
            return {k: s.n_glitch for k, s in r['det'].streams.items()}
        a, b = upto(t0_s), upto(t1_s)
        return {k: v - a.get(k, 0) for k, v in b.items()}
    in_scan = ignored(0, 30)
    print(f"router 0 scan at 25-28.5 s (motion from 45 s): motion {got}, "
          f"{sum(in_scan.values())} scan packets ignored")
    # 3 switched bursts x 0.325 s x 40 ACKs/s = 39 packets, all of them ignored
    assert in_scan.get((0, 1), 0) >= 39, "the scanning router's packets weren't ignored"
    assert not in_scan.get((1, 1), 0), "the other router's packets were ignored"
    # The scan rule must not filter any extra packets of the real movement
    # (the synthetic movement is strong enough that the normal limit drops a few)
    moving, moving_no_scan_rule = ignored(44.9, 70), ignored(44.9, 70, dict(scan_shape=0.4))
    assert moving == moving_no_scan_rule, f"scan rule filtered movement: {moving_no_scan_rule} -> {moving}"
    assert not [m for m in got if m[0] < 44.9], f"router scan flagged as motion: {got}"
    check_motion_found('scan', py, meta)
    check_c_agrees('scan', py, lines)


def test_noisier_room():
    """Board moved somewhere 2.5x noisier with no RSSI change, after tracking
    started: it may flag motion at first but must not get stuck there."""
    steps = [('still', 130), ('relocate', 0, 2.5), ('still', 150), ('motion', 4), ('still', 12)]
    meta, lines, py = scenario('noisier', steps)
    got = motion_times(py['states'])
    late = [m for m in got if m[0] >= 130 and m[0] < 280]
    end = max((m[1] for m in late), default=130)
    f0, f1 = floor_at(py, 129), floor_at(py, 279)
    print(f"noisier room at 130 s: floor {f0:.4f} -> {f1:.4f}; "
          f"false motion until {end:.0f}s, then still")
    assert any(p == 'track' for _ts, p, _w in py['calib']), "never reached tracking"
    # Reference: a fresh calibration on only the data after the move
    after = [line for line, kind, _lab, t in meta if kind == 'csi' and 135 <= t < 280]
    ref = run_python(after)['det'].floor
    print(f"  fresh calibration at the new spot: {ref:.4f} (tracked {f1 / ref:.2f}x of it)")
    assert end < 130 + 120, f"stuck in motion until {end:.0f}s"
    assert f1 > 1.3 * f0 and 0.8 < f1 / ref < 1.25, "floor didn't follow the new spot"
    check_motion_found('noisier', py, meta, allow=[(130, 250)])
    check_c_agrees('noisier', py, lines)


def test_real_captures():
    paths = {n: os.path.join(DATA, n + '.log.gz') for n in ('still', 'move')}
    if not all(os.path.exists(p) for p in paths.values()):
        print("real captures: test/data missing, skipped")
        return
    real = {}
    for n, p in paths.items():
        with gzip.open(p, 'rt', errors='replace') as f:
            real[n] = f.read().splitlines()
    py = run_python(real['still'])
    print(f"real still room (60 s): floor {py['det'].floor:.4f}, "
          f"state changes {py['states']}")
    assert py['states'] == [], "false motion in the still-room capture"
    check_c_agrees('real still', py, real['still'])

    py = run_python(real['move'])
    t0 = next(ts for ts, *_ in py['verdict'])
    got = [((a * 1e6 - t0) / 1e6, (b * 1e6 - t0) / 1e6) for a, b in motion_times(py['states'])]
    print("real movement (100 s): motion " +
          ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in got))
    # The person moved lightly at ~5-40 s and a lot at ~40-62 s, then stayed still
    assert any(a < 41 and b > 62 for a, b in got), "big movement not covered"
    assert all(b < 80 for _a, b in got), "motion held too long after the movement"
    check_c_agrees('real movement', py, real['move'])


def main():
    test_default()
    test_motion_while_calibrating()
    test_board_moved()
    test_router_glitch()
    test_router_scan()
    test_noisier_room()
    test_real_captures()
    print("ALL PYTHON TESTS PASSED")


if __name__ == '__main__':
    try:
        main()
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
