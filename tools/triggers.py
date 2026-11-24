"""Alert rules: what sets off the phone, the light and the boards' buzzer/LED.

One rule says which boards it watches, how many of them must see movement,
and where the movement must be, then what to do:

  boards   [] = any board, or a list ('hub', 'sat:<mac>')
  need     'any' | 'all' | a number (at least that many of them)
  where    'any'   movement anywhere on their wires
           'over'  away from the board: by the routers / devices, or between
           'yours' right by the board (most of its wires react together)
  notify / light / buzz: what it does

A board counts while it moves, and for TOL_S after (boards see the same person
a moment apart, so 'all 3' works when they fire 2 s apart). A rule stays on
for HOLD_S after its condition ends, so one walk is one alert, not ten.

Every rule is judged the same way for every board: there is no 'this only
reaches the hub'. The boards' moving / near-me states come from the dashboard
(board verdict + wires.py), one source for the page and the alerts alike.
"""
import re
import time

TOL_S = 3.0
HOLD_S = 2.0                # the light (off after) and the phone (quiet time) wait on top of this
MAX_RULES = 12
WHERE = ('any', 'over', 'yours')
KEY_RE = re.compile(r'^(hub|sat:[0-9a-f]{2}(:[0-9a-f]{2}){5})$')


def clean_rule(r, n=0):
    """A rule from the page, checked; raises ValueError."""
    if not isinstance(r, dict):
        raise ValueError('not a rule')
    boards = r.get('boards', [])
    if not isinstance(boards, list) or len(boards) > 17 or not all(isinstance(b, str) and KEY_RE.match(b.lower()) for b in boards):
        raise ValueError('bad boards')
    need = r.get('need', 'any')
    if need not in ('any', 'all') and not (isinstance(need, int) and not isinstance(need, bool) and 1 <= need <= 17):
        raise ValueError('bad need')
    where = r.get('where', 'any')
    if where not in WHERE:
        raise ValueError('bad where')
    name = r.get('name', '')
    if not isinstance(name, str) or len(name) > 40:
        raise ValueError('bad name')
    rid = r.get('id') or f'r{n}{int(time.time() * 1000) % 100000}'
    if not isinstance(rid, str) or not re.fullmatch(r'[a-z0-9]{1,16}', rid):
        raise ValueError('bad id')
    return dict(id=rid, name=name.strip() or 'Alert', boards=sorted({b.lower() for b in boards}), need=need,
                where=where, notify=bool(r.get('notify')), light=bool(r.get('light')), buzz=bool(r.get('buzz')),
                on=bool(r.get('on', True)))


def migrate(cfg, zones):
    """The first rule, from the old settings (house rule, boards left out,
    zones that notified or switched the light), so nothing changes by surprise.
    Phone and light are ticked: whether they are set up (Settings) decides, as
    before, so setting the light up later needs no second trip here."""
    off = set(cfg.get('house_off') or [])
    boards = [] if not off else sorted(({'hub'} | {'sat:' + m for m in cfg.get('paired') or []})
                                       - {('hub' if k == 'board' else 'sat:' + k) for k in off})
    over = any(z.get('watch') and (z.get('notify') or z.get('light')) for z in zones or [])
    return [clean_rule(dict(id='r1', name='Movement' + (' over there' if over else ''), boards=boards,
                            need=2 if cfg.get('house_rule') == 'two' else 'any', where='over' if over else 'any',
                            notify=True, light=True, buzz=True))]


class Triggers:
    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.seen = {}                 # (board, where) -> last time it counted
        self.state = {}                # rule id -> dict(on, since, until, boards)

    def update(self, boards, now):
        """boards: {key: dict(moving, near_me)} for every board heard now.
        Returns [(rule, started: bool)] for the rules that are on."""
        for k, b in boards.items():
            if b.get('moving'):
                self.seen[(k, 'any')] = now
                self.seen[(k, 'yours' if b.get('near_me') else 'over')] = now
        out = []
        for r in self.rules:
            st = self.state.setdefault(r['id'], dict(on=False, since=None, until=0.0, boards=[]))
            if not r['on']:
                st.update(on=False, boards=[])
                continue
            watch = r['boards'] or sorted(boards)
            live = [k for k in watch if k in boards]
            hit = [k for k in live if now - self.seen.get((k, r['where']), -1e9) <= TOL_S]
            need = len(live) if r['need'] == 'all' else 1 if r['need'] == 'any' else r['need']
            ok = bool(live) and len(hit) >= max(1, need)
            if r['need'] == 'all' and r['boards'] and len(live) < len(r['boards']):
                ok = False                     # 'all' of a board that isn't heard: never
            started = False
            if ok:
                if not st['on']:
                    st['since'], started = now, True
                st.update(on=True, until=now + HOLD_S, boards=hit)
            elif st['on'] and now >= st['until']:
                st.update(on=False, boards=[])
            if st['on']:
                out.append((r, started))
        return out

    def view(self, now):
        return {rid: dict(on=s['on'], since=s['since'], boards=s['boards']) for rid, s in self.state.items()}
