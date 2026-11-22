"""breath_lite.py (the board's breathing twin): finds a breathing ripple, ignores noise."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from breath_lite import BreathLite, goertzel_power  # noqa: E402


def run(rate_per_min, depth, seconds=60, pps=50, seed=0):
    r = np.random.default_rng(seed)
    br, last = BreathLite(), None
    base = 20 + 6 * np.sin(np.linspace(0, 5, 64))
    for i in range(int(seconds * pps)):
        t = i / pps
        amp = base + r.normal(0, 0.25, 64)
        if depth:
            amp[1:27] *= 1 + depth * np.sin(2 * np.pi * rate_per_min / 60 * t)
            amp[38:64] *= 1 - 0.5 * depth * np.sin(2 * np.pi * rate_per_min / 60 * t)
        got = br.feed(t, amp)
        last = got or last
    return last


def main():
    x = np.sin(2 * np.pi * 0.2 * np.arange(400) / 10)
    assert goertzel_power(x, 0.2) > 100 * goertzel_power(x, 0.6), "Goertzel picks its own frequency"
    b = run(12, 0.01)
    assert b and b['level'] == 'clear' and abs(b['rate'] - 12) <= 1, b
    b = run(20, 0.01, seed=1)
    assert b and b['level'] != 'none' and abs(b['rate'] - 20) <= 1, b
    n = run(12, 0.0, seed=2)
    assert n and n['level'] == 'none', n
    assert run(12, 0.01, seconds=20) is None, "under 40 s: no reading yet"
    twin()
    print('BREATH LITE TESTS PASSED')


def twin():
    """The board's C detector (build-host/score_lines BREATH lines) gives the same readings."""
    import gzip
    import subprocess
    from analyze import parse_line
    here = os.path.dirname(os.path.abspath(__file__))
    scorer = os.path.join(here, '..', 'build-host', 'score_lines')
    if not os.path.exists(scorer):
        print('(no build-host/score_lines: C twin not compared)')
        return
    lines = gzip.open(os.path.join(here, '..', 'test', 'data', 'move.log.gz'), 'rt').read().splitlines()
    out = subprocess.run([scorer], input='\n'.join(lines) + '\n', text=True, capture_output=True, check=True).stdout
    c = [r.split() for r in out.splitlines() if r.startswith('BREATH')]
    br, py = BreathLite(), []
    for line in lines:
        ev = parse_line(line)
        if ev and ev[0] == 'csi' and ev[1]['src'] == 0 and ev[1]['kind'] == 1:
            r = br.feed(ev[1]['ts'] / 1e6, ev[1]['amp'])
            if r:
                py.append(r)
    lv = {'none': 0, 'possible': 1, 'clear': 2}
    assert len(c) == len(py) > 10, (len(c), len(py))
    for a, b in zip(c, py):
        assert int(a[2]) == lv[b['level']] and abs(float(a[3]) - b['rate']) < 0.01, (a, b)
        assert abs(float(a[4]) - b['prominence']) <= 0.02 * max(1.0, b['prominence']), (a, b)


if __name__ == '__main__':
    main()
