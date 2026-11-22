#!/usr/bin/env python3
"""Breathing on a board: a lighter twin of breathing.py, made to run on an
ESP32 (and mirrored in C in main/csi_core.h, br_*; test_pipeline compares).

Instead of PCA over every subcarrier, the 52 used subcarriers are averaged
into 8 groups (each packet's shape over its mean: gain gone), binned at
10 Hz, and every 2 s the last 40 s are detrended (mean, slope, curvature) and
tested at the breathing rates only, 8-36 a minute in half steps (Goertzel),
summed over the 3 groups where it stands out most (on 65 still clips: twice
as often found as summing all 8, and no 'clear' in an empty room). Same verdict rules as breathing.py: a peak's
prominence over the 0.8-3 Hz background, and how many groups agree.

    python breath_lite.py sessions/raw-....log.gz [hh:mm:ss hh:mm:ss]
"""
import math
import sys

import numpy as np

FS = 10.0
WIN_S = 40.0
N = int(WIN_S * FS)               # 400 bins
GROUPS = 8
USED = np.r_[1:27, 38:64]
RATES = np.arange(8.0, 36.01, 0.5) / 60          # Hz
REFS = np.linspace(0.8, 3.0, 12)
CLEAR = (50.0, 0.30)
POSSIBLE = 30.0
TOP = 3                           # groups summed: the ones the breathing shows in most


def _basis(n):
    """Orthonormal mean / slope / curvature over n points (Gram-Schmidt)."""
    s = np.linspace(-1, 1, n)
    B = []
    for v in (np.ones(n), s, s * s):
        for b in B:
            v = v - (v @ b) * b
        B.append(v / math.sqrt(v @ v))
    return np.array(B)


BASIS = _basis(N)
HANN = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / (N - 1))


def goertzel_power(x, f, fs=FS):
    """|X(f)|^2 of x (one frequency), as the board computes it."""
    w = 2 * math.cos(2 * math.pi * f / fs)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + w * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - w * s1 * s2


def estimate(G):
    """G: (N, GROUPS) binned group values, oldest first. A reading dict."""
    U = G - (G.T @ BASIS.T @ BASIS).T                  # detrended
    U = U * HANN[:, None]
    P = np.array([[goertzel_power(U[:, g], f) for g in range(GROUPS)] for f in RATES])   # (rates, groups)
    R = np.array([[goertzel_power(U[:, g], f) for g in range(GROUPS)] for f in REFS])
    # the 3 groups where the breathing stands out most (a breath ripples some
    # subcarriers far more than others; summing all 8 dilutes it)
    pg = P.max(0) / np.maximum(np.median(R, 0), 1e-18)
    top = np.sort(np.argsort(-pg, kind='stable')[:TOP])
    tot, ref = P[:, top].sum(1), R[:, top].sum(1)
    i = int(np.argmax(tot))
    if i == 0 or i == len(RATES) - 1 or not (tot[i] > tot[i - 1] and tot[i] > tot[i + 1]):
        return dict(rate=0.0, prominence=0.0, agreement=0.0, level='none')
    prom = float(tot[i] / max(np.median(ref), 1e-18))
    agree = float(np.mean(np.abs(RATES[np.argmax(P, axis=0)] - RATES[i]) <= 1.5 / 60))
    level = 'clear' if prom >= CLEAR[0] and agree >= CLEAR[1] else 'possible' if prom >= POSSIBLE else 'none'
    return dict(rate=RATES[i] * 60, prominence=prom, agreement=agree, level=level)


class BreathLite:
    """Feed one link's packets (t in s, 64 amplitudes); a reading every 2 s."""

    def __init__(self):
        self.bins = []                  # [(bin index, group values)]
        self.cur, self.acc, self.cnt = None, np.zeros(GROUPS), 0
        self.next_t = None
        self.last = None

    def feed(self, t, amp):
        a = amp[USED].astype(float)
        m = a.mean()
        if m <= 0:
            return None
        g = (a / m).reshape(GROUPS, -1).mean(1) if len(a) % GROUPS == 0 else \
            np.array([c.mean() for c in np.array_split(a / m, GROUPS)])
        b = int(t * FS)
        if self.cur is not None and b < self.cur - 50:   # clock went back: start over
            self.bins, self.cur, self.next_t = [], None, None
        if self.cur is None or b != self.cur:
            if self.cur is not None and self.cnt:
                self.bins.append((self.cur, self.acc / self.cnt))
            self.cur, self.acc, self.cnt = b, np.zeros(GROUPS), 0
        self.acc += g
        self.cnt += 1
        if self.next_t is None:
            self.next_t = t + 2.0
        if t < self.next_t:
            return None
        self.next_t = t + 2.0
        last = self.cur - 1
        have = {k: v for k, v in self.bins if k > last - N}
        self.bins = [(k, v) for k, v in self.bins if k > last - N]
        if len(have) < 0.7 * N:
            return None
        G = np.empty((N, GROUPS))
        prev = next(iter(have.values()))
        for j, k in enumerate(range(last - N + 1, last + 1)):   # missing bins: the last value held
            prev = have.get(k, prev)
            G[j] = prev
        self.last = estimate(G)
        return self.last


def main():
    import gzip
    import time as _t
    from analyze import MAX_APS, parse_line
    path = sys.argv[1]
    t0 = t1 = None
    if len(sys.argv) >= 4:
        t0, t1 = sys.argv[2], sys.argv[3]
    br, wall_off, pend = BreathLite(), None, None
    with gzip.open(path, 'rt', errors='replace') as f:
        for line in f:
            if line.startswith('#T,'):
                pend = float(line.split(',')[1])
                continue
            ev = parse_line(line) if line.startswith('CSI,') else None
            if not ev or ev[0] != 'csi':
                continue
            d = ev[1]
            if pend is not None:
                wall_off, pend = pend - d['ts'] / 1e6, None
            if d['kind'] != 1 or d['src'] >= MAX_APS or d['src'] != 0 or wall_off is None:
                continue
            w = d['ts'] / 1e6 + wall_off
            clock = _t.strftime('%H:%M:%S', _t.localtime(w))
            if t1 and clock > t1:
                break
            r = br.feed(d['ts'] / 1e6, d['amp'])
            if r and (not t0 or clock >= t0):
                print(f"{clock} {r['level']:8s} {r['rate']:5.1f}/min prom {r['prominence']:7.1f} agree {r['agreement']:.2f}")


if __name__ == '__main__':
    main()
