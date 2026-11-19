"""Where and when motion happened, kept on disk (sessions/timeline-YYYY-MM-DD.jsonl).

One line per finished motion period of one link: the board's own verdict
('board'), a router link ('ap:<n>') or a poked device's link (its MAC):

    {"t0": 1795100000.1, "t1": 1795100012.6, "key": "board", "name": "Board"}

Times are wall-clock seconds; a period is filed under the local day it
started. The dashboard writes a line when a period ends, so a crash loses at
most the periods still open. Reading a range returns the periods that
overlap it, oldest first; periods still open come from the caller.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

FILE_RE = re.compile(r'timeline-(\d{4}-\d{2}-\d{2})\.jsonl$')
KEY_RE = re.compile(r'board|ap:[0-7]|[0-9a-f]{2}(:[0-9a-f]{2}){5}')
MAX_SPAN_S = 24 * 3600            # a period longer than this is cut (a stuck verdict)
MAX_RANGE_S = 7 * 24 * 3600       # the longest range read at once
NAME_MAX = 40


def _day(t):
    return datetime.fromtimestamp(t).strftime('%Y-%m-%d')


class Timeline:
    def __init__(self, folder):
        self.folder = folder
        self._cache = {}                  # past day -> (mtime, periods)

    def path(self, day):
        return os.path.join(self.folder, f'timeline-{day}.jsonl')

    def add(self, t0, t1, key, name):
        """File one finished period. Returns False if it is not worth keeping."""
        try:
            t0, t1 = float(t0), float(t1)
        except (TypeError, ValueError):
            return False
        if not (t1 >= t0 > 0) or not isinstance(key, str) or not KEY_RE.fullmatch(key):
            return False
        t1 = min(t1, t0 + MAX_SPAN_S)
        rec = dict(t0=round(t0, 2), t1=round(t1, 2), key=key, name=str(name)[:NAME_MAX])
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(self.path(_day(t0)), 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec) + '\n')
        except OSError:
            return False
        return True

    def _read_day(self, day):
        p = self.path(day)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return []
        hit = self._cache.get(day)
        if hit and hit[0] == mtime:
            return hit[1]
        out = []
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        t0, t1, key = float(r['t0']), float(r['t1']), r['key']
                    except (ValueError, KeyError, TypeError):
                        continue                  # a torn last line after a crash
                    if t1 >= t0 and isinstance(key, str) and KEY_RE.fullmatch(key):
                        out.append((t0, t1, key, str(r.get('name', key))[:NAME_MAX]))
        except OSError:
            return []
        out.sort()
        if day != _day(time.time()):              # today's file still grows: re-read it
            self._cache[day] = (mtime, out)
        return out

    def read(self, t_from, t_to):
        """Periods overlapping [t_from, t_to], oldest first: (t0, t1, key, name)."""
        t_from = max(t_from, t_to - MAX_RANGE_S)
        out = []
        d = datetime.fromtimestamp(t_from).date() - timedelta(days=1)   # one may start the day before
        end = datetime.fromtimestamp(t_to).date()
        while d <= end:
            out += [p for p in self._read_day(d.strftime('%Y-%m-%d')) if p[1] >= t_from and p[0] <= t_to]
            d += timedelta(days=1)
        out.sort()
        return out

    def days(self):
        """The days that have a timeline file, newest first."""
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        return sorted((m.group(1) for m in map(FILE_RE.search, names) if m), reverse=True)
