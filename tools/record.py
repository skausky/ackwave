#!/usr/bin/env python3
"""Record motion events from the board, e.g. overnight.

    python record.py                 # finds the board, writes to sessions/ in the project
    python record.py /dev/ttyACM0 --out DIR --no-raw

Writes a readable log with clock times (events-<start>.log) and, unless
--no-raw, every line from the board (raw-<start>.log.gz) for later analysis.
Keeps the PC from sleeping while it runs (Linux with systemd), reconnects if
the board is unplugged, and prints a summary of all motion when you press
Ctrl+C.
"""
import argparse
import glob
import gzip
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from analyze import parse_line

BRIEF_S = 2.0       # motion shorter than this is listed apart: often noise or a twitch


def find_port():
    ports = sorted(glob.glob('/dev/ttyACM*')) + sorted(glob.glob('/dev/ttyUSB*'))
    return ports[0] if ports else None


def clock(t):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))


def duration(s):
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


class Recorder:
    """Turns board lines into readable events. Time comes from `now()`, so
    tests can drive it with a fake clock."""

    def __init__(self, write, now=time.time):
        self.write, self.now = write, now
        self.floor = 0.0
        self.calibrated = False
        self.motion_since = None
        self.periods = []                         # [(start, end)] wall-clock seconds
        self.started = now()
        self.last_ts = None                       # last board timestamp seen (us)

    def say(self, text):
        self.write(f"{clock(self.now())}  {text}")

    def feed(self, line):
        ev = parse_line(line)
        if ev is None:
            if line.startswith('BUTTON,recalibrate'):
                self.say("BOOT button pressed: recalibrating in 5 s, step away")
            return
        kind, d = ev
        if 'ts' in d:
            self.last_ts = d['ts']
        if kind == 'calib':
            had = self.calibrated
            self.floor = d['floor']
            self.calibrated = d['floor'] > 0
            if self.calibrated and not had:
                self.say(f"Calibrated (quiet level {d['floor']:.4f}), watching for motion")
            elif not self.calibrated and had and d['phase'] in ('wait', 'collect'):
                why = {'button': 'button pressed', 'rssi': 'board seems to have moved',
                       'channel': 'router changed channel', 'boot': 'board restarted'}
                self.say(f"Recalibrating ({why.get(d['why'], d['why'])}), keep still")
        elif kind == 'state':
            t = self.now()
            if d['motion'] and self.motion_since is None:
                self.motion_since = t
                rel = ''
                if self.floor > 0:
                    rel = f" (short x{d['score'] / self.floor:.1f}"
                    if d.get('score_long') is not None:
                        rel += f", long x{d['score_long'] / self.floor:.1f}"
                    rel += ")"
                self.say(f"MOTION started{rel}")
            elif not d['motion'] and self.motion_since is not None:
                self.periods.append((self.motion_since, t))
                brief = " (brief)" if t - self.motion_since < BRIEF_S else ""
                self.say(f"still again after {duration(t - self.motion_since)}{brief}")
                self.motion_since = None
        elif kind == 'channel':
            self.say(f"Router channel {d['ch']}")

    def summary(self):
        end = self.now()
        periods = list(self.periods)
        if self.motion_since is not None:
            periods.append((self.motion_since, end))
        brief = [a for a, b in periods if b - a < BRIEF_S]
        periods = [(a, b) for a, b in periods if b - a >= BRIEF_S]
        total = sum(b - a for a, b in periods)
        out = ["", f"Summary {clock(self.started)} to {clock(end)} "
                   f"({duration(end - self.started)})"]
        if not periods:
            out.append("  No motion detected." if not brief else "  No sustained motion.")
        else:
            out.append(f"  {len(periods)} motion period(s), {duration(total)} in total:")
            for a, b in periods[:200]:
                out.append(f"    {clock(a)}  for {duration(b - a)}")
            if len(periods) > 200:
                out.append(f"    ... and {len(periods) - 200} more")
        if brief:
            out.append(f"  {len(brief)} brief blip(s) under {BRIEF_S:.0f} s "
                       "(often noise, sometimes a small movement):")
            times = [time.strftime('%H:%M', time.localtime(a)) for a in brief]
            for i in range(0, len(times), 10):
                out.append("    " + "  ".join(times[i:i + 10]))
        return out


def keep_awake(what='sleep:idle'):
    """Hold a systemd inhibitor while we run; returns the process or None.
    'sleep:idle' also keeps the screen on; 'sleep' only stops suspend."""
    if not shutil.which('systemd-inhibit'):
        return None
    try:
        return subprocess.Popen(['systemd-inhibit', f'--what={what}',
                                 '--why=Recording CSI motion', 'sleep', 'infinity'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


class SessionLog:
    """Writes events-<start>.log (readable, clock times) and raw-<start>.log.gz
    (every board line, plus '#T,<unix time>,<board ts>' markers every 10 s so
    the raw data can be put on a clock later). Thread-safe: feed() may run in a
    reader thread while close() runs in the main one."""

    MARK_S = 10.0

    def __init__(self, out, raw=True, echo=True, now=time.time):
        os.makedirs(out, exist_ok=True)
        stamp = time.strftime('%Y-%m-%d_%H%M', time.localtime(now()))
        self.ev_path = os.path.join(out, f"events-{stamp}.log")
        self.raw_path = os.path.join(out, f"raw-{stamp}.log.gz") if raw else None
        self.events = open(self.ev_path, 'a', buffering=1)
        self.raw = gzip.open(self.raw_path, 'at', compresslevel=1) if raw else None
        self.echo, self.now = echo, now
        self.lock = threading.Lock()
        self.rec = Recorder(self._write, now=now)
        self.next_mark = 0.0
        self.closed = False
        self.rec.say("Recording started")

    def _write(self, text):
        if self.echo:
            print(text, flush=True)
        self.events.write(text + '\n')

    def say(self, text):
        with self.lock:
            if not self.closed:
                self.rec.say(text)

    def feed(self, line):
        with self.lock:
            if self.closed:
                return
            if self.raw:
                t = self.now()
                if t >= self.next_mark:
                    ts = self.rec.last_ts
                    self.raw.write(f"#T,{t:.3f},{ts if ts is not None else ''}\n")
                    self.next_mark = t + self.MARK_S
                self.raw.write(line if line.endswith('\n') else line + '\n')
            self.rec.feed(line)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.rec.say("Recording stopped")
            for text in self.rec.summary():
                self._write(text)
            if self.raw:
                self.raw.close()
            self.events.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('port', nargs='?', help='serial port (default: find the board)')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sessions'), help='output folder')
    ap.add_argument('--no-raw', action='store_true', help="don't keep the full data")
    ap.add_argument('--baud', type=int, default=921600)
    a = ap.parse_args()

    import serial                                 # pip install pyserial
    port = a.port or find_port()
    if not port:
        sys.exit("No board found (/dev/ttyACM*, /dev/ttyUSB*). Is it plugged in?")
    log = SessionLog(a.out, raw=not a.no_raw)
    awake = keep_awake()
    print(f"Recording from {port}")
    print(f"  events: {log.ev_path}")
    if log.raw_path:
        print(f"  raw:    {log.raw_path}")
    print("  The PC is kept awake while this runs." if awake else
          "  (Couldn't stop the PC from sleeping; check its power settings.)")
    print("Press Ctrl+C to stop.\n")

    ser, warned = None, False
    try:
        while True:
            if ser is None:
                try:
                    ser = serial.Serial(port, a.baud, timeout=1)
                    if warned:
                        log.say("Board reconnected")
                    warned = False
                except (serial.SerialException, OSError):
                    if not warned:
                        log.say("Board not available, waiting for it...")
                        warned = True
                    time.sleep(2)
                    continue
            try:
                b = ser.readline()
            except (serial.SerialException, OSError):
                ser.close()
                ser = None
                continue
            if b:
                log.feed(b.decode('utf-8', 'replace'))
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # a 2nd Ctrl+C mustn't cut the summary
        log.close()
        if ser:
            ser.close()
        release(awake)


def release(awake):
    """Release a keep_awake() inhibitor."""
    if awake:
        awake.terminate()
        try:
            awake.wait(timeout=3)
        except subprocess.TimeoutExpired:
            awake.kill()


if __name__ == '__main__':
    main()
