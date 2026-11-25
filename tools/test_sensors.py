"""sensors.py: each board's periods (moving / still / empty / quiet / weak / gone), how sure, when they stopped."""
import os
import tempfile

import sensors
from sensors import SensorLog, judge


def b(state, **kw):
    d = dict(key='sat:x', name='bedroom back', state=state, own=False, by=[], wires=[{}, {}, {}], model=False,
             p=None, level=0.5, weak=False, acks=90, router_rssi=-60, breathing=None)
    d.update(kw)
    return d


def main():
    # confidence: honest about what it rests on
    st, c, why = judge(b('moving', own=True, level=2.0, by=['Router 1', 'Sonos'], model=True, p=[0, 10, 90]), 5)
    assert st == 'moving' and c > 0.9 and sensors.grade(c) == 'sure', (c, why)
    st, c, why = judge(b('moving', by=['Router 1']), 5)
    assert st == 'moving' and c <= 0.5 and 'one link alone' in why, (c, why)
    st, c, _ = judge(b('moving', own=True, level=1.0, weak=True), 5)
    assert c < 0.55, "a weak link halves the confidence"
    assert judge(b('quiet', model=True, p=[85, 10, 5]))[0] == 'empty'
    assert judge(b('quiet', model=True, p=[5, 90, 5]))[0] == 'still'
    st, c, why = judge(b('quiet'))
    assert st == 'quiet' and 'unknown' in why[0], why
    assert judge(b('quiet', weak=True))[0] == 'weak'
    assert judge(b('quiet', breathing='clear'))[0] == 'still'
    assert judge(b('gone'))[0] == 'gone'

    tmp = tempfile.mkdtemp()
    log = SensorLog(tmp)
    t = 1_000_000.0
    feed = lambda bs, t: log.update({'sat:x': bs}, t)
    assert feed(b('quiet'), t)[0][0] == 'open'
    for i in range(10):                                   # 5 s of movement with a 2 s gap: one period
        t += 0.5
        feed(b('moving' if not 4 <= i <= 7 else 'quiet', own=True, level=1.6), t)
    assert log.cur['sat:x']['state'] == 'moving'
    closed = []
    for i in range(10):                                   # then quiet: it ends after END_S
        t += 0.5
        closed += [e for k, e in feed(b('quiet'), t) if k == 'close']
    assert len(closed) == 1 and closed[0]['state'] == 'moving', closed
    e = closed[0]
    assert abs(e['t1'] - e['t0'] - 4.5) < 0.6, e          # from its first to its last moving report
    assert e['grade'] in ('likely', 'sure') and 'own verdict' in e['why'][0], e
    assert ' → ' in SensorLog.text(e) and 'bedroom back: moving' in SensorLog.text(e)
    # quiet -> still only once it held SETTLE_S; a blip of 'still' doesn't count
    for i in range(10):
        t += 1
        feed(b('quiet', model=True, p=[0, 90, 10]) if i < 3 else b('quiet'), t)
    assert log.cur['sat:x']['state'] == 'quiet', "3 s of 'still' is not a period"
    for i in range(25):
        t += 1
        feed(b('quiet', model=True, p=[0, 90, 10]), t)
    assert log.cur['sat:x']['state'] == 'still'
    # gone: said at once
    got = feed(b('gone'), t + 1)
    assert [k for k, _ in got] == ['close', 'open'] and log.cur['sat:x']['state'] == 'gone'
    # the files: what the Log page reads
    files = [f for f in os.listdir(tmp) if f.startswith('sensors-')]
    assert files, files
    h = SensorLog(tmp).history(48, now=t + 2)
    assert [x['state'] for x in h][:3] == ['still', 'quiet', 'moving'], [x['state'] for x in h]
    v = log.view(t + 2)
    assert v['now'][0]['state'] == 'gone' and v['log'][0]['state'] == 'still'
    print('SENSORS TESTS PASSED')


if __name__ == '__main__':
    main()
