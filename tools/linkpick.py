"""Which devices each board pings, learned over time.

A board has MAX_DEVS device slots. Every board -> device link is scored by how
it behaves:
  - over there: the watched house has movement and it isn't on your side.
    A link that reacts then is useful.
  - your side: movement near the boards. A link that reacts then mostly sees
    your own house: worth less.
  - quiet: nothing anywhere. A link that reacts then is noise.
  - dead: no replies (the device went to sleep or left).
Each board keeps its best links and tries a new candidate in one slot at a
time (EXPLORE_S each), so it keeps finding new paths over there. Scores are
kept on the Pi across restarts (to_json / from_json).
"""
import time

SLOTS = 4                        # the firmware's device slots per board (MAX_DEVS)
EXPLORE_S = 600                  # a trial link: 10 min, then kept or swapped
MIN_RSSI = -82                   # too faint to give a steady link
MIN_EVIDENCE_S = 20              # seconds of 'over there' before a score means much
PRIOR = 0.25                     # an untried link: worth a try
PENDING_S = 120                  # a link asked for: counted as there while it comes up
REST_S = 1800                    # one that never came: left alone 30 min


class Stat:
    __slots__ = ('n_over', 'hit_over', 'n_mine', 'hit_mine', 'n_quiet', 'hit_quiet', 'alive', 'dead', 'tried')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, float(kw.get(k, 0.0)))

    def score(self):
        over = (self.hit_over + PRIOR * 4) / (self.n_over + 4)
        mine = (self.hit_mine + 0.5) / (self.n_mine + 4)
        noise = (self.hit_quiet + 0.2) / (self.n_quiet + 20)
        alive = (self.alive + 5) / (self.alive + self.dead + 10)
        return alive * max(0.0, over - 0.6 * mine - 1.5 * noise)

    def sure(self):
        return self.n_over >= MIN_EVIDENCE_S

    def view(self):
        return {k: round(getattr(self, k), 1) for k in self.__slots__}


class LinkPicker:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.stats = {}               # (board, device) -> Stat
        self.trial = {}               # board -> (device, since)
        self.asked = {}               # (board, device) -> when we asked for it (pending until it shows up)
        self.rest = {}                # (board, device) -> until when it is left alone (never answered)

    def _st(self, board, dev):
        st = self.stats.get((board, dev))
        if st is None:
            st = self.stats[(board, dev)] = Stat()
        return st

    def observe(self, links, over, mine, dt=1.0):
        """links: {(board, device): moving (bool) or None when not heard};
        over/mine: the verdicts right now (movement over there / on your side)."""
        for (b, d), moving in links.items():
            st = self._st(b, d)
            if moving is None:
                st.dead += dt
                continue
            st.alive += dt
            if over:
                st.n_over += dt
                st.hit_over += dt * moving
            elif mine:
                st.n_mine += dt
                st.hit_mine += dt * moving
            else:
                st.n_quiet += dt
                st.hit_quiet += dt * moving

    def choose(self, board, heard, linked, pinned=()):
        """What this board should do now: ('poke', dev), ('unpoke', dev) or None.
        heard: {device: rssi} it hears; linked: the devices it pings now."""
        now = self.clock()
        pinned = set(pinned)
        linked = set(linked)
        for (b, d), at in list(self.asked.items()):
            if b != board:
                continue
            if d in linked:
                self.asked.pop((b, d))               # it took
            elif now - at < PENDING_S:
                linked.add(d)                        # on its way: counts as linked
            else:
                self.asked.pop((b, d))               # never answered: rest it
                self.rest[(b, d)] = now + REST_S
        heard = {d: r for d, r in heard.items() if self.rest.get((board, d), 0) <= now}
        for d in sorted(pinned - set(linked)):   # your picks first: on every board that hears them
            if len(linked) < SLOTS:
                self.asked[(board, d)] = now
                return ('poke', d)
            free = [x for x in linked if x not in pinned]
            if free:
                return ('unpoke', min(free, key=lambda x: self._st(board, x).score()))
        cands = {d for d, r in heard.items() if r is not None and r > MIN_RSSI}
        score = lambda d: self._st(board, d).score()
        tr = self.trial.get(board)
        if tr and tr[0] not in linked and now - tr[1] > 120:
            self.trial.pop(board, None)          # never took (older firmware, gone): free the slot
            tr = None
        if len(linked) < SLOTS:                  # a free slot: filled now, the best candidate
            fresh = sorted(cands - set(linked), key=lambda d: (-score(d), -(heard.get(d) or -100)))
            if fresh:
                if not tr:
                    self.trial[board] = (fresh[0], now)
                self._st(board, fresh[0]).tried += 1
                self.asked[(board, fresh[0])] = now
                return ('poke', fresh[0])
        if tr and now - tr[1] >= EXPLORE_S:      # the trial is over: kept if it beats the worst keeper
            self.trial.pop(board, None)
            keepers = [d for d in linked if d != tr[0] and d not in pinned]
            if len(keepers) >= SLOTS - 1:
                worst = min(keepers, key=score)
                if score(tr[0]) > score(worst):
                    return ('unpoke', worst)     # the new one stays, the worst goes
                return ('unpoke', tr[0])
            return None                          # room enough: it simply stays
        if tr:
            return None
        dead = [d for d in linked if d not in heard and d not in pinned and self._st(board, d).dead > 300]
        if dead:
            return ('unpoke', dead[0])
        # full: free the worst sure keeper for a trial now and then (one slot explores)
        untried = [d for d in cands - set(linked) if self._st(board, d).tried < 3]
        sure = [d for d in linked if self._st(board, d).sure() and d not in pinned]
        if untried and sure:
            worst = min(sure, key=score)
            best_new = max(score(d) for d in untried)
            if score(worst) < max(best_new, 0.2):
                return ('unpoke', worst)
        return None

    def ranking(self, board=None):
        rows = [(b, d, st.score(), st.sure()) for (b, d), st in self.stats.items() if board in (None, b)]
        return sorted(rows, key=lambda r: -r[2])

    def to_json(self):
        return dict(stats=[[b, d, st.view()] for (b, d), st in self.stats.items()])

    def from_json(self, j):
        for row in (j or {}).get('stats', [])[:2000]:
            try:
                b, d, v = row
                self.stats[(str(b), str(d))] = Stat(**{k: v.get(k, 0.0) for k in Stat.__slots__})
            except (TypeError, ValueError, AttributeError):
                continue
