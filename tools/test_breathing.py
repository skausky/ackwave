#!/usr/bin/env python3
"""Breathing estimator: a 15/min ripple is found, noise and slow drift aren't,
and the reading is ready after the window fills."""
import numpy as np

from analyze import VALID
from breathing import WIN_S, Breathing, text


def run(amp_at, seconds, rate=100.0, seed=0):
    rng = np.random.default_rng(seed)
    br, out = Breathing(), []
    for i in range(int(seconds * rate)):
        t = i / rate
        amp = np.ones(64)
        amp[VALID] = amp_at(t) + 0.01 * rng.standard_normal(len(VALID))
        r = br.feed(dict(src=0, kind=1, ch=6, amp=amp), t)
        if r:
            out.append((t, r))
    return br, out


def main():
    rng = np.random.default_rng(3)
    shape = 1 + 0.3 * rng.random(len(VALID))
    chest = rng.standard_normal(len(VALID))
    br, out = run(lambda t: shape + 0.01 * chest * np.sin(2 * np.pi * 0.25 * t), 90)
    assert out[0][0] >= WIN_S - 5, "reading before the window filled"
    last = out[-1][1]
    assert last['level'] == 'clear' and abs(last['rate'] - 15) <= 1.5, last
    assert text(last).startswith('breathing ~15/min')
    _, quiet = run(lambda t: shape, 90, seed=1)
    assert all(r['level'] == 'none' for _t, r in quiet), [r for _t, r in quiet if r['level'] != 'none']
    # slow drift (about 3 /min) must not read as breathing at the bottom of the band
    _, drift = run(lambda t: shape + 0.03 * chest * np.sin(2 * np.pi * 0.05 * t), 90, seed=2)
    assert all(r['level'] == 'none' for _t, r in drift), [r for _t, r in drift if r['level'] != 'none']
    assert text(None) == 'breathing: collecting'
    print(f"BREATHING: 15/min ripple read as {last['rate']:.1f}/min ({last['prominence']:.0f}x, "
          f"{last['agreement']:.0%}); noise and slow drift: none")
    print("BREATHING TESTS PASSED")


if __name__ == '__main__':
    main()
