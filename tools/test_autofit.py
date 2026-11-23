"""autofit.py: a router in the watched area goes where signal strength and
everyday movement say it is; routers you placed exactly, or outside the area, stay."""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofit  # noqa: E402
from track import End, sensitivity  # noqa: E402


def main():
    rng = np.random.default_rng(4)
    # boards on several sides of the watched house: the evidence points one way
    boards = {'sat:a': (-3.0, 0.0), 'sat:b': (3.0, 0.0), 'sat:c': (9.0, 1.0), 'sat:d': (14.0, -13.0), 'sat:e': (-9.0, -12.0)}
    true_r = (2.0, -12.0)
    layout = dict(zones=[dict(id='h', name='House', x=-4, y=-16, w=12, h=8, watch=True)],
                  nodes=[dict(key=k, name=k, x=x, y=y, placed=True) for k, (x, y) in boards.items()]
                  + [dict(key='r:x', name='R', x=4.5, y=-10.5, placed=False, hint=[4.5, -10.5, 3.0]),
                     dict(key='r:far', name='F', x=-20, y=0, placed=False, hint=[-20, 0, 3.0])])
    # signal strength from the true spot: -40 dBm at 1 m, n = 3
    heard = {'r:x': [[k, -40 - 30 * math.log10(math.dist(p, true_r)) + rng.normal(0, 1.5)] for k, p in boards.items()]}
    # moments of movement: someone at random spots in the house, each link reacting by its sensitivity
    snaps = []
    for _ in range(80):
        px, py = rng.uniform(-3, 7), rng.uniform(-15, -9)
        lv = {}
        for k, p in boards.items():
            s = float(sensitivity(np.array([px]), np.array([py]), End(*p), End(*true_r), 0.125)[0])
            lv[f'{k}>r:x'] = max(0.0, 1.8 * s + rng.normal(0, 0.05))
        snaps.append(dict(t=0, links=lv))

    def ends_of(key, over):
        if key in over:
            return [End(*over[key])]
        if key in boards:
            return [End(*boards[key])]
        n = next((n for n in layout['nodes'] if n['key'] == key), None)
        return [End(n['hint'][0], n['hint'][1])] if n else None
    moved, note = autofit.fit(layout, snaps, ends_of, hearings=heard)
    assert 'r:x' in moved and 'r:far' not in moved, (moved, note)
    err_before = math.dist((4.5, -10.5), true_r)
    err_after = math.dist(moved['r:x'], true_r)
    print(f"router: {err_before:.1f} m off -> {err_after:.1f} m ({note})")
    assert err_after < 0.8 * err_before, (err_before, err_after)
    for n in layout['nodes']:
        if n['key'] == 'r:x':
            n['placed'] = True                           # "exactly here": never moved
    assert autofit.fit(layout, snaps, ends_of, hearings=heard)[0] == {}
    assert autofit.fit(dict(layout, zones=[]), snaps, ends_of)[1] == 'no watched area'
    # boards all on one side, far away: the spots can't be told apart, so nothing moves
    side = {k: boards[k] for k in ('sat:a', 'sat:b', 'sat:c')}
    for n in layout['nodes']:
        if n['key'] == 'r:x':
            n['placed'] = False
    layout['nodes'] = [n for n in layout['nodes'] if not n['key'].startswith('sat:') or n['key'] in side]
    heard1 = {'r:x': [h for h in heard['r:x'] if h[0] in side]}
    snaps1 = [dict(t=0, links={k: v for k, v in s_['links'].items() if k.split('>')[0] in side}) for s_ in snaps]
    moved1, note1 = autofit.fit(layout, snaps1, ends_of, hearings=heard1)
    assert 'r:x' not in moved1 or math.dist(moved1['r:x'], true_r) <= err_before + 0.5, (moved1, note1)
    print('unclear layout:', note1)
    print('AUTOFIT TESTS PASSED')


if __name__ == '__main__':
    main()
