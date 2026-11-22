"""mltrain.py and the dashboard's 'Teach the boards': labelled seconds of any
board's features, a trained model, and its weights sent one step at a time."""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mltrain  # noqa: E402
from mlfeat import Model  # noqa: E402


def feat(label, r, shift=0.0):
    base = {'empty': [-3.0, -3.0, 1.05, 0.05, 0.04, 0.55, 0.03, 0.3],
            'still': [-2.7, -2.7, 1.10, 0.25, 0.06, 0.45, 0.05, 0.5],
            'moving': [-1.6, -1.7, 1.35, 0.70, 0.20, 0.20, 0.14, 1.6]}[label]
    f = np.array(base) + r.normal(0, 0.05, 8)
    f[:2] += shift                                  # another board: another quiet level
    return [float(x) for x in f]


def main():
    r = np.random.default_rng(0)
    rows = []
    for b, shift in (('hub', 0.0), ('sat:x', 0.6)):
        for lab in mltrain.LABELS:
            for k in range(40):
                rows.append(dict(t=1000.0 + 60 * mltrain.LABELS.index(lab) + k + (300 if b != 'hub' else 0),
                                 board=b, label=lab, f=feat(lab, r, shift)))
    m = mltrain.train(rows)
    assert m['no'] == 3 and m['nh'] == 0 and len(m['params']) == 18 + 8 * 3 + 3, m.get('error')
    assert all(v >= 0.9 for v in m['score'].values()), m['score']
    model = Model(m['params'], 0, 3)
    for lab in mltrain.LABELS:                       # the board's own shift makes both boards agree
        p = model.forward(feat(lab, r, 0.6), quiet=np.array([-3.0 + 0.6, -3.0 + 0.6]))
        assert int(np.argmax(p)) == mltrain.LABELS.index(lab), (lab, p)
    two = mltrain.train([x for x in rows if x['label'] != 'still'][:60] + [x for x in rows if x['label'] == 'moving'])
    assert two['no'] == 1 and 'moving' in two['score'], two
    assert 'error' in mltrain.train(rows[:10])
    cmds = mltrain.mlw_commands(m)
    assert cmds[0] == 'MLW,begin,0,3' and cmds[-1] == f"MLW,commit,{len(m['params'])}"
    assert all(len(c) <= 120 for c in cmds), "each fits an order from the air"

    # the dashboard: record, train, send (hub by its replies, a board through the hub)
    from web import Dashboard, State
    tmp = tempfile.mkdtemp()
    sent = []
    d = Dashboard(State(60), send=sent.append, sessions_dir=tmp, settings=os.path.join(tmp, 'dashboard.json'))
    HUB = ':'.join(['24', '58', '7c', '00', '00', '01'])
    SAT = ':'.join(['c4', '9e', '7e', '00', '00', '02'])
    d.feed(f'ME,{HUB}\n')
    d.feed(f'SAT,{SAT},-40,1,2,20,15,96,-57,2,60,{":".join(["aa"] * 6)},6\n')
    for lab in mltrain.LABELS:
        for who, shift in ((HUB, 0.0), (SAT, 0.6)):
            key = 'hub' if who == HUB else 'sat:' + SAT
            d.feed(f"FEAT,{who},{','.join(f'{x:.3f}' for x in feat('empty', r, shift))}\n")
            ok, msg = d.command(dict(cmd='teach', which=key, label=lab, secs=30))
            assert ok, msg
            for _ in range(25):
                d.feed(f"FEAT,{who},{','.join(f'{x:.3f}' for x in feat(lab, r, shift))}\n")
    d.feed(f'ML,{HUB},1,5,10,85\n')
    v = d._teach_view()
    assert v['counts']['hub']['moving'] == 25 and v['counts']['sat:' + SAT]['empty'] == 25, v['counts']
    assert v['boards'][0]['key'] == 'hub' and v['boards'][0]['ml']['p'] == [5, 10, 85]
    sent.clear()
    ok, msg = d.command(dict(cmd='teach_train'))
    assert ok and 'sending to' in msg, msg
    assert sent[0] == 'MLW,begin,0,3' and f'SATCMD,{SAT},MLW,begin,0,3' in sent, sent
    n_hub = len(mltrain.mlw_commands(d.teach_model))
    for i in range(n_hub - 1):                      # the hub answers each step; the next goes
        d.feed('MLW,got,8\n' if i < n_hub - 2 else 'MLW,got,45\n')
    assert sent[-1].startswith('MLW,commit,') and 'hub' not in d.mlw_q, sent[-3:]
    for i in range(n_hub - 1):                      # the board confirms each order through the hub
        d.feed(f'SATCMD,{SAT},done,{i + 1}\n')
    assert any(c.startswith(f'SATCMD,{SAT},MLW,commit,') for c in sent) and not d.mlw_q, d.mlw_q
    assert not d.command(dict(cmd='teach', which='sat:nope', label='moving'))[0]
    print('MLTRAIN TESTS PASSED')


if __name__ == '__main__':
    main()
