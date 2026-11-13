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

    # Sensitivity: the board's long threshold, kept applied; the PC detector follows it
    s.clock = lambda: 2e9
    dash.feed('SENS,140\n')
    assert dash.slow(30)['sens'] == 140 and s.det.c['on_long'] == 1.4
    assert dash.command({'cmd': 'sens', 'x100': 160})[0] and sent[-1] == 'SENS,160'
    assert json.load(open(path))['sens'] == 160
    for bad in (109, 301, 'x', None, '160\nCAL'):
        assert not dash.command({'cmd': 'sens', 'x100': bad})[0], bad
    dash.feed('SENS,160\n')
    assert abs(s.det.c['on_long'] - 1.6) < 1e-9 and abs(s.det.c['off_long'] - 1.6 * 1.2 / 1.4) < 1e-9
    n = len(sent)
    s.clock = lambda: 3e9                            # board restarted: back at its own value
    dash.feed('SENS,140\n')
    assert sent[n:] == ['SENS,160'], sent[n:]
    d3 = Dashboard(State(60), send=[].append, settings=path)
    assert d3.cfg['sens'] == 160 and 'SENS,160' in d3.s.outbox
    assert dash.command({'cmd': 'sens', 'x100': 'default'})[0] and sent[-1] == 'SENS,default'
    assert json.load(open(path))['sens'] is None

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
