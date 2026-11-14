#!/usr/bin/env python3
"""Headless test of gui.py: state tracking, reboot/loop handling, and drawing."""
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

from fake_csi import generate  # noqa: E402
from gui import State, View  # noqa: E402


def main():
    clock = [1000.0]
    s0 = State(window_s=60, clock=lambda: clock[0])
    assert s0.source_status()[0] == 'WAITING'
    s0.feed('BUZZER,off')                   # any line counts as data arriving
    assert s0.source_status() is None
    clock[0] += 10
    word, _c, detail = s0.source_status()
    assert word == 'NO DATA' and '10 s' in detail, detail
    s0.lost = 'device reports readiness to read but returned no data'
    assert s0.source_status()[0] == 'DISCONNECTED'
    s0.lost, s0.ended = None, True
    assert s0.source_status()[0] == 'INPUT ENDED'

    s = State(window_s=60)
    lines = [line for line, *_ in generate(seed=2)]
    view = View(s, plt)
    saw_calibrating = False
    for i, line in enumerate(lines):
        s.feed(line)
        saw_calibrating |= s.det.floor <= 0 and s.n_lines > 30
        if i % 400 == 0:
            view.draw()                       # draws at every stage without error
    assert saw_calibrating and s.cal_spans, "calibration period not tracked"
    assert len(s.nodes) == 2 and s.channel == (11, 'csa')
    assert s.det.external and s.board_calib and s.det.floor > 0, "board floor not adopted"
    assert abs(s.det.floor - s.board_calib['floor']) < 1e-12
    assert s.board_motion is False and len(s.board_states) == 4      # 2 motions
    assert len(s.motion_spans) == 2 and all(b is not None for _a, b in s.motion_spans)
    # PC verdict matches the board's (same floor): same number of changes
    assert set(s.scores) == {(0, 0), (0, 1), (1, 0), (1, 1)} and s.long
    assert max(v for _t, v in s.long) > s.det.c['on_long'], "long score never crossed"
    assert 35 < s.rate((0, 1)) < 45 and 8 < s.rate((0, 0)) < 12, "packet rates"
    assert s.stats and s.stats['poke'] > 0 and s.hops

    # Session history on the wall clock: per-second peaks, both motion periods
    assert len(s.hist_motion) == 2 and all(b is not None for _a, b in s.hist_motion)
    assert s.session_counts() == (2, 0)
    assert len(s.hist_t) == len(s.hist_s) == len(s.hist_l) and len(s.hist_t) > 30
    assert all(b > a for a, b in zip(s.hist_t, s.hist_t[1:])), "one bin per second, in order"
    assert max(s.hist_s) > s.det.c['on_short'] and s.hist_cal
    # Header: recent-motion list, time since last motion, link health
    rec = s.recent_motion()
    assert len(rec) == 2 and rec[0][0] > rec[1][0], "newest first"
    assert all(dur >= 2 and peak > s.det.c['on_short'] and not going
               for _a, dur, peak, going in rec), rec
    assert s.last_motion().startswith('last motion') and s.last_motion().endswith('ago')
    word, _col, detail = s.link_health()
    assert word == 'WEAK' and 'router ACK' in detail, (word, detail)   # demo: 40 ACKs/s
    view.draw()
    assert re.search(r' x\d+\.\d', view.recent.get_text()) and 'link WEAK' in view.health.get_text()
    view.details = True
    view.draw()
    assert 'pokes' in view.info.get_text(), "details show the raw counters"
    view.details = False
    # Doppler: spectra kept, header line, both bottom views draw
    assert len(s.dop.rows) > 20 and s.doppler_text().startswith('Doppler')
    assert view.bottom == 'doppler'
    view.draw()
    s.scan_spans.append([s.now - 0.5, s.now + 1.5])          # mid router scan: never "movement"
    assert s.doppler_text() == 'Doppler: router scan (ignored)'
    s.scan_spans.pop()
    tile = {k: [p.get_text() for p in v] for k, v in view.tiles.items()}
    assert tile['breathing'][0] == 'BREATHING' and tile['breathing'][1], tile
    assert tile['doppler'][1] and tile['quiet level'][1] not in ('', 'measuring'), tile
    assert 'breathing' in view.ax_br.get_title(loc='left')
    assert 'Doppler' in view.ax_wf.get_title(loc='left') and s.doppler_text().startswith('Doppler')
    view.bottom = 'subcarriers'
    view.draw()
    assert 'normalised amplitude' in view.ax_wf.get_title(loc='left')
    view.bottom = 'doppler'
    # Help overlay and toasts
    class K:
        key = 'h'
    view._key(K)
    assert view.help.get_visible() and 'recalibrate' in view.help.get_text()
    K.key = 'escape'
    view._key(K)
    assert not view.help.get_visible()
    # Network lines: current network, scan results (names may contain commas)
    s.feed('NETWORK,default,MyNetwork\n')
    assert s.network == ('default', 'MyNetwork')
    s.start_scan()
    assert s.outbox[-1] == 'SCAN' and s.nets_state == 'scanning'
    s.outbox.clear()
    s.start_scan()
    assert s.outbox == [], "no second SCAN while one runs"
    fake = lambda n: ':'.join(['02'] + ['00'] * 4 + [f'{n:02x}'])     # noqa: E731 (no MACs in git)
    for ln in ('NET,6,-70,MyNetwork', f'NETAP,{fake(1)},6,-70,MyNetwork',
               f'NETAP,{fake(2)},11,-80,MyNetwork', 'NET,11,-48,Cafe, upstairs',
               'NET,6,-75,MyNetwork', 'NET,x,-1,bad', 'NETS,done,2'):
        s.feed(ln + '\n')
    assert s.nets == [('Cafe, upstairs', 11, -48), ('MyNetwork', 6, -70)] and s.nets_state == 'done'
    aps = s.net_aps['MyNetwork']
    assert len(aps) == 2 and aps[0][0].endswith(':01') and aps[0][2] == -70, aps
    s.feed('NET,1,-30,Late\n')                       # leftovers after done: ignored
    assert len(s.nets) == 2
    s.feed('NETWORK,saved,Cafe, upstairs\n')
    assert s.network == ('switching', 'Cafe, upstairs')
    s.devices.devs[fake(9)] = object()     # heard on the old network
    s.feed('NETWORK,chosen,Cafe, upstairs\n')         # the board restarted on the new one
    assert s.network == ('chosen', 'Cafe, upstairs') and not s.devices.devs, "old devices forgotten"
    s.devices.devs[fake(9)] = object()
    s.feed('NETWORK,chosen,Cafe, upstairs\n')         # same network repeated: kept
    assert s.devices.devs
    s.devices.devs.clear()
    view.open_network()                      # no picker without a board: says so
    assert 'needs a board' in view.footer.get_text()
    view.recalibrate()                       # no board here: says so instead of nothing
    assert 'needs a board' in view.footer.get_text()
    w_end = s.last_wall
    # Window buttons and pause: drawing at each window works; paused = no redraw
    for w in (120.0, 600.0, 30.0):
        view.set_window(w)
        assert view.view_s == w
    view.toggle_pause()
    lim = view.ax_score.get_xlim()
    for line in lines[-50:]:
        s.feed(line)
    view.draw()
    assert view.paused and view.ax_score.get_xlim() == lim, "paused view moved"
    view.toggle_pause()
    assert not view.paused

    # Board reboot / looping demo: timestamps jump back -> clean restart
    for line in lines[:300]:
        s.feed(line)
    assert s.now < 3 and not s.hops and len(s.nodes) == 2
    assert s.last_wall > w_end and len(s.hist_motion) == 2, "session history kept, clock monotonic"
    assert all(t <= 3 for dq in s.scores.values() for t, _ in dq)
    view.draw()
    board_verdict_history()
    print("GUI TESTS PASSED")


def board_verdict_history():
    """The session's motion periods follow the board's STATE lines (as the events
    log does), not the PC detector; a restart mid-motion ends the period; brief
    blips don't count as the last motion."""
    import base64
    clock = [5000.0]
    s = State(window_s=60, clock=lambda: clock[0])
    zeros = base64.b64encode(bytes(104)).decode()
    csi = lambda ts: f"CSI,{ts},0,1,6,-50,0,0,104,{zeros}"

    def at(sec, line):
        clock[0] = 5000.0 + sec
        s.feed(line)

    at(0, "CALIB,1000000,track,boot,25000,70000,35000")
    s.det.evaluate = lambda ts: 1               # the PC detector disagrees: says motion
    at(1, csi(2_000_000))
    assert len(s.hist_motion) == 1, "no STATE yet: the PC detector stands in"
    s.det.evaluate = lambda ts: -1
    at(4, csi(5_000_000))
    assert s.hist_motion[-1][1] is not None
    at(5, "STATE,6000000,motion,99,40")            # from here the board decides
    s.det.evaluate = lambda ts: -1
    at(6, csi(7_000_000))
    assert s.hist_motion[-1][1] is None, "PC detector ended a period the board still has"
    at(15, "STATE,16000000,still,30,30")
    assert s.session_counts() == (2, 0) and s.hist_motion[-1][1] - s.hist_motion[-1][0] == 10
    s.det.evaluate = lambda ts: 1
    at(14, csi(15_000_000))
    assert len(s.hist_motion) == 2 and s.board_verdict, "PC motion ignored once STATE is seen"
    s.det.evaluate = lambda ts: 0
    # a brief blip doesn't count as 'last motion'
    at(20, "STATE,21000000,motion,95,35")
    at(20.5, "STATE,21500000,still,30,30")
    at(40, csi(41_000_000))
    assert s.session_counts() == (2, 1)
    assert s.last_motion() == 'last motion 25 s ago', s.last_motion()
    # the board restarts mid-motion: the period ends at the restart, the
    # verdict stays the board's
    at(50, "STATE,51000000,motion,99,40")
    at(60, csi(61_000_000))
    at(61, "CALIB,1062260,wait,boot,0,0,0")
    assert s.hist_motion[-1][1] is not None and 9 <= s.hist_motion[-1][1] - s.hist_motion[-1][0] <= 11
    assert s.board_verdict and s.last_motion().startswith('last motion')


if __name__ == '__main__':
    main()
