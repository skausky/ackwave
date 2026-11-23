"""Phone notifications (notify.py): one message per stretch of motion, the
daily cap, the hours, and keys never shown on the page."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify import Notify, in_hours  # noqa: E402

TOK, USR = 'a' * 30, 'b' * 30
fails = 0


def check(name, ok):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name)
    fails += not ok


def make(**cfg):
    sent, now = [], [100000.0]
    n = Notify(dict(dict(enabled=True, token=TOK, user=USR), **cfg), clock=lambda: now[0],
               call=lambda t, u, ti, m, p: (sent.append((m, p)) or (True, 'ok', 9000)), run=lambda f: f(),
               localtime=time.gmtime)
    return n, sent, now


def test_one_per_stretch():
    n, sent, now = make(quiet_min=10)
    n.update(True, 'board 3')
    for _ in range(50):                       # a busy evening: blips on and off
        now[0] += 30; n.update(False); now[0] += 5; n.update(True, 'board 4')
    check('a busy stretch is one message', len(sent) == 1 and sent[0][0] == 'Motion · board 3')
    n.update(False); now[0] += 601; n.update(False); n.update(True, 'board 2')
    check('after 10 min of quiet, a new one', len(sent) == 2 and sent[1][0] == 'Motion · board 2')


def test_cap_and_hours():
    n, sent, now = make(quiet_min=0, max_day=3)
    for _ in range(10):
        n.update(True); now[0] += 1; n.update(False); now[0] += 1
    check('daily cap holds', len(sent) == 3)
    check('night hours wrap midnight', in_hours([22, 7], 23) and in_hours([22, 7], 3) and not in_hours([22, 7], 12))
    n, sent, now = make(quiet_min=0, hours=[22, 7])
    now[0] = 12 * 3600.0                      # noon (UTC clock in the test)
    n.update(True)
    check('outside the hours: nothing', not sent)


def test_settings_and_view():
    n = Notify()
    try:
        n.set(enabled=True)
        check('needs keys before turning on', False)
    except ValueError:
        check('needs keys before turning on', True)
    try:
        n.set(token='short')
        check('bad token refused', False)
    except ValueError:
        check('bad token refused', True)
    n.set(token=TOK, user=USR, enabled=True, urgent=True)
    v = json.dumps(n.view())
    check('keys never in the page payload', TOK not in v and USR not in v and n.view()['has_token'])
    n2, sent, now = make(urgent=True)
    n2.update(True)
    check('high priority sent as 1', sent and sent[0][1] == 1)
    n3, sent3, now3 = make(zones_only=True)
    n3.update(True, 'board 2')
    check('zones only: no message for motion elsewhere', not sent3)
    n3.zone('Someone in Target house')
    check('zones only: a zone still notifies', len(sent3) == 1)


def test_dashboard_keeps_keys_private():
    from web import Dashboard, State
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 'dashboard.json')
    d = Dashboard(State(60), send=lambda _c: None, settings=path,
                  notify_call=lambda *a: (True, 'ok', None), light_run=lambda f: f())
    ok, _msg = d.command(dict(cmd='notify', token=TOK, user=USR, enabled=True, hours=[22, 7]))
    check('notify command saves', ok and d.notify.cfg['hours'] == [22, 7])
    ok, _msg = d.command(dict(cmd='notify', hours='always'))
    check('any time clears the hours', ok and d.notify.cfg['hours'] is None)
    check('settings file is owner-only', os.stat(path).st_mode & 0o077 == 0)
    d2 = Dashboard(State(60), settings=path)
    check('keys survive a restart', d2.notify.cfg['token'] == TOK and d2.notify.cfg['enabled'])


test_one_per_stretch()
test_cap_and_hours()
test_settings_and_view()
test_dashboard_keeps_keys_private()
if fails:
    sys.exit(f'{fails} FAILED')
print('NOTIFY TESTS PASSED')
