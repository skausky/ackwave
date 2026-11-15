#!/usr/bin/env python3
"""Light on motion: edges only, off after stillness, retries, settings checks,
secrets kept out of the page, and the real HTTP call against a local server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from light import Light, http_call, url_ok

ON = 'https://api-v3.voicemonkey.io/trigger?token=SECRET&device=ackwave-motion'
OFF = 'https://api-v3.voicemonkey.io/trigger?token=SECRET&device=ackwave-still'


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(ok=True, **cfg):
    clock, calls = Clock(), []
    result = {'ok': ok}

    def call(url):
        calls.append(url)
        return result['ok'], 'HTTP 200' if result['ok'] else 'HTTP 401: INVALID_TOKEN'
    lt = Light(dict(dict(enabled=True, on_url=ON, off_url=OFF, off_after=60), **cfg),
               clock=clock, call=call, run=lambda f: f())
    return lt, clock, calls, result


def main():
    # URL checks
    assert url_ok('') and url_ok(ON) and url_ok('http://192.168.1.5:8123/api/webhook/x')
    for bad in ('ftp://x/y', 'javascript:alert(1)', 'https://', 'https://a b', 'https://x/\nCAL', 5, None,
                'https://x/\x01?token=SECRET', 'https://x/\x7f'):
        assert not url_ok(bad), bad

    # Edges only: one "on" while motion lasts, "off" after 60 s still
    lt, clock, calls, _ = make()
    assert lt.update(False) is None and calls == [], "doesn't switch off a light it didn't switch on"
    assert lt.update(True) == 'on' and calls == [ON]
    for _ in range(500):
        lt.update(True)
    assert calls == [ON], "one call per motion, not per packet"
    lt.update(False)
    clock.t += 59
    assert lt.update(False) is None
    clock.t += 1
    assert lt.update(False) == 'off' and calls == [ON, OFF]
    clock.t += 100
    lt.update(False)
    assert calls == [ON, OFF], "off once"
    # Motion again before the delay ends: the timer restarts, no extra calls
    lt.update(True)
    clock.t += 50
    lt.update(False)
    clock.t += 30
    lt.update(True)
    clock.t += 1
    lt.update(False)
    clock.t += 59
    lt.update(False)
    assert calls == [ON, OFF, ON], calls
    clock.t += 1
    lt.update(False)
    assert calls == [ON, OFF, ON, OFF]
    v = lt.view()
    assert v['state'] == 'off' and v['last']['ok'] and v['on_host'] == 'api-v3.voicemonkey.io'
    assert 'SECRET' not in json.dumps(v), "tokens never reach the page"

    # A failed call is retried, but at most every 30 s
    lt, clock, calls, res = make(ok=False)
    lt.update(True)
    assert lt.view()['state'] == 'unknown' and not lt.view()['last']['ok']
    for _ in range(50):
        lt.update(True)
    assert len(calls) == 1
    clock.t += 30
    res['ok'] = True
    lt.update(True)
    assert len(calls) == 2 and lt.view()['state'] == 'on'

    # A failed "off" is tried again (at most every 30 s) while the light is ours
    lt, clock, calls, res = make()
    lt.update(True)
    lt.update(False)
    clock.t += 60
    res['ok'] = False
    assert lt.update(False) == 'off' and lt.view()['state'] == 'on' and not lt.view()['last']['ok']
    clock.t += 10
    assert lt.update(False) is None, "not before 30 s"
    clock.t += 20
    res['ok'] = True
    assert lt.update(False) == 'off' and lt.view()['state'] == 'off' and calls == [ON, OFF, OFF]
    # ...but a failed test "off" doesn't make a light it never switched on its own
    lt, clock, calls, res = make(ok=False)
    lt.test('off')
    clock.t += 100
    assert lt.update(False) is None and lt.view()['state'] == 'unknown' and calls == [OFF]

    # No delay: on the moment motion starts, off the moment it stops
    lt, clock, calls, _ = make(off_after=0)
    assert lt.update(True) == 'on'
    clock.t += 0.2
    assert lt.update(False) == 'off' and calls == [ON, OFF]
    assert lt.update(True) == 'on' and calls == [ON, OFF, ON], "and on again at once"

    # No off URL: the routine handles off; the next motion turns it on again
    lt, clock, calls, _ = make(off_url='')
    lt.update(True)
    clock.t += 1
    lt.update(False)
    clock.t += 60
    lt.update(False)
    assert calls == [ON] and lt.view()['state'] == 'off'
    lt.update(True)
    assert calls == [ON, ON]

    # A call in flight blocks a second one
    held = []
    lt = Light(dict(enabled=True, on_url=ON, off_url=OFF, off_after=10), clock=Clock(),
               call=lambda u: (True, 'ok'), run=held.append)
    assert lt.update(True) == 'on'
    assert lt.test('on') == (False, 'a call is still on its way, try again in a moment')
    held[0]()
    assert lt.test('off')[0]

    # Settings: validated; disabling forgets the state
    lt, *_ = make()
    for bad in (dict(on_url='ftp://x'), dict(off_after=-1), dict(off_after=99999), dict(off_after=True),
                dict(off_after='x')):
        try:
            lt.set(**bad)
            raise AssertionError(bad)
        except (ValueError, TypeError):
            pass
    try:
        Light().set(enabled=True)
        raise AssertionError('enabled without a URL')
    except ValueError:
        pass
    assert Light({'enabled': True}).cfg['enabled'] is False, "damaged settings start off"
    lt.update(True)
    lt.set(enabled=False)
    assert lt.view()['state'] == 'unknown' and lt.update(True) is None

    # The real call against a local server, Voice Monkey style replies
    replies = {'/ok': (200, {'success': True, 'data': 'OK'}), '/bad': (401, {'error': 'INVALID_TOKEN'}),
               '/soft': (200, {'success': False, 'error': 'DEVICE_NOT_FOUND'}), '/text': (204, None),
               '/null': (200, {'success': True, 'error': None})}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            code, body = replies[self.path.split('?')[0]]
            data = json.dumps(body).encode() if body is not None else b''
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    assert http_call(base + '/ok?token=SECRET') == (True, 'HTTP 200')
    assert http_call(base + '/bad?token=SECRET') == (False, 'HTTP 401: INVALID_TOKEN')
    assert http_call(base + '/soft') == (False, 'HTTP 200: DEVICE_NOT_FOUND')
    assert http_call(base + '/text')[0]
    assert http_call(base + '/null') == (True, 'HTTP 200'), '"error": null is a success'
    srv.shutdown()
    ok, msg = http_call('http://127.0.0.1:1/x?token=SECRET')
    assert not ok and 'SECRET' not in msg
    ok, msg = http_call('http://127.0.0.1:1/x\x01?token=SECRET')     # the library's error quotes it
    assert not ok and 'SECRET' not in msg, msg

    print('-' * 40)
    print('LIGHT TESTS PASSED')


if __name__ == '__main__':
    main()
