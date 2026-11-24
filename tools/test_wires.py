"""wires.py: which end a movement is near, from which wires share an end (no map)."""
from wires import HOLD_S, Wires, headline

B1, B2, B3, R1, R2 = 'b1', 'b2', 'b3', 'r1', 'r2'
ENDS = {B1: dict(kind='board', name='bedroom'), B2: dict(kind='board', name='stairs'),
        B3: dict(kind='board', name='bathroom'), R1: dict(kind='router', name='front router'),
        R2: dict(kind='router', name='back router')}


def grid(moving, off=()):
    """3 boards x 2 routers (the house now: Devices off, router links only)."""
    return [dict(a=b, b=r, moving=None if (b, r) in off else (b, r) in moving, level=1.2 if (b, r) in moving else 0.2)
            for b in (B1, B2, B3) for r in (R1, R2)]


def by(view):
    return {(w['a'], w['b']): w for w in view['wires']}


def main():
    t = 1000.0
    # near the front router: every board's wire to it reacts, the back one's don't
    v = Wires().update(grid({(B1, R1), (B2, R1), (B3, R1)}), ENDS, t)
    assert v['ends'][R1]['hot'] and not any(v['ends'][b]['hot'] for b in (B1, B2, B3)), v['ends']
    assert all(by(v)[(b, R1)]['where'] == 'b' for b in (B1, B2, B3)), v['wires']
    assert v['words'][0]['kind'] == 'near' and 'front router' in v['words'][0]['text'] and len(v['words']) == 1, v['words']
    assert headline(v)[0] == 'over'
    # near the bedroom board: both of its wires react, nothing else
    v = Wires().update(grid({(B1, R1), (B1, R2)}), ENDS, t)
    assert v['ends'][B1]['hot'] and by(v)[(B1, R1)]['where'] == 'a', v['wires']
    assert headline(v) == ('yours', 'Movement by your boards')
    # between bedroom and the front router: only that wire
    v = Wires().update(grid({(B1, R1)}), ENDS, t)
    assert by(v)[(B1, R1)]['where'] == 'between' and by(v)[(B1, R1)]['span'] == (0.25, 0.75)
    assert 'only that wire' in v['words'][0]['text'] and headline(v)[0] == 'over'
    # one wire alone is never 'near' an end (a spike)
    assert not any(e['hot'] for e in v['ends'].values())
    # an end with no other live wire can't tell: the bedroom's back-router wire isn't answering
    v = Wires().update([dict(a=B1, b=R1, moving=True, level=1.3), dict(a=B1, b=R2, moving=None, level=0),
                        dict(a=B2, b=R1, moving=False, level=0.1)], ENDS, t)
    assert by(v)[(B1, R1)]['where'] == 'a_or_between', by(v)[(B1, R1)]
    assert by(v)[(B1, R2)]['state'] == 'off'
    # wires react a little apart: one that moved 2 s ago still counts, and is drawn as 'recent'
    w = Wires()
    w.update(grid({(B2, R1)}), ENDS, t)
    v = w.update(grid({(B1, R1), (B3, R1)}), ENDS, t + 2)
    assert v['ends'][R1]['react'] == 3 and by(v)[(B2, R1)]['state'] == 'recent', v['ends'][R1]
    v = w.update(grid(set()), ENDS, t + 2 + HOLD_S + 1)
    assert headline(v) == ('quiet', 'All wires quiet') and not v['words']
    # a level close to its threshold counts as reacting (weak movement at the far end)
    g = grid({(B1, R1), (B2, R1)})
    next(x for x in g if (x['a'], x['b']) == (B3, R1))['level'] = 0.95
    v = Wires().update(g, ENDS, t)
    assert v['ends'][R1]['react'] == 3 and by(v)[(B3, R1)]['state'] == 'quiet', "counts for the end, not lit"
    v = Wires().update([dict(x, level=0.95, moving=False) for x in grid(set())], ENDS, t)
    assert not any(e['hot'] for e in v['ends'].values()), "near threshold everywhere but nothing moved: not hot"
    # nothing answering
    v = Wires().update(grid(set(), off={(b, r) for b in (B1, B2, B3) for r in (R1, R2)}), ENDS, t)
    assert headline(v)[0] == 'none'
    print('WIRES TESTS PASSED')


if __name__ == '__main__':
    main()
