"""Settings reach the boards and stay applied (web.py): a setting a board takes but never shows is
said and not re-sent for ever; light-output times the board reports in 16 bits still verify; a model
upload the hub never answers ends; the sensor log reads every day of a week; repeated link names in
the link history get told apart."""
import json
import os
import tempfile
import time

from gui import State
from sensors import SensorLog
from web import Dashboard, out_line

def mac(*b):                       # built at run time: no address is written out in the repo
    return ':'.join(f'{x:02x}' for x in b)


FIRST = mac(0x24, 0x0a, 0xc4, 0x11, 0x22, 0x33)
S1 = mac(0xc4, 0x9e, 0x7e, 1, 1, 1)
R = mac(0x90, 0x0e, 0x84, 1, 1, 1)
D1, D2, D9 = (mac(0x5c, 0xaa, 0xfd, 0, 0, k) for k in (1, 2, 9))


def board(tmp):
    sent = []
    d = Dashboard(State(60), send=sent.append, settings=os.path.join(tmp, 'dashboard.json'), sessions_dir=tmp)
    d.feed(f'ME,{FIRST}\n')
    d.feed(f'SAT,{S1},-40,1,2,20,15,96,-57,0,60,{R},6\n')
    return d, sent


def satc(d, sens=0, out='motion,60'):
    m, secs = out.split(',')
    d.feed(f'SATC,{S1},1,1,{sens},200,beep,2,2700,status,40,strength,ff3300,{m},{secs},\n')


def main():
    tmp = tempfile.mkdtemp()
    d, sent = board(tmp)
    # a setting it takes but never shows: sent again 3 times, then 'failed' and said, not for ever
    d.cfg['board_cfg'][S1] = {'SENS': 'SENS,150'}
    d.sat_applied[S1] = {'SENS': 'done'}
    resent = 0
    for i in range(6):
        d._cfg_fix_at[(S1, 'SENS')] = (time.time() - 200, d._cfg_fix_at.get((S1, 'SENS'), (0, 0))[1])
        d.sat_cmds.pop(S1, None)
        d.sat_q.pop(S1, None)
        n0 = len(sent)
        satc(d, sens=0)
        resent += any('SENS,150' in c for c in sent[n0:])
        if d.sat_applied[S1]['SENS'] == 'pending':
            d.sat_applied[S1]['SENS'] = 'done'          # the board said done, and still shows 0
    assert resent == 3 and d.sat_applied[S1]['SENS'] == 'failed', (resent, d.sat_applied)
    assert any("doesn't stay as set" in e['text'] for e in d.events)
    # verified once it shows it (and the count starts over)
    satc(d, sens=150)
    assert d.sat_applied[S1]['SENS'] == 'verified' and (S1, 'SENS') not in d._cfg_fix_at
    # light output: the board reports up to 65535 s; longer is refused up front
    assert out_line(dict(mode='motion', secs=65535)) and out_line(dict(mode='motion', secs=70000)) is None
    d.cfg['board_cfg'][S1] = {'OUT': 'OUT,motion,65535'}
    d.sat_applied[S1] = {'OUT': 'done'}
    satc(d, out='motion,65535')
    assert d.sat_applied[S1]['OUT'] == 'verified'
    # a model upload the hub never answers: ends (the board isn't skipped for good)
    d.mlw_q['sat:' + S1] = ['MLW,1,0.5', 'MLW,commit,2']
    d.sat_q.pop(S1, None)
    d.sat_cmds[S1] = dict(line='MLW,begin,4,3', kind='MLW,begin,4,3', state='sent', at=time.time() - 1000, tries=1)
    d._sat_pump(S1, time.time())
    assert 'sat:' + S1 not in d.mlw_q, d.mlw_q
    # the sensor log reads every day of a week (not only the first, middle and last)
    log = SensorLog(tmp)
    now = time.time()
    for k in range(7):
        log._write(dict(key='hub', name='Hub', state='moving', t0=now - k * 86400 - 60, t1=now - k * 86400, conf=80,
                        grade='sure', why=[], by=[]))
    got = log.history(24 * 7, now=now + 1)
    assert len([e for e in got if e['state'] == 'moving']) >= 7, len(got)
    # link history: repeated names told apart
    with open(os.path.join(tmp, 'track.jsonl'), 'w') as f:
        for i in range(5):
            f.write(json.dumps({'t': now - 100 + i, 'links': {f'sat:{S1}>{D1}': 1.2,
                                                                f'sat:{S1}>{D2}': 0.2}, 'house': True}) + '\n')
    d.s.devices.name = lambda m: 'Sonos'
    w = d.wire_history(1)
    names = sorted(x['far'] for x in w['wires'])
    assert len(names) == 2 and names[0] != names[1] and all(n.startswith('Sonos') for n in names), names
    assert 1.0 in w['house'] and any(c for c in w['wires'][0]['cells'] if c)
    # the minute wires log (no map needed): written from the wires, read back first, names kept
    tmp2 = tempfile.mkdtemp()
    d2, _ = board(tmp2)
    view = dict(wires=[dict(a='sat:' + S1, b='r:' + R, state='moving'), dict(a='sat:' + S1, b='hub', state='quiet'),
                       dict(a='sat:' + S1, b=D9, state='off')],
                ends={'sat:' + S1: dict(name='Bedroom'), 'r:' + R: dict(name='Front router'), 'hub': dict(name='Hub')})
    t = now - 600
    for i in range(130):                                 # a bit over 2 minutes, twice a second
        d2._wires_log(view, t + i / 2)
    files = [f for f in os.listdir(tmp2) if f.startswith('wires-')]
    assert files, os.listdir(tmp2)
    w2 = d2.wire_history(1)
    rows = {x['far']: x for x in w2['wires']}
    assert set(rows) == {'Front router', 'Hub'} and rows['Front router']['board_name'] == 'Bedroom', rows.keys()
    assert max(c for c in rows['Front router']['cells'] if c is not None) == 1.0
    assert max(c for c in rows['Hub']['cells'] if c is not None) == 0.0
    print('SETTINGS SYNC TESTS PASSED')


if __name__ == '__main__':
    main()
