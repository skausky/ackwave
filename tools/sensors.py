"""Each sensor's story: what it saw, from when to when, and how sure it is.

A sensor is a board (the hub or a satellite). Every half second the dashboard
gives each one ONE state (web.py _board_states: its own verdict, its wires,
its learned model, its breathing reading). This turns that stream into
periods a person can read and trust:

  moving       something moves on this board's links
  still        no movement, but someone seems to be there (learned model / breathing)
  empty        no movement, and the learned model says nobody is there
  quiet        no movement; whether someone is there is unknown (no model)
  weak         the board's router link is too weak to judge (few answers, low signal)
  calibrating  measuring its quiet level
  gone         no report reaches the hub

Each period has a confidence (0-100 %) and a grade (sure / likely / possible),
with the evidence in words. The confidence is honest about what it rests on:
movement from the board's own verdict plus the links and the model that agree;
still / empty only ever from the learned model or breathing (a board can't
tell a sleeper from an empty room on movement alone).

Changes are steadied so the log doesn't flap: movement starts at once and
ends after END_S without it (one walk is one period); every other state must
hold SETTLE_S before the log switches to it. Closed periods are kept in memory
and appended to sensors-YYYY-MM-DD.jsonl (private, next to the other logs).
"""
import json
import os
import time
from collections import deque

STATES = ('moving', 'still', 'empty', 'quiet', 'weak', 'calibrating', 'gone')
WORD = dict(moving='moving', still='someone still', empty='empty', quiet='quiet', weak='weak link',
            calibrating='calibrating', gone='not heard')
END_S = 3.0          # movement: gaps shorter than this are the same period
SETTLE_S = 20.0      # any other state: must last this long before the log takes it
KEEP = 600           # closed periods kept in memory


def _clip(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def grade(c):
    return 'sure' if c >= 0.8 else 'likely' if c >= 0.55 else 'possible'


def judge(b, moving_for=0.0):
    """(state, confidence 0..1, evidence in words) for one board's one state (web.py _board_states)."""
    st = b.get('state')
    why = []
    p = b.get('p') if b.get('model') else None            # learned [empty, still, moving] %
    level = float(b.get('level') or 0.0)                   # 1.0 = at its motion threshold
    weak = bool(b.get('weak'))
    if st == 'gone':
        return 'gone', 0.95, ['no report reaches the hub']
    if st == 'calibrating':
        return 'calibrating', 0.9, ['measuring its quiet level']
    if st == 'moving':
        miss = 1.0
        if b.get('own'):
            e = 0.55 + 0.35 * _clip((level - 1.0) / 1.0)
            miss *= 1 - e
            why.append(f'its own verdict (x{level:.1f} of its threshold)' if level else 'its own verdict')
        by = b.get('by') or []
        if by:
            miss *= 0.65 ** min(len(by), 3)
            n = len(b.get('wires') or [])
            why.append(f"{len(by)} of {n} links: {', '.join(by[:3])}" if n else f"links: {', '.join(by[:3])}")
        if p:
            miss *= 1 - 0.7 * p[2] / 100
            why.append(f'learned model {p[2]}% moving')
        c = 1 - miss
        if moving_for < 2.0:
            c *= 0.85                                       # a moment only: could be a blip
        if weak:
            c *= 0.6
            why.append('weak router link')
        if not b.get('own') and len(by) == 1 and not (p and p[2] >= 50):
            c = min(c, 0.5)                                 # one link alone, nothing else agrees
            why.append('one link alone')
        return 'moving', _clip(c, 0.05, 0.97), why
    if weak:
        return 'weak', 0.8, [f"router link weak ({b.get('acks', 0)} answers/s, {b.get('router_rssi', 0)} dBm)"]
    br = b.get('breathing')
    if st in ('still', 'empty') or p or br == 'clear':
        pe, ps = (p[0], p[1]) if p else (0, 0)
        if br == 'clear':
            return 'still', 0.8, ['breathing seen on its router link'] + ([f'learned model {ps}% still'] if p else [])
        if p and ps >= pe and ps >= 50:
            return 'still', _clip(ps / 100 * 0.9), [f'learned model {ps}% still (a learned guess)']
        if p and pe > ps and pe >= 50:
            return 'empty', _clip(pe / 100 * 0.9), [f'learned model {pe}% empty (a learned guess)']
    c = _clip(1.0 - 0.6 * level, 0.3, 0.95)                 # how surely nothing moves on it
    return 'quiet', c, [f'no movement (x{level:.1f} of its threshold)' + ('' if p else '; no learned model: someone still or nobody, unknown')]


def _dur(s):
    s = int(round(s))
    return f'{s} s' if s < 60 else f'{s // 60} min {s % 60:02d} s' if s < 3600 else f'{s // 3600} h {s % 3600 // 60:02d} min'


def _clock(t):
    return time.strftime('%H:%M:%S', time.localtime(t))


class SensorLog:
    def __init__(self, sessions_dir=None):
        self.dir = sessions_dir
        self.cur = {}                     # key -> the open period
        self.pend = {}                    # key -> (state, since): a change not yet steady
        self.log = deque(maxlen=KEEP)     # closed periods, oldest first

    def update(self, bstates, now):
        """Feed every board's state; returns [(kind, period)] for periods that opened ('open') or closed ('close')."""
        out = []
        for k, b in bstates.items():
            cur = self.cur.get(k)
            mv_for = now - cur['t0'] if cur and cur['state'] == 'moving' else 0.0
            st, c, why = judge(b, mv_for)
            if cur is None:
                self.cur[k] = cur = self._open(k, b, st, c, why, now)
                out.append(('open', dict(cur)))
                continue
            if st == cur['state']:
                self.pend.pop(k, None)
                cur['t_last'] = now
                self._feed(cur, c, why, b)
                continue
            # a change: movement starts at once; other states must settle (moving ends after END_S)
            ps, since = self.pend.get(k, (None, now))
            if ps != st:
                self.pend[k], since = (st, now), now
            if cur['state'] == 'moving':
                hold = END_S                               # one walk is one period
            elif st in ('moving', 'gone'):
                hold = 0.0                                 # said at once
            else:
                hold = SETTLE_S                            # still / empty / quiet: must hold
            if now - since < hold:
                continue
            end = cur['t_last'] if cur['state'] == 'moving' else since   # movement ends at its last report
            out.append(('close', self._close(k, max(end, cur['t0']))))
            self.pend.pop(k, None)
            self.cur[k] = cur = self._open(k, b, st, c, why, since)
            out.append(('open', dict(cur)))
        return out

    def _open(self, k, b, st, c, why, t0):
        return dict(key=k, name=b.get('name', k), state=st, t0=round(t0, 2), t_last=t0, conf=c, n=1,
                    sum=c, peak=c, why=list(why), by=sorted(set(b.get('by') or [])) if st == 'moving' else [])

    @staticmethod
    def _feed(cur, c, why, b):
        cur['n'] += 1
        cur['sum'] += c
        cur['conf'] = c
        if c >= cur['peak']:
            cur['peak'], cur['why'] = c, list(why)
        if cur['state'] == 'moving':
            cur['by'] = sorted(set(cur['by']) | set(b.get('by') or []))

    def _close(self, k, t1):
        cur = self.cur.pop(k)
        e = dict(key=k, name=cur['name'], state=cur['state'], t0=cur['t0'], t1=round(t1, 2),
                 conf=round(100 * cur['peak'] if cur['state'] == 'moving' else 100 * cur['sum'] / cur['n']),
                 why=cur['why'], by=cur['by'])
        e['grade'] = grade(e['conf'] / 100)
        self.log.append(e)
        self._write(e)
        return e

    def _write(self, e):
        if not self.dir:
            return
        try:
            p = os.path.join(self.dir, time.strftime('sensors-%Y-%m-%d.jsonl', time.localtime(e['t0'])))
            with open(p, 'a') as f:
                f.write(json.dumps(e, separators=(',', ':')) + '\n')
        except OSError:
            pass

    def view(self, now, n=80):
        """For the page: each sensor's open period, and the latest closed ones (newest first)."""
        live = []
        for k, cur in sorted(self.cur.items()):
            c = cur['peak'] if cur['state'] == 'moving' else cur['conf']   # a movement: its best evidence so far
            live.append(dict(key=k, name=cur['name'], state=cur['state'], word=WORD[cur['state']], since=cur['t0'],
                             conf=round(100 * c), grade=grade(c), why=cur['why'], by=cur['by'],
                             pending=self.pend.get(k, (None,))[0]))
        return dict(now=live, log=[dict(e, word=WORD[e['state']]) for e in list(self.log)[-n:]][::-1])

    def history(self, hours=24.0, now=None):
        """Closed periods from the files of the last `hours` (newest first), for the Log page."""
        now = now or time.time()
        t0 = now - hours * 3600
        out = []
        if self.dir:
            days = {time.strftime('%Y-%m-%d', time.localtime(t)) for t in (t0, now, (t0 + now) / 2)}
            for d in sorted(days):
                p = os.path.join(self.dir, f'sensors-{d}.jsonl')
                try:
                    with open(p) as f:
                        for line in f:
                            try:
                                e = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(e, dict) and e.get('t1', 0) >= t0 and e.get('state') in WORD:
                                out.append(dict(e, word=WORD[e['state']]))
                except OSError:
                    pass
        else:
            out = [dict(e, word=WORD[e['state']]) for e in self.log if e['t1'] >= t0]
        return out[::-1]

    @staticmethod
    def text(e):
        """One line for the live feed and the add-on's log."""
        if 't1' in e:
            return (f"{e['name']}: {WORD[e['state']]} {_clock(e['t0'])} → {_clock(e['t1'])} "
                    f"({_dur(e['t1'] - e['t0'])}, {e['grade']} {e['conf']}%)"
                    + (f" · on {', '.join(e['by'][:3])}" if e.get('by') else ''))
        return f"{e['name']}: {WORD[e['state']]} since {_clock(e['t0'])} ({grade(e['conf'])} {round(100 * e['conf'])}%)"
