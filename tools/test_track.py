"""track.py: the Pi's tracker finds a person from link reactions; zones; the map keeps zones."""
import math
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
    print(f'tracking median error {np.median(errs):.2f} m')
    print('TRACK TESTS PASSED')


if __name__ == '__main__':
    main()
