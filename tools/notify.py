"""Phone notifications through Pushover (https://pushover.net): one message when
the house goes from still to motion, and optionally when a board stops being
heard. Pushover's iPhone app is a one-time purchase; an application may send
10,000 messages a month, and this sends far fewer:

  - one message per stretch of motion: a new one only after the house has
    been still for `quiet_min` minutes (a busy evening is one message, not 300)
  - at most `max_day` messages a day, whatever happens
  - optionally only between two hours (e.g. 23 to 7: night watch)

The application token and user key are secrets: kept in dashboard.json (owner
only), never shown, logged or put in a message. Calls run in a thread, so the
dashboard never waits on the internet.
"""
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.pushover.net/1/messages.json'
KEY_RE = re.compile(r'[A-Za-z0-9]{20,40}')
DEFAULTS = dict(enabled=False, token='', user='', quiet_min=10, max_day=50, hours=None,
                urgent=False, offline=True, zones_only=False)
QUIET_MIN = (0, 240)
MAX_DAY = (1, 500)
TIMEOUT_S = 10.0


def pushover_call(token, user, title, message, priority=0):
    """POST one message; (ok, short text, remaining this month or None). The
    reply never holds the keys, and neither does the text returned."""
    body = urllib.parse.urlencode(dict(token=token, user=user, title=title[:250], message=message[:1024],
                                       priority=str(priority))).encode()
    req = urllib.request.Request(API, data=body, headers={'User-Agent': 'ackwave'})
    left = None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            code, raw = r.status, r.read(4096)
            left = r.headers.get('X-Limit-App-Remaining')
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read(4096) if e.fp else b''
    except http.client.HTTPException:
        return False, 'no reply (bad answer)', None
    except (urllib.error.URLError, OSError) as e:
        return False, f"no reply ({str(getattr(e, 'reason', e))[:80]})", None
    try:
        j = json.loads(raw or b'null')
    except ValueError:
        j = None
    if code == 200 and isinstance(j, dict) and j.get('status') == 1:
        return True, 'delivered to Pushover', int(left) if left and left.isdigit() else None
    errs = j.get('errors') if isinstance(j, dict) else None
    why = '; '.join(str(x) for x in errs)[:120] if isinstance(errs, list) and errs else f'HTTP {code}'
    return False, why, None


def in_hours(hours, hour):
    """hours = None (always) or [from, to] in whole hours; to < from wraps midnight."""
    if not hours:
        return True
    a, b = hours
    return a <= hour < b if a < b else hour >= a or hour < b


class Notify:
    def __init__(self, cfg=None, clock=time.time, call=pushover_call, run=None, localtime=time.localtime):
        self.cfg = dict(DEFAULTS)
        try:
            self.set(**{k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
        except (ValueError, TypeError):
            pass                                  # damaged settings: start off
        self.clock, self.call, self.localtime = clock, call, localtime
        self.run = run or (lambda f: threading.Thread(target=f, daemon=True).start())
        self.lock = threading.RLock()
        self.motion = False
        self.still_since = None                   # when the house went still
        self.armed = True                         # the next motion start may send
        self.day, self.sent_day = None, 0
        self.last = None                          # dict(t, ok, msg, text)
        self.left = None                          # Pushover: messages left this month

    # ---------- settings ----------
    def set(self, enabled=None, token=None, user=None, quiet_min=None, max_day=None, hours=None,
            urgent=None, offline=None, clear_hours=False, zones_only=None):
        new = dict(self.cfg)
        for k, v in (('token', token), ('user', user)):
            if v is not None:
                v = v.strip()
                if v and not KEY_RE.fullmatch(v):
                    raise ValueError(f"that doesn't look like a Pushover {'API token' if k == 'token' else 'user key'}"
                                     ' (30 letters and digits)')
                new[k] = v
        for k, v, (lo, hi) in (('quiet_min', quiet_min, QUIET_MIN), ('max_day', max_day, MAX_DAY)):
            if v is not None:
                if isinstance(v, bool) or int(v) != v or not lo <= int(v) <= hi:
                    raise ValueError(f'{k}: {lo} to {hi}')
                new[k] = int(v)
        if clear_hours:
            new['hours'] = None
        elif hours is not None:
            if (not isinstance(hours, (list, tuple)) or len(hours) != 2 or
                    any(isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23 for h in hours)):
                raise ValueError('bad hours')
            new['hours'] = None if hours[0] == hours[1] else [hours[0], hours[1]]
        for k, v in (('urgent', urgent), ('offline', offline), ('enabled', enabled), ('zones_only', zones_only)):
            if v is not None:
                new[k] = bool(v)
        if new['enabled'] and not (new['token'] and new['user']):
            raise ValueError('add the API token and your user key first')
        self.cfg = new

    def config(self):
        return dict(self.cfg)

    # ---------- the rule ----------
    def _count_ok(self, now):
        day = time.strftime('%Y-%m-%d', self.localtime(now))
        if day != self.day:
            self.day, self.sent_day = day, 0
        return self.sent_day < self.cfg['max_day']

    def update(self, motion, where=''):
        """The house verdict, often. Sends when motion starts after a quiet
        stretch, inside the hours. Returns the text sent, or None."""
        if not self.cfg['enabled'] or self.cfg['zones_only']:   # zones only: their own messages (zone())
            self.motion = motion
            return None
        now = self.clock()
        with self.lock:
            if not motion:
                if self.motion or self.still_since is None:
                    self.still_since = now
                self.motion = False
                if not self.armed and now - self.still_since >= self.cfg['quiet_min'] * 60:
                    self.armed = True
                return None
            started = not self.motion
            self.motion = True
            if not (started and self.armed):
                return None
            if not in_hours(self.cfg['hours'], self.localtime(now).tm_hour) or not self._count_ok(now):
                return None
            self.armed = False
            text = f"Motion{' · ' + where if where else ''}"
            self._send(text, now)
            return text

    def zone(self, text, night_only=False):
        """Someone walked into a zone that notifies. Within the hours (and only
        22:00-7:00 when night_only), under the daily cap."""
        if not self.cfg['enabled']:
            return None
        now = self.clock()
        h = self.localtime(now).tm_hour
        if not in_hours(self.cfg['hours'], h) or (night_only and not in_hours([22, 7], h)):
            return None
        with self.lock:
            if not self._count_ok(now):
                return None
            self._send(text, now)
        return text

    def board_offline(self, text):
        if not (self.cfg['enabled'] and self.cfg['offline']):
            return None
        now = self.clock()
        with self.lock:
            if not self._count_ok(now):
                return None
            self._send(text, now, urgent=False)
        return text

    def test(self):
        if not (self.cfg['token'] and self.cfg['user']):
            return False, 'add the API token and your user key first'
        with self.lock:
            self._send('Test from ackwave: notifications work', self.clock(), count=False)
        return True, 'Sent a test (the result shows on the card in a few seconds)'

    def _send(self, text, now, urgent=None, count=True):
        if count:
            self.sent_day += 1
        token, user = self.cfg['token'], self.cfg['user']
        pri = 1 if (self.cfg['urgent'] if urgent is None else urgent) else 0

        def go():
            ok, msg, left = self.call(token, user, 'ackwave', text, pri)
            with self.lock:
                self.last = dict(t=self.clock(), ok=ok, msg=msg, text=text)
                if left is not None:
                    self.left = left
        self.run(go)

    # ---------- for the page ----------
    def view(self):
        with self.lock:
            c = self.cfg
            return dict(enabled=c['enabled'], has_token=bool(c['token']), has_user=bool(c['user']),
                        quiet_min=c['quiet_min'], max_day=c['max_day'], hours=c['hours'],
                        urgent=c['urgent'], offline=c['offline'], zones_only=c['zones_only'],
                        sent_today=self.sent_day if self.day == time.strftime('%Y-%m-%d', self.localtime(self.clock())) else 0,
                        left=self.left, last=dict(self.last) if self.last else None)
