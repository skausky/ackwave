#!/usr/bin/env python3
"""Live GUI for the CSI motion sensor.

    python gui.py /dev/ttyUSB0                  # real board (COM5 on Windows)
    python fake_csi.py --realtime --loop | python gui.py -     # demo, no hardware
    python fake_csi.py | python gui.py - --snapshot shot.png   # render one image

With a board it also logs in the background, exactly like record.py (events
and raw data in sessions/, summary when the window closes); --no-log turns
that off. Buttons: 30 s / 2 min / 10 min of detail, and Pause (freezes the
view so the toolbar can zoom and pan; logging continues). Keys: space = pause,
1/2/3 = window. The session timeline at the bottom covers everything since the
GUI started, on a clock.

Shows: short-window motion score per stream (node x beacon/ACK) and the
long-window score, both as multiples of the room's quiet level, with the two
thresholds; the PC's verdict (shaded), the board's own verdict (STATE lines),
calibration periods, a subcarrier waterfall for the busiest stream, and live
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

from analyze import KIND_NAMES, VALID, MotionDetector, open_source, parse_line

BG, PANEL, FG, DIM = '#101418', '#171c22', '#e6e9ed', '#7d8794'
MOTION_C, STILL_C, LONG_C, CAL_C = '#ff5a4f', '#3ecf8e', '#f2f4f7', '#7d8794'
BRIEF_C = '#ffb547'
WINDOWS = (30.0, 120.0, 600.0)                  # selectable detail windows (s)
BRIEF_S = 2.0                                   # as record.py: shorter = brief blip
COLORS = ['#4ea8ff', '#ffb547', '#b48cff', '#5fd4d0', '#ff7eb6', '#c3e88d', '#f78c6c', '#89ddff']


class State:
    """Everything the plots need; fed one parsed event at a time."""

    def __init__(self, window_s, waterfall_s=10.0, clock=time.time):
        self.window_s, self.waterfall_s = window_s, waterfall_s   # window_s: detail kept
        self.clock = clock
        # Whole-session history on the wall clock (survives board reboots):
        # one bin per second with the highest short / long score
        self.hist_t, self.hist_s, self.hist_l = [], [], []
        self.hist_motion, self.hist_cal = [], []     # [wall0, wall1 or None]
        self.wall_off, self.last_wall = None, None
        self.reset()

    def wall(self, t=None):
        """Wall-clock time (unix s) of board time t (default: now)."""
        return self.wall_off + (self.now if t is None else t)

    def reset(self):
        self.det = MotionDetector()
        self.scores = defaultdict(deque)          # (src, kind) -> deque[(t, short / floor)]
        self.long = deque()                       # [(t, long / floor)]
        self.cal_spans = []                       # [t0, t1 or None] without a floor
        self.board_calib = None
        self.arrivals = defaultdict(deque)        # (src, kind) -> deque[t] (rates)
        self.rssi = {}
        self.wf = defaultdict(deque)              # (src, kind) -> deque[(t, vec)]
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
        if line.startswith('ME,'):
            self.me = line.strip()[3:]
            return
        ev = parse_line(line)
        if ev is None:
            return
        kind, d = ev
        if kind == 'learned':
            self.nodes[d['idx']] = d['mac']
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
            self.last_wall = self.wall()
        if kind == 'channel':
            self.channel = (d['ch'], d['why'])
            self.det.on_channel(d['ts'])
            self.hops.append(self.now)
        elif kind == 'calib':                     # the board's quiet level wins
            self.board_calib = d
            c = self.det.c
            if d['floor'] > 0:
                c['on_short'], c['on_long'] = d['on_short'] / d['floor'], d['on_long'] / d['floor']
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
        elif change < 0 and self.motion_spans:
            self.motion_spans[-1][1] = t
            if self.hist_motion and self.hist_motion[-1][1] is None:
                self.hist_motion[-1][1] = w

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
        self.cal_spans = [s for s in self.cal_spans if s[1] is None or s[1] >= t0]

    def rate(self, key):
        dq = self.arrivals[key]
        return len(dq) / 2.0

    def busiest(self):
        live = [k for k, dq in self.wf.items() if dq]
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
    def __init__(self, state, plt, log_name=None, window=WINDOWS[0]):
        self.s, self.plt = state, plt
        self.view_s = window
        self.paused = False
        self.log_name = log_name
        plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9,
                             'text.color': FG, 'axes.labelcolor': DIM,
                             'xtick.color': DIM, 'ytick.color': DIM})
        self.fig = plt.figure(figsize=(11, 8.4), facecolor=BG)
        try:
            self.fig.canvas.manager.set_window_title('CSI motion sensor')
        except AttributeError:
            pass
        gs = self.fig.add_gridspec(4, 1, height_ratios=[0.45, 2.0, 1.0, 0.9],
                                   left=0.07, right=0.97, top=0.97, bottom=0.06, hspace=0.42)
        self.ax_head = self.fig.add_subplot(gs[0])
        self.ax_score = self.fig.add_subplot(gs[1])
        self.ax_tl = self.fig.add_subplot(gs[2])
        self.ax_wf = self.fig.add_subplot(gs[3])
        for ax in (self.ax_score, self.ax_tl, self.ax_wf):
            ax.set_facecolor(PANEL)
            for sp in ax.spines.values():
                sp.set_color('#2a313a')
        self.ax_head.axis('off')
        self.badge = self.ax_head.text(0.0, 0.5, '', fontsize=22, fontweight='bold',
                                       va='center', ha='left', transform=self.ax_head.transAxes)
        self.info = self.ax_head.text(0.2, 0.5, '', fontsize=9.5, va='center', ha='left',
                                      color=FG, family='DejaVu Sans Mono',
                                      transform=self.ax_head.transAxes)
        self.ax_score.set_ylabel('motion score')
        self.ax_wf.set_ylabel('subcarrier')
        self.ax_wf.set_xlabel('time (s)')
        self.wf_img = None
        self._buttons()
        self.fig.canvas.mpl_connect('key_press_event', self._key)

    def _buttons(self):
        from matplotlib.widgets import Button
        self.btns = {}
        labels = [('30 s', WINDOWS[0]), ('2 min', WINDOWS[1]), ('10 min', WINDOWS[2])]
        for i, (lab, w) in enumerate(labels):
            ax = self.fig.add_axes([0.70 + i * 0.062, 0.935, 0.056, 0.035])
            b = Button(ax, lab, color=PANEL, hovercolor='#2a313a')
            b.label.set_color(FG)
            b.on_clicked(lambda _e, w=w: self.set_window(w))
            self.btns[w] = b
        ax = self.fig.add_axes([0.70 + 3 * 0.062, 0.935, 0.07, 0.035])
        self.btn_pause = Button(ax, 'Pause', color=PANEL, hovercolor='#2a313a')
        self.btn_pause.label.set_color(FG)
        self.btn_pause.on_clicked(lambda _e: self.toggle_pause())
        self._mark_buttons()

    def _mark_buttons(self):
        for w, b in self.btns.items():
            b.label.set_fontweight('bold' if w == self.view_s else 'normal')
            b.ax.set_facecolor('#2f3a46' if w == self.view_s else PANEL)
            b.color = '#2f3a46' if w == self.view_s else PANEL
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

    def _key(self, e):
        if e.key == ' ':
            self.toggle_pause()
        elif e.key in ('1', '2', '3'):
            self.set_window(WINDOWS[int(e.key) - 1])

    def draw(self):
        s, ax = self.s, self.ax_score
        motion, det = s.det.motion, s.det
        if det.floor <= 0:
            self.badge.set_text('CALIBRATING')
            self.badge.set_color(CAL_C)
            self.badge.set_fontsize(15)
        else:
            self.badge.set_text('MOTION' if motion else 'STILL')
            self.badge.set_color(MOTION_C if motion else STILL_C)
            self.badge.set_fontsize(22)

        ch = f"ch {s.channel[0]} ({s.channel[1] or 'start'})" if s.channel else 'searching...'
        board = {None: 'n/a', True: 'MOTION', False: 'still'}[s.board_motion]
        src = 'board' if det.external else 'PC'
        quiet = (f"quiet level {det.floor:.4f} ({det.phase}, {det.why}, from {src})"
                 if det.floor > 0 else f"measuring quiet level ({det.phase}, {det.why}) - keep still")
        rows = [f"{ch:18s} nodes {len(s.nodes)}   board says: {board}", quiet]
        streams = sorted(set(s.arrivals) | set(s.scores))
        rows.append('   '.join(
            f"n{src}/{KIND_NAMES[k]} {s.rate((src, k)):5.1f}/s {s.rssi.get((src, k), 0):4d}dBm"
            for src, k in streams[:4]) or 'waiting for CSI...')
        if s.stats:
            st = s.stats
            ratio = st['ack'] / st['poke'] if st['poke'] else 0
            rows.append(f"pokes {st['poke']}  ACK CSI {st['ack']} ({ratio:.0%})  "
                        f"tx err {st['tx_err']}  queue drops {st['qfull']}")
        self.info.set_text('\n'.join(rows))

        if self.paused:
            self.info.set_text('\n'.join(rows) + '   [PAUSED: zoom/pan with the toolbar, logging continues]')
            return
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
            ax.plot(t, v, color=COLORS[(key[0] * 2 + key[1]) % len(COLORS)], lw=1.1,
                    ls='-' if key[1] else '--', alpha=0.85,
                    label=f"short: node {key[0]} {'ACK' if key[1] else 'beacon/AP'}")
        if s.long:
            t, v = np.array(s.long).T
            keep = t >= t0
            t, v = thin(t[keep], v[keep]) if keep.any() else (t[-1:], v[-1:])
            ymax = max(ymax, v.max() * 1.15)
            gaps = np.where(np.diff(t) > max(0.5, win / 600))[0] + 1
            t, v = np.insert(t, gaps, np.nan), np.insert(v, gaps, np.nan)
            ax.plot(t, v, color=LONG_C, lw=2.2, label='long (4 s)')
        for h in s.hops:
            if h >= t0:
                ax.axvline(h, color=FG, lw=0.8, alpha=0.5)
                ax.text(h, ymax * 0.02, ' channel hop', color=FG, fontsize=8, va='bottom')
        ax.axhline(1.0, color=DIM, lw=0.8, ls=':', alpha=0.8)
        ax.axhline(on_s, color=MOTION_C, lw=0.9, ls='--', alpha=0.8)
        ax.axhline(on_l, color=LONG_C, lw=0.9, ls='--', alpha=0.8)
        lab = t0 + win * 0.007
        ax.text(lab, 1.0, ' quiet level', color=DIM, va='bottom', fontsize=8)
        ax.text(lab, on_s, f' short threshold x{on_s:.1f}', color=MOTION_C,
                va='bottom', fontsize=8)
        ax.text(lab, on_l, f' long threshold x{on_l:.1f}', color=LONG_C,
                va='bottom', fontsize=8)
        if det.floor <= 0:
            ax.text((t0 + t1) / 2, ymax * 0.5, 'calibrating: measuring the quiet level',
                    color=FG, ha='center', fontsize=11, alpha=0.8)
        ax.set_xlim(t0, t1)
        ax.set_ylim(0, ymax)
        self._clock_axis(ax)
        ax.set_ylabel('motion score (x quiet level)')
        ax.grid(color='#2a313a', lw=0.6)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc='upper right', facecolor=PANEL, edgecolor='#2a313a',
                      labelcolor=FG, fontsize=8, ncol=4)

        self._timeline()
        key = s.busiest()
        self.ax_wf.cla()
        self.ax_wf.set_facecolor(PANEL)
        self.ax_wf.set_ylabel('subcarrier')
        self.ax_wf.set_xlabel('')
        if key:
            ts, vecs = zip(*s.wf[key])
            m = np.array(vecs).T
            self.ax_wf.imshow(m, aspect='auto', origin='lower', cmap='magma', vmin=0, vmax=2,
                              extent=(ts[0], ts[-1] if len(ts) > 1 else ts[0] + 0.1, 0, len(VALID)),
                              interpolation='nearest')
            self._clock_axis(self.ax_wf)
            self.ax_wf.set_title(f"normalised amplitude, node {key[0]} "
                                 f"{'ACK' if key[1] else 'beacon/AP'}",
                                 color=DIM, fontsize=8.5, loc='left')
        self.fig.canvas.draw_idle()


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
        ax.grid(color='#2a313a', lw=0.6)
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
        title = (f"session since {since}: {n_long} motion period(s), {n_brief} brief blip(s)"
                 "   (red = motion, orange ticks = brief, grey = calibrating)")
        if self.log_name:
            title += f"   logging to {self.log_name}"
        ax.set_title(title, color=DIM, fontsize=8.5, loc='left')
        ax.set_ylabel('peak / s')


def reader(source, baud, q, log=None):
    try:
        for line in open_source(source, baud):
            if log:
                log.feed(line)                   # in this thread: never waits for drawing
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
    q = queue.Queue()
    threading.Thread(target=reader, args=(a.source, a.baud, q, log), daemon=True).start()
    view = View(state, plt, os.path.basename(log.ev_path) if log else None, a.window)

    def tick(_):
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            if line.startswith('#ERR'):
                print(line[1:], file=sys.stderr)
                continue
            state.feed(line)
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
