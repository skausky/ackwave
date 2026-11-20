#!/usr/bin/env python3
"""Is a second board worth it? Compares the boards' motion periods on the
timeline (sessions/timeline-*.jsonl): the primary board ('board') against
each other board ('sat:<mac>'), and the others pairwise.

For each pair A, B over the range:
  both      A's periods that B also saw (overlapping within --tol seconds)
  only A    A's periods B did not see; only B likewise
  blips     A's periods under --brief seconds, and how many B confirmed

    python tools/compare.py                 # the last 24 h
    python tools/compare.py --hours 168     # the last week

A board that adds coverage shows many "only B" periods; one that cuts false
blips confirms few of A's blips while confirming its long periods.
"""
import argparse
import json
import os
import sys
import time
from itertools import combinations

from timeline import Timeline

HERE = os.path.dirname(os.path.abspath(__file__))
TOL_S = 2.0
BRIEF_S = 2.0


def overlaps(p, others, tol):
    a, b = p[0], p[1]
    return any(q[0] <= b + tol and q[1] >= a - tol for q in others)


def compare(pa, pb, tol=TOL_S, brief=BRIEF_S):
    """Counts for two lists of (t0, t1) periods."""
    a_seen = [p for p in pa if overlaps(p, pb, tol)]
    b_seen = [p for p in pb if overlaps(p, pa, tol)]
    a_brief = [p for p in pa if p[1] - p[0] < brief]
    b_brief = [p for p in pb if p[1] - p[0] < brief]
    return dict(a=len(pa), b=len(pb), both_a=len(a_seen), both_b=len(b_seen),
                only_a=len(pa) - len(a_seen), only_b=len(pb) - len(b_seen),
                a_brief=len(a_brief), a_brief_confirmed=sum(overlaps(p, pb, tol) for p in a_brief),
                b_brief=len(b_brief), b_brief_confirmed=sum(overlaps(p, pa, tol) for p in b_brief),
                a_long=len(pa) - len(a_brief),
                a_long_confirmed=sum(overlaps(p, pb, tol) for p in pa if p[1] - p[0] >= brief))


def board_periods(spans):
    """{key: (name, [(t0, t1)])} for the boards' own verdicts."""
    out = {}
    for t0, t1, key, name in spans:
        if key == 'board' or key.startswith('sat:'):
            nm, ps = out.setdefault(key, ('Board' if key == 'board' else name, []))
            ps.append((t0, t1))
    return out


def report(spans, tol=TOL_S, brief=BRIEF_S):
    """Lines of text for every pair of boards."""
    boards = board_periods(spans)
    if len(boards) < 2:
        return [f"Only {len(boards)} board on the timeline in this range: nothing to compare."]
    keys = sorted(boards, key=lambda k: (k != 'board', k))
    lines = []
    for ka, kb in combinations(keys, 2):
        (na, pa), (nb, pb) = boards[ka], boards[kb]
        c = compare(pa, pb, tol, brief)
        lines += [f"{na} vs {nb}",
                  f"  periods        {na}: {c['a']:4d}   {nb}: {c['b']:4d}",
                  f"  seen by both   {c['both_a']:4d} of {na}'s, {c['both_b']} of {nb}'s",
                  f"  only {na:<10.10s}{c['only_a']:4d}",
                  f"  only {nb:<10.10s}{c['only_b']:4d}   (coverage {nb} adds)",
                  f"  {na}'s blips under {brief:g} s: {c['a_brief']}, confirmed by {nb}: {c['a_brief_confirmed']}",
                  f"  {na}'s longer periods: {c['a_long']}, confirmed by {nb}: {c['a_long_confirmed']}",
                  ""]
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--hours', type=float, default=24.0)
    ap.add_argument('--sessions', default=os.path.join(os.path.dirname(HERE), 'sessions'))
    ap.add_argument('--tol', type=float, default=TOL_S, help='seconds two periods may be apart and still match')
    ap.add_argument('--brief', type=float, default=BRIEF_S, help='periods shorter than this are blips')
    a = ap.parse_args()
    names = {}
    try:
        with open(os.path.join(a.sessions, 'dashboard.json')) as f:
            names = json.load(f).get('board_names') or {}
    except (OSError, ValueError):
        pass
    now = time.time()
    spans = [(t0, t1, k, names.get(k[4:], nm) if k.startswith('sat:') else nm)
             for t0, t1, k, nm in Timeline(a.sessions).read(now - a.hours * 3600, now)]
    print(f"Last {a.hours:g} h, periods matched within {a.tol:g} s\n")
    print('\n'.join(report(spans, a.tol, a.brief)))


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
