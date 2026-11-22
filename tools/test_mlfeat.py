"""mlfeat.py: gain-proof features; still vs moving told apart; gain steps ignored."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mlfeat import FEATURES, stream_features  # noqa: E402


def synth(moving, gain_step=False, n=600, rate=100.0, seed=0):
    r = np.random.default_rng(seed)
    t = np.arange(n) / rate
    base = 20 + 8 * np.sin(np.linspace(0, 6, 52))
    amp = np.tile(base, (n, 1)) + r.normal(0, 0.3, (n, 52))
    if moving:                      # a body: the shape sways a few times a second
        for k in range(52):
            amp[:, k] += 3 * np.sin(2 * np.pi * 1.5 * t + k / 4.0)
    if gain_step:                   # the radio's gain jumps: everything x1.6
        amp[n // 2:] *= 1.6
    rssi = -55 + r.normal(0, 0.5, n)
    return t, amp, rssi


def main():
    _, still = stream_features(*synth(False))
    _, move = stream_features(*synth(True))
    _, gain = stream_features(*synth(False, gain_step=True))
    assert still.shape[1] == len(FEATURES) and len(still) >= 1
    j = FEATURES.index('jump')
    assert move[:, j].mean() > still[:, j].mean() + 1.0, (move[:, j].mean(), still[:, j].mean())
    assert np.abs(gain[:, j].mean() - still[:, j].mean()) < 0.3, "a gain step is not motion"
    ac = FEATURES.index('sp_ac')
    assert move[:, ac].mean() > still[:, ac].mean(), "a moving body's spread changes smoothly"
    t, a, r = synth(False, n=10)
    assert len(stream_features(t, a, r)[0]) == 0, "too few packets: no windows"
    assert np.isfinite(move).all() and np.isfinite(still).all()
    # the live shadow: packets in, a verdict out, only for its own link
    from mlfeat import Shadow
    sh = Shadow()
    calls = []
    for still_or_move in (False, True):
        t, a, r = synth(still_or_move, n=800, seed=5)
        t = t + (0 if not still_or_move else 8.0)
        for i in range(len(t)):
            amp = np.zeros(64); amp[1:27] = a[i, :26]; amp[38:64] = a[i, 26:]
            got = sh.feed(0, dict(kind=1, src=0, amp=amp, rssi=r[i]), t[i])
            if got is not None:
                calls.append((round(t[i], 1), got))
        if not still_or_move:
            assert not sh.motion and sh.p is not None and sh.p < 0.5, sh.p
    assert sh.motion and calls and calls[-1][1] is True, calls
    assert sh.feed(1, dict(kind=1, src=1, amp=np.zeros(64), rssi=-50), 30.0) is None, "another link: ignored"
    print('MLFEAT TESTS PASSED')


if __name__ == '__main__':
    main()
