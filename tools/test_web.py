#!/usr/bin/env python3
"""Web dashboard: JSON payloads from demo data, commands, per-link mode, and
the HTTP server's safety checks (host, cross-site POSTs)."""
import http.client
import json
import os
import tempfile
import time

from fake_csi import generate
from gui import State
from web import Dashboard, serve


def request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=20)   # a busy PC (the dashboard, a browser) can be slow: no false failures
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
    assert 'links' not in sl and dict(sl['settings'], hub=None) == {'per_link': False, 'sound': None, 'sens': None, 'alerts': [], 'router_names': {}, 'board_names': {}, 'act_place': 0, 'hub': None, 'paired': [], 'house_rule': 'confirm', 'house_off': []}
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
    # The board's LED: validated; the board's report is what the page shows
    assert dash.command({'cmd': 'led', 'mode': 'motion', 'bright': 200, 'pattern': 'pulse', 'color': '#00FF80'})[0]
    assert sent[-1] == 'LEDSET,motion,200,pulse,00ff80'
    for bad in (dict(mode='disco', bright=40, pattern='steady', color='#ff0000'),
                dict(mode='off', bright=0, pattern='steady', color='#ff0000'),
                dict(mode='off', bright=256, pattern='steady', color='#ff0000'),
                dict(mode='off', bright=True, pattern='steady', color='#ff0000'),
                dict(mode='off', bright='40', pattern='steady', color='#ff0000'),
                dict(mode='off', bright=40, pattern='wiggle', color='#ff0000'),
                dict(mode='off', bright=40, pattern='steady', color='red'),
                dict(mode='off', bright=40, pattern='steady', color='#ff0000\nCAL'), dict(mode='off')):
        assert not dash.command(dict(cmd='led', **bad))[0], bad
    assert sent[-1] == 'LEDSET,motion,200,pulse,00ff80', "nothing sent for a bad setting"
    assert dash.command({'cmd': 'led', 'mode': 'default'})[0] and sent[-1] == 'LEDSET,default'
    assert dash.slow(30)['led'] is None
    dash.feed('LEDSET,off,25,blink,0000ff\n')
    assert dash.slow(30)['led'] == dict(mode='off', bright=25, pattern='blink', color='#0000ff')
    dash.feed('LEDSET,error,not-saved\n')              # not a report: kept as it was
    assert dash.slow(30)['led']['mode'] == 'off'
    assert not Dashboard(State(60)).command({'cmd': 'led', 'mode': 'default'})[0], "needs a board"

    # Access points chosen one by one (APS): validated, any network names
    mac = lambda last: ':'.join(f'{x:02x}' for x in (0x50, 2, 0x91, 0x1a, 0x2b, last))
    assert dash.command({'cmd': 'aps', 'macs': [mac(1), mac(2).upper()]})[0]
    assert sent[-1] == f'APS,{mac(1)},{mac(2)}'
    for bad in ([], [mac(i) for i in range(9)], [mac(1), mac(1)], ['51' + mac(1)[2:]],
                [mac(1) + '\nCAL'], ['nope'], mac(1), [1]):
        assert not dash.command({'cmd': 'aps', 'macs': bad})[0], bad
    assert sent[-1] == f'APS,{mac(1)},{mac(2)}', "nothing sent for a bad list"
    assert dash.command({'cmd': 'aps_clear'})[0] and sent[-1] == 'APS,clear'
    dash.feed(f'APS,2,{mac(1)},{mac(2)}\n')
    assert dash.slow(30)['network']['ap_list'] == [mac(1), mac(2)]
    dash.feed('APS,0\n')
    assert dash.slow(30)['network']['ap_list'] == []
    dash.feed('APS,3,junk\n')                        # malformed: ignored
    assert dash.slow(30)['network']['ap_list'] == []
    assert not dash.command({'cmd': 'mode', 'mode': 'bogus'})[0]
    assert not dash.command({'cmd': 'toggle', 'mac': 'nope'})[0]
    assert not dash.command({'cmd': 'rm -rf'})[0]

    # Light on motion: validated, saved (private file), kept secret from the page,
    # and driven by the verdict the badge shows
    lpath, calls = os.path.join(tmp, 'light.json'), []
    dl = Dashboard(State(600), settings=lpath, light_call=lambda u: (calls.append(u), (True, 'HTTP 200'))[1],
                   light_run=lambda f: f())
    on, off = 'https://example.com/on?token=SECRET', 'https://example.com/off?token=SECRET'
    assert not dl.command({'cmd': 'light', 'enabled': True})[0], "needs an on URL first"
    for bad in ({'on_url': 'file:///etc/passwd'}, {'on_url': 'https://x/\nCAL'}, {'off_after': -1},
                {'off_after': [5]}, {'on_url': 7}):
        assert not dl.command(dict(cmd='light', **bad))[0], bad
    assert dl.command({'cmd': 'light', 'enabled': True, 'on_url': on, 'off_url': off, 'off_after': 30})[0]
    assert json.load(open(lpath))['light'] == {'enabled': True, 'on_url': on, 'off_url': off, 'off_after': 30}
    assert os.stat(lpath).st_mode & 0o077 == 0, "only the owner can read the tokens"
    os.chmod(lpath, 0o644)                           # an older, readable file is tightened too
    assert dl.command({'cmd': 'light', 'off_after': 30})[0] and os.stat(lpath).st_mode & 0o077 == 0
    assert 'SECRET' not in json.dumps(dl.fast(30)) + json.dumps(dl.slow(30))
    assert dl.fast(30)['header']['light']['on_host'] == 'example.com'
    assert not dl.command({'cmd': 'light_test', 'what': 'dim'})[0]
    assert dl.command({'cmd': 'light_test', 'what': 'off'})[0] and calls == [off]
    calls.clear()
    for line in lines:                               # the demo has motion in it...
        dl.feed(line)
    dl.tick()
    assert calls == [], "...but without a board (demo, file) the light is never switched"
    assert dl.fast(30)['header']['light']['live'] is False
    live = Dashboard(State(600), send=[].append, settings=lpath,
                     light_call=lambda u: (calls.append(u), (True, 'HTTP 200'))[1], light_run=lambda f: f())
    for line in lines:                               # a live board: switched on motion
        live.feed(line)
    assert calls and calls[0] == on, calls
    assert all(a != b for a, b in zip(calls, calls[1:])), "on and off alternate: edges only"
    assert live.fast(30)['header']['light']['live'] is True
    assert Dashboard(State(60), settings=lpath).light.cfg['on_url'] == on, "kept across restarts"
    assert dl.command({'cmd': 'light', 'enabled': False, 'on_url': '', 'off_url': ''})[0]
    assert json.load(open(lpath))['light']['on_url'] == ''

    # Per-link mode: off by default; once on, each ACK link gets its own analysis
    assert dash.command({'cmd': 'settings', 'per_link': True})[0]
    saved = json.load(open(os.path.join(tmp, 'dashboard.json')))
    assert len(saved.pop('board_secret') or '0' * 64) == 64, "the boards' secret, if a board was seen"
    assert dict(saved, hub=None) == {
        'per_link': True, 'sound': None, 'sens': None, 'alerts': [], 'router_names': {}, 'board_names': {}, 'act_place': 0, 'hub': None, 'paired': [], 'house_rule': 'confirm', 'house_off': [],
        'light': {'enabled': False, 'on_url': '', 'off_url': '', 'off_after': 120}}
    for line in lines[half:]:
        dash.feed(line)
    sl = dash.slow(30)
    links = sl['links']
    assert len(links) >= 2 and all(r['activity'] >= 0 and r['spark']['t'] for r in links), links
    # each link has its own verdict; the demo's motion shows on some link, and
    # the board's own verdict comes along for comparison
    assert all(r['state'] in ('motion', 'still', 'calibrating', 'weak', 'silent') for r in links)
    assert any(r['state'] in ('motion', 'still') for r in links), links
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
    for i in range(800):                             # the device link sees motion...
        dp.feed(csi(ts, 0)); dp.feed(csi(ts + 3000, 10, wild=True))
        if i % 25 == 0:                              # ...and one that rarely answers changes wildly
            dp.feed(csi(ts + 6000, 11, wild=True))
        ts += 10_000
    assert dp.links[10]['md'].state(ts) == 'motion'
    # 4 ACKs a second is too few to judge: no verdict, however wild
    assert dp.links[11]['md'].state(ts) == 'weak' and not dp.links[11]['md'].spans
    row = [r for r in dp.slow(30)['links'] if r['src'] == 11][0]
    assert row['state'] == 'weak' and not row['moving'] and not row['spans'] and row['weak'], row
    # where the motion is: the badge stays the board's verdict and names the link
    badge = dp.fast(30)['header']['badge']
    assert badge['word'] == 'STILL' and 'only the device 10 link sees motion now' in badge['sub'], badge
    for _ in range(1000):                            # ...then it is quiet there again
        dp.feed(csi(ts, 0)); dp.feed(csi(ts + 3000, 10))
        ts += 10_000
    assert dp.links[10]['md'].state(ts) == 'still'
    rec = dp.fast(30)['header']['recent']            # a period only a device link saw is listed as such
    assert [r for r in rec if r['only'] and r['where'] == 'device 10 only' and not r['going']], rec
    assert all(e[2] == 10 and e[1] is not None for e in dp.link_events), dp.link_events
    for _ in range(300):                             # motion on both links
        dp.feed(csi(ts, 0, wild=True)); dp.feed(csi(ts + 3000, 10, wild=True))
        ts += 10_000
    h = dp.fast(30)['header']
    assert h['badge']['word'] == 'MOTION' and h['badge']['sub'].endswith('seen by router + device 10'), h['badge']
    assert h['recent'][0]['where'] == 'router + device 10' and not h['recent'][0]['only'], h['recent']
    dp.cfg['per_link'] = False                       # without per-link analysis: as before
    h = dp.fast(30)['header']
    assert all(r['where'] == '' and not r['only'] for r in h['recent']) and 'seen by' not in h['badge']['sub']
    dp.cfg['per_link'] = True
    for _ in range(500):                             # the device stops answering mid-motion
        dp.feed(csi(ts, 0))
        ts += 10_000
    row = [r for r in dp.slow(30)['links'] if r['src'] == 10][0]
    assert row['state'] == 'silent' and row['activity'] == 0 and row['weak'], row
    a, b = dp.links[10]['md'].spans[-1]
    assert b is not None and b - a < 3 + 2.5, (a, b)
    dp.feed(csi(1_000_000, 0))                       # board restarted
    assert list(dp.links) == [0] and not dp.links[0]['md'].spans
    assert all(e[1] is not None for e in dp.link_events), "a restart ends the open link periods"

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
        assert st == 200 and set(json.loads(body)) == {'fast', 'slow', 'debug'}
        # Data endpoints: the dashboard header and an allowed Host, like commands
        st, _ = request(port, 'GET', '/api/stats')
        assert st == 403, "stats without the dashboard header refused"
        st, _ = request(port, 'GET', '/api/stats', headers={'X-Ackwave': '1', 'Host': 'evil.example'})
        assert st == 403, "stats for a foreign Host refused"
        st, body = request(port, 'GET', '/api/stats', headers={'X-Ackwave': '1'})
        got = json.loads(body)
        assert st == 200 and {'sessions', 'by_hour', 'heatmap', 'durations', 'today'} <= set(got)
        assert len(got['by_hour']) == 24 and got['sessions'][0]['live']
        # The Map page: its script, and the layout saved on the PC (/api/map)
        st, body = request(port, 'GET', '/map.js')
        assert st == 200 and b'window.AckMap' in body
        hdr = {'X-Ackwave': '1', 'Content-Type': 'application/json'}
        st, body = request(port, 'GET', '/api/map', headers={'X-Ackwave': '1'})
        assert st == 200 and json.loads(body) == {}, "no map yet: empty"
        mac = ':'.join(f'{x:02x}' for x in (0x50, 2, 0x91, 1, 2, 3))
        layout = {'unit': 'ft', 'env': 'home', 'view': {'x': 1.5, 'y': -2, 's': 30},
                  'nodes': [{'key': 'ap:0', 'name': 'router', 'x': 6.1, 'y': 0.4, 'placed': True, 'junk': 1},
                            {'key': mac, 'name': 'Amazon', 'x': -2, 'y': 3.5, 'eirp': 16}],
                  'img': 'data:image/png;base64,AAAA', 'extra': {'a': 1}}
        st, body = request(port, 'POST', '/api/map', json.dumps(layout), hdr)
        assert st == 200 and json.loads(body)['ok'], body
        st, body = request(port, 'GET', '/api/map', headers={'X-Ackwave': '1'})
        got = json.loads(body)
        assert got['nodes'][0] == {'key': 'ap:0', 'name': 'router', 'x': 6.1, 'y': 0.4, 'placed': True}, got
        assert got['nodes'][1]['eirp'] == 16 and got['view'] == {'x': 1.5, 'y': -2, 's': 30}
        assert 'extra' not in got and 'img' not in got and 'junk' not in got['nodes'][0], "unknown keys dropped"
        mpath = os.path.join(os.path.dirname(dash.settings_path), 'map.json')
        assert os.path.isfile(mpath) and not os.path.exists(mpath + '.tmp'), "written atomically"
        st, _ = request(port, 'GET', '/api/map')
        assert st == 403, "map without the dashboard header refused"
        st, _ = request(port, 'POST', '/api/map', json.dumps(layout), {'Content-Type': 'application/json'})
        assert st == 403, "saving without the dashboard header refused"
        st, _ = request(port, 'POST', '/api/map', json.dumps(layout), dict(hdr, Host='evil.example'))
        assert st == 403, "saving for a foreign Host refused"
        st, _ = request(port, 'POST', '/api/map', json.dumps(layout), dict(hdr, Origin='http://evil.example'))
        assert st == 403, "cross-site save refused"
        big = dict(layout, pad='x' * (300 * 1024))
        st, _ = request(port, 'POST', '/api/map', json.dumps(big), hdr)
        assert st == 413, "over the size cap"
        for bad in ({'nodes': [{'key': 'ap:0', 'x': float('nan'), 'y': 0}]}, {'nodes': [{'key': 'ap:0', 'x': True, 'y': 0}]},
                    {'nodes': [{'key': 'ap:0', 'x': 1e9, 'y': 0}]}, {'nodes': [{'key': 'a\nb', 'x': 0, 'y': 0}]},
                    {'nodes': [{'x': 0, 'y': 0}]}, {'nodes': 'x'}, {'unit': 'cubit'}, {'view': {'x': 0, 'y': 0, 's': 0}},
                    {'nodes': [{'key': str(i), 'x': 0, 'y': 0} for i in range(65)]}, [1, 2]):
            st, _ = request(port, 'POST', '/api/map', json.dumps(bad), hdr)
            assert st == 400, bad
        st, body = request(port, 'GET', '/api/map', headers={'X-Ackwave': '1'})
        assert json.loads(body) == got, "a refused save changes nothing"
        # Pictures under the map (satellite, floor plans): raw bytes per slot, checked by content
        png = b'\x89PNG\r\n\x1a\n' + b'\0' * 64
        st, _ = request(port, 'GET', '/api/plan?id=p1', headers={'X-Ackwave': '1'})
        assert st == 404, "no picture yet"
        st, body = request(port, 'POST', '/api/plan?id=p1', png, {'X-Ackwave': '1'})
        assert st == 200 and json.loads(body)['ok'], body
        st, body = request(port, 'GET', '/api/plan?id=p1', headers={'X-Ackwave': '1'})
        assert st == 200 and body == png
        for data, why in ((b'<svg onload=alert(1)>', 'not a picture'), (b'GIF89a' + b'\0' * 20, 'not a PNG/JPEG/WebP')):
            st, _ = request(port, 'POST', '/api/plan?id=p2', data, {'X-Ackwave': '1'})
            assert st == 400, why
        st, _ = request(port, 'POST', '/api/plan?id=../x', png, {'X-Ackwave': '1'})
        assert st == 400, "only the four slots"
        st, _ = request(port, 'POST', '/api/plan?id=p2', png, {})
        assert st == 403, "upload without the dashboard header refused"
        st, _ = request(port, 'GET', '/api/plan?id=p1')
        assert st == 403, "picture without the dashboard header refused"
        ok, msg = dash.plan_save('p3', b'\xff\xd8\xff' + b'\0' * (12 * 1024 * 1024))
        assert not ok and 'too big' in msg
        layer = {'id': 'p1', 'name': 'satellite', 'x': -20, 'y': -15.5, 'w': 60, 'rot': 0.1, 'opacity': 0.8}
        st, body = request(port, 'POST', '/api/map', json.dumps(dict(got, plans=[layer])), hdr)
        assert st == 200, body
        st, body = request(port, 'GET', '/api/map', headers={'X-Ackwave': '1'})
        assert json.loads(body)['plans'] == [dict(layer, hidden=False)]
        for bad in ([dict(layer, id='p9')], [layer, layer], [dict(layer, w=0)], [dict(layer, opacity=2)], 'x'):
            st, _ = request(port, 'POST', '/api/map', json.dumps(dict(got, plans=bad)), hdr)
            assert st == 400, bad
        st, body = request(port, 'POST', '/api/plan?id=p1', b'', {'X-Ackwave': '1'})
        assert st == 200 and json.loads(body)['ok']
        st, _ = request(port, 'GET', '/api/plan?id=p1', headers={'X-Ackwave': '1'})
        assert st == 404, "removed"
        st, _ = request(port, 'GET', '/api/nope', headers={'X-Ackwave': '1'})
        assert st == 404
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
    pages_and_debug(tmp)
    ingress_mode()
    device_alerts(tmp)
    failed_send(tmp)
    print("WEB TESTS PASSED")


def ingress_mode():
    """As a Home Assistant add-on: only HA's ingress proxy is answered, with
    whatever Host it sends; the page uses relative addresses (it is served
    under /api/hassio_ingress/<token>/)."""
    import re
    from web import serve
    d = Dashboard(State(60))
    srv = serve(d, port=0, only_from='127.0.0.1')          # this test plays the proxy
    port = srv.server_address[1]
    st, _ = request(port, 'GET', '/', headers={'Host': 'homeassistant.local:8123'})
    assert st == 200, st
    st, body = request(port, 'POST', '/cmd', body=b'{"cmd":"cal"}',
                       headers={'Host': 'homeassistant.local:8123', 'X-Ackwave': '1',
                                'Content-Type': 'application/json'})
    assert st in (200, 400) and b'host not allowed' not in body, body
    srv.shutdown()
    srv2 = serve(d, port=0, only_from='172.30.32.2')       # anyone else: refused
    st, _ = request(srv2.server_address[1], 'GET', '/', headers={'Host': 'localhost'})
    assert st == 403, st
    srv2.shutdown()
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
    for name in ('app.js', 'map.js', 'index.html'):
        with open(os.path.join(web_dir, name)) as f:
            text = f.read()
        bad = re.findall(r"""(?:fetch|EventSource|get)\(\s*['"`]/|(?:href|src)="/""", text)
        assert not bad, (name, bad)


def failed_send(tmp):
    """A command that can't be sent is reported and dropped: it never stays in
    the outbox to fail again on every line and block every later command. A
    non-ASCII network name goes out as UTF-8 (the board takes bytes)."""
    wire, fail = [], {'on': True}

    def send(text):
        if fail['on'] and text.startswith('USE,'):
            raise OSError('port gone')
        wire.append(text.encode('utf-8') + b'\n')

    s = State(600)
    dash = Dashboard(s, send=send, settings=os.path.join(tmp, 'd2.json'))
    dash.tick()
    ok, _ = dash.command({'cmd': 'use', 'name': 'Café ☕'})
    assert ok and not s.outbox, "the failed command was dropped"
    assert any('not sent' in r[1] for r in dash.refused), "and reported on the Debug page"
    dash.tick()                                   # no exception: nothing left to fail
    ok, _ = dash.command({'cmd': 'cal'})
    assert ok and wire[-1] == b'CAL\n', "later commands still go out"
    fail['on'] = False
    ok, _ = dash.command({'cmd': 'use', 'name': 'Café ☕'})
    assert ok and wire[-1] == 'USE,Café ☕\n'.encode('utf-8')


def _mac(*b):
    """An address built from bytes (no address literals in the repo)."""
    return ':'.join(f'{x:02x}' for x in b)


AP0, AP1, AP2 = (_mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf0 + i) for i in range(3))   # the demo's routers
DEV_A, DEV_B = _mac(0x11, 0x22, 0x33, 0x44, 0x55, 0x66), _mac(0x11, 0x22, 0x33, 0x44, 0x55, 0x77)


def routers_named(d, tmp):
    """Routers have names everywhere ('Router 1', or the nickname you gave
    its BSSID), and each device says which router it talks through."""
    sl = d.slow(30)
    rs = {r['idx']: r for r in sl['routers']}
    assert set(rs) >= {0, 1} and rs[0]['bssid'] == AP0 and rs[0]['name'] == 'Router 1'
    assert rs[0]['rate'] > 0 and rs[0]['state'] == 'ok' and rs[1]['state'] == 'far'
    d.feed(f'DEV,{DEV_A},-58,40,10,300,1\n')       # a device on router 2
    d.feed(f'DEV,{DEV_B},-61,40,10,300\n')         # an older board: router unknown
    rows = {r['mac']: r for r in d.slow(30)['devices']['rows']}
    assert rows[DEV_A]['router'] == 'Router 2' and rows[DEV_B]['router'] == ''
    on = {r['idx']: [x['mac'] for x in r['devices']] for r in d.slow(30)['routers']}
    assert DEV_A in on[1] and DEV_A not in on[0]
    assert all(DEV_B not in v for v in on.values()), "router unknown: listed under none"
    path = os.path.join(tmp, 'names.json')
    d.settings_path = path
    ok, msg = d.command({'cmd': 'router_name', 'bssid': AP1.upper(), 'name': '  Living   room '})
    assert ok and 'Living room' in msg, msg
    assert d.s.devices.link_label(1) == 'Living room'
    rows = {r['mac']: r for r in d.slow(30)['devices']['rows']}
    assert rows[DEV_A]['router'] == 'Living room'
    with open(path) as f:
        assert json.load(f)['router_names'] == {AP1: 'Living room'}
    for bad in ({'bssid': 'nope', 'name': 'x'}, {'bssid': AP1, 'name': 'x' * 25},
                {'bssid': AP1, 'name': 'a\x01b'}, {'bssid': AP1, 'name': 3}):
        assert not d.command(dict(bad, cmd='router_name'))[0], bad
    # a fresh dashboard loads the names (and drops bad ones)
    with open(path, 'w') as f:
        json.dump({'router_names': {AP1: 'Living room', 'junk': 'x', AP2: ''}}, f)
    d2 = Dashboard(State(60), settings=path)
    assert d2.cfg['router_names'] == {AP1: 'Living room'}
    assert d.command({'cmd': 'router_name', 'bssid': AP1, 'name': ''})[0]
    assert d.s.devices.link_label(1) == 'Router 2', "an empty name removes the nickname"


def other_boards(tmp):
    """Other boards' verdicts (SAT lines, from their hellos): listed with a
    name you can change, each hello counted once, motion filed in the
    timeline, and a board that goes quiet shown as gone."""
    from timeline import Timeline
    B = _mac(0x24, 0x0a, 0xc4, 0x12, 0x34, 0x99)
    path = os.path.join(tmp, 'boards.json')
    d = Dashboard(State(60), settings=path)
    d.timeline = Timeline(tmp)
    assert d.slow(30)['boards'] == []
    d.feed(f'SAT,{B},-41,7,2,20,15,96,-57,2,3600\n')           # calibrated, still
    b = d.slow(30)['boards'][0]
    assert b['mac'] == B and b['state'] == 'still' and b['name'] == 'Board 34:99', b
    assert b['level'] == 0.4 and b['acks'] == 96 and b['router_rssi'] == -57 and b['links'] == 2
    d.feed(f'SAT,{B},-41,8,3,80,40,96,-57,2,3601\n')           # motion
    d.feed(f'SAT,{B},-41,8,3,80,40,96,-57,2,3601\n')           # the same hello again: ignored
    assert d.slow(30)['boards'][0]['state'] == 'motion' and 'sat:' + B in d._open
    d.feed(f'SAT,{B},-41,9,2,10,10,96,-57,2,3602\n')           # still again: a period filed
    got = [p for p in d.timeline.read(time.time() - 60, time.time() + 1) if p[2] == 'sat:' + B]
    assert len(got) == 1 and got[0][3] == 'Board 34:99', got
    lanes = [l['key'] for l in d.timeline_range(1)['lanes']]
    assert [k for k in lanes if k != 'house'][:2] == ['board', 'sat:' + B], lanes
    d.feed(f'SAT,{B},-41,10,0,0,0,0,-127,0,5\n')               # restarted: calibrating
    assert d.slow(30)['boards'][0]['state'] == 'calibrating'
    ok, msg = d.command({'cmd': 'board_name', 'mac': B.upper(), 'name': ' Kitchen '})
    assert ok and d.slow(30)['boards'][0]['name'] == 'Kitchen', msg
    with open(path) as f:
        assert json.load(f)['board_names'] == {B: 'Kitchen'}
    assert Dashboard(State(60), settings=path).cfg['board_names'] == {B: 'Kitchen'}
    for bad in ({'mac': 'nope', 'name': 'x'}, {'mac': B, 'name': 'x' * 25}, {'mac': B, 'name': None}):
        assert not d.command(dict(bad, cmd='board_name'))[0], bad
    d.feed(f'PEER,{B},-41,30,200,6,1\n')                       # the same board in Devices: your name
    assert any(r['name'] == 'Kitchen' for r in d.slow(30)['devices']['rows'])
    d.feed(f'SAT,{B},-41,11,3,80,40,96,-57,2,3700\n')
    d.sats[B]['at'] -= 60                                       # not heard for a minute
    d.tick()
    assert d.slow(30)['boards'][0]['state'] == 'gone' and 'sat:' + B not in d._open
    for junk in ('SAT,nope', f'SAT,{B},x,1,2,3,4,5,6,7,8', f'SAT,{B},-41,1,2'):
        d.feed(junk + '\n')                                    # never an error
    assert len(d.sats) == 1


def device_alerts(tmp):
    """Device links you tick drive the alerts (light, and the board's LED and
    buzzer via EXT) once their slow window confirms; the board's verdict is
    never touched."""
    import numpy as np
    rng = np.random.default_rng(7)

    def csi(ts, src, wild=False, kick=0.0):
        a = 30 + rng.normal(0, 8 if wild else 0.3, 64) + kick
        if wild:
            a += 10 * np.sin(ts / 1e6 * 6 + np.arange(64) / 5)
        return f"CSI,{ts},{src},1,6,-50,0,0,128,{' '.join(f'0 {int(round(x))}' for x in a)}\n"
    mac = ':'.join(f'{x:02x}' for x in (0x50, 2, 0x91, 0x1a, 0x2b, 0x3c))
    other = ':'.join(f'{x:02x}' for x in (0x50, 2, 0x91, 0x1a, 0x2b, 0x3d))

    def make(send=True):
        sent, calls = [], []
        s = State(600)
        d = Dashboard(s, send=sent.append if send else None, settings=os.path.join(tmp, 'alerts.json'),
                      light_call=lambda u: (calls.append(u), (True, 'HTTP 200'))[1], light_run=lambda f: f())
        d.cfg['per_link'] = True
        s.devices.poking[mac] = 10
        d.light.set(enabled=True, on_url='https://example.com/on', off_url='https://example.com/off', off_after=0)
        return d, sent, calls

    def run(d, seconds, wild_dev=False, spike=False, ts=[1_000_000]):
        for i in range(int(seconds * 100)):
            t = ts[0]
            d.feed(csi(t, 0))                        # the router (the board's verdict): quiet
            kick = 25.0 if spike and i < 3 else 0.0  # a lone 30 ms jolt on the device link
            d.feed(csi(t + 3000, 10, wild=wild_dev, kick=kick))
            ts[0] += 10_000

    # validation, saved, and reloaded
    d, sent, calls = make()
    for bad in ('x', [1], ['nope'], [mac] * 9, [mac + '\nCAL']):
        assert not d.command({'cmd': 'alerts', 'keys': bad})[0], bad
    ok, msg = d.command({'cmd': 'alerts', 'keys': [mac.upper()]})
    assert ok and d.cfg['alerts'] == [mac] and 'plus 1 device link' in msg, msg
    assert json.load(open(os.path.join(tmp, 'alerts.json')))['alerts'] == [mac]
    assert Dashboard(State(60), settings=os.path.join(tmp, 'alerts.json')).cfg['alerts'] == [mac]

    run(d, 40)                                       # both quiet: nothing
    assert 'EXT,1' not in sent and calls == []
    run(d, 0.5, spike=True)                          # a lone spike on the device link: nothing
    run(d, 3)
    assert 'EXT,1' not in sent and calls == [], "a spike alone doesn't set the alerts off"
    run(d, 10, wild_dev=True)                        # steady motion on the ticked device link
    assert 'EXT,1' in sent and calls[:1] == ['https://example.com/on'], (sent[-3:], calls)
    assert not d.s.board_motion and not d.s.det.motion, "the board's own verdict is untouched"
    sl = d.slow(30)
    row = [r for r in sl['radio'] if r['key'] == mac][0]
    assert row['alerts'] and row['alerting'] and d.fast(30)['header']['alerting'] == [d.s.devices.link_label(10)]
    n = sent.count('EXT,1')
    d.ext_at -= 2                                    # a second later: told again (the board holds it 5 s)
    d.tick()
    assert sent.count('EXT,1') == n + 1
    run(d, 20)                                       # quiet again
    assert sent[-1] == 'EXT,0' or 'EXT,0' in sent, sent[-5:]
    assert calls[-1] == 'https://example.com/off'

    # an unticked device link sees the same motion: no alerts
    d2, sent2, calls2 = make()
    d2.command({'cmd': 'alerts', 'keys': [other]})
    run(d2, 40); run(d2, 10, wild_dev=True)
    assert 'EXT,1' not in sent2 and calls2 == []
    # no board (a demo or a file): never
    d3, sent3, calls3 = make(send=False)
    d3.cfg['alerts'] = [mac]
    run(d3, 40); run(d3, 10, wild_dev=True)
    assert sent3 == [] and calls3 == [] and d3.fast(30)['header']['alerting'] == []


def pages_and_debug(tmp):
    """The multi-page front end and the data behind its new pages."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
    js = open(os.path.join(here, 'app.js')).read()
    mapjs = open(os.path.join(here, 'map.js')).read()
    html = open(os.path.join(here, 'index.html')).read()
    # Names from the network (devices, networks, labels) only ever go in as text
    for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval('):
        assert bad not in js and bad not in mapjs, bad
    routes = ['home', 'activity', 'graphs', 'stats', 'links', 'map', 'debug', 'settings']
    for r in routes:
        assert f'data-page="{r}"' in html and f'href="#/{r}"' in html, r
    assert "const ROUTES = ['home', 'activity', 'graphs', 'stats', 'links', 'map', 'debug', 'settings'];" in js
    assert '<script src="map.js"></script>' in html and 'localStorage' not in mapjs, "the layout lives on the PC"
    assert 'data-page="train"' not in html and 'data-page="ai"' not in html, "no placeholder pages"
    assert js.count('new EventSource(') == 1, "one live connection for every page"
    assert "'X-Ackwave': '1'" in js and "fetch('api/stats'" in js

    # Debug, gating, thresholds, today: from a feed with a refused command
    sent = []
    s = State(600)
    d = Dashboard(s, send=sent.append, sessions_dir=tmp)
    for line, *_ in generate(seed=3):
        d.feed(line)
    d.feed('GATE,0,-49,98,2900,ok\n')
    d.feed('GATE,1,-82,-1,0,far\n')
    assert d.command({'cmd': 'cal'})[0]
    d.feed('REFUSED,bad-command\n')
    dbg = d.debug(30)
    assert dbg['refused'][-1][1] == 'REFUSED,bad-command' and dbg['refused'][-1][2] == 'CAL', dbg['refused']
    assert dbg['sent'][-1][1] == 'CAL'
    assert dbg['traces'] and all(len(t['t']) == len(t['jump']) == len(t['gap']) <= 602 for t in dbg['traces'])
    assert all(j >= 0 for t in dbg['traces'] for j in t['jump']) and dbg['glitch'] == 0.4
    assert [c['phase'] for c in dbg['calibs']][:2] == ['wait', 'collect'], dbg['calibs']
    assert all(not line.startswith('CSI,') for _t, line in dbg['tail']) and len(dbg['tail']) <= 250
    assert len(json.dumps(dbg)) < 120_000
    sl = d.slow(30)
    # The live radio list (always on): one entry per live ACK link, for the Map page
    assert sl['radio'] and sl['channel'] in range(1, 14)
    for r in sl['radio']:
        assert {'key', 'label', 'ap', 'rssi', 'rate'} <= set(r) and r['rate'] > 0, r
    assert sl['radio'][0]['key'] == 'ap:0' and sl['radio'][0]['label'] == 'Router 1'
    assert [(g['node'], g['state']) for g in sl['gates']] == [(0, 'ok'), (1, 'far')]
    routers_named(d, tmp)
    other_boards(tmp)
    assert sl['thresholds']['on_short'] == 2.8 and sl['thresholds']['short_gate'] == 1.2
    assert set(sl['today']) == {'periods', 'blips', 'motion_s'}
    import web as webmod
    assert sl['tz'] == webmod.PC_TZ, "pages show the PC's time zone"
    os.environ['TZ'] = 'America/Chicago'
    assert webmod.pc_timezone() == 'America/Chicago'
    os.environ['TZ'] = 'x;alert(1)'
    assert webmod.pc_timezone() != 'x;alert(1)', "a strange TZ is not passed on"
    del os.environ['TZ']
    st = d.stats()
    assert st['sessions'][0]['live'] and st['sessions'][0]['periods'] == d.s.session_counts()[0]


if __name__ == '__main__':
    main()
