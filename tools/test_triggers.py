"""triggers.py: alert rules by board, how many, and where."""
from triggers import HOLD_S, TOL_S, Triggers, clean_rule, migrate

def mac(n):
    return ':'.join(['aa'] + ['00'] * 4 + [f'{n:02x}'])   # built here: no address written out in the source


S1, S2, S3 = ('sat:' + mac(n) for n in (1, 2, 3))


def mv(*keys, near=()):
    return {k: dict(moving=k in keys, near_me=k in near) for k in (S1, S2, S3, 'hub')}


def main():
    only1 = clean_rule(dict(id='a', name='Board 1', boards=[S1], need='any', notify=True))
    one3 = clean_rule(dict(id='b', boards=[S1, S3], need='all', light=True))
    all3 = clean_rule(dict(id='c', boards=[S1, S2, S3], need='all'))
    two = clean_rule(dict(id='d', boards=[], need=2))
    over = clean_rule(dict(id='e', boards=[S2], where='over'))
    t = Triggers([only1, one3, all3, two, over])
    on = lambda res: sorted(r['id'] for r, _s in res)
    assert on(t.update(mv(), 0)) == []
    assert on(t.update(mv(S2), 1)) == ['e'], "board 2 alone: only its rule"
    res = t.update(mv(S1), 2)
    assert on(res) == ['a', 'd', 'e'] and dict((r['id'], s) for r, s in res)['a'], "board 1: its rule starts; 2 within 3 s: 'any two'"
    assert on(t.update(mv(S3), 3)) == ['a', 'b', 'c', 'd', 'e'], "1 and 3 a second apart: both; 2, 1, 3 within 3 s: all 3"
    t4 = Triggers([all3])
    t4.update(mv(S1), 0)
    t4.update(mv(S2), 1)
    assert t4.update(mv(S3), 1 + TOL_S + 0.5) == [], "board 1 too long ago: not all 3"
    assert not any(s for _r, s in t.update(mv(S1), 5)), "already on: no new start"
    later = 5 + TOL_S + HOLD_S + 1
    assert on(t.update(mv(), later)) == [], "all quiet after the hold"
    # where: near board 2 itself does not set off an 'over there' rule
    assert 'e' not in on(t.update(mv(S2, near=(S2,)), later + 1))
    # 'all' of a board not heard never fires
    t2 = Triggers([clean_rule(dict(id='x', boards=[S1, S2], need='all'))])
    b = mv(S1)
    del b[S2]
    assert t2.update(b, 0) == []
    # a rule switched off never fires
    t3 = Triggers([clean_rule(dict(id='y', boards=[S1], on=False))])
    assert t3.update(mv(S1), 0) == []
    # bad input
    for bad in (dict(boards='x'), dict(boards=['sat:zz']), dict(need=0), dict(need=True), dict(where='attic'), dict(id='A B')):
        try:
            clean_rule(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    # the old settings become the first rule
    r = migrate(dict(house_off=[mac(2)], paired=[mac(1), mac(2)], house_rule='two'),
                [dict(watch=True, notify=True)])[0]
    assert r['boards'] == ['hub', S1] and r['need'] == 2 and r['where'] == 'over' and r['notify'] and r['light'], r
    assert migrate({}, [])[0]['boards'] == [] and migrate({}, [])[0]['where'] == 'any'
    print('TRIGGERS TESTS PASSED')


if __name__ == '__main__':
    main()
