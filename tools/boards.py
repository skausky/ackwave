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
                    hub_ch=self.pair.hub_ch, reports=self.pair.reports, answered=self.pair.answered)


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
