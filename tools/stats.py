#!/usr/bin/env python3
"""Activity statistics across recorded sessions, for the dashboard's Stats page.

Reads sessions/events-*.log (history.parse) plus the live session's motion
periods, and works out: each session's totals, motion by hour of day, a
day x hour heatmap, how long motion periods last, and today's totals.
Everything uses the board's own verdict (what the events logs record).
"""
import datetime
import gzip
import os
import time

from history import BRIEF_S, brief_split, parse, sessions

DUR_BUCKETS = [(0, 2, '< 2 s'), (2, 5, '2–5 s'), (5, 10, '5–10 s'), (10, 30, '10–30 s'),
               (30, 60, '30–60 s'), (60, 300, '1–5 min'), (300, float('inf'), '≥ 5 min')]
HEAT_DAYS = 21                                  # days shown in the heatmap


class Sessions:
    """events-*.log files, parsed once each and again only when they change."""

    def __init__(self, folder):
        self.folder = folder
        self.cache = {}                         # path -> ((mtime, size), parsed)

    def load(self, exclude=None):
        out = []
        for path in sessions(self.folder):
            if exclude and os.path.basename(path) == exclude:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (st.st_mtime, st.st_size)
            hit = self.cache.get(path)
            if not hit or hit[0] != key:
                try:
                    hit = (key, self._checked(path, parse(path)))
                except (OSError, ValueError):
                    continue
                self.cache[path] = hit
            if hit[1]['start'] is not None:
                out.append(hit[1])
        return out

    @staticmethod
    def _data_end(raw):
        """Unix time of the last clock marker in a raw log: when its data stopped."""
        last = None
        try:
            with gzip.open(raw, 'rt', errors='replace') as f:
                for line in f:
                    if line.startswith('#T,'):
                        last = line
        except (OSError, EOFError):
            pass
        try:
            return float(last.split(',')[1]) if last else None
        except (ValueError, IndexError):
            return None

    def _checked(self, path, s):
        """A log that ends in motion may be one whose data silently stopped (logs
        from before 2026-11-14 couldn't tell): its raw log's last clock marker
        says when, and the session ends there."""
        if not s.get('open_end'):
            return s
        raw = os.path.join(os.path.dirname(path),
                           'raw-' + os.path.basename(path)[len('events-'):-len('.log')] + '.log.gz')
        if not os.path.isfile(raw):
            return s
        stop = self._data_end(raw)
        if stop is None or stop >= s['end'] - 60:
            return s
        stop += 10                               # markers are 10 s apart
        s = dict(s, end=stop, data_stopped=stop,
                 motion=[(a, min(b, stop)) for a, b in s['motion'] if a < stop])
        return s


def _hour_cells(a, b):
    """Split [a, b] (unix s) at local hour boundaries: yields (day, hour, seconds)."""
    t = a
    while t < b:
        d = datetime.datetime.fromtimestamp(t)
        nxt = (d.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)).timestamp()
        end = min(b, nxt)
        yield d.date().isoformat(), d.hour, end - t
        t = end


def summarize(past, live=None, now=None):
    """past: parsed sessions (history.parse); live: the running session as
    dict(start, end, motion=[(a, b)]) or None. Returns the Stats page's data."""
    now = now or time.time()
    all_s = list(past) + ([live] if live else [])
    rows = []
    for s in all_s:
        long_, brief = brief_split(s['motion'])
        rows.append(dict(name=os.path.basename(s.get('path') or 'live session'),
                         live=s is live, start=s['start'], end=s['end'],
                         data_stopped=s.get('data_stopped'),
                         hours=round((s['end'] - s['start']) / 3600, 2),
                         periods=len(long_), blips=len(brief),
                         motion_s=round(sum(b - a for a, b in long_))))
    rows.sort(key=lambda r: r['start'], reverse=True)

    # Recorded time and motion per (day, hour); sessions can overlap (a GUI
    # and a recording at once): count each second of recording once
    covered, motion = {}, {}
    spans = sorted((s['start'], s['end']) for s in all_s if s['end'] > s['start'])
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in merged:
        for day, h, sec in _hour_cells(a, b):
            covered[(day, h)] = covered.get((day, h), 0) + sec
    periods = sorted({(a, b) for s in all_s for a, b in s['motion'] if b - a >= BRIEF_S})
    last_end = None
    for a, b in periods:                         # overlapping sessions log the same motion twice
        if last_end is not None and a < last_end:
            a = last_end
        if b <= a:
            continue
        last_end = b
        for day, h, sec in _hour_cells(a, b):
            motion[(day, h)] = motion.get((day, h), 0) + sec

    by_hour = []
    for h in range(24):
        rec = sum(v for (d, hh), v in covered.items() if hh == h)
        mot = sum(v for (d, hh), v in motion.items() if hh == h)
        by_hour.append(dict(hour=h, recorded_h=round(rec / 3600, 2), motion_s=round(mot),
                            per_hour_s=round(mot / (rec / 3600), 1) if rec >= 60 else None))

    today = datetime.date.fromtimestamp(now)
    days = sorted({d for d, _h in covered})
    days = [d for d in days if (today - datetime.date.fromisoformat(d)).days < HEAT_DAYS]
    heat = dict(days=days,
                motion_s=[[round(motion.get((d, h), 0)) for h in range(24)] for d in days],
                recorded=[[round(covered.get((d, h), 0)) for h in range(24)] for d in days])

    durs = [b - a for a, b in {p for s in all_s for p in s['motion']}]   # overlapping logs: once
    hist = [dict(label=lab, n=sum(lo <= x < hi for x in durs)) for lo, hi, lab in DUR_BUCKETS]

    midnight = datetime.datetime.combine(today, datetime.time()).timestamp()
    t_long = [(a, b) for a, b in periods if a >= midnight]
    t_brief = {a for s in all_s for a, b in s['motion'] if b - a < BRIEF_S and a >= midnight}
    return dict(sessions=rows[:60], by_hour=by_hour, heatmap=heat, durations=hist,
                today=dict(periods=len(t_long), blips=len(t_brief),
                           motion_s=round(sum(b - a for a, b in t_long))),
                n_sessions=len(rows))
