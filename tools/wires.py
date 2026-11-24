"""Where along the wires: which end a movement is near, with no map at all.

Every link is a wire between two ends: a board, a router, a device. A person
changes the wires they stand on, and the closer they are to an end, the more
of that end's wires they stand on (every wire fans out from it, so near the
end they all pass close by). So, for a wire that moves:

  near an end   most of that end's other wires react too
  between       both ends' other wires stay quiet
  can't tell    an end with no other wire says nothing (shown as such)

This only needs to know which wires share an end, never where anything is:
a router no one placed works exactly as well as one placed to the inch. The
more wires share an end (several boards poking the same router, two boards
poking the same device), the surer it is. A single wire's spike is never
called 'near' anything: an end is 'hot' only with 2 or more wires reacting.

Wires react a little apart in time (a person reaches one before the other),
so a wire counts as reacting for HOLD_S after it moved, or while its level is
near its threshold (REACT of it).
"""
import math

HOLD_S = 4.0               # a wire that moved counts as reacting this long after
REACT = 0.9                # or while its level is this close to its threshold (1.0 = threshold; a quiet
                           # wire sits near 0.7: its slow window's quiet level / its 1.4x threshold)
HOT_FRAC = 0.6             # an end is hot when this share of its live wires react (and at least 2)
SPAN = {                   # where on the wire (0 = end a, 1 = end b) each answer lights it
    'a': (0.0, 0.3), 'b': (0.7, 1.0), 'between': (0.25, 0.75),
    'a_or_between': (0.0, 0.75), 'b_or_between': (0.25, 1.0), 'both': (0.0, 1.0), 'unknown': (0.0, 1.0),
}


class Wires:
    def __init__(self):
        self.moved_at = {}             # wire key -> when it last moved

    def update(self, wires, ends, now):
        """wires: [dict(a, b, moving: bool|None (None = not answering), level)];
        ends: {key: dict(kind='board'|'router'|'device', name, presence=None|'empty'|'still'|'moving')}.
        Returns the view: each wire with its state and where on it, each end with
        whether it is hot, and what it means in words (most telling first)."""
        live = [w for w in wires if w['moving'] is not None]
        for w in live:
            if w['moving']:
                self.moved_at[(w['a'], w['b'])] = now
        for k in [k for k, t in self.moved_at.items() if now - t > 600]:
            del self.moved_at[k]

        def moved(w):
            t = self.moved_at.get((w['a'], w['b']))
            return bool(w['moving']) or (t is not None and now - t <= HOLD_S)

        def reacting(w):                 # for judging an end: a wire near its threshold counts too
            return moved(w) or (w.get('level') or 0) >= REACT

        at_end = {}
        for w in live:
            for e in (w['a'], w['b']):
                at_end.setdefault(e, []).append(w)
        hot = {}
        for e, ws in at_end.items():
            n, r = len(ws), sum(1 for w in ws if reacting(w))
            hot[e] = dict(n=n, react=r, hot=n >= 2 and r >= 2 and r >= HOT_FRAC * n and any(moved(w) for w in ws))

        def says(e, w):
            """Does end e tell anything about wire w? None: no other wire there."""
            others = [o for o in at_end.get(e, []) if o is not w]
            if not others:
                return None
            return hot[e]['hot']

        out = []
        for w in wires:
            st = 'off' if w['moving'] is None else 'moving' if w['moving'] else \
                'recent' if moved(w) else 'quiet'
            where = None
            if st in ('moving', 'recent'):
                ha, hb = says(w['a'], w), says(w['b'], w)
                where = ('both' if ha and hb else 'a' if ha else 'b' if hb else
                         'between' if ha is False and hb is False else
                         'a_or_between' if hb is False else 'b_or_between' if ha is False else 'unknown')
            t = self.moved_at.get((w['a'], w['b']))
            out.append(dict(a=w['a'], b=w['b'], state=st, level=round(w.get('level') or 0, 2), where=where,
                            span=SPAN.get(where), ago=round(now - t, 1) if t is not None else None))
        return dict(wires=out, ends={e: dict(ends.get(e, {}), **hot.get(e, dict(n=0, react=0, hot=False)))
                                     for e in set(ends) | set(at_end)},
                    words=self._words(out, ends, hot))

    @staticmethod
    def _words(out, ends, hot):
        """What it means, most telling first: hot ends, then lone wires between."""
        name = lambda e: (ends.get(e) or {}).get('name') or e
        kind = lambda e: (ends.get(e) or {}).get('kind') or 'device'
        lines, explained = [], set()
        for e, h in sorted(hot.items(), key=lambda kv: (-kv[1]['react'], kv[0])):
            if not h['hot']:
                continue
            side = 'by your board' if kind(e) == 'board' else 'over there'
            lines.append(dict(kind='near', end=e, side='yours' if kind(e) == 'board' else 'over',
                              text=f"Near {name(e)} ({side}): {h['react']} of its {h['n']} wires reacted"))
            explained |= {(w['a'], w['b']) for w in out if e in (w['a'], w['b'])}
        for w in out:
            if w['where'] is None or (w['a'], w['b']) in explained:
                continue
            a, b = name(w['a']), name(w['b'])
            text = {'between': f"Between {a} and {b}: only that wire, both ends quiet",
                    'a_or_between': f"Between {a} and {b}, or near {a}: {b}'s other wires are quiet",
                    'b_or_between': f"Between {a} and {b}, or near {b}: {a}'s other wires are quiet",
                    'unknown': f"Somewhere on {a} – {b} (no other wire shares its ends to tell where)",
                    'both': f"Near both {a} and {b}"}.get(w['where'])
            if text:
                both_boards = kind(w['a']) == 'board' and kind(w['b']) == 'board'   # a wire between two of your boards
                lines.append(dict(kind=w['where'], end=None, side='yours' if both_boards else 'over',
                                  text=text, a=w['a'], b=w['b']))
        return lines


def headline(view):
    """One sentence for Home: by your boards, over there, or quiet."""
    near = [l for l in view['words'] if l['kind'] == 'near']
    if any(l['side'] == 'yours' for l in near):
        return 'yours', 'Movement by your boards'
    if view['words']:
        return 'over', 'Movement away from your boards: by the routers or between'
    if any(w['state'] == 'off' for w in view['wires']) and not any(w['state'] != 'off' for w in view['wires']):
        return 'none', 'No wire is answering'
    return 'quiet', 'All wires quiet'


def level_of(ln):
    """A SatLink's level, or 0 when it has none yet."""
    try:
        v = ln.level()
        return v if math.isfinite(v) else 0.0
    except (AttributeError, ZeroDivisionError):
        return 0.0
