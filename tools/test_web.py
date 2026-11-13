#!/usr/bin/env python3
"""Web dashboard: JSON payloads from demo data, commands, per-link mode, and
the HTTP server's safety checks (host, cross-site POSTs)."""
import http.client
import json
import os
import tempfile

from fake_csi import generate
from gui import State
from web import Dashboard, serve


def request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def main():
    tmp = tempfile.mkdtemp()
    sent = []
    s = State(600)
    dash = Dashboard(s, send=sent.append, prefs=os.path.join(tmp, 'devices.json'),
                     settings=os.path.join(tmp, 'dashboard.json'))
    lines = [line for line, *_ in generate(seed=2)]
    half = len(lines) // 2
    for line in lines[:half]:
        dash.feed(line)

    # Payloads: JSON, bounded in size, with the parts the page draws
    f, sl = dash.fast(30), dash.slow(30)
    blob = json.dumps(f) + json.dumps(sl)
    assert len(blob) < 400_000, len(blob)
    assert f['header']['badge']['word'] in ('STILL', 'MOTION', 'CALIBRATING')
    assert f['score']['series'] and f['window'][1] > f['window'][0]
    assert 'links' not in sl and sl['settings'] == {'per_link': False, 'sound': None, 'sens': None}
    assert sent[:2] == ['SOUND', 'SENS'], "asks the board for its buzzer and threshold settings"
    assert sl['timeline']['end'] >= sl['timeline']['start']
    f600 = dash.fast(600)
    assert all(len(se['t']) <= 702 for se in f600['score']['series']), "series thinned"

    # Commands: validated, mapped to board commands
    assert dash.command({'cmd': 'cal'})[0] and sent[-1] == 'CAL'
    assert dash.command({'cmd': 'scan'})[0] and sent[-1] == 'SCAN'
    assert dash.command({'cmd': 'use', 'name': 'MyNetwork'})[0] and sent[-1] == 'USE,MyNetwork'
    assert not dash.command({'cmd': 'use', 'name': 'x' * 33})[0], "name too long"
    assert not dash.command({'cmd': 'use', 'name': 'a\nCAL'})[0], "no command injection"
    assert dash.command({'cmd': 'use_default'})[0] and sent[-1] == 'USE_DEFAULT'
    assert not dash.command({'cmd': 'mode', 'mode': 'bogus'})[0]
    assert not dash.command({'cmd': 'toggle', 'mac': 'nope'})[0]
    assert not dash.command({'cmd': 'rm -rf'})[0]

    # Per-link mode: off by default; once on, each ACK link gets its own analysis
    assert dash.command({'cmd': 'settings', 'per_link': True})[0]
    assert json.load(open(os.path.join(tmp, 'dashboard.json'))) == {'per_link': True, 'sound': None, 'sens': None}
    for line in lines[half:]:
        dash.feed(line)
    sl = dash.slow(30)
    links = sl['links']
    assert len(links) >= 2 and all(r['activity'] >= 0 and r['spark']['t'] for r in links), links
    # each link has its own verdict; the demo's motion shows on some link, and
    # the board's own verdict comes along for comparison
    assert all(r['state'] in ('motion', 'still', 'calibrating') for r in links)
    sl600 = dash.slow(600)
    assert any(r['spans'] for r in sl600['links']), "per-link motion spans"
    assert all(a <= b for r in sl600['links'] for a, b in r['spans'])
    assert isinstance(sl600['board_spans'], list)
    # motion that started before the kept history still shows (it ends in view)
    bs = dash.s.board_states
    saved_states, saved_motion = list(bs), dash.s.board_motion
    bs.clear(); bs.append((dash.s.now - 5, False))
    w1 = dash.s.wall(dash.s.now)
    spans = dash._board_spans(dash.s.now - 30, dash.s.now)
    assert len(spans) == 1 and abs(spans[0][0] - (w1 - 30)) < 0.05 and abs(spans[0][1] - (w1 - 5)) < 0.05, spans
    bs.clear(); dash.s.board_motion = True       # ongoing, its start long gone
    assert len(dash._board_spans(dash.s.now - 30, dash.s.now)) == 1
    bs.extend(saved_states); dash.s.board_motion = saved_motion
    router = [ln for ln in dash.links.values()][0]['md'].det
    assert router.c['on_long'] == s.det.c['on_long'], "links follow the board's thresholds"
    other = [r['src'] for r in links if r['src'] != 0][0]
    assert dash.fast(30, other)['breathing'] is not None and dash.slow(30, other)['doppler'] is not None
    assert dash.command({'cmd': 'settings', 'per_link': False})[0] and not dash.links
    assert Dashboard(State(60), settings=os.path.join(tmp, 'dashboard.json')).cfg['per_link'] is False

    # Buzzer: validated, saved, and kept applied after a board restart
    path = os.path.join(tmp, 'dashboard.json')
    assert dash.command({'cmd': 'sound', 'mode': 'off', 'vol': 15, 'hz': 3500})[0] and sent[-1] == 'SOUND,off,15,3500'
    assert json.load(open(path))['sound'] == ['off', 15, 3500]
    for bad in ({'mode': 'loud', 'vol': 15, 'hz': 3500}, {'mode': 'beep', 'vol': 51, 'hz': 3500},
                {'mode': 'beep', 'vol': 15, 'hz': 9000}, {'mode': 'beep', 'vol': 'x', 'hz': 3500},
                {'mode': 'beep\nCAL', 'vol': 15, 'hz': 3500}, {'mode': 'beep'}):
        assert not dash.command(dict(cmd='sound', **bad))[0], bad
    assert sent[-1] == 'SOUND,off,15,3500' and json.load(open(path))['sound'] == ['off', 15, 3500]
    dash.feed('SOUND,off,15,3500\n')                # the board confirms: nothing more to send
    assert sent[-1] == 'SOUND,off,15,3500' and dash.slow(30)['sound']['mode'] == 'off'
    n = len(sent)
    s.clock = lambda: 1e9                            # later: the board restarted with its own settings
    dash.feed('SOUND,beep,15,3500\n')
    assert sent[n:] == ['SOUND,off,15,3500'], sent[n:]
    dash.feed('SOUND,beep,15,3500\n')               # no flood while it catches up
    assert len(sent) == n + 1
    assert dash.command({'cmd': 'sound', 'test': True})[0] and sent[-1] == 'SOUND,test'
    sent2 = []
    d2 = Dashboard(State(60), send=sent2.append, settings=path)
    assert d2.cfg['sound'] == ['off', 15, 3500] and d2.s.outbox == ['SOUND,off,15,3500', 'SENS'], d2.s.outbox
    assert dash.command({'cmd': 'sound', 'mode': 'default'})[0] and sent[-1] == 'SOUND,default'
    assert json.load(open(path))['sound'] is None
    dash.feed('SOUND,beep,15,3500,no-buzzer\n')
    assert sent[-1] == 'SOUND,default' and dash.slow(30)['sound']['buzzer'] is False
    assert not Dashboard(State(60), settings=path).command({'cmd': 'sound', 'test': True})[0], "needs a board"
    for damaged in ('[]', '"x"', '{', '{"sound": [1, 2], "sens": "high"}'):
        open(path, 'w').write(damaged)
        d4 = Dashboard(State(60), send=[].append, settings=path)   # starts with defaults
        assert d4.cfg['sound'] is None and d4.cfg['sens'] is None, damaged

    # Sensitivity: the board's long threshold, kept applied; the PC detector follows it
    s.clock = lambda: 2e9
    dash.feed('SENS,140\n')
    assert dash.slow(30)['sens'] == 140 and s.det.c['on_long'] == 1.4
    assert dash.command({'cmd': 'sens', 'x100': 160})[0] and sent[-1] == 'SENS,160'
    assert json.load(open(path))['sens'] == 160
    for bad in (109, 301, 'x', None, '160\nCAL', 160.9, True, '160'):
        assert not dash.command({'cmd': 'sens', 'x100': bad})[0], bad
    dash.feed('SENS,160\n')
    assert abs(s.det.c['on_long'] - 1.6) < 1e-9 and abs(s.det.c['off_long'] - 1.6 * 1.2 / 1.4) < 1e-9
    n = len(sent)
    s.clock = lambda: 3e9                            # board restarted: back at its own value
    dash.feed('SENS,140\n')
    assert sent[n:] == ['SENS,160'], sent[n:]
    d3 = Dashboard(State(60), send=[].append, settings=path)
    assert d3.cfg['sens'] == 160 and 'SENS,160' in d3.s.outbox
    # A mismatch inside the 3 s window (the board restarted just after a send)
    # is fixed by the board's next report, 5 s later
    now = [4e9]
    s.clock = lambda: now[0]
    assert dash.command({'cmd': 'sens', 'x100': 160})[0] and sent[-1] == 'SENS,160'
    n = len(sent)
    now[0] += 1
    dash.feed('SENS,140\n')                         # boot report, inside the window
    assert len(sent) == n
    now[0] += 5
    dash.feed('SENS,140\n')                         # periodic report
    assert sent[n:] == ['SENS,160'], sent[n:]
    # CALIB carries the board's thresholds; the end thresholds keep the ratio
    dash.feed('CALIB,1,track,boot,50000,140000,90000\n')
    c = s.det.c
    assert abs(c['on_long'] - 1.8) < 1e-9 and abs(c['off_long'] - 1.8 * 1.2 / 1.4) < 1e-9
    assert abs(c['on_short'] - 2.8) < 1e-9 and abs(c['off_short'] - 2.0) < 1e-9
    assert dash.command({'cmd': 'sens', 'x100': 'default'})[0] and sent[-1] == 'SENS,default'
    assert json.load(open(path))['sens'] is None

    # A device that answers now and then (long gaps) isn't the router scanning
    import numpy as np
    sd = State(60)
    amp = ' '.join(['0 30'] * 64)
    for i in range(400):                             # router: 100/s, steady
        sd.feed(f'CSI,{1_000_000 + i * 10_000},0,1,6,-50,0,0,128,{amp}\n')
        if i % 50 == 0:                              # device: one ACK every 0.5 s
            sd.feed(f'CSI,{1_000_000 + i * 10_000 + 5000},10,1,6,-60,0,0,128,{amp}\n')
    assert not sd.scan_spans, sd.scan_spans
    gap = 4_990_000 + 400_000                        # now the router goes quiet for 0.4 s
    for i in range(100):
        sd.feed(f'CSI,{gap + i * 10_000},0,1,6,-50,0,0,128,{amp}\n')
    assert sd.scan_spans, "a gap on the router link is still a scan"

    # Per-link: a link's trace is in units of its own quiet level; a link that
    # goes silent mid-motion ends its motion; a board restart drops old link data
    sp = State(600)
    dp = Dashboard(sp)
    dp.cfg['per_link'] = True
    rng = np.random.default_rng(1)

    def csi(ts, src, wild=False):
        a = 30 + rng.normal(0, 8 if wild else 0.3, 64)
        if wild:
            a += 10 * np.sin(ts / 1e6 * 6 + np.arange(64) / 5)
        return f"CSI,{ts},{src},1,6,-50,0,0,128,{' '.join(f'0 {int(round(x))}' for x in a)}\n"
    ts = 1_000_000
    for _ in range(4000):                            # 40 s still: router and a device
        dp.feed(csi(ts, 0)); dp.feed(csi(ts + 3000, 10))
        ts += 10_000
    row = [r for r in dp.slow(30)['links'] if r['src'] == 10][0]
    assert row['state'] == 'still' and 0.3 < np.median(row['spark']['v']) < 2.8, row
    for _ in range(800):                             # the device link sees motion...
        dp.feed(csi(ts, 0)); dp.feed(csi(ts + 3000, 10, wild=True))
        ts += 10_000
    assert dp.links[10]['md'].state() == 'motion'
    for _ in range(500):                             # ...then stops answering
        dp.feed(csi(ts, 0))
        ts += 10_000
    row = [r for r in dp.slow(30)['links'] if r['src'] == 10][0]
    assert row['state'] == 'still' and row['activity'] == 0, row
    a, b = dp.links[10]['md'].spans[-1]
    assert b is not None and b - a < 8 + 2.5, (a, b)
    dp.feed(csi(1_000_000, 0))                       # board restarted
    assert list(dp.links) == [0] and not dp.links[0]['md'].spans

    # Live clock: a stale first line (from the board's USB buffer) put the
    # mapping ahead; the first fresh line pulls it back. Files never correct.
    st = State(60)
    st.live, now = True, [1000.0]
    st.clock = lambda: now[0]
    st.feed('STATS,100000000,1,0,1,0,0,0\n')        # board time 100 s, but from 17 min ago
    st.feed('STATS,1120000000,1,0,1,0,0,0\n')       # board time 1120 s: fresh
    assert abs(st.wall() - now[0]) < 0.01, st.wall() - now[0]
    st2 = State(60)
    st2.clock = lambda: 1000.0
    st2.feed('STATS,100000000,1,0,1,0,0,0\n')
    st2.feed('STATS,1120000000,1,0,1,0,0,0\n')      # a file: read fast, never corrected
    assert abs(st2.wall() - (1000.0 + 1020)) < 0.01

    # HTTP: pages, one-off snapshot, and the safety checks
    srv = serve(dash, '127.0.0.1', 0)
    port = srv.server_address[1]
    try:
        st, body = request(port, 'GET', '/')
        assert st == 200 and b'ackwave' in body
        st, body = request(port, 'GET', '/snapshot?win=120')
        assert st == 200 and set(json.loads(body)) == {'fast', 'slow'}
        st, _ = request(port, 'GET', '/', headers={'Host': 'evil.example:80'})
        assert st == 403, "DNS-rebinding host refused"
        ok = {'X-Ackwave': '1', 'Content-Type': 'application/json'}
        st, _ = request(port, 'POST', '/cmd', '{"cmd":"cal"}', {'Content-Type': 'application/json'})
        assert st == 403, "POST without the dashboard header refused"
        st, _ = request(port, 'POST', '/cmd', '{"cmd":"cal"}', dict(ok, Origin='http://evil.example'))
        assert st == 403, "cross-site POST refused"
        n = len(sent)
        st, body = request(port, 'POST', '/cmd', '{"cmd":"cal"}', ok)
        assert st == 200 and json.loads(body)['ok'] and len(sent) == n + 1
        st, _ = request(port, 'POST', '/cmd', '[1,2]', ok)
        assert st == 400
        st, _ = request(port, 'GET', '/../web.py')
        assert st == 404, "only the three static files are served"
    finally:
        srv.shutdown()
    print("WEB TESTS PASSED")


if __name__ == '__main__':
    main()
