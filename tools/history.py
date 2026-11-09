#!/usr/bin/env python3
"""Look back at a recorded session (from record.py or the GUI's background log).

    python history.py                 # newest session in sessions/
    python history.py 2026-11-08      # a session whose file name contains this
    python history.py --list          # all sessions with their motion counts
    python history.py FILE --png out.png   # save the picture instead of a window

Shows the session on a clock: motion periods (red), brief blips (orange
ticks), calibrating (grey) and times the board was unplugged (dark), plus how
much motion there was per 15 minutes. Zoom and pan with the toolbar.
"""
import argparse
import glob
import os
import re
import sys
import time

LINE = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.*)')
BRIEF_S = 2.0
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sessions')


def parse(path):
    """Session dict from an events log: start, end, motion [(a, b)], cal [(a, b)], gaps."""
    s = dict(path=path, start=None, end=None, motion=[], cal=[], gaps=[], notes=[])
    on = cal = gap = None
    last = None
    with open(path, errors='replace') as f:
        for raw in f:
            m = LINE.match(raw.rstrip('\n'))
            if not m:
                continue
            t = time.mktime(time.strptime(m[1], '%Y-%m-%d %H:%M:%S'))
            text = m[2]
            last = t
            if s['start'] is None:
                s['start'] = t
            if text.startswith('MOTION started'):
                on = t
            elif text.startswith('still again') and on is not None:
                # durations are logged rounded; the text has the exact length
                s['motion'].append((on, max(t, on)))
                on = None
            elif text.startswith('Recalibrating'):
                cal = t
                s['notes'].append((t, text))
            elif text.startswith('Calibrated'):
                s['cal'].append((cal if cal is not None else s['start'], t))
                cal = None
            elif text.startswith('Board not available'):
                gap = t
                s['notes'].append((t, text))
            elif text.startswith('Board reconnected') and gap is not None:
                s['gaps'].append((gap, t))
                gap = None
            elif text.startswith('Recording stopped'):
                break
    end = last if last is not None else s['start']
    if on is not None:
        s['motion'].append((on, end))
    if gap is not None:
        s['gaps'].append((gap, end))
    s['end'] = end
    return s


def brief_split(motion):
    """Motion periods split into (sustained, brief). Logged times have 1 s
    resolution, so 'brief' means under BRIEF_S by the clock."""
    return ([p for p in motion if p[1] - p[0] >= BRIEF_S],
            [p for p in motion if p[1] - p[0] < BRIEF_S])


def fmt_dur(s):
    s = int(round(s))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s" if s < 3600 else \
        f"{s // 3600} h {s % 3600 // 60:02d} min"


def sessions(folder=OUT):
    return sorted(glob.glob(os.path.join(folder, 'events-*.log')))


def pick(arg, folder=OUT):
    if arg and os.path.isfile(arg):
        return arg
    found = [p for p in sessions(folder) if not arg or arg in os.path.basename(p)]
    if not found:
        sys.exit(f"No session found in {folder}" + (f" matching '{arg}'" if arg else ''))
    return found[-1]


def text_summary(s):
    clock = lambda t: time.strftime('%H:%M:%S', time.localtime(t))
    long_, brief = brief_split(s['motion'])
    out = [f"{os.path.basename(s['path'])}: {time.strftime('%a %d %b %H:%M', time.localtime(s['start']))}"
           f" to {clock(s['end'])} ({fmt_dur(s['end'] - s['start'])})",
           f"  {len(long_)} motion period(s), {fmt_dur(sum(b - a for a, b in long_))} in total;"
           f" {len(brief)} brief blip(s)"]
    for a, b in long_:
        out.append(f"    {clock(a)}  for {fmt_dur(b - a)}")
    for t, note in s['notes']:
        out.append(f"  {clock(t)}  {note}")
    return out


def draw(s, plt):
    import matplotlib.dates as mdates
    import datetime
    BG, PANEL, FG, DIM = '#101418', '#171c22', '#e6e9ed', '#7d8794'
    MOTION_C, BRIEF_C, CAL_C = '#ff5a4f', '#ffb547', '#7d8794'
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9, 'text.color': FG,
                         'axes.labelcolor': DIM, 'xtick.color': DIM, 'ytick.color': DIM})
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(12, 5.5), facecolor=BG, sharex=True,
                                  gridspec_kw=dict(height_ratios=[1.3, 1], hspace=0.25))
    try:
        fig.canvas.manager.set_window_title('CSI motion history')
    except AttributeError:
        pass
    day = 86400.0
    tz = datetime.datetime.now().astimezone().tzinfo
    long_, brief = brief_split(s['motion'])
    for a_ in (ax, ax2):
        a_.set_facecolor(PANEL)
        a_.grid(color='#2a313a', lw=0.6)
        for sp in a_.spines.values():
            sp.set_color('#2a313a')
    for a, b in s['cal']:
        ax.axvspan(a / day, b / day, color=CAL_C, alpha=0.25, lw=0)
    for a, b in s['gaps']:
        ax.axvspan(a / day, b / day, color='#000000', alpha=0.6, lw=0)
    if long_:                                     # true length, plus a line so short ones show
        ax.broken_barh([(a / day, (b - a) / day) for a, b in long_], (0.3, 0.7),
                       color=MOTION_C, lw=0)
        ax.vlines([a / day for a, _b in long_], 0.3, 1.0, color=MOTION_C, lw=1.2)
    if brief:
        ax.vlines([a / day for a, _b in brief], 0, 0.25, color=BRIEF_C, lw=1)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.12, 0.65], ['brief', 'motion'])
    clock = lambda t: time.strftime('%H:%M', time.localtime(t))
    ax.set_title(f"{time.strftime('%a %d %b', time.localtime(s['start']))}  {clock(s['start'])}"
                 f"-{clock(s['end'])}:  {len(long_)} motion period(s), "
                 f"{fmt_dur(sum(b - a for a, b in long_))} in total, {len(brief)} brief blip(s)",
                 color=FG, fontsize=10, loc='left')

    # motion seconds per 15 minutes, and brief blips per 15 minutes
    bin_s = 900
    b0 = (s['start'] // bin_s) * bin_s
    n = int((s['end'] - b0) // bin_s) + 1
    secs = [0.0] * n
    blips = [0] * n
    for a, b in long_:
        t = a
        while t < b:
            i = int((t - b0) // bin_s)
            nxt = min(b, b0 + (i + 1) * bin_s)
            if 0 <= i < n:
                secs[i] += nxt - t
            t = nxt
    for a, _b in brief:
        i = int((a - b0) // bin_s)
        if 0 <= i < n:
            blips[i] += 1
    x = [(b0 + i * bin_s) / day for i in range(n)]
    ax2.bar(x, secs, width=bin_s / day * 0.9, align='edge', color=MOTION_C, alpha=0.8,
            label='motion (s per 15 min)')
    ax2.plot([v + bin_s / 2 / day for v in x], blips, 'o', color=BRIEF_C, ms=3.5,
             label='brief blips per 15 min')
    ax2.set_ylabel('per 15 min')
    ax2.set_ylim(0, max(max(secs), max(blips), 5) * 1.15)
    ax2.legend(loc='upper left', facecolor=PANEL, edgecolor='#2a313a', labelcolor=FG,
               fontsize=8)
    ax2.set_xlim(s['start'] / day, max(s['end'], s['start'] + 60) / day)
    loc = mdates.AutoDateLocator(tz=tz, minticks=5, maxticks=14)
    ax2.xaxis.set_major_locator(loc)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=tz))
    fig.subplots_adjust(left=0.07, right=0.98, top=0.92, bottom=0.08)
    return fig


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('session', nargs='?', help='file, or part of its name (default: newest)')
    ap.add_argument('--list', action='store_true', help='list all sessions')
    ap.add_argument('--png', help='save the picture to this file instead of opening a window')
    ap.add_argument('--dir', default=OUT, help='session folder')
    a = ap.parse_args()

    if a.list:
        found = sessions(a.dir)
        if not found:
            print(f"No sessions in {a.dir} yet.")
        for p in found:
            s = parse(p)
            if s['start'] is None:
                continue
            long_, brief = brief_split(s['motion'])
            print(f"{os.path.basename(p):32s} {time.strftime('%a %d %b %H:%M', time.localtime(s['start']))}"
                  f"  {fmt_dur(s['end'] - s['start']):>14s}  {len(long_):3d} motion, {len(brief):3d} brief")
        return

    s = parse(pick(a.session, a.dir))
    if s['start'] is None:
        sys.exit(f"{s['path']} has no events yet.")
    print('\n'.join(text_summary(s)))
    import matplotlib
    if a.png:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig = draw(s, plt)
    if a.png:
        fig.savefig(a.png, dpi=120, facecolor=fig.get_facecolor())
        print(f"saved {a.png}")
    else:
        plt.show()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
