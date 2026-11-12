#!/usr/bin/env python3
"""Breathing rate from the router's ACKs (experimental, PC side only).

A breathing chest moves a few mm, 8-36 times a minute. Close to the link
(the person in or near the path between board and router) that ripples
the CSI amplitude at the breathing rate. Every 2 s this takes the last
40 s of the router's ACK amplitudes at 10 Hz, removes each subcarrier's
mean and trend, and looks at the 3 strongest patterns common to all
subcarriers (PCA): the strongest spectral peak at 8-36 /min, how far it
stands above the 0.8-3 Hz background ("prominence"), and how many single
subcarriers peak at the same rate ("agreement").

First real test (standing still about 1 m from the board, between it and
the router, through a wall and a full wardrobe, counted 10 /min): 11 /min
with a clear peak for the whole minute, while sitting normally at the desk
and the empty room showed none. Off the path (asleep a few feet behind the
board) nothing was found. Thresholds below come from that one test.

    python breathing.py sessions/raw-2026-11-10_1013.log.gz 10:46:30 10:47:40
prints the reading every 2 s for part of a recorded session.
"""
import sys
from collections import deque

import numpy as np

from analyze import MAX_APS, VALID

FS = 10.0                         # resampling rate (Hz)
WIN_S = 40.0                      # shorter = faster to react, coarser rate steps
EVERY_S = 2.0
BAND = (8 / 60, 36 / 60)          # breaths per minute -> Hz
REF = (0.8, 3.0)                  # background band for the prominence
CLEAR = (50.0, 0.30)              # prominence, agreement for "clear"
POSSIBLE = 30.0                   # prominence for "possible"


class Breathing:
    """Feed it every CSI packet; it estimates breathing on the router's ACKs."""

    def __init__(self, src=None):
        self.src = src                           # None: the APs' ACKs; else only node src
        self.buf = {}                            # (src, 1) -> deque[(t, x)]
        self.next_t = None
        self.last = None                         # latest reading (dict) or None

    def feed(self, d, t):
        """d = parsed CSI dict, t = its time in seconds. Returns a new reading or None."""
        if d['kind'] != 1 or (d['src'] >= MAX_APS if self.src is None else d['src'] != self.src):
            return None
        a = d['amp'][VALID]
        m = a.mean()
        if m <= 0:
            return None
        q = self.buf.setdefault((d['src'], 1), deque())
        if q and t < q[-1][0] - 5:               # clock went back: start over
            self.buf.clear()
            q = self.buf.setdefault((d['src'], 1), deque())
            self.next_t = None
        if not q or t - q[-1][0] >= 1 / FS / 2:  # 20 Hz is plenty for 10 Hz analysis
            q.append((t, a / m))
        while q and q[0][0] < t - WIN_S:
            q.popleft()
        if self.next_t is None:
            self.next_t = t + EVERY_S
        if t < self.next_t:
            return None
        self.next_t = t + EVERY_S
        q = max(self.buf.values(), key=len)
        self.last = estimate(q, t)
        return self.last


def estimate(q, t_end):
    """Breathing reading over the last WIN_S, or None if there is too little data."""
    t = np.fromiter((p[0] for p in q), float)
    if len(t) < 300 or t[-1] - t[0] < WIN_S - 5:
        return None
    X = np.array([p[1] for p in q])
    n = int(WIN_S * FS)
    g = t_end - WIN_S + np.arange(n) / FS
    U = np.empty((n, X.shape[1]))
    for j in range(X.shape[1]):
        U[:, j] = np.interp(g, t, X[:, j])
    # remove each subcarrier's mean, trend and curvature: slow drift otherwise
    # shows up as a "rhythm" at the bottom of the band
    s = np.linspace(-1, 1, n)
    B = np.stack([np.ones(n), s, s * s - 1 / 3], 1)
    U -= B @ np.linalg.lstsq(B, U, rcond=None)[0]
    win = np.hanning(n)
    f = np.fft.rfftfreq(n, 1 / FS)
    band = (f >= BAND[0]) & (f <= BAND[1])
    ref = (f > REF[0]) & (f < REF[1])
    _, v = np.linalg.eigh(U.T @ U)                       # PCs, strongest last (see doppler.py)
    best = (0.0, 0.0)
    for k in range(3):
        P = np.abs(np.fft.rfft((U @ v[:, -1 - k]) * win)) ** 2
        i = int(np.argmax(np.where(band, P, 0)))
        if not (P[i] > P[i - 1] and P[i] > P[i + 1]):    # a real peak, not a slope's edge
            continue
        prom = float(P[i] / max(np.median(P[ref]), 1e-18))
        if prom > best[1]:
            best = (f[i], prom)
    rate, prom = best
    if prom == 0:
        return dict(rate=0.0, prominence=0.0, agreement=0.0, level='none')
    Ps = np.abs(np.fft.rfft(U * win[:, None], axis=0)) ** 2
    peaks = f[np.argmax(np.where(band[:, None], Ps, 0), axis=0)]
    agree = float(np.mean(np.abs(peaks - rate) <= 1.5 / 60))
    level = ('clear' if prom >= CLEAR[0] and agree >= CLEAR[1]
             else 'possible' if prom >= POSSIBLE else 'none')
    return dict(rate=rate * 60, prominence=prom, agreement=agree, level=level)


def text(r):
    """Short header text for a reading."""
    if r is None:
        return 'breathing: collecting'
    if r['level'] == 'none':
        return 'breathing: none'
    return f"breathing ~{r['rate']:.0f}/min ({r['level']})"


def main():
    from doppler import recording, stamp
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    br = Breathing()
    print('time         rate/min  prominence  agreement  level')
    for w, d, inside in recording(*sys.argv[1:], warm_s=WIN_S):
        r = br.feed(d, d['ts'] / 1e6)
        if r and inside:
            print(f"{stamp(w)}  {r['rate']:6.1f}  {r['prominence']:9.0f}x  {r['agreement']:8.0%}   {r['level']}")


if __name__ == '__main__':
    main()
