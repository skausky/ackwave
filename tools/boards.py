"""Several boards in one dashboard (docs/redesign/multi-board.md, step A).

The primary board (the hub, chosen by its saved MAC) is the dashboard's
State as always. Every other board on USB gets an OtherBoard: its own
gui.State (its own detector, links, devices), its own clock mapping, its
own key handling; its verdict goes on the timeline as 'sat:<mac>', the same
lane its sealed reports would use. In step A other boards are only watched:
nothing is sent to them except their key.

Lines from a stream that carries several boards (fake_csi --boards, later
the hub) are tagged '@<mac>,<line>'; untagged lines belong to the board on
that port.
"""
import math
import re
import time
from collections import deque

from devices import Devices
from keys import Pairing

TAG_RE = re.compile(r'@([0-9a-f]{2}(?::[0-9a-f]{2}){5}),')
TS_KINDS = ('CSI,', 'STATE,', 'CALIB,', 'STATS,', 'CHANNEL,')   # second field = board µs


def split_tag(line):
    """('aa:..', rest) for a tagged line, else (None, line)."""
    m = TAG_RE.match(line)
    return (m.group(1), line[m.end():]) if m else (None, line)


def board_ts(line):
    """The board's own time (µs) of a line that carries one, else None."""
    if not line.startswith(TS_KINDS):
        return None
    try:
        return int(line.split(',', 2)[1])
    except (IndexError, ValueError):
        return None


class ClockMap:
    """Board time -> PC time. A line can only arrive after the board wrote
    it, so the smallest (PC time - board time) seen is the best offset. It is
    taken over a sliding window (WINDOW_S, in BUCKET_S buckets), not since
    the start, so it follows a crystal that runs slow or fast (up to ~20 ppm,
    ~70 ms an hour). A board clock going back means it restarted: start over."""
    WINDOW_S = 600.0
    BUCKET_S = 10.0

    def __init__(self):
        self.buckets = deque()           # (bucket index, smallest offset in it)
        self.last_ts = None

    def feed(self, ts_us, pc):
        if self.last_ts is not None and ts_us < self.last_ts - 1_000_000:
            self.buckets.clear()
        self.last_ts = ts_us
        off = pc - ts_us / 1e6
        b = int(pc // self.BUCKET_S)
        if self.buckets and self.buckets[-1][0] == b:
            if off < self.buckets[-1][1]:
                self.buckets[-1] = (b, off)
        else:
            self.buckets.append((b, off))
        while self.buckets and self.buckets[0][0] < b - self.WINDOW_S / self.BUCKET_S:
            self.buckets.popleft()

    def offset(self):
        return min(o for _b, o in self.buckets) if self.buckets else None

    def wall(self, ts_us, default=None):
        off = self.offset()
        return ts_us / 1e6 + off if off is not None else default


class OtherBoard:
    """A board on USB that isn't the primary one: watched, not driven."""

    def __init__(self, mac, state, send=None, secret=None, period=None):
        self.mac = mac
        self.s = state
        self.s.devices = Devices(mode='manual')      # its own list; we send it nothing
        self.send = send
        self.clock = ClockMap()
        self.pair = Pairing(secret)
        self.pair.me = mac
        self.period = period                         # (key, on, wall, name) -> files it
        self.motion = False
        self.since = None
        self.last = None                             # PC time of its last line

    def feed(self, line, pc=None):
        pc = time.time() if pc is None else pc
        self.last = pc
        ts = board_ts(line)
        if ts is not None:
            self.clock.feed(ts, pc)
        self.pair.feed(line)
        self.s.feed(line)
        self.s.outbox.clear()                        # step A: watched only
        if line.startswith('STATE,') and self.s.board_motion is not None:
            on = bool(self.s.board_motion)
            if on != self.motion or self.since is None:
                w = self.clock.wall(ts, pc) if ts is not None else pc
                self.motion, self.since = on, w
                if self.period:
                    self.period('sat:' + self.mac, on, w)

    def report(self):
        """The same fields a sealed report gives, from its own lines."""
        s = self.s
        det = s.det
        f = det.floor
        ks = kl = 0.0
        if f > 0 and s.now:
            ms, ml = det.max_scores(int(s.now * 1e6))
            if ms is not None:
                ks, kl = ms / (det.c['on_short'] * f), ml / (det.c['on_long'] * f)
        st = s.stats or {}
        g = s.devices.gate.get(0)
        return dict(mac=self.mac, motion=bool(self.motion), calibrated=f > 0,
                    weak=bool(g and g.get('state') in ('weak', 'silent')),
                    k_short=round(ks, 2), k_long=round(kl, 2), acks=round(s.rate((0, 1))) if s.now else 0,
                    router_rssi=(g or {}).get('rssi', 0) or 0, links=len(s.devices.poking),
                    up_s=int((st.get('ts') or 0) / 1e6), rssi=0, seq=0,
                    at=self.last or 0, since=self.since or self.last or 0, via='usb',
                    hub_ch=self.pair.hub_ch, reports=self.pair.reports, answered=self.pair.answered,
                    bssid=(s.nodes.get(0) or '').lower() or None, ch=s.channel[0] if s.channel else None)


# ---------- The house verdict (docs/redesign/multi-board.md, step C) ----------
HOUSE_RULES = ('any', 'confirm', 'two')
HOUSE_TEXT = {'any': 'any board', 'confirm': 'any board; a blip needs a second board',
              'two': 'two boards must agree'}


class House:
    """Combines the boards' own verdicts. Sources are 'board' (the hub) and
    other boards' MACs; each says moving or not, every update.

      any      motion when any board sees it
      confirm  (default) motion lasting BRIEF_S counts from one board alone;
               a shorter one only if another board also saw motion within TOL_S
      two      at least two boards seeing motion within TOL_S of each other

    With a single board every rule gives exactly its own verdict."""
    BRIEF_S = 2.0
    TOL_S = 1.5

    def __init__(self):
        self.since = {}                  # source -> when its current motion started
        self.ended = {}                  # source -> when its last motion ended

    def update(self, states, now):
        for src, on in states.items():
            if on and src not in self.since:
                self.since[src] = now
            elif not on and src in self.since:
                self.ended[src] = now
                del self.since[src]
        for src in [s for s in self.since if s not in states]:   # gone: its motion ended
            self.ended[src] = now
            del self.since[src]

    def _near(self, src, now):
        """Another source moving now, or whose motion ended within TOL_S."""
        return any(o != src for o in self.since) or any(
            o != src and now - t <= self.TOL_S for o, t in self.ended.items())

    def verdict(self, rule, now, n_sources):
        """(motion, the sources that count)."""
        moving = sorted(self.since)
        if n_sources <= 1 or rule == 'any':
            return bool(moving), moving
        if rule == 'two':
            ok = [s for s in moving if self._near(s, now)]
            return bool(ok), ok
        ok = [s for s in moving if now - self.since[s] >= self.BRIEF_S or self._near(s, now)]
        return bool(ok), ok


# ---------- Direction of travel ----------
class Travel:
    """Someone walking from board to board: one board's motion starts, then
    another's STEP_MIN_S..STEP_MAX_S later. Starts closer together than
    STEP_MIN_S are one spot both boards cover (not a walk); a gap over
    STEP_MAX_S, or END_S without a new step, starts a new path.

    start(key, t) for every motion start; returns the path [(t, key), ...]
    when it just grew to 2+ boards, else None. n numbers the paths, so the
    caller can update one entry while a path grows."""
    STEP_MIN_S = 0.7
    STEP_MAX_S = 8.0
    END_S = 12.0

    def __init__(self):
        self.path = []
        self.n = 0

    def _new(self, key, t):
        self.path = [(t, key)]
        self.n += 1

    def start(self, key, t):
        p = self.path
        if not p or t - p[-1][0] > self.END_S:
            self._new(key, t)
            return None
        last_t, last_k = p[-1]
        if key == last_k:                # the same board again: still there
            return None
        dt = t - last_t
        if dt < self.STEP_MIN_S:         # both at once: one spot
            return None
        if dt > self.STEP_MAX_S:
            self._new(key, t)
            return None
        p.append((t, key))
        return p


# ---------- Other boards' links (SATL lines, step 3) ----------
class SatLink:
    """One link of another board, judged from its scores (x the board's quiet
    level). A device link is noisier than the router's, so each link keeps its
    own quiet level: a slow average of its scores while it isn't moving. It
    moves when the short score passes ON_SHORT x that, or the long one ON_LONG x."""
    ON_SHORT, ON_LONG, OFF = 2.8, 1.4, 0.75
    TAU_S = 120.0                         # the quiet level follows over ~2 min

    def __init__(self, flags):
        self.flags = flags
        self.q_s = self.q_l = None        # its quiet levels
        self.moving = False
        self.moving_since = None
        self.ks = self.kl = 0.0
        self.at = self.t_last = None

    CONFIRM_S = 1.0

    @property
    def confirmed(self):
        """Moving, and not a lone spike: its slow (4 s) window agrees, or it has
        moved for CONFIRM_S (two reports in a row)."""
        if not self.moving or self.q_l is None:
            return False
        return self.kl >= self.ON_LONG * self.OFF * self.q_l or \
            (self.moving_since is not None and self.at - self.moving_since >= self.CONFIRM_S)

    def feed(self, ks, kl, now):
        self.ks, self.kl, self.at = ks, kl, now
        if self.q_s is None:
            self.q_s, self.q_l = max(ks, 0.5), max(kl, 0.5)
            self.t_last = now
            return self.moving
        dt = max(0.0, now - self.t_last)
        self.t_last = now
        rs, rl = ks / self.q_s, kl / self.q_l
        if self.moving:
            self.moving = rs > self.ON_SHORT * self.OFF or rl > self.ON_LONG * self.OFF
        else:
            self.moving = rs >= self.ON_SHORT or rl >= self.ON_LONG
        if self.moving and self.moving_since is None:
            self.moving_since = now
        elif not self.moving:
            self.moving_since = None
        if not self.moving:               # learn the quiet level only while quiet
            a = 1 - math.exp(-dt / self.TAU_S)
            self.q_s += a * (max(ks, 0.3) - self.q_s)
            self.q_l += a * (max(kl, 0.3) - self.q_l)
        return self.moving

    def level(self):
        if self.q_s is None:
            return 0.0
        return max(self.ks / (self.ON_SHORT * self.q_s), self.kl / (self.ON_LONG * self.q_l))
