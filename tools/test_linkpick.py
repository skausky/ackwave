"""linkpick.py: every slot used; links that see 'over there' kept, your-side and
noisy ones swapped out; your picks pinned on every board."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import linkpick as L  # noqa: E402


def main():
    t = [0.0]
    pk = L.LinkPicker(clock=lambda: t[0])
    heard = {f'd{i}': -60 - i for i in range(7)}
    linked = set()

    def run(n=1):
        for _ in range(n):
            a = pk.choose('b', heard, linked)
            if a:
                (linked.add if a[0] == 'poke' else linked.discard)(a[1])
            t[0] += 5
    run(10)
    assert len(linked) == L.SLOTS, linked                     # every slot used
    good = {'d5', 'd6'}
    # an hour: movement over there seen by d5/d6 only; your side seen by everything else
    for s in range(3600):
        over, mine = s % 10 < 3, 3 <= s % 10 < 5
        pk.observe({('b', d): (d in good if over else mine) for d in heard}, over, mine)
        if s % 5 == 0:
            run()
    assert good <= linked, (linked, [(d, round(sc, 2)) for _, d, sc, _ in pk.ranking('b')])
    print('kept:', sorted(linked))
    # your pick: always on
    pin = next(d for d in sorted(heard) if d not in linked)
    pk.trial.clear()
    a = pk.choose('b', heard, linked, pinned={pin})
    assert a and a[0] == 'unpoke' and a[1] not in good, a      # room made for it, never a good one
    linked.discard(a[1])
    assert pk.choose('b', heard, linked, pinned={pin}) == ('poke', pin)
    j = pk.to_json()
    pk2 = L.LinkPicker()
    pk2.from_json(j)
    assert abs(pk2.stats[('b', 'd5')].score() - pk.stats[('b', 'd5')].score()) < 1e-6
    print('LINKPICK TESTS PASSED')


if __name__ == '__main__':
    main()
