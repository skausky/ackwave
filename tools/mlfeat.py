#!/usr/bin/env python3
"""Gain-proof motion features for a learned detector (PC side, experimental).

One link's CSI amplitudes (one packet per row) are cut into 1 s windows; each
window is described by numbers that say *how* the signal moves, all ratios so
the radio's automatic gain cancels out (the idea from the ESPectre review,
docs/redesign/README.md; no code or data of theirs is used here):

  jump      mean change of the normalised shape over 0.1 s (log)
  jump4     the same over the last 4 s (log)
  burst     the biggest 0.25 s chunk's jump over the window's mean
  sp_ac     lag-1 autocorrelation of the per-packet spread (spread = sd/mean
            across subcarriers): smooth when a body moves, white when idle
  sp_iqr    the spread's interquartile range over its median
  sp_zcr    how often the spread crosses its mean (per packet)
  tcv       median over subcarriers of the time sd/mean of the normalised shape
  rssi_sd   the signal strength's spread (dB)

    python mlfeat.py sessions/raw-....log.gz      # one row per second of a recording
"""
import gzip
import sys

import numpy as np

FEATURES = ('jump', 'jump4', 'burst', 'sp_ac', 'sp_iqr', 'sp_zcr', 'tcv', 'rssi_sd')
WIN_S = 1.0
LONG_S = 4.0
MIN_PKTS = 15                      # fewer packets in a window: no verdict
LAG_S = 0.1                        # the shape jump's time step
USED = np.r_[1:27, 38:64]          # the LLTF's used subcarriers (our order)


def _norm(a):
    """Each packet's amplitudes over their mean: gain gone."""
    m = a.mean(axis=1, keepdims=True)
    return a / np.maximum(m, 1e-9)


def window_features(t, amp, rssi, t_end):
    """Features of the window ending at t_end; amp is (n, k) amplitudes on the
    used subcarriers, t in seconds. None when too few packets."""
    w = (t > t_end - WIN_S) & (t <= t_end)
    lw = (t > t_end - LONG_S) & (t <= t_end)
    if w.sum() < MIN_PKTS:
        return None
    a = _norm(amp[lw])
    tl = t[lw]
    # shape jump: each packet against the one LAG_S earlier (the same at any packet rate)
    j = np.searchsorted(tl, tl - LAG_S, side='right') - 1
    ok = j >= 0
    d = np.abs(a[ok] - a[j[ok]]).mean(axis=1)
    td = tl[ok]
    win = td > t_end - WIN_S
    if win.sum() < MIN_PKTS - 1:
        return None
    jump = d[win].mean()
    jump4 = d.mean()
    chunks = [d[win & (td > t_end - WIN_S + i * 0.25) & (td <= t_end - WIN_S + (i + 1) * 0.25)] for i in range(4)]
    burst = max((c.mean() for c in chunks if len(c)), default=jump) / max(jump, 1e-9)
    aw = _norm(amp[w])
    sp = aw.std(axis=1)                                  # spread across subcarriers (already / mean)
    s0 = sp - sp.mean()
    den = (s0 * s0).sum()
    sp_ac = float((s0[1:] * s0[:-1]).sum() / den) if den > 0 else 0.0
    q1, q2, q3 = np.percentile(sp, [25, 50, 75])
    sp_iqr = (q3 - q1) / max(q2, 1e-9)
    sp_zcr = float((np.diff(np.sign(s0)) != 0).mean())
    tcv = float(np.median(aw.std(axis=0) / np.maximum(aw.mean(axis=0), 1e-9)))
    rssi_sd = float(np.std(rssi[w]))
    return np.array([np.log(jump + 1e-6), np.log(jump4 + 1e-6), burst, sp_ac, sp_iqr, sp_zcr, tcv, rssi_sd])


def stream_features(t, amp, rssi, hop=WIN_S):
    """(times, features) every `hop` s over one link's packets."""
    out_t, out_f = [], []
    if len(t) < MIN_PKTS:
        return np.array(out_t), np.zeros((0, len(FEATURES)))
    te = t[0] + LONG_S
    while te <= t[-1]:
        f = window_features(t, amp, rssi, te)
        if f is not None:
            out_t.append(te)
            out_f.append(f)
        te += hop
    return np.array(out_t), np.array(out_f).reshape(-1, len(FEATURES))


def _lines(f):
    """A file's lines; a recording cut off mid-write (a killed recorder) ends where it was cut."""
    try:
        yield from f
    except (EOFError, OSError):
        return


def read_raw(path):
    """A raw recording's router-link packets, on the wall clock: (t, amp, rssi)
    of the link with the most ACKs (the one the board decides with)."""
    from analyze import MAX_APS, parse_line
    op = gzip.open if path.endswith('.gz') else open
    rows = {}
    off = None                                           # wall - board seconds, from the #T markers
    pend_wall = None
    with op(path, 'rt', errors='replace') as f:
        for line in _lines(f):
            if line.startswith('#T,'):
                try:
                    pend_wall = float(line.split(',')[1])
                except (ValueError, IndexError):
                    pass
                continue
            if not line.startswith('CSI,'):
                continue
            ev = parse_line(line)
            if not ev or ev[0] != 'csi':
                continue
            d = ev[1]
            if pend_wall is not None:
                off = pend_wall - d['ts'] / 1e6
                pend_wall = None
            if off is None or d['kind'] != 1 or d['src'] >= MAX_APS:
                continue
            r = rows.setdefault(d['src'], ([], [], []))
            r[0].append(d['ts'] / 1e6 + off)
            r[1].append(d['amp'][USED])
            r[2].append(d['rssi'])
    if not rows:
        return np.zeros(0), np.zeros((0, len(USED))), np.zeros(0)
    t, a, r = max(rows.values(), key=lambda v: len(v[0]))
    t = np.array(t)
    order = np.argsort(t, kind='stable')
    return t[order], np.array(a, dtype=float)[order], np.array(r, dtype=float)[order]


class Model:
    """The board's model (csi_core.h ml_forward), from a params list or ml_motion.json."""

    def __init__(self, p, nh=0, no=1):
        p = np.asarray(p, float)
        F = len(FEATURES)
        self.nh, self.no = nh, no
        self.mu, self.sd, self.ref = p[:F], p[F:2 * F], p[2 * F:2 * F + 2]
        w = p[2 * F + 2:]
        if nh:
            self.w1, self.b1 = w[:F * nh].reshape(F, nh), w[F * nh:F * nh + nh]
            w = w[F * nh + nh:]
        nin = nh or F
        self.w2, self.b2 = w[:nin * no].reshape(nin, no), w[nin * no:nin * no + no]

    @classmethod
    def load(cls, path=None):
        import json
        import os
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ml_motion.json')
        with open(path) as f:
            m = json.load(f)
        if tuple(m['features']) != FEATURES:
            raise ValueError('the model was made for other features')
        nh, no = int(m.get('hidden', 0)), int(m.get('outputs', 1))
        p = list(m['mu']) + list(m['sd']) + list(m.get('ref', [0.0, 0.0]))
        p += (list(np.ravel(m['w1'])) + list(m['b1']) + list(np.ravel(m['w2'])) + list(m['b2'])) if nh else \
            (list(np.ravel(m['w'])) + ([m['b']] if np.isscalar(m['b']) else list(m['b'])))
        return cls(p, nh, no), float(m.get('threshold', 0.5))

    def params(self):
        out = list(self.mu) + list(self.sd) + list(self.ref)
        if self.nh:
            out += list(self.w1.ravel()) + list(self.b1)
        return out + list(self.w2.ravel()) + list(self.b2)

    def forward(self, f, quiet=None):
        """[p_empty, p_still, p_moving] (1-output models: [1-p, 0, p])."""
        x = np.array(f, float)
        if quiet is not None:
            x[:2] += self.ref - quiet
        x = (x - self.mu) / np.maximum(self.sd, 1e-9)
        h = np.tanh(x @ self.w1 + self.b1) if self.nh else x
        z = h @ self.w2 + self.b2
        if self.no == 1:
            p = 1 / (1 + np.exp(-z[0]))
            return np.array([1 - p, 0.0, p])
        e = np.exp(z - z.max())
        return e / e.sum()


class Quiet:
    """This board's quiet level of jump, jump4: their 20th percentile over the
    last 5 min of features (csi_core.h ml_quiet_t)."""
    N, MIN = 300, 60

    def __init__(self):
        from collections import deque
        self.ring = deque(maxlen=self.N)

    def add(self, f):
        self.ring.append((f[0], f[1]))
        return self.level()

    def level(self):
        if len(self.ring) < self.MIN:
            return None
        a = np.array(self.ring, dtype=np.float32)
        return np.array([np.percentile(a[:, 0], 20), np.percentile(a[:, 1], 20)])


class Shadow:
    """The learned detector, live, beside the board's own verdict (experimental):
    the decision link's packets in, every second a probability of movement;
    motion once two of the last three seconds say so, still once three in a
    row don't. It never changes the board's verdict, the light or the alerts.
    Same steps as the board (csi_core.h ml_*): the twin the tests compare."""

    def __init__(self, path=None):
        from collections import deque
        self.model, self.thr = Model.load(path)
        self.quiet = Quiet()
        self.buf = deque()
        self.next_t = None
        self.recent = deque(maxlen=3)
        self.p = None
        self.probs = None
        self.f = None
        self.motion = False

    def feed(self, src, d, t):
        """One CSI packet (analyze.parse_line's dict, t in s). Returns True/False
        when the verdict changes, else None."""
        if d.get('kind') != 1 or d.get('src') != src:
            return None
        if self.buf and t < self.buf[-1][0]:
            self.buf.clear()                         # the board restarted
            self.next_t = None
        if self.next_t is None:
            self.next_t = t + LONG_S
        got = None
        while t > self.next_t:                       # windows on a 1 s grid, as the board (ml_feed)
            te = self.next_t
            self.next_t += WIN_S
            if not self.buf:
                continue
            ts = np.array([b[0] for b in self.buf])
            f = window_features(ts, np.array([b[1] for b in self.buf]), np.array([b[2] for b in self.buf]), te)
            if f is None or not np.isfinite(f).all():
                self.p = None
                continue
            ch = self.step(f)
            got = ch if ch is not None else got
        self.buf.append((t, d['amp'][USED].astype(float), float(d['rssi'])))
        while self.buf and self.buf[0][0] <= t - LONG_S - 0.5:
            self.buf.popleft()
        return got

    def step(self, f):
        """One second's features (from the Pi or from a board's report)."""
        q = self.quiet.add(f)
        self.f = f
        self.probs = self.model.forward(f, q)
        self.p = float(self.probs[2])
        self.recent.append(self.p > self.thr)
        was = self.motion
        if not was and sum(self.recent) >= 2:
            self.motion = True
        elif was and len(self.recent) == 3 and not any(self.recent):
            self.motion = False
        return self.motion if self.motion != was else None


def main():
    for path in sys.argv[1:]:
        t, a, r = read_raw(path)
        ts, fs = stream_features(t, a, r)
        print(f"# {path}: {len(t)} packets, {len(ts)} windows")
        print('t,' + ','.join(FEATURES))
        for ti, fi in zip(ts, fs):
            print(f"{ti:.1f}," + ','.join(f'{x:.4f}' for x in fi))


if __name__ == '__main__':
    main()
