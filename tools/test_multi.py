"""Step A of several boards (docs/redesign/multi-board.md): boards.py's clock
and tags, the dashboard routing lines from several ports and a tagged
stream, the primary chosen by saved MAC, and compare.py."""
import json
import os
import tempfile
import time

from boards import ClockMap, board_ts, split_tag
from compare import compare, report
from fake_csi import OTHER_BOARDS, generate_boards
from gui import State
from timeline import Timeline
from web import Dashboard, pump
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
    assert lanes[0] == 'board' and all(k.startswith('sat:') for k in lanes[1:3]), lanes


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


if __name__ == '__main__':
    clock()
    comparing()
    tagged_stream()
    ports()
    print("MULTI TESTS PASSED")
