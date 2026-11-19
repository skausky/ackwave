#!/usr/bin/env python3
"""Activity recognition by example (experimental, PC side only).

You show it what an activity looks like by recording a short clip (about 10 s)
of yourself doing it, and it recognises that activity when the signal looks
like one of its clips again. No training sessions: a few clips per activity,
recorded on different days, and corrections (each one becomes a clip) are
enough. When nothing looks close it says "unknown" instead of guessing.

What it looks at: the router link's ACKs (the link the board decides with).
Every HOP_S it takes the last WIN_S of normalised amplitudes and describes
them with a few numbers that say *how* the signal moves, more than *how much*:
  e_short, e_long   movement energy over the last 3 s and 20 s (log)
  spread, burst     whether the movement fills the window (walking) or comes
                    in one burst (sitting down, getting up)
  bands (4)         where the movement's power sits: 0.3-1, 1-3, 3-8, 8-20 Hz
  breath            a steady 8-36 /min rhythm over the last 20 s (log ratio)
  rssi_sd           signal-strength spread over the window
  shift             how far the channel's static shape moved over 20 s
plus the channel's static shape itself (52 values): where a still body is,
which is what tells sitting from lying. The shape only means something on the
channel it was recorded on, so it is compared only between clips and windows
on the same channel.

Two kinds of label:
  state   lasts (empty, still, sitting, lying, walking): every window is matched
  event   a moment (sitting down, getting up): recorded as a clip holding one
          event; it is found in the clip by its burst, and matched live only
          in windows that hold a burst

    python activity.py sessions/raw-....log.gz      # windows of a recording
"""
import json
import math
import os
import sys
import time
from collections import deque

import numpy as np

from analyze import MAX_APS, parse_line

HOP_S = 0.5                       # a feature window every this often
WIN_S = 3.0                       # short window
LONG_S = 20.0                     # long window (breathing, energy, shape drift)
CHUNK_S = 0.25                    # sub-windows for spread / burst
FS = 50.0                         # resampling rate for the spectrum (Hz)
MIN_RATE = 15.0                   # ACKs/s below this: no verdict (a weak link reads as movement)
GLITCH_SHIFT = 0.25               # static shape jumped this far: a router glitch, not a person
USED = np.r_[1:27, 38:64]         # the LLTF's 52 used subcarriers
K = 3                             # neighbours per label
TRIM_HEAD_S, TRIM_TAIL_S = 2.0, 1.0   # a clip's start and end are dropped (tapping the button)
EVENT_HALF_S = 1.0                # an event's windows: centres within this of its peak
MAX_CLIPS = 30                    # per label; the oldest go first
SHAPE_W = 0.5                     # weight of the static shape against the movement features
UNKNOWN_X = 3.0                   # farther than this x the typical distance: "nothing close"...
FAR_X = 4.5                       # ...unless one activity wins clearly (its distance at least
CLEAR_MARGIN = 0.3                # CLEAR_MARGIN below the next one's) within this x
SAME_CLIP_X = 1.6                 # an activity with one clip: its yardstick is widened this much
# smallest per-feature spread used for scaling (so a feature that barely varies
# in the clips can't blow small differences up)
MIN_SD = np.array([0.1, 0.1, 0.15, 0.1, 0.08, 0.08, 0.08, 0.08, 0.2, 0.1, 0.02])
BANDS = ((0.3, 1.0), (1.0, 3.0), (3.0, 8.0), (8.0, 20.0))
DYN_NAMES = ('e_short', 'e_long', 'spread', 'burst', 'b_0.3-1', 'b_1-3', 'b_3-8', 'b_8-20',
             'breath', 'rssi_sd', 'shift')
N_DYN = len(DYN_NAMES)

# Labels the dashboard offers; any other name works too (kind 'state')
LABELS = {
    'empty':       dict(kind='state', name='Empty', hint='Leave the room. It waits 10 s for you to go, then records 20 s.',
                        delay=10, secs=20),
    'still':       dict(kind='state', name='Still', hint='Stay where you usually are and keep still.', delay=3, secs=10),
    'sitting':     dict(kind='state', name='Sitting', hint='Sit where you usually sit and keep still.', delay=3, secs=10),
    'lying':       dict(kind='state', name='Lying', hint='Lie where you usually lie and keep still.', delay=3, secs=10),
    'walking':     dict(kind='state', name='Walking', hint='Walk around normally.', delay=3, secs=10),
    'sit_down':    dict(kind='event', name='Sitting down', hint='Stand, then sit down once while it records.',
                        delay=3, secs=10),
    'stand_up':    dict(kind='event', name='Getting up', hint='Sit, then stand up once while it records.',
                        delay=3, secs=10),
    'lie_down':    dict(kind='event', name='Lying down', hint='Lie down once while it records.', delay=3, secs=10),
    'get_up_bed':  dict(kind='event', name='Getting out of bed', hint='Get up from lying once while it records.',
                        delay=3, secs=10),
}


def label_info(label):
    return LABELS.get(label, dict(kind='state', name=label.replace('_', ' ').capitalize(), hint='', delay=3,
                                  secs=10))


def _top_mean(v, k=8):
    v = np.sort(v)
    return float(v[-k:].mean())


def _pc1(x):
    """The strongest pattern common to all subcarriers of x (time x 52, each
    column centred and detrended): its time series."""
    if x.shape[0] < 4:
        return np.zeros(x.shape[0])
    # x projected on the top eigenvector of x'x: the same as the SVD's
    # u[:, 0] * s[0] (up to sign, which no spectrum sees), much cheaper
    _w, v = np.linalg.eigh(x.T @ x)
    return x @ v[:, -1]


def _detrend(x, t):
    """Remove each column's mean and straight-line trend."""
    tc = t - t.mean()
    d = float((tc * tc).sum()) or 1.0
    slope = (tc[:, None] * (x - x.mean(0))).sum(0) / d
    return x - x.mean(0) - tc[:, None] * slope


def _resample(t, x, fs):
    """x (time x n) at irregular times t onto a regular grid; (grid, values)."""
    n = int((t[-1] - t[0]) * fs)
    if n < 8:
        return None, None
    g = t[0] + np.arange(n) / fs
    return g, np.stack([np.interp(g, t, x[:, j]) for j in range(x.shape[1])], 1)


class Features:
    """Feed it every CSI packet; it makes a feature window every HOP_S from the
    router link (the AP ACK stream heard most). window() gives the latest."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.buf = deque()                       # (t, rssi, x[52]) for the chosen link
        self.counts = {}                         # AP ACK source -> packets lately
        self.src = None
        self.ch = None
        self.next_t = None
        self.last = None                         # the latest window (dict) or None
        self.last_ts = None

    def feed(self, d, t):
        """d = parsed CSI dict, t = its time in seconds (board clock). Returns a
        new window (dict) when one is due, else None."""
        if self.last_ts is not None and d['ts'] < self.last_ts - 1_000_000:
            self.reset()                         # board restarted
        self.last_ts = d['ts']
        if d['kind'] != 1 or d['src'] >= MAX_APS:
            return None
        if self.ch is not None and d['ch'] != self.ch:
            self.reset()                         # new channel: old shapes mean nothing
            self.last_ts = d['ts']               # (still watching for a restart)
        self.ch = d['ch']
        src = d['src']
        self.counts[src] = self.counts.get(src, 0) + 1
        if self.src is None or (src != self.src and self.counts[src] > 2 * self.counts.get(self.src, 0) + 50):
            self.src = src                       # follow the AP that answers most
            self.buf.clear()
        if src != self.src:
            return None
        a = np.asarray(d['amp'], dtype=float)[USED]
        m = a.mean()
        if m <= 0:
            return None
        self.buf.append((t, d['rssi'], a / m))
        while self.buf and self.buf[0][0] < t - LONG_S:
            self.buf.popleft()
        if len(self.counts) > 8 or sum(self.counts.values()) > 100_000:
            self.counts = {k: v // 2 for k, v in self.counts.items()}
        if self.next_t is None:
            self.next_t = t + WIN_S
        if t < self.next_t:
            return None
        self.next_t = t + HOP_S
        self.last = self._window(t)
        return self.last

    def _window(self, t):
        ts = np.fromiter((p[0] for p in self.buf), float)
        xs = np.stack([p[2] for p in self.buf])
        rs = np.fromiter((p[1] for p in self.buf), float)
        w = ts >= t - WIN_S
        rate = float(w.sum()) / WIN_S
        out = dict(t=t, ch=self.ch, src=self.src, rate=rate, ok=False, glitch=False)
        if rate < MIN_RATE or w.sum() < 16:
            return out
        tw, xw = ts[w], xs[w]
        dx = _detrend(xw, tw)
        e_short = math.log10(_top_mean(dx.var(0)) + 1e-9)
        dl = _detrend(xs, ts)
        e_long = math.log10(_top_mean(dl.var(0)) + 1e-9)
        # spread / burst: energy per 0.25 s chunk
        k = np.floor((tw - tw[0]) / CHUNK_S).astype(int)
        ce = np.array([dx[k == i].var(0).mean() if (k == i).sum() >= 3 else np.nan for i in range(k.max() + 1)])
        ce = ce[np.isfinite(ce)]
        if ce.size < 4:
            return out
        spread = float((ce > 0.5 * ce.max()).mean())
        burst = math.log10((ce.max() + 1e-12) / (np.median(ce) + 1e-12))
        # where the movement's power sits (3 s, strongest common pattern)
        g, xr = _resample(tw, dx, FS)
        bands = [0.0] * len(BANDS)
        if g is not None:
            pc = _pc1(xr - xr.mean(0))
            spec = np.abs(np.fft.rfft(pc * np.hanning(pc.size))) ** 2
            f = np.fft.rfftfreq(pc.size, 1 / FS)
            tot = float(spec[(f >= BANDS[0][0]) & (f < BANDS[-1][1])].sum()) or 1e-12
            bands = [float(spec[(f >= lo) & (f < hi)].sum()) / tot for lo, hi in BANDS]
        # breathing: a peak at 8-36 /min over the long window, against 0.6-3 Hz
        breath = 0.0
        if ts[-1] - ts[0] >= 0.75 * LONG_S:
            g2, xl = _resample(ts, dl, 10.0)
            if g2 is not None:
                pc = _pc1(xl - xl.mean(0))
                spec = np.abs(np.fft.rfft(pc * np.hanning(pc.size))) ** 2
                f = np.fft.rfftfreq(pc.size, 0.1)
                br, ref = spec[(f >= 0.13) & (f <= 0.6)], spec[(f > 0.6) & (f <= 3.0)]
                if br.size and ref.size:
                    breath = math.log10((br.max() + 1e-12) / (np.median(ref) + 1e-12))
        shape = xw.mean(0)
        old = xs[ts < ts[0] + WIN_S].mean(0)
        shift = float(np.abs(shape - old).mean())
        out.update(ok=True, glitch=shift > GLITCH_SHIFT,
                   dyn=[e_short, e_long, spread, burst, *bands, breath, float(rs[w].std()), shift],
                   shape=shape.tolist())
        return out


# ---------------------------------------------------------------- clips + matching
class Activities:
    """The clips you recorded and the matcher. Clips are saved in `path`
    (sessions/activities.json); each holds the feature windows it recorded."""

    def __init__(self, path=None, clock=time.time):
        self.path, self.clock = path, clock
        self.clips = []
        self.feat = Features()
        self.rec = None                  # recording in progress: dict(label, start, end, wins)
        self.recent = deque(maxlen=6)    # the last matches (for smoothing)
        self.result = None               # the latest verdict (dict)
        self.events = deque(maxlen=50)   # dict(t, label, dist): events seen
        self._event_hold = 0.0
        self._scale = None
        self.history = deque(maxlen=int(30 / HOP_S))   # recent windows, for corrections
        self.last_saved = None           # dict(label, ok, windows, t) of the last recording
        self.load()

    # ---------- storage ----------
    def load(self):
        if not self.path:
            return
        try:
            with open(self.path) as f:
                got = json.load(f)
            clips = got.get('clips', []) if isinstance(got, dict) else []
            self.clips = [c for c in clips if self._clip_ok(c)]
        except (OSError, ValueError, TypeError, AttributeError):
            self.clips = []
        self._scale = None

    @staticmethod
    def _clip_ok(c):
        try:
            return (isinstance(c['label'], str) and 0 < len(c['label']) <= 32 and c['wins'] and
                    all(len(w['dyn']) == N_DYN and len(w['shape']) == len(USED) for w in c['wins']))
        except (KeyError, TypeError):
            return False

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.tmp'
        r = lambda v: [round(x, 5) for x in v]
        clips = [dict(c, wins=[dict(dyn=r(w['dyn']), shape=r(w['shape']), t=w.get('t', 0)) for w in c['wins']])
                 for c in self.clips]
        with open(tmp, 'w') as f:
            json.dump(dict(version=1, clips=clips), f)
        os.replace(tmp, self.path)

    def add_clip(self, label, wins, ch, source='recorded', t=None):
        """Store windows as a clip; returns it (None if there is nothing usable)."""
        wins = [dict(dyn=w['dyn'], shape=w['shape'], t=round(w['t'], 2)) for w in wins
                if w.get('ok') and not w.get('glitch')]
        if not wins:
            return None
        c = dict(id=f"{int((t or self.clock()) * 1000):x}{len(self.clips):02x}", label=label,
                 kind=label_info(label)['kind'], t=t or self.clock(), ch=ch, source=source, wins=wins)
        self.clips.append(c)
        mine = [x for x in self.clips if x['label'] == label]
        for old in mine[:-MAX_CLIPS]:            # oldest first
            self.clips.remove(old)
        self._scale = None
        self.save()
        return c

    def delete(self, clip_id):
        n = len(self.clips)
        self.clips = [c for c in self.clips if c['id'] != clip_id]
        if len(self.clips) != n:
            self._scale = None
            self.save()
        return len(self.clips) != n

    def clear(self, label=None):
        self.clips = [c for c in self.clips if label is not None and c['label'] != label]
        self._scale = None
        self.save()

    # ---------- recording ----------
    def start(self, label, now, delay=None, secs=None):
        """Start recording `label` after `delay` s, for `secs` s (board clock)."""
        info = label_info(label)
        delay = info['delay'] if delay is None else delay
        secs = info['secs'] if secs is None else secs
        self.rec = dict(label=label, kind=info['kind'], start=now + delay, end=now + delay + secs, wins=[],
                        ch=None)

    def cancel(self):
        self.rec = None

    def recording(self, now):
        """What the page shows while recording: dict or None."""
        r = self.rec
        if not r:
            return None
        if now < r['start']:
            return dict(label=r['label'], phase='countdown', left=round(r['start'] - now, 1))
        return dict(label=r['label'], phase='recording', left=round(max(0.0, r['end'] - now), 1),
                    total=round(r['end'] - r['start'], 1), got=len(r['wins']))

    def _finish(self):
        r, self.rec = self.rec, None
        wins = [w for w in r['wins'] if r['start'] + TRIM_HEAD_S + WIN_S <= w['t'] <= r['end'] - TRIM_TAIL_S]
        if r['kind'] == 'event':
            wins = self._event_windows(r['wins'])
        c = self.add_clip(r['label'], wins, r['ch'])
        self.last_saved = dict(label=r['label'], ok=c is not None, windows=len(c['wins']) if c else 0,
                               t=self.clock())
        return c

    @staticmethod
    def _event_windows(wins):
        """The windows around a clip's one event: the window whose 3 s holds the
        biggest burst, and its neighbours within EVENT_HALF_S."""
        good = [w for w in wins if w.get('ok') and not w.get('glitch')]
        if not good:
            return []
        peak = max(good, key=lambda w: w['dyn'][0])            # most energy in 3 s
        return [w for w in good if abs(w['t'] - peak['t']) <= EVENT_HALF_S]

    # ---------- matching ----------
    def _prep(self):
        """Per-feature scales (how much each varies across all clips, so no one
        number dominates), the scale of shape differences, and the typical
        distance from a window to its nearest neighbour of the same label: the
        yardstick for "close" and "unknown"."""
        if self._scale is not None:
            return self._scale
        # every clip window as rows of arrays, so distances are computed for
        # all of them at once (a Python loop per pair took seconds with many
        # clips, and the dashboard waits for this)
        rows = [(i, c, w) for i, c in enumerate(self.clips) for w in c['wins']]
        if not rows:
            self._scale = dict(sd=np.ones(N_DYN), shape=0.02, typ=1.0)
            self._mat = None
            return self._scale
        labs = sorted({c['label'] for _i, c, _w in rows})
        m = dict(dyn=np.array([w['dyn'] for _i, _c, w in rows], float),
                 shape=np.array([w['shape'] for _i, _c, w in rows], float),
                 lab=np.array([labs.index(c['label']) for _i, c, _w in rows]),
                 clip=np.array([i for i, _c, _w in rows]),
                 ch=np.array([-1 if c['ch'] is None else c['ch'] for _i, c, _w in rows]),
                 t=np.array([w.get('t', 0) for _i, _c, w in rows], float), labs=labs)
        # how much each number varies within the same activity (averaged over
        # activities): pooling all windows would let the most uniform activity
        # (a still room) set the scale and blow every other difference up
        spreads = []
        for j in range(len(labs)):
            d = m['dyn'][m['lab'] == j]
            if len(d) >= 3:
                spreads.append(np.median(np.abs(d - np.median(d, 0)), 0) * 1.4826)
        sd = np.mean(spreads, 0) if spreads else np.zeros(N_DYN)
        sd = np.maximum(sd, MIN_SD)
        # shape: half the median difference between any two windows, so a
        # typical difference counts about as much as one feature's
        sh = m['shape']
        pick = sh[:: max(1, len(sh) // 60)]
        diffs = np.abs(pick[:, None, :] - pick[None, :, :]).mean(2)
        iu = np.triu_indices(len(pick), 1)
        shape = float(np.median(diffs[iu])) if iu[0].size else 0.02
        self._scale = dict(sd=sd, shape=max(shape, 0.005) * 2.0, typ=1.0)
        self._mat = m
        # nearest neighbour of the same label: in another clip when there is
        # one, else in the same clip but not overlapping (>= WIN_S apart)
        clips_of = {j: set(m['clip'][m['lab'] == j].tolist()) for j in range(len(labs))}
        near = []
        for r in range(0, len(rows), 2):
            j, ci = m['lab'][r], m['clip'][r]
            dist = self._dists(m['dyn'][r], m['shape'][r], m['ch'][r])
            if len(clips_of[j]) > 1:
                cand, k = (m['lab'] == j) & (m['clip'] != ci), 1.0
            else:                                 # one clip: its own windows are more alike than
                cand = (m['clip'] == ci) & (np.abs(m['t'] - m['t'][r]) >= WIN_S)   # any later moment,
                k = SAME_CLIP_X                                                     # so allow more
            if cand.any():
                near.append(k * float(dist[cand].min()))
        if near:
            self._scale['typ'] = max(float(np.median(near)), 0.3)
        return self._scale

    def _dists(self, dyn, shape, ch):
        """Distance from one window to every clip window (an array, in _mat's order)."""
        s, m = self._scale, self._mat
        dd = (m['dyn'] - np.asarray(dyn, float)) / s['sd']
        d = np.mean(dd * dd, 1)
        same = m['ch'] == (-1 if ch is None else ch)          # where a still body is: same channel only
        if same.any():
            sh = np.mean(np.abs(m['shape'][same] - np.asarray(shape, float)), 1) / s['shape']
            d[same] += SHAPE_W * sh * sh
        return np.sqrt(d)

    def match(self, w):
        """Score one window against every clip: {label: mean distance to its K
        nearest windows}."""
        self._prep()
        m = self._mat
        if m is None:
            return {}
        dist = self._dists(w['dyn'], w['shape'], w['ch'])
        scores = {}
        for j, lab in enumerate(m['labs']):
            v = dist[m['lab'] == j]
            k = min(K, v.size)
            scores[lab] = float(np.partition(v, k - 1)[:k].mean())
        return scores

    def labels(self):
        out = {}
        for c in self.clips:
            o = out.setdefault(c['label'], dict(label=c['label'], name=label_info(c['label'])['name'],
                                                kind=c['kind'], clips=0, last=0.0, chs=set()))
            o['clips'] += 1
            o['last'] = max(o['last'], c['t'])
            o['chs'].add(c['ch'])
        for o in out.values():
            o['chs'] = sorted(x for x in o['chs'] if x is not None)
        return out

    # ---------- live ----------
    def feed(self, d, t):
        """Every CSI packet. Returns the new verdict when a window was made."""
        w = self.feat.feed(d, t)
        if w is None:
            return None
        self.history.append(w)
        r = self.rec
        if r is not None and w['t'] < r['start'] - 61:
            self.rec = r = None                  # the board's clock jumped back (restart, demo loop)
        if r is not None:
            if r['start'] <= w['t']:
                r['ch'] = r['ch'] or w['ch']
                r['wins'].append(w)
            if w['t'] >= r['end']:
                self._finish()
        self.result = self._verdict(w)
        return self.result

    def _verdict(self, w):
        base = dict(t=w['t'], ch=w['ch'], rate=round(w['rate']))
        if not self.clips:
            return dict(base, state='uncalibrated')
        if not w['ok']:
            self.recent.clear()
            return dict(base, state='no-data', why='weak link' if w['rate'] < MIN_RATE else 'starting')
        if w['glitch']:
            return dict(base, state='unknown', why='router glitch')
        scores = self.match(w)
        typ = self._scale['typ']
        kinds = {c['label']: c['kind'] for c in self.clips}
        is_burst = w['dyn'][3] > 0.6 and w['dyn'][2] < 0.5      # one burst in the window
        # events: only in a burst, and only once per burst
        ev = sorted((v, lab) for lab, v in scores.items() if kinds[lab] == 'event')
        if ev and is_burst and w['t'] >= self._event_hold and ev[0][0] <= UNKNOWN_X * typ:
            st = sorted(v for lab, v in scores.items() if kinds[lab] == 'state')
            if not st or ev[0][0] < st[0]:
                self.events.append(dict(t=w['t'], label=ev[0][1], name=label_info(ev[0][1])['name'],
                                        dist=round(ev[0][0], 2)))
                self._event_hold = w['t'] + WIN_S
        st = sorted((v, lab) for lab, v in scores.items() if kinds[lab] == 'state')
        if not st:
            return dict(base, state='unknown', why='no states calibrated', scores=self._rounded(scores))
        best, lab = st[0]
        second = st[1][0] if len(st) > 1 else best * 2 + typ
        margin = (second - best) / (second + 1e-9)
        # close to a clip, or a clear winner that is not far off: a match
        near = best <= UNKNOWN_X * typ or (best <= FAR_X * typ and margin >= CLEAR_MARGIN)
        self.recent.append(lab if near else None)
        votes = [x for x in self.recent if x is not None]
        top = max(set(votes), key=votes.count) if votes else None
        if top is None or votes.count(top) < max(2, len(self.recent) // 2):
            return dict(base, state='unknown', why='changing' if near else 'nothing close',
                        scores=self._rounded(scores), best=lab)
        conf = 'close' if best <= 1.5 * typ and margin > 0.2 else 'likely'
        return dict(base, state=top, name=label_info(top)['name'], conf=conf, dist=round(best / typ, 2),
                    scores=self._rounded(scores))

    @staticmethod
    def _rounded(scores):
        return {k: round(v, 2) for k, v in scores.items()}

    def correct(self, label, now, secs=6.0):
        """The verdict was wrong (or right): keep the last `secs` of windows as a
        clip of `label`. Returns the clip or None."""
        wins = [w for w in self.history if w['t'] >= now - secs and w['ok'] and not w['glitch']]
        if not wins:
            return None
        return self.add_clip(label, wins, wins[-1]['ch'], source='correction')


def windows_of(lines):
    """Feature windows of a recording (lines of a raw log)."""
    f = Features()
    t0 = None
    for line in lines:
        got = parse_line(line)
        if not got or got[0] != 'csi':
            continue
        d = got[1]
        t0 = d['ts'] if t0 is None else t0
        w = f.feed(d, (d['ts'] - t0) / 1e6)
        if w is not None:
            yield w


def main():
    import gzip
    path = sys.argv[1]
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt', errors='ignore') as fh:
        print('    t  ' + ' '.join(f'{n:>8}' for n in DYN_NAMES))
        for w in windows_of(fh):
            if w['ok']:
                print(f"{w['t']:5.1f}  " + ' '.join(f'{v:8.3f}' for v in w['dyn']) + ('  glitch' if w['glitch'] else ''))


if __name__ == '__main__':
    main()
