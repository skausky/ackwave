#!/usr/bin/env python3
"""Tests record.py's event log and summary with a fake clock."""
import gzip
import os
import tempfile

from record import Recorder, SessionLog, clock, duration, replay


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
    restart_and_recalibration()
    calibration_notes()
    network_lines()
    replay_raw_log()
    print("RECORD TESTS PASSED")


def drive(feed, t0=1_800_000_000.0):
    """A Recorder fed (seconds, line) pairs on a fake clock: (log text, recorder)."""
    t, out = [t0], []
    rec = Recorder(out.append, now=lambda: t[0])
    for at, line in feed:
        t[0] = t0 + at
        rec.feed(line)
    return out, rec


def restart_and_recalibration():
    # A board restarted mid-motion starts 'still' and never says so: the open
    # period ends at the restart, and the next real start is not swallowed.
    calib = "CALIB,{},fast,boot,25000,70000,35000"
    out, rec = drive([
        (0, calib.format(14_000_000)),
        (10, "STATE,24000000,motion,99,35"),
        (20, "CSI,34000000,0,1,6,-50,0,0,104,AAAA"),
        (25, "ESP-ROM:esp32s3-20210327"),
        (25, "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)"),
        (26, "CALIB,1062260,wait,boot,0,0,0"),
        (40, calib.format(14_952_456)),
        (500, "STATE,512149708,motion,47,32"),
        (516, "STATE,528269549,still,24,27"),
    ])
    text = '\n'.join(out)
    assert "still again after 15 s (board restarted)" in text, text
    assert text.count("Board restarted") == 1, text           # banner + clock jump: once
    assert rec.periods == [(1_800_000_010.0, 1_800_000_025.0), (1_800_000_500.0, 1_800_000_516.0)]
    assert rec.last_ts == 528269549

    # Without the banner (lines lost): the board clock going back is enough
    out, rec = drive([(0, calib.format(14_000_000)), (10, "STATE,24000000,motion,99,35"),
                      (30, "CALIB,1062260,wait,boot,0,0,0")])
    assert "still again after 20 s (board restarted)" in '\n'.join(out) and rec.motion_since is None
    # A CSI line with a garbled time is no restart
    out, rec = drive([(0, calib.format(14_000_000)), (10, "STATE,24000000,motion,99,35"),
                      (11, "CSI,12,0,1,6,-50,0,0,104,AAAA"), (12, "CSI,x,0,1")])
    assert rec.motion_since is not None and "Board restarted" not in '\n'.join(out)

    # Recalibrating while in motion closes the period, even if no STATE,still comes
    out, rec = drive([(0, calib.format(14_000_000)), (10, "STATE,24000000,motion,99,35"),
                      (13, "BUTTON,recalibrate"), (13, "CALIB,27000000,wait,button,0,0,0"),
                      (14, "STATE,28000000,still,0,0")])
    text = '\n'.join(out)
    assert "still again after 3 s (recalibrating)" in text and text.count("still again") == 1, text
    assert "Recalibrating (button pressed)" in text and len(rec.periods) == 1


def calibration_notes():
    # The settled quiet level is logged; a big drop says it moved while calibrating
    out, _ = drive([(0, "CALIB,30209795,fast,boot,41953,117470,58735"),
                    (25, "CALIB,55224861,fast,boot,24399,68317,34158"),
                    (100, "CALIB,129594346,track,boot,23662,66256,33128"),
                    (105, "CALIB,134598912,track,boot,23631,66168,33084")])
    text = '\n'.join(out)
    assert "Calibrated (quiet level 0.0420)" in text
    assert text.count("Quiet level settled") == 1
    assert "settled at 0.0237 (first 0.0420: something probably moved" in text, text
    out, _ = drive([(0, "CALIB,1,fast,button,21015,58843,29421"),
                    (100, "CALIB,2,track,button,21223,59427,29713")])
    assert "Quiet level settled at 0.0212\n" in '\n'.join(out) + '\n'


def network_lines():
    # Network names never go into the events log; a scan is named as the
    # reason for the channel recalibration just before it
    name, other = 'MyNetwork', 'OtherNet'
    out, _ = drive([
        (0, f"NETWORK,default,{name}"), (0, "CALIB,1,track,boot,25000,70000,35000"),
        (5, f"NETWORK,default,{name}"),
        (10, "CALIB,2000000,wait,channel,0,0,0"),
        (14, f"NET,6,-49,{name}"), (14, "NETS,done,1"),
        (20, f"NETWORK,saved,{other}"),
        (25, f"NETWORK,chosen,{other}"),
        (100, "NETS,ignored,just-scanned"),
    ])
    text = '\n'.join(out)
    assert name not in text and other not in text, text
    assert "Recalibrating (router changed channel)" in text
    assert "scanned for networks: that was the channel change above" in text
    assert "Network switched from the PC" in text
    assert text.count("watching a different network") == 1
    assert text.count("scanned for networks") == 1, text


def replay_raw_log():
    # replay(): clock from the #T markers plus board time; a gzip cut off
    # mid-recording is read up to where it ends
    lines = ["#T,1800000000.000,",
             "CALIB,14000000,fast,boot,25000,70000,35000",
             "#T,1800000010.000,14000000",
             "STATE,16500000,motion,99,40",
             "STATE,19000000,still,30,30",
             "#T,1800000020.000,24000000"] + \
            [f"CSI,{24000000 + i * 10000},0,1,6,-50,0,0,104,AAAA" for i in range(3000)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'raw.log.gz')
        with gzip.open(path, 'wt') as f:
            f.write('\n'.join(lines) + '\n')
        data = open(path, 'rb').read()
        open(path, 'wb').write(data[:-12])       # no gzip trailer, as when copied while recording
        out = []
        rec = replay(path, out.append)
    text = '\n'.join(out)
    assert f"{clock(1_800_000_012.5)}  MOTION started" in text, text
    assert f"{clock(1_800_000_015.0)}  still again after 2 s" in text, text
    assert len(rec.periods) == 1 and rec.periods[0] == (1_800_000_012.5, 1_800_000_015.0)
    assert "1 motion period(s)" in text and "Summary" in text


if __name__ == '__main__':
    main()
