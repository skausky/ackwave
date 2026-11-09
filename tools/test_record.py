#!/usr/bin/env python3
"""Tests record.py's event log and summary with a fake clock."""
import gzip
import os
import tempfile

from record import Recorder, SessionLog, duration


def main():
    t = [1_800_000_000.0]
    out = []
    rec = Recorder(out.append, now=lambda: t[0])
    feed = [
        (0, "CALIB,1,collect,boot,0,0,0"),
        (13, "CALIB,2,fast,boot,33000,92400,49500"),
        (100, "CSI,3,0,1,6,-60,0,0,128," + " ".join(["1"] * 128)),
        (3600, "STATE,4,motion,132,61"),
        (3600 + 17, "STATE,5,still,40,40"),
        (5000, "BUTTON,recalibrate"),
        (5005, "CALIB,6,collect,button,0,0,0"),
        (5020, "CALIB,7,fast,button,34000,95200,51000"),
        (6000, "STATE,9,motion,95,35"),
        (6001, "STATE,10,still,40,35"),
        (7200, "STATE,8,motion,300,90"),
    ]
    for at, line in feed:
        t[0] = 1_800_000_000.0 + at
        rec.feed(line)
    t[0] += 125
    text = '\n'.join(out + rec.summary())
    print(text)
    assert "Calibrated (quiet level 0.0330)" in text
    assert "MOTION started (short x4.0, long x1.8)" in text
    assert "still again after 17 s" in text
    assert "BOOT button pressed" in text and "Recalibrating (button pressed)" in text
    assert text.count("MOTION started") == 3
    assert "still again after 1 s (brief)" in text
    assert "1 brief blip(s) under 2 s" in text
    assert "2 motion period(s)" in text and "for 2 min 05 s" in text   # still ongoing at stop
    assert duration(59) == "59 s" and duration(3725) == "1 h 02 min"

    empty = Recorder(out.append, now=lambda: 0.0)
    assert any("No motion detected" in s for s in empty.summary())
    blip = Recorder(out.append, now=lambda: t[0])
    blip.feed("STATE,1,motion,95,35")
    t[0] += 0.5
    blip.feed("STATE,2,still,40,35")
    assert any("No sustained motion" in s for s in blip.summary())

    # SessionLog: both files, clock markers in the raw data, safe double close
    with tempfile.TemporaryDirectory() as tmp:
        clk = [1_800_000_000.0]
        log = SessionLog(tmp, echo=False, now=lambda: clk[0])
        for i in range(30):                       # 30 s of lines, 1 per second
            log.feed(f"STATS,{(i + 1) * 1_000_000},1,0,1,0,0,0\n")
            clk[0] += 1
        log.feed("STATE,31000000,motion,132,61")
        clk[0] += 5
        log.feed("STATE,36000000,still,40,40")
        log.close()
        log.close()
        log.feed("STATS,1,1,0,1,0,0,0")            # after close: ignored
        names = sorted(os.listdir(tmp))
        assert [n.split('-')[0] for n in names] == ['events', 'raw'], names
        raw = gzip.open(os.path.join(tmp, names[1]), 'rt').read().splitlines()
        marks = [r for r in raw if r.startswith('#T,')]
        assert len(marks) == 4 and marks[1] == "#T,1800000010.000,10000000", marks
        assert len(raw) == 32 + 4 and raw[-1] == "STATE,36000000,still,40,40"
        ev = open(os.path.join(tmp, names[0])).read()
        assert "MOTION started" in ev and "1 motion period(s)" in ev
        assert ev.count("Recording stopped") == 1
    print("RECORD TESTS PASSED")


if __name__ == '__main__':
    main()
