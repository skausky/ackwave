#!/usr/bin/env python3
"""Web dashboard for the CSI motion sensor: the live view in a browser.

    python web.py /dev/ttyACM0                 # real board, http://localhost:8765
    python fake_csi.py --realtime --loop | python web.py -      # demo, no hardware

Same processing as gui.py (its State class), and with a board it logs to
sessions/ exactly like record.py. The server listens on 127.0.0.1 only, so
nothing else on your network can reach it; from a phone, use an SSH tunnel
(ssh -L 8765:localhost:8765 you@this-pc, then open http://localhost:8765).
--host 0.0.0.0 opens it to your LAN instead (anyone there can then see it and
send commands to the board).

Live data reaches the page as Server-Sent Events; commands come back as JSON
POSTs, which must carry an X-Ackwave header (a cross-site page can't add it
without a CORS preflight, which this server never grants) and a Host header
naming this server (no DNS rebinding).
"""
import argparse
import json
import zlib
import math
import os
import queue
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from collections import deque

from activity import LABELS, Activities, label_info
from analyze import MAX_APS, MotionDetector, open_port, parse_line
from breathing import Breathing
from timeline import Timeline
from keys import SECRET_RE, Pairing, new_secret, redact
from boards import HOUSE_RULES, HOUSE_TEXT, House, OtherBoard, SatLink, Travel, split_tag
from devices import MAX_DEVS, MODES, Devices, Oui, fmt_age
from stats import Sessions, summarize
from doppler import FREQS, LOW_HZ, Doppler
from light import Light, http_call
from notify import Notify, pushover_call
from track import End, Tracker, in_zone
from mlfeat import Shadow
import mltrain
import autofit
import linkpick
from gui import (BRIEF_C, BRIEF_S, CAL_C, COLORS, DIM, FG, LED_MODES, LED_PATTERNS, LONG_C, MOTION_C,
                 SCAN_C, STILL_C, SENS_RANGE, SOUND_MODES, WINDOWS, State, reader, thin)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8'),
          '/map.js': ('map.js', 'text/javascript; charset=utf-8')}
MAP_MAX = 256 * 1024                        # the Map page's saved layout (no pictures): bytes
MAP_NODES = 64
PLAN_MAX = 12 * 1024 * 1024                 # one map picture (satellite, floor plan): bytes
PLAN_IDS = ('p1', 'p2', 'p3', 'p4')         # up to 4 picture layers, bottom first
PLAN_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'))
MAP_UNITS, MAP_ENVS = ('ft', 'm'), ('outdoor', 'open', 'home', 'cluttered')
FAST_S, SLOW_S, DEBUG_S = 0.25, 1.0, 2.0     # update periods of the three event types
TAIL_N = 250                                # board lines kept for the Debug page (not CSI)
TODAY_S = 10.0                              # today's totals: recomputed this often
SETTINGS = {'per_link': False}              # dashboard settings (sessions/dashboard.json)


THRESHOLD_KEYS = ('on_short', 'off_short', 'on_long', 'off_long', 'short_gate')
MAC_RE = re.compile(r'[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}')
SPARK_S = 30.0                              # a link's score trace in its card
# ACKs a second (over its last 2 s) a link needs for a verdict of its own: weak
# under the first, until back over the second
LINK_MIN_RATE, LINK_OK_RATE = 15.0, 20.0
LINK_EVENTS = 400                           # device-link motion periods remembered
ALERT_MAX = 8                               # device links that may drive the alerts
EXT_EVERY_S = 1.0                           # EXT,1 repeated this often (the board holds it 5 s)
ACT_LABEL_RE = re.compile(r'[a-z0-9_]{1,24}')  # an activity's key (own names allowed)
ROUTER_NAME_MAX, ROUTER_NAMES_MAX = 24, 32    # nicknames you give routers (by BSSID)
BOARD_NAMES_MAX = 16                           # ...and other ackwave boards (by MAC), same length
SAT_GONE_S = 10                                # another board not heard this long: gone
SAT_MAX = 16
PEER_POKE_MAX, PEER_MIN_RSSI = 3, -82          # each board pokes up to 3 other boards it hears this well
PEER_RETRY_S, PEER_TRIES = 120, 3              # a poke that didn't take: tried again after 2 min, 3 times at most
OUT_MODES = ('off', 'motion', 'on')


def out_line(c):
    """('OUT,<mode>,<s>', words) from a page command, or None."""
    mode, secs = c.get('mode'), c.get('secs', 60)
    if mode == 'default':
        return 'OUT,default', "light output back to the firmware's (with motion, off after 1 min)"
    if mode not in OUT_MODES or isinstance(secs, bool) or not isinstance(secs, int) or not 0 <= secs <= 86400:
        return None
    words = {'off': 'light output off', 'on': 'light output always on',
             'motion': f'light on with motion, off after {fmt_age(secs) if secs else "the motion ends"}'}[mode]
    return f'OUT,{mode},{secs}', words
FOLLOW_WAIT_S = 25                             # a network switch waits this long for the boards to confirm
OTHERS_HOLD_S = 15.0
EVENTS_MAX = 300                               # events kept for the live stream
EVENT_TYPES = ('motion', 'automation', 'health', 'calib', 'device', 'network')                           # another board's motion keeps the alert on this long after
ACT_HOLD_S = 3.0                              # an activity counts once it lasts this long (no flicker)
ACT_NOT_KEPT = ('uncalibrated', 'no-data', 'unknown')   # shown, but not filed in the timeline


class LinkDetector:
    """One link's own motion verdict (experimental, display only): the board's
    algorithm fed just this link's ACKs, with its own quiet level, so a device
    link is judged against its own noise, not the router's. A link that
    answers too rarely gets no verdict at all ("weak"): a handful of ACKs a
    second reads as motion that isn't there. Spans are in board time
    (State.now) and kept for the longest window."""

    def __init__(self):
        self.det = MotionDetector()
        self.spans = []                          # motion: [t0, t1 or None]
        self.weak_spans = []                     # too weak to judge: [t0, t1 or None]
        self.scores = deque()                    # (t, its short score) for SPARK_S
        self.recent = deque()                    # times (us) of the last 2 s of ACKs
        self.weak = True                         # until it shows a steady rate
        self.motion = False                      # the verdict shown: never while weak
        self.last_ts = self.ch = None

    def feed(self, d, t, thresholds):
        """One ACK. Returns +1 / -1 when the link's motion starts / ends, else 0."""
        if self.last_ts is not None and d['ts'] < self.last_ts - 1_000_000:
            self.__init__()                      # board restarted: a new time base
        self.last_ts = d['ts']
        if self.ch is not None and d['ch'] != self.ch:
            self.det.on_channel(d['ts'])         # new channel: new baselines
        self.ch = d['ch']
        self.det.c.update(thresholds)            # the board's (SENS can change them)
        sc = self.det.on_csi(dict(d, src=0))     # scored as this detector's only link
        if sc is not None:
            self.scores.append((t, sc))
        while self.scores and self.scores[0][0] < t - SPARK_S:
            self.scores.popleft()
        self.recent.append(d['ts'])
        while self.recent[0] < d['ts'] - 2_000_000:
            self.recent.popleft()
        return self._verdict(d['ts'], t, len(self.recent) / 2.0)

    def silent(self, now_us):
        return self.last_ts is None or now_us - self.last_ts > self.det.c['stale_s'] * 1e6

    def idle(self, now_us, t):
        """Another link's packet arrived: a link that went silent ends its
        motion (as the board's detector would) and counts as weak from here."""
        if (self.motion or not self.weak) and self.silent(now_us):
            self.recent.clear()
            return self._verdict(now_us, t, 0.0)
        return 0

    def _verdict(self, now_us, t, rate):
        self.det.evaluate(now_us)
        weak = rate < (LINK_OK_RATE if self.weak else LINK_MIN_RATE)
        if weak or self.weak_spans:
            self._span(self.weak_spans, weak, t)
        self.weak = weak
        motion = self.det.motion and not weak
        change = int(motion) - int(self.motion)
        self.motion = motion
        self._span(self.spans, motion, t)
        return change

    @staticmethod
    def _span(spans, on, t):
        """Open, keep or close the last span; forget those older than the longest window."""
        is_open = bool(spans) and spans[-1][1] is None
        if on and not is_open:
            spans.append([t, None])
        elif not on and is_open:
            spans[-1][1] = t
        while spans and spans[0][1] is not None and spans[0][1] < t - WINDOWS[-1]:
            spans.pop(0)

    def state(self, now_us):
        if self.silent(now_us):
            return 'silent'
        if self.weak:
            return 'weak'
        if self.det.floor <= 0 or self.det.phase in ('wait', 'collect'):
            return 'calibrating'
        return 'motion' if self.motion else 'still'


def strength(peak, on_short):
    """A motion period's strength in a word, from its peak short-window score
    (× quiet level) against the short window's threshold."""
    if peak is None or not on_short:
        return ''
    r = peak / on_short
    return 'gentle' if r < 1 else 'moderate' if r < 1.8 else 'strong'


def pc_timezone():
    """The PC's time zone name (e.g. 'America/Chicago'), so every page shows
    the PC's time whatever device views it; None if it can't be found."""
    ok = lambda z: bool(z) and re.fullmatch(r'[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}', z) is not None
    z = os.environ.get('TZ', '').lstrip(':')
    if ok(z):
        return z
    try:
        with open('/etc/timezone') as f:
            z = f.read().strip()
        if ok(z):
            return z
    except OSError:
        pass
    try:
        z = os.path.realpath('/etc/localtime').split('zoneinfo/', 1)[1]
        if ok(z):
            return z
    except (OSError, IndexError):
        pass
    return None


PC_TZ = pc_timezone()


def r2(x):
    return None if x is None else round(float(x), 2)


class Dashboard:
    """Owns the State; turns it into JSON for the page and runs its commands."""

    def __init__(self, state, send=None, log_name=None, prefs=None, settings=None, sessions_dir=None,
                 light_call=http_call, light_run=None, demo=False, notify_call=pushover_call):
        self.s, self.send, self.log_name, self.prefs = state, send, log_name, prefs
        self.demo = demo                 # demo mode: a simulated light, nothing sent anywhere
        state.devices.router_name = self._router_name   # nicknames on every page
        state.devices.board_name = lambda m: self.cfg['board_names'].get(m)
        self.sats = {}                   # other boards heard through ours: MAC -> latest report
        self.sat_cmds = {}               # MAC -> the last command sent to it through the hub, and its state
        self.follow = None               # a network switch waiting for the other boards to confirm
        self.peer_tries = {}             # (board, other board) -> (pokes asked, when): links between the boards
        self.out = None                  # the hub's light output as it last said (OUT lines)
        self.breath = {}                 # 'hub' / 'sat:<mac>' -> its latest breathing reading (BREATH lines)
        self.mlb = {}                    # 'hub' / 'sat:<mac>' -> its learned model's last second (ML lines)
        self.feat = {}                   # 'hub' / 'sat:<mac>' -> deque of (wall, 8 features) (FEAT lines)
        self.teach = None                # recording labelled features: dict(board, label, until)
        self.mlw_q = {}                  # board key -> commands still to send (weights, one at a time)
        self.out_pin = None
        # Where people are, all the time (track.py: the Map's tracker on the Pi); zones on the map
        self.tracker = Tracker()
        self.track_layout = (None, {})   # (map file mtime, layout)
        self.zone_in = {}                # zone id -> people inside now
        self.zone_said = {}              # zone id -> when it last notified
        self.today = ('', {})            # (local date, {(ix, iy): steps someone was tracked there}) at 0.5 m
        self.snaps = deque(maxlen=300)   # moments of movement: every link's level (self-calibration's data)
        self.user_touch = {}             # node key -> when you last moved it (the automatic fit leaves it a day)
        self.autofit_state = dict(at=None, note='', moved={})   # the Pi's last automatic router fit
        self._autofit_busy = False
        self.picker = linkpick.LinkPicker()      # which devices each board pings: learned over time
        self.picker_off = {}                     # (board, device) -> when the picker dropped it
        self._picker_loaded = False
        self.house = House()             # every board's verdict combined (boards.py)
        self.travel = Travel()           # someone walking from board to board (boards.py)
        self.travel_ev = None            # its live-stream entry while the path grows
        self.events = deque(maxlen=EVENTS_MAX)   # the live stream on Home: what happened, newest last
        self.event_id = 0
        self.started_wall = time.time()
        self.sat_links = {}              # other board -> {its link's far end: SatLink}
        self.demo_people = {}            # demo: where the simulated people really are
        self.lost_since = None           # monotonic: when the hub's port went away (None: connected)
        self.surveys = {}                # other board -> {who it hears: {kind, rssi, at}}
        self.house_now = (False, [])     # (motion, the boards that count)
        # Several boards on USB (boards.py): the primary is self.s; the rest are watched
        self.others = {}                 # MAC -> OtherBoard
        self.log = None                  # the session log, when lines come from several ports
        self.src_board = {}              # port index -> the MAC its ME line said
        self.src_send = {}               # port index -> send() to that board
        self.primary = None              # the primary board's MAC, once chosen
        self.n_src = 1
        self.sessions_dir = sessions_dir
        self.sessions = Sessions(sessions_dir) if sessions_dir else None
        # Where and when motion happened, on disk (timeline.py): the periods
        # still open are kept here until they end
        self.timeline = Timeline(sessions_dir) if sessions_dir else None
        self._open = {}                          # key -> [wall0, name]
        self.started = time.time()
        self._today = (0.0, None)                # (when, today's totals)
        # For the Debug page: recent board lines (not CSI), commands sent,
        # commands the board refused, and the calibration history
        self.tail = deque(maxlen=TAIL_N)         # (wall, line)
        self.sent_cmds = deque(maxlen=50)        # (wall, command)
        self.refused = deque(maxlen=50)          # (wall, board line, last command sent before it)
        self.calibs = deque(maxlen=100)          # dict(t, phase, why, floor) at each phase change
        self.lock = threading.Lock()
        self.version = 0                         # bumps on every line fed
        self.settings_path = settings
        self.cfg = dict(SETTINGS, sound=None, sens=None, alerts=[], router_names={}, board_names={}, act_place=0, hub=None, paired=[],
                        house_rule='confirm', house_off=[])   # house_off: boards that don't count   # sound, sens: kept on the board; None = its own
        # Device links that drive the alerts (light, buzzer, LED) besides the board:
        # src -> True once that link's slow window confirmed its current motion
        self.confirmed = {}
        self.ext_sent, self.ext_at = False, 0.0       # what the board was last told (EXT)
        # sound: [mode, vol, hz]; sens: long-window threshold x100
        got = {}
        if settings:
            try:
                with open(settings) as f:
                    got = json.load(f)
                if not isinstance(got, dict):    # damaged: same as missing
                    got = {}
                self.cfg.update({k: got[k] for k in SETTINGS if k in got})
                al = got.get('alerts')
                if isinstance(al, list) and len(al) <= ALERT_MAX and all(
                        isinstance(k, str) and MAC_RE.fullmatch(k) for k in al):
                    self.cfg['alerts'] = [k.lower() for k in al]
                ap = got.get('act_place')
                if isinstance(ap, int) and not isinstance(ap, bool) and 0 <= ap < 10 ** 6:
                    self.cfg['act_place'] = ap
                rn = got.get('router_names')
                if isinstance(rn, dict) and len(rn) <= ROUTER_NAMES_MAX:
                    self.cfg['router_names'] = {k.lower(): v for k, v in rn.items()
                                                if isinstance(k, str) and MAC_RE.fullmatch(k) and _router_nick(v)}
                bn = got.get('board_names')
                if isinstance(bn, dict) and len(bn) <= BOARD_NAMES_MAX:
                    self.cfg['board_names'] = {k.lower(): v for k, v in bn.items()
                                               if isinstance(k, str) and MAC_RE.fullmatch(k) and _router_nick(v)}
            except (OSError, ValueError):
                got = {}
        try:                                     # re-applied whenever the board differs
            if got.get('sound') is not None:
                state.want_sound(*got['sound'])
                self.cfg['sound'] = list(state.sound_want)
        except (ValueError, TypeError):
            pass
        try:
            if got.get('sens') is not None:
                state.want_sens(got['sens'])
                self.cfg['sens'] = state.sens_want
        except (ValueError, TypeError):
            pass
        # Keys for the boards' sealed reports (keys.py): one secret, kept like the light URLs
        sec = got.get('board_secret')
        self.board_secret = sec if isinstance(sec, str) and SECRET_RE.fullmatch(sec) else None
        self.pair = Pairing(self.board_secret)
        hub = got.get('hub')
        if isinstance(hub, str) and MAC_RE.fullmatch(hub):
            self.cfg['hub'] = hub.lower()
        if got.get('house_rule') in HOUSE_RULES:
            self.cfg['house_rule'] = got['house_rule']
        ho = got.get('house_off')
        if isinstance(ho, list) and len(ho) <= SAT_MAX + 1:
            self.cfg['house_off'] = sorted({k.lower() for k in ho if isinstance(k, str) and
                                            (k == 'board' or MAC_RE.fullmatch(k))})
        pr = got.get('paired')
        if isinstance(pr, list) and len(pr) <= SAT_MAX:
            self.cfg['paired'] = sorted({m.lower() for m in pr if isinstance(m, str) and MAC_RE.fullmatch(m)})
        # Light on motion: web hooks called when motion starts / the room goes still
        if demo:                         # a light that only pretends: the demo shows the automation
            self.light = Light(dict(enabled=True, on_url='http://demo.invalid/on', off_url='http://demo.invalid/off',
                                    off_after=20), call=lambda url: (True, 'demo'), run=light_run)
        else:
            self.light = Light(got.get('light') if isinstance(got.get('light'), dict) else None,
                               call=light_call, run=light_run)
        # Phone notifications (Pushover): the demo pretends, nothing leaves the PC
        nc = got.get('notify') if isinstance(got.get('notify'), dict) else None
        if demo:
            self.notify = Notify(nc, call=lambda *_a: (True, 'demo: not sent', None), run=light_run)
        else:
            self.notify = Notify(nc, call=notify_call, run=light_run)
        if send and state.sound_want is None:
            state.outbox.append('SOUND')         # ask the board for its buzzer settings
        if send and state.sens_want is None:
            state.outbox.append('SENS')
        if send:
            state.outbox.append('OUT')           # its light output (older firmware: REFUSED, harmless)
        # Per-link analysis (experimental, off unless switched on): node -> analysers
        self.links = {}                          # src -> dict(br=Breathing, dop=Doppler, hist=deque, md)
        # Activity recognition by example (activity.py): clips in sessions/activities.json
        self.act = Activities(os.path.join(os.path.dirname(settings), 'activities.json') if settings else None)
        self.act_log = deque(maxlen=300)         # [wall0, wall1, state] activity periods, oldest first
        self.act_cur = None                      # [wall0, state]: the activity now (once it held ACT_HOLD_S)
        self.act_cand = None                     # [wall0, state]: a different one, not held long enough yet
        self.csi_ts = None                       # last CSI time seen with per-link on
        # When each device link saw motion, on the wall clock (survives board
        # restarts): [wall0, wall1 or None, node, name]. Says where a motion was.
        self.link_events = []
        state.on_csi = self._on_csi
        try:                                     # the learned detector, beside the board (experimental)
            self.ml = Shadow()
        except (OSError, ValueError, KeyError) as e:
            print(f"learned detector off: {e}", file=sys.stderr)
            self.ml = None

    def _on_csi(self, d, t):
        """Every CSI packet (called inside State.feed, lock held)."""
        # where the board and router are: the router heard most (by its address),
        # the channel, and how often you said you moved them. Posture clips only
        # count at the placement they were recorded at.
        src = self.act.feat.src
        self.act.place = f"{self.s.nodes.get(src, src)}|{d['ch']}|{self.cfg['act_place']}"
        if self.ml is not None and 'hub' not in self.mlb:      # the hub's own model wins once it reports
            ch = self.ml.feed(src, d, t)
            if ch is not None:
                self._ml_period(ch, self.s.wall(t) if self.s.wall_off is not None else time.time())
        r = self.act.feed(d, t)
        if r is not None:
            self._act_period(r['state'], self.s.wall(t) if self.s.wall_off is not None else time.time())
        if not self.cfg['per_link']:
            return
        if self.csi_ts is not None and d['ts'] < self.csi_ts - 1_000_000:
            self._drop_links()                   # board restarted: old times mean nothing now
        self.csi_ts = d['ts']
        for node, ln in self.links.items():
            self._link_change(node, ln['md'].idle(d['ts'], t), t)
        if d['kind'] != 1:
            return
        src = d['src']
        if src not in self.links:
            self.links[src] = dict(br=Breathing(src), dop=Doppler(WINDOWS[-1], src), hist=deque(),
                                   md=LinkDetector())
        ln = self.links[src]
        self._link_change(src, ln['md'].feed(d, t, {k: self.s.det.c[k] for k in THRESHOLD_KEYS}), t)
        ln['dop'].feed(d, t)
        r = ln['br'].feed(d, t)
        if r is not None:
            ln['hist'].append((t, r))
            while ln['hist'] and ln['hist'][0][0] < t - WINDOWS[-1]:
                ln['hist'].popleft()

    def _act_period(self, st, w):
        """The activity verdict (every 0.5 s) as periods: a new one starts only
        after it held ACT_HOLD_S, so a second of 'unknown' between two walks
        doesn't split the walk. Finished periods go in the log and, when they
        are a real activity, in the timeline."""
        cur = self.act_cur
        if cur is None:
            self.act_cur = [w, st]
            return
        if st == cur[1]:
            self.act_cand = None
            return
        if self.act_cand is None or self.act_cand[1] != st:
            self.act_cand = [w, st]
            return
        if w - self.act_cand[0] < ACT_HOLD_S:
            return
        w0, w1 = cur[0], self.act_cand[0]
        self.act_log.append([w0, w1, cur[1]])
        if cur[1] not in ACT_NOT_KEPT and self.timeline and w1 > w0:
            self.timeline.add(w0, w1, 'activity', cur[1])
        self.act_cur, self.act_cand = self.act_cand, None

    def _event(self, kind, text, key=None, important=False, t=None):
        """One entry of the live stream. key: the map node it is about (or None)."""
        self.event_id += 1
        e = dict(id=self.event_id, t=r2(t or time.time()), type=kind, text=str(text)[:120], key=key,
                 important=important)
        self.events.append(e)
        return e

    def _event_name(self, key, name):
        if key == 'board':
            return 'Hub' if self.sats else 'Board'
        if key.startswith('sat:') and '>' not in key:
            return self.board_label(key[4:])
        return name or key

    def _paths_now(self, most=3):
        """The links seeing motion right now, as paths: '4 → kartik front, 3 → Sonos'
        (the board you know, the far end it watches). The strongest first."""
        now = time.time()
        rows = []
        for mac, links in self.sat_links.items():
            for tmac, ln in links.items():
                if ln.moving and ln.at is not None and now - ln.at <= SAT_GONE_S:
                    tkey, tname = self._target_key(tmac, ln.flags)
                    if tkey == 'hub' and 'hub' in set(self._layout().get('track_off', [])):
                        continue
                    rows.append((ln.level(), f"{self.board_label(mac)} → {tname}"))
        rows.sort(reverse=True)
        return [t for _, t in rows[:most]]

    def _board_key(self, mac):
        mac = mac.lower()
        hubs = {(self.s.me or '').lower(), (self.cfg.get('hub') or '').lower()} - {''}
        return 'hub' if mac in hubs else 'sat:' + mac

    def _ml_line(self, line):
        """ML,<board>,<moving>,<p empty>,<p still>,<p moving> / FEAT,<board>,<8 numbers>."""
        p = line.split(',')
        if len(p) < 3 or not MAC_RE.fullmatch(p[1]):
            return
        key, now = self._board_key(p[1]), time.time()
        try:
            if p[0] == 'ML' and len(p) == 6:
                was = self.mlb.get(key, {}).get('motion')
                motion = p[2] == '1'
                self.mlb[key] = dict(motion=motion, p=[int(p[3]), int(p[4]), int(p[5])], at=now)
                if motion != was and self.timeline:
                    name = ('Hub' if key == 'hub' else self.board_label(key[4:])) + ' · learned'
                    if motion:
                        self.ml_open_b = getattr(self, 'ml_open_b', {})
                        self.ml_open_b[key] = now
                    elif getattr(self, 'ml_open_b', {}).get(key) is not None:
                        self.timeline.add(self.ml_open_b.pop(key), now, 'ml:' + key, name)
            elif p[0] == 'FEAT' and len(p) == 10:
                f = [float(x) for x in p[2:]]
                if all(math.isfinite(x) for x in f):
                    self.feat.setdefault(key, deque(maxlen=120)).append((now, f))
                    tc = self.teach
                    if tc and tc['board'] == key and now <= tc['until']:
                        self._teach_save(key, tc['label'], f, now)
        except ValueError:
            pass

    def _teach_save(self, key, label, f, now):
        if not self.sessions_dir:
            return
        try:
            with open(os.path.join(self.sessions_dir, 'ml_labels.jsonl'), 'a') as fh:
                fh.write(json.dumps(dict(t=round(now, 2), board=key, label=label, f=[round(x, 4) for x in f])) + '\n')
            self.teach['n'] = self.teach.get('n', 0) + 1
        except OSError:
            pass

    def _mlw_line(self, line):
        """The hub's own answers to weights: the next command goes once one is taken."""
        q = self.mlw_q.get('hub')
        if q and (line.startswith('MLW,got,') or line.startswith(('MLW,loaded', 'MLW,built-in'))):
            self.s.outbox.append(q.pop(0))
            if not q:
                del self.mlw_q['hub']
        elif q and line.startswith('MLW,error'):
            del self.mlw_q['hub']
            self._event('health', f"Hub refused the new model ({line[10:40]})", 'hub', important=True)

    def _mlw_step_sat(self, mac, done):
        """A board confirmed an order: its next weights command, through the hub."""
        key = 'sat:' + mac
        q = self.mlw_q.get(key)
        if not q:
            return
        if not done:
            del self.mlw_q[key]
            self._event('health', f"{self.board_label(mac)}: the new model didn't arrive (older firmware?)", key, important=True)
            return
        cmd = q.pop(0)
        self.s.outbox.append(f'SATCMD,{mac},{cmd}')
        self.sat_cmds[mac] = dict(line=cmd, state='sent', at=time.time())
        if not q:
            del self.mlw_q[key]

    def _ml_period(self, on, wall):
        """The learned detector's periods: the timeline only (a lane to compare
        with the board's), never the live stream, the light or the alerts."""
        if on:
            self.ml_open = wall
        elif getattr(self, 'ml_open', None) is not None:
            if self.timeline:
                self.timeline.add(self.ml_open, max(self.ml_open, wall), 'ml', 'Learned (experimental)')
            self.ml_open = None

    def _period(self, key, on, wall, name=''):
        """A link's (or the board's) motion period opens or closes; a closed one
        is filed in the timeline. Each one is also one entry of the live stream
        (open: 'now'; closed: its length)."""
        if on and key not in self._open:
            self._open[key] = [wall, name]
            mk = 'hub' if key == 'board' else None if key == 'house' else key.split('>')[0]
            e = self._event('motion', self._event_name(key, name), mk, t=wall)
            e['open'], e['house'] = True, key == 'house'
            self._open_ev = getattr(self, '_open_ev', {})
            self._open_ev[key] = e
            if key == 'board' or key.startswith('sat:'):
                self._travel(mk, wall)
        elif not on and key in self._open:
            w0, nm = self._open.pop(key)
            if self.timeline:
                self.timeline.add(w0, max(w0, wall), key, nm)
            e = getattr(self, '_open_ev', {}).pop(key, None)
            if e:
                e['open'], e['dur'] = False, r2(max(0.0, wall - w0))

    def _travel(self, key, wall):
        """A board's motion started: maybe the next step of someone walking."""
        if not self.sats:
            return
        path = self.travel.start(key, wall)
        if path is None:
            return
        names = [self._event_name('board' if k == 'hub' else k, '') for _t, k in path]
        text = ' → '.join(names)
        keys = [k for _t, k in path]
        e = self.travel_ev
        if e is not None and e.get('path_n') == self.travel.n and e in self.events:
            e.update(text=text, keys=keys, key=keys[-1], dur=r2(path[-1][0] - path[0][0]))
        else:
            e = self._event('travel', text, keys[-1], t=path[0][0])
            e.update(keys=keys, path_n=self.travel.n, dur=r2(path[-1][0] - path[0][0]))
            self.travel_ev = e

    def _board_period(self):
        """The board's own verdict as timeline periods."""
        s = self.s
        if s.wall_off is None:
            return
        sp = s.motion_spans
        on = bool(sp) and sp[-1][1] is None
        if on:
            self._period('board', True, s.wall(sp[-1][0]), 'Board')
        elif 'board' in self._open:
            end = sp[-1][1] if sp and sp[-1][1] is not None else None
            self._period('board', False, s.wall(end) if end is not None else (s.last_wall or time.time()))

    # ---------- other boards (their hellos, heard by ours) ----------
    def board_label(self, mac):
        return self.cfg['board_names'].get(mac) or f"Board {mac[-5:]}"

    def _sat(self, line):
        ev = parse_line(line)
        if not ev or ev[0] != 'sat':
            return
        d, now = ev[1], time.time()
        old = self.sats.get(d['mac'])
        if old is None and len(self.sats) >= SAT_MAX:
            return
        if old and old['seq'] == d['seq'] and now - old['at'] < SAT_GONE_S:
            return                                   # the same hello, heard twice
        if old and old.get('via') == 'usb' and now - old['at'] < SAT_GONE_S:
            return                                   # on USB too: its own lines say more
        if old and old.get('gone_said'):
            self._event('health', f"{self.board_label(d['mac'])} back", 'sat:' + d['mac'])
        elif old is None and (self.demo or now - self.started_wall > 60):
            self._event('health', f"{self.board_label(d['mac'])} joined", 'sat:' + d['mac'])
        d['at'] = now
        d['since'] = old['since'] if old and old['motion'] == d['motion'] and now - old['at'] < SAT_GONE_S else now
        self.sats[d['mac']] = d
        self._period('sat:' + d['mac'], d['motion'] and d['calibrated'], now, self.board_label(d['mac']))

    def _hub_boards(self):
        """The other boards the hub should hold keys for: every board paired on
        this PC (its reports find the hub on any channel) and those it hears."""
        heard = {m for m, dv in self.s.devices.devs.items() if dv.board and dv.same}
        return sorted((heard | set(self.cfg['paired'])) - {self.cfg['hub']})

    def _note_paired(self, pair):
        """A board now holds its key: remember it, so the hub gets its key too."""
        if pair.key_ok() and pair.me != self.cfg['hub'] and pair.me not in self.cfg['paired'] \
                and len(self.cfg['paired']) < SAT_MAX:
            self.cfg['paired'] = sorted(self.cfg['paired'] + [pair.me])
            self._save_settings()

    def _pair_cmds(self):
        """Hand the connected board its key and, if it is the hub, its boards' keys."""
        if not self.send or not self.pair.me or not self.settings_path:
            return
        changed = False
        if not self.board_secret:
            self.board_secret = self.pair.secret = new_secret()
            changed = True
        if not self.cfg['hub']:                      # the first board ever connected is the hub
            self.cfg['hub'] = self.pair.me
            changed = True
        if changed:
            self._save_settings()
        self._note_paired(self.pair)
        self.s.outbox += self.pair.commands(self.pair.me == self.cfg['hub'], self._hub_boards(), time.time(),
                                            hub_mac=self.cfg['hub'])

    # ---------- several ports (step A) ----------
    def set_sources(self, sends, log=None):
        """Lines will come from several ports (feed_src); sends[i] writes to port i."""
        self.n_src = len(sends)
        self.src_send = dict(enumerate(sends))
        self.log = log
        self.send = None                             # until the primary is known

    def _choose_primary(self, now):
        """The hub's saved MAC if it is plugged in, else the first board that
        said who it is. Waits (up to 8 s) until every port has said."""
        known = {i: m for i, m in self.src_board.items()}
        if not known:
            return
        if self.cfg['hub'] in known.values():
            pick = self.cfg['hub']
        elif len(known) == self.n_src or now - self._started_src > 8:
            pick = known[min(known)]
        else:
            return
        self.primary = pick
        port = next(i for i, m in known.items() if m == pick)
        self.send = self.src_send.get(port)
        self.s.live = self.send is not None

    def feed_src(self, src, line, now=None):
        """One line from port `src`. Tagged lines ('@<mac>,') name their board."""
        now = time.time() if now is None else now
        mac, body = split_tag(line)
        if mac is None:
            if body.startswith('ME,'):
                m = body.strip()[3:].lower()
                if len(m) == 17:
                    self.src_board[src] = m
            mac = self.src_board.get(src)
        if self.primary is None:
            if not hasattr(self, '_started_src'):
                self._started_src = now
            with self.lock:
                self._choose_primary(now)
            if self.primary is None:
                return                               # the first seconds: who is who
        if mac is None or mac == self.primary:
            if self.log and mac == self.primary:
                self.log.feed(body)
            self.feed(body)
            return
        if self.log:
            self.log.feed_raw(f'@{mac},{body}')
        self.feed_other(mac, body, self.src_send.get(src) if self.src_board.get(src) == mac else None, now)

    def feed_other(self, mac, line, send=None, now=None):
        """A line of a board that isn't the primary one."""
        now = time.time() if now is None else now
        with self.lock:
            ob = self.others.get(mac)
            if ob is None:
                if len(self.others) >= SAT_MAX:
                    return
                ob = self.others[mac] = OtherBoard(mac, State(WINDOWS[-1]), send, self.board_secret,
                                                   lambda k, on, w: self._period(k, on, w, self.board_label(k[4:])))
            if send is not None:
                ob.send = send
            ob.feed(line, now)
            if ob.send and line.startswith('KEY,') and self.board_secret:
                self._note_paired(ob.pair)
                for c in ob.pair.commands(False, self._hub_boards(), now, hub_mac=self.cfg['hub']):
                    try:
                        ob.send(c)
                        self.sent_cmds.append((now, f'{mac[-5:]}: {redact(c)}'))
                    except Exception as e:
                        print(f"command not sent ({redact(c)[:40]!r}): {e}", file=sys.stderr)
            if line.startswith(('STATE,', 'STATS,', 'CALIB,')) or mac not in self.sats:
                self.sats[mac] = ob.report()

    BOARD_CMDS = ('cal', 'sound', 'sens', 'led', 'out')

    def _board_command(self, mac, c):
        """A command for another board: recalibrate, buzzer, sensitivity, LED.
        On USB here: sent straight to it. Otherwise through the hub, from the
        air (SATCMD; firmware with orders). The board saves each one itself."""
        what = c.get('cmd')
        with self.lock:
            mac = mac.lower() if isinstance(mac, str) else mac
            ob = self.others.get(mac) if isinstance(mac, str) else None
            air = (ob is None or not ob.send) and isinstance(mac, str) and mac in self.sats and bool(self.send)
            if not air and (ob is None or not ob.send):
                return False, 'that board is not on USB here, nor heard through the hub'
            if what not in self.BOARD_CMDS:
                return False, 'only the hub takes that command'
            name = self.board_label(mac)
            if what == 'cal':
                line, msg = 'CAL', f'{name}: recalibrating, step away'
            elif what == 'out':
                got = out_line(c)
                if not got:
                    return False, 'bad light output setting'
                line, msg = got[0], f'{name}: {got[1]}'
            elif what == 'sound':
                if c.get('test'):
                    line, msg = 'SOUND,test', f'{name}: playing the motion sound'
                elif c.get('mode') == 'default':
                    line, msg = 'SOUND,default', f"{name}: buzzer back to the firmware's settings"
                else:
                    try:
                        mode, vol, hz = c.get('mode'), int(c.get('vol')), int(c.get('hz'))
                    except (TypeError, ValueError):
                        return False, 'bad buzzer setting'
                    if mode not in SOUND_MODES or not 1 <= vol <= 50 or not 500 <= hz <= 6000:
                        return False, 'bad buzzer setting'
                    line, msg = f'SOUND,{mode},{vol},{hz}', f'{name}: buzzer {mode}'
            elif what == 'sens':
                x = c.get('x100')
                if x == 'default':
                    line, msg = 'SENS,default', f"{name}: sensitivity back to the firmware's"
                elif isinstance(x, int) and not isinstance(x, bool) and SENS_RANGE[0] <= x <= SENS_RANGE[1]:
                    line, msg = f'SENS,{x}', f'{name}: long-window threshold x{x / 100:.1f}'
                else:
                    return False, 'bad sensitivity'
            else:
                if c.get('mode') == 'default':
                    line, msg = 'LEDSET,default', f"{name}: LED back to the firmware's settings"
                else:
                    color, bright = c.get('color'), c.get('bright')
                    if c.get('mode') not in LED_MODES or c.get('pattern') not in LED_PATTERNS \
                            or not isinstance(bright, int) or isinstance(bright, bool) or not 1 <= bright <= 255 \
                            or not isinstance(color, str) or not re.fullmatch(r'#?[0-9a-fA-F]{6}', color):
                        return False, 'bad LED setting'
                    line = f"LEDSET,{c['mode']},{bright},{c['pattern']},{color.lstrip('#').lower()}"
                    msg = f"{name}: LED {c['mode']}"
            if air:
                self.s.outbox.append(f'SATCMD,{mac},{line}')
                self.sat_cmds[mac] = dict(line=line, state='sent', at=time.time())
                self._flush()
                return True, msg + ' (through the hub: it takes it at its next report, a few seconds)'
            try:
                ob.send(line)
            except Exception as e:
                self.refused.append((self._now(), f'not sent: {e}'[:120], line))
                return False, f'not sent: {e}'
            self.sent_cmds.append((self._now(), f'{mac[-5:]}: {line}'))
            return True, msg

    def _board_settings(self):
        """Each other board on USB: what its buzzer, sensitivity and LED are now."""
        return {m: dict(name=self.board_label(m), sound=ob.s.sound, sens=ob.s.sens, led=ob.s.led)
                for m, ob in sorted(self.others.items()) if ob.send}

    def _sat_extra(self, line):
        """Another board's links (SATL) and who it hears (SURV), from the hub."""
        ev = parse_line(line)
        if not ev or ev[1]['mac'] not in self.sats:      # only boards whose sealed report opened
            return
        kind, d = ev
        now = time.time()
        if kind == 'satl':
            if d['mac'] not in self.sat_links:           # once per board: the add-on's log says so
                print(f"{self.board_label(d['mac'])}: {len(d['links'])} links reporting", flush=True)
            links = self.sat_links.setdefault(d['mac'], {})
            for e in d['links'][:16]:
                ln = links.get(e['mac'])
                if ln is None:
                    if len(links) >= 16:
                        continue
                    ln = links[e['mac']] = SatLink(e['flags'])
                ln.flags = e['flags']
                was = bool(ln.moving)
                ln.feed(e['ks'], e['kl'], now)
                if bool(ln.moving) != was:               # each link's own history: which path saw it
                    tkey, tname = self._target_key(e['mac'], ln.flags)
                    self._period(f"sat:{d['mac']}>{tkey}", bool(ln.moving), now,
                                 f"{self.board_label(d['mac'])} → {tname}")
        else:
            self.surveys[d['mac']] = {e['mac']: dict(kind=e['kind'], rssi=e['rssi'], at=now) for e in d['seen'][:40]}

    def _target_key(self, mac, flags):
        """A satellite link's far end as the Map names it: the hub, another
        board, one of the hub's routers (ap:N), another router (r:<bssid>), or a device (its MAC)."""
        hub = self.cfg['hub'] or self.s.me
        if mac == hub:
            return 'hub', 'Hub'
        if mac in self.sats or mac in self.cfg['paired']:
            return 'sat:' + mac, self.board_label(mac)
        if flags & 2:                                    # a router: on the map by its address
            return 'r:' + mac, self._router_label(mac)
        return mac, self.s.devices.name(mac) or mac[-8:]

    def _sats_gone(self):
        """A board that went quiet: its open motion period ends when it was last heard."""
        now = time.time()
        for mac, d in self.sats.items():
            if now - d['at'] > SAT_GONE_S:
                self._period('sat:' + mac, False, d['at'])
                for k in [k for k in self._open if k.startswith(f'sat:{mac}>')]:
                    self._period(k, False, d['at'])
                if not d.get('gone_said'):
                    d['gone_said'] = True
                    self._event('health', f"{self.board_label(mac)} not heard", 'sat:' + mac, important=True)
                    if self.send:
                        self.notify.board_offline(f"Board {self.board_label(mac)} is not heard any more")

    def _boards(self):
        """The other boards, as the hub hears them (their hellos). A board heard
        but sending no report yet still needs its key: plug it in here once."""
        now = time.time()
        out = []
        for mac in sorted(set(self._hub_boards()) - set(self.sats)):
            dv = self.s.devices.devs.get(mac)              # paired boards may not be heard at all
            paired = mac in self.cfg['paired']
            out.append(dict(mac=mac, name=self.board_label(mac), nick=self.cfg['board_names'].get(mac, ''),
                            state='quiet' if paired else 'unpaired', level=0, weak=False, acks=0,
                            router_rssi=0, links=0, up_s=0,
                            heard_s=round(now - dv.last_heard, 1) if dv else None,
                            rssi=dv.rssi if dv else 0, since=None))
        for mac, d in sorted(self.sats.items()):
            gone = now - d['at'] > SAT_GONE_S
            out.append(dict(mac=mac, name=self.board_label(mac), nick=self.cfg['board_names'].get(mac, ''),
                            state='gone' if gone else 'calibrating' if not d['calibrated'] else
                            'motion' if d['motion'] else 'still',
                            level=round(max(d['k_short'], d['k_long']), 2), weak=d['weak'],
                            acks=d['acks'], router_rssi=d['router_rssi'], links=d['links'],
                            up_s=d['up_s'], heard_s=round(now - d['at'], 1), rssi=d['rssi'],
                            since=r2(d['since']), via=d.get('via', 'air')))
        return out

    def _link_key(self, src):
        if src < MAX_APS:
            return f'ap:{src}'
        return next((m for m, i in self.s.devices.poking.items() if i == src), None)

    def _link_change(self, src, change, t):
        """A link's motion started (+1) or ended (-1): filed in the timeline, and
        remembered for device links (Recent motion says where)."""
        if not change:
            return
        key = self._link_key(src)
        if key:
            self._period(key, change > 0, self.s.wall(t), self.s.devices.link_label(src))
        if src < MAX_APS:
            return
        w = self.s.wall(t)
        if change > 0:
            self.link_events.append([w, None, src, self.s.devices.link_label(src)])
            del self.link_events[:-LINK_EVENTS]
        else:
            for e in reversed(self.link_events):
                if e[2] == src and e[1] is None:
                    e[1] = w
                    break

    def _drop_links(self):
        """Forget the per-link analysers; their open motion periods end here."""
        for e in self.link_events:
            if e[1] is None:
                e[1] = max(e[0], self.s.last_wall or e[0])
        for key in [k for k in self._open if k != 'board' and not k.startswith('sat:')]:
            self._period(key, False, self.s.last_wall or time.time())
        self.links.clear()
        self.csi_ts = None

    def _link_motions(self):
        """Device-link motion periods as (wall0, wall1, still going, name)."""
        now = self.s.last_wall or 0
        return [(a, b if b is not None else max(now, a), b is None, name)
                for a, b, _src, name in self.link_events]

    def _recent(self, n=6):
        """Newest first: the last motion periods and, with per-link analysis on,
        where each was: which device links saw it too, plus periods only a
        device link saw (2 s or longer; the board itself stayed still)."""
        s = self.s
        rows = [dict(a=a, b=a + dur, going=going, peak=peak, only=False, where='')
                for a, dur, peak, going in s.recent_motion(n)]
        if self.cfg['per_link']:
            ev = sorted(self._link_motions())
            for r in rows:
                names = sorted({nm for a, b, _g, nm in ev if a <= r['b'] + 1 and b >= r['a'] - 1})
                r['where'] = ' + '.join(['router'] + names)
            now = s.last_wall or 0
            mains = [(a, b if b is not None else now) for a, b in s.hist_motion]
            eps = []                             # periods at most 2 s apart are one
            for a, b, going, nm in ev:
                if eps and a <= eps[-1]['b'] + 2:
                    e = eps[-1]
                    e['b'], e['going'] = max(e['b'], b), e['going'] or going
                    e['names'].add(nm)
                else:
                    eps.append(dict(a=a, b=b, going=going, names={nm}))
            for e in eps[-4 * n:]:
                if e['b'] - e['a'] < BRIEF_S or any(a <= e['b'] + 1 and b >= e['a'] - 1 for a, b in mains):
                    continue
                rows.append(dict(a=e['a'], b=e['b'], going=e['going'], peak=None, only=True,
                                 where=' + '.join(sorted(e['names'])) + ' only'))
        rows.sort(key=lambda r: -r['a'])
        out = []
        on_s = s.det.c['on_short']
        for r in rows[:n]:
            dur = r['b'] - r['a']
            brief = not r['going'] and dur < BRIEF_S
            word = strength(r['peak'], on_s)
            what = ('brief blip (often noise)' if brief else
                    f"{word} motion".strip().capitalize() if not r['only'] else 'Motion on a device link only')
            out.append(dict(at=time.strftime('%H:%M:%S', time.localtime(r['a'])), t=r2(r['a']),
                            dur=('now ' + fmt_age(dur)) if r['going']
                            else ('brief' if dur < BRIEF_S else fmt_age(dur)),
                            secs=r2(dur), strength=word, what=what,
                            peak=r2(r['peak']), going=r['going'], only=r['only'], where=r['where'],
                            brief=brief))
        return out

    def _meaning(self):
        """The verdict in plain words: (headline, detail) for the header and Home."""
        s, det = self.s, self.s.det
        now = s.last_wall or time.time()
        on_s = det.c['on_short']
        where = 'in the sensing zone'
        near = self._where_now() if self.cfg['per_link'] else []
        if det.motion and s.hist_motion and s.hist_motion[-1][1] is None:
            a = s.hist_motion[-1][0]
            word = strength(s.hist_peak[-1] if s.hist_peak else None, on_s)
            head = f"{word.capitalize() + ' m' if word else 'M'}otion {where}"
            detail = f"for {fmt_age(now - a)}"
            if self.cfg['per_link']:
                detail += ' · seen by ' + ' + '.join(['router'] + near)
            return head, detail
        only = (f" · only the {' + '.join(near)} link{'s see' if len(near) > 1 else ' sees'} motion now"
                if near else '')
        last = [(a, b, pk) for (a, b), pk in zip(s.hist_motion, s.hist_peak)
                if b is not None and b - a >= BRIEF_S]
        if not last:
            return 'All quiet', 'no motion yet this session' + only
        a, b, pk = last[-1]
        word = strength(pk, on_s)
        detail = f"last: {word + ' ' if word else ''}motion for {fmt_age(b - a)}, " \
                 f"{time.strftime('%H:%M', time.localtime(a))}"
        return f"All quiet for {fmt_age(now - b)}", detail + only

    def _activity(self):
        """How close the room is to the motion threshold right now (100 = at it)."""
        det = self.s.det
        f = det.floor
        if f <= 0:
            return None
        ms, ml = det.max_scores(int(self.s.now * 1e6))
        if ms is None:
            return None
        rs, rl = ms / (det.c['on_short'] * f), ml / (det.c['on_long'] * f)
        pct = 100 * max(rs, rl)
        word = ('calm' if pct < 50 else 'some movement, under the threshold' if pct < 100
                else 'over the motion threshold')
        return dict(pct=round(min(pct, 400)), word=word, short=r2(ms / f), long=r2(ml / f),
                    on_s=r2(det.c['on_short']), on_l=r2(det.c['on_long']),
                    by='the fast (0.5 s) window' if rs >= rl else 'the slow (4 s) window')

    def _where_now(self):
        """Names of the device links that see motion right now."""
        return sorted({nm for _a, _b, going, nm in self._link_motions() if going})

    # ---------- input ----------
    def feed(self, line):
        with self.lock:
            if line.startswith('DEMO,'):                # the simulated people (demo only): the Map shows the truth
                if self.demo:
                    f = line.strip().split(',')
                    try:
                        if len(f) == 5:
                            self.demo_people[f[1][:16]] = dict(x=float(f[2]), y=float(f[3]), walking=f[4] == '1')
                        elif len(f) == 3 and f[2] == 'out':
                            self.demo_people.pop(f[1][:16], None)
                    except ValueError:
                        pass
                return
            if line.startswith('SATCMD,'):
                self._satcmd_line(line.strip())
            elif line.startswith(('ML,', 'FEAT,')):        # a board's learned model: its verdict, its features
                self._ml_line(line.strip())
            elif line.startswith('MLW,'):
                self._mlw_line(line.strip())
            elif line.startswith('BREATH,'):             # a board's breathing reading (its router link)
                p = line.strip().split(',')
                if len(p) == 6 and MAC_RE.fullmatch(p[1]) and p[2] in ('none', 'possible', 'clear'):
                    try:
                        mac = p[1].lower()
                        key = 'hub' if mac == (self.s.me or '').lower() else 'sat:' + mac
                        self.breath[key] = dict(level=p[2], rate=round(float(p[3]), 1), prom=float(p[4]),
                                                agree=int(p[5]), at=time.time())
                    except ValueError:
                        pass
            elif line.startswith('OUT,'):                # the hub's light output: OUT,<mode>,<s>,<on|off|none>
                p = line.strip().split(',')
                if len(p) == 4 and p[1] in OUT_MODES and p[2].isdigit():
                    self.out = dict(mode=p[1], secs=int(p[2]), state=p[3])
                elif len(p) == 2:                        # OUT,gpio5 / OUT,nopin at boot
                    self.out_pin = p[1]
            if line.startswith('SAT,'):
                self._sat(line)
            elif line.startswith(('SATL,', 'SURV,')):
                self._sat_extra(line)
            if self.pair.feed(line) or line.startswith('ME,'):
                self._pair_cmds()
            if line.startswith('DEV,'):                 # a device heard for the first time
                mac = line[4:21].lower()
                if mac not in self.s.devices.devs and (self.demo or time.time() - self.started_wall > 60) \
                        and self.s.devices.board_seen:
                    self._event('device', f"New device · {self.s.devices.name(mac) or mac}", mac)
            elif line.startswith('CHANNEL,'):
                p = line.strip().split(',')
                if len(p) > 3 and p[3] in ('csa', 'scan') and self.s.channel and p[2] != str(self.s.channel[0]):
                    self._event('network', f"Router channel {self.s.channel[0]} → {p[2]}", 'hub', important=True)
            self.s.feed(line)
            self.version += 1
            if time.monotonic() - getattr(self, '_track_at', 0.0) >= 0.5:   # twice a second, like the page
                self._track_at = time.monotonic()
                try:
                    self._track_step()
                except Exception as e:
                    print(f"tracking: {e}", file=sys.stderr)
            self._board_period()
            self._light_rule()
            if not line.startswith('CSI,'):
                self._debug_line(line.rstrip('\r\n'))
            self._flush()

    def _now(self):
        return self.s.last_wall or time.time()

    def _motion_now(self):
        """The verdict the badge shows; no data or calibrating counts as still."""
        s = self.s
        return bool(s.det.motion) and s.det.floor > 0 and not s.source_status()

    # ---------- people and zones, on the Pi ----------
    def _layout(self):
        if time.monotonic() - getattr(self, '_layout_at', -9.0) < 1.0:    # checked at most once a second
            return self.track_layout[1]
        self._layout_at = time.monotonic()
        p = self._map_path()
        try:
            mt = os.path.getmtime(p) if p else None
        except OSError:
            mt = None
        if mt != self.track_layout[0]:
            self.track_layout = (mt, self.map_load() if mt else {})
        return self.track_layout[1]

    def _track_links(self):
        """The live links as the Map draws them, with their ends from the saved
        layout: [dict(key, ends, end_keys, level)] for track.Tracker."""
        m = self._layout()
        fh = m.get('floor_h', 3.0)
        hub_fl = m.get('hub_floor', 1)
        nodes = {n['key']: n for n in m.get('nodes', [])}

        def ends_of(key):
            if key == 'hub':
                return [End(0, 0, 0, m.get('hub_facing'))]
            n = nodes.get(key)
            if not n:
                return None
            z = ((n.get('floor') or hub_fl) - hub_fl) * fh
            if n.get('placed') or key.startswith('sat:'):
                return [End(n['x'], n['y'], z, n.get('facing'))]
            if n.get('fit'):                     # the Pi fitted it: there, give or take a metre
                fx, fy = n['fit']
                return [End(fx, fy, z)] + [End(fx + math.cos(a), fy + math.sin(a), z) for a in (0, math.pi / 2, math.pi, -math.pi / 2)]
            au = n.get('auto')                   # the map's own estimate: its ellipse (map.js endSamples)
            if au and not n.get('hint'):
                if au[0] < 1:
                    return [End(n['x'], n['y'], z)]
                c, sn = math.cos(au[2]), math.sin(au[2])
                return [End(n['x'], n['y'], z), End(n['x'] + au[0] * c, n['y'] + au[0] * sn, z),
                        End(n['x'] - au[0] * c, n['y'] - au[0] * sn, z)]
            h = n.get('hint')                    # a rough spot, else around where the map last put it
            cx, cy, r = (h[0], h[1], h[2] * 0.6) if h else (n['x'], n['y'], 3.0)
            return [End(cx, cy, z)] + [End(cx + r * math.cos(a), cy + r * math.sin(a), z)
                                       for a in (0, math.pi / 2, math.pi, -math.pi / 2)]
        q = lambda x: round(min(3.0, max(0.0, x)) * 20) / 20
        off = set(m.get('track_off', []))
        now_l = time.time()
        # a board's learned model as a second opinion on its links: 'empty' makes a
        # twitch count less, 'moving' lets it count fully (x0.35 .. x1)
        def trust(key):
            v = self.mlb.get(key)
            if not v or now_l - v['at'] > 10:
                return 1.0
            pm = v['p'][2] / 100.0
            return 1.0 if v['motion'] else max(0.35, min(1.0, 0.35 + 1.3 * pm))
        out = []
        act = self._activity()
        for r in self._radio():
            if r.get('rssi') is not None and r['rssi'] <= -80:
                continue
            node = r.get('node') or r['key']
            if r['src'] == 0:
                level = q(act['pct'] / 100) if act else (1.25 if self._motion_now() else 0.0)
            elif r.get('activity') is not None:
                level = q(r['activity'] / 2.8)
            elif r.get('moving') is not None:            # a verdict without a level (map.js strength())
                level = 1.25 if r['moving'] else 0.0 if r.get('state') == 'still' else None
            else:
                level = None
            a, n = ends_of('hub'), ends_of(node)
            if n and 'hub' not in off and node not in off:
                lvl = None if level is None else q(level * trust('hub'))
                out.append(dict(key=r['key'], ends=[(x, y) for x in a for y in n], end_keys=('hub', node), level=lvl))
        for b in self._map_boards():
            if b['state'] not in ('motion', 'still'):
                continue
            a = ends_of(b['key'])
            if not a:
                continue
            for l in b['links']:
                n = ends_of(l['key'])
                if not n or l['key'] == b['key'] or b['key'] in off or l['key'] in off:
                    continue
                out.append(dict(key=f"{b['key']}>{l['key']}", ends=[(x, y) for x in a for y in n],
                                end_keys=(b['key'], l['key']), level=q(l['level'] * trust(b['key']))))
        wz = next((z for z in m.get('zones', []) if z.get('watch')), None)
        if wz:                                   # only links with an end in the watched area
            pos = {k: (n['x'], n['y']) for k, n in nodes.items()}
            pos['hub'] = (0.0, 0.0)
            inside = lambda k: k in pos and in_zone(wz, *pos[k])
            out = [l for l in out if any(inside(k) for k in l['end_keys'])]
        return out, m

    def _track_step(self):
        if not (self.send or self.demo):
            return
        if not getattr(self, '_walk_err_done', False):   # the walks so far set the circle's floor
            self._walk_err_done = True
            self._walk_err()
        links, m = self._track_links()
        now = time.time()
        area = next((z for z in m.get('zones', []) if z.get('watch')), None)
        if area is None:                         # nothing to watch yet: the house is drawn first
            self.tracker.people, self.tracker.near = [], None
            return
        if not self.tracker.step(links, now, area):
            return
        lv = {l['key']: l['level'] for l in links if l['level'] is not None}
        self._track_log(now, lv)
        if len(lv) >= 3 and max(lv.values()) >= 0.8 and (not self.snaps or now - self.snaps[-1]['t'] >= 3):
            self.snaps.append(dict(t=round(now, 1), links=lv))   # someone moving: a moment to fit routers on
        day = time.strftime('%Y-%m-%d')
        if self.today[0] != day:
            self.today = (day, {})
        for p in self.tracker.people:
            if p.est:
                c = (math.floor(p.est['x'] / 0.5), math.floor(p.est['y'] / 0.5))
                if c in self.today[1] or len(self.today[1]) < 20000:
                    self.today[1][c] = self.today[1].get(c, 0) + 1
        zones = m.get('zones', [])
        near = self.tracker.near['key'] if self.tracker.near else None
        for z in zones:
            if 'near' in z:
                inside = [1] if near == z['near'] else []
            else:
                inside = [p for p in self.tracker.people if p.est and in_zone(z, p.est['x'], p.est['y'])]
            was = self.zone_in.get(z['id'], 0)
            self.zone_in[z['id']] = len(inside)
            if inside and not was:
                n = len(self.tracker.people)
                self._event('zone', f"Motion at {z['name']}" if 'near' in z else f"Someone in {z['name']}", None,
                            important=bool(z.get('notify') or z.get('light')))
                if z.get('notify') and self.send and now - self.zone_said.get(z['id'], 0) >= 60 * self.notify.cfg['quiet_min']:
                    paths = self._paths_now()
                    if self.notify.zone(f"Movement in {z['name']}" + (f" · seen on {', '.join(paths)}" if paths else '')
                                        + (f" · {n} people" if n > 1 else ''), z.get('night')):
                        self.zone_said[z['id']] = now

    def _walk_err(self):
        """The circle's floor: the median real error of the last week's walk-check
        taps (at least 8), never below the model's own allowance."""
        errs = sorted(math.hypot(r['est'][0] - r['x'], r['est'][1] - r['y'])
                      for r in self.walks(hours=24 * 7)['walks'] if r.get('est'))
        if len(errs) >= 8:
            self.tracker.model_err = max(1.2, errs[len(errs) // 2])
        return self.tracker.model_err

    def walks(self, hours=24.0):
        """The walk check's taps (sessions/walks.jsonl) from the last hours, newest 500."""
        out = []
        if self.sessions_dir:
            try:
                with open(os.path.join(self.sessions_dir, 'walks.jsonl')) as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(r, dict) and time.time() - r.get('t', 0) <= hours * 3600:
                            out.append(r)
            except OSError:
                pass
        return dict(walks=out[-500:])

    def _send_model(self, model):
        """Queue a model's MLW steps for every board that sends features; returns their names."""
        cmds, sent = mltrain.mlw_commands(model), []
        for key in [k for k in self.feat if time.time() - self.feat[k][-1][0] < 30]:
            if key == 'hub':
                if self.send:
                    self.mlw_q['hub'] = cmds[1:]
                    self.s.outbox.append(cmds[0])
                    sent.append('the hub')
            elif key[4:] in self.sats and self.send:
                self.mlw_q[key] = cmds[1:]
                self.s.outbox.append(f'SATCMD,{key[4:]},{cmds[0]}')
                self.sat_cmds[key[4:]] = dict(line=cmds[0], state='sent', at=time.time())
                sent.append(self.board_label(key[4:]))
        self.model_sent = dict(at=time.time(), to=sent, labels=model.get('labels'))
        return sent

    def _teach_view(self):
        now = time.time()
        rows = mltrain.read_rows(os.path.join(self.sessions_dir, 'ml_labels.jsonl')) if self.sessions_dir else []
        boards = [dict(key=k, name='Hub' if k == 'hub' else self.board_label(k[4:]),
                       live=now - self.feat[k][-1][0] < 30 if self.feat.get(k) else False,
                       ml=self.mlb.get(k)) for k in sorted(self.feat, key=lambda k: (k != 'hub', k))]
        tc = self.teach
        rec = dict(board=tc['board'], label=tc['label'], left=round(tc['until'] - now, 1), n=tc.get('n', 0)) \
            if tc and now <= tc['until'] else None
        m = getattr(self, 'teach_model', None)
        ms = getattr(self, 'model_sent', None)
        return dict(boards=boards, counts=mltrain.counts(rows), rec=rec, sending=sorted(self.mlw_q),
                    sent=dict(at=ms['at'], to=ms['to'], labels=ms['labels'], done=not self.mlw_q) if ms else None,
                    model=dict(score=m['score'], labels=m['labels'], n=m['n']) if m else None)

    def _where_words(self):
        """'someone near board 3 (within 6 ft)' for the Pi's best-tracked person, or ''."""
        ppl = sorted((p for p in self.tracker.people if p.est), key=lambda p: p.est['r'])
        if not ppl:
            return ''
        e = ppl[0].est
        nodes = {n['key']: n for n in self._layout().get('nodes', [])}
        spots = [('the hub', 0.0, 0.0)] + [(f"board {self.board_label(k[4:])}" if self.board_label(k[4:]).isdigit()
                                            else self.board_label(k[4:]), n['x'], n['y'])
                                           for k, n in nodes.items() if k.startswith('sat:')]
        name, d = min(((nm, math.hypot(x - e['x'], y - e['y'])) for nm, x, y in spots), key=lambda v: v[1])
        unit = self._layout().get('unit', 'ft')
        r = f"{e['r'] / 0.3048:.0f} ft" if unit == 'ft' else f"{e['r']:.0f} m"
        many = f"{len(ppl)} people · " if len(ppl) > 1 else ''
        return f"{many}someone {'near' if d < 3 else 'closest to'} {name} (within {r})"

    TRACK_LOG_MAX = 40 * 1024 * 1024             # the tracking log rolls over at this size (one .old kept)

    def _track_log(self, now, lv):
        """Once a second, what the tracking saw and decided (sessions/track.jsonl): the
        links' levels, the boards' learned verdicts, where people were put. For tuning
        the tracking on this house's own data; private, kept on the Pi."""
        if not self.sessions_dir or now - getattr(self, '_tlog_at', 0.0) < 1.0:
            return
        self._tlog_at = now
        p = os.path.join(self.sessions_dir, 'track.jsonl')
        rec = dict(t=round(now, 2), links={k: round(v, 3) for k, v in lv.items()},
                   ml={k: v['p'] for k, v in self.mlb.items() if now - v['at'] <= 10},
                   people=[[round(q.est['x'], 2), round(q.est['y'], 2), round(q.est['r'], 2)] for q in self.tracker.people if q.est],
                   near=self.tracker.near['key'] if self.tracker.near else None,
                   house=bool(self.house_now[0]) if getattr(self, 'house_now', None) else None)
        try:
            if os.path.exists(p) and os.path.getsize(p) > self.TRACK_LOG_MAX:
                os.replace(p, p + '.old')
            with open(p, 'a') as f:
                f.write(json.dumps(rec, separators=(',', ':')) + '\n')
        except OSError:
            pass

    def _people(self):
        """For the page: the Pi's tracking (works with no page open), the zones, and
        who's still but breathing near a board (presence)."""
        nk = self.tracker.near['key'] if self.tracker.near else None
        now = time.time()
        still = {k: dict(level=b['level'], rate=b['rate'], name='Hub' if k == 'hub' else self.board_label(k[4:]))
                 for k, b in self.breath.items() if now - b['at'] <= 10 and b['level'] != 'none'}
        cells = sorted(self.today[1].items(), key=lambda kv: -kv[1])[:2000]
        return dict(people=[p.view() for p in self.tracker.people if p.est], near=nk,
                    your_side=now - (getattr(self.tracker, 'outside_at', None) or 0) < 4,
                    zones={k: v for k, v in self.zone_in.items()}, breathing=still,
                    model_err=round(self.tracker.model_err, 2),
                    learned={k: dict(motion=v['motion'], p=v['p']) for k, v in self.mlb.items() if now - v['at'] <= 10},
                    autofit=dict(self.autofit_state, paused=sorted(k for k, at in self.user_touch.items() if now - at < 86400)),
                    today=dict(cs=0.5, cells=[[c[0], c[1], n] for c, n in cells]))

    def _picker_step(self):
        """Once a second: how each board -> device link behaved (over there, your
        side, quiet); saved every 5 min next to the map."""
        now = time.time()
        last = getattr(self, '_picker_at', 0.0)
        if now - last < 1.0:
            return
        dt = min(5.0, now - last) if last else 1.0
        self._picker_at = now
        p = self._map_path()
        lp = os.path.join(os.path.dirname(p), 'links.json') if p else None
        if not self._picker_loaded:
            self._picker_loaded = True
            try:
                with open(lp) as f:
                    self.picker.from_json(json.load(f))
            except (OSError, ValueError, TypeError):
                pass
        m = self._layout()
        wz = next((z for z in m.get('zones', []) if z.get('watch')), None)
        if not wz:
            return
        mine = now - (getattr(self.tracker, 'outside_at', None) or 0) < 3
        over = not mine and (self.zone_in.get(wz['id'], 0) > 0 or bool(self.tracker.people))
        hub = (self.cfg['hub'] or self.s.me or '').lower()
        links = {}
        for sat, lk in self.sat_links.items():
            off = self.picker_off.get(sat, {})
            for dev, ln in lk.items():
                if ln.flags & 2 or dev in self.sats or dev == hub or dev in off:
                    continue                             # routers and boards always kept; dropped ones not 'dead'
                fresh = ln.at is not None and now - ln.at <= SAT_GONE_S
                links[(sat, dev)] = bool(ln.moving) if fresh else None
        self.picker.observe(links, over, mine, dt)
        if lp and now - getattr(self, '_picker_saved', 0.0) > 300:
            self._picker_saved = now
            try:
                tmp = lp + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(self.picker.to_json(), f)
                os.replace(tmp, lp)
            except OSError:
                pass

    AUTOFIT_EVERY_S = 900

    def _autofit_step(self):
        """Every 15 min, in the background: the routers that may move (roughly placed
        or automatic, inside the watched area) go where signal strength and everyday
        movement say they fit best. A router you moved in the last day is left alone."""
        now = time.time()
        if self._autofit_busy or not (self.send or self.demo) or now - getattr(self, '_autofit_at', 0.0) < self.AUTOFIT_EVERY_S:
            return
        self._autofit_at = now
        layout = json.loads(json.dumps(self._layout()))
        if not any(z.get('watch') for z in layout.get('zones', [])):
            return
        snaps, heard = list(self.snaps)[-120:], self._hearings()
        skip = set()                             # your circle bounds it: the fit works inside where you put it
        self._autofit_busy = True

        def run():
            try:
                moved, note = autofit.fit(layout, snaps, lambda k, over: self._ends_for(layout, k, over), skip, heard)
                with self.lock:
                    self.autofit_state = dict(at=time.time(), note=note, moved={})
                    if moved:
                        cur = self.map_load()
                        edits = []
                        for n in cur.get('nodes', []):
                            if n['key'] in moved and not n.get('placed'):
                                x, y = moved[n['key']]
                                self.autofit_state['moved'][n['key']] = round(math.dist((x, y), (n['x'], n['y'])), 1)
                                n.update(x=x, y=y, fit=[round(x, 2), round(y, 2)])   # the Pi's spot; your hint stays yours
                                edits.append('node:' + n['key'])
                        if edits:
                            self._merge_auto = True
                            try:
                                self.map_save(dict(cur, edits=edits), who='the automatic router fit')
                            finally:
                                self._merge_auto = False
                            print(f"routers fitted automatically: {note}", flush=True)
            except Exception as e:                       # never take the dashboard down
                print(f"automatic router fit: {e}", file=sys.stderr)
            finally:
                self._autofit_busy = False
        threading.Thread(target=run, daemon=True).start()

    def _ends_for(self, layout, key, over):
        """A link end's spots for the fit: a candidate spot when it's being tried, else as tracking uses it."""
        if key in over:
            return [End(*over[key], 0.0)]
        nodes = {n['key']: n for n in layout.get('nodes', [])}
        fh, hub_fl = layout.get('floor_h', 3.0), layout.get('hub_floor', 1)
        if key == 'hub':
            return [End(0, 0, 0, layout.get('hub_facing'))]
        n = nodes.get(key)
        if not n:
            return None
        z = ((n.get('floor') or hub_fl) - hub_fl) * fh
        if n.get('placed') or key.startswith('sat:'):
            return [End(n['x'], n['y'], z, n.get('facing'))]
        cx, cy = (n['hint'][0], n['hint'][1]) if n.get('hint') else (n['x'], n['y'])
        return [End(cx, cy, z)]

    def _peer_step(self):
        """Links between the boards: each satellite pokes up to PEER_POKE_MAX boards it
        hears well (other satellites first, then the hub), asked through the hub one
        command at a time. A board link has both ends exactly where you placed them:
        the map's most trustworthy kind. Needs firmware that takes POKE from the air."""
        if not self.send or not self.cfg.get('peer_links', True):
            return
        now = time.time()
        if now - getattr(self, '_peer_at', 0.0) < 5:
            return
        self._peer_at = now
        hub = (self.cfg['hub'] or self.s.me or '').lower()
        live = {m for m, d in self.sats.items() if now - d['at'] <= SAT_GONE_S and d.get('via') != 'usb'}
        boards = live | ({hub} if hub else set())
        m = self._layout()
        wz = next((z for z in m.get('zones', []) if z.get('watch')), None)
        targets = None
        if wz:                                           # devices you placed inside the watched area
            targets = {n['key'] for n in m.get('nodes', []) if MAC_RE.fullmatch(n['key'])    # placed, rough or auto:
                       and in_zone(wz, *(n['hint'][:2] if n.get('hint') else (n['x'], n['y'])))}  # in that house is enough
        for sat in sorted(live):
            if 'sat:' + sat in self.mlw_q:
                continue                                 # a model is on its way: not now
            c = self.sat_cmds.get(sat)
            if c and c.get('state') in ('sent', 'queued') and now - c.get('at', 0) < 60:
                continue                                 # one command at a time
            heard = self.surveys.get(sat, {})
            if targets is not None:                      # a watched area: every slot used, the devices worth it learned (linkpick)
                devs = {m: e['rssi'] for m, e in heard.items() if e['kind'] == 'device' and now - e['at'] < 60
                        and m not in boards and m not in self.sats}
                lk = self.sat_links.get(sat, {})
                linked = {m for m, ln in lk.items() if not (ln.flags & 2) and m not in boards and m not in self.sats
                          and ln.at is not None and now - ln.at < 300 and m not in self.picker_off.get(sat, {})}
                pinned = (set(self.s.devices.manual) if self.s.devices.mode == 'manual' else set()) & set(devs)   # your picks on the Devices page: on every board that hears them
                act = self.picker.choose(sat, devs, linked, pinned)
                if act:
                    verb, dev = act
                    off = self.picker_off.setdefault(sat, {})
                    if verb == 'unpoke':
                        off[dev] = now
                    else:
                        off.pop(dev, None)
                    line = f"{'POKE' if verb == 'poke' else 'UNPOKE'},{dev}"
                    self.s.outbox.append(f'SATCMD,{sat},{line}')
                    self.sat_cmds[sat] = dict(line=line, state='sent', at=now)
                    tag = lambda m: f"{self.s.devices.name(m) or 'device'}#{zlib.crc32(m.encode()) % 1000:03d}"
                    print(f"{self.board_label(sat)}: {'trying a link to' if verb == 'poke' else 'dropping its link to'} {tag(dev)} "
                          f"(links now: {', '.join(sorted(tag(m) for m in linked)) or 'none'}; reporting: "
                          f"{', '.join(sorted(tag(m) + ('/r' if ln.flags & 2 else '') + ('' if ln.at and now - ln.at < 300 else '/old') for m, ln in lk.items()))})",
                          flush=True)
                continue
            else:
                cands = sorted((m for m, e in heard.items() if e['kind'] == 'board' and m in boards and m != sat
                                and now - e['at'] < 60 and e['rssi'] > PEER_MIN_RSSI),
                               key=lambda m: (m == hub, -heard[m]['rssi']))
                linked = {m for m in self.sat_links.get(sat, {}) if m in boards}
            for other in [m for m in cands if m not in linked][:max(0, PEER_POKE_MAX - len(linked))]:
                n, at = self.peer_tries.get((sat, other), (0, 0.0))
                if n >= PEER_TRIES or now - at < PEER_RETRY_S:
                    continue
                self.peer_tries[(sat, other)] = (n + 1, now)
                self.s.outbox.append(f'SATCMD,{sat},POKE,{other}')
                self.sat_cmds[sat] = dict(line=f'POKE,{other}', state='sent', at=now)
                print(f"{self.board_label(sat)}: asked to link with "
                      f"{'the hub' if other == hub else self.board_label(other) if other in boards else self.s.devices.name(other) or 'a device'}",
                      flush=True)
                break

    def _switch_network(self, line, what):
        """A new network for the hub: the other boards are told first (through
        the hub, from the air) and the hub switches once they confirmed, or
        after FOLLOW_WAIT_S (a board with older firmware never confirms)."""
        now = time.time()
        boards = [m for m, d in self.sats.items() if now - d['at'] <= SAT_GONE_S and d.get('via') != 'usb']
        if not boards:
            self.s.outbox.append(line)
            return f'{what}: the board restarts (about 15 s)'
        for m in boards:
            self.s.outbox.append(f'SATCMD,{m},{line}')
            self.sat_cmds[m] = dict(line=line, state='sent', at=now)
        self.follow = dict(line=line, boards=boards, at=now)
        names = ', '.join(self.board_label(m) for m in boards)
        return f'{what}: telling {names} first, then the hub restarts (up to {FOLLOW_WAIT_S} s)'

    def _follow_step(self):
        f = self.follow
        if not f:
            return
        states = {m: self.sat_cmds.get(m, {}).get('state') for m in f['boards']}
        if all(st in ('done', 'failed', 'error') for st in states.values()) or time.time() - f['at'] > FOLLOW_WAIT_S:
            self.follow = None
            self.s.outbox.append(f['line'])
            ok = [self.board_label(m) for m, st in states.items() if st == 'done']
            bad = [self.board_label(m) for m, st in states.items() if st != 'done']
            self._event('network', ('Boards switched too: ' + ', '.join(ok) if ok else 'No board confirmed') +
                        (f" · not confirmed: {', '.join(bad)} (older firmware? reflash it)" if bad else '') +
                        ' · the hub switches now', 'hub', important=True)

    def _satcmd_line(self, line):
        """SATCMD,<board>,queued|done|failed|error,<n or why> from the hub."""
        p = line.split(',')
        if len(p) >= 3 and p[1] == 'error':          # the hub couldn't read the command at all
            print(f"hub: board command refused ({p[2]})", flush=True)
            for m, c in self.sat_cmds.items():
                if c.get('state') == 'sent':
                    c['state'], c['why'] = 'error', p[2]
            return
        if len(p) < 3 or not MAC_RE.fullmatch(p[1]):
            return
        mac, st = p[1].lower(), p[2]
        if st not in ('queued', 'done', 'failed', 'error'):
            return
        print(f"{self.board_label(mac)}: command {st} {p[3] if len(p) > 3 else ''}", flush=True)   # the add-on's log
        c = self.sat_cmds.setdefault(mac, dict(line='', at=time.time()))
        c['state'], c['why'] = st, p[3] if len(p) > 3 else ''
        if ('sat:' + mac) in self.mlw_q and st in ('done', 'failed', 'error'):
            self._mlw_step_sat(mac, st == 'done')
            return
        if st in ('done', 'failed', 'error') and not self.follow:
            self._event('health', f"{self.board_label(mac)}: " + {
                'done': f"done ({c.get('line', '').split(',')[0].lower() or 'command'})",
                'failed': 'never confirmed the command (older firmware? reflash it)',
                'error': f"command refused by the hub ({c['why']})"}[st], 'sat:' + mac, important=st != 'done')

    def tick(self):
        """Once a second from a timer, so the light still goes off when no data
        comes, and the board keeps hearing that a device link sees motion."""
        with self.lock:
            self._sats_gone()
            self._follow_step()
            self._peer_step()
            self._autofit_step()
            self._picker_step()
            try:
                self._track_step()
            except Exception as e:           # tracking must never stop the dashboard
                print(f"tracking: {e}", file=sys.stderr)
            self._light_rule()
            self._flush()

    def _src_key(self, src):
        """A link's key as the Map page and the alerts list name it."""
        if src < MAX_APS:
            return f'ap:{src}'
        for mac, i in self.s.devices.poking.items():
            if i == src:
                return mac
        return None

    def _alerting(self):
        """Device links you ticked that see motion now. One counts once its slow
        (4 s) window has confirmed the motion, and until that motion ends, so a
        lone spike on a noisy link never sets off the light or the buzzer."""
        out = []
        if not self.cfg['per_link'] or not self.cfg['alerts']:
            self.confirmed.clear()
            return out
        for src, ln in self.links.items():
            if src < MAX_APS or self._src_key(src) not in self.cfg['alerts']:
                self.confirmed.pop(src, None)
                continue
            md = ln['md']
            if not md.motion:
                self.confirmed[src] = False
            elif md.det.m_long:
                self.confirmed[src] = True
            if md.motion and self.confirmed.get(src):
                out.append(src)
        return out

    def _light_rule(self):
        """The alerts: the light (on the PC) and the board's LED and buzzer (EXT).
        Only a live board drives them: demo or file input (no board to send to)
        never does, or a demo's fake motion would switch a real plug. The board's
        own verdict counts, plus the device links you ticked."""
        motion, by = self._house()
        if not self.send and not self.demo:
            return
        now_m = time.monotonic()
        if any(b != 'board' for b in by):            # another board sees motion: held a while, so a
            self.others_until = now_m + OTHERS_HOLD_S  # burst of movement is one alert, not ten
        ext = bool(self._alerting()) or now_m < getattr(self, 'others_until', 0.0)
        was = self.light.on
        board_words = [('Hub' if b == 'board' else 'board ' + self.board_label(b)) for b in by]
        # Zones that switch the light (Map page): then only motion in them counts for
        # the light (the alerts and the boards' LED and buzzer keep the house verdict)
        lz = [z for z in self._layout().get('zones', []) if z.get('light')]
        if lz:
            light_by = [z['name'] for z in lz if self.zone_in.get(z['id'])]
            self.light.update(bool(light_by))
        else:
            light_by = board_words or (['a device link'] if ext else [])
            self.light.update(motion or ext)
        names = board_words
        where = (self._where_words() if motion or ext else '') or ', '.join(names) or ('a device link' if ext else '')
        if motion or ext:
            paths = self._paths_now()
            if paths:
                where = (where + ' · ' if where else '') + 'seen on ' + ', '.join(paths)
        if self.notify.update(motion or ext, where):
            self._event('automation', 'Phone notified · motion', 'notify')
        if self.light.on != was and self.light.on is not None and self.light.cfg.get('enabled'):
            src = ', '.join(light_by) or 'a device link'
            self._event('automation', f"Light on · motion at {src}" if self.light.on else
                        ('Light off · the zones are still' if lz else 'Light off · the house is still'),
                        'light', important=True)
        now = time.monotonic()
        if ext and (not self.ext_sent or now - self.ext_at >= EXT_EVERY_S):
            self.s.outbox.append('EXT,1')
            self.ext_sent, self.ext_at = True, now
        elif not ext and self.ext_sent:
            self.s.outbox.append('EXT,0')
            self.ext_sent = False

    def _house(self):
        """Every board's own verdict combined by the rule you chose (boards.House).
        One board alone: its verdict, exactly as before."""
        now = time.time()
        off = set(self.cfg['house_off']) if self.sats else set()   # alone, the hub always counts
        states = {} if 'board' in off else {'board': self._motion_now()}
        for mac, d in self.sats.items():
            if now - d['at'] <= SAT_GONE_S and d['calibrated'] and mac not in off:
                states[mac] = bool(d['motion'])
        self.house.update(states, now)
        motion, by = self.house.verdict(self.cfg['house_rule'], now, len(states))
        if len(states) > 1 or len(self.sats):
            self._period('house', motion, now, 'House')
        self.house_now = (motion, by)
        return motion, by

    def _map_boards(self):
        """For the Map: every other board as a point, with its link to its own
        router. A router the hub also watches shares the hub's dot (r:<bssid>)."""
        now = time.time()
        out = []
        for mac, d in sorted(self.sats.items()):
            gone = now - d['at'] > SAT_GONE_S
            b = d.get('bssid')
            if b:
                rkey, rname = f'r:{b}', self._router_label(b)
            else:
                rkey = rname = None
            now_l = []
            for tmac, ln in sorted(self.sat_links.get(mac, {}).items()):
                if ln.at is None or now - ln.at > SAT_GONE_S:
                    continue
                tkey, tname = self._target_key(tmac, ln.flags)
                now_l.append(dict(key=tkey, name=tname, moving=ln.moving and not gone,
                                  level=round(ln.level(), 2), router=bool(ln.flags & 2)))
            out.append(dict(key='sat:' + mac, label=self.board_label(mac), links=now_l,
                            state='gone' if gone else 'calibrating' if not d['calibrated'] else
                            'motion' if d['motion'] else 'still',
                            rssi=d.get('rssi') or None, router=rkey, router_name=rname,
                            router_rssi=d.get('router_rssi') or None, level=round(max(d['k_short'], d['k_long']), 2),
                            via=d.get('via', 'air')))
        return out

    def _hearings(self):
        """Who hears whom, how strongly: {target key: [[observer key, dBm], ...]}.
        The Map places what you haven't (routers, devices, unplaced boards)
        from these and the boards you placed. Observers: 'hub' (its own links
        and the boards' hellos) and 'sat:<mac>' (their surveys, every 10 s)."""
        now = time.time()
        out = {}
        add = lambda key, by, rssi: rssi and -100 < rssi < 0 and key != by and \
            out.setdefault(key, []).append([by, int(rssi)])
        for r in self._radio():
            add(r.get('node') or r['key'], 'hub', r['rssi'])
        for mac, d in self.sats.items():
            if now - d['at'] > SAT_GONE_S:
                continue
            add('sat:' + mac, 'hub', d.get('rssi'))
            for tmac, e in self.surveys.get(mac, {}).items():
                if now - e['at'] > 120:
                    continue
                key, _nm = self._target_key(tmac, 2 if e['kind'] == 'router' else 0)
                add(key, 'sat:' + mac, e['rssi'])
        return out

    def _sat_cmd_view(self, mac):
        """The last command sent to a board through the hub, for the page."""
        c = self.sat_cmds.get(mac)
        if not c:
            return None
        return dict(what=c.get('line', '').split(',')[0].lower(), state=c.get('state'), age=round(time.time() - c['at']))

    def _house_slow(self):
        motion, by = self.house_now
        off = set(self.cfg['house_off'])
        sources = [dict(key='board', name='Hub', use='board' not in off)] + \
            [dict(key=m, name=self.board_label(m), use=m not in off, cmd=self._sat_cmd_view(m)) for m in sorted(self.sats)]
        return dict(motion=motion, by=['Hub' if b == 'board' else self.board_label(b) for b in by],
                    sources=sources, rule=self.cfg['house_rule'], rules={k: HOUSE_TEXT[k] for k in HOUSE_RULES},
                    boards=1 + sum(1 for d in self.sats.values() if time.time() - d['at'] <= SAT_GONE_S))

    def _debug_line(self, line):
        t = self._now()
        self.tail.append((t, line[:300]))
        if line.startswith('REFUSED'):
            last = self.sent_cmds[-1][1] if self.sent_cmds else ''
            self.refused.append((t, line[:120], last))
        elif line.startswith('CALIB,'):
            c = self.s.board_calib
            if c and line.startswith('CALIB,') and (
                    not self.calibs or (self.calibs[-1]['phase'], self.calibs[-1]['why']) != (c['phase'], c['why'])):
                self.calibs.append(dict(t=r2(t), phase=c['phase'], why=c['why'], floor=c['floor']))
                prev = self.calibs[-2]['phase'] if len(self.calibs) > 1 else None
                if c['phase'] in ('wait', 'collect') and prev not in ('wait', 'collect'):
                    self._event('calib', f"Calibrating ({c['why']})", 'hub')
                elif c['phase'] in ('fast', 'track') and prev in ('wait', 'collect'):
                    self._event('calib', 'Calibrated, watching', 'hub')

    def _flush(self):
        # Taken off the outbox first: a command that fails (serial unplugged, a
        # bad character) is reported and dropped, never retried on every line
        cmds, self.s.outbox[:] = list(self.s.outbox), []
        for cmd in cmds:
            if not self.send:
                continue
            try:
                self.send(cmd)
            except Exception as e:
                print(f"command not sent ({redact(cmd)[:40]!r}): {e}", file=sys.stderr)
                self.refused.append((self._now(), f'not sent: {e}'[:120], redact(cmd)))
                continue
            self.sent_cmds.append((self._now(), redact(cmd)))

    # ---------- commands from the page ----------
    def command(self, c):
        """Run one command dict; returns (ok, message)."""
        what = c.get('cmd')
        board = c.get('board')
        if board is not None and board != self.primary and board != self.s.me:
            return self._board_command(board, c)
        with self.lock:
            if what == 'cal':
                if not self.send:
                    return False, 'recalibrate needs a board (this is a demo or a file)'
                self.s.outbox.append('CAL')
                msg = 'Recalibrating: step away, the quiet level is measured again in 5 s'
            elif what == 'scan':
                if not self.send:
                    return False, 'scanning needs a board'
                if self.s.scanning():
                    return True, 'Already scanning'
                self.s.start_scan()
                msg = 'Scanning all channels (about 4 s)'
            elif what == 'use':
                name = c.get('name', '')
                if not isinstance(name, str) or not 1 <= len(name.encode()) <= 32 \
                        or any(ch in name for ch in '\r\n'):
                    return False, 'bad network name'
                if not self.send:
                    return False, 'choosing a network needs a board'
                msg = self._switch_network(f'USE,{name}', f'Switching to "{name}"')
            elif what == 'aps':
                macs = c.get('macs')
                if not isinstance(macs, list) or not 1 <= len(macs) <= MAX_APS:
                    return False, f'pick 1 to {MAX_APS} access points'
                if not all(isinstance(m, str) and MAC_RE.fullmatch(m) for m in macs):
                    return False, 'bad access point address'
                macs = [m.lower() for m in macs]
                if len(set(macs)) != len(macs) or any(int(m[:2], 16) & 1 for m in macs):
                    return False, 'bad access point list'
                if not self.send:
                    return False, 'choosing access points needs a board'
                msg = self._switch_network('APS,' + ','.join(macs),
                                    f"Watching {len(macs)} chosen access point{'s' if len(macs) > 1 else ''}")
            elif what == 'aps_clear':
                if not self.send:
                    return False, 'choosing access points needs a board'
                msg = self._switch_network('APS,clear', 'Back to every access point of the network')
            elif what == 'use_default':
                if not self.send:
                    return False, 'choosing a network needs a board'
                msg = self._switch_network('USE_DEFAULT', 'Back to the default network')
            elif what == 'net_sync':                     # the other boards onto the hub's network, the hub stays
                st = self.s
                if st.ap_list:
                    line = 'APS,' + ','.join(st.ap_list)
                elif st.network and st.network[0] in ('default', 'chosen') and st.network[1]:
                    line = f'USE,{st.network[1]}'
                else:
                    return False, "the hub hasn't said its network yet"
                now = time.time()
                boards = [m for m, dd in self.sats.items() if now - dd['at'] <= SAT_GONE_S and dd.get('via') != 'usb']
                if not boards or not self.send:
                    return False, 'no other board heard right now'
                for m in boards:
                    self.s.outbox.append(f'SATCMD,{m},{line}')
                    self.sat_cmds[m] = dict(line=line, state='sent', at=now)
                msg = (f"Sending the hub's network to {', '.join(self.board_label(m) for m in boards)}: each restarts on it "
                       '(about 15 s); the hub stays as it is')
            elif what == 'house_use':                    # a board counts for the house, or not
                key, use = c.get('key'), c.get('use')
                if not isinstance(key, str) or not (key == 'board' or MAC_RE.fullmatch(key)) \
                        or not isinstance(use, bool):
                    return False, 'bad board'
                key = key.lower()
                off = set(self.cfg['house_off'])
                (off.discard if use else off.add)(key)
                if len(off) > SAT_MAX + 1:
                    return False, 'too many boards'
                self.cfg['house_off'] = sorted(off)
                self._save_settings()
                name = 'The hub' if key == 'board' else self.board_label(key)
                msg = f"{name} {'counts' if use else 'no longer counts'} for the light and alerts"
            elif what == 'house_rule':
                if c.get('rule') not in HOUSE_RULES:
                    return False, 'bad rule'
                self.cfg['house_rule'] = c['rule']
                self._save_settings()
                msg = f"Motion when {HOUSE_TEXT[c['rule']]}"
            elif what == 'settings':
                changed = {k: bool(v) for k, v in c.items() if k in SETTINGS}
                if not changed:
                    return False, 'nothing to change'
                self.cfg.update(changed)
                if not self.cfg['per_link']:
                    self._drop_links()               # free the analysers
                self._save_settings()
                msg = 'Per-link analysis on (experimental)' if self.cfg['per_link'] \
                    else 'Per-link analysis off'
            elif what == 'sound':
                if not self.send:
                    return False, 'the buzzer needs a board'
                if c.get('test'):
                    self.s.outbox.append('SOUND,test')
                    msg = 'Playing the motion sound'
                elif c.get('mode') == 'default':
                    self.s.want_sound(None)
                    self.cfg['sound'] = None
                    self._save_settings()
                    msg = "Buzzer back to the firmware's settings"
                else:
                    try:
                        self.s.want_sound(c.get('mode'), int(c.get('vol')), int(c.get('hz')))
                    except (TypeError, ValueError):
                        return False, 'bad buzzer setting'
                    self.cfg['sound'] = list(self.s.sound_want)
                    self._save_settings()
                    msg = 'Buzzer off' if c['mode'] == 'off' else f"Buzzer: {c['mode']}"
            elif what == 'sens':
                if not self.send:
                    return False, 'sensitivity needs a board'
                x = c.get('x100')
                if x == 'default':
                    self.s.want_sens(None)
                    self.cfg['sens'] = None
                    msg = "Sensitivity back to the firmware's"
                else:
                    if x is None:                # only 'default' resets it
                        return False, 'bad sensitivity'
                    try:
                        self.s.want_sens(x)      # a whole number, 110-300
                    except ValueError:
                        return False, 'bad sensitivity'
                    self.cfg['sens'] = self.s.sens_want
                    msg = f'Long-window threshold x{self.s.sens_want / 100:.1f}'
                self._save_settings()
            elif what == 'router_name':
                bssid, name = c.get('bssid'), c.get('name')
                if not isinstance(bssid, str) or not MAC_RE.fullmatch(bssid):
                    return False, 'bad router address'
                if not isinstance(name, str):
                    return False, 'bad name'
                name = ' '.join(name.split())
                names = self.cfg['router_names']
                if not name:
                    names.pop(bssid.lower(), None)
                    msg = 'Name removed'
                elif not _router_nick(name):
                    return False, f'a name is 1-{ROUTER_NAME_MAX} characters'
                elif bssid.lower() not in names and len(names) >= ROUTER_NAMES_MAX:
                    return False, 'too many router names: remove one first'
                else:
                    names[bssid.lower()] = name
                    msg = f'Router named "{name}"'
                self._save_settings()
            elif what == 'board_name':
                mac, name = c.get('mac'), c.get('name')
                if not isinstance(mac, str) or not MAC_RE.fullmatch(mac):
                    return False, 'bad board address'
                if not isinstance(name, str):
                    return False, 'bad name'
                name = ' '.join(name.split())
                names = self.cfg['board_names']
                if not name:
                    names.pop(mac.lower(), None)
                    msg = 'Name removed'
                elif not _router_nick(name):
                    return False, f'a name is 1-{ROUTER_NAME_MAX} characters'
                elif mac.lower() not in names and len(names) >= BOARD_NAMES_MAX:
                    return False, 'too many board names: remove one first'
                else:
                    names[mac.lower()] = name
                    msg = f'Board named "{name}"'
                self._save_settings()
            elif what == 'alerts':
                keys = c.get('keys')
                if not isinstance(keys, list) or len(keys) > ALERT_MAX or not all(
                        isinstance(k, str) and MAC_RE.fullmatch(k) for k in keys):
                    return False, 'bad list of device links'
                self.cfg['alerts'] = sorted({k.lower() for k in keys})
                self._save_settings()
                n = len(self.cfg['alerts'])
                msg = (f"Alerts: the board plus {n} device link{'s' if n != 1 else ''}" if n
                       else 'Alerts: the board only')
                if n and not self.cfg['per_link']:
                    msg += ' (switch on per-link analysis for them to count)'
            elif what == 'light':
                try:
                    self.light.set(**{k: c.get(k) for k in ('enabled', 'on_url', 'off_url', 'off_after')})
                except (ValueError, TypeError) as e:
                    return False, str(e) if isinstance(e, ValueError) else 'bad light setting'
                self._save_settings()
                cf = self.light.cfg
                msg = ('Light on motion: off' if not cf['enabled'] else
                       'Light on motion: on, off as soon as the room is still' if not cf['off_after'] else
                       f"Light on motion: on, off after {fmt_age(cf['off_after'])} of stillness")
            elif what == 'teach':                        # record labelled seconds of one board's features
                board, label = c.get('which'), c.get('label')
                secs = c.get('secs', 30)
                if label == 'stop':
                    self.teach = None
                    return True, 'stopped'
                if board not in self.feat or label not in mltrain.LABELS or isinstance(secs, bool) \
                        or not isinstance(secs, int) or not 5 <= secs <= 600:
                    return False, 'pick a board that sends its features (new firmware) and a label'
                self.teach = dict(board=board, label=label, until=time.time() + secs, secs=secs, n=0)
                name = 'the hub' if board == 'hub' else self.board_label(board[4:])
                msg = f"Recording '{label}' on {name} for {secs} s"
            elif what == 'teach_train':                  # train on everything recorded, send to the boards
                if not self.sessions_dir:
                    return False, 'nowhere to keep the recordings'
                got = mltrain.train(mltrain.read_rows(os.path.join(self.sessions_dir, 'ml_labels.jsonl')))
                if 'error' in got:
                    return False, got['error']
                self.teach_model = got
                try:
                    with open(os.path.join(self.sessions_dir, 'ml_house.json'), 'w') as fh:
                        json.dump(got, fh)
                except OSError:
                    pass
                sent = self._send_model(got)
                sc = ', '.join(f"{k} {int(v * 100)}%" for k, v in got['score'].items())
                msg = (f"Trained on {got['n']} s ({sc} right on held-out recordings); "
                       + (f"sending to {', '.join(sent)} ({len(mltrain.mlw_commands(got))} steps each)" if sent else 'no board to send it to right now'))
            elif what == 'teach_send':                   # the saved model (sessions/ml_house.json), to every board again
                try:
                    with open(os.path.join(self.sessions_dir or '', 'ml_house.json')) as fh:
                        got = json.load(fh)
                    assert isinstance(got.get('params'), list) and got.get('no') in (1, 3) and got.get('nh') == 0
                    assert len(got['params']) == mltrain.mlw_param_count(got) and all(
                        isinstance(v, (int, float)) and math.isfinite(v) for v in got['params'])
                except (OSError, ValueError, AssertionError, TypeError):
                    return False, 'no saved model to send'
                self.teach_model = got
                sent = self._send_model(got)
                msg = (f"Sending the saved model ({', '.join(got.get('labels', []))}) to {', '.join(sent)}"
                       if sent else 'no board to send it to right now')
            elif what == 'teach_clear':
                if self.sessions_dir:
                    try:
                        os.replace(os.path.join(self.sessions_dir, 'ml_labels.jsonl'),
                                   os.path.join(self.sessions_dir, f'ml_labels-{int(time.time())}.jsonl.old'))
                    except OSError:
                        pass
                msg = 'Recordings put aside (kept as a .old file)'
            elif what == 'walk_mark':                    # the walk check: where you really are now
                try:
                    x, y = _num(c.get('x'), -10000, 10000), _num(c.get('y'), -10000, 10000)
                except ValueError:
                    return False, 'bad spot'
                ppl = [p for p in self.tracker.people if p.est]
                near = min(ppl, key=lambda p: math.hypot(p.est['x'] - x, p.est['y'] - y)) if ppl else None
                err = math.hypot(near.est['x'] - x, near.est['y'] - y) if near else None
                links, _m = self._track_links()
                rec = dict(t=round(time.time(), 2), x=round(x, 2), y=round(y, 2),
                           est=[round(near.est['x'], 2), round(near.est['y'], 2)] if near else None,
                           links={l['key']: l['level'] for l in links if l['level'] is not None})
                if self.sessions_dir:
                    try:
                        with open(os.path.join(self.sessions_dir, 'walks.jsonl'), 'a') as f:
                            f.write(json.dumps(rec) + '\n')
                    except OSError:
                        pass
                self._walk_err()
                msg = (f"Marked · the Pi's tracking was {err:.1f} m off [pi {err:.2f}]" if err is not None
                       else 'Marked · the Pi tracks nobody right now')
            elif what == 'out':                          # the hub's own light output pin
                got = out_line(c)
                if not got:
                    return False, 'bad light output setting'
                if not self.send:
                    return False, 'that needs the board'
                self.s.outbox.append(got[0])
                msg = f'Hub: {got[1]}'
            elif what == 'notify':
                try:
                    always = c.get('hours') == 'always'
                    self.notify.set(**{k: (None if k == 'hours' and always else c.get(k))
                                       for k in ('enabled', 'token', 'user', 'quiet_min', 'max_day',
                                                 'hours', 'urgent', 'offline', 'zones_only')}, clear_hours=always)
                except (ValueError, TypeError) as e:
                    return False, str(e) if isinstance(e, ValueError) else 'bad notification setting'
                self._save_settings()
                cf = self.notify.cfg
                msg = ('Phone notifications: off' if not cf['enabled'] else
                       f"Phone notifications: on, at most one per {cf['quiet_min']} min of quiet")
            elif what == 'notify_test':
                return self.notify.test()
            elif what == 'light_test':
                if c.get('what') not in ('on', 'off'):
                    return False, 'bad test'
                return self.light.test(c['what'])
            elif what == 'led':
                if not self.send:
                    return False, 'the LED needs a board'
                if c.get('mode') == 'default':
                    self.s.outbox.append('LEDSET,default')
                    msg = "LED back to the firmware's settings"
                else:
                    color = c.get('color')
                    bright = c.get('bright')
                    if c.get('mode') not in LED_MODES or c.get('pattern') not in LED_PATTERNS \
                            or not isinstance(bright, int) or isinstance(bright, bool) or not 1 <= bright <= 255 \
                            or not isinstance(color, str) or not re.fullmatch(r'#?[0-9a-fA-F]{6}', color):
                        return False, 'bad LED setting'
                    self.s.outbox.append(f"LEDSET,{c['mode']},{bright},{c['pattern']},{color.lstrip('#').lower()}")
                    msg = {'off': 'LED off', 'motion': 'LED: motion only', 'status': 'LED: status'}[c['mode']]
            elif what.startswith('act_'):
                return self._act_command(what, c)
            elif what == 'mode':
                if c.get('mode') not in MODES:
                    return False, 'bad mode'
                self.s.outbox += self.s.devices.set_mode(c['mode'])
                self._save_prefs()
                msg = f"Device mode: {c['mode']}"
            elif what == 'toggle':
                mac = c.get('mac', '')
                if mac not in self.s.devices.devs:
                    return False, 'unknown device'
                self.s.outbox += self.s.devices.toggle(mac)
                self._save_prefs()
                msg = 'Device list updated'
            else:
                return False, 'unknown command'
            self._flush()
            return True, msg

    def _act_command(self, what, c):
        """Activity calibration commands (lock held). Work with demo data too."""
        a = self.act
        label = c.get('label')
        if what in ('act_record', 'act_fix') and (not isinstance(label, str) or not ACT_LABEL_RE.fullmatch(label)):
            return False, 'bad activity name (a-z, 0-9 and _ only)'
        if what == 'act_record':
            if self.s.wall_off is None:
                return False, 'no data yet: wait for the board'
            if a.rec is not None:
                return False, 'already recording: wait for it or cancel'
            info = label_info(label)
            try:
                delay = info['delay'] if c.get('delay') is None else float(c['delay'])
                secs = info['secs'] if c.get('secs') is None else float(c['secs'])
            except (TypeError, ValueError):
                return False, 'bad timing'
            if not (0 <= delay <= 60 and 6 <= secs <= 60):
                return False, 'countdown 0-60 s, recording 6-60 s'
            a.start(label, self.s.now, delay, secs)
            return True, f"{info['name']}: recording starts in {delay:.0f} s"
        if what == 'act_moved':
            self.cfg['act_place'] += 1
            self._save_settings()
            return True, ('Noted: the board or router moved. Movement keeps working; record a short '
                          'still clip of each posture here (sitting, lying, standing) to see postures again')
        if what == 'act_cancel':
            a.cancel()
            return True, 'recording cancelled'
        if what == 'act_delete':
            ok = a.delete(str(c.get('id', '')))
            return (True, 'clip deleted') if ok else (False, 'no such clip')
        if what == 'act_clear':
            if label is not None and (not isinstance(label, str) or not ACT_LABEL_RE.fullmatch(label)):
                return False, 'bad activity name'
            n = sum(1 for x in a.clips if label is None or x['label'] == label)
            a.clear(label)
            return True, f"{n} clip{'s' if n != 1 else ''} deleted"
        if what == 'act_fix':
            clip = a.correct(label, self.s.now)
            if clip is None:
                return False, 'nothing recent to learn from (no usable data in the last 6 s)'
            return True, f"Learned: that was {label_info(label)['name'].lower()} (clip of {len(clip['wins'])} windows)"
        return False, 'unknown command'

    def _activity_fast(self):
        """The live activity, recording progress and recent events (4 times a second)."""
        a, s = self.act, self.s
        r = dict(a.result) if a.result else dict(state='uncalibrated' if not a.clips else 'no-data')
        r.pop('scores', None) if r.get('state') in ('no-data',) else None
        out = dict(now=r, recording=a.recording(s.now), saved=a.last_saved)
        if s.wall_off is not None:
            out['events'] = [dict(e, wall=r2(s.wall(e['t']))) for e in list(a.events)[-5:]]
            if 't' in r:
                r['wall'] = r2(s.wall(r['t']))
        return out

    def _activity_slow(self):
        """Clips by activity, the label list and the recent changes (once a second)."""
        a = self.act
        labels = a.labels()
        known = [dict(key=k, name=v['name'], kind=v['kind'], hint=v['hint'], delay=v['delay'], secs=v['secs'],
                      **{kk: vv for kk, vv in labels.get(k, {}).items() if kk in ('clips', 'last', 'chs')})
                 for k, v in LABELS.items()]
        own = [dict(key=k, name=v['name'], kind=v['kind'], hint='', delay=3, secs=10, clips=v['clips'],
                    last=v['last'], chs=v['chs']) for k, v in labels.items() if k not in LABELS]
        clips = [dict(id=c['id'], label=c['label'], name=label_info(c['label'])['name'], t=r2(c['t']), ch=c['ch'],
                      source=c.get('source', 'recorded'), windows=len(c['wins'])) for c in reversed(a.clips)]
        return dict(labels=known + own, clips=clips[:100], log=[[r2(a), r2(b), st] for a, b, st in list(self.act_log)[-39:]]
                    + ([[r2(self.act_cur[0]), None, self.act_cur[1]]] if self.act_cur else []),
                    channel=a.feat.ch, place=a.place,
                    posture_check={k: list(v) for k, v in a.posture_check.items()},
                    posture_ok=sorted(a.posture_ok))

    def _save_settings(self):
        if self.settings_path:
            try:
                os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
                # The light URLs hold secret tokens: owner-only from the start
                fd = os.open(self.settings_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.fchmod(fd, 0o600)                 # an older file may have been readable
                with os.fdopen(fd, 'w') as f:
                    json.dump(dict(self.cfg, light=self.light.config(), notify=self.notify.config(),
                                   board_secret=self.board_secret), f)
            except OSError:
                pass

    def _save_prefs(self):
        if self.prefs:
            try:
                os.makedirs(os.path.dirname(self.prefs), exist_ok=True)
                self.s.devices.save(self.prefs)
            except OSError:
                pass

    # ---------- output ----------
    def _pick(self, link):
        """(breathing, its history, doppler) for the chosen link: None = the router."""
        ln = self.links.get(link) if self.cfg['per_link'] and link is not None else None
        if ln:
            return ln['br'], ln['hist'], ln['dop']
        return self.s.br, self.s.br_hist, self.s.dop

    def fast(self, win, link=None):
        """Header, tiles, score chart and breathing strip (4 times a second)."""
        with self.lock:
            s = self.s
            br, hist, dop = self._pick(link)
            out = dict(header=self._header(br, dop), have_time=s.wall_off is not None,
                       settings=self.cfg, activity=self._activity_fast())
            if s.wall_off is None:
                return out
            t1 = max(s.now, win)
            t0 = t1 - win
            out['score'] = self._score(t0, t1, win)
            out['breathing'] = [[r2(s.wall(t)), r2(r['rate']), r['level']]
                                for t, r in hist if t >= t0]
            out['window'] = [r2(s.wall(t0)), r2(s.wall(t1))]
            out['filling'] = s.now < win           # the window stands still until it has filled
            return out

    def slow(self, win, link=None):
        """Doppler, session timeline, links, devices, networks, raw counters (once a second)."""
        with self.lock:
            s = self.s
            out = dict(devices=self._devices(), network=self._network(), details=self._details(),
                       routers=self._routers(), boards=self._boards(), house=self._house_slow(), tz=PC_TZ,
                       map_boards=self._map_boards(), hearings=self._hearings(), tracked=self._people(),
                       teach=self._teach_view(),
                       demo_people=list(self.demo_people.values()) if self.demo else None, events=list(self.events)[-80:],
                       board_settings=self._board_settings(), primary=self.primary or self.s.me,
                       pairing=self.pair.status(self.pair.me == self.cfg['hub'], self._hub_boards()),
                       settings=self.cfg, sound=s.sound, sens=s.sens, led=s.led, gates=self._gates(),
                       thresholds=self._thresholds(), today=self._today_totals(),
                       radio=self._radio(), channel=s.channel[0] if s.channel else None,
                       act=self._activity_slow())
            if s.wall_off is None:
                return out
            t1 = max(s.now, win)
            out['doppler'] = self._doppler(t1 - win, t1, self._pick(link)[2])
            out['timeline'] = self._timeline()
            if self.cfg['per_link']:
                out['links'] = self._links(t1 - win, t1)
                out['board_spans'] = self._board_spans(t1 - win, t1)
            return out

    def debug(self, win):
        """The Debug page: router ACK traces, calibration history, board lines,
        commands sent and refused (every 2 s)."""
        with self.lock:
            s = self.s
            out = dict(calibs=list(self.calibs),
                       tail=[[r2(t), line] for t, line in self.tail],
                       sent=[[r2(t), c] for t, c in self.sent_cmds],
                       refused=[[r2(t), line, last] for t, line, last in self.refused])
            if s.wall_off is None:
                return out
            t1 = max(s.now, win)
            t0 = t1 - win
            pts = [p for p in s.trace if p[0] >= t0]
            traces = []
            for node in sorted({p[1] for p in pts}):
                mine = [p for p in pts if p[1] == node]
                t = np.array([p[0] for p in mine])
                bins = np.floor((t - t0) / max(win / 600, 0.05)).astype(int)
                rows = {}
                for b, (_t, _n, j, g) in zip(bins, mine):   # each bin's largest jump and gap
                    r = rows.setdefault(b, [_t, j, g])
                    r[1], r[2] = max(r[1], j), max(r[2], g)
                rows = [rows[k] for k in sorted(rows)]
                traces.append(dict(node=int(node), label=s.devices.link_label(int(node)),
                                   color=COLORS[(int(node) * 2 + 1) % len(COLORS)],
                                   t=[r2(s.wall(r[0])) for r in rows], jump=[round(r[1], 3) for r in rows],
                                   gap=[round(r[2], 1) for r in rows]))
            c = s.det.c
            out.update(window=[r2(s.wall(t0)), r2(s.wall(t1))], traces=traces,
                       glitch=c['glitch_shape'], scan_shape=c['scan_shape'],
                       scan_gap_ms=c['scan_gap_us'] / 1e3)
            return out

    def stats(self):
        """The Stats page: past sessions (sessions/events-*.log) plus this one."""
        with self.lock:
            live = self._live_session()
        past = self.sessions.load(exclude=self.log_name) if self.sessions else []
        return summarize(past, live)

    def _live_session(self):
        s = self.s
        end = s.last_wall or time.time()
        start = min([self.started] + [a for a, _b in s.hist_motion])
        return dict(start=start, end=end, path=self.log_name or 'live session',
                    motion=[(a, b if b is not None else end) for a, b in s.hist_motion])

    def _today_totals(self):
        when, val = self._today
        if val is None or time.time() - when >= TODAY_S:
            past = self.sessions.load(exclude=self.log_name) if self.sessions else []
            val = summarize(past, self._live_session())['today']
            self._today = (time.time(), val)
        return val

    # ---------- the Map page ----------
    def _map_path(self):
        return os.path.join(os.path.dirname(self.settings_path), 'map.json') if self.settings_path else None

    def _plan_path(self, pid):
        return os.path.join(os.path.dirname(self.settings_path), f'map-{pid}.img') if self.settings_path else None

    @staticmethod
    def plan_type(data):
        """The picture's type from its first bytes (PNG, JPEG or WebP), else None."""
        for magic, ctype in PLAN_MAGIC:
            if data.startswith(magic):
                return ctype
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return 'image/webp'
        return None

    def plan_save(self, pid, data):
        """Store (or with empty data, delete) map picture `pid`; (ok, message)."""
        if pid not in PLAN_IDS:
            return False, 'bad picture slot'
        p = self._plan_path(pid)
        if not p:
            return False, 'nowhere to save the picture (no sessions folder)'
        if not data:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError as e:
                return False, f'could not remove the picture ({e.strerror})'
            return True, 'picture removed'
        if len(data) > PLAN_MAX:
            return False, 'picture too big (12 MB at most)'
        if not self.plan_type(data):
            return False, 'not a PNG, JPEG or WebP picture'
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(p + '.tmp', p)
        except OSError as e:
            return False, f'could not save the picture ({e.strerror})'
        return True, 'picture saved'

    def plan_load(self, pid):
        """(bytes, content type) of map picture `pid`, or None."""
        p = self._plan_path(pid) if pid in PLAN_IDS else None
        try:
            with open(p, 'rb') as f:
                data = f.read(PLAN_MAX + 1)
        except (OSError, TypeError):
            return None
        ctype = self.plan_type(data)
        return (data, ctype) if ctype and len(data) <= PLAN_MAX else None

    def map_load(self):
        """The saved layout ({} if none or unreadable)."""
        p = self._map_path()
        try:
            with open(p) as f:
                got = json.load(f)
            return clean_map(got)
        except (OSError, ValueError, TypeError):
            return {}

    MAP_SECTIONS = {'zones': ('zones',), 'track_off': ('track_off',), 'plans': ('plans',), 'unit': ('unit',),
                    'env': ('env',), 'view': ('view',), 'hub': ('hub_floor', 'floor_h', 'hub_facing')}

    def _map_merge(self, new, edits):
        """Several open pages each save the whole layout: merge instead of the last
        writer winning. Only what this page changed (edits) replaces what is stored;
        a node placed by you is never replaced by another page's automatic guess."""
        old = self.map_load()
        if not old:
            return new
        out = dict(old)
        for sec, keys in self.MAP_SECTIONS.items():
            if sec in edits:
                for k in keys:
                    if k in new:
                        out[k] = new[k]
                    else:
                        out.pop(k, None)
        mine = lambda n: bool(n.get('placed') or n.get('hint'))
        nodes = {n['key']: n for n in old.get('nodes', [])}
        for n in new.get('nodes', []):
            ex = nodes.get(n['key'])
            if ex is not None and mine(ex) and not mine(n) and ('node:' + n['key']) not in edits:
                continue                                 # an automatic guess never beats your placement
            if ex is None or ('node:' + n['key']) in edits or (not mine(ex) and not mine(n)):
                if ex is not None and ('node:' + n['key']) in edits and mine(n) and (
                        ex.get('hint') != n.get('hint') or ex.get('placed') != n.get('placed') or
                        (n.get('placed') and (ex['x'], ex['y']) != (n['x'], n['y']))):
                    if not getattr(self, '_merge_auto', False):
                        self.user_touch[n['key']] = time.time()    # you moved it: the automatic fit leaves it a day
                if ex is not None and ex.get('fit') and 'fit' not in n:   # the Pi's fitted spot: kept unless you moved it
                    moved_by_you = (ex.get('hint') != n.get('hint') or ex.get('placed') != n.get('placed'))
                    if not moved_by_you or getattr(self, '_merge_auto', False):
                        n = dict(n, fit=ex['fit'])
                nodes[n['key']] = n
        out['nodes'] = list(nodes.values())[:64]
        return out

    def map_save(self, m, who=''):
        """Validate and store a layout; (ok, message). Written atomically. With
        'edits' (what this page changed), merged into what is stored (_map_merge)."""
        edits = m.pop('edits', None) if isinstance(m, dict) else None
        try:
            m = clean_map(m)
        except ValueError as e:
            return False, str(e)
        before = self.map_load()
        if isinstance(edits, list) and len(edits) <= 200 and all(isinstance(e, str) for e in edits):
            m = self._map_merge(m, set(edits))
        elif edits is None and self.map_load():
            # a page from before the merge (still open somewhere): it may only add what's new,
            # never replace your zones, settings or placements with its old copy
            m = self._map_merge(m, set())
        p = self._map_path()
        if not p:
            return False, 'nowhere to save the map (no sessions folder)'
        if before:                                   # zones or boards left out changed: said in the add-on's log
            zn = lambda mm: sorted(z.get('name', '') for z in mm.get('zones', []))
            if zn(before) != zn(m) or sorted(before.get('track_off', [])) != sorted(m.get('track_off', [])):
                print(f"map: zones {zn(before)} -> {zn(m)}, left out {before.get('track_off', [])} -> "
                      f"{m.get('track_off', [])} (edits: {sorted(edits) if isinstance(edits, list) else 'an old page'}; {who[:60]})",
                      flush=True)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(m, f)
            os.replace(tmp, p)
        except OSError as e:
            return False, f'could not save the map ({e.strerror})'
        return True, 'saved'

    def _radio(self):
        """Every live ACK link (always, not only with per-link analysis): what the
        Map page draws. key: the device's MAC for a poked device, 'ap:<n>' for an
        access point (ap:0 = the router)."""
        s = self.s
        by_idx = {i: mac for mac, i in s.devices.poking.items()}
        out = []
        for (src, kind) in sorted(s.arrivals):
            if kind != 1:
                continue
            rate = s.rate((src, 1))
            if rate <= 0:
                continue
            ap = src < MAX_APS
            key = f'ap:{src}' if ap else by_idx.get(src)
            if key is None:
                continue                         # a device that isn't poked any more
            via = ''
            if not ap:
                d = s.devices.devs.get(key)
                via = s.devices.link_label(d.ap) if d is not None and 0 <= d.ap < MAX_APS else ''
            row = dict(key=key, src=int(src), label=s.devices.link_label(src), ap=ap, via=via,
                       # the map's dot for it: a router by its address, so another
                       # network's router never takes this one's place
                       node='r:' + s.nodes[src].lower() if ap and s.nodes.get(src) else key,
                       rssi=s.rssi.get((src, 1)), rate=round(rate),
                       alerts=key in self.cfg['alerts'],
                       alerting=bool(self.confirmed.get(src)) and src in self.links and self.links[src]['md'].motion)
            ln = self.links.get(src) if self.cfg['per_link'] else None
            if ln:
                md = ln['md']
                now_us = int(s.now * 1e6)
                st = md.state(now_us)
                ms, _ml = md.det.max_scores(now_us)
                f = md.det.floor
                row.update(state=st, moving=st == 'motion',
                           activity=r2(ms / f) if ms is not None and f > 0 and st in ('motion', 'still') else None)
                b = ln['br'].last                    # its own breathing, when it finds one
                if b is not None and b['level'] != 'none':
                    row['breathing'] = dict(rate=r2(b['rate']), level=b['level'])
            out.append(row)
        return out

    def timeline_range(self, hours):
        """Motion periods of the last `hours` (30 s to 7 days), one lane per link:
        the board's verdict first, then routers, then devices."""
        hours = min(max(hours if math.isfinite(hours) else 24.0, 30 / 3600), 168.0)   # 30 s to 7 days
        with self.lock:
            now = max(self.s.last_wall or 0, time.time())    # board periods use the PC clock
            t0 = now - hours * 3600
            spans = [list(p) for p in self.timeline.read(t0, now)] if self.timeline else []
            spans += [[w0, now, k, nm, True] for k, (w0, nm) in self._open.items() if w0 <= now]
            if self.act_cur and self.act_cur[1] not in ACT_NOT_KEPT and self.act_cur[0] <= now:
                spans.append([self.act_cur[0], now, 'activity', self.act_cur[1], True])
            names = {}
            for p in spans:
                names[p[2]] = p[3]                   # the newest name of each link
            names.pop('activity', None)              # one lane, its periods carry their own names
            if any(p[2] == 'activity' for p in spans):
                names['activity'] = 'Activity'
            for k in names:                          # today's names win (a router renamed since)
                if k.startswith('ap:'):
                    names[k] = self._router_name(int(k[3:]))
                elif k.startswith('sat:') and '>' not in k:
                    names[k] = self.board_label(k[4:])
                elif k != 'board' and k in self.s.devices.devs:
                    names[k] = self.s.devices.name(k) or names[k]
            order = lambda k: (-1 if k == 'house' else 0 if k == 'board' else 1.5 if '>' in k else 1 if k.startswith('sat:')
                               else 2 if k == 'activity' else 3 if k.startswith('ap:') else 4, k)
            lanes = [dict(key=k, name='Board' if k == 'board' else names[k])
                     for k in sorted(names, key=order)]
            if not any(l['key'] == 'board' for l in lanes):
                lanes.insert(1 if lanes and lanes[0]['key'] == 'house' else 0, dict(key='board', name='Board'))
            return dict(t0=r2(t0), t1=r2(now), hours=hours, lanes=lanes,
                        spans=[[r2(p[0]), r2(p[1]), p[2], len(p) > 4,
                                label_info(p[3])['name'] if p[2] == 'activity' else p[3]] for p in spans],
                        per_link=self.cfg['per_link'])

    def _router_label(self, mac):
        """A router by its address: your nickname, else the hub's 'Router N' when
        the hub watches it, else 'Router <last digits>'."""
        mac = mac.lower()
        for i, m in self.s.nodes.items():
            if 0 <= i < MAX_APS and m and m.lower() == mac:
                return self._router_name(i)
        return self.cfg['router_names'].get(mac) or f'Router {mac[-5:]}'

    def _router_name(self, idx):
        """A router's name for every page: your nickname for its BSSID, else
        'Router N' (N = its node + 1). Used by Devices.link_label."""
        mac = self.s.nodes.get(idx)
        nick = self.cfg['router_names'].get(mac.lower()) if mac else None
        return nick or f"Router {idx + 1}"

    def _routers(self):
        """The routers (access points) the board learned: name, network,
        channel, signal, ACK rate, and the devices that talk through each."""
        s, dv = self.s, self.s.devices
        ssid_of = {b.lower(): n for n, aps in s.net_aps.items() for b, _c, _r in aps}
        net = s.network[1] if s.network and s.network[0] != 'none' else ''
        on = {}
        for r in dv.rows():
            if 0 <= r['ap'] < MAX_APS and r['status'] != 'gone':
                on.setdefault(r['ap'], []).append(dict(mac=r['mac'], name=r['name'], poked=r['poked']))
        out = []
        for idx, mac in sorted(s.nodes.items()):
            if not 0 <= idx < MAX_APS:
                continue
            g = dv.gate.get(idx)
            rssi = s.rssi.get((idx, 1)) or (g['rssi'] if g else None)
            out.append(dict(idx=int(idx), bssid=mac, name=self._router_name(idx),
                            nick=self.cfg['router_names'].get(mac.lower(), ''),
                            network=ssid_of.get(mac.lower(), net), ch=s.channel[0] if s.channel else None,
                            rssi=rssi, rate=round(s.rate((idx, 1))), state=g['state'] if g else None,
                            devices=on.get(idx, [])))
        return out

    def _gates(self):
        """Each poked link's gating state from the board's GATE lines."""
        dv = self.s.devices
        return [dict(node=i, label=dv.link_label(i), ap=i < MAX_APS, rssi=g['rssi'],
                     answered=g['yield_pct'], sent=g['sent'], state=g['state'])
                for i, g in sorted(dv.gate.items())]

    def _thresholds(self):
        c, det = self.s.det.c, self.s.det
        return dict(on_short=r2(c['on_short']), off_short=r2(c['off_short']),
                    on_long=r2(c['on_long']), off_long=r2(c['off_long']),
                    short_gate=r2(c['short_gate']), floor=det.floor, phase=det.phase,
                    sens=self.s.sens, sens_range=list(SENS_RANGE))

    def _header(self, br=None, dop=None):
        s, det = self.s, self.s.det
        status = s.source_status()
        if status:
            word, col, detail = status
            badge = dict(word=word, color=col, sub=detail)
        elif det.floor <= 0:
            badge = dict(word='CALIBRATING', color=CAL_C, sub='measuring the quiet level: keep still')
        else:
            head, sub = self._meaning()          # the verdict stays the board's
            badge = dict(word='MOTION' if det.motion else 'STILL', head=head,
                         color=MOTION_C if det.motion else STILL_C, sub=sub, hub=bool(det.motion))
            hm, hby = self.house_now
            others = [self.board_label(b) for b in hby if b != 'board']
            if hm and others and not det.motion:  # another board sees what the hub doesn't
                badge = dict(word='MOTION', head=f"Motion at {', '.join(others)}", color=MOTION_C,
                             sub="seen by another board; the hub's own links are still", hub=False)
        hw, hc, hd = s.link_health()
        tiles = {}
        r = (br or s.br).last
        if r is None:
            tiles['breathing'] = ('…', DIM, 'collecting 40 s')
        elif r['level'] == 'none':
            tiles['breathing'] = ('—', DIM, 'none found')
        else:
            tiles['breathing'] = (f"{r['rate']:.0f}/min",
                                  STILL_C if r['level'] == 'clear' else BRIEF_C,
                                  f"{r['level']} · last 40 s")
        d = s.doppler_reading(dop)
        if d is None:
            tiles['doppler'] = ('…', DIM, 'collecting')
        elif not d:
            tiles['doppler'] = ('—', DIM, 'nothing moving')
        elif d.get('scan'):
            tiles['doppler'] = ('scan', SCAN_C, 'router scanning (ignored)')
        elif d['step']:
            tiles['doppler'] = (f"{d['f']:.1f} Hz", BRIEF_C, 'step-like: router?')
        else:
            tiles['doppler'] = (f"{d['f']:.1f} Hz", FG, f"~{d['speed']:.2f} m/s")
        src = 'board' if det.external else 'PC'
        tiles['quiet'] = ((f"{det.floor:.4f}", FG, f"{det.phase} · from {src}") if det.floor > 0
                          else ('measuring', CAL_C, 'keep still'))
        if status:
            tiles = {k: (v, DIM, sub) for k, (v, _c, sub) in tiles.items()}
        recent = self._recent()
        n_long, n_brief = s.session_counts()
        dv = s.devices
        return dict(badge=badge, stale=bool(status), activity=None if status else self._activity(),
                    now=r2(s.last_wall or time.time()),
                    link=dict(word=hw, color=hc, detail=hd),
                    tiles={k: dict(value=v, color=c, detail=t) for k, (v, c, t) in tiles.items()},
                    recent=recent, board={None: 'n/a', True: 'motion', False: 'still'}[s.board_motion],
                    counts=dict(motion=n_long, brief=n_brief, scans=len(s.hist_scans)),
                    devices=f"{len(dv.devs)} heard · poking {len(dv.poking)}/{MAX_DEVS} ({dv.mode})",
                    log=self.log_name, board_connected=bool(self.send),
                    alerting=[s.devices.link_label(x) for x in self._alerting()] if self.send else [],
                    light=dict(self.light.view(), live=bool(self.send) or self.demo),
                    notify=dict(self.notify.view(), live=bool(self.send) or self.demo),
                    out=dict(self.out or {}, pin=self.out_pin, known=self.out is not None))

    def _score(self, t0, t1, win):
        s, det = self.s, self.s.det
        series = []
        for key in sorted(set(s.arrivals) | set(s.scores)):
            pts = s.scores.get(key)
            if not pts or key[0] >= MAX_APS:     # device links: see the Links card
                continue
            t, v = np.array(pts).T
            keep = t >= t0
            if not keep.any():
                continue
            t, v = thin(t[keep], v[keep], 700)
            series.append(dict(label=s.label(key), color=COLORS[(key[0] * 2 + key[1]) % len(COLORS)],
                               dash='solid' if key[1] else 'dash',
                               t=[r2(s.wall(x)) for x in t], v=[r2(x) for x in v]))
        long_ = None
        if s.long:
            t, v = np.array(s.long).T
            keep = t >= t0
            if keep.any():
                t, v = thin(t[keep], v[keep], 700)
                long_ = dict(t=[r2(s.wall(x)) for x in t], v=[r2(x) for x in v])

        def spans(lst, end):
            return [[r2(s.wall(max(a, t0))), r2(s.wall(b if b is not None else end))]
                    for a, b in lst if (b if b is not None else end) >= t0]
        return dict(series=series, long=long_, gap=max(0.5, win / 600),
                    on_s=r2(det.c['on_short']), on_l=r2(det.c['on_long']),
                    calibrating=det.floor <= 0,
                    motion=spans(s.motion_spans, t1), cal=spans(s.cal_spans, t1),
                    scans=spans(s.scan_spans, t1),
                    hops=[r2(s.wall(h)) for h in s.hops if h >= t0],
                    board=[[r2(s.wall(t)), bool(m)] for t, m in s.board_states if t >= t0])

    def _doppler(self, t0, t1, dop=None):
        s = self.s
        dop = dop or s.dop
        rows = [r for r in dop.rows if r[0] >= t0]
        if len(rows) < 2 or dop.base is None:
            return None
        ts = np.array([r[0] for r in rows])
        m = np.array([r[1] for r in rows])
        db = 10 * np.log10(np.maximum(m, 1e-12) / np.maximum(dop.base, 1e-12)[None, :])
        db = np.clip(db, 0, 20)
        k = int(np.ceil(len(ts) / 240))         # at most ~240 columns, each bin's peak
        if k > 1:
            n = len(ts) // k * k
            ts = ts[:n:k]
            db = db[:n].reshape(-1, k, db.shape[1]).max(1)
        return dict(t=[r2(s.wall(x)) for x in ts], fmax=float(FREQS[-1]), low=LOW_HZ,
                    step=r2((ts[1] - ts[0]) if len(ts) > 1 else 0.25),
                    db=np.rint(db).astype(int).tolist())

    def _timeline(self):
        s = self.s
        end = s.last_wall or time.time()
        start = s.hist_t[0] if s.hist_t else end - 60
        start = min(start, end - 60)
        out = dict(start=r2(start), end=r2(end), on_s=r2(s.det.c['on_short']),
                   on_l=r2(s.det.c['on_long']))
        if s.hist_t:
            t = np.array(s.hist_t, float) + 0.5
            tt, vs = thin(t, np.array(s.hist_s), 1200)
            _, vl = thin(t, np.array(s.hist_l), 1200)
            out.update(t=[r2(x) for x in tt], s=[r2(x) for x in vs], l=[r2(x) for x in vl])
        out['motion'] = [[r2(a), r2(b if b is not None else end),
                          ((b if b is not None else end) - a) < BRIEF_S] for a, b in s.hist_motion]
        out['cal'] = [[r2(a), r2(b if b is not None else end)] for a, b in s.hist_cal]
        out['scans'] = [r2(a) for a, _b in s.hist_scans]
        return out

    def _links(self, t0, t1):
        """One row per ACK link: rate, signal, its own motion verdict and scores
        against its own quiet level, when it saw motion in [t0, t1], and its
        own breathing / Doppler."""
        s = self.s
        out = []
        for src in sorted(self.links):
            key, ln = (src, 1), self.links[src]
            md = ln['md']
            pts = [p for p in md.scores if p[0] >= s.now - SPARK_S]
            if not pts and not s.scores.get(key):    # nothing from it in the whole window
                continue
            f = md.det.floor                     # its own quiet level (0 while calibrating)
            # its own scores (not State.scores: those are in units of the board's quiet level)
            t, v = np.array(pts).T if pts else (np.zeros(0), np.zeros(0))
            q = f if f > 0 else max(float(np.percentile(v, 10)), 1e-6) if pts else 1.0
            tt, vv = thin(t, v / q, 150)
            now_us = int(s.now * 1e6)
            ms, ml = md.det.max_scores(now_us)   # None once its data is stale
            state = md.state(now_us)
            judged = f > 0 and state in ('motion', 'still')
            short = ms / f if ms is not None and judged else None
            long_ = ml / f if ml is not None and judged else None

            def clip(spans):
                return [[r2(s.wall(max(a, t0))), r2(s.wall(min(b if b is not None else t1, t1)))]
                        for a, b in spans if (b is None or b >= t0) and a <= t1]
            row = dict(src=key[0], label=s.devices.link_label(key[0]), ap=key[0] < MAX_APS,
                       rate=round(s.rate(key)), rssi=s.rssi.get(key, 0), state=state,
                       activity=r2(short or 0.0), long=r2(long_), moving=state == 'motion',
                       spans=clip(md.spans), weak=clip(md.weak_spans),
                       spark=dict(t=[r2(s.wall(x)) for x in tt], v=[r2(x) for x in vv]),
                       breathing=None, doppler=None)
            b = ln['br'].last
            row['breathing'] = None if b is None else dict(rate=r2(b['rate']), level=b['level'])
            d = s.doppler_reading(ln['dop'])
            row['doppler'] = None if d is None else (
                {} if not d else dict(f=r2(d['f']), scan=bool(d.get('scan')), step=bool(d.get('step'))))
            out.append(row)
        return out

    def _board_spans(self, t0, t1):
        """The board's own verdict (router link) as motion spans in [t0, t1]."""
        s = self.s
        spans, start = [], None
        # The board reports changes only (older ones are dropped): the state
        # before the first change kept is its opposite, or with none kept, the
        # current verdict
        before = (not s.board_states[0][1]) if s.board_states else bool(s.board_motion)
        if before:
            start = -float('inf')
        for t, m in s.board_states:
            if m and start is None:
                start = t
            elif not m and start is not None:
                if t >= t0:
                    spans.append([r2(s.wall(max(start, t0))), r2(s.wall(min(t, t1)))])
                start = None
        if start is not None:
            spans.append([r2(s.wall(max(start, t0))), r2(s.wall(t1))])
        return spans

    def _devices(self):
        dv = self.s.devices
        rows = []
        for r in dv.rows():
            rows.append(dict(mac=r['mac'], name=r['name'], rssi=r['rssi'], tx=r['tx'],
                             rate=round(r['rate'] * 60), heard=fmt_age(r['heard']) + ' ago',
                             why=r['why'], link=r['link'], poked=r['poked'], picked=r['picked'],
                             status=r['status'], ap=r['ap'], router=r['router'],
                             board=r['board']))
        return dict(mode=dv.mode, modes=list(MODES), rows=rows, max=MAX_DEVS,
                    poking=len(dv.poking), seen=dv.board_seen)

    def _network(self):
        how, name = self.s.network or ('?', '')
        return dict(how=how, name=name, state=self.s.nets_state, ap_list=self.s.ap_list,
                    channel=self.s.channel[0] if self.s.channel else None,
                    nets=[dict(name=n, ch=ch, rssi=rssi,
                               aps=[dict(bssid=b, ch=c, rssi=r) for b, c, r in self.s.net_aps.get(n, [])])
                          for n, ch, rssi in self.s.nets])

    def _details(self):
        s, det = self.s, self.s.det
        live = [k for k in sorted(set(s.arrivals) | set(s.scores)) if s.rate(k) > 0]
        out = dict(channel=list(s.channel) if s.channel else None, phase=det.phase, why=det.why,
                   floor=det.floor, streams=[dict(label=s.label(k), rate=s.rate(k),
                                                  rssi=s.rssi.get(k, 0)) for k in live])
        if s.stats:
            out['stats'] = {k: v for k, v in s.stats.items() if isinstance(v, (int, float))}
        return out


def _router_nick(v):
    """A router nickname: 1-24 printable characters, no surrounding spaces."""
    return (isinstance(v, str) and 0 < len(v) <= ROUTER_NAME_MAX and v == v.strip()
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in v))


def _num(v, lo=-1e6, hi=1e6):
    """A finite number in [lo, hi], else ValueError (booleans aren't numbers here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError('bad number in the map')
    return float(v)


def _text(v, n=64):
    if not isinstance(v, str) or len(v) > n or any(ord(c) < 0x20 for c in v):
        raise ValueError('bad text in the map')
    return v


def clean_map(m):
    """The Map page's layout, checked: only known keys, finite numbers, short
    strings. Positions are metres from the board. Raises ValueError."""
    if not isinstance(m, dict):
        raise ValueError('the map must be an object')
    out = {}
    if 'unit' in m:
        if m['unit'] not in MAP_UNITS:
            raise ValueError('bad unit')
        out['unit'] = m['unit']
    if 'env' in m:
        if m['env'] not in MAP_ENVS:
            raise ValueError('bad environment')
        out['env'] = m['env']
    if 'view' in m:
        v = m['view']
        if not isinstance(v, dict):
            raise ValueError('bad view')
        out['view'] = dict(x=_num(v.get('x')), y=_num(v.get('y')), s=_num(v.get('s'), 0.01, 1e5))
    if 'hub_floor' in m:
        out['hub_floor'] = int(_num(m['hub_floor'], 1, 9))
    if 'floor_h' in m:
        out['floor_h'] = _num(m['floor_h'], 2, 6)
    if 'hub_facing' in m:
        out['hub_facing'] = _num(m['hub_facing'], -7, 7)
    if 'nodes' in m:
        nodes = m['nodes']
        if not isinstance(nodes, list) or len(nodes) > MAP_NODES:
            raise ValueError('bad nodes')
        out['nodes'] = []
        for n in nodes:
            if not isinstance(n, dict):
                raise ValueError('bad node')
            c = dict(key=_text(n.get('key')), x=_num(n.get('x'), -10000, 10000), y=_num(n.get('y'), -10000, 10000))
            if 'name' in n:
                c['name'] = _text(n['name'])
            if 'eirp' in n:
                c['eirp'] = _num(n['eirp'], -10, 40)
            if 'placed' in n:
                c['placed'] = bool(n['placed'])
            if n.get('floor'):
                c['floor'] = int(_num(n['floor'], 1, 9))
            if 'facing' in n:                    # radians: where its antenna looks
                c['facing'] = _num(n['facing'], -7, 7)
            h = n.get('hint')                    # a rough spot: [x, y, radius in m]
            if isinstance(h, list) and len(h) == 3:
                c['hint'] = [_num(h[0], -10000, 10000), _num(h[1], -10000, 10000), _num(h[2], 1, 100)]
            f = n.get('fit')                     # the Pi's fitted spot (autofit): [x, y]
            if isinstance(f, list) and len(f) == 2:
                c['fit'] = [_num(f[0], -10000, 10000), _num(f[1], -10000, 10000)]
            au = n.get('auto')                   # the map's estimate of an unplaced end: [a, b, rot] (metres, radians)
            if isinstance(au, list) and len(au) == 3:
                c['auto'] = [_num(au[0], 0, 1000), _num(au[1], 0, 1000), _num(au[2], -7, 7)]
            out['nodes'].append(c)
    if 'track_off' in m:                        # boards left out of the tracking: 'hub' / 'sat:<mac>'
        to = m['track_off']
        if not isinstance(to, list) or len(to) > SAT_MAX + 1 or not all(
                isinstance(k, str) and (k == 'hub' or (k.startswith('sat:') and MAC_RE.fullmatch(k[4:]))) for k in to):
            raise ValueError('bad boards left out of the map')
        out['track_off'] = sorted({k.lower() for k in to})
    if 'zones' in m:                            # areas on the map: who's in them, and what to do then
        zs = m['zones']
        if not isinstance(zs, list) or len(zs) > 16:
            raise ValueError('bad zones')
        out['zones'] = []
        for z in zs:
            if not isinstance(z, dict) or not isinstance(z.get('id'), str) or not re.fullmatch(r'[a-z0-9]{1,12}', z['id']):
                raise ValueError('bad zone')
            c = dict(id=z['id'], name=_text(z.get('name', 'Zone'), 32),
                     notify=bool(z.get('notify', False)), night=bool(z.get('night', False)),
                     light=bool(z.get('light', False)))
            if z.get('near') is not None:            # "near <router or board>": the motion's end, no area
                c['near'] = _text(z['near'], 64)
            else:
                c.update(x=_num(z.get('x'), -10000, 10000), y=_num(z.get('y'), -10000, 10000),
                         w=_num(z.get('w'), 0.3, 1000), h=_num(z.get('h'), 0.3, 1000))
            if z.get('watch') and 'near' not in c:
                c['watch'] = True                    # the watched area: people placed only in here
            out['zones'].append(c)
    if 'plans' in m:                            # picture layers: where each sits, in metres
        plans = m['plans']
        if not isinstance(plans, list) or len(plans) > len(PLAN_IDS):
            raise ValueError('bad picture layers')
        out['plans'], seen = [], set()
        for p in plans:
            if not isinstance(p, dict) or p.get('id') not in PLAN_IDS or p['id'] in seen:
                raise ValueError('bad picture layer')
            seen.add(p['id'])
            c = dict(id=p['id'], x=_num(p.get('x'), -100000, 100000), y=_num(p.get('y'), -100000, 100000),
                     w=_num(p.get('w'), 0.1, 100000), rot=_num(p.get('rot', 0), -7, 7),
                     opacity=_num(p.get('opacity', 1), 0, 1), hidden=bool(p.get('hidden', False)))
            if 'name' in p:
                c['name'] = _text(p['name'])
            out['plans'].append(c)
    return out


def make_handler(dash, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_a):              # quiet console
            pass

        def _host_ok(self):
            host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
            return host in allowed_hosts

        def _reply(self, code, body, ctype='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._reply(403, {'error': 'host not allowed'})
            u = urlparse(self.path)
            if u.path in STATIC:
                name, ctype = STATIC[u.path]
                with open(os.path.join(HERE, 'web', name), 'rb') as f:
                    return self._reply(200, f.read(), ctype)
            if u.path == '/events':
                return self._events(parse_qs(u.query))
            if u.path.startswith('/api/'):          # data endpoints: same rules as commands
                if self.headers.get('X-Ackwave') != '1':
                    return self._reply(403, {'error': 'missing X-Ackwave header'})
                if u.path == '/api/stats':
                    return self._reply(200, dash.stats())
                if u.path == '/api/map':
                    return self._reply(200, dash.map_load())
                if u.path == '/api/walks':
                    return self._reply(200, dash.walks())
                if u.path == '/api/snaps':
                    return self._reply(200, dict(snaps=list(dash.snaps)))
                if u.path == '/api/plan':
                    got = dash.plan_load(parse_qs(u.query).get('id', [''])[0])
                    if not got:
                        return self._reply(404, {'error': 'no picture'})
                    return self._reply(200, got[0], got[1])
                if u.path == '/api/timeline':
                    try:
                        hours = float(parse_qs(u.query).get('hours', ['24'])[0])
                    except ValueError:
                        hours = 24.0
                    return self._reply(200, dash.timeline_range(hours))
                return self._reply(404, {'error': 'not found'})
            if u.path == '/snapshot':                # one-off (screenshots, debugging)
                q = parse_qs(u.query)
                win, link = self._win(q), self._link(q)
                return self._reply(200, {'fast': dash.fast(win, link), 'slow': dash.slow(win, link),
                                         'debug': dash.debug(win)})
            self._reply(404, {'error': 'not found'})

        @staticmethod
        def _win(q):
            try:
                win = float(q.get('win', [WINDOWS[0]])[0])
            except ValueError:
                win = WINDOWS[0]
            return min(max(win, 10.0), WINDOWS[-1])

        @staticmethod
        def _link(q):
            try:
                return int(q['link'][0]) if 'link' in q else None
            except ValueError:
                return None

        def _events(self, q):
            win, link = self._win(q), self._link(q)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            next_slow = next_debug = 0.0
            try:
                while True:
                    now = time.monotonic()
                    msgs = [('fast', dash.fast(win, link))]
                    if now >= next_slow:
                        next_slow = now + SLOW_S
                        try:
                            msgs.append(('slow', dash.slow(win, link)))
                        except Exception as e:        # one broken part must not stop the page
                            if not getattr(dash, '_slow_err', None):
                                import traceback
                                traceback.print_exc()
                            dash._slow_err = repr(e)
                    if now >= next_debug:
                        msgs.append(('debug', dash.debug(win)))
                        next_debug = now + DEBUG_S
                    for ev, data in msgs:
                        self.wfile.write(f"event: {ev}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(FAST_S)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self):
            if not self._host_ok():
                return self._reply(403, {'ok': False, 'msg': 'host not allowed'})
            origin = self.headers.get('Origin')
            host = self.headers.get('Host', '')
            if self.headers.get('X-Ackwave') != '1' or (origin and urlparse(origin).netloc != host):
                return self._reply(403, {'ok': False, 'msg': 'cross-site request refused'})
            path = urlparse(self.path).path
            if path == '/api/map':
                try:
                    n = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    return self._reply(400, {'ok': False, 'msg': 'bad request'})
                if n > MAP_MAX:
                    self.close_connection = True
                    left = min(n, 16 * MAP_MAX)          # read it away so the reply gets through
                    while left > 0:
                        got = self.rfile.read(min(left, 65536))
                        if not got:
                            break
                        left -= len(got)
                    return self._reply(413, {'ok': False, 'msg': 'map too big'})
                try:
                    m = json.loads(self.rfile.read(n) or b'{}')
                except ValueError:
                    return self._reply(400, {'ok': False, 'msg': 'bad request'})
                ok, msg = dash.map_save(m, who=self.headers.get('User-Agent') or '')
                return self._reply(200 if ok else 400, dict(ok=ok, msg=msg, map=dash.map_load() if ok else None))
            if path == '/api/plan':                   # a map picture: raw bytes, empty = remove
                try:
                    n = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    return self._reply(400, {'ok': False, 'msg': 'bad request'})
                if n > PLAN_MAX:
                    self.close_connection = True      # don't read 12+ MB just to refuse it
                    return self._reply(413, {'ok': False, 'msg': 'picture too big (12 MB at most)'})
                ok, msg = dash.plan_save(parse_qs(urlparse(self.path).query).get('id', [''])[0],
                                         self.rfile.read(n) if n else b'')
                return self._reply(200 if ok else 400, {'ok': ok, 'msg': msg})
            if path != '/cmd':
                return self._reply(404, {'ok': False, 'msg': 'not found'})
            try:
                n = int(self.headers.get('Content-Length') or 0)
                c = json.loads(self.rfile.read(min(n, 4096)) or b'{}')
                if not isinstance(c, dict):
                    raise ValueError
            except ValueError:
                return self._reply(400, {'ok': False, 'msg': 'bad request'})
            ok, msg = dash.command(c)
            self._reply(200 if ok else 400, {'ok': ok, 'msg': msg})
    return Handler


INGRESS_PROXY = '172.30.32.2'   # Home Assistant's ingress proxy, the only client in --ingress mode


def serve(dash, host='127.0.0.1', port=8765, only_from=None):
    """Start the HTTP server in a thread; returns it (server.server_address has the port).
    only_from: a client address; every other client is refused (Home Assistant ingress)."""
    if only_from:                                # behind HA's proxy: it sends HA's own Host
        srv = ThreadingHTTPServer(('0.0.0.0', port), make_handler(dash, set()))
        srv.daemon_threads = True
        srv.RequestHandlerClass._host_ok = lambda self: self.client_address[0] == only_from
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    allowed = {'localhost', '127.0.0.1', '::1'}
    if host not in ('127.0.0.1', 'localhost', '::1'):
        import socket
        allowed |= {host, socket.gethostname().lower()}
        try:
            allowed |= {socket.gethostbyname(socket.gethostname())}
        except OSError:
            pass
        if host in ('0.0.0.0', '::'):
            allowed.add('*')
    srv = ThreadingHTTPServer((host, port), make_handler(dash, allowed))
    srv.daemon_threads = True
    if '*' in allowed:                           # LAN mode: any Host the user types
        srv.RequestHandlerClass._host_ok = lambda self: True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class LagQueue(queue.Queue):
    """The readers' lines for the pump. If the dashboard falls behind (a busy
    Pi), CSI lines past LIMIT waiting are dropped instead of piling up (memory,
    a live view minutes late); every other line (states, commands, reports)
    always gets through."""
    LIMIT = 4000                                 # ~20 s of CSI from one board

    def __init__(self):
        super().__init__()
        self.dropped = 0

    def put(self, item, block=True, timeout=None):
        line = item[1] if isinstance(item, tuple) else item
        if isinstance(line, str) and self.qsize() > self.LIMIT:
            body = line[19:] if line.startswith('@') else line      # '@<mac>,' tagged: another board's
            if body.startswith('CSI,'):
                self.dropped += 1
                return
        super().put(item, block, timeout)


def pump(dash, q):
    """Feed lines from the reader thread into the dashboard until input ends."""
    s = dash.s
    while True:
        line = q.get()
        if line is None:
            with dash.lock:
                s.ended = s.lost is None
            return
        if line.startswith('#ERR'):
            print(line[1:], file=sys.stderr, flush=True)
            with dash.lock:
                s.lost = line[5:].strip() or 'connection lost'
                dash.lost_since = dash.lost_since or time.monotonic()
            continue
        if line.startswith('#OK'):               # the port is back (SerialSource)
            print('Board reconnected', flush=True)
            with dash.lock:
                s.lost, dash.lost_since = None, None
            continue
        try:
            if line.startswith('@'):             # another board, in a tagged stream
                mac, body = split_tag(line)
                if mac:
                    dash.feed_other(mac, body)
                    continue
            dash.feed(line)
        except Exception as e:                   # one odd line must not stop the dashboard
            print(f"skipped a line ({e})", file=sys.stderr)


def reader_src(i, lines, q):
    """One port of several: (port index, line) into the queue."""
    try:
        for line in lines:
            if line:                             # '#ERR' / '#OK' too: the port went away / came back
                q.put((i, line))
    except Exception as e:                       # unplugged etc.
        q.put((i, f"#ERR {e}"))
    q.put((i, None))


def pump_multi(dash, q, n):
    """Lines from several ports until they all end."""
    open_ = n
    while open_:
        i, line = q.get()
        if line is None:
            open_ -= 1
            continue
        if line.startswith('#ERR'):
            print(f"port {i + 1}: {line[1:]}", file=sys.stderr, flush=True)
            if dash.src_board.get(i) == dash.primary:
                with dash.lock:
                    dash.s.lost = line[5:].strip() or 'connection lost'
                    dash.lost_since = dash.lost_since or time.monotonic()
            continue
        if line.startswith('#OK'):
            print(f"port {i + 1}: reconnected", flush=True)
            if dash.src_board.get(i) == dash.primary:
                with dash.lock:
                    dash.s.lost, dash.lost_since = None, None
            continue
        try:
            dash.feed_src(i, line)
        except Exception as e:
            print(f"skipped a line ({e})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('source', nargs='+', help="serial port(s) (/dev/ttyACM0, COM5), or '-' for stdin; "
                    "several ports: one board each, the hub (saved) is the primary")
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1',
                    help='127.0.0.1 = this PC only (default); 0.0.0.0 = whole LAN')
    ap.add_argument('--open', action='store_true', help='open it in the browser')
    ap.add_argument('--ingress', action='store_true',
                    help=f"Home Assistant add-on: answer only HA's ingress proxy ({INGRESS_PROXY})")
    ap.add_argument('--no-log', action='store_true', help="don't log to sessions/")
    ap.add_argument('--no-raw', action='store_true', help="log events only, not every board line (small disks)")
    ap.add_argument('--demo', action='store_true', help='demo data: a simulated light, nothing switched for real')
    ap.add_argument('--give-up', type=float, default=0, metavar='S',
                    help="exit (code 1) when the hub's port has been gone S seconds, so a supervisor "
                         "restarts it (the Home Assistant add-on: 60); 0 = keep waiting")
    ap.add_argument('--out', default=os.path.join(os.path.dirname(HERE), 'sessions'), help='log folder')
    a = ap.parse_args()

    state = State(WINDOWS[-1])
    multi = len(a.source) > 1
    if multi and '-' in a.source:
        ap.error("'-' (stdin) can't be mixed with ports: tag its lines '@<mac>,' instead")
    log = awake = None
    if a.source[0] != '-' and not a.no_log:
        from record import SessionLog, keep_awake
        log = SessionLog(a.out, raw=not a.no_raw)
        awake = keep_awake('sleep')
        print(f"Logging to {log.ev_path}")
    prefs = os.path.join(a.out, 'devices.json')
    mode, picks = Devices.load_prefs(prefs)
    oui = Oui()
    state.devices = Devices(oui=oui, mode=mode, manual=picks)

    def say_oui():                               # once: which maker list names the devices
        oui.ready.wait()
        print(f"Device makers: {len(oui.table)} prefixes from {oui.source}" if oui.table else
              "Device makers: no list found (install ieee-data or arp-scan): devices show as 'unknown'",
              flush=True)
    threading.Thread(target=say_oui, daemon=True).start()
    opened = [open_port(src, a.baud, reconnect=True) for src in a.source]
    lines, send = opened[0]
    state.live = send is not None
    dash = Dashboard(state, send, os.path.basename(log.ev_path) if log else None, prefs,
                     os.path.join(a.out, 'dashboard.json'), sessions_dir=a.out, demo=a.demo)
    q = LagQueue()
    if multi:
        dash.set_sources([sd for _l, sd in opened], log)
        for i, (ls, _sd) in enumerate(opened):
            threading.Thread(target=reader_src, args=(i, ls, q), daemon=True).start()
        print(f"{len(opened)} boards: the hub (saved) is the primary; the others are watched")
    else:
        threading.Thread(target=reader, args=(lines, q, log), daemon=True).start()
    srv = serve(dash, a.host, a.port, only_from=INGRESS_PROXY if a.ingress else None)

    def ticker():
        said, said_at = 0, 0.0
        while True:
            time.sleep(1.0)
            if q.dropped > said and time.monotonic() - said_at > 60:
                print(f"Falling behind: skipped {q.dropped - said} CSI lines to keep up "
                      f"({q.qsize()} waiting)", flush=True)
                said, said_at = q.dropped, time.monotonic()
            try:
                dash.tick()
            except Exception as e:               # the light and EXT must keep running
                print(f"tick failed ({e})", file=sys.stderr)
    threading.Thread(target=ticker, daemon=True).start()
    gave_up = []

    def watch_port():                            # the hub's port gone too long: exit, be restarted
        import _thread
        while True:
            time.sleep(2.0)
            since = dash.lost_since
            if since is not None and time.monotonic() - since > a.give_up:
                print(f"The board has been gone {a.give_up:g} s: exiting so it is restarted", flush=True)
                gave_up.append(1)
                _thread.interrupt_main()
                return
    if a.give_up > 0:
        threading.Thread(target=watch_port, daemon=True).start()
    # Docker / Home Assistant stop with SIGTERM: leave like Ctrl+C, so the logs close cleanly
    def on_term(*_a):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    url = f"http://localhost:{srv.server_address[1]}"
    print(f"Dashboard: {url}   (Ctrl+C to stop)")
    if a.host not in ('127.0.0.1', 'localhost', '::1'):
        print(f"  Open to the network on {a.host}: anyone there can view it and send commands.")
    else:
        print(f"  From a phone: ssh -L {a.port}:localhost:{a.port} <you>@<this-pc>, "
              f"then open {url} on the phone.")
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        if multi:
            pump_multi(dash, q, len(opened))
        else:
            pump(dash, q)
        print("Input ended; the dashboard stays up until Ctrl+C.")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        if log:
            log.close()
        if awake:
            from record import release
            release(awake)
    if gave_up:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
