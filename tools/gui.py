#!/usr/bin/env python3
"""Live GUI for the CSI motion sensor.

    python gui.py /dev/ttyUSB0                  # real board (COM5 on Windows)
    python fake_csi.py --realtime --loop | python gui.py -     # demo, no hardware
    python fake_csi.py | python gui.py - --snapshot shot.png   # render one image

With a board it also logs in the background, exactly like record.py (events
and raw data in sessions/, summary when the window closes); --no-log turns
that off. Buttons: 30 s / 2 min / 10 min of detail, and Pause (freezes the
view so the toolbar can zoom and pan; logging continues). Keys: space = pause,
1/2/3 = window, c = recalibrate (with a board), d = raw firmware counters,
w = Doppler / subcarrier view, h or ? = help overlay with keys and colours.
The header shows link health, time since the last motion and the most recent
motion periods with their peak score. The session timeline at the
bottom covers everything since the GUI started, on a clock.

A second window lists the devices the board hears on your network, with
their maker (from the `oui` tool's database, or the system IEEE list),
signal, and whether they look stationary. In Auto mode the board pokes up to
4 stationary ones as extra links; tick a device to choose yourself (Manual).
Only the router link decides motion / still; device links are plotted thin
and dotted. Your mode and picks are remembered in sessions/devices.json.

Shows: short-window motion score per stream (node x beacon/ACK) and the
long-window score, both as multiples of the room's quiet level, with the two
thresholds; the PC's verdict (shaded), the board's own verdict (STATE lines),
calibration periods, router scans the detector ignored (grey), a Doppler
spectrogram of the router's ACKs (experimental, doppler.py; key w swaps in the
subcarrier waterfall), a breathing-rate strip (experimental, breathing.py;
needs someone still near the path), header tiles for both, and live
packet rates / firmware counters. The quiet level comes from the board's
CALIB lines (so PC and board agree); without them the PC measures its own.
"""
import argparse
import datetime
import os
import queue
import sys
import threading
import time
from collections import defaultdict, deque

import numpy as np

from analyze import (MAX_APS, VALID, MotionDetector, open_port, open_source, parse_line,
                     set_thresholds)
from breathing import Breathing, text as breathing_text
from doppler import FREQS, LOW_HZ, Doppler
from devices import MAX_DEVS, MODES, Devices, Oui, fmt_age

BG, PANEL, FG, DIM = '#101418', '#171c22', '#e6e9ed', '#7d8794'
MOTION_C, STILL_C, LONG_C, CAL_C = '#ff5a4f', '#3ecf8e', '#f2f4f7', '#7d8794'
BRIEF_C = '#ffb547'
SCAN_C = '#9aa5b8'                              # router scans (ignored by the detector)
HELP = '\n'.join([
    'Keys',
    '',
    'space     pause / back to live (logging never stops)',
    '1 2 3     show 30 s / 2 min / 10 min',
    'c         recalibrate (step away for 5 s)',
    'n         network: choose which router to watch',
    'w         Doppler view / subcarrier view',
    'd         details: raw board counters',
    'h  ?      this help (Esc closes)',
    '',
    'Colours',
    '',
    'red       motion            green   still / clear breathing',
    'orange    brief blip        amber   possible / router step?',
    'grey band router scan, ignored by the board',
])
GRID_C = '#2a313a'                              # grid lines, borders, hover
SEL_C = '#2f3a46'                               # selected button / row
HEAD_C = '#222932'                              # table header
POKE_C = '#5fd4d0'                              # devices being poked
WINDOWS = (30.0, 120.0, 600.0)                  # selectable detail windows (s)
BRIEF_S = 2.0                                   # as record.py: shorter = brief blip
SOUND_MODES = ('off', 'beep', 'alarm')                 # the board's buzzer modes (SOUND)
SENS_RANGE = (110, 300)                                # long threshold x100 the board accepts (SENS)
COLORS = ['#4ea8ff', '#ffb547', '#b48cff', '#5fd4d0', '#ff7eb6', '#c3e88d', '#f78c6c', '#89ddff']


class Kept:
    """A board setting the PC keeps at its own choice: re-sent whenever the
    board reports something else (it reports every 5 s, and from scratch after
    a restart), but not more than once per 3 s while it catches up."""

    def __init__(self, state, fmt, default_cmd):
        self.state, self.fmt, self.default_cmd = state, fmt, default_cmd
        self.want = None                                 # None = the board's own value
        self.sent = None                                 # state.clock() of the last send

    def set(self, value):
        self.want = value
        if value is None:
            self.state.outbox.append(self.default_cmd)
        else:
            self._send()

    def seen(self, value):
        """The board reported value."""
        now = self.state.clock()
        if self.want is not None and value != self.want and \
                (self.sent is None or not 0 <= now - self.sent <= 3):
            self._send()

    def _send(self):
        self.sent = self.state.clock()
        self.state.outbox.append(self.fmt(self.want))


class State:
    """Everything the plots need; fed one parsed event at a time."""

    def __init__(self, window_s, waterfall_s=10.0, clock=time.time, devices=None):
        self.window_s, self.waterfall_s = window_s, waterfall_s   # window_s: detail kept
        self.clock = clock
        self.devices = devices or Devices(clock=clock)   # survives board reboots
        self.outbox = []                                 # commands for the board
        self.on_csi = None                               # extra per-packet hook: on_csi(d, t)
        self.network = None                              # (how, name): how = default/chosen/none
        self.nets, self.nets_state = [], 'idle'          # SCAN results: [(name, ch, rssi)]
        self.net_aps = {}                                # name -> [(bssid, ch, rssi)]
        self.net_confirmed = None                        # last network the board reported watching
        # Board settings the PC keeps applied (the board reports them every 5 s)
        self.sound = None                                # board's buzzer: dict(mode, vol, hz, buzzer)
        self.kept_sound = Kept(self, lambda w: 'SOUND,%s,%d,%d' % w, 'SOUND,default')
        self.sens = None                                 # board's long threshold x100 (SENS)
        self.kept_sens = Kept(self, lambda w: f'SENS,{w}', 'SENS,default')
        # Whole-session history on the wall clock (survives board reboots):
        # one bin per second with the highest short / long score
        self.hist_t, self.hist_s, self.hist_l = [], [], []
        self.hist_motion, self.hist_cal = [], []     # [wall0, wall1 or None]
        self.hist_peak = []                          # peak short score per hist_motion entry
        self.hist_scans = []                         # [wall0, wall1]: router scans ignored
        self.wall_off, self.last_wall = None, None
        self.live = False                            # a board, not a file: arrival time ~ now
        self.last_rx = None                          # clock() of the last line received
        self.lost = None                             # error text once the source failed
        self.ended = False                           # input ran out (file / demo)
        self.reset()

    def _network_line(self, line):
        tag, _, rest = line.partition(',')
        if tag == 'NETWORK':
            how, _, name = rest.partition(',')
            if how in ('default', 'chosen', 'none'):
                if self.net_confirmed is not None and self.net_confirmed != name:
                    self.devices.forget_network()        # switched: old devices don't apply
                self.net_confirmed = name
                self.network = (how, name)
            elif how == 'saved':
                self.network = ('switching', name)
        elif tag in ('NET', 'NETAP') and not self.scanning():
            return                                       # leftovers of an earlier scan
        elif tag == 'NET':
            p = rest.split(',', 2)
            if len(p) == 3:
                try:
                    ch, rssi, name = int(p[0]), int(p[1]), p[2]
                except ValueError:
                    return
                old = [n for n in self.nets if n[0] == name]   # one row per name
                if not old or rssi > old[0][2]:
                    self.nets = [n for n in self.nets if n[0] != name] + [(name, ch, rssi)]
        elif tag == 'NETAP':
            p = rest.split(',', 3)
            if len(p) == 4:
                try:
                    ap = (p[0], int(p[1]), int(p[2]))
                except ValueError:
                    return
                aps = self.net_aps.setdefault(p[3], [])
                if ap[0] not in [a[0] for a in aps]:
                    aps.append(ap)
                    aps.sort(key=lambda a: -a[2])
        elif rest.startswith('done'):
            self.nets.sort(key=lambda n: -n[2])
            self.nets_state = 'done'
        elif rest.startswith('ignored'):                 # it had just scanned: try again shortly
            self.nets_state = 'idle'

    def _sound_line(self, line):
        p = line.split(',')
        if len(p) < 4 or p[1] not in SOUND_MODES:
            return
        try:
            self.sound = dict(mode=p[1], vol=int(p[2]), hz=int(p[3]), buzzer=p[4:5] != ['no-buzzer'])
        except ValueError:
            return
        self.kept_sound.seen((self.sound['mode'], self.sound['vol'], self.sound['hz']))

    def want_sound(self, mode, vol=0, hz=0):
        """Keep the board's buzzer at this (mode, vol 1-50, hz 500-6000), or
        mode None = leave it at the firmware's settings."""
        if mode is not None and (mode not in SOUND_MODES or not 1 <= vol <= 50
                                 or not 500 <= hz <= 6000):
            raise ValueError('bad sound setting')
        self.kept_sound.set(None if mode is None else (mode, int(vol), int(hz)))

    @property
    def sound_want(self):
        return self.kept_sound.want

    def _sens_line(self, line):
        try:
            x = int(line.split(',')[1])
        except (IndexError, ValueError):
            return
        if not SENS_RANGE[0] <= x <= SENS_RANGE[1]:
            return
        self.sens = x
        self._apply_sens()
        self.kept_sens.seen(x)

    def _apply_sens(self):
        """The PC's detector uses the board's long threshold (CALIB carries it
        too, every 5 s; this applies a change at once)."""
        if self.sens:
            set_thresholds(self.det.c, on_long=self.sens / 100)

    def want_sens(self, x100):
        """Keep the board's long-window threshold at x100 (110-300), or None =
        the firmware's own."""
        if x100 is not None and (isinstance(x100, bool) or not isinstance(x100, int)
                                 or not SENS_RANGE[0] <= x100 <= SENS_RANGE[1]):
            raise ValueError('bad sensitivity')
        self.kept_sens.set(x100)

    @property
    def sens_want(self):
        return self.kept_sens.want

    def start_scan(self):
        """Ask the board for nearby networks (about 4 s; pokes pause meanwhile)."""
        if self.scanning():
            return                                       # one at a time
        self.nets, self.net_aps, self.nets_state = [], {}, 'scanning'
        self.scan_t = self.clock()
        self.outbox.append('SCAN')

    def scanning(self):
        """A scan is running (its NETS,done not in yet; given up after 15 s,
        e.g. when the board restarted meanwhile)."""
        return self.nets_state == 'scanning' and self.clock() - getattr(self, 'scan_t', 0) < 15

    def wall(self, t=None):
        """Wall-clock time (unix s) of board time t (default: now)."""
        return self.wall_off + (self.now if t is None else t)

    def reset(self):
        self.det = MotionDetector()
        if getattr(self, 'sens', None):
            self._apply_sens()                     # survives a reset
        self.scores = defaultdict(deque)          # (src, kind) -> deque[(t, short / floor)]
        self.long = deque()                       # [(t, long / floor)]
        self.cal_spans = []                       # [t0, t1 or None] without a floor
        self.board_calib = None
        self.arrivals = defaultdict(deque)        # (src, kind) -> deque[t] (rates)
        self.rssi = {}
        self.wf = defaultdict(deque)              # (src, kind) -> deque[(t, vec)]
        self.dop = Doppler(keep_s=self.window_s)  # router-ACK Doppler spectra
        self.br = Breathing()                     # router-ACK breathing rate
        self.br_hist = deque()                    # (t, reading) for the breathing strip
        self.scan_spans = []                      # [t0, t1]: router scan suspected
        self.motion_spans = []                    # [t0, t1 or None]
        self.board_states = deque()               # (t, motion: bool)
        self.board_motion = None
        self.hops = []
        self.nodes, self.channel, self.stats, self.me = {}, None, None, None
        self.last_ts = None
        self.now = 0.0
        self.n_lines = 0

    def feed(self, line):
        self.n_lines += 1
        self.last_rx = self.clock()
        if line.startswith('ME,'):
            self.me = line.strip()[3:]
            return
        if line.startswith(('NETWORK,', 'NET,', 'NETAP,', 'NETS,')):
            self._network_line(line.rstrip('\r\n'))
            return
        if line.startswith('SOUND,'):
            self._sound_line(line.rstrip('\r\n'))
            return
        if line.startswith('SENS,'):
            self._sens_line(line.rstrip('\r\n'))
            return
        ev = parse_line(line)
        if ev is None:
            return
        kind, d = ev
        if kind == 'learned':
            self.nodes[d['idx']] = d['mac']
            return
        if kind in ('dev', 'gate', 'poking', 'unpoked', 'refused', 'stats'):
            self.outbox += self.devices.feed(kind, d)
            if kind != 'stats':
                return
        if 'ts' in d:
            if self.last_ts is not None and d['ts'] < self.last_ts - 1_000_000:
                nodes, me = self.nodes, self.me      # board rebooted / demo looped
                self._close_spans()
                self.reset()
                self.nodes, self.me = nodes, me
                self.wall_off = None
            self.last_ts = d['ts']
            self.now = d['ts'] / 1e6
            if self.wall_off is None:                # keep the clock monotonic
                w = self.clock()
                if self.last_wall is not None:
                    w = max(w, self.last_wall + 0.1)
                self.wall_off = w - self.now
            elif self.live and self.clock() - self.now < self.wall_off - 1.0:
                # A live line can arrive late (it waited in the board's USB
                # buffer) but never early: a line this much "earlier" than the
                # mapping says means the mapping came from a stale line
                self.wall_off = self.clock() - self.now
            self.last_wall = self.wall()
        if kind == 'channel':
            self.channel = (d['ch'], d['why'])
            self.det.on_channel(d['ts'])
            self.hops.append(self.now)
        elif kind == 'calib':                     # the board's quiet level wins
            self.board_calib = d
            c = self.det.c
            if d['floor'] > 0:                   # the board's thresholds (SENS may change on_long)
                set_thresholds(c, on_short=d['on_short'] / d['floor'], on_long=d['on_long'] / d['floor'])
            self.det.set_external(d['floor'], d['phase'], d['why'])
        elif kind == 'stats':
            self.stats = d
        elif kind == 'state':
            self.board_states.append((self.now, d['motion']))
            self.board_motion = d['motion']
        elif kind == 'csi':
            self._csi(d)
        self._trim()

    def _csi(self, d):
        t, key = self.now, (d['src'], d['kind'])
        if self.channel is None:
            self.channel = (d['ch'], '')
        self.arrivals[key].append(t)
        self.rssi[key] = d['rssi']
        s = self.det.on_csi(d)
        self.dop.feed(d, t)
        r = self.br.feed(d, t)
        if r is not None:
            self.br_hist.append((t, r))
        self._scan(d, t)
        if self.on_csi:
            self.on_csi(d, t)
        a = d['amp'][VALID]
        if a.mean() > 0:
            self.wf[key].append((t, a / a.mean()))
        change = self.det.evaluate(d['ts'])
        f = self.det.floor
        w = self.wall()
        calibrating = f <= 0
        if calibrating and not (self.cal_spans and self.cal_spans[-1][1] is None):
            self.cal_spans.append([t, None])
            self.hist_cal.append([w, None])
        elif not calibrating and self.cal_spans and self.cal_spans[-1][1] is None:
            self.cal_spans[-1][1] = t
            if self.hist_cal and self.hist_cal[-1][1] is None:
                self.hist_cal[-1][1] = w
        ms = None
        if not calibrating:
            if s is not None:
                self.scores[key].append((t, s / f))
            ms, ml = self.det.max_scores(d['ts'])
            if ml is not None and (not self.long or t - self.long[-1][0] >= 0.05):
                self.long.append((t, ml / f))
            if ms is not None:
                sec = int(w)
                if self.hist_t and self.hist_t[-1] == sec:
                    self.hist_s[-1] = max(self.hist_s[-1], ms / f)
                    self.hist_l[-1] = max(self.hist_l[-1], ml / f)
                else:
                    self.hist_t.append(sec)
                    self.hist_s.append(ms / f)
                    self.hist_l.append(ml / f)
        if change > 0:
            self.motion_spans.append([t, None])
            self.hist_motion.append([w, None])
            self.hist_peak.append(0.0)
        elif change < 0 and self.motion_spans:
            self.motion_spans[-1][1] = t
            if self.hist_motion and self.hist_motion[-1][1] is None:
                self.hist_motion[-1][1] = w
        if ms is not None and self.hist_motion and self.hist_motion[-1][1] is None:
            self.hist_peak[-1] = max(self.hist_peak[-1], ms / f)

    def _scan(self, d, t):
        """Track router scans the detector is filtering (ACK gaps, see md_update).
        Router links only: a device that answers now and then (a smart plug
        asleep between ACKs) has long gaps too, but that isn't the router scanning."""
        st = self.det.streams.get((d['src'], 1))
        if d['kind'] != 1 or d['src'] >= MAX_APS or st is None or d['ts'] >= st.scan_until:
            return
        end = st.scan_until / 1e6
        w = self.wall()
        if self.scan_spans and t <= self.scan_spans[-1][1] + 1:
            self.scan_spans[-1][1] = max(self.scan_spans[-1][1], end)
            self.hist_scans[-1][1] = max(self.hist_scans[-1][1], self.wall(end))
        else:
            self.scan_spans.append([t, end])
            self.hist_scans.append([w, self.wall(end)])

    def _close_spans(self):
        """End open session spans (before a reboot / loop resets the board time)."""
        if self.wall_off is None:
            return
        for spans in (self.hist_motion, self.hist_cal):
            if spans and spans[-1][1] is None:
                spans[-1][1] = self.wall()

    def session_counts(self):
        """(motion periods, brief blips) so far, as record.py counts them."""
        now = self.last_wall or 0
        d = [(b if b is not None else now) - a for a, b in self.hist_motion]
        return sum(x >= BRIEF_S for x in d), sum(x < BRIEF_S for x in d)

    def recent_motion(self, n=4):
        """Newest first: (start wall, seconds, peak x quiet level, still going)."""
        now = self.last_wall or 0
        out = []
        for (a, b), peak in zip(self.hist_motion[-n:], self.hist_peak[-n:]):
            out.append((a, (b if b is not None else now) - a, peak, b is None))
        return out[::-1]

    def last_motion(self):
        """'MOTION for 12 s', 'last motion 3 min ago', or 'no motion yet'."""
        if not self.hist_motion:
            return 'no motion yet'
        a, b = self.hist_motion[-1]
        now = self.last_wall or a
        if b is None:
            return f"motion for {fmt_age(now - a)}"
        return f"last motion {fmt_age(now - b)} ago"

    def doppler_reading(self, dop=None):
        """The last second's Doppler (of `dop`, default the router's): None (no
        data), {} (nothing moving) or dict(f=Hz, speed=m/s, step=looks like a router step)."""
        rows = [r for r in (dop or self.dop).rows if r[0] >= self.now - 1.0]
        if not rows:
            return None
        if any(a - 1 <= self.now <= b for a, b in self.scan_spans):   # the router, not a body
            return dict(f=0.0, speed=0.0, step=True, scan=True)
        fs = [f['f'] for _t, _p, f in rows if f['moving']]
        if len(fs) < 2:                          # noise: one window, or no low-band energy
            return {}
        f = float(np.median(fs))
        low = np.median([x['low'] for _t, _p, x in rows])
        return dict(f=f, speed=f * self.dop_wavelength() / 2, step=low >= 0.75 and f < LOW_HZ)

    def doppler_text(self):
        """The Doppler reading as one line."""
        r = self.doppler_reading()
        if r is None:
            return 'Doppler: waiting'
        if not r:
            return 'Doppler: nothing moving'
        if r.get('scan'):
            return 'Doppler: router scan (ignored)'
        txt = f"Doppler {r['f']:4.1f} Hz (~{r['speed']:.2f} m/s)"
        return txt + ('  step-like: router?' if r['step'] else '')

    def dop_wavelength(self):
        from doppler import wavelength
        return wavelength(self.channel[0] if self.channel else 6)

    def source_status(self, silent_s=3.0):
        """None while data flows, else (badge, colour, detail) for the header."""
        if self.lost:
            why = self.lost if len(self.lost) <= 36 else self.lost[:35] + '…'
            return 'DISCONNECTED', MOTION_C, f"{why}  (replug + restart)"
        if self.ended:
            return 'INPUT ENDED', DIM, 'no more data (file or demo finished)'
        if self.last_rx is None:
            return 'WAITING', DIM, 'waiting for the board...'
        quiet = self.clock() - self.last_rx
        if quiet > silent_s:
            return 'NO DATA', BRIEF_C, f"board silent for {fmt_age(quiet)} - unplugged?"
        return None

    def link_health(self):
        """(word, colour, detail) for the link that decides motion."""
        if self.channel is None or not self.channel[0]:
            return 'SEARCHING', BRIEF_C, 'looking for the router'
        live = [k for k in self.arrivals if k[0] < MAX_APS and self.rate(k) > 0]
        if not live:
            return 'NO SIGNAL', MOTION_C, f"nothing from the router on ch {self.channel[0]}"
        acks = [k for k in live if k[1] == 1]
        key = max(acks or live, key=self.rate)
        rate, rssi = self.rate(key), self.rssi.get(key, 0)
        if acks and rate >= 60 and rssi >= -80:
            word, col = 'GOOD', STILL_C
        elif rate >= 15:
            word, col = 'WEAK', BRIEF_C
        else:
            word, col = 'POOR', MOTION_C
        return word, col, f"{self.label(key)} {rate:.0f}/s  {rssi} dBm  ch {self.channel[0]}"

    def _trim(self):
        t0 = self.now - self.window_s
        for dq in list(self.scores.values()) + [self.long]:
            while dq and dq[0][0] < t0:
                dq.popleft()
        for dq in self.arrivals.values():
            while dq and dq[0] < self.now - 2.0:
                dq.popleft()
        for dq in self.wf.values():
            while dq and dq[0][0] < self.now - self.waterfall_s:
                dq.popleft()
        while self.board_states and self.board_states[0][0] < t0:
            self.board_states.popleft()
        self.motion_spans = [s for s in self.motion_spans if s[1] is None or s[1] >= t0]
        self.scan_spans = [s for s in self.scan_spans if s[1] >= t0]
        while self.br_hist and self.br_hist[0][0] < t0:
            self.br_hist.popleft()
        self.cal_spans = [s for s in self.cal_spans if s[1] is None or s[1] >= t0]

    def label(self, key):
        """'router ACK', 'Espressif ACK', ... for a (node, kind) stream."""
        return f"{self.devices.link_label(key[0])} {'ACK' if key[1] else 'frames'}"

    def rate(self, key):
        dq = self.arrivals[key]
        return len(dq) / 2.0

    def busiest(self):
        live = [k for k, dq in self.wf.items() if dq]
        aps = [k for k in live if k[0] < MAX_APS]       # prefer the links that decide
        live = aps or live
        return max(live, key=lambda k: len(self.wf[k])) if live else None


def thin(t, v, n=2000):
    """At most ~n points for plotting, keeping each bin's peak."""
    if len(t) <= n:
        return t, v
    k = int(np.ceil(len(t) / n))
    m = len(t) // k * k
    tt = t[:m].reshape(-1, k)[:, 0]
    vv = v[:m].reshape(-1, k).max(1)
    return np.r_[tt, t[m:]], np.r_[vv, v[m:]]


class View:
    def __init__(self, state, plt, log_name=None, window=WINDOWS[0], send=None):
        self.s, self.plt = state, plt
        self.send = send                         # send(cmd) to the board, or None
        self.on_network = None                   # 'n' / Network button: open the picker
        self.view_s = window
        self.paused = False
        self.log_name = log_name
        plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9,
                             'text.color': FG, 'axes.labelcolor': DIM,
                             'xtick.color': DIM, 'ytick.color': DIM})
        for km, key in (('keymap.back', 'c'), ('keymap.home', 'h')):   # ours, not the toolbar's
            if key in plt.rcParams[km]:
                plt.rcParams[km] = [k for k in plt.rcParams[km] if k != key]
        self.toast_text, self.toast_until = '', 0.0
        self.fig = plt.figure(figsize=(11.5, 9.4), facecolor=BG)
        try:
            self.fig.canvas.manager.set_window_title('CSI motion sensor')
        except AttributeError:
            pass
        self.details = False                     # 'd': raw firmware counters
        self.bottom = 'doppler'                  # 'w': 'doppler' or 'subcarriers'
        # header | live window: motion score, Doppler, breathing | whole session
        gs = self.fig.add_gridspec(5, 1, height_ratios=[0.62, 1.75, 0.72, 0.42, 0.9],
                                   left=0.07, right=0.97, top=0.975, bottom=0.05, hspace=0.5)
        self.ax_head = self.fig.add_subplot(gs[0])
        self.ax_score = self.fig.add_subplot(gs[1])
        self.ax_wf = self.fig.add_subplot(gs[2])
        self.ax_br = self.fig.add_subplot(gs[3])
        self.ax_tl = self.fig.add_subplot(gs[4])
        for ax in (self.ax_score, self.ax_tl, self.ax_wf, self.ax_br):
            ax.set_facecolor(PANEL)
            for sp in ax.spines.values():
                sp.set_color(GRID_C)
        self.ax_head.axis('off')
        head = dict(ha='left', transform=self.ax_head.transAxes)
        mono = dict(head, family='DejaVu Sans Mono')
        self.badge = self.ax_head.text(0.0, 0.58, '', fontsize=24, fontweight='bold',
                                       va='center', **head)
        self.since = self.ax_head.text(0.0, 0.16, '', fontsize=9.5, va='center', color=DIM,
                                       **head)
        self.health = self.ax_head.text(0.2, 1.0, '', fontsize=9.5, fontweight='bold',
                                        va='top', **mono)
        # three tiles: label / big value / small detail
        self.tiles = {}
        for i, name in enumerate(('breathing', 'doppler', 'quiet level')):
            x = 0.2 + i * 0.152
            self.tiles[name] = (
                self.ax_head.text(x, 0.66, name.upper(), fontsize=8, fontweight='bold',
                                  color=DIM, va='center', **head),
                self.ax_head.text(x, 0.40, '', fontsize=13, fontweight='bold', va='center', **head),
                self.ax_head.text(x, 0.14, '', fontsize=8, color=DIM, va='center', **head))
        self.footer = self.ax_head.text(0.2, -0.12, '', fontsize=8, color=DIM, va='center',
                                        **head)
        self.info = self.ax_head.text(0.2, 0.72, '', fontsize=9, va='top', color=FG, **mono)
        self.recent = self.ax_head.text(0.66, 0.54, '', fontsize=8.5, va='top', color=FG,
                                        linespacing=1.25, **mono)
        self.ax_score.set_ylabel('motion score')
        self.ax_wf.set_ylabel('subcarrier')
        self.ax_wf.set_xlabel('time (s)')
        self.wf_img = None
        self.help = self.fig.text(
            0.5, 0.55, HELP, ha='center', va='center', multialignment='left', fontsize=10, color=FG,
            family='DejaVu Sans Mono',
            linespacing=1.5, zorder=10, visible=False,
            bbox=dict(boxstyle='round,pad=1.2', facecolor=BG, edgecolor=DIM, alpha=0.97))
        self._buttons()
        self.fig.canvas.mpl_connect('key_press_event', self._key)

    def _buttons(self):
        from matplotlib.widgets import Button
        self.btns = {}
        labels = [('30 s', WINDOWS[0]), ('2 min', WINDOWS[1]), ('10 min', WINDOWS[2])]
        for i, (lab, w) in enumerate(labels):
            ax = self.fig.add_axes([0.70 + i * 0.062, 0.935, 0.056, 0.035])
            b = Button(ax, lab, color=PANEL, hovercolor=GRID_C)
            b.label.set_color(FG)
            b.on_clicked(lambda _e, w=w: self.set_window(w))
            self.btns[w] = b
        ax = self.fig.add_axes([0.70 + 3 * 0.062, 0.935, 0.07, 0.035])
        self.btn_pause = Button(ax, 'Pause', color=PANEL, hovercolor=GRID_C)
        self.btn_pause.label.set_color(FG)
        self.btn_pause.on_clicked(lambda _e: self.toggle_pause())
        self.btn_cal = None
        if self.send:
            ax = self.fig.add_axes([0.70 - 0.095, 0.935, 0.088, 0.035])
            self.btn_cal = Button(ax, 'Recalibrate', color=PANEL, hovercolor=GRID_C)
            self.btn_cal.label.set_color(FG)
            self.btn_cal.on_clicked(lambda _e: self.recalibrate())
            ax = self.fig.add_axes([0.70 - 0.095 - 0.078, 0.935, 0.072, 0.035])
            self.btn_net = Button(ax, 'Network', color=PANEL, hovercolor=GRID_C)
            self.btn_net.label.set_color(FG)
            self.btn_net.on_clicked(lambda _e: self.open_network())
        self._mark_buttons()

    def _mark_buttons(self):
        for w, b in self.btns.items():
            b.label.set_fontweight('bold' if w == self.view_s else 'normal')
            b.ax.set_facecolor(SEL_C if w == self.view_s else PANEL)
            b.color = SEL_C if w == self.view_s else PANEL
        self.btn_pause.label.set_text('Live' if self.paused else 'Pause')
        self.btn_pause.label.set_color(BRIEF_C if self.paused else FG)

    def set_window(self, w):
        self.view_s = w
        self._mark_buttons()
        if not self.paused:
            self.draw()

    def toggle_pause(self):
        self.paused = not self.paused
        self._mark_buttons()
        if not self.paused:
            self.draw()
        else:
            self.fig.canvas.draw_idle()

    def toast(self, text, secs=5.0):
        """A short confirmation in the footer line."""
        self.toast_text, self.toast_until = text, time.monotonic() + secs
        self._header()
        self.fig.canvas.draw_idle()

    def recalibrate(self):
        """Same as the board's button: re-measure the quiet level in 5 s."""
        if self.send:
            self.send('CAL')
            self.toast('Recalibrating: step away - the quiet level is measured again in 5 s')
        else:
            self.toast('Recalibrate needs a board (this is a demo or a file)')

    def open_network(self):
        if self.on_network:
            self.on_network()
        else:
            self.toast('Choosing a network needs a board (this is a demo or a file)')

    def _key(self, e):
        if e.key == 'n':
            self.open_network()
        elif e.key == ' ':
            self.toggle_pause()
        elif e.key == 'c':
            self.recalibrate()
        elif e.key in ('h', '?'):
            self.help.set_visible(not self.help.get_visible())
            self.fig.canvas.draw_idle()
        elif e.key == 'escape' and self.help.get_visible():
            self.help.set_visible(False)
            self.fig.canvas.draw_idle()
        elif e.key == 'w':
            self.bottom = 'subcarriers' if self.bottom == 'doppler' else 'doppler'
            self.draw()
            self.fig.canvas.draw_idle()
        elif e.key == 'd':
            self.details = not self.details
            self.draw()
            self.fig.canvas.draw_idle()
        elif e.key in ('1', '2', '3'):
            self.set_window(WINDOWS[int(e.key) - 1])

    def _header(self):
        """Badge, link health, quiet level and the recent-motion list."""
        s, det = self.s, self.s.det
        status = s.source_status()
        if status:                               # no live data: say so instead of a stale verdict
            word, col, detail = status
            self.badge.set_text(word)
            self.badge.set_color(col)
            self.badge.set_fontsize(15 if len(word) > 8 else 22)
        elif det.floor <= 0:
            self.badge.set_text('CALIBRATING')
            self.badge.set_color(CAL_C)
            self.badge.set_fontsize(15)
        else:
            self.badge.set_text('MOTION' if det.motion else 'STILL')
            self.badge.set_color(MOTION_C if det.motion else STILL_C)
            self.badge.set_fontsize(22)
        self.since.set_text('' if status else s.last_motion())   # its clock stops with the data
        word, col, detail = status or s.link_health()
        self.health.set_text(detail if status else f"link {word}   {detail}")
        self.health.set_color(col)

        board = {None: 'n/a', True: 'MOTION', False: 'still'}[s.board_motion]
        src = 'board' if det.external else 'PC'
        dv = s.devices
        if det.floor > 0:
            quiet = f"quiet {det.floor:.4f} ({det.phase}, {src})"
        else:
            quiet = f"measuring quiet level ({det.phase}, {det.why}) - keep still"
        for parts in self.tiles.values():
            for p in parts:
                p.set_visible(not self.details)
        self.footer.set_visible(not self.details)
        self.info.set_visible(self.details)
        if not self.details:
            self._tiles(board, src)
            if status:                           # stale numbers: grey them out
                for _lab, val, _sub in self.tiles.values():
                    val.set_color(DIM)
            rows = ['']
            self.footer.set_text(
                f"board says {board}  ·  {len(dv.devs)} devices heard, poking {len(dv.poking)}"
                f"/{MAX_DEVS} ({dv.mode})  ·  h help"
                + ('   ·   PAUSED' if self.paused else ''))
            if time.monotonic() < self.toast_until:
                self.footer.set_text(self.toast_text)
            self.footer.set_color(FG if time.monotonic() < self.toast_until else DIM)
        else:
            ch = f"ch {s.channel[0]} ({s.channel[1] or 'start'})" if s.channel else 'searching'
            rows = [f"{ch}   {quiet}, why {det.why}"]
            streams = sorted(set(s.arrivals) | set(s.scores))
            live = [k for k in streams if s.rate(k) > 0]
            rows.append('  '.join(f"{s.label(k)} {s.rate(k):.0f}/s {s.rssi.get(k, 0)}dBm"
                                  for k in live[:3]) or 'waiting for CSI...')
            if s.stats:
                st = s.stats
                ratio = st['ack'] / st['poke'] if st['poke'] else 0
                rows.append(f"pokes {st['poke']}  ACK CSI {st['ack']} ({ratio:.0%})  "
                            f"tx err {st['tx_err']}")
                rows.append(f"queue drops {st['qfull']}  USB lost {st.get('lost', 0)}  router replies {st.get('reply', 0)}"
                            f"  APs {len(s.nodes)}   d = less")
        if self.paused and self.details:
            rows[0] += '   [PAUSED]'
        self.info.set_text('\n'.join(rows))

        lines = ['recent motion           peak']
        for a, dur, peak, going in s.recent_motion():
            what = f"now {fmt_age(dur)}" if going else fmt_age(dur)
            if not going and dur < BRIEF_S:
                what = 'brief'
            lines.append(f"{time.strftime('%H:%M:%S', time.localtime(a))}  "
                         f"{what:12s} x{peak:.1f}")
        if len(lines) == 1:
            lines.append('none yet')
        self.recent.set_text('\n'.join(lines))

    def _tiles(self, board, src):
        """Fill the breathing / Doppler / quiet-level tiles."""
        s, det = self.s, self.s.det
        def put(name, value, color, detail):
            _lab, val, sub_ = self.tiles[name]
            val.set_text(value)
            val.set_color(color)
            sub_.set_text(detail)
        r = s.br.last
        if r is None:
            put('breathing', '…', DIM, 'collecting 40 s')
        elif r['level'] == 'none':
            put('breathing', '—', DIM, 'none found')
        else:
            put('breathing', f"{r['rate']:.0f} /min", STILL_C if r['level'] == 'clear' else BRIEF_C,
                f"{r['level']} · last 40 s")
        d = s.doppler_reading()
        if d is None:
            put('doppler', '…', DIM, 'collecting')
        elif not d:
            put('doppler', '—', DIM, 'nothing moving')
        elif d.get('scan'):
            put('doppler', 'scan', SCAN_C, 'router scanning (ignored)')
        elif d['step']:
            put('doppler', f"{d['f']:.1f} Hz", BRIEF_C, 'step-like: router?')
        else:
            put('doppler', f"{d['f']:.1f} Hz", FG, f"~{d['speed']:.2f} m/s")
        if det.floor > 0:
            put('quiet level', f"{det.floor:.4f}", FG, f"{det.phase} · from {src}")
        else:
            put('quiet level', 'measuring', CAL_C, 'keep still')

    def _scans(self, ax, t0, label_y=None):
        """Grey bands where the detector was filtering a router scan."""
        first = True
        for a, b in self.s.scan_spans:
            if b < t0:
                continue
            ax.axvspan(max(a, t0), b, color=SCAN_C, alpha=0.16, lw=0)
            if label_y is not None and first:
                ax.text(max(a, t0), label_y, ' router scan (ignored)', color=SCAN_C,
                        fontsize=8, va='top')
                first = False

    def _breathing(self, t0, t1):
        """Breathing-rate strip on the same clock as the score plot."""
        s, ax = self.s, self.ax_br
        ax.cla()
        ax.set_facecolor(PANEL)
        ax.axhspan(12, 20, color=STILL_C, alpha=0.05, lw=0)      # typical resting range
        pts = [(t, r) for t, r in s.br_hist if t >= t0]
        # filled = clear, hollow = possible: readable without the colours too
        for level, col, size, fill in (('possible', BRIEF_C, 18, False), ('clear', STILL_C, 22, True)):
            p = [(t, r['rate']) for t, r in pts if r['level'] == level]
            if p:
                ax.scatter(*zip(*p), s=size, zorder=3, linewidths=1.1,
                           facecolors=col if fill else 'none', edgecolors=col)
        none = [t for t, r in pts if r['level'] == 'none']
        if none:
            ax.scatter(none, [1.5] * len(none), s=4, color=DIM, lw=0, alpha=0.6)
        ax.set_xlim(t0, t1)
        ax.set_ylim(0, 36)
        ax.set_yticks([0, 12, 24, 36])
        ax.set_ylabel('/min')
        ax.grid(color=GRID_C, lw=0.6)
        self._clock_axis(ax)
        ax.set_title("breathing rate (experimental): filled green = clear, hollow amber = possible; "
                     "needs someone still near the board-router line, covers the last 40 s",
                     color=DIM, fontsize=8.5, loc='left')

    def draw(self):
        s, ax = self.s, self.ax_score
        det = s.det
        self._header()
        if self.paused:
            return
        streams = sorted(set(s.arrivals) | set(s.scores))
        ax.cla()
        ax.set_facecolor(PANEL)
        win = self.view_s
        t1 = max(s.now, win)
        t0 = t1 - win
        for a, b in s.motion_spans:
            ax.axvspan(max(a, t0), b if b is not None else t1, color=MOTION_C, alpha=0.13, lw=0)
        for a, b in s.cal_spans:
            ax.axvspan(max(a, t0), b if b is not None else t1, color=CAL_C, alpha=0.12, lw=0)
        for t, m in s.board_states:
            ax.axvline(t, color=MOTION_C if m else STILL_C, lw=1, ls=':', alpha=0.9)
        on_s, on_l = det.c['on_short'], det.c['on_long']
        ymax = on_s * 1.6
        for i, key in enumerate(streams):
            pts = s.scores.get(key)
            if not pts:
                continue
            t, v = np.array(pts).T
            keep = t >= t0
            if not keep.any():
                continue
            t, v = thin(t[keep], v[keep])
            ymax = max(ymax, v.max() * 1.15)
            gaps = np.where(np.diff(t) > max(0.5, win / 600))[0] + 1   # break over gaps
            t, v = np.insert(t, gaps, np.nan), np.insert(v, gaps, np.nan)
            is_ap = key[0] < MAX_APS
            ax.plot(t, v, color=COLORS[(key[0] * 2 + key[1]) % len(COLORS)],
                    lw=1.1 if is_ap else 0.8, ls=('-' if key[1] else '--') if is_ap else ':',
                    alpha=0.85 if is_ap else 0.7,
                    label=f"{s.label(key)}" + ('' if is_ap else ' (not in verdict)'))
        if s.long:
            t, v = np.array(s.long).T
            keep = t >= t0
            t, v = thin(t[keep], v[keep]) if keep.any() else (t[-1:], v[-1:])
            ymax = max(ymax, v.max() * 1.15)
            gaps = np.where(np.diff(t) > max(0.5, win / 600))[0] + 1
            t, v = np.insert(t, gaps, np.nan), np.insert(v, gaps, np.nan)
            ax.plot(t, v, color=LONG_C, lw=2.2, label='long (4 s)')
        self._scans(ax, t0, label_y=ymax * 0.97)
        for h in s.hops:
            if h >= t0:
                ax.axvline(h, color=FG, lw=0.8, alpha=0.5)
                ax.text(h, ymax * 0.02, ' channel hop', color=FG, fontsize=8, va='bottom')
        ax.axhline(1.0, color=DIM, lw=0.8, ls=':', alpha=0.8)
        ax.axhline(on_s, color=MOTION_C, lw=0.9, ls='--', alpha=0.8)
        ax.axhline(on_l, color=LONG_C, lw=0.9, ls='--', alpha=0.8)
        lab = t1 - win * 0.006                   # right edge: clear of channel-hop labels
        ax.text(lab, 1.0, 'quiet level ', color=DIM, va='top', ha='right', fontsize=8)
        ax.text(lab, on_s, f'short threshold x{on_s:.1f} ', color=MOTION_C,
                va='bottom', ha='right', fontsize=8)
        ax.text(lab, on_l, f'long threshold x{on_l:.1f} ', color=LONG_C,
                va='bottom', ha='right', fontsize=8)
        if det.floor <= 0:
            ax.text((t0 + t1) / 2, ymax * 0.5, 'calibrating: measuring the quiet level',
                    color=FG, ha='center', fontsize=11, alpha=0.8)
        ax.set_xlim(t0, t1)
        ax.set_ylim(0, ymax)
        self._clock_axis(ax)
        ax.set_ylabel('motion score (x quiet level)')
        ax.grid(color=GRID_C, lw=0.6)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc='upper right', facecolor=PANEL, edgecolor=GRID_C,
                      labelcolor=FG, fontsize=8, ncol=4)

        self._timeline()
        self._breathing(t0, t1)
        self.ax_wf.cla()
        self.ax_wf.set_facecolor(PANEL)
        self.ax_wf.set_xlabel('')
        if self.bottom == 'doppler':
            self._doppler(t0, t1)
            self.fig.canvas.draw_idle()
            return
        key = s.busiest()
        self.ax_wf.set_ylabel('subcarrier')
        if key:
            ts, vecs = zip(*s.wf[key])
            m = np.array(vecs).T
            self.ax_wf.imshow(m, aspect='auto', origin='lower', cmap='magma', vmin=0, vmax=2,
                              extent=(ts[0], ts[-1] if len(ts) > 1 else ts[0] + 0.1, 0, len(VALID)),
                              interpolation='nearest')
            self.ax_wf.set_xlim(ts[0], ts[-1] if len(ts) > 1 else ts[0] + 0.1)
            self._clock_axis(self.ax_wf)
            self.ax_wf.set_title(f"normalised amplitude, {s.label(key)}",
                                 color=DIM, fontsize=8.5, loc='left')
        self.fig.canvas.draw_idle()


    def _doppler(self, t0, t1):
        """Doppler spectrogram over the same window as the score plot."""
        s, ax = self.s, self.ax_wf
        ax.set_ylabel('Hz')
        rows = [r for r in s.dop.rows if r[0] >= t0]
        if len(rows) > 1 and s.dop.base is not None:
            ts = [r[0] for r in rows]
            m = np.array([r[1] for r in rows]).T
            db = 10 * np.log10(np.maximum(m, 1e-12) / np.maximum(s.dop.base, 1e-12)[:, None])
            ax.imshow(np.clip(db, 0, 20), aspect='auto', origin='lower', cmap='magma', vmin=0,
                      vmax=20, extent=(ts[0], ts[-1], 0, FREQS[-1]), interpolation='nearest')
            ax.set_xlim(t0, t1)
            self._clock_axis(ax)
        ax.set_xlim(t0, t1)
        self._scans(ax, t0)
        ax.set_ylim(0, FREQS[-1])
        ax.axhline(LOW_HZ, color=DIM, lw=0.6, ls=':')
        ax.set_title("Doppler of the router ACKs, dB over the quiet room (experimental): movement = "
                     "lasting low band with streaks upward, router scan = 1 s blob under 3 Hz",
                     color=DIM, fontsize=8.5, loc='left')

    def _clock_axis(self, ax):
        """Label a board-time axis with the wall clock."""
        from matplotlib.ticker import FuncFormatter
        s = self.s
        if s.wall_off is not None:
            ax.xaxis.set_major_formatter(FuncFormatter(
                lambda x, _p: time.strftime('%H:%M:%S', time.localtime(s.wall(x)))))

    def _timeline(self):
        """Whole session on the wall clock: peak scores per second, motion periods."""
        import matplotlib.dates as mdates
        s, ax = self.s, self.ax_tl
        ax.cla()
        ax.set_facecolor(PANEL)
        ax.grid(color=GRID_C, lw=0.6)
        on_s, on_l = s.det.c['on_short'], s.det.c['on_long']
        ymax = on_s * 1.8
        day = 86400.0                             # matplotlib dates: days since 1970
        end = (s.last_wall or time.time())
        start = s.hist_t[0] if s.hist_t else end - 60
        if end - start < 60:
            start = end - 60
        for a, b in s.hist_cal:
            b = b if b is not None else end
            ax.axvspan(a / day, b / day, color=CAL_C, alpha=0.18, lw=0)
        full = [(a, (b if b is not None else end) - a) for a, b in s.hist_motion]
        long_ = [(a / day, max(d, 1) / day) for a, d in full if d >= BRIEF_S]
        brief = [a / day for a, d in full if d < BRIEF_S]
        if long_:
            ax.broken_barh(long_, (0, ymax), color=MOTION_C, alpha=0.35, lw=0)
        if brief:
            ax.vlines(brief, 0, ymax * 0.25, color=BRIEF_C, lw=0.8, alpha=0.8)
        if s.hist_scans:                          # router scans the detector ignored
            ax.vlines([a / day for a, _b in s.hist_scans], ymax * 0.85, ymax, color=SCAN_C,
                      lw=1.0, alpha=0.9)
        if s.hist_t:
            t = np.array(s.hist_t, float) + 0.5
            for vals, col, lw in ((s.hist_s, COLORS[0], 0.7), (s.hist_l, LONG_C, 1.1)):
                tt, vv = thin(t, np.minimum(np.array(vals), ymax * 0.98), 1500)
                gaps = np.where(np.diff(tt) > max(5, (end - start) / 300))[0] + 1
                tt, vv = np.insert(tt, gaps, np.nan), np.insert(vv, gaps, np.nan)
                ax.plot(tt / day, vv, color=col, lw=lw, alpha=0.85)
        ax.axhline(on_s, color=MOTION_C, lw=0.7, ls='--', alpha=0.6)
        ax.axhline(on_l, color=LONG_C, lw=0.7, ls='--', alpha=0.6)
        ax.set_xlim(start / day, end / day)
        ax.set_ylim(0, ymax)
        tz = datetime.datetime.now().astimezone().tzinfo
        loc = mdates.AutoDateLocator(tz=tz, minticks=4, maxticks=10)
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M' if end - start > 300 else '%H:%M:%S', tz=tz))
        n_long, n_brief = s.session_counts()
        since = time.strftime('%H:%M', time.localtime(start))
        title = (f"session since {since}: {n_long} motion period(s), {n_brief} brief blip(s), "
                 f"{len(s.hist_scans)} router scan(s) ignored   (red = motion, orange = brief, "
                 "grey ticks at top = router scan, grey = calibrating)")
        if self.log_name:
            title += f"   logging to {self.log_name}"
        ax.set_title(title, color=DIM, fontsize=8.5, loc='left')
        ax.set_ylabel('peak / s')


class DevicePanel:
    """Tk window listing the devices on our network. Click a row's first
    column, double-click a row, or select it and press Space / Enter to poke
    / stop poking it."""

    COLS = (('poke', 'Poke', 46), ('name', 'Device (maker)', 170), ('mac', 'MAC', 130),
            ('rssi', 'Signal', 66), ('tx', 'Sent', 62), ('rate', 'Frames/min', 92),
            ('heard', 'Last heard', 118), ('status', 'Looks', 330), ('link', 'Poke link', 170))

    def __init__(self, master, state, send, on_change=None):
        import tkinter as tk
        from tkinter import ttk
        self.s, self.send, self.on_change = state, send, on_change
        self.win = tk.Toplevel(master)
        self.win.title('Devices on your network')
        self.win.geometry('1240x380')
        self.win.minsize(900, 240)
        self.win.configure(bg=BG)
        self.win.protocol('WM_DELETE_WINDOW', self.win.withdraw)   # hide, don't kill
        st = ttk.Style(self.win)
        try:
            st.theme_use('clam')
        except tk.TclError:
            pass
        st.configure('D.Treeview', background=PANEL, fieldbackground=PANEL, foreground=FG,
                     rowheight=22, borderwidth=0)
        st.configure('D.Treeview.Heading', background=HEAD_C, foreground=FG, relief='flat')
        st.map('D.Treeview', background=[('selected', SEL_C)])
        st.configure('D.TRadiobutton', background=BG, foreground=FG)
        st.map('D.TRadiobutton', background=[('active', BG)])
        st.configure('D.TLabel', background=BG, foreground=DIM)

        top = tk.Frame(self.win, bg=BG)
        top.pack(fill='x', padx=10, pady=(8, 4))
        ttk.Label(top, text='Poke:', style='D.TLabel').pack(side='left')
        self.mode = tk.StringVar(value=state.devices.mode)
        names = {'auto': 'Auto (stationary only)', 'all': 'All active (max 4)',
                 'manual': 'Manual', 'off': 'Off (router only)'}
        for m in MODES:
            ttk.Radiobutton(top, text=names[m], value=m, variable=self.mode, style='D.TRadiobutton',
                            command=self._mode).pack(side='left', padx=6)
        self.note = ttk.Label(top, text='', style='D.TLabel')
        self.note.pack(side='right')

        self.tree = ttk.Treeview(self.win, columns=[c for c, _, _ in self.COLS], show='headings',
                                 style='D.Treeview', selectmode='browse')
        for c, title, w in self.COLS:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor='w', stretch=c == 'status')
        self.tree.tag_configure('poked', foreground=POKE_C)
        self.tree.tag_configure('stationary', foreground=STILL_C)
        self.tree.tag_configure('dim', foreground=DIM)
        self.tree.pack(fill='both', expand=True, padx=10)
        self.tree.bind('<Button-1>', self._click)
        self.tree.bind('<Double-1>', lambda e: self._toggle(self.tree.identify_row(e.y)))
        for key in ('<space>', '<Return>'):      # keyboard: select a row, then Space / Enter
            self.tree.bind(key, lambda _e: self._toggle(self.tree.focus()) or 'break')

        self.foot = ttk.Label(self.win, style='D.TLabel', text=(
            'Only devices talking to your own network (your SSID) are listed. The board is '
            '2.4 GHz only: devices on 5 GHz never appear.\nOnly the router link decides '
            'motion; device links are extra, plotted dotted. Phones use private MACs and '
            'move, so Auto skips them.\nTo poke a device: tick its box, double-click it, or '
            'select it with the arrow keys and press Space.'))
        self.foot.pack(fill='x', padx=10, pady=(4, 8))

    def _mode(self):
        self._send(self.s.devices.set_mode(self.mode.get()))

    def _click(self, e):
        if self.tree.identify_column(e.x) == '#1':           # the Poke column
            self._toggle(self.tree.identify_row(e.y))

    def _toggle(self, mac):
        if mac:
            self._send(self.s.devices.toggle(mac))
            self.mode.set(self.s.devices.mode)

    def _send(self, cmds):
        for c in cmds:
            if self.send:
                self.send(c)
        if self.on_change:
            self.on_change()
        self.refresh()

    def refresh(self):
        dv = self.s.devices
        rows = dv.rows()
        seen = set()
        for r in rows:
            mac = r['mac']
            seen.add(mac)
            box = '\u2611' if r['poked'] else ('\u2610' if not r['picked'] else '\u2026')
            vals = (box, r['name'], mac, f"{r['rssi']} dBm" if r['rssi'] else '-',
                    r['tx'], f"{r['rate'] * 60:.0f}", fmt_age(r['heard']) + ' ago',
                    r['why'], r['link'])
            tag = ('poked',) if r['poked'] else ('stationary',) if r['status'] == 'stationary' \
                else ('dim',) if r['status'] in ('gone', 'quiet', 'random') else ()
            if self.tree.exists(mac):
                self.tree.item(mac, values=vals, tags=tag)
            else:
                self.tree.insert('', 'end', iid=mac, values=vals, tags=tag)
        for iid in self.tree.get_children():
            if iid not in seen:
                self.tree.delete(iid)
        for i, r in enumerate(rows):                         # keep the sort order
            self.tree.move(r['mac'], '', i)
        n_st = sum(r['status'] == 'stationary' for r in rows)
        if not dv.board_seen:
            txt = 'waiting for the board...'
        elif not rows:
            txt = 'no devices heard on your network yet (2.4 GHz only; give it a minute)'
        else:
            txt = f"{len(rows)} heard, {n_st} stationary, poking {len(dv.poking)}/{MAX_DEVS}"
        if not self.send:
            txt += '   (no board: commands not sent)'
        self.note.configure(text=txt)


class NetworkPanel:
    """Tk window: which network the board watches, and a list of nearby ones
    to switch to. The choice is saved on the board (it restarts), so it stays
    after unplugging; 'Back to default' returns to the name built in from
    main/my_network.h."""

    def __init__(self, master, state, send):
        import tkinter as tk
        from tkinter import ttk
        self.s, self.send, self.tk = state, send, tk
        self.win = tk.Toplevel(master)
        self.win.title('Network to watch')
        self.win.geometry('560x420')
        self.win.minsize(460, 300)
        self.win.configure(bg=BG)
        self.win.protocol('WM_DELETE_WINDOW', self.win.withdraw)
        self.win.withdraw()                      # opened with 'n' / the Network button
        st = ttk.Style(self.win)
        st.configure('N.Treeview', background=PANEL, fieldbackground=PANEL, foreground=FG,
                     rowheight=24, borderwidth=0)
        st.configure('N.Treeview.Heading', background=HEAD_C, foreground=FG, relief='flat')
        st.map('N.Treeview', background=[('selected', SEL_C)])
        st.configure('N.TLabel', background=BG, foreground=FG)
        st.configure('N.Dim.TLabel', background=BG, foreground=DIM)
        self.now = ttk.Label(self.win, style='N.TLabel', font=('DejaVu Sans', 11, 'bold'))
        self.now.pack(fill='x', padx=12, pady=(10, 2))
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill='x', padx=12, pady=4)
        ttk.Button(bar, text='Scan', command=self.scan).pack(side='left')
        ttk.Button(bar, text='Watch selected', command=self.use).pack(side='left', padx=6)
        ttk.Button(bar, text='Back to default', command=self.default).pack(side='left')
        self.note = ttk.Label(bar, style='N.Dim.TLabel')
        self.note.pack(side='right')
        cols = (('name', 'Network', 300), ('ch', 'Channel', 70), ('sig', 'Signal', 110))
        self.tree = ttk.Treeview(self.win, columns=[c for c, _, _ in cols], show='headings',
                                 style='N.Treeview', selectmode='browse')
        for c, title, w in cols:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor='w', stretch=c == 'name')
        self.tree.tag_configure('cur', foreground=STILL_C)
        self.tree.pack(fill='both', expand=True, padx=12)
        self.tree.bind('<Double-1>', lambda e: self.use())
        self.tree.bind('<Return>', lambda e: self.use())
        ttk.Label(self.win, style='N.Dim.TLabel', wraplength=520, text=(
            'Pick your own router. The board only listens and never joins, so no password '
            'is needed. It is 2.4 GHz only: 5 GHz-only networks are not listed. The choice '
            'is saved on the board, which restarts and recalibrates (about 15 s).')).pack(
            fill='x', padx=12, pady=(6, 10))
        self.shown = None

    def open(self):
        self.win.deiconify()
        self.win.lift()
        if self.s.nets_state == 'idle':
            self.scan()
        self.refresh()

    def scan(self):
        self.s.start_scan()
        self.refresh()

    def _ask(self, title, text):
        from tkinter import messagebox
        return messagebox.askyesno(title, text, parent=self.win)

    def use(self):
        sel = self.tree.focus()
        if not sel:
            self.note.configure(text='select a network first')
            return
        name = self.s.nets[int(sel)][0]
        if self._ask('Switch network', f'Watch "{name}" from now on?\n\nThe board restarts '
                     'and recalibrates (about 15 s). Only pick your own network.'):
            self.s.outbox.append(f'USE,{name}')
            self.note.configure(text='switching...')

    def default(self):
        if self._ask('Back to default', 'Go back to the network built into the firmware '
                     '(main/my_network.h)?\n\nThe board restarts (about 15 s).'):
            self.s.outbox.append('USE_DEFAULT')
            self.note.configure(text='switching...')

    def refresh(self):
        if not self.win.winfo_viewable():
            return
        how, name = self.s.network or ('?', '')
        self.now.configure(text={'none': 'Not watching any network yet: pick one below',
                                 'switching': f'Switching to "{name}"...',
                                 '?': 'Waiting for the board...'}.get(
            how, f'Watching "{name}"' + ('  (default)' if how == 'default' else '  (chosen here)')))
        st = self.s.nets_state
        if st == 'scanning':
            self.note.configure(text='scanning all channels (about 4 s)...')
        elif st == 'done' and self.note.cget('text').startswith('scanning'):
            self.note.configure(text=f'{len(self.s.nets)} networks nearby')
        key = (st, len(self.s.nets), name)
        if key == self.shown:
            return
        self.shown = key
        self.tree.delete(*self.tree.get_children())
        for i, (n, ch, rssi) in enumerate(self.s.nets):
            bars = '\u2588' * max(1, min(4, (rssi + 95) // 10)) if rssi > -95 else '\u2581'
            self.tree.insert('', 'end', iid=str(i), values=(n, ch, f'{bars}  {rssi} dBm'),
                             tags=('cur',) if n == name else ())


def reader(lines, q, log=None):
    try:
        for line in lines:
            if log:
                try:
                    log.feed(line)               # in this thread: never waits for drawing
                except Exception as e:           # one odd line must not stop the stream
                    print(f"log: skipped a line ({e})", file=sys.stderr)
            q.put(line)
    except Exception as e:                       # serial unplugged etc.
        q.put(f"#ERR {e}")
    q.put(None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('source', help="serial port (/dev/ttyUSB0, COM5) or '-' for stdin")
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--window', type=float, default=WINDOWS[0], help='seconds shown at first')
    ap.add_argument('--snapshot', metavar='PNG', help='read all input, save one image, exit')
    ap.add_argument('--no-log', action='store_true', help="don't log to sessions/")
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sessions'), help='log folder')
    a = ap.parse_args()

    state = State(max(WINDOWS[-1], a.window))

    if a.snapshot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        for line in open_source(a.source, a.baud):
            state.feed(line)
        view = View(state, plt, window=a.window)
        view.draw()
        plt.savefig(a.snapshot, dpi=130, facecolor=BG)
        print(f"saved {a.snapshot} ({state.n_lines} lines)")
        return

    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    log = awake = None
    if a.source != '-' and not a.no_log:          # a real board: log like record.py
        from record import SessionLog, keep_awake
        log = SessionLog(a.out)
        awake = keep_awake('sleep')               # no suspend while logging; screen may blank
        print(f"Logging to {log.ev_path}\n  (and {os.path.basename(log.raw_path)});"
              " close the window to stop.")
    prefs = os.path.join(a.out, 'devices.json')
    mode, picks = Devices.load_prefs(prefs)
    state.devices = Devices(oui=Oui(), mode=mode, manual=picks)
    lines, send = open_port(a.source, a.baud)
    state.live = send is not None
    if send:                                     # buzzer choice made in the web dashboard
        try:
            import json
            with open(os.path.join(a.out, 'dashboard.json')) as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                saved = {}
            if saved.get('sound'):
                state.want_sound(*saved['sound'])
            if saved.get('sens'):
                state.want_sens(saved['sens'])
        except (OSError, ValueError, TypeError):
            pass
    q = queue.Queue()
    threading.Thread(target=reader, args=(lines, q, log), daemon=True).start()
    view = View(state, plt, os.path.basename(log.ev_path) if log else None, a.window, send)
    panel = None
    try:
        view.fig.canvas.get_tk_widget().winfo_toplevel().minsize(1000, 820)   # header stays readable
    except Exception:                            # not the Tk backend
        pass
    try:
        panel = DevicePanel(view.fig.canvas.get_tk_widget().winfo_toplevel(), state, send,
                            on_change=lambda: (os.makedirs(a.out, exist_ok=True),
                                               state.devices.save(prefs)))
    except Exception as e:                       # not the Tk backend: plots only
        print(f"(device list unavailable: {e})", file=sys.stderr)
    netpanel = None
    if send:
        try:
            netpanel = NetworkPanel(view.fig.canvas.get_tk_widget().winfo_toplevel(), state, send)
            view.on_network = netpanel.open
        except Exception as e:
            print(f"(network picker unavailable: {e})", file=sys.stderr)
    last_panel = [0.0]

    def tick(_):
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                state.ended = state.lost is None
                break
            if line.startswith('#ERR'):
                print(line[1:], file=sys.stderr)
                state.lost = line[5:].strip() or 'connection lost'
                continue
            state.feed(line)
        for cmd in state.outbox:
            if send:
                send(cmd)
        state.outbox.clear()
        if panel and time.monotonic() - last_panel[0] > 1.0:
            last_panel[0] = time.monotonic()
            panel.refresh()
        if netpanel and time.monotonic() - last_panel[0] < 0.2:
            netpanel.refresh()
        view.draw()
        return []

    _anim = FuncAnimation(view.fig, tick, interval=100, cache_frame_data=False)  # noqa: F841
    import signal
    # Ctrl+C in the terminal closes the window, so the log gets its summary
    signal.signal(signal.SIGINT, lambda *_a: plt.close('all'))
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            log.close()
        if awake:
            from record import release
            release(awake)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
