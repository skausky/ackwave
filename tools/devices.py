"""Devices on our network: who made them (OUI), whether they look stationary,
and which ones the board should poke.

The board lists every device it hears talking to our APs (DEV lines) and
pokes the ones we ask for (POKE / UNPOKE). This module decides what to ask
for. It is pure logic fed with parsed board events, so it is tested without
hardware; the GUI shows it and sends the commands.

Modes:
  auto    poke up to 4 devices that look stationary (the default)
  all     poke up to 4 of the most active devices, phones included
  manual  poke exactly the ones you tick
  off     poke no devices (the router link always runs)

A device looks stationary when, after OBSERVE_S of watching, it has a real
(not randomized) MAC, has actually sent frames (a sleeping device never
answers a poke), is not too weak, and its signal stays steady. Phones fail
the first test: they use a private random address per network. A poked
device that stops answering is dropped for RETRY_S.

Note the board's DEV signal is an 8-frame average, so the steadiness test
sees slow drift (a carried phone), not a single person walking past.
"""
import csv
import json
import os
import statistics
import threading
import time
from collections import deque

MAX_DEVS = 4
OBSERVE_S = 180          # watch this long before calling a device stationary
MIN_TX = 20              # frames it must have sent
MIN_SAMPLES = 4          # signal readings needed to judge steadiness
MIN_RSSI = -80           # dBm, same bar as the board's AP gating
MAX_SPREAD = 3.0         # dB, std of its signal readings
SPREAD_WINDOW_S = 600
GONE_S = 120             # not heard this long: gone
FAIL_S = 60              # poked but silent / weak this long: drop it
RETRY_S = 1800           # a dropped device may be tried again after this
REFUSE_RETRY_S = 60
MODES = ('auto', 'all', 'manual', 'off')

STATUS_TEXT = {
    'stationary': 'stationary',
    'watching': 'watching',
    'moving': 'signal unsteady',
    'quiet': 'rarely transmits',
    'weak': 'too weak',
    'random': 'private MAC',
    'dropped': "didn't answer",
    'gone': 'gone',
}


def mac_is_random(mac):
    """Locally administered: phones' private / random addresses."""
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except ValueError:
        return False


class Oui:
    """MAC prefix -> maker. Uses the `oui` tool's database when present
    (~/.oui-database/master_oui.json), else the system IEEE lists. Loads in
    the background; lookups return None until it's ready."""

    PATHS = (os.path.expanduser('~/.oui-database/master_oui.json'),
             '/usr/share/ieee-data/oui.csv',
             '/usr/share/arp-scan/ieee-oui.txt')

    def __init__(self, paths=None, background=True):
        self.table, self.source = {}, None
        self.ready = threading.Event()
        paths = self.PATHS if paths is None else paths
        if background:
            threading.Thread(target=self._load, args=(paths,), daemon=True).start()
        else:
            self._load(paths)

    def _load(self, paths):
        for p in paths:
            try:
                t = self._read(p)
            except (OSError, ValueError):
                continue
            if t:
                self.table, self.source = t, p
                break
        self.ready.set()

    @staticmethod
    def _read(path):
        t = {}
        if path.endswith('.json'):
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
            for k, v in raw.items():
                key = k.replace(':', '').replace('-', '').upper()
                if len(key) == 6 and key not in t and isinstance(v, dict):
                    name = v.get('short_name') or v.get('manufacturer') or ''
                    t[key] = (name, v.get('manufacturer') or name, v.get('type') or '')
        elif path.endswith('.csv'):
            with open(path, encoding='utf-8', errors='replace') as f:
                for row in csv.reader(f):
                    if len(row) >= 3 and row[0] == 'MA-L' and len(row[1]) == 6:
                        t[row[1].upper()] = (row[2].split(',')[0].strip(), row[2].strip(), '')
        else:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    p = line.rstrip('\n').split('\t', 1)
                    if len(p) == 2 and len(p[0]) == 6:
                        t[p[0].upper()] = (p[1].split(',')[0].strip(), p[1].strip(), '')
        return t

    def lookup(self, mac):
        """(short name, full name, type) or None."""
        return self.table.get(mac.replace(':', '')[:6].upper())

    def name(self, mac):
        if mac_is_random(mac):
            return 'private address'
        hit = self.lookup(mac)
        if hit:
            return hit[0]
        return 'unknown' if self.ready.is_set() else '...'


class Device:
    __slots__ = ('mac', 'first', 'last_heard', 'tx', 'rx', 'rssi', 'samples', 'tx_hist')

    def __init__(self, mac, now):
        self.mac = mac
        self.first = self.last_heard = now
        self.tx = self.rx = 0
        self.rssi = 0
        self.samples = deque()           # (t, rssi) each time it had sent more frames
        self.tx_hist = deque()           # (t, tx) for its send rate

    def tx_rate(self, now, span=60.0):
        h = [(t, n) for t, n in self.tx_hist if t >= now - span]
        if len(h) < 2 or h[-1][0] <= h[0][0]:
            return 0.0
        return (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0])

    def spread(self, now):
        v = [r for t, r in self.samples if t >= now - SPREAD_WINDOW_S]
        return statistics.pstdev(v) if len(v) >= MIN_SAMPLES else None


class Devices:
    def __init__(self, oui=None, clock=time.time, mode='auto', manual=()):
        self.oui = oui
        self.clock = clock
        self.mode = mode if mode in MODES else 'auto'
        self.manual = set(manual)
        self.devs = {}
        self.poking = {}                 # mac -> node index, as the board last said
        self._cycle = {}
        self.gate = {}                   # node index -> last GATE dict
        self.dropped = {}                # mac -> (time, why)
        self.refused = {}                # mac -> (time, why)
        self.bad_since = {}
        self.board_seen = False          # any DEV / POKING / STATS seen yet

    def forget_network(self):
        """The board now watches another network: what it heard on the old one
        no longer applies (the user's mode and picks stay)."""
        self.devs, self.poking, self._cycle, self.gate = {}, {}, {}, {}
        self.dropped, self.refused, self.bad_since = {}, {}, {}

    # ---- events from the board ----
    def feed(self, kind, d):
        """Feed one parsed event. Returns commands to send (maybe empty)."""
        now = self.clock()
        if kind == 'dev':
            self.board_seen = True
            dv = self.devs.get(d['mac'])
            if dv is None:
                dv = self.devs[d['mac']] = Device(d['mac'], now)
            heard = now - d['age_ms'] / 1000.0
            dv.last_heard = max(dv.last_heard, heard)
            if d['tx'] > dv.tx and d['rssi'] < 0:        # fresh signal reading
                dv.samples.append((now, d['rssi']))
                while dv.samples and dv.samples[0][0] < now - SPREAD_WINDOW_S:
                    dv.samples.popleft()
            if d['tx'] < dv.tx:                           # board rebooted
                dv.tx_hist.clear()
            dv.tx, dv.rx = d['tx'], d['rx']
            if d['rssi'] < 0:
                dv.rssi = d['rssi']
            dv.tx_hist.append((now, d['tx']))
            while dv.tx_hist and dv.tx_hist[0][0] < now - 300:
                dv.tx_hist.popleft()
        elif kind == 'gate':
            self.gate[d['idx']] = d
        elif kind == 'poking':
            self.board_seen = True
            self._cycle[d['mac']] = d['idx']
            self.poking[d['mac']] = d['idx']
            self.refused.pop(d['mac'], None)
        elif kind == 'unpoked':
            if d['mac'] == 'all':
                self.poking.clear()
            else:
                self.poking.pop(d['mac'], None)
        elif kind == 'refused':
            if d['mac']:
                self.refused[d['mac']] = (now, d['why'])
        elif kind == 'stats':                             # end of the board's 5 s report
            self.board_seen = True
            self.poking = dict(self._cycle)
            self._cycle = {}
            self._check_health(now)
            return self.reconcile()
        return []

    def _check_health(self, now):
        for mac, idx in self.poking.items():
            g = self.gate.get(idx)
            if g and g['state'] in ('silent', 'weak'):
                t0 = self.bad_since.setdefault(mac, now)
                if now - t0 >= FAIL_S and self.mode in ('auto', 'all'):
                    why = "didn't answer pokes" if g['state'] == 'silent' else 'signal too weak'
                    self.dropped[mac] = (now, why)
                    self.bad_since.pop(mac, None)
            else:
                self.bad_since.pop(mac, None)

    # ---- judgement ----
    def status(self, dv, now=None):
        """(status key, human reason)."""
        now = self.clock() if now is None else now
        if now - dv.last_heard > GONE_S:
            return 'gone', f"not heard for {fmt_age(now - dv.last_heard)}"
        drop = self.dropped.get(dv.mac)
        if drop and now - drop[0] < RETRY_S:
            return 'dropped', drop[1]
        if mac_is_random(dv.mac):
            return 'random', 'private MAC (likely a phone or laptop)'
        watched = now - dv.first
        if watched < OBSERVE_S:
            return 'watching', f"watching, {fmt_age(OBSERVE_S - watched)} left"
        if dv.tx < MIN_TX:
            return 'quiet', f"sent only {dv.tx} frames (asleep / power saving)"
        if dv.rssi < MIN_RSSI:
            return 'weak', f"{dv.rssi} dBm is too weak"
        sp = dv.spread(now)
        if sp is None:
            return 'quiet', 'too few transmissions to judge'
        if sp > MAX_SPREAD:
            return 'moving', f"signal varies +/-{sp:.1f} dB"
        return 'stationary', f"steady signal (+/-{sp:.1f} dB)"

    def desired(self, now=None):
        """The set of MACs that should be poked in the current mode."""
        now = self.clock() if now is None else now
        if self.mode == 'off':
            return set()
        if self.mode == 'manual':
            return set(sorted(self.manual)[:MAX_DEVS])

        def ok(dv):
            r = self.refused.get(dv.mac)
            return not (r and now - r[0] < REFUSE_RETRY_S)

        if self.mode == 'auto':
            cands = [dv for dv in self.devs.values()
                     if ok(dv) and self.status(dv, now)[0] == 'stationary']
            cands.sort(key=lambda dv: (dv.mac not in self.poking, -dv.rssi, dv.mac))
        else:                                            # all: most active first
            cands = [dv for dv in self.devs.values()
                     if ok(dv) and dv.tx > 0
                     and self.status(dv, now)[0] not in ('gone', 'dropped')]
            cands.sort(key=lambda dv: (dv.mac not in self.poking, -dv.tx_rate(now), dv.mac))
        return {dv.mac for dv in cands[:MAX_DEVS]}

    def reconcile(self):
        """Commands that move the board from what it pokes to what we want."""
        if not self.board_seen:
            return []
        want = self.desired()
        cmds = [f"UNPOKE,{m}" for m in sorted(self.poking) if m not in want]
        room = MAX_DEVS - (len(self.poking) - len(cmds))
        cmds += [f"POKE,{m}" for m in sorted(want - set(self.poking))][:max(room, 0)]
        return cmds

    # ---- user actions ----
    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(mode)
        if mode == 'manual' and self.mode != 'manual':
            self.manual = set(self.poking)           # start from what runs now
        self.mode = mode
        return self.reconcile()

    def toggle(self, mac):
        """Tick / untick a device. Switches to manual mode. Returns commands."""
        if self.mode != 'manual':
            self.manual = set(self.poking)
            self.mode = 'manual'
        if mac in self.manual:
            self.manual.discard(mac)
        elif len(self.manual) < MAX_DEVS:
            self.manual.add(mac)
            self.dropped.pop(mac, None)
        return self.reconcile()

    # ---- display ----
    def name(self, mac):
        return self.oui.name(mac) if self.oui else ('private address' if mac_is_random(mac) else '')

    def link_label(self, idx):
        """Short name for node idx: 'router', 'AP 1' or a device's maker."""
        if idx < 8:
            return 'router' if idx == 0 else f"AP {idx}"
        for mac, i in self.poking.items():
            if i == idx:
                return self.name(mac) or mac[-8:]
        return f"device {idx}"

    def rows(self, now=None):
        """Table rows, poked first, then by status, then strongest."""
        now = self.clock() if now is None else now
        order = {k: i for i, k in enumerate(STATUS_TEXT)}
        out = []
        for dv in self.devs.values():
            st, why = self.status(dv, now)
            idx = self.poking.get(dv.mac)
            g = self.gate.get(idx) if idx is not None else None
            link = ''
            if idx is not None:
                link = 'starting' if not g else (
                    f"{g['state']}, {g['yield_pct']}% answered" if g['yield_pct'] >= 0
                    else f"{g['state']}, judging")
            out.append(dict(mac=dv.mac, name=self.name(dv.mac), rssi=dv.rssi, tx=dv.tx,
                            rx=dv.rx, rate=dv.tx_rate(now), heard=now - dv.last_heard,
                            status=st, why=why, poked=idx is not None, idx=idx, link=link,
                            picked=dv.mac in self.manual))
        out.sort(key=lambda r: (not r['poked'], order.get(r['status'], 99), -r['rssi'] if r['rssi'] else 999))
        return out

    # ---- remember mode and picks between runs ----
    def save(self, path):
        try:
            with open(path, 'w') as f:
                json.dump({'mode': self.mode, 'manual': sorted(self.manual)}, f)
        except OSError:
            pass

    @staticmethod
    def load_prefs(path):
        try:
            with open(path) as f:
                p = json.load(f)
            return p.get('mode', 'auto'), p.get('manual', [])
        except (OSError, ValueError):
            return 'auto', []


def fmt_age(s):
    s = max(0, int(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s" if s < 600 else f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"
