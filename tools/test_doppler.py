#!/usr/bin/env python3
"""Doppler analyzer: a ripple shows up at its frequency, a step is "step-like",
quiet stays quiet, and the fake data's moving reflector is found."""
import numpy as np

from analyze import VALID, parse_line
from doppler import FREQS, Doppler, wavelength
from fake_csi import generate


def feed(dop, amp_at, seconds, rate=100.0, t0=0.0):
    out = []
    for i in range(int(seconds * rate)):
        t = t0 + i / rate
        amp = np.ones(64)
        amp[VALID] = amp_at(t)
        r = dop.feed(dict(src=0, kind=1, ch=6, amp=amp), t)
        if r:
            out.append((t, r))
    return out


def main():
    rng = np.random.default_rng(1)
    shape = 1 + 0.3 * rng.random(len(VALID))
    pattern = rng.standard_normal(len(VALID))
    noise = lambda: 0.01 * rng.standard_normal(len(VALID))   # noqa: E731

    dop = Doppler()
    quiet = feed(dop, lambda t: shape + noise(), 30)
    assert quiet and all(r['f'] == 0 for _t, r in quiet[-20:]), "quiet room shows a frequency"
    assert not any(r['moving'] for _t, r in quiet), "quiet room counted as moving"

    ripple = feed(dop, lambda t: shape + 0.05 * pattern * np.sin(2 * np.pi * 10 * t) + noise(),
                  4, t0=30)
    peak = FREQS[np.argmax(dop.rows[-1][1][1:]) + 1]
    assert 9 <= peak <= 11, f"10 Hz ripple peaks at {peak} Hz"
    f = np.median([r['f'] for _t, r in ripple[-8:]])
    # a 10 Hz ripple has no energy under 3 Hz: a clean tone, not a moving body
    slow = feed(dop, lambda t: shape + 0.05 * pattern * np.sin(2 * np.pi * 2 * t) + noise(), 4, t0=34)
    assert any(r['moving'] for _t, r in slow), "a 2 Hz ripple isn't movement-like"
    assert 8 <= f <= 12, f"10 Hz ripple read as {f:.1f} Hz"
    assert abs(ripple[-1][1]['speed'] - 10 * wavelength(6) / 2) < 0.15

    feed(dop, lambda t: shape + noise(), 10, t0=38)              # quiet again
    step = feed(dop, lambda t: shape + (0.3 * pattern if t >= 49.5 else 0) + noise(), 2, t0=48.5)
    lows = [r['low'] for t, r in step if 49.4 < t < 50.3]
    assert lows and max(lows) > 0.75, f"a step isn't low-frequency: {lows}"

    # Clock going back (board reboot / demo loop): starts over cleanly
    feed(dop, lambda t: shape + noise(), 2, t0=0)
    assert all(t < 3 for t, _p, _f in dop.rows)

    # Fake data: the moving reflector ripples at 2.0-2.7 Hz
    dop, peaks = Doppler(), []
    for line, kind, label, t in generate(seed=2):
        if kind != 'csi':
            continue
        d = parse_line(line)[1]
        if dop.feed(d, d['ts'] / 1e6) and label == 'motion':
            peaks.append(FREQS[np.argmax(dop.rows[-1][1][1:]) + 1])
    assert peaks and 1.5 <= np.median(peaks) <= 3.5, f"fake movement peaks at {np.median(peaks)} Hz"
    print(f"DOPPLER: 10 Hz ripple read as {f:.1f} Hz, step low-share {max(lows):.0%}, "
          f"fake movement peak {np.median(peaks):.1f} Hz")
    print("DOPPLER TESTS PASSED")


if __name__ == '__main__':
    main()
