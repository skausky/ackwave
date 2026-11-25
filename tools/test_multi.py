"""Step A of several boards (docs/redesign/multi-board.md): boards.py's clock
and tags, the dashboard routing lines from several ports and a tagged
stream, the primary chosen by saved MAC, and compare.py."""
import json
import os
import tempfile
import time

from boards import ClockMap, House, SatLink, board_ts, split_tag
from compare import compare, report
from fake_csi import OTHER_BOARDS, generate_boards
from gui import State
from timeline import Timeline
from web import Dashboard, LagQueue, pump
import queue


def mac(*b):
    return ':'.join(f'{x:02x}' for x in b)


FIRST = mac(0x24, 0x0a, 0xc4, 0x11, 0x22, 0x33)      # fake_csi's first board
TAGGED = mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0x01)


def clock():
    c = ClockMap()
    assert c.wall(5_000_000) is None
    c.feed(5_000_000, 1000.30)                  # waited 0.3 s in a buffer
    c.feed(6_000_000, 1001.01)                  # 10 ms
    assert abs(c.offset() - 995.01) < 1e-6 and abs(c.wall(7_000_000) - 1002.01) < 1e-6
    # a clock 20 ppm slow: the offset follows within the window, not stuck at the start
    c2 = ClockMap()
    for s in range(0, 3600, 2):
        c2.feed(int(s * (1 - 20e-6) * 1e6), 5000.0 + s + 0.005)
    want = 5000.0 + 3598 + 0.005 - 3598 * (1 - 20e-6)
    assert abs(c2.offset() - want) < 0.015, (c2.offset(), want)   # a plain minimum would be 72 ms off
    c2.feed(1_000_000, 9000.0)                  # restarted
    assert abs(c2.offset() - 8999.0) < 1e-6
    assert split_tag(f'@{TAGGED},STATE,5,still,0') == (TAGGED, 'STATE,5,still,0')
    assert split_tag('STATE,5') == (None, 'STATE,5') and split_tag('@nope,x') == (None, '@nope,x')
    assert board_ts('CSI,123,0,1') == 123 and board_ts('STATE,9,motion') == 9 and board_ts('DEV,x') is None
    assert board_ts('CSI,garbage,1') is None


def tagged_stream():
    """fake_csi --boards 3 through the dashboard's stdin path: the first
    board is the dashboard as always, the others are watched."""
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(600), settings=os.path.join(tmp, 'dashboard.json'), sessions_dir=tmp)
    q = queue.Queue()
    for line, _t in generate_boards(3, seed=1):
        q.put(line + '\n')
    q.put(None)
    pump(d, q)
    macs = {m for m, *_ in OTHER_BOARDS}
    assert set(d.others) == macs, set(d.others)
    assert d.s.me == FIRST, "the first board is still the dashboard's own"
    for m in macs:
        ob = d.others[m]
        assert ob.clock.offset() is not None and ob.s.det.floor > 0, "its own clock and detector"
    rows = {b['mac']: b for b in d.slow(30)['boards']}
    assert macs <= set(rows), rows
    keys = {p[2] for p in d.timeline.read(time.time() - 600, time.time() + 600)} | set(d._open)
    assert {'sat:' + m for m in macs} <= keys, keys
    lanes = [l['key'] for l in d.timeline_range(1)['lanes']]
    assert lanes[:2] == ['house', 'board'] and all(k.startswith('sat:') for k in lanes[2:4]), lanes


class Log:
    def __init__(self):
        self.main, self.raw = [], []

    def feed(self, line):
        self.main.append(line)

    def feed_raw(self, line):
        self.raw.append(line)


def ports():
    """Two USB ports: the saved hub is the primary, whichever port it is on;
    the other board's lines go to its own state and the raw log, tagged."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 'dashboard.json')
    hub = OTHER_BOARDS[0][0]
    with open(path, 'w') as f:
        json.dump({'hub': hub}, f)
    sent = {0: [], 1: []}
    log = Log()
    d = Dashboard(State(600), settings=path, sessions_dir=tmp)
    d.set_sources([sent[0].append, sent[1].append], log)
    rows = list(generate_boards(2, seed=2))
    t0 = time.time()
    for line, t in rows:                         # port 0: the plain board; port 1: the hub
        mac, body = split_tag(line)
        d.feed_src(1 if mac else 0, body + '\n', now=t0 + (t or 0))
    assert d.primary == hub and d.s.me == hub, (d.primary, d.s.me)
    assert d.send is not None and set(d.others) == {FIRST}
    assert log.raw and all(r.startswith(f'@{FIRST},') for r in log.raw)
    assert log.main and not any(l.startswith('@') for l in log.main)
    # nothing but its key goes to a watched board
    assert all(c.startswith(('KEY', 'PEERKEY')) for c in sent[0]), sent[0]


def comparing():
    a = [(0, 10), (20, 21), (40, 41.5), (60, 70)]
    b = [(1, 9), (21.5, 22), (80, 90)]
    c = compare(a, b)
    assert (c['both_a'], c['only_a'], c['only_b'], c['both_b']) == (2, 2, 1, 2), c
    assert (c['a_brief'], c['a_brief_confirmed'], c['a_long'], c['a_long_confirmed']) == (2, 1, 2, 1), c
    assert compare([], b)['only_b'] == 3 and compare(a, [])['only_a'] == 4
    spans = [(0, 10, 'board', 'Board'), (1, 9, 'sat:x', 'Kitchen'), (30, 31, 'sat:x', 'Kitchen'),
             (2, 3, 'ap:0', 'Router 1')]
    txt = '\n'.join(report(spans))
    assert 'Board vs Kitchen' in txt and 'only Kitchen      1' in txt, txt
    assert 'nothing to compare' in report([(0, 1, 'board', 'Board')])[0]


def house():
    """Combining the boards' verdicts: one board is always its own verdict;
    'confirm' lets a blip through only with a second board; 'two' needs two."""
    h = House()
    h.update({'board': True}, 0.0)
    for rule in ('any', 'confirm', 'two'):
        assert h.verdict(rule, 0.1, 1) == (True, ['board']), "one board: exactly its verdict"
    h = House()
    h.update({'board': False, 'K': True}, 10.0)
    assert h.verdict('any', 10.5, 2)[0]
    assert not h.verdict('confirm', 10.5, 2)[0], "a lone blip waits"
    assert h.verdict('confirm', 12.1, 2) == (True, ['K']), "lasting 2 s: counts alone"
    assert not h.verdict('two', 12.1, 2)[0]
    h.update({'board': True, 'K': True}, 12.5)
    assert h.verdict('two', 12.6, 2)[0] and h.verdict('confirm', 12.6, 2)[1] == ['K', 'board']
    # a short one confirmed by another board that just stopped (within 1.5 s)
    h2 = House()
    h2.update({'board': True, 'K': False}, 20.0)
    h2.update({'board': False, 'K': False}, 21.0)
    h2.update({'board': False, 'K': True}, 21.5)
    assert h2.verdict('confirm', 21.6, 2)[0] and h2.verdict('two', 21.6, 2)[0]
    h2.update({'board': False, 'K': True}, 24.0)
    assert not h2.verdict('two', 24.0, 2)[0], "too long ago"
    h2.update({'board': False}, 25.0)                # K went quiet: its motion ended
    assert not h2.verdict('any', 25.0, 2)[0]
    # in the dashboard: a satellite's report drives the light, the badge and the timeline
    import tempfile
    from web import Dashboard
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(60), send=lambda c: None, settings=os.path.join(tmp, 'dashboard.json'),
                  sessions_dir=tmp)
    d.light.update = lambda on: setattr(d, '_light_on', on)
    for line, _t in generate_boards(1, seed=4):
        if not line.startswith('SAT,'):              # the demo's own satellite: not here
            d.feed(line + '\n')
    assert d.slow(30)['house']['boards'] == 1
    sat = mac(0xc4, 0x9e, 0x7e, 1, 2, 3)
    for i in range(6):                               # 3 s of motion from the satellite alone
        d.feed(f'SAT,{sat},-40,{i},3,90,70,95,-60,1,{i}\n')
        d.house.since[sat] -= 0.6                    # time passes
        d.tick()
    hs = d.slow(30)['house']
    assert hs['boards'] == 2 and hs['motion'] and hs['by'] == [f'Board {sat[-5:]}'], hs
    assert d._light_on is True, "a satellite's motion switches the light"
    b = d.fast(30)['header']['badge']
    assert b['word'] == 'MOTION' and sat[-5:] in b['head'], b
    if not d._motion_now():                          # the instant replay: the hub's motion is old
        d.house.ended.pop('board', None)
    assert d.command({'cmd': 'house_rule', 'rule': 'two'})[0] and d.cfg['house_rule'] == 'two'
    d.tick()
    assert not d.slow(30)['house']['motion'], "two boards needed now"
    assert not d.command({'cmd': 'house_rule', 'rule': 'nope'})[0]
    # the hub need not count: only the satellite drives the light then
    assert d.command({'cmd': 'house_rule', 'rule': 'any'})[0]
    ok, msg = d.command({'cmd': 'house_use', 'key': 'board', 'use': False})
    assert ok and 'no longer counts' in msg and d.cfg['house_off'] == ['board']
    d.house.since.pop('board', None)
    d.feed(f'SAT,{sat},-40,50,2,10,10,95,-60,1,50\n')      # the satellite: still
    d.tick()
    assert not d.slow(30)['house']['motion'], "the hub's motion no longer counts (the House lane; alerts: rules)"
    src = {x['key']: x['use'] for x in d.slow(30)['house']['sources']}
    assert src == {'board': False, sat: True}, src
    assert not d.command({'cmd': 'house_use', 'key': 'nope', 'use': True})[0]
    assert not d.command({'cmd': 'house_use', 'key': 'board', 'use': 'yes'})[0]
    assert d.command({'cmd': 'house_use', 'key': 'board', 'use': True})[0] and d.cfg['house_off'] == []
    d0 = Dashboard(State(60), settings=os.path.join(tmp, 'alone.json'))
    d0.cfg['house_off'] = ['board']                           # alone, the hub always counts
    d0.s.det.motion, d0.s.det.floor = True, 0.02
    d0._motion_now = lambda: True
    assert d0._house()[0], "one board: exactly its verdict"
    with open(os.path.join(tmp, 'dashboard.json')) as f:
        assert json.load(f)['house_rule'] == 'any'


def sat_links():
    """Other boards' links (SATL) judged per link against each link's own quiet
    level; the survey (SURV) kept; the map gets each link's far end."""
    from analyze import parse_line
    import tempfile
    from web import Dashboard
    R, D, S = mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1), mac(0x50, 0x02, 0x91, 1, 2, 3), mac(0xc4, 0x9e, 0x7e, 9, 9, 9)
    ev = parse_line(f'SATL,{S},{R}:3:44:30,{D}:4:60:20')
    assert ev == ('satl', dict(mac=S, links=[dict(mac=R, flags=3, ks=2.2, kl=1.5),
                                              dict(mac=D, flags=4, ks=3.0, kl=1.0)])), ev
    assert parse_line(f'SURV,{S},{R}:0:-60,{D}:2:-71')[1]['seen'][1] == dict(mac=D, kind='device', rssi=-71)
    assert parse_line(f'SATL,{S},{R}:3:44') is None and parse_line('SATL,nope') is None
    ln = SatLink(4)                                   # a noisy device link: quiet at 2.5x the floor
    t = 0.0
    for _ in range(600):
        t += 0.5
        assert not ln.feed(2.5, 2.4, t), "its own noise is not motion"
    assert ln.feed(9.0, 3.0, t + 0.5), "well above its own quiet level: motion"
    assert ln.feed(6.0, 2.6, t + 1.0), "still over the off level"
    assert not ln.feed(2.4, 2.4, t + 1.5)
    # 2026-11-24 21:20: after a router switch two links came up above their threshold
    # and stayed 'moving' for 12 h (the quiet level only learned while quiet)
    ln, t = SatLink(3), 0.0
    ln.feed(1.0, 1.0, t)
    stuck = 0
    for _ in range(4 * 600):                          # 10 min at 4 reports/s, steady at 1.6x
        t += 0.25
        stuck += ln.feed(1.6, 1.6, t)
    assert not ln.moving and stuck < 4 * 60, f"a steady link is not movement for ever ({stuck} reports moving)"
    # a sleeper-like rhythm (15 s of movement a minute) keeps its quiet level low: each burst still counts
    ln, t, hits = SatLink(3), 0.0, 0
    for i in range(4 * 1800):
        t += 0.25
        burst = (i // 4) % 60 < 15
        m = ln.feed(4.0 if burst else 1.0, 2.0 if burst else 1.0, t)
        hits += m and burst and i > 4 * 600
    assert hits > 0.8 * 4 * 15 * 20, f"the bursts are still seen once it has learned ({hits})"
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(60), settings=os.path.join(tmp, 'dashboard.json'))
    d.feed(f'ME,{FIRST}\n')
    d.feed(f'LEARNED,1,{R}\n')
    d.feed(f'SATL,{S},{R}:3:22:21\n')               # before its sealed report: not taken
    assert S not in d.sat_links
    d.feed(f'SAT,{S},-40,1,2,20,15,96,-57,2,60,{R},6\n')
    for i in range(10):
        d.feed(f'SATL,{S},{R}:3:22:21,{FIRST}:4:25:24,{D}:4:30:28\n')
    d.feed(f'SURV,{S},{R}:0:-60,{FIRST}:1:-58\n')
    sl = d.slow(30)
    mb = sl['map_boards'][0]
    hr = sl['hearings']                             # who hears whom: the Map places from these
    RK = 'r:' + R.lower()                           # a router is keyed by its address
    assert ['sat:' + S, -60] in hr[RK] and ['sat:' + S, -58] in hr['hub'], hr
    assert ['hub', -40] in hr['sat:' + S], hr
    keys = {l['key']: l for l in mb['links']}
    assert set(keys) == {RK, 'hub', D} and keys[RK]['router'] and not keys[D]['router'], keys
    assert keys[RK]['name'] == 'Router 2', "a router the hub watches keeps the hub's name for it"
    assert mb['router'] == RK, "a router the hub watches shares its dot"
    assert d.surveys[S][FIRST]['rssi'] == -58


def follow_network():
    """A new network for the hub: the other boards are told first through the
    hub (SATCMD), the hub switches once they confirmed or after the wait; a
    board's own command goes through the hub when it isn't on USB here."""
    import tempfile
    import web
    from web import Dashboard
    S1, S2 = mac(0xc4, 0x9e, 0x7e, 1, 1, 1), mac(0x68, 0x9d, 0xd2, 2, 2, 2)
    sent = []
    d = Dashboard(State(60), send=sent.append, settings=os.path.join(tempfile.mkdtemp(), 'dashboard.json'))
    d.feed(f'ME,{FIRST}\n')
    for S in (S1, S2):
        d.feed(f'SAT,{S},-40,1,2,20,15,96,-57,2,60,{mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)},6\n')
    sent.clear()
    ok, msg = d.command(dict(cmd='use', name='Other net'))
    assert ok and 'telling' in msg, msg
    assert sorted(sent) == sorted([f'SATCMD,{S1},USE,Other net', f'SATCMD,{S2},USE,Other net']), sent
    d.tick()
    assert 'USE,Other net' not in sent, "the hub waits for the boards"
    d.feed(f'SATCMD,{S1},queued,5\n')
    d.feed(f'SATCMD,{S1},done,5\n')
    d.tick()
    assert 'USE,Other net' not in sent, "one board still to confirm"
    d.feed(f'SATCMD,{S2},done,6\n')
    d.tick()
    assert sent[-1] == 'USE,Other net' and d.follow is None, sent
    ev = [e['text'] for e in d.events if e['type'] == 'network']
    assert ev and 'switched too' in ev[-1] and 'not confirmed' not in ev[-1], ev
    # a board with older firmware never confirms: the hub goes after the wait
    sent.clear()
    d.command(dict(cmd='use_default'))
    d.feed(f'SATCMD,{S1},done,7\n')
    d.follow['at'] -= web.FOLLOW_WAIT_S + 1
    d.tick()
    assert sent[-1] == 'USE_DEFAULT', sent
    ev = [e['text'] for e in d.events if e['type'] == 'network']
    assert 'not confirmed' in ev[-1], ev
    # one board's own command, from the air
    d.feed(f'SATCMD,{S2},failed,8\n')                # the hub gave up on it: the board's queue moves on
    sent.clear()
    ok, msg = d.command(dict(cmd='cal', board=S2))
    assert ok and 'through the hub' in msg and sent == [f'SATCMD,{S2},CAL'], (msg, sent)
    d.feed(f'SATCMD,{S2},done,9\n')                  # one at a time: the next goes once it confirmed
    ok, _ = d.command(dict(cmd='sens', board=S2, x100=150))
    assert ok and sent[-1] == f'SATCMD,{S2},SENS,150'
    assert d._sat_settings_view(S2)['SENS'] == dict(line='SENS,150', state='pending')
    ok, _ = d.command(dict(cmd='out', board=S2, mode='motion', secs=120))
    assert ok and sent[-1] == f'SATCMD,{S2},SENS,150', "waits for the SENS to be confirmed (one order per board at the hub)"
    d.feed(f'SATCMD,{S2},done,10\n')
    assert sent[-1] == f'SATCMD,{S2},OUT,motion,120', sent[-1]
    assert d._sat_settings_view(S2)['SENS']['state'] == 'done'
    # a restart: every setting you gave it goes again
    d.feed(f'SATCMD,{S2},done,11\n')
    d.sats[S2]['up_s'] = 5000
    sent.clear()
    d.feed(f'SAT,{S2},-40,99,2,20,15,96,-57,0,12,{mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)},6\n')
    assert sent and sent[0] in (f'SATCMD,{S2},OUT,motion,120', f'SATCMD,{S2},SENS,150'), sent
    assert any('restarted' in e['text'] for e in d.events), "said"
    d.sat_q.clear()
    ok, _ = d.command(dict(cmd='out', mode='on', secs=0))
    assert ok and sent[-1] == 'OUT,on,0', "the hub's own pin"
    assert not d.command(dict(cmd='out', mode='disco', secs=1))[0] and not d.command(dict(cmd='out', mode='on', secs=-1))[0]
    # links between the boards: a satellite pokes the boards it hears well, one command at a time
    d.sat_cmds.clear()
    for S in (S1, S2):
        d.feed(f'SAT,{S},-40,1,2,20,15,96,-57,2,60,{mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)},6\n')
    d.feed(f'SURV,{S1},{S2}:1:-60,{FIRST}:1:-55\n')
    sent.clear(); d._peer_at = 0; d.tick()
    assert f'SATCMD,{S1},POKE,{S2}' in sent, sent          # the other satellite before the hub
    sent.clear(); d._peer_at = 0; d.tick()
    assert not any(c.startswith(f'SATCMD,{S1},') for c in sent), "one at a time"
    d.feed(f'SATCMD,{S1},done,30\n')
    d.feed(f'SATL,{S1},{S2}:4:22:21\n')                    # its report now has that link
    sent.clear(); d._peer_at = 0; d.tick()
    assert f'SATCMD,{S1},POKE,{FIRST}' in sent, sent        # then the hub
    d.feed(f'SATCMD,{S1},done,31\n')
    d.peer_tries[(S1, FIRST)] = (3, 0.0)                    # older firmware: never linked, given up after 3
    sent.clear(); d._peer_at = 0; d.tick()
    assert not any('POKE' in c for c in sent), sent
    d.feed('NETWORK,chosen,HomeNet\n')                # the other boards onto the hub's network; the hub stays
    sent.clear()
    ok, _ = d.command(dict(cmd='net_sync'))
    assert ok and sorted(sent) == sorted([f'SATCMD,{S1},USE,HomeNet', f'SATCMD,{S2},USE,HomeNet']), sent
    d.feed(f'BREATH,{S2},clear,14.5,180,75\n')        # someone still near board S2 (presence)
    d.feed(f'BREATH,{FIRST},none,0.0,0,0\n')
    br = d._people()['breathing']
    assert set(br) == {'sat:' + S2} and br['sat:' + S2]['rate'] == 14.5 and br['sat:' + S2]['level'] == 'clear', br
    d.feed(f'BREATH,{S2},maybe,1,1,1\n')              # garbage: ignored
    assert d.breath['sat:' + S2]['level'] == 'clear'
    d.feed('OUT,motion,60,off\n')
    assert d.out == dict(mode='motion', secs=60, state='off'), d.out
    d.feed(f'SATCMD,{S2},failed,9\n')
    assert any('older firmware' in e['text'] for e in d.events if e['type'] == 'health')
    assert not d.command(dict(cmd='cal', board=mac(1, 2, 3, 4, 5, 6)))[0], "a board nobody hears"
    # alone (no other boards): the hub switches at once
    sent2 = []
    d2 = Dashboard(State(60), send=sent2.append)
    d2.feed(f'ME,{FIRST}\n')
    sent2.clear()
    d2.command(dict(cmd='use', name='Net'))
    assert sent2 == ['USE,Net'], sent2


def poke_policy():
    """The settings reach every board, judged on what each one reports, not
    on what was asked: Devices Off stops all pokes on the satellites too (the
    pokes they restore from their own memory after a restart as well); links
    between boards off stops only board links; Manual keeps only your picks.
    The switch is saved, and a board on USB follows it too."""
    import tempfile
    S1, S2 = mac(0xc4, 0x9e, 0x7e, 1, 1, 1), mac(0x68, 0x9d, 0xd2, 2, 2, 2)
    DEV, PICK = mac(0x5c, 0xaa, 0xfd, 9, 9, 9), mac(0x5c, 0xaa, 0xfd, 8, 8, 8)
    BSSID = mac(0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xf1)
    seq = [10]

    def sat(d, S, links):
        seq[0] += 1
        d.feed(f'SAT,{S},-40,{seq[0]},2,20,15,96,-57,{links},60,{BSSID},6\n')

    def tick(d, sent):
        sent.clear()
        d._peer_at = 0
        d.tick()
        return [c for c in sent if c.startswith('SATCMD,')]

    def done(d, S):
        d.feed(f'SATCMD,{S},done,{seq[0]}\n')

    tmp = tempfile.mkdtemp()
    sent = []
    d = Dashboard(State(60), send=sent.append, settings=os.path.join(tmp, 'dashboard.json'))
    d.feed(f'ME,{FIRST}\n')
    for S in (S1, S2):
        sat(d, S, 2)
    d.feed(f'SATL,{S1},{S2}:4:22:21,{DEV}:4:5:5\n')            # S1 links to S2 and to a device
    d.feed(f'SURV,{S1},{S2}:1:-60,{FIRST}:1:-55,{DEV}:2:-60\n')
    # Off: every board stops, the board links included, and nothing is asked for
    assert d.command(dict(cmd='mode', mode='off'))[0]
    got = tick(d, sent)
    assert f'SATCMD,{S1},UNPOKE,all' in got and f'SATCMD,{S2},UNPOKE,all' in got, got
    assert not any(',POKE,' in c for c in got), got
    assert d._boards()[-2]['policy'] == 'Devices: Off', d._boards()
    for S in (S1, S2):
        done(d, S)
    assert not tick(d, sent), "not re-sent at once"
    for S in (S1, S2):
        sat(d, S, 0)                                            # both report no slots in use
    assert not tick(d, sent), "nothing left to stop, and nothing asked for"
    # S1 restarts and pokes what it saved on the board (silent: no link scores yet)
    sat(d, S1, 2)
    assert tick(d, sent) == [], "not more often than every 30 s"
    d._sat_policy_at.clear()
    assert tick(d, sent) == [f'SATCMD,{S1},UNPOKE,all'], "found again from what the board reports"
    done(d, S1)
    sat(d, S1, 0)
    assert not tick(d, sent)
    assert d._boards()[-2]['pokes'] == [] and d._boards()[-2]['policy'] is None
    # Auto, but links between boards off: only the board link goes, no board link asked for
    assert d.command(dict(cmd='mode', mode='auto'))[0]
    ok, msg = d.command(dict(cmd='settings', peer_links=False))
    assert ok and 'Links between boards off' in msg, msg
    sat(d, S1, 2)
    d.feed(f'SATL,{S1},{S2}:4:22:21,{DEV}:4:5:5\n')
    got = tick(d, sent)
    assert f'SATCMD,{S1},UNPOKE,{S2}' in got and not any(',POKE,' in c for c in got), got
    done(d, S1)
    sat(d, S1, 1)
    d.feed(f'SATL,{S1},{DEV}:4:5:5\n')
    sat(d, S2, 0)
    got = tick(d, sent)
    assert not any(f',POKE,{S1}' in c or f',POKE,{S2}' in c or f',POKE,{FIRST}' in c for c in got), got
    # the reported bug: a slot busy with a board that never answers shows no link scores,
    # only one more slot in use; with board links off it is unpoked by every board address
    for S in (S1, S2):
        done(d, S)
    d._sat_policy_at.clear()
    sat(d, S1, 2)                                               # 2 slots, but only the device reports
    d.feed(f'SATL,{S1},{DEV}:4:5:5\n')
    asked = set()
    for _ in range(4):
        got = tick(d, sent)
        asked |= {c.split(',', 3)[3] for c in got if f'SATCMD,{S1},UNPOKE,' in c}
        done(d, S1)
        sat(d, S1, 2)
        d.feed(f'SATL,{S1},{DEV}:4:5:5\n')
    assert {S2, FIRST.lower()} <= {a.lower() for a in asked}, asked
    # newer firmware says what it is set to and what each slot pokes (SATC): the policy is kept
    # on the board too, and a silent slot is known by its address at once
    done(d, S1)
    d._sat_policy_at.clear()
    satc = lambda S, dv, bd, slots: d.feed(f'SATC,{S},{dv},{bd},0,200,beep,2,2700,status,40,strength,ff3300,motion,60,{"|".join(slots)}\n')
    sat(d, S1, 2)
    satc(S1, 1, 1, [DEV, S2])
    got = tick(d, sent)
    assert f'SATCMD,{S1},POLICY,1,0' in got, got                  # the board's own policy first
    sent.clear()
    done(d, S1)                                                  # confirmed: the next in its queue goes
    assert f'SATCMD,{S1},UNPOKE,{S2}' in sent, ("the silent board slot, by the address the board gave", sent)
    done(d, S1)
    satc(S1, 1, 0, [DEV])
    sat(d, S1, 1)
    d._sat_policy_at.clear()
    assert not [c for c in tick(d, sent) if 'POLICY' in c or 'UNPOKE' in c], "in line now: nothing more"
    assert d._sat_settings_view(S1)['POLICY']['state'] == 'verified'
    # a setting the board confirmed but doesn't show (reset by its button): sent again
    d.cfg['board_cfg'][S1] = {'SENS': 'SENS,150'}
    d.sat_applied[S1] = {'SENS': 'done'}
    satc(S1, 1, 0, [DEV])
    assert d.sat_applied[S1]['SENS'] == 'pending' and any('SENS,150' in c for c in sent[-3:]), sent[-3:]
    done(d, S1)
    d.feed(f'SATC,{S1},1,0,150,200,beep,2,2700,status,40,strength,ff3300,motion,60,{DEV}\n')
    assert d.sat_applied[S1]['SENS'] == 'verified'
    d.cfg['board_cfg'].pop(S1)
    sat(d, S1, 1)
    assert json.load(open(os.path.join(tmp, 'dashboard.json')))['peer_links'] is False, "saved"
    d2 = Dashboard(State(60), settings=os.path.join(tmp, 'dashboard.json'))
    assert d2.cfg['peer_links'] is False, "and read back after a restart"
    assert d2.s.devices.allow_boards is False, "the hub's own Auto mode leaves boards out too"
    # back on: board links are asked for again at once (earlier tries forgotten)
    assert d.command(dict(cmd='settings', peer_links=True))[0]
    for S in (S1, S2):
        done(d, S)
    got = tick(d, sent)
    assert f'SATCMD,{S1},POLICY,1,1' in got, got                  # the board's own policy first
    d.feed(f'SATC,{S1},1,1,0,200,beep,2,2700,status,40,strength,ff3300,motion,60,{DEV}\n')
    done(d, S1)
    got = tick(d, sent)
    assert f'SATCMD,{S1},POKE,{S2}' in got, got
    # Manual: a device that isn't one of your picks goes; your pick is asked for
    done(d, S1)
    d.s.devices.mode, d.s.devices.manual = 'manual', {PICK}
    d.feed(f'SURV,{S1},{S2}:1:-60,{FIRST}:1:-55,{DEV}:2:-60,{PICK}:2:-62\n')
    sat(d, S1, 1)
    d.feed(f'SATL,{S1},{DEV}:4:5:5\n')
    got = tick(d, sent)
    assert f'SATCMD,{S1},UNPOKE,{DEV}' in got, got
    done(d, S1)
    sat(d, S1, 0)
    got = tick(d, sent)
    assert f'SATCMD,{S1},POKE,{PICK}' in got, got
    # a board on USB here follows the policy too, from the POKING lines it prints
    usb = []
    d.command(dict(cmd='mode', mode='off'))
    d.feed_other(S2, 'POKING,8,' + DEV + '\n', send=usb.append)
    d.feed_other(S2, 'STATS,1000000,1,0,0,0,0,0,0,0\n', send=usb.append)
    d.others[S2].s.devices.poking = {DEV: 8}
    tick(d, sent)
    assert usb == ['UNPOKE,all'], usb
    assert any(row[3] == 'Devices: Off' for row in d.policy_log), list(d.policy_log)


def wires_and_rules():
    """Three satellites, two routers, no map at all: someone near the front
    router makes every board's wire to it react, so it is 'near the front
    router', each board says 'moving' with that wire named (never 'still'
    while one of its wires moves), and the alerts fire by board: 1 and 3, all
    3, but not a rule watching only by the boards. The page renders it."""
    import subprocess
    import tempfile
    S = [mac(0xc4, 0x9e, 0x7e, 1, 1, i) for i in (1, 2, 3)]
    R1, R2 = mac(0x90, 0x0e, 0x84, 1, 1, 1), mac(0x90, 0x0e, 0x84, 2, 2, 2)
    tmp = tempfile.mkdtemp()
    sent = []
    d = Dashboard(State(60), send=sent.append, settings=os.path.join(tmp, 'dashboard.json'))
    d.feed(f'ME,{FIRST}\n')
    seq = [100]

    def report(front, back=10):
        for s_ in S:
            seq[0] += 1
            d.feed(f'SAT,{s_},-40,{seq[0]},2,20,15,96,-57,0,60,{R1},6\n')
            d.feed(f'SATL,{s_},{R1}:6:{front}:{front},{R2}:6:{back}:{back}\n')

    report(10)                                         # quiet: learns each wire's quiet level
    d._judge_step()
    assert d.wv['headline'] == 'All wires quiet' and all(b['state'] == 'quiet' for k, b in d.bstates.items() if k != 'hub'), (d.wv['headline'], d.wv['wires']) or \
        [(k, b['state']) for k, b in d.bstates.items()]
    rules = [dict(id='a', name='1 and 3', boards=['sat:' + S[0], 'sat:' + S[2]], need='all', notify=True),
             dict(id='b', name='All three', boards=['sat:' + m for m in S], need='all', light=True),
             dict(id='c', name='By the boards', boards=[], where='yours', buzz=True)]
    assert d.command(dict(cmd='rules', rules=rules))[0]
    report(90)                                         # the front router's wire of every board reacts
    d._judge_step()
    assert d.wv['ends']['r:' + R1]['hot'] and not d.wv['ends']['r:' + R2]['hot'], d.wv['ends']
    assert d.wv['side'] == 'over' and 'Near' in d.wv['words'][0]['text'], d.wv['words']
    for m in S:
        b = d.bstates['sat:' + m]
        assert b['state'] == 'moving' and len(b['by']) == 1 and not b['near_me'], b
    on = {r['id'] for r, _s in d.trig_active}
    assert on == {'a', 'b'}, on
    assert 'EXT,1' not in sent, "the buzzer rule wants movement right by the boards"
    assert any("Alert '1 and 3'" in e['text'] for e in d.events)
    sl = d.slow(30)
    assert sl['rules']['state']['a']['on'] and len(sl['bstates']) == 4
    # the page renders this payload without an error
    p = os.path.join(tmp, 'slow.json')
    with open(p, 'w') as f:
        json.dump(sl, f)
    r = subprocess.run(['node', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_pages_load.js'), p],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # one board, right by it: both of its wires react, nothing else
    report(10)
    seq[0] += 1
    d.feed(f'SATL,{S[1]},{R1}:6:90:90,{R2}:6:90:90\n')
    d.wires.moved_at.clear()
    d._judge_step()
    assert d.wv['ends']['sat:' + S[1]]['hot'] and d.bstates['sat:' + S[1]]['near_me'], d.wv['ends']
    assert d.wv['side'] == 'yours'


def live_stream_and_demo():
    """The demo house: three boards (one joining late), a person walking, a
    device joining, the simulated light; and the live stream it produces."""
    import tempfile
    from fake_csi import HOUSE_SATS, HOUSE_NEW_DEV, house_layout, house_lines, house_person
    from web import Dashboard
    lines = list(house_lines(duration=60.0, seed=5))
    sat_lines = [l for l, _t in lines if l.startswith('SAT,')]
    macs = {l.split(',')[1] for l in sat_lines}
    assert macs == {m for m, *_ in HOUSE_SATS}, macs
    office = HOUSE_SATS[2][0]
    first = next(t for l, t in lines if l.startswith(f'SAT,{office},'))
    assert first >= HOUSE_SATS[2][4], "the Office board joins late"
    assert any(l.startswith(f'DEV,{HOUSE_NEW_DEV[0]}') for l, _t in lines)
    assert any(house_person(t)[1] for t in range(60)) and not house_person(0)[1], "walks, after a pause"
    assert len(house_layout()['nodes']) == 2 + 3 + 3 + 1
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(600), settings=os.path.join(tmp, 'dashboard.json'), sessions_dir=tmp, demo=True)
    for l, _t in lines:
        d.feed(l + '\n')
        d.tick() if l.startswith('STATS,') else None
    evs = d.slow(30)['events']
    kinds = {e['type'] for e in evs}
    assert {'motion', 'health', 'device'} <= kinds, kinds
    assert any(e['type'] == 'health' and 'joined' in e['text'] for e in evs)
    mo = [e for e in evs if e['type'] == 'motion' and not e.get('open')]
    assert mo and all(e['dur'] >= 0 for e in mo), "a finished motion has its length"
    assert d.light.cfg['enabled'] and 'demo.invalid' in d.light.cfg['on_url'], "demo: a light that only pretends"
    ids = [e['id'] for e in evs]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def reconnects():
    # the hub's port goes away and comes back: DISCONNECTED, then data again
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(60), settings=os.path.join(tmp, 'dashboard.json'))
    q = queue.Queue()
    q.put('#ERR device reports readiness to read but returned no data')
    q.put(None)
    pump(d, q)
    assert d.s.lost and d.lost_since is not None, "the port is gone"
    assert d.s.source_status()[0] == 'DISCONNECTED'
    q.put('#OK reconnected')
    q.put(None)
    pump(d, q)
    assert d.s.lost is None and d.lost_since is None, "back"
    assert d.s.source_status() is None or d.s.source_status()[0] != 'DISCONNECTED'


def lag_queue():
    # behind: CSI lines (own or tagged) are dropped past the limit, nothing else
    q = LagQueue()
    q.LIMIT = 3
    tag = '@' + ':'.join(['aa'] * 6) + ','
    for i in range(6):
        q.put('CSI,1,0,1,6,-50,0,0,4,AAAA')
    q.put(tag + 'CSI,1,0,1,6,-50,0,0,4,AAAA')
    q.put((1, 'CSI,1'))
    q.put('STATE,1,motion,1,1')
    q.put((0, tag + 'SAT,x'))
    assert q.dropped == 4 and q.qsize() == 6, (q.dropped, q.qsize())


def travel():
    from boards import Travel
    tr = Travel()
    assert tr.start('sat:a', 100.0) is None                  # one board: no path yet
    assert tr.start('sat:b', 100.3) is None                  # at once: one spot both see
    p = tr.start('sat:b', 103.0)
    assert [k for _t, k in p] == ['sat:a', 'sat:b'] and tr.n == 1
    assert tr.start('sat:b', 104.0) is None                  # still there
    p = tr.start('hub', 109.0)
    assert [k for _t, k in p] == ['sat:a', 'sat:b', 'hub'] and tr.n == 1, "the same path grows"
    assert tr.start('sat:a', 130.0) is None and tr.n == 2    # long after: a new path
    assert tr.start('sat:b', 140.0) is None                  # over STEP_MAX_S: not a step
    # the dashboard: one live-stream entry that grows with the path
    tmp = tempfile.mkdtemp()
    d = Dashboard(State(60), settings=os.path.join(tmp, 'dashboard.json'))
    d.sats = {'aa': {}, 'bb': {}}
    d.cfg['board_names'] = {'aa': 'Kitchen', 'bb': 'Office'}
    d._period('sat:aa', True, 200.0)
    d._period('sat:bb', True, 203.0)
    d._period('board', True, 206.0)
    ev = [e for e in d.events if e['type'] == 'travel']
    assert len(ev) == 1 and ev[0]['text'] == 'Kitchen → Office → Hub', ev
    assert ev[0]['keys'] == ['sat:aa', 'sat:bb', 'hub'] and ev[0]['dur'] == 6.0


if __name__ == '__main__':
    clock()
    comparing()
    house()
    sat_links()
    follow_network()
    poke_policy()
    wires_and_rules()
    live_stream_and_demo()
    tagged_stream()
    ports()
    reconnects()
    lag_queue()
    travel()
    print("MULTI TESTS PASSED")
