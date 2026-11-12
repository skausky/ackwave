#!/usr/bin/env python3
"""Web dashboard for the CSI motion sensor: the live view in a browser.

    python web.py /dev/ttyACM0                 # real board, http://localhost:8765
    python fake_csi.py --realtime --loop | python web.py -      # demo, no hardware

Same processing as gui.py (its State class), and with a board it logs to
sessions/ exactly like record.py. The server listens on 127.0.0.1 only, so
nothing else on your network can reach it; from a phone, use an SSH tunnel
(ssh -L 8765:localhost:8765 you@this-pc, then open http://localhost:8765).
--host 0.0.0.0 opens it to your LAN instead (anyone there can then see it and
send commands to the board).

Live data reaches the page as Server-Sent Events; commands come back as JSON
POSTs, which must carry an X-Ackwave header (a cross-site page can't add it
without a CORS preflight, which this server never grants) and a Host header
naming this server (no DNS rebinding).
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from collections import deque

from analyze import MAX_APS, open_port
from breathing import Breathing
from devices import MAX_DEVS, MODES, Devices, Oui, fmt_age
from doppler import FREQS, LOW_HZ, Doppler
from gui import (BRIEF_C, BRIEF_S, CAL_C, COLORS, DIM, FG, LONG_C, MOTION_C, SCAN_C, STILL_C,
                 SOUND_MODES, WINDOWS, State, reader, thin)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}
FAST_S, SLOW_S = 0.25, 1.0                  # update periods of the two event types
SETTINGS = {'per_link': False}              # dashboard settings (sessions/dashboard.json)
LINK_BASE_S = 120.0                         # per-link quiet level: 10th percentile over this


def r2(x):
    return None if x is None else round(float(x), 2)


class Dashboard:
    """Owns the State; turns it into JSON for the page and runs its commands."""

    def __init__(self, state, send=None, log_name=None, prefs=None, settings=None):
        self.s, self.send, self.log_name, self.prefs = state, send, log_name, prefs
        self.lock = threading.Lock()
        self.version = 0                         # bumps on every line fed
        self.settings_path = settings
        self.cfg = dict(SETTINGS, sound=None)    # sound: [mode, vol, hz] kept on the board, None = its own
        if settings:
            try:
                with open(settings) as f:
                    got = json.load(f)
                self.cfg.update({k: got[k] for k in SETTINGS if k in got})
                snd = got.get('sound')
                if snd is not None:
                    state.want_sound(*snd)       # re-applied whenever the board differs
                    self.cfg['sound'] = list(state.sound_want)
            except (OSError, ValueError, TypeError):
                pass
        if send and state.sound_want is None:
            state.outbox.append('SOUND')         # ask the board for its buzzer settings
        # Per-link analysis (experimental, off unless switched on): node -> analysers
        self.links = {}                          # src -> dict(br=Breathing, dop=Doppler, hist=deque)
        state.on_csi = self._on_csi

    def _on_csi(self, d, t):
        """Every CSI packet (called inside State.feed, lock held)."""
        if not self.cfg['per_link'] or d['kind'] != 1:
            return
        src = d['src']
        if src not in self.links:
            self.links[src] = dict(br=Breathing(src), dop=Doppler(WINDOWS[-1], src), hist=deque())
        ln = self.links[src]
        ln['dop'].feed(d, t)
        r = ln['br'].feed(d, t)
        if r is not None:
            ln['hist'].append((t, r))
            while ln['hist'] and ln['hist'][0][0] < t - WINDOWS[-1]:
                ln['hist'].popleft()

    # ---------- input ----------
    def feed(self, line):
        with self.lock:
            self.s.feed(line)
            self.version += 1
            self._flush()

    def _flush(self):
        for cmd in self.s.outbox:
            if self.send:
                self.send(cmd)
        self.s.outbox.clear()

    # ---------- commands from the page ----------
    def command(self, c):
        """Run one command dict; returns (ok, message)."""
        what = c.get('cmd')
        with self.lock:
            if what == 'cal':
                if not self.send:
                    return False, 'recalibrate needs a board (this is a demo or a file)'
                self.s.outbox.append('CAL')
                msg = 'Recalibrating: step away, the quiet level is measured again in 5 s'
            elif what == 'scan':
                if not self.send:
                    return False, 'scanning needs a board'
                if self.s.scanning():
                    return True, 'Already scanning'
                self.s.start_scan()
                msg = 'Scanning all channels (about 4 s)'
            elif what == 'use':
                name = c.get('name', '')
                if not isinstance(name, str) or not 1 <= len(name.encode()) <= 32 \
                        or any(ch in name for ch in '\r\n'):
                    return False, 'bad network name'
                if not self.send:
                    return False, 'choosing a network needs a board'
                self.s.outbox.append(f'USE,{name}')
                msg = f'Switching to "{name}": the board restarts (about 15 s)'
            elif what == 'use_default':
                if not self.send:
                    return False, 'choosing a network needs a board'
                self.s.outbox.append('USE_DEFAULT')
                msg = 'Back to the default network: the board restarts (about 15 s)'
            elif what == 'settings':
                changed = {k: bool(v) for k, v in c.items() if k in SETTINGS}
                if not changed:
                    return False, 'nothing to change'
                self.cfg.update(changed)
                if not self.cfg['per_link']:
                    self.links.clear()               # free the analysers
                self._save_settings()
                msg = 'Per-link analysis on (experimental)' if self.cfg['per_link'] \
                    else 'Per-link analysis off'
            elif what == 'sound':
                if not self.send:
                    return False, 'the buzzer needs a board'
                if c.get('test'):
                    self.s.outbox.append('SOUND,test')
                    msg = 'Playing the motion sound'
                elif c.get('mode') == 'default':
                    self.s.want_sound(None, 0, 0)
                    self.cfg['sound'] = None
                    self._save_settings()
                    msg = "Buzzer back to the firmware's settings"
                else:
                    try:
                        self.s.want_sound(c.get('mode'), int(c.get('vol')), int(c.get('hz')))
                    except (TypeError, ValueError):
                        return False, 'bad buzzer setting'
                    self.cfg['sound'] = list(self.s.sound_want)
                    self._save_settings()
                    msg = 'Buzzer off' if c['mode'] == 'off' else f"Buzzer: {c['mode']}"
            elif what == 'mode':
                if c.get('mode') not in MODES:
                    return False, 'bad mode'
                self.s.outbox += self.s.devices.set_mode(c['mode'])
                self._save_prefs()
                msg = f"Device mode: {c['mode']}"
            elif what == 'toggle':
                mac = c.get('mac', '')
                if mac not in self.s.devices.devs:
                    return False, 'unknown device'
                self.s.outbox += self.s.devices.toggle(mac)
                self._save_prefs()
                msg = 'Device list updated'
            else:
                return False, 'unknown command'
            self._flush()
            return True, msg

    def _save_settings(self):
        if self.settings_path:
            try:
                os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
                with open(self.settings_path, 'w') as f:
                    json.dump(self.cfg, f)
            except OSError:
                pass

    def _save_prefs(self):
        if self.prefs:
            try:
                os.makedirs(os.path.dirname(self.prefs), exist_ok=True)
                self.s.devices.save(self.prefs)
            except OSError:
                pass

    # ---------- output ----------
    def _pick(self, link):
        """(breathing, its history, doppler) for the chosen link: None = the router."""
        ln = self.links.get(link) if self.cfg['per_link'] and link is not None else None
        if ln:
            return ln['br'], ln['hist'], ln['dop']
        return self.s.br, self.s.br_hist, self.s.dop

    def fast(self, win, link=None):
        """Header, tiles, score chart and breathing strip (4 times a second)."""
        with self.lock:
            s = self.s
            br, hist, dop = self._pick(link)
            out = dict(header=self._header(br, dop), have_time=s.wall_off is not None,
                       settings=self.cfg)
            if s.wall_off is None:
                return out
            t1 = max(s.now, win)
            t0 = t1 - win
            out['score'] = self._score(t0, t1, win)
            out['breathing'] = [[r2(s.wall(t)), r2(r['rate']), r['level']]
                                for t, r in hist if t >= t0]
            out['window'] = [r2(s.wall(t0)), r2(s.wall(t1))]
            return out

    def slow(self, win, link=None):
        """Doppler, session timeline, links, devices, networks, raw counters (once a second)."""
        with self.lock:
            s = self.s
            out = dict(devices=self._devices(), network=self._network(), details=self._details(),
                       settings=self.cfg, sound=s.sound)
            if s.wall_off is None:
                return out
            t1 = max(s.now, win)
            out['doppler'] = self._doppler(t1 - win, t1, self._pick(link)[2])
            out['timeline'] = self._timeline()
            if self.cfg['per_link']:
                out['links'] = self._links()
            return out

    def _header(self, br=None, dop=None):
        s, det = self.s, self.s.det
        status = s.source_status()
        if status:
            word, col, detail = status
            badge = dict(word=word, color=col, sub=detail)
        elif det.floor <= 0:
            badge = dict(word='CALIBRATING', color=CAL_C, sub='measuring the quiet level: keep still')
        else:
            badge = dict(word='MOTION' if det.motion else 'STILL',
                         color=MOTION_C if det.motion else STILL_C, sub=s.last_motion())
        hw, hc, hd = s.link_health()
        tiles = {}
        r = (br or s.br).last
        if r is None:
            tiles['breathing'] = ('…', DIM, 'collecting 40 s')
        elif r['level'] == 'none':
            tiles['breathing'] = ('—', DIM, 'none found')
        else:
            tiles['breathing'] = (f"{r['rate']:.0f}/min",
                                  STILL_C if r['level'] == 'clear' else BRIEF_C,
                                  f"{r['level']} · last 40 s")
        d = s.doppler_reading(dop)
        if d is None:
            tiles['doppler'] = ('…', DIM, 'collecting')
        elif not d:
            tiles['doppler'] = ('—', DIM, 'nothing moving')
        elif d.get('scan'):
            tiles['doppler'] = ('scan', SCAN_C, 'router scanning (ignored)')
        elif d['step']:
            tiles['doppler'] = (f"{d['f']:.1f} Hz", BRIEF_C, 'step-like: router?')
        else:
            tiles['doppler'] = (f"{d['f']:.1f} Hz", FG, f"~{d['speed']:.2f} m/s")
        src = 'board' if det.external else 'PC'
        tiles['quiet'] = ((f"{det.floor:.4f}", FG, f"{det.phase} · from {src}") if det.floor > 0
                          else ('measuring', CAL_C, 'keep still'))
        if status:
            tiles = {k: (v, DIM, sub) for k, (v, _c, sub) in tiles.items()}
        recent = []
        for a, dur, peak, going in s.recent_motion(6):
            recent.append(dict(at=time.strftime('%H:%M:%S', time.localtime(a)),
                               dur=('now ' + fmt_age(dur)) if going
                               else ('brief' if dur < BRIEF_S else fmt_age(dur)),
                               peak=r2(peak), going=going, brief=not going and dur < BRIEF_S))
        n_long, n_brief = s.session_counts()
        dv = s.devices
        return dict(badge=badge, stale=bool(status),
                    link=dict(word=hw, color=hc, detail=hd),
                    tiles={k: dict(value=v, color=c, detail=t) for k, (v, c, t) in tiles.items()},
                    recent=recent, board={None: 'n/a', True: 'motion', False: 'still'}[s.board_motion],
                    counts=dict(motion=n_long, brief=n_brief, scans=len(s.hist_scans)),
                    devices=f"{len(dv.devs)} heard · poking {len(dv.poking)}/{MAX_DEVS} ({dv.mode})",
                    log=self.log_name, board_connected=bool(self.send))

    def _score(self, t0, t1, win):
        s, det = self.s, self.s.det
        series = []
        for key in sorted(set(s.arrivals) | set(s.scores)):
            pts = s.scores.get(key)
            if not pts:
                continue
            t, v = np.array(pts).T
            keep = t >= t0
            if not keep.any():
                continue
            t, v = thin(t[keep], v[keep], 700)
            is_ap = key[0] < MAX_APS
            series.append(dict(label=s.label(key) + ('' if is_ap else ' (extra link)'),
                               color=COLORS[(key[0] * 2 + key[1]) % len(COLORS)],
                               dash='solid' if is_ap and key[1] else 'dash' if is_ap else 'dot',
                               t=[r2(s.wall(x)) for x in t], v=[r2(x) for x in v]))
        long_ = None
        if s.long:
            t, v = np.array(s.long).T
            keep = t >= t0
            if keep.any():
                t, v = thin(t[keep], v[keep], 700)
                long_ = dict(t=[r2(s.wall(x)) for x in t], v=[r2(x) for x in v])

        def spans(lst, end):
            return [[r2(s.wall(max(a, t0))), r2(s.wall(b if b is not None else end))]
                    for a, b in lst if (b if b is not None else end) >= t0]
        return dict(series=series, long=long_, gap=max(0.5, win / 600),
                    on_s=r2(det.c['on_short']), on_l=r2(det.c['on_long']),
                    calibrating=det.floor <= 0,
                    motion=spans(s.motion_spans, t1), cal=spans(s.cal_spans, t1),
                    scans=spans(s.scan_spans, t1),
                    hops=[r2(s.wall(h)) for h in s.hops if h >= t0],
                    board=[[r2(s.wall(t)), bool(m)] for t, m in s.board_states if t >= t0])

    def _doppler(self, t0, t1, dop=None):
        s = self.s
        dop = dop or s.dop
        rows = [r for r in dop.rows if r[0] >= t0]
        if len(rows) < 2 or dop.base is None:
            return None
        ts = np.array([r[0] for r in rows])
        m = np.array([r[1] for r in rows])
        db = 10 * np.log10(np.maximum(m, 1e-12) / np.maximum(dop.base, 1e-12)[None, :])
        db = np.clip(db, 0, 20)
        k = int(np.ceil(len(ts) / 240))         # at most ~240 columns, each bin's peak
        if k > 1:
            n = len(ts) // k * k
            ts = ts[:n:k]
            db = db[:n].reshape(-1, k, db.shape[1]).max(1)
        return dict(t=[r2(s.wall(x)) for x in ts], fmax=float(FREQS[-1]), low=LOW_HZ,
                    step=r2((ts[1] - ts[0]) if len(ts) > 1 else 0.25),
                    db=np.rint(db).astype(int).tolist())

    def _timeline(self):
        s = self.s
        end = s.last_wall or time.time()
        start = s.hist_t[0] if s.hist_t else end - 60
        start = min(start, end - 60)
        out = dict(start=r2(start), end=r2(end), on_s=r2(s.det.c['on_short']),
                   on_l=r2(s.det.c['on_long']))
        if s.hist_t:
            t = np.array(s.hist_t, float) + 0.5
            tt, vs = thin(t, np.array(s.hist_s), 1200)
            _, vl = thin(t, np.array(s.hist_l), 1200)
            out.update(t=[r2(x) for x in tt], s=[r2(x) for x in vs], l=[r2(x) for x in vl])
        out['motion'] = [[r2(a), r2(b if b is not None else end),
                          ((b if b is not None else end) - a) < BRIEF_S] for a, b in s.hist_motion]
        out['cal'] = [[r2(a), r2(b if b is not None else end)] for a, b in s.hist_cal]
        out['scans'] = [r2(a) for a, _b in s.hist_scans]
        return out

    def _links(self):
        """One row per ACK link: rate, signal, activity against its own quiet level
        (10th percentile of its last 2 min), and its own breathing / Doppler."""
        s = self.s
        f = s.det.floor
        out = []
        for key in sorted(k for k in s.scores if k[1] == 1):
            pts = s.scores[key]
            if not pts or f <= 0:
                continue
            t, v = np.array(pts).T
            base_v = v[t >= s.now - LINK_BASE_S]
            if len(base_v) < 20:
                continue
            q = max(float(np.percentile(base_v, 10)), 1e-6)
            recent = t >= s.now - 30
            tt, vv = thin(t[recent], v[recent] / q, 150)
            now = v[t >= s.now - 0.5]
            act = float(now.mean() / q) if len(now) else 0.0
            ln = self.links.get(key[0])
            row = dict(src=key[0], label=s.devices.link_label(key[0]), ap=key[0] < MAX_APS,
                       rate=round(s.rate(key)), rssi=s.rssi.get(key, 0), activity=r2(act),
                       moving=act > s.det.c['on_short'],
                       spark=dict(t=[r2(s.wall(x)) for x in tt], v=[r2(x) for x in vv]),
                       breathing=None, doppler=None)
            if ln:
                b = ln['br'].last
                row['breathing'] = None if b is None else dict(rate=r2(b['rate']), level=b['level'])
                d = s.doppler_reading(ln['dop'])
                row['doppler'] = None if d is None else (
                    {} if not d else dict(f=r2(d['f']), scan=bool(d.get('scan')), step=bool(d.get('step'))))
            out.append(row)
        return out

    def _devices(self):
        dv = self.s.devices
        rows = []
        for r in dv.rows():
            rows.append(dict(mac=r['mac'], name=r['name'], rssi=r['rssi'], tx=r['tx'],
                             rate=round(r['rate'] * 60), heard=fmt_age(r['heard']) + ' ago',
                             why=r['why'], link=r['link'], poked=r['poked'], picked=r['picked'],
                             status=r['status']))
        return dict(mode=dv.mode, modes=list(MODES), rows=rows, max=MAX_DEVS,
                    poking=len(dv.poking), seen=dv.board_seen)

    def _network(self):
        how, name = self.s.network or ('?', '')
        return dict(how=how, name=name, state=self.s.nets_state,
                    nets=[dict(name=n, ch=ch, rssi=rssi,
                               aps=[dict(bssid=b, ch=c, rssi=r) for b, c, r in self.s.net_aps.get(n, [])])
                          for n, ch, rssi in self.s.nets])

    def _details(self):
        s, det = self.s, self.s.det
        live = [k for k in sorted(set(s.arrivals) | set(s.scores)) if s.rate(k) > 0]
        out = dict(channel=list(s.channel) if s.channel else None, phase=det.phase, why=det.why,
                   floor=det.floor, streams=[dict(label=s.label(k), rate=s.rate(k),
                                                  rssi=s.rssi.get(k, 0)) for k in live])
        if s.stats:
            out['stats'] = {k: v for k, v in s.stats.items() if isinstance(v, (int, float))}
        return out


def make_handler(dash, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_a):              # quiet console
            pass

        def _host_ok(self):
            host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
            return host in allowed_hosts

        def _reply(self, code, body, ctype='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._reply(403, {'error': 'host not allowed'})
            u = urlparse(self.path)
            if u.path in STATIC:
                name, ctype = STATIC[u.path]
                with open(os.path.join(HERE, 'web', name), 'rb') as f:
                    return self._reply(200, f.read(), ctype)
            if u.path == '/events':
                return self._events(parse_qs(u.query))
            if u.path == '/snapshot':                # one-off (screenshots, debugging)
                q = parse_qs(u.query)
                win, link = self._win(q), self._link(q)
                return self._reply(200, {'fast': dash.fast(win, link), 'slow': dash.slow(win, link)})
            self._reply(404, {'error': 'not found'})

        @staticmethod
        def _win(q):
            try:
                win = float(q.get('win', [WINDOWS[0]])[0])
            except ValueError:
                win = WINDOWS[0]
            return min(max(win, 10.0), WINDOWS[-1])

        @staticmethod
        def _link(q):
            try:
                return int(q['link'][0]) if 'link' in q else None
            except ValueError:
                return None

        def _events(self, q):
            win, link = self._win(q), self._link(q)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            next_slow = 0.0
            try:
                while True:
                    now = time.monotonic()
                    msgs = [('fast', dash.fast(win, link))]
                    if now >= next_slow:
                        msgs.append(('slow', dash.slow(win, link)))
                        next_slow = now + SLOW_S
                    for ev, data in msgs:
                        self.wfile.write(f"event: {ev}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(FAST_S)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self):
            if not self._host_ok():
                return self._reply(403, {'ok': False, 'msg': 'host not allowed'})
            origin = self.headers.get('Origin')
            host = self.headers.get('Host', '')
            if self.headers.get('X-Ackwave') != '1' or (origin and urlparse(origin).netloc != host):
                return self._reply(403, {'ok': False, 'msg': 'cross-site request refused'})
            if urlparse(self.path).path != '/cmd':
                return self._reply(404, {'ok': False, 'msg': 'not found'})
            try:
                n = int(self.headers.get('Content-Length') or 0)
                c = json.loads(self.rfile.read(min(n, 4096)) or b'{}')
                if not isinstance(c, dict):
                    raise ValueError
            except ValueError:
                return self._reply(400, {'ok': False, 'msg': 'bad request'})
            ok, msg = dash.command(c)
            self._reply(200 if ok else 400, {'ok': ok, 'msg': msg})
    return Handler


def serve(dash, host='127.0.0.1', port=8765):
    """Start the HTTP server in a thread; returns it (server.server_address has the port)."""
    allowed = {'localhost', '127.0.0.1', '::1'}
    if host not in ('127.0.0.1', 'localhost', '::1'):
        import socket
        allowed |= {host, socket.gethostname().lower()}
        try:
            allowed |= {socket.gethostbyname(socket.gethostname())}
        except OSError:
            pass
        if host in ('0.0.0.0', '::'):
            allowed.add('*')
    srv = ThreadingHTTPServer((host, port), make_handler(dash, allowed))
    srv.daemon_threads = True
    if '*' in allowed:                           # LAN mode: any Host the user types
        srv.RequestHandlerClass._host_ok = lambda self: True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def pump(dash, q):
    """Feed lines from the reader thread into the dashboard until input ends."""
    s = dash.s
    while True:
        line = q.get()
        if line is None:
            with dash.lock:
                s.ended = s.lost is None
            return
        if line.startswith('#ERR'):
            print(line[1:], file=sys.stderr)
            with dash.lock:
                s.lost = line[5:].strip() or 'connection lost'
            continue
        try:
            dash.feed(line)
        except Exception as e:                   # one odd line must not stop the dashboard
            print(f"skipped a line ({e})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('source', help="serial port (/dev/ttyACM0, COM5) or '-' for stdin")
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1',
                    help='127.0.0.1 = this PC only (default); 0.0.0.0 = whole LAN')
    ap.add_argument('--open', action='store_true', help='open it in the browser')
    ap.add_argument('--no-log', action='store_true', help="don't log to sessions/")
    ap.add_argument('--out', default=os.path.join(os.path.dirname(HERE), 'sessions'), help='log folder')
    a = ap.parse_args()

    state = State(WINDOWS[-1])
    log = awake = None
    if a.source != '-' and not a.no_log:
        from record import SessionLog, keep_awake
        log = SessionLog(a.out)
        awake = keep_awake('sleep')
        print(f"Logging to {log.ev_path}")
    prefs = os.path.join(a.out, 'devices.json')
    mode, picks = Devices.load_prefs(prefs)
    state.devices = Devices(oui=Oui(), mode=mode, manual=picks)
    lines, send = open_port(a.source, a.baud)
    dash = Dashboard(state, send, os.path.basename(log.ev_path) if log else None, prefs,
                     os.path.join(a.out, 'dashboard.json'))
    q = queue.Queue()
    threading.Thread(target=reader, args=(lines, q, log), daemon=True).start()
    srv = serve(dash, a.host, a.port)
    url = f"http://localhost:{srv.server_address[1]}"
    print(f"Dashboard: {url}   (Ctrl+C to stop)")
    if a.host not in ('127.0.0.1', 'localhost', '::1'):
        print(f"  Open to the network on {a.host}: anyone there can view it and send commands.")
    else:
        print(f"  From a phone: ssh -L {a.port}:localhost:{a.port} <you>@<this-pc>, "
              f"then open {url} on the phone.")
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        pump(dash, q)
        print("Input ended; the dashboard stays up until Ctrl+C.")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        if log:
            log.close()
        if awake:
            from record import release
            release(awake)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
