"""Routers placed by themselves (the Pi, every 15 min): the Map page's "Fit
routers automatically", run without a page open.

Two kinds of evidence, combined:
  signal strength: how loudly each board hears the router (dBm). A spot is
    scored by how well "the router is here" explains every board's level
    (log-distance fading, a floor's loss, the board's facing; the router's own
    power is unknown and solved for). This alone finds the right part of the
    watched area, even for a router left on automatic.
  everyday movement: (below)

A router in the wrong spot makes everyday movement fit badly: at moments
when people move, no single spot explains every link's reaction. So each
router that may move (roughly placed or automatic, inside the watched area)
is tried at spots within its circle, and the spot where those moments fit
best (the smallest total misfit of one person, as track.py measures it) is
kept. Only links reaching into the watched area count, and people are only
ever placed inside it. A router you dragged lately is left alone.
"""
import math

import numpy as np

from track import G0, SIG0, SIG1, WG, End, in_zone, sensitivity

CELL = 1.0                       # the people grid (m): coarse is plenty for a fit
STEP = 0.75                      # router candidates (m)
MIN_GAIN = 0.10                  # only a fit at least 10 % better moves anything
FADE_N, FLOOR_DB, SIGMA_DB = 3.0, 12.0, 6.0   # indoor fading, a floor's loss, a level's scatter
W_RSSI = 0.5                     # signal strength's weight against movement in the score


def _grid(area):
    xs = np.arange(area['x'] + CELL / 2, area['x'] + area['w'], CELL)
    ys = np.arange(area['y'] + CELL / 2, area['y'] + area['h'], CELL)
    return np.meshgrid(xs, ys)


def misfit(snaps, links, PX, PY, lam=0.125):
    """Mean over the moments of the best one-person misfit on the grid.
    links: {key: [(End, End), ...]} (end samples); snaps: [{key: level}]."""
    keys = [k for k in links if any(k in s for s in snaps)]
    if len(keys) < 2:
        return None
    S = np.stack([np.mean([sensitivity(PX, PY, a, n, lam) for a, n in links[k]], axis=0).ravel() for k in keys])
    total, n = 0.0, 0
    for s in snaps:
        idx = [i for i, k in enumerate(keys) if k in s]
        if len(idx) < 2:
            continue
        act = np.array([s[keys[i]] for i in idx])
        if act.max() < 0.3:
            continue
        Sk = S[idx]
        w = 1 / (SIG0 + SIG1 * act) ** 2
        num = G0 * WG + (w * act) @ Sk
        den = WG + (w[:, None] * Sk * Sk).sum(0)
        g = np.maximum(0, num / den)
        R = (g - G0) ** 2 * WG + (w[:, None] * (act[:, None] - g[None, :] * Sk) ** 2).sum(0)
        total += float(R.min()) / len(idx)
        n += 1
    return total / n if n else None


def rssi_misfit(spot, heard, nodes, floor_h=3.0, hub_floor=1, router_floor=None):
    """How badly 'the router is at spot' explains the boards' levels: the mean
    squared residual / SIGMA_DB^2 (its power solved), or None with under 2 boards."""
    from track import gain_at
    rows = []
    for by, dbm in heard:
        if by == 'hub':
            bx, by_, bf, face = 0.0, 0.0, hub_floor, layout_hub_facing[0]
        else:
            n = nodes.get(by)
            if not n:
                continue
            bx, by_, bf, face = n['x'], n['y'], n.get('floor') or hub_floor, n.get('facing')
        rf = router_floor or hub_floor
        d = max(0.5, math.dist((bx, by_, (bf - 1) * floor_h), (spot[0], spot[1], (rf - 1) * floor_h)))
        g = 20 * math.log10(float(gain_at(End(bx, by_, 0, face), np.array([spot[0]]), np.array([spot[1]]))[0]))
        rows.append((dbm, -10 * FADE_N * math.log10(d) - FLOOR_DB * abs(bf - rf) + g))
    if len(rows) < 2:
        return None
    P = sum(r - l for r, l in rows) / len(rows)            # the router's power at 1 m, solved
    return sum((r - (P + l)) ** 2 for r, l in rows) / len(rows) / SIGMA_DB ** 2


layout_hub_facing = [None]


STABLE_M = 1.5                   # both halves of the data must agree this closely, or nothing moves


def fit(layout, snaps, ends_of, skip=(), hearings=None):
    """New spots for the routers that may move, but only where the evidence is
    steady: fitted on all moments and again on each half; a router moves only if
    both halves put it within STABLE_M of the full answer (a lopsided layout,
    every line from one side, can't tell spots apart: then it stays)."""
    moved, note = _fit(layout, snaps, ends_of, skip, hearings)
    if not moved or len(snaps) < 30:
        return moved, note
    halves = [_fit(layout, snaps[i::2], ends_of, skip, hearings)[0] for i in (0, 1)]
    steady = {k: v for k, v in moved.items()
              if all(k in h and math.dist(h[k], v) <= STABLE_M for h in halves)}
    if not steady:
        return {}, 'the evidence is not steady enough to move anything (lines from one side only?)'
    return steady, note


def _fit(layout, snaps, ends_of, skip=(), hearings=None):
    """New spots for the routers that may move: {key: (x, y)} and a note, or ({}, why).
    ends_of(key, override) -> [End, ...] for a link end, override = {key: (x, y)}."""
    zones = layout.get('zones', [])
    area = next((z for z in zones if z.get('watch')), None)
    if area is None:
        return {}, 'no watched area'
    nodes = {n['key']: n for n in layout.get('nodes', [])}
    movable = [k for k, n in nodes.items() if k.startswith('r:') and not n.get('placed') and k not in skip
               and in_zone(area, *(n['hint'][:2] if n.get('hint') else (n['x'], n['y'])))]
    if not movable:
        return {}, 'no router to fit (set one to roughly here or automatic, inside the watched area)'
    snaps = [s['links'] for s in snaps if s.get('links')]
    if len(snaps) < 15:
        return {}, f'not enough movement yet ({len(snaps)} moments)'
    PX, PY = _grid(area)
    link_keys = sorted({k for s in snaps for k in s})

    def links_with(over):
        out = {}
        for lk in link_keys:
            a_key, n_key = lk.split('>', 1) if '>' in lk else ('hub', lk)
            a, n = ends_of(a_key, over), ends_of(n_key, over)
            if a and n and (in_zone(area, *_xy(nodes, a_key, over)) or in_zone(area, *_xy(nodes, n_key, over))):
                out[lk] = [(x, y) for x in a for y in n]
        return out

    layout_hub_facing[0] = layout.get('hub_facing')
    heard = hearings or {}
    fh, hf = layout.get('floor_h', 3.0), layout.get('hub_floor', 1)

    def score(over_):
        m = misfit(snaps, links_with(over_), PX, PY)
        if m is None:
            return None
        sc = m / max(base_m[0], 1e-9)
        for k in movable:
            sp = over_.get(k) or _xy(nodes, k, {})
            rr = rssi_misfit(sp, heard.get(k, []), nodes, fh, hf, nodes[k].get('floor'))
            if rr is not None:
                sc += W_RSSI * rr
        return sc

    over = {}
    base_m = [misfit(snaps, links_with(over), PX, PY)]
    if base_m[0] is None:
        return {}, 'too few links reach the watched area'
    before = score(over)
    best_total = before
    for _ in range(2):                                   # each router in turn, twice
        for k in movable:
            n = nodes[k]
            cx, cy, r = (n['hint'][0], n['hint'][1], n['hint'][2]) if n.get('hint') else (n['x'], n['y'], 8.0)
            best = (over.get(k, (cx, cy)), best_total)
            for dy in np.arange(-r, r + 1e-6, STEP):
                for dx in np.arange(-r, r + 1e-6, STEP):
                    if math.hypot(dx, dy) > r or not in_zone(area, cx + dx, cy + dy):
                        continue
                    trial = dict(over, **{k: (cx + dx, cy + dy)})
                    v = score(trial)
                    if v is not None and v < best[1]:
                        best = ((cx + dx, cy + dy), v)
            over[k] = best[0]
            best_total = best[1]
    if best_total > (1 - MIN_GAIN) * before:
        return {}, 'already fits well'
    return over, f"fits {round(100 * (1 - best_total / before))} % better"


def _xy(nodes, key, over):
    if key in over:
        return over[key]
    if key == 'hub':
        return (0.0, 0.0)
    n = nodes.get(key)
    if not n:
        return (1e9, 1e9)
    return (n['hint'][0], n['hint'][1]) if n.get('hint') else (n['x'], n['y'])


__all__ = ['fit', 'misfit', 'End']
