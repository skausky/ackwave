#!/usr/bin/env python3
"""Tests history.py on a small events log (as record.py / the GUI write it)."""
import os
import tempfile

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

from history import brief_split, draw, parse, pick, text_summary  # noqa: E402

LOG = """2026-11-09 23:00:00  Recording started
2026-11-09 23:00:12  Calibrated (quiet level 0.0321), watching for motion
2026-11-09 23:10:00  MOTION started (short x4.0, long x1.8)
2026-11-09 23:10:17  still again after 17 s
2026-11-09 23:20:00  MOTION started (short x2.9, long x1.0)
2026-11-09 23:20:01  still again after 1 s (brief)
2026-11-09 23:30:00  Board not available, waiting for it...
2026-11-09 23:31:00  Board reconnected
2026-11-09 23:40:00  Recalibrating (button pressed), keep still
2026-11-09 23:40:15  Calibrated (quiet level 0.0330), watching for motion
2026-11-09 23:50:00  MOTION started (short x5.0, long x2.0)
2026-11-09 23:55:00  Recording stopped

Summary 2026-11-09 23:00:00 to 2026-11-09 23:55:00 (55 min 00 s)
  1 motion period(s), 17 s in total:
"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'events-2026-11-09_2300.log')
        open(p, 'w').write(LOG)
        open(os.path.join(tmp, 'events-2026-11-01_2200.log'), 'w').write(LOG)
        assert pick(None, tmp) == p and pick('11-01', tmp).endswith('2200.log')
        s = parse(p)
        long_, brief = brief_split(s['motion'])
        assert [b - a for a, b in long_] == [17, 300], long_   # last one open until stop
        assert len(brief) == 1 and len(s['gaps']) == 1 and s['gaps'][0][1] - s['gaps'][0][0] == 60
        assert len(s['cal']) == 2 and s['cal'][1][1] - s['cal'][1][0] == 15
        assert s['end'] - s['start'] == 55 * 60          # summary text after stop is ignored
        txt = '\n'.join(text_summary(s))
        print(txt)
        assert "2 motion period(s)" in txt and "1 brief blip(s)" in txt and "Board not available" in txt
        draw(s, plt).savefig(os.path.join(tmp, 'h.png'))
    print("HISTORY TESTS PASSED")


if __name__ == '__main__':
    main()
