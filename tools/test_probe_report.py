#!/usr/bin/env python3
"""Tests probe_report.py on a synthetic probe log in the firmware's exact format:
PROBE,<c|a>,t_us,mac,dmac,sig_mode,rate,len,rssi,dt_us,pending,verdict,rx_ts"""
from probe_report import parse, report, poke_ta, our_addrs

ME, AP, OTHER = '24:0a:c4:11:22:33', 'aa:bb:cc:dd:ee:f0', '11:22:33:44:55:66'


def probe(what, t, mac, dmac, sig, ln, dt, pending, verdict, rx_ts):
    return f"PROBE,{what},{t},{mac},{dmac},{sig},11,{ln},-50,{dt},{pending},{verdict},{rx_ts}"


def old_style():
    """Older firmware: pokes from our own MAC, ACKs addressed to ME."""
    lines = ["junk", f"ME,{ME}", "PROBE,c,bad", "STATS,1,100,2,90,40,5,0"]
    for i in range(100):
        t = 1000 + i * 10000
        dt = 200 + (i % 10) * 30                        # ACK 200-470 us after poke
        lines.append(probe('a', t + dt, '00:00:00:00:00:00', ME, 0, 0, dt, 0, 0, 5000 + i))
        if i < 90:                                      # 90 ACKs produced CSI
            verdict = 1 if i < 88 else -1               # classifier missed 2
            lines.append(probe('c', t + dt + 40, ME, ME, 0, 128, dt + 40, 0, verdict, 5000 + i))
    for i in range(40):                                 # beacons from the AP
        lines.append(probe('c', 3_000_000 + i, AP, 'ff:ff:ff:ff:ff:ff', 0, 128, 9999, -1, 0, 90000 + i))
    lines.append(probe('c', 4_000_000, OTHER, ME, 0, 128, 100, 0, 1, 99999))  # false tag

    out = []
    res = report(*parse(lines), out=out.append)
    print('\n'.join(out))
    assert res['acks_seen'] == 100 and res['ack_csi'] == 90
    assert res['tagged'] == 89 and res['correct'] == 88 and res['missed'] == 2
    assert 500 <= res['suggest_win'] <= 1000
    assert any('dmac is our own MAC' in o for o in out)


def poke_ta_style():
    """Current firmware: pokes from a made-up TA; foreign ACKs and frames around."""
    ta0 = poke_ta(ME, 0)
    lines = [f"ME,{ME}", "STATS,1,100,0,95,0,20,0"]
    for i in range(100):
        t = 1000 + i * 10000
        dt = 400 + (i % 20) * 200                       # 400-4200 us (loaded board)
        lines.append(probe('a', t + dt, '00:00:00:00:00:00', ta0, 0, 0, dt, 0, 0, 5000 + i))
        if i < 95:
            lines.append(probe('c', t + dt + 40, '00:00:00:00:00:00', ta0, 0, 128,
                               dt + 40, 0, 1, 5000 + i))
    for i in range(30):                                 # other devices' ACKs and data
        lines.append(probe('a', 2_000_000 + i, '00:00:00:00:00:00', OTHER, 0, 0, 1, 0, 0, 70000 + i))
        lines.append(probe('c', 2_000_000 + i, OTHER, OTHER, 0, 128, 1, 0, -1, 80000 + i))
    out = []
    res = report(*parse(lines), out=out.append)
    print('\n'.join(out))
    assert res['acks_seen'] == 100 and res['ack_csi'] == 95 and res['foreign_acks'] == 30
    assert res['tagged'] == 95 and res['correct'] == 95 and res['missed'] == 0
    assert res['suggest_win'] >= 4200
    assert any('made-up poke address' in o for o in out)

    # No ACKs to us, but foreign ones: control frames do get through
    out = []
    report(*parse([f"ME,{ME}", probe('a', 1, '00:00:00:00:00:00', OTHER, 0, 0, 1, 0, 0, 1),
                   probe('c', 1, OTHER, OTHER, 0, 128, 1, 0, -1, 2)]), out=out.append)
    assert any('Other devices' in o for o in out)


def main():
    # Same vector as TA1_VEC in test/test_core.c, keeps the C and Python mirrors in sync
    assert poke_ta(ME, 1) == '26:0a:c4:11:22:34'
    assert poke_ta('24:0a:c4:11:22:fe', 3) == '26:0a:c4:11:22:01'
    assert ME in our_addrs(ME) and poke_ta(ME, 7) in our_addrs(ME) and our_addrs(None) == set()
    old_style()
    poke_ta_style()
    assert report(*parse(["nothing here"]), out=lambda *_: None) == {}
    print("PROBE REPORT TESTS PASSED")


if __name__ == '__main__':
    main()
