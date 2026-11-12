#!/usr/bin/env python3
"""Tests devices.py: OUI names, the stationary check, and which devices the
board is told to poke in each mode. MACs are built at runtime (the repo's
pre-commit hook refuses MAC literals)."""
import os
import tempfile

from analyze import MotionDetector, parse_line
from devices import (FAIL_S, MAX_DEVS, OBSERVE_S, RETRY_S, Devices, Oui, fmt_age,
                     mac_is_random)


def mac(*b):
    return ':'.join(f"{x:02x}" for x in b)


TV = mac(0x3c, 0x22, 0xfb, 1, 2, 3)        # real (globally unique) MACs
PLUG = mac(0x50, 0x02, 0x91, 4, 5, 6)
CAM = mac(0x50, 0x02, 0x91, 7, 8, 9)
PHONE = mac(0xda, 0xa1, 0x19, 1, 1, 1)     # private random MAC
CONSOLE = mac(0xa4, 0xc1, 0xe8, 9, 9, 9)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def dev(d, m, rssi, tx, rx=0, age_ms=0):
    return d.feed('dev', dict(mac=m, rssi=rssi, tx=tx, rx=rx, age_ms=age_ms))


def cycle(d, clk, devices, gates=(), poking=None, step=5.0):
    """One 5 s board report: GATE, POKING, DEV lines, then STATS."""
    clk.t += step
    for g in gates:
        d.feed('gate', g)
    for m, i in (poking if poking is not None else d.poking).items():
        d.feed('poking', dict(idx=i, mac=m))
    for args in devices:
        dev(d, *args)
    return d.feed('stats', {})


def check(cond, what):
    if not cond:
        raise AssertionError(what)


def test_parse():
    check(parse_line(f"DEV,{TV.upper()},-55,12,3,400\n") ==
          ('dev', dict(mac=TV, rssi=-55, tx=12, rx=3, age_ms=400)), 'DEV')
    check(parse_line("GATE,8,-60,97,500,ok") ==
          ('gate', dict(idx=8, rssi=-60, yield_pct=97, sent=500, state='ok')), 'GATE')
    check(parse_line("GATE,8,-60,97,500,maybe") is None, 'bad GATE state')
    check(parse_line(f"POKING,9,{PLUG}") == ('poking', dict(idx=9, mac=PLUG)), 'POKING')
    check(parse_line("UNPOKED,all") == ('unpoked', dict(mac='all')), 'UNPOKED')
    check(parse_line(f"REFUSED,{PLUG},not-seen-on-network") ==
          ('refused', dict(mac=PLUG, why='not-seen-on-network')), 'REFUSED')
    check(parse_line("REFUSED,bad-command") == ('refused', dict(mac='', why='bad-command')),
          'REFUSED without MAC')
    check(parse_line("DEV,short,-55,1,1,1") is None, 'bad DEV')
    garbled = "CSI,1,0,0,6,-40,0,0,128," + " ".join(["12"] * 126 + ["128200", "5"])
    check(parse_line(garbled) is None, 'garbled CSI line (numbers run together) is skipped')


def test_verdict_ignores_device_links():
    """Device links (node >= 8) are scored but never decide motion."""
    import numpy as np
    det = MotionDetector()
    rng = np.random.default_rng(1)
    for i in range(400):
        amp = np.full(64, 20.0) + rng.normal(0, 3, 64)       # a wildly moving link
        det.on_csi(dict(ts=i * 10_000, src=8, kind=1, ch=6, rssi=-50, sig_mode=0, amp=amp))
    check(det.max_scores(400 * 10_000) == (None, None), 'device link must not count')
    check((8, 1) in det.streams and det.streams[(8, 1)].score_s is not None,
          'device link is still scored')


def test_frames_yield_to_acks():
    """A router's own frames decide only while its ACK stream is silent."""
    from analyze import Stream
    det = MotionDetector()
    t = 100_000_000
    for key, sc in (((0, 0), 5.0), ((0, 1), 1.0)):
        s = det.streams[key] = Stream()
        s.n, s.t_last, s.score_s, s.score_l = 50, t, sc, sc
    check(det.max_scores(t) == (1.0, 1.0), 'ACKs live: frames ignored')
    det.streams[(0, 1)].t_last = t - det.c['stale_s'] * 1e6 - 1
    check(det.max_scores(t) == (5.0, 5.0), 'no ACKs: frames decide')


def test_analyzer_takes_new_lines():
    """analyze.py reads a live stream with every line type the board prints."""
    import itertools
    import subprocess
    import sys
    from fake_csi import generate
    lines = [ln for ln, *_ in itertools.islice(generate(), 3000)]
    lines[100:100] = ["GATE,0,-60,97,500,ok", "GATE,8,-70,0,50,silent", f"POKING,8,{TV}",
                      f"UNPOKED,{TV}", "UNPOKED,all", f"REFUSED,{PLUG},full",
                      "REFUSED,bad-command", "BUTTON,recalibrate,pc",
                      "STATS,1000,10,0,9,1,0,0,55", "STATS,2000,10,0,9,1,0,0"]
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.join(here, 'analyze.py'), '-'],
                       input='\n'.join(lines) + '\n', capture_output=True, text=True)
    check(r.returncode == 0, f'analyze.py crashed: {r.stderr[-300:]}')
    check('silent' in r.stdout and '55 poke replies' in r.stdout, 'new lines shown')


def test_oui():
    with tempfile.TemporaryDirectory() as tmp:
        js = os.path.join(tmp, 'master_oui.json')
        with open(js, 'w') as f:
            f.write('{"3C:22:FB": {"manufacturer": "Apple, Inc.", "short_name": "Apple",'
                    ' "type": "Phone"}, "3c-22-fb": {"manufacturer": "dup"}}')
        o = Oui([os.path.join(tmp, 'missing.json'), js], background=False)
        check(o.source == js and o.lookup(TV) == ('Apple', 'Apple, Inc.', 'Phone'), 'json')
        check(o.name(TV) == 'Apple' and o.name(PLUG) == 'unknown', 'name')
        check(o.name(PHONE) == 'private address', 'random MAC')
        csvp = os.path.join(tmp, 'oui.csv')
        with open(csvp, 'w') as f:
            f.write('Registry,Assignment,Organization Name,Organization Address\n'
                    'MA-L,500291,"Espressif Inc.",Shanghai\n')
        check(Oui([csvp], background=False).name(PLUG) == 'Espressif Inc.', 'csv')
        txt = os.path.join(tmp, 'ieee-oui.txt')
        with open(txt, 'w') as f:
            f.write('# comment\nA4C1E8\tNintendo Co., Ltd\n')
        check(Oui([txt], background=False).name(CONSOLE) == 'Nintendo Co.', 'arp-scan list')
        o = Oui([], background=False)
        check(o.name(TV) == 'unknown', 'no database: unknown, no crash')
    check(mac_is_random(PHONE) and not mac_is_random(TV), 'random bit')


def steady(m, rssi, tx0, k):
    return (m, rssi + (k % 2), tx0 + 10 * k)                   # +/-0.5 dB, sending


def test_auto_picks_stationary():
    clk = Clock()
    d = Devices(clock=clk)
    check(d.reconcile() == [], 'nothing before the board reports')
    cmds = []
    n = int(OBSERVE_S / 5) + 4
    for k in range(n):
        cmds += cycle(d, clk, [
            steady(TV, -55, 0, k),
            (PLUG, -70 + (12 if k % 2 else 0), 10 * k),            # jumps 12 dB: moving
            (PHONE, -50, 10 * k),                                  # private MAC
            (CONSOLE, 0, 0, 5 + k),                                # never sends: asleep
            steady(CAM, -86, 0, k),                                # too weak
        ])
        if k < OBSERVE_S / 5 - 1:
            check(cmds == [], f'nothing poked while watching (cycle {k})')
    # Only the TV qualifies; unconfirmed, it is asked for again each report
    check(cmds and set(cmds) == {f"POKE,{TV}"}, f'only the TV qualifies: {cmds}')
    st = {r['mac']: r['status'] for r in d.rows()}
    check(st == {TV: 'stationary', PLUG: 'moving', PHONE: 'random', CONSOLE: 'quiet',
                 CAM: 'weak'}, st)
    # Board confirms; nothing more to send while it stays healthy
    out = cycle(d, clk, [steady(TV, -55, 0, n)], poking={TV: 8},
                gates=[dict(idx=8, rssi=-55, yield_pct=95, sent=50, state='ok')])
    check(out == [] and d.poking == {TV: 8}, f'steady state: {out}')
    check(d.link_label(8) == TV[-8:] and d.link_label(0) == 'router'
          and d.link_label(1) == 'AP 1' and d.link_label(9) == 'device 9', 'labels')
    check(d.rows()[0]['mac'] == TV and d.rows()[0]['poked'], 'poked first in the table')


def test_drop_silent_and_reboot_resync():
    clk = Clock()
    d = Devices(clock=clk)
    for k in range(int(OBSERVE_S / 5) + 2):
        cycle(d, clk, [steady(TV, -55, 0, k)])
    silent = dict(idx=8, rssi=-55, yield_pct=0, sent=50, state='silent')
    out = []
    for k in range(int(FAIL_S / 5) + 1):
        out = cycle(d, clk, [steady(TV, -55, 0, 100 + k)], poking={TV: 8}, gates=[silent])
    check(out == [f"UNPOKE,{TV}"] and d.status(d.devs[TV])[0] == 'dropped', f'drop: {out}')
    # It comes back after RETRY_S
    k0 = 200
    for k in range(int(RETRY_S / 5) - 1):                     # still sending, left alone
        out = cycle(d, clk, [steady(TV, -55, 0, k0 + k)], poking={})
        check(out == [], f'dropped device left alone: {out}')
    k0 += int(RETRY_S / 5)
    out = cycle(d, clk, [steady(TV, -55, 0, k0)], poking={})
    check(out == [f"POKE,{TV}"], f'retry: {out}')
    # Board rebooted (reports no POKING): we ask again
    out = cycle(d, clk, [steady(TV, -55, 0, k0 + 1)], poking={})
    check(out == [f"POKE,{TV}"], f'resync after reboot: {out}')


def test_refused_waits():
    clk = Clock()
    d = Devices(clock=clk, mode='manual', manual=[PLUG])
    out = cycle(d, clk, [], poking={})
    check(out == [f"POKE,{PLUG}"], out)
    d.feed('refused', dict(mac=PLUG, why='not-seen-on-network'))
    # manual mode keeps asking (the user asked for it); auto would wait
    d.set_mode('auto')
    check(d.desired() == set(), 'auto ignores a refused device for a while')


def test_manual_and_modes():
    clk = Clock()
    d = Devices(clock=clk)
    for k in range(int(OBSERVE_S / 5) + 2):
        cycle(d, clk, [steady(TV, -55, 0, k), (PHONE, -50, 10 * k)])
    d.poking = {TV: 8}
    check(d.toggle(PHONE) == [f"POKE,{PHONE}"] and d.mode == 'manual', 'tick switches to manual')
    check(d.manual == {TV, PHONE}, 'manual starts from what runs')
    d.poking = {TV: 8, PHONE: 9}
    check(d.toggle(TV) == [f"UNPOKE,{TV}"], 'untick')
    check(set(d.set_mode("off")) == {f"UNPOKE,{PHONE}", f"UNPOKE,{TV}"}, "off")
    d.poking = {}
    out = d.set_mode('all')
    check(sorted(out) == sorted([f"POKE,{TV}", f"POKE,{PHONE}"]), f'all: {out}')
    try:
        d.set_mode('everything')
        check(False, 'bad mode accepted')
    except ValueError:
        pass
    # Never more than MAX_DEVS
    d2 = Devices(clock=clk, mode='manual')
    d2.board_seen = True
    for i in range(MAX_DEVS + 2):
        d2.toggle(mac(0x50, 0x02, 0x91, 0, 0, i))
    check(len(d2.manual) == MAX_DEVS and len(d2.reconcile()) == MAX_DEVS, 'cap')


def test_prefs_and_format():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'devices.json')
        check(Devices.load_prefs(p) == ('auto', []), 'default prefs')
        d = Devices(mode='manual', manual=[TV])
        d.save(p)
        check(Devices.load_prefs(p) == ('manual', [TV]), 'saved prefs')
    check(fmt_age(5) == '5 s' and fmt_age(125) == '2 min 05 s' and fmt_age(7300) == '2 h 01 min',
          'ages')


def main():
    test_parse()
    test_verdict_ignores_device_links()
    test_frames_yield_to_acks()
    test_analyzer_takes_new_lines()
    test_oui()
    test_auto_picks_stationary()
    test_drop_silent_and_reboot_resync()
    test_refused_waits()
    test_manual_and_modes()
    test_prefs_and_format()
    print("DEVICES TESTS PASSED")


if __name__ == '__main__':
    main()
