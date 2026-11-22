"""Where people are, on the Pi: the Map page's tracker (web/map.js), in numpy.

The browser draws the tracking while a page is open; this runs all the time in
the dashboard, so zones, alerts and the people count work with no page open.
Same model and constants as map.js (keep them in step):

  every link reacts to a person at spot x by its sensitivity there (the
  Fresnel zone, the ends' antenna patterns), scaled by how much they move;
  person 1 is the spot that best explains every link's activity above its own
  idle level; what's left over may be person 2 (and 3), only if it explains
  the rest clearly better than noise would; each person keeps a Bayes filter.

Positions are metres from the hub (the Map's layout). A link end you didn't
place is spread over a few spots around where the map last put it (or over
its rough spot): unknown routers blur, they don't mislead.
"""
import math

import numpy as np

CELL = 0.25
SIG0, SIG1, MODEL_ERR = 0.45, 0.25, 1.2
MAX_PEOPLE, MULTI_GAIN, MULTI_LEFT, MULTI_SEP, KEEP_S = 3, 4.0, 1.5, 2.5, 2.5
BACK = 0.45                       # a board's back: 45 % of the amplitude (-7 dB)
G0, WG = 1.2, 1 / 1.5 ** 2
MAX_CELLS = 12000


class End:
    """A link end: a spot (metres), its height, and where it faces (radians or None)."""
    __slots__ = ('x', 'y', 'z', 'facing')

    def __init__(self, x, y, z=0.0, facing=None):
        self.x, self.y, self.z, self.facing = float(x), float(y), float(z), facing


def gain_at(e, px, py):
    """The end's antenna pattern towards (px, py): 1 ahead, BACK behind."""
    if e.facing is None:
        return np.ones_like(px)
    dx, dy = px - e.x, py - e.y
    c = np.cos(np.arctan2(dy, dx) - e.facing)
    g = BACK + (1 - BACK) * ((1 + c) / 2) ** 1.5
    return np.where(dx * dx + dy * dy < 0.09, 1.0, g)


def sensitivity(px, py, a, n, lam):
    """How much a person at (px, py) changes the a-n link (0..1): map.js sensitivity()."""
    d = math.dist((a.x, a.y, a.z), (n.x, n.y, n.z))
    hx, hy = n.x - a.x, n.y - a.y
    hh = hx * hx + hy * hy
    t = np.clip(((px - a.x) * hx + (py - a.y) * hy) / hh, 0, 1) if hh > 1e-6 else np.full_like(px, 0.5)
    pz = a.z + t * (n.z - a.z)
    d1 = np.sqrt((px - a.x) ** 2 + (py - a.y) ** 2 + (pz - a.z) ** 2)
    d2 = np.sqrt((px - n.x) ** 2 + (py - n.y) ** 2 + (pz - n.z) ** 2)
    zones = (d1 + d2 - d) / (lam / 2)
    along = 1 / (1 + 0.22 * zones)
    ga, gn = gain_at(a, px, py), gain_at(n, px, py)
    ends = 0.55 * np.maximum(np.exp(-d1 / 0.7) * ga, np.exp(-d2 / 0.7) * gn)
    return np.minimum(1.0, along * along * ga * gn + ends)


def _blur(img, r):
    """Box blur of r cells, twice (about a Gaussian)."""
    if r < 1:
        return img
    k = np.ones(2 * r + 1) / (2 * r + 1)
    for _ in range(2):
        img = np.apply_along_axis(lambda v: np.convolve(np.pad(v, r, mode='edge'), k, 'valid'), 1, img)
        img = np.apply_along_axis(lambda v: np.convolve(np.pad(v, r, mode='edge'), k, 'valid'), 0, img)
    return img


class Person:
    def __init__(self, pid, t):
        self.id, self.post, self.est, self.trail, self.seen = pid, None, None, [], t
        self.speed, self.heading = 0.0, None

    def view(self):
        e = self.est or {}
        return dict(id=self.id, x=round(e.get('x', 0.0), 2), y=round(e.get('y', 0.0), 2), r=round(e.get('r', 0.0), 2),
                    speed=round(self.speed, 2), heading=None if self.heading is None else round(self.heading, 2))


class Tracker:
    """step(links, t) twice a second; links: [dict(key, ends=[(End, End), ...], level)]
    where level is the link's strength (0 quiet, 1 = its threshold) or None (no verdict)."""

    def __init__(self, lam=0.125):
        self.lam = lam
        self.base = {}                    # key -> idle level (the low envelope)
        self.base_at = None
        self.people = []
        self.next_id = 0
        self.at = None
        self.grid = None
        self.near = None                  # dict(key, t): the end the motion is at

    def excess(self, key, st, t):
        b = self.base.get(key)
        if b is None or st < b:
            b = st
        else:
            b += (st - b) * min(1.0, (t - (self.base_at or t)) / 60.0)
        self.base[key] = b
        return max(0.0, st - b) / max(0.3, 1 - b)

    def step(self, links, t):
        if self.at is not None and t - self.at < 0.5:
            return False
        dt = min(5.0, t - self.at) if self.at is not None else 0.5
        self.at = t
        obs = []
        for ln in links:
            if ln.get('level') is None:
                continue
            obs.append(dict(a=self.excess(ln['key'], ln['level'], t), ends=ln['ends'], key=ln['key'],
                            end_keys=ln.get('end_keys', ())))
        self.base_at = t
        self._near(obs, t)
        self.people = [p for p in self.people if t - p.seen < KEEP_S]
        if len(obs) < 2 or not any(o['a'] >= 0.6 for o in obs):
            return True
        xs = [e.x for o in obs for pair in o['ends'] for e in pair] + [0.0]
        ys = [e.y for o in obs for pair in o['ends'] for e in pair] + [0.0]
        x0, y0 = math.floor(min(xs) - 2), math.floor(min(ys) - 2)
        x1, y1 = math.ceil(max(xs) + 2), math.ceil(max(ys) + 2)
        cs = CELL
        gw, gh = math.ceil((x1 - x0) / cs), math.ceil((y1 - y0) / cs)
        while gw * gh > MAX_CELLS:
            cs *= 1.5
            gw, gh = math.ceil((x1 - x0) / cs), math.ceil((y1 - y0) / cs)
        g = (x0, y0, gw, gh, cs)
        if self.grid != g:
            self.grid = g
            for p in self.people:
                p.post = None
        px = x0 + (np.arange(gw) + 0.5) * cs
        py = y0 + (np.arange(gh) + 0.5) * cs
        PX, PY = np.meshgrid(px, py)                       # (gh, gw)
        S = np.stack([np.mean([sensitivity(PX, PY, a, n, self.lam) for a, n in o['ends']], axis=0) for o in obs])
        S = S.reshape(len(obs), -1)                         # (K, N)
        w = np.array([1 / (SIG0 + SIG1 * o['a']) ** 2 for o in obs])
        act = np.array([o['a'] for o in obs])

        def fit_one(act):
            num = G0 * WG + (w * act) @ S
            den = WG + (w[:, None] * S * S).sum(0)
            gg = np.maximum(0, num / den)
            R = (gg - G0) ** 2 * WG + (w[:, None] * (act[:, None] - gg[None, :] * S) ** 2).sum(0)
            return R, gg

        found = []
        for p in range(MAX_PEOPLE):
            if not (act >= 0.6).any():
                break
            R, Gc = fit_one(act)
            best = int(np.argmin(R))
            if p > 0 and (act.max() < MULTI_LEFT or (w * act * act).sum() - R[best] < MULTI_GAIN):
                break
            bx, by = x0 + (best % gw + 0.5) * cs, y0 + (best // gw + 0.5) * cs
            if any(math.hypot(f['x'] - bx, f['y'] - by) < MULTI_SEP for f in found):
                break
            found.append(dict(R=R, Rmin=float(R[best]), x=bx, y=by))
            act = np.maximum(0, act - Gc[best] * S[:, best])
        used = set()
        for f in found:
            pk = int(np.argmin(f['R']))
            fx, fy = x0 + (pk % gw + 0.5) * cs, y0 + (pk // gw + 0.5) * cs
            bi, bd = -1, 1e9
            for i, tp in enumerate(self.people):
                if i in used or not tp.est:
                    continue
                dd = math.hypot(tp.est['x'] - fx, tp.est['y'] - fy)
                if dd < bd:
                    bi, bd = i, dd
            if bi < 0 or (bd > 3 and len(found) > len(self.people)):
                if len(self.people) >= MAX_PEOPLE:
                    continue
                self.next_id += 1
                self.people.append(Person(self.next_id, t))
                bi = len(self.people) - 1
            used.add(bi)
            self._update(self.people[bi], f, t, dt, gw, gh, cs, x0, y0)
        self.people = [p for i, p in enumerate(self.people) if i in used or t - p.seen < 1.0]
        self.people.sort(key=lambda p: p.id)
        return True

    def _update(self, p, f, t, dt, gw, gh, cs, x0, y0):
        like = np.exp(-(f['R'] - f['Rmin']) / 2).reshape(gh, gw)
        if p.post is not None and p.post.shape == like.shape:
            prior = _blur(p.post, max(1, round(1.0 * dt / cs)))
            post = like * (0.85 * prior + 0.15 / like.size)
        else:
            post = like
        post = post / post.sum()
        sm = _blur(post, max(1, round(0.5 / cs)))
        bj, bi = np.unravel_index(int(np.argmax(sm)), sm.shape)
        sx, sy = x0 + (bi + 0.5) * cs, y0 + (bj + 0.5) * cs
        PX, PY = np.meshgrid(x0 + (np.arange(gw) + 0.5) * cs, y0 + (np.arange(gh) + 0.5) * cs)
        dist = np.hypot(PX - sx, PY - sy).ravel()
        pp = post.ravel()
        order = np.argsort(dist)
        cum = np.cumsum(pp[order])
        r68 = float(dist[order][min(len(cum) - 1, int(np.searchsorted(cum, 0.68)))])
        p.post = post
        p.est = dict(x=sx, y=sy, r=math.hypot(max(cs, r68), MODEL_ERR))
        p.seen = t
        p.trail.append((sx, sy, t))
        p.trail = [q for q in p.trail if t - q[2] <= 12]
        back = next((q for q in p.trail if t - q[2] <= 2.2), None)
        if back and t - back[2] >= 0.9:
            dx, dy = sx - back[0], sy - back[1]
            sp = math.hypot(dx, dy) / (t - back[2])
            p.speed = 0.6 * p.speed + 0.4 * min(sp, 4.0)
            if math.hypot(dx, dy) > 0.4:
                p.heading = math.atan2(dy, dx)

    def _near(self, obs, t):
        """map.js nearEnd(): the end whose links react much more than the rest."""
        ends = {}
        for o in obs:
            for k in o['end_keys']:
                e = ends.setdefault(k, [0.0, 0, 0])
                e[0] += o['a']
                e[1] += 1
                e[2] += o['a'] >= 0.6
        total = sum(o['a'] for o in obs)
        best = None
        for k, (sm, n, hot) in ends.items():
            if n < 2:
                continue
            inside = sm / n
            rest = (total - sm) / (len(obs) - n) if len(obs) > n else 0.0
            if inside < 0.8 or hot / n < 0.67 or rest > 0.5 * inside:
                continue
            if best is None or inside - rest > best[1]:
                best = (k, inside - rest)
        if best:
            self.near = dict(key=best[0], t=t)
        elif self.near and t - self.near['t'] > 2.5:
            self.near = None


def in_zone(z, x, y):
    """A zone: a rectangle {x, y, w, h} (metres, the Map's layout)."""
    return z['x'] <= x <= z['x'] + z['w'] and z['y'] <= y <= z['y'] + z['h']


def locate_once(links, lam=0.125, cs=0.5):
    """One person's most likely spot from the links' levels at one moment (no
    filter, no idle levels): links = [(ends, level)], ends = [(End, End), ...].
    Returns (x, y) or None. Used to replay the walk check's taps."""
    links = [(e, a) for e, a in links if a is not None and e]
    if len(links) < 2 or max(a for _, a in links) < 0.3:
        return None
    xs = [p.x for e, _ in links for pair in e for p in pair] + [0.0]
    ys = [p.y for e, _ in links for pair in e for p in pair] + [0.0]
    x0, y0 = math.floor(min(xs) - 2), math.floor(min(ys) - 2)
    gw, gh = math.ceil((max(xs) + 2 - x0) / cs), math.ceil((max(ys) + 2 - y0) / cs)
    while gw * gh > MAX_CELLS:
        cs *= 1.5
        gw, gh = math.ceil((max(xs) + 2 - x0) / cs), math.ceil((max(ys) + 2 - y0) / cs)
    PX, PY = np.meshgrid(x0 + (np.arange(gw) + 0.5) * cs, y0 + (np.arange(gh) + 0.5) * cs)
    S = np.stack([np.mean([sensitivity(PX, PY, a, n, lam) for a, n in e], axis=0).ravel() for e, _ in links])
    act = np.array([a for _, a in links])
    w = 1 / (SIG0 + SIG1 * act) ** 2
    num = G0 * WG + (w * act) @ S
    den = WG + (w[:, None] * S * S).sum(0)
    gg = np.maximum(0, num / den)
    R = (gg - G0) ** 2 * WG + (w[:, None] * (act[:, None] - gg[None, :] * S) ** 2).sum(0)
    c = int(np.argmin(R))
    return x0 + (c % gw + 0.5) * cs, y0 + (c // gw + 0.5) * cs
