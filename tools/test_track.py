"""track.py: the Pi's tracker finds a person from link reactions; zones; the map keeps zones."""
import math
import tempfile
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track import End, Tracker, in_zone, sensitivity  # noqa: E402


def main():
    hub, a, b, r = End(0, 0), End(6, 0), End(0, 6), End(6, 6)
    pairs = {'h>a': (hub, a), 'h>b': (hub, b), 'a>b': (a, b), 'h>r': (hub, r), 'a>r': (a, r), 'b>r': (b, r)}
    ends = {'h>a': ('hub', 'a'), 'h>b': ('hub', 'b'), 'a>b': ('a', 'b'), 'h>r': ('hub', 'r'), 'a>r': ('a', 'r'), 'b>r': ('b', 'r')}
    rng = np.random.default_rng(3)

    def links(px, py, amp=1.6):
        out = []
        for k, (e1, e2) in pairs.items():
            s = float(sensitivity(np.array([px]), np.array([py]), e1, e2, 0.125)[0])
            out.append(dict(key=k, ends=[(e1, e2)], end_keys=ends[k], level=max(0.0, amp * s + rng.normal(0, 0.05))))
        return out
    tr = Tracker()
    t = 1000.0
    for _ in range(130):                          # a minute of quiet: each link learns its idle level
        tr.step([dict(l, level=0.05) for l in links(3, 3)], t)
        t += 0.5
    errs = []
    for i in range(16):                           # someone walks from (1.5, 1.5) to (4.5, 4.5): ~0.5 m/s
        px = py = 1.5 + 3 * i / 15
        tr.step(links(px, py), t)
        t += 0.5
        if tr.people and i > 4:
            p = tr.people[0].est
            errs.append(math.hypot(p['x'] - px, p['y'] - py))
    assert len(tr.people) >= 1 and errs, "found someone"
    assert np.median(errs) < 2.0, f"median error {np.median(errs):.2f} m"
    assert tr.people[0].speed > 0.3, "walking: a speed"
    for _ in range(10):                           # quiet again: the person goes
        tr.step([dict(l, level=0.05) for l in links(3, 3)], t)
        t += 0.5
    assert not tr.people, "nobody once it is quiet"
    assert tr.step([], t + 1) and not tr.people, "no links: nothing"
    t += 1.5
    # one link's spike is not a person (a new person counts once found twice)
    spike = [dict(l, level=0.05) for l in links(3, 3)]
    spike[0]['level'] = 1.5
    tr.step(spike, t)
    t += 0.5
    for _ in range(6):
        tr.step([dict(l, level=0.05) for l in links(3, 3)], t)
        t += 0.5
        assert not tr.people, "a one-step spike made a person"
    # someone walks in and stops: held where they stand while their links still react a little
    for i in range(8):
        tr.step(links(1.5 + 0.3 * i, 3.0), t)
        t += 0.5
    assert len(tr.people) == 1, "found the walker"
    for _ in range(20):                           # 10 s standing at (3.6, 3.0): small movements only
        tr.step(links(3.6, 3.0, amp=0.35), t)
        t += 0.5
    assert len(tr.people) == 1, "a person who stopped is still there"
    e = tr.people[0].est
    assert math.hypot(e['x'] - 3.6, e['y'] - 3.0) < 1.5, f"held near where they stopped: {e}"
    for k in range(12):                           # they leave: gone within a few seconds
        tr.step([dict(l, level=0.05) for l in links(3, 3)], t)
        t += 0.5
        if not tr.people:
            break
    assert not tr.people and k * 0.5 <= 4.0, f"still there {k * 0.5} s after leaving"
    # map.js runs the same tracker in the browser: its constants must match (seconds there are ms)
    import re
    import track as T
    js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'map.js')).read()
    for py, jn, scale in (('MULTI_GAIN', 'MULTI_GAIN', 1), ('MULTI_LEFT', 'MULTI_LEFT', 1), ('MULTI_SEP', 'MULTI_SEP', 1),
                          ('KEEP_S', 'KEEP_MS', 1000), ('WEAK', 'WEAK', 1), ('HOLD_S', 'HOLD_MS', 1000), ('HOLD_Z', 'HOLD_Z', 1),
                          ('HOLD_A', 'HOLD_A', 1), ('CONFIRM', 'CONFIRM', 1), ('CAND_S', 'CAND_MS', 1000), ('SPEED', 'SPEED', 1),
                          ('SPEED_STILL', 'SPEED_STILL', 1), ('JUMP_P', 'JUMP_P', 1), ('MEAN_R', 'MEAN_R', 1), ('MAX_PEOPLE', 'MAX_PEOPLE', 1)):
        m = re.search(r'\b' + jn + r' = ([0-9.]+)', js)
        assert m and abs(float(m.group(1)) - getattr(T, py) * scale) < 1e-9, f"map.js {jn} != track.py {py}"
    # a router's shared reaction (every board's link to it at once) never drags a person:
    # board 'a' sees someone right in front of it; the router end lights up too
    tr2 = Tracker()
    t2 = 5000.0
    def lk(level_ar, level_br, level_hr, level_ab):
        ra, rb, rh = End(6, 6), End(6, 6), End(6, 6)
        return [dict(key='a>r', ends=[(a, ra)], end_keys=('sat:a', 'r:x'), level=level_ar),
                dict(key='b>r', ends=[(b, rb)], end_keys=('sat:b', 'r:x'), level=level_br),
                dict(key='h>r', ends=[(hub, rh)], end_keys=('hub', 'r:x'), level=level_hr),
                dict(key='a>b', ends=[(a, b)], end_keys=('sat:a', 'sat:b'), level=level_ab),
                dict(key='h>a', ends=[(hub, a)], end_keys=('hub', 'sat:a'), level=0.9)]
    for _ in range(130):
        tr2.step(lk(0.05, 0.05, 0.05, 0.05), t2); t2 += 0.5
    for _ in range(12):
        tr2.step(lk(1.9, 1.0, 1.0, 0.9), t2); t2 += 0.5
    assert tr2.people, "someone near board a"
    pe = tr2.people[0].est
    assert math.hypot(pe['x'] - 6, pe['y'] - 6) > 2.5, f"not dragged to the router: {pe}"
    # a board left out of the map: none of its links place people (the Pi's links)
    from web import Dashboard as _D, State as _S
    tmp2 = tempfile.mkdtemp()
    dd = _D(_S(60), send=lambda c: None, sessions_dir=tmp2, settings=os.path.join(tmp2, 'dashboard.json'))
    assert dd.map_save({'nodes': [dict(key='r:x', name='r', x=5.0, y=0.0, placed=True)], 'track_off': ['hub']})[0]
    dd._layout_at = -9
    dd._radio = lambda: [dict(key='ap:0', src=0, node='r:x', rssi=-50, activity=2.0)]
    dd._map_boards = lambda: []
    links_now, _m = dd._track_links()
    assert links_now == [], links_now
    assert dd.map_save({'nodes': [dict(key='r:x', name='r', x=5.0, y=0.0, placed=True)]})[0]
    dd._layout_at = -9
    assert len(dd._track_links()[0]) == 1, "back in once ticked again"
    # a watched area: people only inside it, from the links with an end in it
    trw = Tracker()
    tw = 9000.0
    for _ in range(130):
        trw.step([dict(l, level=0.05) for l in links(3, 3)], tw, dict(x=4.5, y=4.5, w=3, h=3)); tw += 0.5
    for i in range(12):
        trw.step(links(5.5, 5.5), tw, dict(x=4.5, y=4.5, w=3, h=3)); tw += 0.5
    assert trw.people, "someone in the watched area"
    pw = trw.people[0].est
    assert 4.5 <= pw['x'] <= 7.5 and 4.5 <= pw['y'] <= 7.5, pw
    z = dict(x=1, y=1, w=2, h=2)
    assert in_zone(z, 2, 2) and not in_zone(z, 3.5, 2)
    # the map keeps zones (checked), and refuses bad ones
    from web import clean_map
    m = clean_map({'zones': [dict(id='abc1', name='Front door', x=1, y=2, w=3, h=2, notify=True)]})
    assert m['zones'][0]['name'] == 'Front door' and m['zones'][0]['notify'] and not m['zones'][0]['night']
    for bad in ([dict(id='A B', name='x', x=0, y=0, w=1, h=1)], [dict(id='z', name='x', x=0, y=0, w=0, h=1)], 'nope'):
        try:
            clean_map({'zones': bad})
            raise AssertionError(f'accepted {bad}')
        except ValueError:
            pass
    # the walk check: a tap is compared with the Pi's tracking and saved with the link levels
    import json
    from web import Dashboard, State
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(60), sessions_dir=tmp)
    d.tracker.people = tr.people or []
    from track import Person
    p = Person(1, t); p.est = dict(x=2.0, y=3.0, r=1.0); d.tracker.people = [p]
    ok, msg = d.command(dict(cmd='walk_mark', x=2.0, y=4.5))
    assert ok and '[pi 1.50]' in msg, msg
    rec = json.loads(open(os.path.join(tmp, 'walks.jsonl')).read().splitlines()[-1])
    assert rec['x'] == 2.0 and rec['est'] == [2.0, 3.0], rec
    assert not d.command(dict(cmd='walk_mark', x='a', y=1))[0]
    for i in range(8):                            # walks measured: the circle can't be smaller than they say
        d.command(dict(cmd='walk_mark', x=2.0, y=3.0 + 2.5))
    assert abs(d.tracker.model_err - 2.5) < 0.01 and d._people()['model_err'] == 2.5, d.tracker.model_err
    words = d._where_words()
    assert words.startswith('someone') and 'the hub' in words and 'within' in words, words
    # a zone near a router switches the light; board motion elsewhere doesn't
    import time as _t
    calls = []
    d = Dashboard(State(60), send=lambda c: None, sessions_dir=tmp, settings=os.path.join(tmp, 'dashboard.json'), light_call=lambda url: (calls.append(url) or (True, 'ok')),
                  light_run=lambda f: f())
    assert d.command(dict(cmd='light', enabled=True, on_url='http://plug.invalid/on', off_url='http://plug.invalid/off', off_after=0))[0]
    assert d.map_save({'zones': [dict(id='n1', name='Near router', near='r:aa:bb', light=True)]})[0]
    d._layout_at = -9
    d.tracker.near = dict(key='r:aa:bb', t=_t.time())
    d._track_step()
    d._light_rule()
    assert d.zone_in.get('n1') == 1 and d.light.on is True and calls[-1].endswith('/on'), (d.zone_in, d.light.on, calls)
    assert any('Motion at Near router' in e['text'] for e in d.events)
    d.tracker.near = dict(key='sat:' + ':'.join(['11', '22', '33', '44', '55', '66']), t=_t.time())   # motion at a board instead
    d.tracker.at = None
    d._track_step()
    d._light_rule()
    assert d.zone_in.get('n1') == 0 and d.light.on is False, (d.zone_in, d.light.on)
    print(f'tracking median error {np.median(errs):.2f} m')
    print('TRACK TESTS PASSED')


if __name__ == '__main__':
    main()
