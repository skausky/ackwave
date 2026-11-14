#!/usr/bin/env python3
"""Tests stats.py: session totals, hour-of-day and day x hour activity, the
duration histogram, today's totals, overlapping sessions counted once, and
old logs whose board data silently stopped."""
import datetime
import gzip
import os
import tempfile
import time

from stats import Sessions, summarize


def ts(s):
    return time.mktime(time.strptime(s, '%Y-%m-%d %H:%M:%S'))


LOG_A = """2026-11-10 22:00:00  Recording started
2026-11-10 22:00:05  Calibrated (quiet level 0.0250), watching for motion
2026-11-10 22:10:00  MOTION started (short x2.0, long x1.4)
2026-11-10 22:10:30  still again after 30 s
2026-11-10 22:59:50  MOTION started (short x2.0, long x1.4)
2026-11-10 23:00:10  still again after 20 s
2026-11-10 23:30:00  MOTION started (short x2.9, long x1.2)
2026-11-10 23:30:01  still again after 1 s (brief)
2026-11-11 01:00:00  Recording stopped
"""
# A second session overlapping the first (GUI and recording at once): same motion
LOG_B = """2026-11-10 22:05:00  Recording started
2026-11-10 22:10:00  MOTION started (short x2.0, long x1.4)
2026-11-10 22:10:30  still again after 30 s
2026-11-10 22:30:00  Recording stopped
"""
# An old log whose board data stopped at 09:20 though the log runs to the next day
LOG_STALL = """2026-11-12 09:18:13  Recording started
2026-11-12 09:19:56  MOTION started (short x1.9, long x1.5)
2026-11-13 07:45:55  Recording stopped
"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (('events-2026-11-10_2200.log', LOG_A), ('events-2026-11-10_2205.log', LOG_B),
                           ('events-2026-11-12_0918.log', LOG_STALL)):
            open(os.path.join(tmp, name), 'w').write(text)
        with gzip.open(os.path.join(tmp, 'raw-2026-11-12_0918.log.gz'), 'wt') as f:
            f.write(f"#T,{ts('2026-11-12 09:18:13'):.3f},\nSTATS,1,1,0,1,0,0,0\n"
                    f"#T,{ts('2026-11-12 09:20:13'):.3f},120000000\nSTATS,2,1,0,1,0,0,0\n")
        folder = Sessions(tmp)
        past = folder.load()
        assert len(past) == 3
        stall = [s for s in past if s['path'].endswith('0918.log')][0]
        assert stall['data_stopped'] == ts('2026-11-12 09:20:23'), "the raw log's last marker + 10 s"
        assert stall['end'] == stall['data_stopped'] and stall['motion'][-1][1] == stall['data_stopped']

        live = dict(start=ts('2026-11-14 10:00:00'), end=ts('2026-11-14 10:30:00'), path='live',
                    motion=[(ts('2026-11-14 10:05:00'), ts('2026-11-14 10:06:00')),
                            (ts('2026-11-14 10:20:00'), ts('2026-11-14 10:20:01'))])
        st = summarize(past, live, now=ts('2026-11-14 10:30:00'))

        rows = {r['name']: r for r in st['sessions']}
        a = rows['events-2026-11-10_2200.log']
        assert (a['periods'], a['blips'], a['motion_s'], a['hours']) == (2, 1, 50, 3.0), a
        assert st['sessions'][0]['live'], "newest first"
        assert rows['events-2026-11-12_0918.log']['data_stopped']

        # The 22:10 motion is in both overlapping logs: counted once. 22:59:50-23:00:10
        # splits over two hours
        h = {r['hour']: r for r in st['by_hour']}
        assert h[22]['motion_s'] == 30 + 10 and h[23]['motion_s'] == 10, (h[22], h[23])
        assert h[22]['recorded_h'] == 1.0, "overlapping sessions: each second recorded once"
        assert h[2]['per_hour_s'] is None, "never recorded at 02:00"
        assert h[10]['motion_s'] == 60 and h[10]['per_hour_s'] == 120.0, h[10]

        hm = st['heatmap']
        assert hm['days'][-1] == '2026-11-14' and '2026-11-10' in hm['days']
        d = hm['days'].index('2026-11-10')
        assert hm['motion_s'][d][22] == 40 and hm['recorded'][d][22] == 3600 and hm['recorded'][d][3] == 0
        # older than HEAT_DAYS: left out
        old = summarize(past, live, now=ts('2026-12-30 12:00:00'))
        assert old['heatmap']['days'] == []

        durs = {b['label']: b['n'] for b in st['durations']}
        # 30 s (in both overlapping logs: once), 20 s, 1 s, 27 s (stalled log), 60 s, 1 s
        assert durs['< 2 s'] == 2 and durs['10–30 s'] == 2 and durs['30–60 s'] == 1 \
            and durs['1–5 min'] == 1 and sum(durs.values()) == 6, durs
        assert st['today'] == dict(periods=1, blips=1, motion_s=60), st['today']

        # A changed file is parsed again; an unchanged one comes from the cache
        p = os.path.join(tmp, 'events-2026-11-10_2205.log')
        first = folder.cache[p][1]
        assert folder.load() and folder.cache[p][1] is first
        open(p, 'a').write("2026-11-10 22:31:00  Recording started\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        folder.load()
        assert folder.cache[p][1] is not first
        assert summarize([], None)['sessions'] == [] and datetime.date.today()
    print("STATS TESTS PASSED")


if __name__ == '__main__':
    main()
