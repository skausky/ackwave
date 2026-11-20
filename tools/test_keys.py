"""Tests for keys.py (each board's key, handed out over USB) and how the
dashboard keeps the secret: never on the page, never in the Debug list."""
import json
import os
import stat
import tempfile

from gui import State
from keys import MAX_PEERS, Pairing, board_key, fingerprint, new_secret, redact
from web import Dashboard


def mac(*b):
    return ':'.join(f'{x:02x}' for x in b)


HUB, SAT1, SAT2 = mac(0x98, 0xa3, 0x16, 1, 2, 3), mac(0xc4, 0x9e, 0x7e, 4, 5, 6), mac(0x68, 0x9d, 0xd2, 7, 8, 9)


def derive():
    s = new_secret()
    assert len(s) == 64 and s != new_secret()
    k1, k2 = board_key(s, SAT1), board_key(s, SAT2)
    assert len(k1) == 16 and k1 != k2 and board_key(s, SAT1) == k1, "one key per board, stable"
    assert board_key(new_secret(), SAT1) != k1
    assert len(fingerprint(k1)) == 8 and fingerprint(k1) != fingerprint(k2)
    assert redact('KEY,' + k1.hex()) == 'KEY,<hidden>' and redact('KEY,clear') == 'KEY,clear'
    assert redact(f'PEERKEY,{SAT1},{k1.hex()}') == f'PEERKEY,{SAT1},<hidden>'
    assert redact(f'PEERKEY,{SAT1},clear') == f'PEERKEY,{SAT1},clear' and redact('CAL') == 'CAL'


def pairing():
    s = new_secret()
    p = Pairing(s)
    assert p.commands(True, [SAT1], 0) == [], "nothing before the board says what it holds"
    p.feed(f'ME,{HUB}')
    assert p.commands(True, [SAT1], 0) == []
    assert p.feed('KEY,none')
    c = p.commands(True, [SAT1, SAT2], 0)
    assert c == [f'KEY,{board_key(s, HUB).hex()}', f'PEERKEY,{SAT2},{board_key(s, SAT2).hex()}',
                 f'PEERKEY,{SAT1},{board_key(s, SAT1).hex()}'], c          # by MAC
    assert p.commands(True, [SAT1, SAT2], 5) == [], "not again while it saves"
    # the board now lists them (PEERKEY lines, then KEY closes the report)
    p.feed(f'PEERKEY,{SAT1},{fingerprint(board_key(s, SAT1))}')
    p.feed(f'PEERKEY,{SAT2},{fingerprint(board_key(s, SAT2))}')
    p.feed(f'KEY,{fingerprint(board_key(s, HUB))}')
    assert p.commands(True, [SAT1, SAT2], 100) == []
    st = p.status(True, [SAT1, SAT2])
    assert st['key'] == 'ok' and st['paired'] == sorted([SAT1, SAT2])
    # a board that went away: its key is taken back
    p.feed(f'PEERKEY,{SAT1},{fingerprint(board_key(s, SAT1))}')
    p.feed(f'PEERKEY,{SAT2},{fingerprint(board_key(s, SAT2))}')
    p.feed(f'KEY,{fingerprint(board_key(s, HUB))}')
    assert p.commands(True, [SAT1], 200) == [f'PEERKEY,{SAT2},clear']
    # a satellite plugged in: its own key only, and any boards' keys it held are cleared
    q = Pairing(s)
    q.feed(f'ME,{SAT1}')
    q.feed(f'PEERKEY,{SAT2},{fingerprint(board_key(s, SAT2))}')
    q.feed('KEY,none')
    assert q.commands(False, [SAT2, HUB], 0) == [f'KEY,{board_key(s, SAT1).hex()}', 'PEERKEY,clear']
    # a wrong key (another PC's) is replaced; the board changing resets what we knew
    q.feed(f'KEY,{fingerprint(board_key(new_secret(), SAT1))}')
    assert q.commands(False, [], 100) == [f'KEY,{board_key(s, SAT1).hex()}']
    q.feed(f'ME,{SAT2}')
    assert q.key_fp is None and q.commands(False, [], 200) == []
    # where reports go: a satellite is told the hub; the hub sends none
    r = Pairing(s)
    r.feed(f'ME,{SAT1}')
    r.feed('HUB,none')
    r.feed(f'KEY,{fingerprint(board_key(s, SAT1))}')
    assert r.key_ok() and r.commands(False, [], 0, hub_mac=HUB) == [f'HUB,{HUB}']
    r.feed(f'HUB,{HUB},6,40,38')
    assert r.commands(False, [], 100, hub_mac=HUB) == [] and (r.hub_ch, r.reports, r.answered) == (6, 40, 38)
    h = Pairing(s)
    h.feed(f'ME,{HUB}')
    h.feed(f'HUB,{SAT1},6,0,0')
    h.feed(f'KEY,{fingerprint(board_key(s, HUB))}')
    assert h.commands(True, [], 0, hub_mac=HUB) == ['HUB,clear']
    # at most 8 boards' keys; junk lines never count
    many = [mac(0x24, 0, 0, 0, 0, i) for i in range(12)]
    p2 = Pairing(s)
    p2.feed(f'ME,{HUB}')
    p2.feed('KEY,none')
    assert len([c for c in p2.commands(True, many, 0) if c.startswith('PEERKEY')]) == MAX_PEERS
    for junk in ('KEY,zzz', 'PEERKEY,nope,1234abcd', 'SEAL,x,1', 'KEY'):
        p2.feed(junk)
    assert p2.key_fp == 'none'
    p2.feed('SEAL,3,1')
    assert p2.status(True, [])['bad'] == 3 and p2.status(True, [])['replays'] == 1


def dashboard():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 'dashboard.json')
    sent = []
    d = Dashboard(State(60), send=sent.append, settings=path)
    d.feed(f'ME,{HUB}\n')
    d.feed(f'PEER,{SAT1},-41,10,200,6,1\n')
    d.feed('KEY,none\n')
    with open(path) as f:
        cfg = json.load(f)
    secret = cfg['board_secret']
    assert len(secret) == 64 and cfg['hub'] == HUB, "a secret made once; the first board is the hub"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "owner-only, like the light URLs"
    assert f'KEY,{board_key(secret, HUB).hex()}' in sent
    assert f'PEERKEY,{SAT1},{board_key(secret, SAT1).hex()}' in sent
    page = json.dumps([d.slow(30), d.fast(30), d.debug(30)])
    assert secret not in page and board_key(secret, HUB).hex() not in page and \
        board_key(secret, SAT1).hex() not in page, "never on the page"
    assert any(c == 'KEY,<hidden>' for _t, c in d.sent_cmds), d.sent_cmds
    b = d.slow(30)['boards']
    assert b and b[0]['mac'] == SAT1 and b[0]['state'] == 'unpaired', b
    assert d.slow(30)['pairing']['hub'] is True
    # its reports arrive: no longer "needs its key"
    d.feed(f'SAT,{SAT1},-41,7,2,20,15,96,-57,2,3600\n')
    assert d.slow(30)['boards'][0]['state'] == 'still'
    # restarted: the same secret and hub; another board plugged in is not the hub
    sent2 = []
    d2 = Dashboard(State(60), send=sent2.append, settings=path)
    d2.feed(f'ME,{SAT2}\n')
    d2.feed(f'PEERKEY,{SAT1},{fingerprint(board_key(secret, SAT1))}\n')
    d2.feed('KEY,none\n')
    assert d2.board_secret == secret and d2.cfg['hub'] == HUB
    assert [c for c in sent2 if 'KEY' in c] == [f'KEY,{board_key(secret, SAT2).hex()}', 'PEERKEY,clear'], sent2
    # a satellite plugged in alone gets the hub's address and is remembered as
    # paired, so the hub gets its key even when it can't hear its hellos
    sent3 = []
    d4 = Dashboard(State(60), send=sent3.append, settings=path)
    d4.feed(f'ME,{SAT1}\n')
    d4.feed('HUB,none\n')
    d4.feed(f'KEY,{fingerprint(board_key(secret, SAT1))}\n')
    assert f'HUB,{HUB}' in sent3 and SAT1 in d4.cfg['paired'], (sent3, d4.cfg['paired'])
    with open(path) as f:
        assert SAT1 in json.load(f)['paired']
    sent4 = []
    d5 = Dashboard(State(60), send=sent4.append, settings=path)
    d5.feed(f'ME,{HUB}\n')
    d5.feed('KEY,none\n')                      # nothing heard over the air: still paired
    assert f'PEERKEY,{SAT1},{board_key(secret, SAT1).hex()}' in sent4, sent4
    b = {x['mac']: x for x in d5.slow(30)['boards']}   # paired, never heard: listed, no crash
    assert b[SAT1]['state'] == 'quiet' and b[SAT1]['heard_s'] is None, b
    # a file input (no board to talk to): no secret is made
    d3 = Dashboard(State(60), settings=os.path.join(tmp, 'other.json'))
    d3.feed(f'ME,{HUB}\n')
    d3.feed('KEY,none\n')
    assert d3.board_secret is None and not os.path.exists(os.path.join(tmp, 'other.json'))


if __name__ == '__main__':
    derive()
    pairing()
    dashboard()
    print("KEYS TESTS PASSED")
