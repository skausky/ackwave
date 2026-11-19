"""Tests for timeline.py (where and when motion happened, on disk) and the
dashboard's /api/timeline range."""
import json
import os
import tempfile
import time
from datetime import datetime, timedelta

from fake_csi import generate
from gui import State
from timeline import Timeline
from web import Dashboard


def store():
    tmp = tempfile.mkdtemp()
    tl = Timeline(tmp)
    now = time.time()
    assert tl.add(now - 100, now - 90, 'board', 'Board')
    assert tl.add(now - 95, now - 91, 'ap:1', 'Router 2')
    assert not tl.add(now - 10, now - 20, 'board', 'Board'), "ends before it starts"
    assert not tl.add(now - 10, now, 'ap:9', 'x'), "no such router"
    assert not tl.add(now - 10, now, '../etc', 'x'), "keys are checked"
    assert not tl.add('soon', now, 'board', 'x')
    got = tl.read(now - 3600, now)
    assert [p[2] for p in got] == ['board', 'ap:1'] and got[0][1] - got[0][0] == 10, got
    assert tl.read(now - 50, now) == [], "periods outside the range are left out"
    # a crash can tear the last line: it is skipped, the rest still reads
    with open(tl.path(datetime.fromtimestamp(now - 100).strftime('%Y-%m-%d')), 'a') as f:
        f.write('{"t0": 1, "t1"')
    assert len(tl.read(now - 3600, now)) == 2
    # a period that started yesterday shows in a range that starts today
    midnight = datetime.combine(datetime.now().date(), datetime.min.time()).timestamp()
    assert tl.add(midnight - 30, midnight + 30, 'board', 'Board')
    assert any(p[0] == round(midnight - 30, 2) for p in tl.read(midnight + 1, midnight + 60))
    # a stuck verdict is cut at 24 h; names are cut at 40 characters
    assert tl.add(now - 5 * 86400, now, 'board', 'x' * 99)
    p = [p for p in tl.read(now - 6 * 86400, now) if p[0] == round(now - 5 * 86400, 2)][0]
    assert p[1] - p[0] <= 86400 and len(p[3]) == 40
    assert tl.days()[0] == datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    assert yesterday in tl.days()


def dashboard():
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(600), sessions_dir=tmp, settings=os.path.join(tmp, 'dashboard.json'))
    assert d.command({'cmd': 'settings', 'per_link': True})[0]
    for line, *_ in generate(seed=3):
        d.feed(line)
    r = d.timeline_range(24)
    keys = {s[2] for s in r['spans']}
    assert {'board', 'ap:0'} <= keys, keys
    assert r['lanes'][0] == {'key': 'board', 'name': 'Board'} and r['per_link']
    assert [l['name'] for l in r['lanes'] if l['key'] == 'ap:0'] == ['Router 1']
    assert all(s[0] <= s[1] for s in r['spans'])
    with open(os.path.join(tmp, f"timeline-{datetime.now().strftime('%Y-%m-%d')}.jsonl")) as f:
        lines = [json.loads(x) for x in f]
    assert lines and all(set(x) == {'t0', 't1', 'key', 'name'} for x in lines)
    # silly ranges are clamped, never an error
    assert d.timeline_range(float('nan'))['hours'] == 24 and d.timeline_range(1e9)['hours'] == 168
    assert d.timeline_range(-5)['hours'] == 30 / 3600 and d.timeline_range(0.5)['hours'] == 0.5
    # no sessions folder (file input): nothing on disk, still answers
    d2 = Dashboard(State(60))
    assert d2.timeline_range(1)['spans'] == []


def activity_periods():
    """The activity verdict flickers (walking, a second of unknown, walking):
    the history keeps whole periods, and only real activities are filed."""
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(60), sessions_dir=tmp, settings=os.path.join(tmp, 'dashboard.json'))
    w = time.time() - 600
    seq = (['walking'] * 20 + ['unknown'] * 3 + ['walking'] * 20 + ['sitting'] * 30
           + ['unknown'] * 12 + ['still'] * 10)                     # one verdict every 0.5 s
    for i, st in enumerate(seq):
        d._act_period(st, w + i * 0.5)
    log = [(st, round(b - a, 1)) for a, b, st in d.act_log]
    assert log == [('walking', 21.5), ('sitting', 15.0), ('unknown', 6.0)], log
    assert d.act_cur[1] == 'still'
    got = [(p[2], p[3]) for p in d.timeline.read(w - 1, w + 100)]
    assert got == [('activity', 'walking'), ('activity', 'sitting')], got   # no 'unknown' on disk
    r = d.timeline_range(1)
    assert [l['key'] for l in r['lanes']][:2] == ['board', 'activity']
    assert {s[4] for s in r['spans'] if s[2] == 'activity'} >= {'Walking', 'Sitting', 'Still'}
    # "I moved the board or router": a new placement, kept in the settings
    ok, msg = d.command({'cmd': 'act_moved'})
    assert ok and d.cfg['act_place'] == 1 and 'posture' in msg
    with open(os.path.join(tmp, 'dashboard.json')) as f:
        assert json.load(f)['act_place'] == 1
    d2 = Dashboard(State(60), sessions_dir=tmp, settings=os.path.join(tmp, 'dashboard.json'))
    assert d2.cfg['act_place'] == 1, "remembered"
    sl = d2.slow(30)['act']
    assert {'place', 'posture_check', 'posture_ok'} <= set(sl)


if __name__ == '__main__':
    store()
    activity_periods()
    dashboard()
    print("TIMELINE TESTS PASSED")
