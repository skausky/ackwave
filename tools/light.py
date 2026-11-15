"""Light on motion: calls a web hook when motion starts, and another once the
room has been still for a while. Made for smart plugs that only a cloud can
switch (an Amazon Smart Plug: Voice Monkey turns each URL into an Alexa
Routine), but any URL that switches something works (Home Assistant, IFTTT...).

Only edges are sent: one call when motion starts, one when it has been still
for `off_after` seconds. A light it didn't switch on is never switched off by
it. Calls run in a thread so the dashboard never waits on the internet, and a
failed call is retried at most every RETRY_S while the state still wants it.
URLs carry secret tokens, so they are never shown, logged or put in messages:
the page only sees which host each one goes to.
"""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULTS = {'enabled': False, 'on_url': '', 'off_url': '', 'off_after': 120}
OFF_AFTER = (0, 3600)           # allowed seconds of stillness before "off" (0 = as soon as still)
RETRY_S = 30.0                  # a failed call is tried again at most this often
TIMEOUT_S = 10.0
MAX_URL = 2000


def url_ok(u):
    """'' (none) or an http(s) URL with a host, no whitespace and no control
    characters (the HTTP library's error for those would quote the URL)."""
    if u == '':
        return True
    if not isinstance(u, str) or len(u) > MAX_URL or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in u):
        return False
    p = urlparse(u)
    return p.scheme in ('http', 'https') and bool(p.hostname)


def http_call(url):
    """GET the URL; (ok, short message). Understands Voice Monkey's JSON replies
    ({"success": false, "error": ...}) and never puts the URL in the message."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ackwave'})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            code, body = r.status, r.read(4096)
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read(4096) if e.fp else b''
    except (ValueError, http.client.InvalidURL):  # its text can quote the URL (and the token)
        return False, "no reply (bad URL)"
    except http.client.HTTPException:            # a broken reply
        return False, "no reply (bad answer)"
    except (urllib.error.URLError, OSError) as e:
        why = getattr(e, 'reason', e)
        return False, f"no reply ({str(why)[:80]})"
    err = None
    try:
        j = json.loads(body or b'null')
        if isinstance(j, dict):
            if j.get('success') is False or j.get('error'):   # "error": null is fine
                err = str(j.get('error') or 'refused')[:80]
    except ValueError:
        pass
    if 200 <= code < 300 and not err:
        return True, f"HTTP {code}"
    return False, f"HTTP {code}" + (f": {err}" if err else '')


class Light:
    """Turns the motion verdict into on/off calls. update() is cheap and may be
    called for every packet; it returns 'on' / 'off' when it starts a call."""

    def __init__(self, cfg=None, clock=time.time, call=http_call, run=None):
        self.cfg = dict(DEFAULTS)
        try:
            self.set(**{k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
        except (ValueError, TypeError):
            pass                                 # damaged settings: start off
        self.clock, self.call = clock, call
        # How to run a call: a thread by default; tests run it inline
        self.run = run or (lambda f: threading.Thread(target=f, daemon=True).start())
        self.lock = threading.RLock()            # re-entrant: tests run calls inline
        self.on = None                           # what we last switched it to; None = unknown
        self.still_since = None                  # when the room went still (None = motion)
        self.busy = False                        # a call is in flight
        self.retry_at = 0.0
        self.last = None                         # dict(t, what, ok, msg) of the last call

    # ---------- settings ----------
    def set(self, enabled=None, on_url=None, off_url=None, off_after=None):
        """Change some settings (None = keep). Raises ValueError on a bad one."""
        new = dict(self.cfg)
        for k, v in (('on_url', on_url), ('off_url', off_url)):
            if v is not None:
                if not url_ok(v):
                    raise ValueError(f"bad {'on' if k == 'on_url' else 'off'} URL (http:// or https:// only)")
                new[k] = v
        if off_after is not None:
            if isinstance(off_after, bool):
                raise ValueError('bad delay')
            s = int(off_after)
            if not OFF_AFTER[0] <= s <= OFF_AFTER[1]:
                raise ValueError(f'the delay must be 0 s (right away) to {OFF_AFTER[1] // 60} min')
            new['off_after'] = s
        if enabled is not None:
            new['enabled'] = bool(enabled)
        if new['enabled'] and not new['on_url']:
            raise ValueError('add the URL that turns the light on first')
        self.cfg = new
        if not new['enabled']:
            self.on, self.retry_at = None, 0.0   # forget: we no longer own the light

    def config(self):
        return dict(self.cfg)

    # ---------- the rule ----------
    def update(self, motion):
        if not self.cfg['enabled']:
            return None
        now = self.clock()
        with self.lock:
            if motion:
                self.still_since = None
                if self.on is not True:
                    return self._fire('on', now)
                return None
            if self.still_since is None:
                self.still_since = now
            if self.on is True and now - self.still_since >= self.cfg['off_after']:
                if not self.cfg['off_url']:      # nothing to call: the routine handles off
                    self.on = False
                    return None
                return self._fire('off', now)
        return None

    def test(self, what):
        """Send one call now (Settings page buttons). (ok, message)."""
        url = self.cfg['on_url' if what == 'on' else 'off_url']
        if not url:
            return False, f"no URL for {what} yet"
        with self.lock:
            if self.busy:
                return False, 'a call is still on its way, try again in a moment'
            self._fire(what, self.clock(), force=True)
        return True, f"Sent \"{what}\" (the result shows on the card in a few seconds)"

    def _fire(self, what, now, force=False):
        """Start a call (lock held). Returns what was fired, or None."""
        if self.busy or (not force and now < self.retry_at):
            return None
        url = self.cfg['on_url' if what == 'on' else 'off_url']
        self.busy = True
        before = self.on
        self.on = what == 'on'                   # assume it worked; undone on failure

        def go():
            ok, msg = self.call(url)
            with self.lock:
                self.busy = False
                self.last = dict(t=self.clock(), what=what, ok=ok, msg=msg)
                if not ok:
                    # "on" failed: unknown, the rule tries again while there is motion.
                    # "off" failed: as it was, so a light we switched on is tried again
                    self.on = None if what == 'on' else before
                    self.retry_at = self.clock() + RETRY_S
        self.run(go)
        return what

    # ---------- for the page ----------
    def view(self):
        host = lambda u: (urlparse(u).hostname or '') if u else ''
        with self.lock:
            c = self.cfg
            return dict(enabled=c['enabled'], on_host=host(c['on_url']), off_host=host(c['off_url']),
                        off_after=c['off_after'],
                        state={True: 'on', False: 'off', None: 'unknown'}[self.on],
                        busy=self.busy, last=dict(self.last) if self.last else None)
