#!/usr/bin/env python3
"""Doppler view of the router's ACK stream (experimental, PC side only).

A moving body changes the length of a reflected path, which makes the CSI
amplitude of every subcarrier ripple at the Doppler frequency
f = 2 v / wavelength (about 16 Hz per m/s on 2.4 GHz). Every hop (0.25 s)
this takes the last 1 s of normalised amplitudes, resamples them to 100 Hz,
removes each subcarrier's mean and trend, keeps the 3 strongest patterns
common to all subcarriers (PCA) and sums their power spectra.

What it showed on real captures:
  quiet room   flat, noise-like spectrum (centre ~20 Hz), low power
  walking      ~40 % under 3 Hz, the rest spread up to ~20 Hz, lasting
  hand waving  mostly 0.5-8 Hz
  router scan  high power, 83-95 % of it under 3 Hz: a step, not a movement
  noise        now and then a window pokes above the quiet room at ~25-30 Hz
               with only 2-8 % under 3 Hz; 'moving' needs >= 12 % (MOVING_LOW)
It does not decide motion; the GUI shows it next to the detector.

    python doppler.py sessions/raw-2026-11-09_2238.log.gz 07:07:20 07:08:20
prints one line per hop for that stretch of a recorded session.
"""
import sys
from collections import deque

import numpy as np

from analyze import MAX_APS, VALID, parse_line

FS = 100.0                        # resampling rate (Hz)
WIN = 100                         # 1 s window
HOP_S = 0.25
N_PC = 3
LOW_HZ = 3.0                      # "step-like" energy is below this
MOVING_LOW = 0.12                 # movement keeps >= this share under 3 Hz; quiet-room
                                  # noise that pokes above the baseline sits at 2-8 %
FREQS = np.fft.rfftfreq(WIN, 1 / FS)
_HANN = np.hanning(WIN)


def wavelength(ch):
    """Metres, for a 2.4 GHz channel number."""
    return 299_792_458 / ((2407 + 5 * ch) * 1e6) if ch and ch < 15 else 0.123


class Doppler:
    """Feed it every CSI packet; it keeps spectra for the deciding ACK stream."""

    def __init__(self, keep_s=600.0, src=None):
        self.keep_s = keep_s
        self.src = src                            # None: the APs' ACKs; else only node src
        self.buf = {}                             # (src, kind) -> deque[(t, x)]
        self.rows = deque()                       # (t, spectrum, features)
        self.base = None                          # quiet-room spectrum (slow median)
        self.next_t = None
        self.ch = 6

    def feed(self, d, t):
        """d = parsed CSI dict, t = its time in seconds (board clock)."""
        if d['kind'] != 1 or (d['src'] >= MAX_APS if self.src is None else d['src'] != self.src):
            return None
        self.ch = d.get('ch') or self.ch
        a = d['amp'][VALID]
        m = a.mean()
        if m <= 0:
            return None
        q = self.buf.setdefault((d['src'], 1), deque())
        q.append((t, a / m))
        while q and q[0][0] < t - 1.5:
            q.popleft()
        if self.next_t is None or t < self.next_t - 5:   # start, or the clock went back
            self.next_t = t + HOP_S
            self.rows.clear()
            return None
        if t < self.next_t:
            return None
        self.next_t += HOP_S
        q = max(self.buf.values(), key=len)              # busiest router ACK stream
        row = spectrum(q, t)
        if row is None:
            return None
        feat = self.features(row)
        self.rows.append((t, row, feat))
        while self.rows and self.rows[0][0] < t - self.keep_s:
            self.rows.popleft()
        return feat

    def features(self, p):
        """power (log10), share under 3 Hz, excess-power frequency and speed."""
        tot = p[1:].sum()
        low = p[(FREQS > 0) & (FREQS < LOW_HZ)].sum() / tot if tot > 0 else 0.0
        if self.base is None:
            self.base = p.copy()
        else:                                     # follows quiet fast, motion slowly
            k = np.where(p < self.base, 0.05, 0.002)
            self.base += k * (p - self.base)
        ex = np.clip(p - 2 * self.base, 0, None)[1:]
        # a frequency only when clearly above the quiet room (half the power)
        f_ex = float((ex * FREQS[1:]).sum() / ex.sum()) if ex.sum() > 0.5 * tot else 0.0
        return dict(power=float(np.log10(tot + 1e-12)), low=float(low), f=f_ex,
                    speed=f_ex * wavelength(self.ch) / 2,
                    moving=f_ex > 0 and low >= MOVING_LOW)


def spectrum(q, t_end):
    """Summed power spectrum of the top PCs over the last 1 s, or None."""
    t = np.fromiter((p[0] for p in q), float)
    if len(t) < WIN // 2 or t[-1] - t[0] < 0.9:
        return None
    X = np.array([p[1] for p in q])
    g = t_end - 1.0 + np.arange(WIN) / FS
    U = np.empty((WIN, X.shape[1]))
    for j in range(X.shape[1]):
        U[:, j] = np.interp(g, t, X[:, j])
    # remove each subcarrier's mean and linear trend (the static channel)
    s = np.arange(WIN) - (WIN - 1) / 2
    U -= U.mean(0)
    U -= np.outer(s, (s @ U) / (s @ s))
    _, _, vt = np.linalg.svd(U, full_matrices=False)
    pcs = U @ vt[:N_PC].T * _HANN[:, None]
    return (np.abs(np.fft.rfft(pcs, axis=0)) ** 2).sum(1)


def recording(path, a, b, warm_s=60.0):
    """Yields (wall time, parsed CSI dict) from a recorded raw log, for the
    first a..b (HH:MM:SS, may cross midnight) plus warm_s before it."""
    import gzip
    import time

    def clock(hms, after, slack=1):
        """Unix time of hms on after's day, or the next day if it is more than
        slack s before 'after' (a session that runs past midnight)."""
        day = time.strftime('%Y-%m-%d ', time.localtime(after))
        x = time.mktime(time.strptime(day + hms, '%Y-%m-%d %H:%M:%S'))
        return x + 86400 if x < after - slack else x

    off = t_a = t_b = None
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', errors='replace') as f:
        try:
            for line in f:
                if line.startswith('#T,'):
                    p = line.strip().split(',')
                    if len(p) > 2 and p[2]:
                        off = float(p[1]) - int(p[2]) / 1e6
                        if t_a is None:          # a start just before the first marker is today
                            t_a = clock(a, float(p[1]), slack=12 * 3600)
                            t_b = clock(b, t_a)
                    continue
                if off is None or not line.startswith('CSI,'):
                    continue
                try:
                    w = int(line.split(',', 2)[1]) / 1e6 + off
                except ValueError:
                    continue
                if w < t_a - warm_s:             # cheap skip up to the window
                    continue
                if w > t_b:
                    return
                ev = parse_line(line)
                if ev:
                    yield w, ev[1], w >= t_a
        except EOFError:                         # a session still being written
            return


def stamp(w):
    import time
    return time.strftime('%H:%M:%S', time.localtime(w)) + f".{int(w % 1 * 100):02d}"


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    dop = Doppler()
    print('time         power  <3Hz   freq   speed')
    for w, d, inside in recording(*sys.argv[1:]):   # 60 s to learn the quiet spectrum
        r = dop.feed(d, d['ts'] / 1e6)
        if r and inside:
            print(stamp(w) + f"  {r['power']:5.2f}  {r['low']:4.0%}  {r['f']:5.1f}Hz {r['speed']:4.2f} m/s"
                  + ('  moving' if r['moving'] else ''))


if __name__ == '__main__':
    main()
