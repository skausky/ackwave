#!/usr/bin/env python3
"""Headless test of gui.py: state tracking, reboot/loop handling, and drawing."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

from fake_csi import generate  # noqa: E402
from gui import State, View  # noqa: E402


def main():
    s = State(window_s=60)
    lines = [line for line, *_ in generate(seed=2)]
    view = View(s, plt)
    saw_calibrating = False
    for i, line in enumerate(lines):
        s.feed(line)
        saw_calibrating |= s.det.floor <= 0 and s.n_lines > 30
        if i % 400 == 0:
            view.draw()                       # draws at every stage without error
    assert saw_calibrating and s.cal_spans, "calibration period not tracked"
    assert len(s.nodes) == 2 and s.channel == (11, 'csa')
    assert s.det.external and s.board_calib and s.det.floor > 0, "board floor not adopted"
    assert abs(s.det.floor - s.board_calib['floor']) < 1e-12
    assert s.board_motion is False and len(s.board_states) == 4      # 2 motions
    assert len(s.motion_spans) == 2 and all(b is not None for _a, b in s.motion_spans)
    # PC verdict matches the board's (same floor): same number of changes
    assert set(s.scores) == {(0, 0), (0, 1), (1, 0), (1, 1)} and s.long
    assert max(v for _t, v in s.long) > s.det.c['on_long'], "long score never crossed"
    assert 35 < s.rate((0, 1)) < 45 and 8 < s.rate((0, 0)) < 12, "packet rates"
    assert s.stats and s.stats['poke'] > 0 and s.hops

    # Session history on the wall clock: per-second peaks, both motion periods
    assert len(s.hist_motion) == 2 and all(b is not None for _a, b in s.hist_motion)
    assert s.session_counts() == (2, 0)
    assert len(s.hist_t) == len(s.hist_s) == len(s.hist_l) and len(s.hist_t) > 30
    assert all(b > a for a, b in zip(s.hist_t, s.hist_t[1:])), "one bin per second, in order"
    assert max(s.hist_s) > s.det.c['on_short'] and s.hist_cal
    # Header: recent-motion list, time since last motion, link health
    rec = s.recent_motion()
    assert len(rec) == 2 and rec[0][0] > rec[1][0], "newest first"
    assert all(dur >= 2 and peak > s.det.c['on_short'] and not going
               for _a, dur, peak, going in rec), rec
    assert s.last_motion().startswith('last motion') and s.last_motion().endswith('ago')
    word, _col, detail = s.link_health()
    assert word == 'WEAK' and 'router ACK' in detail, (word, detail)   # demo: 40 ACKs/s
    view.draw()
    assert 'peak x' in view.recent.get_text() and 'link WEAK' in view.health.get_text()
    view.details = True
    view.draw()
    assert 'pokes' in view.info.get_text(), "details show the raw counters"
    view.details = False
    # Doppler: spectra kept, header line, both bottom views draw
    assert len(s.dop.rows) > 20 and s.doppler_text().startswith('Doppler')
    assert view.bottom == 'doppler'
    view.draw()
    assert 'Doppler' in view.ax_wf.get_title(loc='left') and 'Doppler' in view.info.get_text()
    view.bottom = 'subcarriers'
    view.draw()
    assert 'normalised amplitude' in view.ax_wf.get_title(loc='left')
    view.bottom = 'doppler'
    w_end = s.last_wall
    # Window buttons and pause: drawing at each window works; paused = no redraw
    for w in (120.0, 600.0, 30.0):
        view.set_window(w)
        assert view.view_s == w
    view.toggle_pause()
    lim = view.ax_score.get_xlim()
    for line in lines[-50:]:
        s.feed(line)
    view.draw()
    assert view.paused and view.ax_score.get_xlim() == lim, "paused view moved"
    view.toggle_pause()
    assert not view.paused

    # Board reboot / looping demo: timestamps jump back -> clean restart
    for line in lines[:300]:
        s.feed(line)
    assert s.now < 3 and not s.hops and len(s.nodes) == 2
    assert s.last_wall > w_end and len(s.hist_motion) == 2, "session history kept, clock monotonic"
    assert all(t <= 3 for dq in s.scores.values() for t, _ in dq)
    view.draw()
    print("GUI TESTS PASSED")


if __name__ == '__main__':
    main()
