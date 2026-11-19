#!/usr/bin/env python3
"""Activity recognition by example (activity.py): features, clips, matching,
"unknown", moments, storage, corrections, and the dashboard's commands.

Recognition is checked on the two real ESP32-S3 captures in test/data (a still
room, and a person moving): clips from one part of a capture must recognise
other parts of it."""
import gzip
import json
import os
import tempfile
from collections import Counter

import numpy as np

from activity import MAX_CLIPS, Activities, Features, label_info
from analyze import parse_line

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'test', 'data')


class Clock:
    """Feeds captures one after another on one continuous board clock."""

    def __init__(self):
        self.off = 0.0

    def capture(self, name):
        t0 = last = None
        with gzip.open(os.path.join(DATA, f'{name}.log.gz'), 'rt') as fh:
            for line in fh:
                g = parse_line(line)
                if not g or g[0] != 'csi':
                    continue
                d = dict(g[1])
                t0 = d['ts'] if t0 is None else t0
                last = (d['ts'] - t0) / 1e6
                t = self.off + last
                d['ts'] = int(t * 1e6) + 10 ** 9
                yield d, t, last
        self.off += last + 1.0


def run(a, gen, record=None, lo=0.0, hi=1e9):
    """Feed a capture; record=(label, at, secs) starts a clip at `at` s into it.
    Returns the verdicts made between lo and hi s into the capture."""
    out, started = [], False
    for d, t, rel in gen:
        if record and not started and rel >= record[1]:
            a.start(record[0], t, delay=0, secs=record[2])
            started = True
        r = a.feed(d, t)
        if r is not None and lo <= rel <= hi:
            out.append(r['state'])
    return Counter(out)


def synthetic(a, secs, t0, rng, motion=None, rate=100.0, ch=6, base=None):
    """Feed `secs` of synthetic router ACKs from t0: a fixed channel shape with
    measurement noise; motion(t) -> amplitude of a slow ripple on top."""
    base = base if base is not None else 20 + 8 * np.sin(np.arange(64) / 3.0)
    out = []
    n = int(secs * rate)
    for i in range(n):
        t = t0 + i / rate
        amp = base * (1 + 0.01 * rng.standard_normal(64))
        if motion:
            m = motion(t)
            amp = amp * (1 + m * np.sin(2 * np.pi * 1.5 * t + np.arange(64) / 5.0))
        d = dict(ts=int(t * 1e6), src=0, kind=1, ch=ch, rssi=-55, sig_mode=0, amp=np.abs(amp))
        r = a.feed(d, t)
        if r is not None:
            out.append(r)
    return out


def real_captures():
    """Clips from one part of each capture recognise the rest."""
    a, clk = Activities(None), Clock()
    run(a, clk.capture('still'), record=('still', 3, 12))
    run(a, clk.capture('move'), record=('walking', 46, 10))
    labels = Counter(c['label'] for c in a.clips)
    assert labels == {'still': 1, 'walking': 1}, labels
    a.recent.clear()
    got = run(a, clk.capture('still'), lo=20)
    assert got['still'] >= 0.9 * sum(got.values()), f"still room: {got}"
    a.recent.clear()
    got = run(a, clk.capture('move'), lo=57, hi=66)            # strong movement after the clip
    # one short clip: never wrong, but "unknown" where the movement looks unlike it
    assert got['still'] == 0 and got['walking'] >= 0.6 * sum(got.values()), f"moving, 1 clip: {got}"
    run(a, clk.capture('move'), record=('walking', 3, 12))     # a second clip, another part
    a.recent.clear()
    got = run(a, clk.capture('move'), lo=57, hi=66)
    assert got['walking'] >= 0.9 * sum(got.values()), f"moving, 2 clips: {got}"
    a.recent.clear()
    got = run(a, clk.capture('still'), lo=20)
    assert got['still'] >= 0.9 * sum(got.values()), f"still room, 2 walking clips: {got}"
    a.recent.clear()
    got = run(a, clk.capture('move'), lo=80)                   # the capture's quiet end
    assert got['walking'] == 0 and got['still'] >= 0.8 * sum(got.values()), f"quiet end: {got}"
    print("real captures: still and walking recognised from clips of other parts; a 2nd clip helps")


def unknown_and_weak():
    rng = np.random.default_rng(1)
    a = Activities(None)
    r = synthetic(a, 5, 0.0, rng)
    assert r[-1]['state'] == 'uncalibrated', "no clips yet"
    synthetic(a, 20, 5.0, rng)                    # settled: the 20 s window holds only this
    a.start('still', 25.0, delay=0, secs=10)
    synthetic(a, 10.5, 25.0, rng)
    synthetic(a, 20, 36.0, rng, motion=lambda t: 0.08)
    a.start('walking', 56.0, delay=0, secs=10)
    synthetic(a, 10.5, 56.0, rng, motion=lambda t: 0.08)
    assert Counter(c['label'] for c in a.clips) == {'still': 1, 'walking': 1}
    r = synthetic(a, 25, 67.0, rng)
    assert r[-1]['state'] == 'still', r[-1]
    r = synthetic(a, 25, 92.0, rng, motion=lambda t: 0.08)
    assert r[-1]['state'] == 'walking', r[-1]
    # something unlike either clip: a far stronger, faster movement
    r = synthetic(a, 25, 117.0, rng, motion=lambda t: 0.9 * np.sin(2 * np.pi * 9 * t))
    assert r[-1]['state'] == 'unknown' and r[-1]['why'] == 'nothing close', r[-1]
    # a weak link (few ACKs) gives no verdict at all
    r = synthetic(a, 6, 142.0, rng, rate=8.0)
    assert r[-1]['state'] == 'no-data' and r[-1]['why'] == 'weak link', r[-1]
    print("unknown when nothing is close; no verdict on a weak link")


def moments():
    """A moment (sitting down) is learned from a clip holding one burst and
    found again in a new burst, once; a still room raises none."""
    rng = np.random.default_rng(2)
    a = Activities(None)
    burst = lambda at: (lambda t: 0.12 * np.exp(-((t - at) / 0.35) ** 2))
    synthetic(a, 20, 0.0, rng)
    a.start('still', 20.0, delay=0, secs=12)
    synthetic(a, 12.5, 20.0, rng)
    a.start('sit_down', 33.0, delay=0, secs=10)
    synthetic(a, 10.5, 33.0, rng, motion=burst(39.0))
    clip = [c for c in a.clips if c['label'] == 'sit_down']
    assert clip and clip[0]['kind'] == 'event' and 1 <= len(clip[0]['wins']) <= 6, clip
    synthetic(a, 25, 44.0, rng)
    a.events.clear()
    synthetic(a, 20, 69.0, rng)
    assert not a.events, f"no moments in a still room: {list(a.events)}"
    synthetic(a, 25, 89.0, rng, motion=burst(95.0))
    assert [e['label'] for e in a.events] == ['sit_down'], list(a.events)
    print("moments: learned from one burst, found once in a new one")


def storage_and_corrections():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 'activities.json')
    rng = np.random.default_rng(3)
    a = Activities(path)
    a.start('still', 0.0, delay=0, secs=10)
    synthetic(a, 10.5, 0.0, rng)
    assert os.path.exists(path)
    b = Activities(path)
    assert len(b.clips) == 1 and b.clips[0]['label'] == 'still' and b.clips[0]['ch'] == 6
    # corrections: the last 6 s become a clip of the label you pick
    synthetic(b, 8, 20.0, rng, motion=lambda t: 0.08)
    c = b.correct('walking', 28.0)
    assert c and c['source'] == 'correction' and c['label'] == 'walking'
    assert Activities(path).labels().keys() == {'still', 'walking'}
    # at most MAX_CLIPS per activity: the oldest go
    w = b.clips[0]['wins']
    for i in range(MAX_CLIPS + 3):
        b.add_clip('still', [dict(w[0], ok=True, glitch=False, t=float(i))], 6, t=1000.0 + i)
    still = [x for x in b.clips if x['label'] == 'still']
    assert len(still) == MAX_CLIPS and min(x['t'] for x in still) == 1003.0
    assert b.delete(still[0]['id']) and not b.delete('nope')
    b.clear('still')
    assert {x['label'] for x in b.clips} == {'walking'}
    b.clear()
    assert not Activities(path).clips
    # a damaged file is the same as none
    with open(path, 'w') as f:
        f.write('{"clips": [{"label": 5}]}')
    assert Activities(path).clips == []
    with open(path, 'w') as f:
        f.write('not json')
    assert Activities(path).clips == []
    assert label_info('cooking')['kind'] == 'state' and label_info('sit_down')['kind'] == 'event'
    print("clips saved, capped, deleted; corrections become clips; damaged files ignored")


def features_basics():
    """Movement shows up in the energy and in the low bands; a channel change
    or a board restart starts the features over."""
    rng = np.random.default_rng(4)
    f = Features()
    quiet = moving = None
    for i in range(800):
        t = i / 100
        d = dict(ts=int(t * 1e6), src=0, kind=1, ch=6, rssi=-50,
                 amp=20 + 8 * np.sin(np.arange(64) / 3.0) * (1 + 0.01 * rng.standard_normal(64)))
        quiet = f.feed(d, t) or quiet
    for i in range(800, 1600):
        t = i / 100
        amp = (20 + 8 * np.sin(np.arange(64) / 3.0)) * (1 + 0.08 * np.sin(2 * np.pi * 1.5 * t + np.arange(64) / 5))
        moving = f.feed(dict(ts=int(t * 1e6), src=0, kind=1, ch=6, rssi=-50, amp=amp), t) or moving
    assert quiet['ok'] and moving['ok']
    assert moving['dyn'][0] > quiet['dyn'][0] + 1.0, (quiet['dyn'], moving['dyn'])
    assert moving['dyn'][5] > 0.5, "a 1.5 Hz ripple puts its power in the 1-3 Hz band"
    f.feed(dict(ts=16_100_000, src=0, kind=1, ch=11, rssi=-50, amp=np.ones(64) * 20), 16.1)
    assert f.ch == 11 and len(f.buf) == 1, "a new channel starts over"
    f.feed(dict(ts=5_000, src=0, kind=1, ch=11, rssi=-50, amp=np.ones(64) * 20), 0.005)
    assert len(f.buf) == 1 and f.next_t is not None and f.next_t < 4, "a restarted board starts over"
    print("features: movement raises the energy; channel change and restart reset")


def dashboard():
    """The Activity page's commands and payloads, on the demo stream."""
    from fake_csi import generate
    from gui import State
    from web import Dashboard
    tmp = tempfile.mkdtemp()
    s = State(600)
    dash = Dashboard(s, send=lambda _c: None, settings=os.path.join(tmp, 'dashboard.json'))
    ok, msg = dash.command({'cmd': 'act_record', 'label': 'still'})
    assert not ok and 'no data' in msg
    lines = [line for line, *_ in generate(seed=5)]
    for line in lines[:3000]:
        dash.feed(line)
    f = dash.fast(30)['activity']
    assert f['now']['state'] == 'uncalibrated' and f['recording'] is None
    assert not dash.command({'cmd': 'act_record', 'label': 'Bad Name!'})[0]
    assert not dash.command({'cmd': 'act_record', 'label': 'still', 'secs': 999})[0]
    ok, msg = dash.command({'cmd': 'act_record', 'label': 'still', 'delay': 0, 'secs': 8})
    assert ok, msg
    assert not dash.command({'cmd': 'act_record', 'label': 'walking'})[0], "one recording at a time"
    assert dash.fast(30)['activity']['recording']['label'] == 'still'
    i = 3000
    while dash.act.rec is not None and i < len(lines):
        dash.feed(lines[i])
        i += 1
    f = dash.fast(30)['activity']
    assert f['recording'] is None and f['saved']['label'] == 'still', f
    sl = dash.slow(30)['act']
    row = next(x for x in sl['labels'] if x['key'] == 'still')
    assert row['clips'] == (1 if f['saved']['ok'] else 0)
    assert any(x['key'] == 'sit_down' and x['kind'] == 'event' for x in sl['labels'])
    json.dumps(dash.fast(30)), json.dumps(dash.slow(30))
    assert os.path.exists(os.path.join(tmp, 'activities.json')) == bool(f['saved']['ok'])
    ok, _ = dash.command({'cmd': 'act_record', 'label': 'cooking', 'delay': 5})
    assert ok and dash.command({'cmd': 'act_cancel'})[0] and dash.act.rec is None
    if sl['clips']:
        assert dash.command({'cmd': 'act_delete', 'id': sl['clips'][0]['id']})[0]
    assert dash.command({'cmd': 'act_clear'})[0] and not dash.act.clips
    assert dash.command({'cmd': 'act_nope'}) == (False, 'unknown command')
    print("dashboard: record, cancel, delete, clear; payloads are JSON")


def posture_and_movement():
    """Two answers at once: movement (how the signal moves) from every clip,
    posture (where a still body is) only from clips recorded at this placement
    and only once the clips show it can be trusted. After 'I moved the board',
    movement keeps working and posture says it doesn't know."""
    rng = np.random.default_rng(7)
    a = Activities(None)
    a.place = 'R|6|0'
    sit = 20 + 8 * np.sin(np.arange(64) / 3.0)                 # two body positions: two channel shapes
    lie = 20 + 8 * np.sin(np.arange(64) / 3.0 + 1.3)
    t = 0.0
    for label, base, motion in (('sitting', sit, None), ('lying', lie, None), ('sitting', sit, None),
                                ('lying', lie, None), ('walking', sit, lambda x: 0.08), ('walking', sit, lambda x: 0.08)):
        synthetic(a, 22, t, rng, base=base, motion=motion)     # the 20 s window settles first
        a.start(label, t + 22, delay=0, secs=12)
        synthetic(a, 12.5, t + 22, rng, base=base, motion=motion)
        t += 40
    assert [c['label'] for c in a.clips] == ['sitting', 'lying', 'sitting', 'lying', 'walking', 'walking']
    assert all(c['place'] == 'R|6|0' for c in a.clips), "clips remember where they were recorded"
    a._scale = None
    a._prep()
    assert a.posture_ok == {'sitting', 'lying'}, a.posture_check

    def verdicts(base, motion=None, secs=25):
        nonlocal t
        out = synthetic(a, secs, t, rng, base=base, motion=motion)
        t += secs + 1
        return out[-10:]
    v = verdicts(lie)
    assert all(r.get('posture') == 'lying' and r.get('movement') == 'still' for r in v[-5:]), v[-5:]
    assert v[-1]['state'] == 'lying' and v[-1]['name'] == 'Lying · still'
    v = verdicts(sit)
    assert v[-1]['posture'] == 'sitting' and v[-1]['name'] == 'Sitting · still', v[-1]
    v = verdicts(sit, motion=lambda x: 0.08)
    assert v[-1]['movement'] == 'walking' and v[-1]['state'] == 'walking', v[-1]
    # the board moved: a new placement; posture has no clips here, movement still works
    a.place = 'R|6|1'
    other = 20 + 8 * np.sin(np.arange(64) / 2.0)
    v = verdicts(other)
    assert v[-1]['movement'] == 'still' and v[-1]['posture'] is None and not v[-1]['posture_here'], v[-1]
    assert v[-1]['name'] == 'Still'
    v = verdicts(other, motion=lambda x: 0.08)
    assert v[-1]['movement'] == 'walking', v[-1]
    print("posture and movement: both learned; after a move, movement still works and posture waits")


def main():
    features_basics()
    posture_and_movement()
    real_captures()
    unknown_and_weak()
    moments()
    storage_and_corrections()
    dashboard()
    print("ACTIVITY TESTS PASSED")


if __name__ == '__main__':
    main()
