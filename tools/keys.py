"""Keys for the boards' sealed reports, handed out over USB.

Each board seals its reports (motion, scores; AES-CCM, main/seal.h) with its
own key. One secret on this PC (sessions/dashboard.json, owner-only, never
in git) gives every board's key: HMAC-SHA256(secret, "ackwave-board" + MAC),
first 16 bytes. The hub, the board on this PC's USB that collects the
others' reports, also holds the keys of the boards it hears (PEERKEY). A
board plugged in that is not the hub gets only its own key, so one stolen
satellite reveals only its own reports.

Boards print fingerprints, never keys (KEY,<fp> and PEERKEY,<mac>,<fp>,
fp = SHA-256("ackwave-key" + key)[:4]); Pairing compares them with the
expected ones and returns the commands that fix any difference.
"""
import hashlib
import hmac
import re
import secrets

MAC_RE = re.compile(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}')
FP_RE = re.compile(r'[0-9a-f]{8}')
MAX_PEERS = 8                    # keys one hub holds (MAX_PEERS in csi_core.h)
SECRET_RE = re.compile(r'[0-9a-f]{64}')


def new_secret():
    return secrets.token_hex(32)


def board_key(secret, mac):
    """16-byte key for the board with this MAC."""
    return hmac.new(bytes.fromhex(secret), b'ackwave-board' + bytes.fromhex(mac.replace(':', '')),
                    hashlib.sha256).digest()[:16]


def fingerprint(key):
    return hashlib.sha256(b'ackwave-key' + key).digest()[:4].hex()


def redact(cmd):
    """A command as the Debug page and the console may show it."""
    if cmd.startswith('KEY,') and cmd != 'KEY,clear':
        return 'KEY,<hidden>'
    if cmd.startswith('PEERKEY,') and not cmd.endswith(',clear') and cmd != 'PEERKEY,clear':
        return ','.join(cmd.split(',')[:2]) + ',<hidden>'
    return cmd


class Pairing:
    """What the connected board holds, from its lines, and what it should."""

    def __init__(self, secret):
        self.secret = secret
        self.me = None              # the connected board's MAC
        self.key_fp = None          # its own key: fingerprint, 'none', or None (not said yet)
        self.peer_fps = {}          # MAC -> fingerprint, as it last listed them
        self._cycle = {}
        self.bad = self.replays = 0
        self.sent_at = {}           # command kind -> when last sent (no flood while it saves)
        self.hub = None             # where it sends its reports: MAC, 'none', or None (not said yet)
        self.hub_ch = self.reports = self.answered = 0

    def feed(self, line):
        """Returns True if the line was one of ours."""
        p = line.strip().split(',')
        if p[0] == 'ME' and len(p) == 2 and MAC_RE.fullmatch(p[1].lower()):
            if p[1].lower() != self.me:
                self.me, self.key_fp, self.peer_fps, self._cycle = p[1].lower(), None, {}, {}
            return False                                     # gui.State reads it too
        if p[0] == 'KEY' and len(p) == 2 and (p[1] == 'none' or FP_RE.fullmatch(p[1])):
            self.key_fp = p[1]
            self.peer_fps, self._cycle = self._cycle, {}     # the 5 s report: PEERKEY lines, then KEY
            return True
        if p[0] == 'PEERKEY' and len(p) == 3 and MAC_RE.fullmatch(p[1]) and FP_RE.fullmatch(p[2]):
            self._cycle[p[1]] = p[2]
            return True
        if p[0] == 'HUB' and len(p) == 2 and p[1] == 'none':
            self.hub = 'none'
            return True
        if p[0] == 'HUB' and len(p) == 5 and MAC_RE.fullmatch(p[1]) and all(x.lstrip('-').isdigit() for x in p[2:]):
            self.hub, self.hub_ch, self.reports, self.answered = p[1], int(p[2]), int(p[3]), int(p[4])
            return True
        if p[0] == 'SEAL' and len(p) == 3 and p[1].isdigit() and p[2].isdigit():
            self.bad, self.replays = int(p[1]), int(p[2])
            return True
        return p[0] in ('PEERKEY', 'PEERKEYS', 'KEY', 'HUB')

    def commands(self, hub, boards, now, every=10.0, hub_mac=None):
        """Commands for the connected board. hub: is it the hub; boards: the
        other boards' MACs it should hold keys for (only if it is the hub);
        hub_mac: where a board that isn't the hub sends its reports."""
        if not self.me or self.key_fp is None or not self.secret:
            return []
        out = []
        own = fingerprint(board_key(self.secret, self.me))
        if self.key_fp != own:
            out.append(('key', 'KEY,' + board_key(self.secret, self.me).hex()))
        want = {m: board_key(self.secret, m) for m in sorted(boards)[:MAX_PEERS] if m != self.me} if hub else {}
        if hub and self.hub not in (None, 'none'):
            out.append(('hub', 'HUB,clear'))                 # the hub sends no reports
        elif not hub and hub_mac and self.hub is not None and self.hub != hub_mac:
            out.append(('hub', f'HUB,{hub_mac}'))
        if not hub and self.peer_fps:
            out.append(('peers', 'PEERKEY,clear'))
        else:
            for m in sorted(set(self.peer_fps) - set(want)):
                out.append(('peer:' + m, f'PEERKEY,{m},clear'))
            for m, k in want.items():
                if self.peer_fps.get(m) != fingerprint(k):
                    out.append(('peer:' + m, f'PEERKEY,{m},{k.hex()}'))
        cmds = []
        for kind, c in out:                                  # one try per kind per `every` s
            if now - self.sent_at.get(kind, -1e9) >= every:
                self.sent_at[kind] = now
                cmds.append(c)
        return cmds

    def status(self, hub, boards):
        own = fingerprint(board_key(self.secret, self.me)) if self.me and self.secret else None
        return dict(key='unknown' if self.key_fp is None else 'none' if self.key_fp == 'none'
                    else 'ok' if self.key_fp == own else 'other',
                    hub=hub, paired=sorted(m for m in boards if self.peer_fps.get(m) ==
                                           fingerprint(board_key(self.secret, m)))
                    if hub and self.secret else [],
                    bad=self.bad, replays=self.replays)

    def key_ok(self):
        """True once the board says it holds its own key."""
        return bool(self.me and self.secret and self.key_fp == fingerprint(board_key(self.secret, self.me)))
