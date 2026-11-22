'use strict';
// ackwave Map page: where the board senses motion. Every live link (the router,
// poked devices) is a dot; its 1st Fresnel zone, drawn to scale from the real
// distance and the board's channel, is where a person changes that link the most.
// Positions are metres from the board, saved on the PC (/api/map). Built with
// createElement/textContent only: names come from the network.
window.AckMap = (() => {
  const C = 299792458, FT = 0.3048, IN = 0.0254;
  // The board and ackwave's own limits (Espressif datasheets; main.c, README)
  const BOARD = { txDbm: 20.0, sensDbm: -93, gainDbi: 0 };
  const LIMIT_WEAK = -85, LIMIT_DROP = -80;
  const EIRP = { ap: 20, device: 17 };      // typical, used to place a dot from its signal at first
  const ENVS = {
    outdoor: { label: 'Outdoors, open', n: 2.0 },
    open: { label: 'Indoors, open plan', n: 2.2 },
    home: { label: 'Indoors, furnished home', n: 2.8 },
    cluttered: { label: 'Indoors, cluttered', n: 3.3 },
  };
  const PALETTE = ['#5fd4d0', '#4ea8ff', '#b48cff', '#ffb547', '#c3e88d', '#f78c6c', '#89ddff', '#ff7eb6'];
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const $ = (s) => document.querySelector(s);
  const el = (tag, props = {}, ...kids) => {
    const e = Object.assign(document.createElement(tag), props);
    for (const k of kids) if (k !== null && k !== undefined) e.append(k);
    return e;
  };

  // ---------- state ----------
  const M = { unit: 'ft', env: 'home', view: null, nodes: new Map(), plans: [], hubFloor: 1, floorH: 3.0, hubFacing: null };
  // nodes: key -> {key, name, x, y, placed, floor}. floor: the one you chose (1 = ground); a router or
  // device you haven't given one gets the floor that fits its signals best (autoFloor)
  // Picture layers, bottom first: {id, name, x, y (top-left corner, metres), w (width, metres), rot (radians),
  // opacity, hidden, img (once loaded), iw, ih (its pixels)}. A picture pixel (u, v) lands at
  // (x, y) + rotate(rot) * (u, v) * w / iw.
  const PLAN_IDS = ['p1', 'p2', 'p3', 'p4'];
  let tool = null;                          // {kind: 'move'|'scale'|'align', id, pts: [], step}
  let readOnly = false;                     // on Home: look, pan and zoom; nothing is moved or saved
  let stageMark = null;                     // where the map lives on the Map page (it visits Home)
  let pageView = null, homeFit = false, homeTouched = false;   // homeTouched: you moved Home's view     // the Map page's view, kept while Home shows its own
  let homeView = null;                      // Home's view: fitted once, then yours (this visit)
  let radio = [], channel = 6, boardMotion = false, hubAct = null;   // hubAct: the hub's activity, 1 = its threshold
  let others = [];                          // the other boards (slow.map_boards): each a point with its router
  let demoPeople = [];                      // demo only: the simulated people's real spots
  let hearings = {};                        // slow.hearings: target key -> [[observer key, dBm]]
  const heardAvg = new Map();               // 'target|observer' -> smoothed dBm
  let locAt = 0;
  let routersInfo = [];                     // the hub's routers (slow.routers): idx -> bssid, for naming
  let showOnly = null;                      // null = every board's links; 'hub' or 'sat:<mac>' = one board's
  const WEAK_DBM = -80;                     // links weaker than this are drawn faint
  // ---------- life: every effect shows something real ----------
  //  particles along a link   its pokes and answers (their rate: the answers a second)
  //  a bright band running    that link sees motion
  //  a node's glow            how active it is; dim when its board isn't heard
  //  rings once               something heard for the first time
  //  a wave to the light      the light was switched on, from the board(s) that saw it
  //  a path between boards    someone walked from one board's area to the next (in order)
  const reduced = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const fx = { seen: new Map(), quality: 2, dtAvg: 16, lastT: 0, skip: false, waves: [], lightWas: null, by: [],
    walk: null, walkSeen: '' };   // walk: someone walking board to board {keys, t0}
  let lightInfo = null;                     // the light hook (header.light): a node on the map when switched on
  let autoFit = true;                       // refit as things appear, until you pan, zoom or drag
  let viewByUser = false;                   // only a view you set is saved
  let nodeCount = 0;
  let loaded = false, visible = false, raf = 0, saveTimer = 0, dirty = true;
  let cv, ctx, W = 0, H = 0, dpr = 1;
  const ptrs = new Map();                   // pointerId -> {x, y}
  let drag = null;                          // {key} | {pan: true, x0, y0, vx, vy} | {pinch...}

  // ---------- radio maths ----------
  const freq = () => (2407 + 5 * channel) * 1e6;
  const lambda = () => C / freq();
  const pl1 = () => 20 * Math.log10(4 * Math.PI * freq() / C);     // free-space loss over the first metre
  // The fade with distance: measured from your placed boards when it can be
  // (envFit), else the environment you picked
  let envFit = null;                        // {A, n, lf, pairs, checks} from board-to-board signals
  const envN = () => (envFit ? envFit.n : ENVS[M.env].n);
  const FLOOR_LOSS_DB = 10;                 // a floor/ceiling, until measured
  const eirpOf = (r) => (r.ap ? EIRP.ap : EIRP.device);
  // What the board should hear at d metres in this environment (no walls)
  const at1m = (r) => (envFit ? envFit.A + eirpOf(r) - BOARD.txDbm : eirpOf(r) + BOARD.gainDbi - pl1());   // its level at 1 m
  const expected = (r, d) => at1m(r) - 10 * envN() * Math.log10(Math.max(d, 0.3));
  // A first guess at the distance from the signal (rough: walls and furniture make it look farther)
  const guessDist = (r) => Math.min(40, Math.max(1, 10 ** ((at1m(r) - r.rssi) / (10 * envN()))));
  const zoneR = (n, d1, d2) => Math.sqrt(n * lambda() * d1 * d2 / (d1 + d2));

  // ---------- units ----------
  function fmtLen(m) {
    if (M.unit === 'm') return m < 1 ? `${Math.round(m * 100)} cm` : `${m.toFixed(m < 10 ? 1 : 0)} m`;
    const inch = Math.round(m / IN);           // under 1 ft: inches; under 10 ft: to the inch; else whole feet
    if (inch < 12) return `${inch}″`;
    if (inch < 120) {
      const ft = Math.floor(inch / 12), i = inch - ft * 12;
      return i ? `${ft}′ ${i}″` : `${ft}′`;
    }
    return `${Math.round(m / FT)}′`;
  }

  // ---------- nodes from the live links ----------
  // A router's dot is keyed by its address ('r:<bssid>'), not by the hub's
  // slot ('ap:N'): switching networks then shows the new routers as new dots
  // and keeps the old ones' places for when you switch back.
  function nodeFor(r, k) {
    const key = r.node || r.key;
    let n = M.nodes.get(key);
    if (!n) {                                // first time heard: place it from its signal
      const d = r.rssi !== null && r.rssi !== undefined ? guessDist(r) : 5;
      const ang = r.key === 'ap:0' ? 0 : (-0.7 + 0.6 * k);
      n = { key, name: r.label, x: d * Math.cos(ang), y: d * Math.sin(ang), placed: false };
      M.nodes.set(key, n);
      scheduleSave();
    }
    n.name = r.label;
    return n;
  }
  const HUB = { x: 0, y: 0 };                // positions are metres from the hub
  // Height: floors are M.floorH apart (about 3 m: ceiling plus floor)
  const floorOf = (n) => (n === HUB ? M.hubFloor : n.floor || n.autoFloor || M.hubFloor);
  const zOf = (n) => (floorOf(n) - 1) * M.floorH;
  const dist3 = (a, n) => Math.hypot(n.x - a.x, n.y - a.y, zOf(n) - zOf(a));
  const floorsUsed = () => new Set([M.hubFloor, ...[...M.nodes.values()].map((n) => n.floor || n.autoFloor || M.hubFloor)]).size > 1;
  // Another board: first placed from how strongly the hub hears it
  function boardNode(b, k) {
    let n = M.nodes.get(b.key);
    if (!n) {
      const d = b.rssi ? guessDist({ ap: false, rssi: b.rssi }) : 6, ang = Math.PI - 0.6 + 0.6 * k;
      n = { key: b.key, name: b.label, x: d * Math.cos(ang), y: d * Math.sin(ang), placed: false };
      M.nodes.set(b.key, n);
      scheduleSave();
    }
    n.name = b.label; n.board = true;
    return n;
  }
  // A link's far end by key: the hub, another board's square, or a dot (a
  // router or device the hub may share), first placed near the board that sees it
  function nodeAt(key, name, near, rssi, ap) {
    if (key === 'hub') return HUB;
    if (key.startsWith('sat:')) {
      const k = others.findIndex((o) => o.key === key);
      return k >= 0 ? boardNode(others[k], k) : null;
    }
    let n = M.nodes.get(key);
    if (!n) {
      const d = rssi ? guessDist({ ap, rssi }) : 4, ang = Math.random() * 2 * Math.PI;
      n = { key, name: name || key, x: near.x + d * Math.cos(ang), y: near.y + d * Math.sin(ang), placed: false };
      M.nodes.set(key, n);
      scheduleSave();
    }
    if (name) n.name = name;
    return n;
  }
  // [{r, a, n, color, d}]: the hub's live links (a = the hub), then each other
  // board's link to its router (a = that board)
  // Layouts saved before routers had addresses keep them as 'ap:N' (the hub's
  // slot). Decided once, when the live links first show up: if the hub is on
  // the network the layout was made with (the one the other boards watch, or
  // there are no other boards), each dot goes to the router now in its slot;
  // otherwise those dots are retired ('old:ap:N', kept in the file, never
  // drawn) so a later network can't inherit them.
  function adoptOldRouters(live) {
    const legacy = [...M.nodes.keys()].filter((k) => /^ap:\d+$/.test(k));
    if (!legacy.length) return;
    const hubRouters = live.filter((r) => r.ap && r.node && r.node !== r.key);
    if (!hubRouters.length || others.some((b) => !b.router && b.state !== 'gone')) return;   // not known yet
    const hubNodes = new Set(hubRouters.map((r) => r.node));
    const sameNet = !others.length || others.some((b) => b.router && hubNodes.has(b.router));
    for (const k of legacy) {
      const old = M.nodes.get(k), r = sameNet && hubRouters.find((x) => x.key === k);
      M.nodes.delete(k);
      if (r && !M.nodes.has(r.node)) M.nodes.set(r.node, Object.assign(old, { key: r.node }));
      else if (!M.nodes.has('old:' + k)) M.nodes.set('old:' + k, Object.assign(old, { key: 'old:' + k }));
    }
    scheduleSave();
  }
  function links() {
    const live = [...radio].sort((a, b) => (a.key === 'ap:0' ? -1 : b.key === 'ap:0' ? 1 : a.src - b.src));
    adoptOldRouters(live);
    const out = live.map((r, k) => {
      const bk = others.findIndex((o) => o.key === 'sat:' + r.key);   // a board the hub pokes: its square
      const n = bk >= 0 ? boardNode(others[bk], bk) : nodeFor(r, k);
      return { r, a: HUB, n, color: PALETTE[k % PALETTE.length], d: dist3(HUB, n) };
    });
    others.forEach((b, k) => {
      const a = boardNode(b, k);
      // its links as it reports them (newer firmware); else just its router
      const ls = b.links && b.links.length ? b.links
        : b.router ? [{ key: b.router, name: b.router_name, moving: b.state === 'motion', router: true }] : [];
      ls.forEach((l, j) => {
        const n = nodeAt(l.key, l.name, a, l.router ? b.router_rssi : null, !!l.router);
        if (!n || n === a) return;
        const r = { key: `${b.key}>${l.key}`, label: `${b.label} → ${l.name || 'its router'}`, ap: !!l.router,
          rssi: l.router && l.key === b.router ? b.router_rssi : null, board: b, link: l,
          state: b.state === 'gone' ? 'silent' : l.moving ? 'motion' : b.state === 'calibrating' ? 'calibrating' : 'still' };
        out.push({ r, a, n, color: PALETTE[(live.length + 3 * k + j) % PALETTE.length], d: dist3(a, n) });
      });
    });
    if (!showOnly) return out;
    return out.filter(({ a }) => (showOnly === 'hub' ? a === HUB : a.key === showOnly));
  }
  const weak = (r) => r.rssi !== null && r.rssi !== undefined && r.rssi <= WEAK_DBM;
  const moving = (r) => (r.link ? !!r.link.moving && r.board.state !== 'gone'
    : r.board ? r.board.state === 'motion' : r.key === 'ap:0' ? boardMotion : !!r.moving);

  // ---------- placing what you didn't: from who hears it, how strongly ----------
  // Anchors are the hub (always at 0, 0) and the boards you placed. For a
  // router, a device or a board you haven't placed, every anchor that hears it
  // gives a signal; the log-distance model (this environment's n) says how far
  // that is. Its transmit power is unknown, so it is solved too (pulled towards
  // a typical value). A grid search finds the best spot and how sure that is:
  // one anchor gives a ring, two a pair of spots, three or more a point.
  const SIGMA_DB = 6, SIGMA_P = 15;           // a signal's scatter indoors; how far its power may be from typical (walls)
  function smoothHeard() {
    for (const [key, obs] of Object.entries(hearings))
      for (const [by, dbm] of obs) {
        const k = key + '|' + by, v = heardAvg.get(k);
        heardAvg.set(k, v === undefined ? dbm : 0.92 * v + 0.08 * dbm);   // ~12 updates: a slow, steady level
      }
  }
  function anchorOf(by) {
    if (by === 'hub') return HUB;
    const n = M.nodes.get(by);
    return n && n.placed ? n : null;
  }
  function solveOne(key, n) {
    const obs = [];
    for (const [by] of hearings[key] || []) {
      const a = anchorOf(by);
      if (a && a !== n) obs.push([a, heardAvg.get(key + '|' + by), zOf(a), floorOf(a)]);
    }
    const hint = n.hint;                         // a rough spot: only inside it
    if (!obs.length) return hint ? { x: hint.x, y: hint.y, a: hint.r * 0.7, b: hint.r * 0.7, rot: 0, heardBy: 0, floor: n.floor || M.hubFloor } : null;
    // its floor: the one you chose, else every floor the anchors are on (and the ground), best fit wins
    const floors = n.floor ? [n.floor] : [...new Set([1, M.hubFloor, ...obs.map(([a]) => floorOf(a))])];
    const kind = key.startsWith('sat:') ? BOARD.txDbm : (key.startsWith('ap:') || key.startsWith('r:')) ? EIRP.ap : EIRP.device;
    // its likely power: from the boards' own (measured) when there is a fit
    const P0 = envFit ? envFit.A + (kind - BOARD.txDbm) : kind - pl1();
    const nn = 10 * envN(), lf = envFit ? envFit.lf : FLOOR_LOSS_DB, wm = 1 / SIGMA_DB ** 2, wp = 1 / SIGMA_P ** 2;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const [a] of obs) { x0 = Math.min(x0, a.x); x1 = Math.max(x1, a.x); y0 = Math.min(y0, a.y); y1 = Math.max(y1, a.y); }
    const pad = 15, G = 90;
    x0 -= pad; x1 += pad; y0 -= pad; y1 += pad;
    if (hint) { x0 = hint.x - hint.r; x1 = hint.x + hint.r; y0 = hint.y - hint.r; y1 = hint.y + hint.r; }
    const step = Math.max(x1 - x0, y1 - y0) / G;
    let cells = [], best = null;
    for (const fl of floors) {
      const gz = (fl - 1) * M.floorH, fc = [];
      let fb = null;
      for (let gy = y0; gy <= y1; gy += step)
        for (let gx = x0; gx <= x1; gx += step) {
          if (hint && Math.hypot(gx - hint.x, gy - hint.y) > hint.r) continue;
          let sp = 0;
          const ls = obs.map(([a, r, az, af]) => {
            const l = nn * Math.log10(Math.max(0.5, Math.hypot(gx - a.x, gy - a.y, gz - az))) + lf * Math.abs(fl - af)
              - gainDb(a, gx, gy);                 // a router behind a board reads weaker
            sp += (r + l) * wm; return l;
          });
          const P = (sp + P0 * wp) / (obs.length * wm + wp);
          let J = (P - P0) ** 2 * wp;
          obs.forEach(([, r], i) => { J += (r - (P - ls[i])) ** 2 * wm; });
          fc.push([gx, gy, J]);
          if (!fb || J < fb[2]) fb = [gx, gy, J];
        }
      if (fb && (!best || fb[2] < best[2])) { best = fb; best.floor = fl; cells = fc; }
    }
    if (!best) return null;
    // how sure: the spread of the likely cells around the best one
    let sw = 0, sxx = 0, syy = 0, sxy = 0;
    for (const [gx, gy, J] of cells) {
      const w = Math.exp(-(J - best[2]) / 2);
      if (w < 1e-3) continue;
      const dx = gx - best[0], dy = gy - best[1];
      sw += w; sxx += w * dx * dx; syy += w * dy * dy; sxy += w * dx * dy;
    }
    sxx /= sw; syy /= sw; sxy /= sw;
    const tr = (sxx + syy) / 2, det = Math.sqrt(Math.max(0, ((sxx - syy) / 2) ** 2 + sxy * sxy));
    return { x: best[0], y: best[1], a: Math.sqrt(tr + det) + step / 2, b: Math.sqrt(Math.max(0, tr - det)) + step / 2,
      rot: 0.5 * Math.atan2(2 * sxy, sxx - syy), heardBy: obs.length, floor: best.floor };
  }
  // The house's own radio model, from boards whose places you know hearing
  // each other: dBm = A - 10 n log10(d) - lf * floors between. Least squares
  // over the pairs: n (how fast signal fades here), lf (what a floor costs,
  // when some pairs cross floors), A (a board's level at 1 m). Each pair's
  // misfit is a check of the placement: far off means a square is misplaced
  // (or a wall in the way).
  function fitEnv() {
    const rows = [];
    for (const [key, obs] of Object.entries(hearings)) {
      if (key !== 'hub' && !key.startsWith('sat:')) continue;   // boards only: routers send at other powers
      const t = anchorOf(key);
      if (!t) continue;
      for (const [by] of obs) {
        const a = anchorOf(by);
        if (!a || a === t) continue;
        const d = dist3(a, t), r = heardAvg.get(key + '|' + by);
        if (d < 0.5 || r === undefined) continue;
        rows.push({ key, by, d, r: r - gainDb(a, t.x, t.y) - gainDb(t, a.x, a.y), fl: Math.abs(floorOf(a) - floorOf(t)) });
      }
    }
    const cross = rows.some((x) => x.fl > 0);
    const dists = new Set(rows.map((x) => Math.round(x.d)));
    if (rows.length < 3 || dists.size < 2) return null;
    // normal equations for [A, n, lf] (lf fixed when no pair crosses floors)
    const k = cross && rows.length >= 4 ? 3 : 2;
    const N = Array.from({ length: k }, () => new Array(k).fill(0)), b = new Array(k).fill(0);
    for (const x of rows) {
      const f = [1, -10 * Math.log10(x.d), -x.fl].slice(0, k);
      const y = x.r + (k === 2 ? FLOOR_LOSS_DB * x.fl : 0);
      for (let i = 0; i < k; i++) { b[i] += f[i] * y; for (let j = 0; j < k; j++) N[i][j] += f[i] * f[j]; }
    }
    for (let i = 0; i < k; i++) N[i][i] += 1e-6;   // Gauss-Jordan on a 2x2 / 3x3
    for (let c = 0; c < k; c++) {
      let p = c;
      for (let r = c + 1; r < k; r++) if (Math.abs(N[r][c]) > Math.abs(N[p][c])) p = r;
      [N[c], N[p]] = [N[p], N[c]]; [b[c], b[p]] = [b[p], b[c]];
      if (Math.abs(N[c][c]) < 1e-9) return null;
      for (let r = 0; r < k; r++) {
        if (r === c) continue;
        const m = N[r][c] / N[c][c];
        for (let j = c; j < k; j++) N[r][j] -= m * N[c][j];
        b[r] -= m * b[c];
      }
    }
    const sol = b.map((v, i) => v / N[i][i]);
    const n = Math.min(5, Math.max(1.6, sol[1])), lf = k === 3 ? Math.min(25, Math.max(0, sol[2])) : FLOOR_LOSS_DB;
    const A = rows.reduce((s2, x) => s2 + x.r + 10 * n * Math.log10(x.d) + lf * x.fl, 0) / rows.length;
    const checks = rows.map((x) => ({ ...x, off: x.r - (A - 10 * n * Math.log10(x.d) - lf * x.fl) }))
      .filter((x) => Math.abs(x.off) > 10).sort((p, q) => Math.abs(q.off) - Math.abs(p.off));
    return { A, n, lf, measuredLf: k === 3, pairs: rows.length, checks };
  }
  // Every few seconds: move what you haven't placed to where its signals say
  function locate(t) {
    if (t - locAt < 3000) return;
    locAt = t;
    smoothHeard();
    envFit = fitEnv();
    let moved = false;
    for (const [key, n] of M.nodes) {
      if (n.placed || key === 'light') continue;
      const s = solveOne(key, n);
      if (!s) { n.auto = null; continue; }
      // calm: a new estimate within half its own uncertainty is the same
      // place (signals wobble); past that it glides there
      const shift = n.auto ? Math.hypot(s.x - n.x, s.y - n.y) : Infinity;
      if (shift > 0.5 * s.a || !n.auto) {
        const k = n.auto ? 0.3 : 1;
        n.x += (s.x - n.x) * k; n.y += (s.y - n.y) * k;
      }
      n.auto = s;
      if (!n.floor) {                            // another floor only once it wins three times running
        if (s.floor === n.autoFloor || !n.autoFloor) { n.autoFloor = s.floor; n.floorVotes = 0; }
        else if (++n.floorVotes >= 3) { n.autoFloor = s.floor; n.floorVotes = 0; }
      }
      moved = true;
    }
    if (moved) { dirty = true; scheduleSave(); }
  }
  // An unplaced node: back to the signals (a placed one keeps where you put it)
  function unplace(key) {
    const n = M.nodes.get(key);
    if (!n) return;
    n.placed = false; n.auto = null; locAt = 0; dirty = true; renderList(); scheduleSave();
  }

  // How strongly a link sees motion: 1 = its threshold, 0 = quiet, quarter
  // steps (finer would redo the map at every flicker). null: unknown.
  function strength(r) {
    const q = (x) => Math.round(Math.min(3, Math.max(0, x)) * 20) / 20;
    if (r.link) return r.link.level !== undefined ? q(r.link.level) : r.link.moving ? 1.25 : 0;
    if (r.board) return r.board.level !== undefined ? q(r.board.level / 2.8) : r.board.state === 'motion' ? 1.25 : 0;
    if (r.key === 'ap:0') return hubAct !== null ? q(hubAct) : boardMotion ? 1.25 : 0;
    if (r.activity !== undefined && r.activity !== null) return q(r.activity / 2.8);
    return r.moving ? 1.25 : r.state === 'still' ? 0 : null;
  }
  // Has this link a verdict right now (moving or still)? Unknown ones don't vote.
  function judged(r) {
    if (r.link) return r.board.state === 'motion' || r.board.state === 'still';
    if (r.board) return r.board.state === 'motion' || r.board.state === 'still';
    if (r.key === 'ap:0') return true;           // the hub's own verdict
    return r.state === 'motion' || r.state === 'still';
  }
  // A link's ends as [a, n] pairs: just them, or for an estimated end three
  // spots along its uncertainty (centre and 1 sigma either way)
  function endSamples(a, n) {
    const spread = (m) => {
      if (!m || m === HUB || m.placed || m.board || (m.key || '').startsWith('sat:')) return [m];
      if (!m.auto) {                             // nothing known but a rough distance: anywhere around 3 m (or its rough spot)
        const c = m.hint || m, rr = m.hint ? m.hint.r * 0.6 : 3;
        const at = (ang) => ({ x: c.x + rr * Math.cos(ang), y: c.y + rr * Math.sin(ang), floor: m.floor, autoFloor: m.autoFloor });
        return [m, at(0), at(Math.PI / 2), at(Math.PI), at(-Math.PI / 2)];
      }
      if (m.auto.a < 1) return [m];
      const c = Math.cos(m.auto.rot), sn = Math.sin(m.auto.rot), k = m.auto.a;
      const at = (f) => ({ x: m.x + f * k * c, y: m.y + f * k * sn, floor: m.floor, autoFloor: m.autoFloor });
      return [m, at(1), at(-1)];
    };
    const out = [];
    for (const ea of spread(a)) for (const en of spread(n)) out.push([ea, en]);
    return out;
  }

  // ---------- sensitivity: where a person changes each link ----------
  // An estimate, not a measurement. A body changes a link most inside its
  // first Fresnel zone (path through the point at most lambda/2 longer than
  // the direct one); multipath carries the change well beyond it, fading over
  // the outer zones; and a body next to either end changes every path at
  // once. Walls and furniture bend all of this: the walk test measures it.
  // Across floors the link slopes: the point is taken at the link's height
  // above it, so the glow is the zone seen from above.
  function sensitivity(px, py, a, n, d, lam) {
    const za = zOf(a), zn = zOf(n), hx = n.x - a.x, hy = n.y - a.y, hh = hx * hx + hy * hy;
    const t = hh > 1e-6 ? Math.min(1, Math.max(0, ((px - a.x) * hx + (py - a.y) * hy) / hh)) : 0.5;
    const pz = za + t * (zn - za);
    const d1 = Math.hypot(px - a.x, py - a.y, pz - za), d2 = Math.hypot(px - n.x, py - n.y, pz - zn);
    const zones = (d1 + d2 - d) / (lam / 2);           // which Fresnel zone the point is in
    const along = 1 / (1 + 0.22 * zones);               // ~0.8 at the first zone's edge, ~0.2 at 1.5 m out
    // a board hears best where it faces: both ends' patterns towards the point
    const ga = gainAt(a, px, py), gn = gainAt(n, px, py);
    const ends = 0.55 * Math.max(Math.exp(-d1 / 0.7) * ga, Math.exp(-d2 / 0.7) * gn);
    return Math.min(1, along * along * ga * gn + ends);
  }
  // ---------- facing: a board's antenna pattern ----------
  // Its PCB antenna on a wall: strongest ahead, the wall behind (about 45% of
  // the amplitude, -7 dB), the sides in between. No facing set: the same all round.
  const BACK = 0.45;
  const facingOf = (m) => (m === HUB ? M.hubFacing : m && m.facing);
  function gainAt(m, px, py) {
    const f = facingOf(m);
    if (f === null || f === undefined) return 1;
    const dx = px - m.x, dy = py - m.y;
    if (dx * dx + dy * dy < 0.09) return 1;            // right at it
    const c = Math.cos(Math.atan2(dy, dx) - f);
    return BACK + (1 - BACK) * ((1 + c) / 2) ** 1.5;
  }
  const gainDb = (m, px, py) => 20 * Math.log10(gainAt(m, px, py));
  const heatCv = document.createElement('canvas'), heatMv = document.createElement('canvas');
  // Where the person likely is: a tracking estimate from every link's strength.
  //
  // The model (per spot x on a 25 cm grid): a person at x changes link i by
  // about g * s_i(x), s_i its sensitivity there (the glow), g how much they
  // move (unknown, fitted per spot, with a prior). The links' measured
  // excess activity a_i (over each one's own idle level) is compared with
  // that; the misfit gives a likelihood. Two side-by-side links that react
  // differently put the person on the stronger one's side; quiet links rule
  // out their zones. A Bayes filter carries it over time (a person moves
  // ~1 m/s), so the estimate tracks and doesn't flicker.
  const track = { grid: null, people: [], nextId: 0, est: null, base: new Map(), at: 0 };
  const CELL = 0.25;
  const SIG0 = 0.45, SIG1 = 0.25, MODEL_ERR = 1.2;          // a link's scatter: idle noise, plus more the stronger it reacts
  let heatKey = '', heatView = null, viewSeen = '', viewMovedAt = 0;
  function rgbOf(hex) { const v = parseInt(hex.slice(1), 16); return [v >> 16 & 255, v >> 8 & 255, v & 255]; }
  // Two layers at a third of the screen's resolution: still links in their
  // colour, moving ones in the motion colour (pulsed when drawn). Rebuilt only
  // when the view, a position or a link's state changes.
  // While the view moves (a pan, a pinch, a fly-to) the last image is moved
  // with it; it is recomputed once the view has been still ~0.2 s (a Pi can't
  // redo it every frame). Returns the view it was computed at.
  function heat(L, t) {
    const S = fx.quality === 2 ? 3 : 4, w = Math.max(1, Math.ceil(W / S)), h = Math.max(1, Math.ceil(H / S));
    const vk = JSON.stringify(M.view);
    if (vk !== viewSeen) { viewSeen = vk; viewMovedAt = t; }
    const geo = JSON.stringify([w, h, channel, !!track.est, L.map(({ a, n, color, r }) => [a.x, a.y, n.x, n.y, color, moving(r), facingOf(a), facingOf(n)])]);
    const key = geo + '|' + vk;
    if (key === heatKey) return heatView;
    if (heatView && geo === heatKey.split('|')[0] && t - viewMovedAt < 200) return heatView;   // still moving
    heatKey = key;
    heatView = { ...M.view };
    const lam = lambda(), motionRgb = rgbOf(css('--motion'));
    const still = new ImageData(w, h), mov = new ImageData(w, h);
    const spec = L.filter(({ d }) => d >= 0.05).map(({ a, n, color, d, r }) =>
      ({ a, n, d, mv: moving(r), rgb: rgbOf(color), w: weak(r) ? 0.35 : 1 }));
    const dimMv = !!track.est;                    // the estimate leads: the links' own red glow steps back
    for (let j = 0; j < h; j++) {
      const wy = M.view.y + (j * S - H / 2) / M.view.s;
      for (let i = 0; i < w; i++) {
        const wx = M.view.x + (i * S - W / 2) / M.view.s;
        // overlapping links blend: opacity adds up, colour is their weighted mix
        let clear = 1, r = 0, g = 0, b = 0, sum = 0, clearMv = 1;
        for (const l of spec) {
          const v = l.w * sensitivity(wx, wy, l.a, l.n, l.d, lam);
          if (l.mv) { clearMv *= 1 - 0.75 * v ** 1.6; continue; }
          const a = 0.45 * v * v;
          clear *= 1 - a;
          r += l.rgb[0] * a; g += l.rgb[1] * a; b += l.rgb[2] * a; sum += a;
        }
        const o = 4 * (j * w + i);
        if (sum > 0) { still.data[o] = r / sum; still.data[o + 1] = g / sum; still.data[o + 2] = b / sum; still.data[o + 3] = 255 * (1 - clear); }
        if (clearMv < 1) { mov.data[o] = motionRgb[0]; mov.data[o + 1] = motionRgb[1]; mov.data[o + 2] = motionRgb[2]; mov.data[o + 3] = 255 * (1 - clearMv); }
      }
    }
    if (dimMv) for (let o = 3; o < mov.data.length; o += 4) mov.data[o] *= 0.35;
    for (const [c, img] of [[heatCv, still], [heatMv, mov]]) {
      c.width = w; c.height = h;
      c.getContext('2d').putImageData(img, 0, 0);
    }
    return heatView;
  }

  // ---------- view ----------
  const X = (x) => W / 2 + (x - M.view.x) * M.view.s;
  const Y = (y) => H / 2 + (y - M.view.y) * M.view.s;
  const toWorld = (px, py) => [M.view.x + (px - W / 2) / M.view.s, M.view.y + (py - H / 2) / M.view.s];
  function fit() {
    // the hub, the boards, and what is placed or well located; a rough guess
    // (a ring from one board) or a picture never pulls the view far out
    const sure = (n) => n === HUB || n.board || n.placed || (n.auto && n.auto.a < 6);
    const pts = [[0, 0], ...links().flatMap(({ a, n }) => [a, n]).filter(sure).map((n) => [n.x, n.y])];
    for (const b of others) { const n = M.nodes.get(b.key); if (n) pts.push([n.x, n.y]); }
    for (const p of M.plans) {                       // pictures only when there is nothing else
      if (pts.length > 1 || p.hidden || !p.img) continue;
      const h = p.w * p.ih / p.iw, c = Math.cos(p.rot), s = Math.sin(p.rot);
      for (const [u, v] of [[0, 0], [p.w, 0], [0, h], [p.w, h]]) pts.push([p.x + u * c - v * s, p.y + u * s + v * c]);
    }
    let x0 = Math.min(...pts.map((p) => p[0])), x1 = Math.max(...pts.map((p) => p[0]));
    let y0 = Math.min(...pts.map((p) => p[1])), y1 = Math.max(...pts.map((p) => p[1]));
    const sx = (Math.max(W, 200) - 110) / Math.max(x1 - x0, 2), sy = (Math.max(H, 200) - 110) / Math.max(y1 - y0, 2);
    M.view = { x: (x0 + x1) / 2, y: (y0 + y1) / 2 + 0.3, s: Math.min(2000, Math.max(2, Math.min(sx, sy))) };   // room for labels
    dirty = true;
  }
  function zoomAt(px, py, k) {
    if (readOnly) homeTouched = true;
    if (!M.view) return;
    autoFit = false; viewByUser = true;
    const [wx, wy] = toWorld(px, py);
    M.view.s = Math.min(2000, Math.max(2, M.view.s * k));
    M.view.x = wx - (px - W / 2) / M.view.s;
    M.view.y = wy - (py - H / 2) / M.view.s;
    dirty = true;
    if (!readOnly) scheduleSave();
  }

  // ---------- drawing ----------
  function resize() {
    dpr = window.devicePixelRatio || 1;
    const box = cv.parentElement;
    const was = [W, H];
    W = box.clientWidth; H = box.clientHeight;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    // Home's view follows its box until you pan or zoom it yourself
    if (readOnly && !homeTouched && loaded && (was[0] !== W || was[1] !== H) && W > 0) fit();
    dirty = true;
  }
  function drawPlans() {
    for (const p of M.plans) {
      if (p.hidden || !p.img) continue;
      const mpp = p.w / p.iw;
      ctx.save();
      ctx.translate(X(p.x), Y(p.y));
      ctx.rotate(p.rot);
      ctx.scale(mpp * M.view.s, mpp * M.view.s);
      // while aligning a plan to the map, show it faintly once its corners are picked
      ctx.globalAlpha = tool && tool.kind === 'align' && tool.id === p.id && tool.pts.length >= 2 ? 0.25 : p.opacity;
      ctx.drawImage(p.img, 0, 0);
      ctx.restore();
    }
    if (M.plans.some((p) => !p.hidden && p.img)) {   // a dark veil so the ovals and labels stay readable
      ctx.fillStyle = 'rgba(13,17,22,0.25)'; ctx.fillRect(0, 0, W, H);
    }
  }
  // world point <-> a picture's own pixels
  const toPlan = (p, wx, wy) => {
    const k = p.iw / p.w, dx = wx - p.x, dy = wy - p.y, c = Math.cos(-p.rot), s = Math.sin(-p.rot);
    return [(dx * c - dy * s) * k, (dx * s + dy * c) * k];
  };
  function grid() {
    if (M.plans.some((p) => !p.hidden && p.img)) ctx.globalAlpha = 0.45;   // fainter over a picture
    const unitM = M.unit === 'ft' ? FT : 1;
    let step = unitM;                               // a line every 1 ft / 1 m, fewer when zoomed out
    for (const k of [1, 5, 10, 50, 100]) { step = unitM * k; if (step * M.view.s >= 14) break; }
    const major = Math.round(M.unit === 'ft' ? (step < 10 * FT ? 10 * FT / step : 5) : (step < 5 ? 5 / step : 5));   // lines per major line
    const [wx0, wy0] = toWorld(0, 0), [wx1, wy1] = toWorld(W, H);
    ctx.lineWidth = 1;
    for (let x = Math.floor(wx0 / step) * step; x <= wx1; x += step) {
      const isMajor = Math.abs(x / (step * major) - Math.round(x / (step * major))) < 1e-6;
      ctx.strokeStyle = isMajor ? css('--line') : css('--grid');
      ctx.beginPath(); ctx.moveTo(Math.round(X(x)) + 0.5, 0); ctx.lineTo(Math.round(X(x)) + 0.5, H); ctx.stroke();
    }
    for (let y = Math.floor(wy0 / step) * step; y <= wy1; y += step) {
      const isMajor = Math.abs(y / (step * major) - Math.round(y / (step * major))) < 1e-6;
      ctx.strokeStyle = isMajor ? css('--line') : css('--grid');
      ctx.beginPath(); ctx.moveTo(0, Math.round(Y(y)) + 0.5); ctx.lineTo(W, Math.round(Y(y)) + 0.5); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    // scale bar, bottom left
    const barM = step * major, bx = 14, by = H - 16, bw = barM * M.view.s;
    if (bw < W * 0.6) {
      ctx.strokeStyle = css('--fg'); ctx.fillStyle = css('--fg'); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(bx, by - 4); ctx.lineTo(bx, by); ctx.lineTo(bx + bw, by); ctx.lineTo(bx + bw, by - 4); ctx.stroke();
      ctx.font = '11px ui-sans-serif, system-ui, sans-serif'; ctx.textBaseline = 'bottom';
      ctx.fillText(fmtLen(barM), bx + 4, by - 5);
    }
    ctx.globalAlpha = 1;
  }
  function ellipse(cx, cy, a, b, rot) {
    ctx.beginPath();
    ctx.ellipse(X(cx), Y(cy), Math.max(a * M.view.s, 0.5), Math.max(b * M.view.s, 0.5), rot, 0, Math.PI * 2);
  }
  function alpha(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
  }
  function lightNode() {
    if (!lightInfo || !lightInfo.enabled) return null;
    let n = M.nodes.get('light');
    if (!n) {
      n = { key: 'light', name: 'Light', x: 2, y: -2, placed: false };
      M.nodes.set('light', n);
      scheduleSave();
    }
    n.name = 'Light';
    return n;
  }
  // Frame budget: slow frames step quality down (2 full, 1 no glow blur, 0 no
  // particles at 30 fps); fast frames step it back up
  function budget(t) {
    const dt = fx.lastT ? Math.min(t - fx.lastT, 100) : 16;
    fx.lastT = t;
    fx.dtAvg += 0.08 * (dt - fx.dtAvg);
    if (fx.dtAvg > 26 && fx.quality > 0) { fx.quality--; fx.dtAvg = 16; }
    else if (fx.dtAvg < 15 && fx.quality < 2 && Math.random() < 0.005) fx.quality++;
  }
  function particles(L, t, motionC) {
    if (reduced.matches || fx.quality === 0) return;
    for (const { r, a, n, color, d } of L) {
      if (d < 0.3 || (r.board && r.board.state === 'gone')) continue;
      const rate = r.rate !== undefined ? r.rate : 40;      // satellites: about their usual flow
      const count = Math.max(1, Math.min(6, Math.round(rate / 18))), mv = moving(r);
      const speed = mv ? 0.9 : 0.45;                        // links per second
      ctx.fillStyle = mv ? motionC : color;
      if (fx.quality === 2) { ctx.shadowColor = mv ? motionC : color; ctx.shadowBlur = 8; }
      for (let i = 0; i < count; i++) {
        const f = ((t / 1000) * speed + i / count) % 1;
        const x = X(a.x + (n.x - a.x) * f), y = Y(a.y + (n.y - a.y) * f);
        ctx.globalAlpha = Math.sin(f * Math.PI) * (weak(r) ? 0.35 : 0.9);   // fade in and out at the ends
        ctx.beginPath(); ctx.arc(x, y, mv ? 2.4 : 1.7, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1; ctx.shadowBlur = 0;
      if (mv) {                                             // the band that runs while it sees motion
        const f = (t / 650) % 1, x = X(a.x + (n.x - a.x) * f), y = Y(a.y + (n.y - a.y) * f);
        const g = ctx.createRadialGradient(x, y, 0, x, y, 22);
        g.addColorStop(0, alpha(motionC, 0.55)); g.addColorStop(1, alpha(motionC, 0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 22, 0, Math.PI * 2); ctx.fill();
      }
    }
  }
  function ripples(t, keys) {                // a node heard for the first time: rings, once
    for (const [key, x, y] of keys) {
      if (!fx.seen.has(key)) fx.seen.set(key, fx.seen.size === 0 && !loaded ? -1e9 : t);
      const age = t - fx.seen.get(key);
      if (reduced.matches || age < 0 || age > 1800) continue;
      for (const k of [0, 0.35]) {
        const p = age / 1800 - k;
        if (p <= 0 || p >= 1) continue;
        ctx.strokeStyle = alpha(css('--accent'), 0.6 * (1 - p)); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, 10 + 46 * p, 0, Math.PI * 2); ctx.stroke();
      }
    }
  }
  function lightWaves(t) {                   // light switched on: a wave from each source to it
    const ln = M.nodes.get('light');
    fx.waves = fx.waves.filter((w) => t - w.t0 < 2200);
    if (!ln) return;
    const warn = css('--warn') || '#ffb547';
    for (const w of fx.waves) {
      const p = Math.min(1, (t - w.t0) / 1100);
      const x = X(w.from.x + (ln.x - w.from.x) * p), y = Y(w.from.y + (ln.y - w.from.y) * p);
      if (p < 1 && !reduced.matches) {
        ctx.strokeStyle = alpha(warn, 0.35); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(X(w.from.x), Y(w.from.y)); ctx.lineTo(x, y); ctx.stroke();
        const g = ctx.createRadialGradient(x, y, 0, x, y, 26);
        g.addColorStop(0, alpha(warn, 0.85)); g.addColorStop(1, alpha(warn, 0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 26, 0, Math.PI * 2); ctx.fill();
      } else {
        const q = (t - w.t0 - 1100) / 1100;
        if (q > 0 && q < 1) {
          ctx.strokeStyle = alpha(warn, 0.7 * (1 - q)); ctx.lineWidth = 3;
          ctx.beginPath(); ctx.arc(X(ln.x), Y(ln.y), 12 + 40 * q, 0, Math.PI * 2); ctx.stroke();
        }
      }
    }
  }
  function drawLight(t) {
    const ln = lightNode();
    if (!ln) return;
    const on = lightInfo.state === 'on', x = X(ln.x), y = Y(ln.y), warn = css('--warn') || '#ffb547';
    if (on) {
      const g = ctx.createRadialGradient(x, y, 0, x, y, 34);
      g.addColorStop(0, alpha(warn, 0.5)); g.addColorStop(1, alpha(warn, 0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 34, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = on ? warn : css('--card'); ctx.strokeStyle = warn; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y - 2, 7, Math.PI * 0.85, Math.PI * 2.15); ctx.lineTo(x + 3, y + 7); ctx.lineTo(x - 3, y + 7); ctx.closePath();
    ctx.fill(); ctx.stroke();
    label(x, y + 13, 'Light', on ? 'on' : lightInfo.state, on ? warn : css('--fg'));
  }
  // A board's facing: an arrow you drag to aim, and its pattern as a faint
  // outline (how far it hears each way). Not set yet: a faint arrow up,
  // on the Map page only, as a hint that it can be turned.
  const AIM_PX = 34;
  const aimOf = (m) => { const f = facingOf(m); return typeof f === 'number' ? f : -Math.PI / 2; };
  function drawFacing(m) {
    const f = facingOf(m), set = typeof f === 'number';
    if (!set && readOnly) return;
    const x = X(m.x), y = Y(m.y), a = aimOf(m), col = css('--accent');
    ctx.save();
    if (set) {                                     // the pattern: r(angle) ~ gain that way
      ctx.beginPath();
      for (let i = 0; i <= 48; i++) {
        const th = (i / 48) * 2 * Math.PI, g = BACK + (1 - BACK) * ((1 + Math.cos(th)) / 2) ** 1.5, rr = 14 + 30 * g;
        const px = x + rr * Math.cos(th + a), py = y + rr * Math.sin(th + a);
        i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
      }
      ctx.fillStyle = alpha(col, 0.07); ctx.fill();
      ctx.strokeStyle = alpha(col, 0.3); ctx.lineWidth = 1; ctx.stroke();
    }
    const tx = x + AIM_PX * Math.cos(a), ty = y + AIM_PX * Math.sin(a);
    ctx.globalAlpha = set ? 1 : 0.35;
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2; ctx.setLineDash(set ? [] : [3, 3]);
    ctx.beginPath(); ctx.moveTo(x + 12 * Math.cos(a), y + 12 * Math.sin(a)); ctx.lineTo(tx, ty); ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(tx + 6 * Math.cos(a), ty + 6 * Math.sin(a));
    ctx.lineTo(tx + 6 * Math.cos(a + 2.4), ty + 6 * Math.sin(a + 2.4));
    ctx.lineTo(tx + 6 * Math.cos(a - 2.4), ty + 6 * Math.sin(a - 2.4));
    ctx.closePath(); ctx.fill();
    ctx.restore();
  }
  function facingHit(px, py) {
    const nodes = [HUB, ...others.map((b) => M.nodes.get(b.key)).filter(Boolean)];
    for (const m of nodes) {
      const a = aimOf(m), tx = X(m.x) + AIM_PX * Math.cos(a), ty = Y(m.y) + AIM_PX * Math.sin(a);
      if (Math.hypot(px - tx, py - ty) <= 11) return m;
    }
    return null;
  }
  // A small glyph in a dot: Wi-Fi arcs for a router, the maker's initial for a device
  function glyph(x, y, r, n) {
    ctx.save();
    ctx.strokeStyle = 'rgba(4,16,20,.85)'; ctx.fillStyle = 'rgba(4,16,20,.85)'; ctx.lineWidth = 1.4;
    if (r.ap) {
      for (const k of [3, 6]) { ctx.beginPath(); ctx.arc(x, y + 3, k, Math.PI * 1.2, Math.PI * 1.8); ctx.stroke(); }
      ctx.beginPath(); ctx.arc(x, y + 3, 1.2, 0, Math.PI * 2); ctx.fill();
    } else {
      const ch = String(n.name || '?').replace(/[^A-Za-z0-9]/g, '').charAt(0).toUpperCase() || '?';
      ctx.font = '700 10px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(ch, x, y + 0.5);
    }
    ctx.restore();
  }
  function draw(t) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!loaded) return;                           // the saved layout first: guesses would set the view
    const cnt = links().length;                    // something new appeared: show it (a saved view stays at load)
    if (!M.view || (cnt > nodeCount && (readOnly ? !homeTouched : autoFit && !viewByUser))) fit();   // never over a view you set
    nodeCount = cnt;
    locate(t);
    stepTrack(t);
    drawPlans();
    grid();
    drawLikely();
    drawHints();
    const L = links(), lam = lambda(), motionC = css('--motion');
    const pulse = 0.5 + 0.5 * Math.sin(t / 260);
    const hv = heat(L, t);                         // where each link is sensitive (estimate)
    ctx.imageSmoothingEnabled = true;
    if (hv) {                                      // drawn where it was computed, moved with the view
      const k = M.view.s / hv.s;
      ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (W / 2 - (W / 2) * k + (hv.x - M.view.x) * M.view.s),
                       dpr * (H / 2 - (H / 2) * k + (hv.y - M.view.y) * M.view.s));
      ctx.drawImage(heatCv, 0, 0, W, H);
      ctx.globalAlpha = 0.45 + 0.55 * pulse;
      ctx.drawImage(heatMv, 0, 0, W, H);
      ctx.globalAlpha = 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    for (const { r, a: e, n, color, d } of L) {    // zones first, dots on top
      if (d < 0.05) continue;
      const rot = Math.atan2(n.y - e.y, n.x - e.x), cx = (e.x + n.x) / 2, cy = (e.y + n.y) / 2;
      const A = (d + lam / 2) / 2, b = Math.sqrt(Math.max(A * A - d * d / 4, 0));
      const a = Math.max(A * Math.hypot(n.x - e.x, n.y - e.y) / d, b);   // seen from above
      ellipse(cx, cy, a, b, rot);                  // the first Fresnel zone: a thin outline
      const mv = moving(r), wk = weak(r) && !mv;
      ctx.strokeStyle = mv ? motionC : alpha(color, wk ? 0.3 : 0.8); ctx.lineWidth = mv ? 1.8 : 1;
      ctx.setLineDash(wk ? [4, 4] : []);
      ctx.stroke();
      ctx.strokeStyle = alpha(color, 0.55); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(X(e.x), Y(e.y)); ctx.lineTo(X(n.x), Y(n.y)); ctx.stroke();
      // the distance, at the middle, kept upright
      ctx.save();
      ctx.translate(X(cx), Y(cy));
      let ang = rot; if (ang > Math.PI / 2 || ang < -Math.PI / 2) ang += Math.PI;
      ctx.rotate(ang);
      ctx.font = '11.5px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      if (!readOnly) { ctx.fillStyle = css('--fg'); ctx.fillText(fmtLen(d), 0, -4); }   // Home: no lengths
      ctx.restore();
    }
    particles(L, t, motionC);
    drawTrack(t);
    drawNear(t);
    for (const dp of demoPeople) {                 // demo: the truth, to see how close the estimates are
      const x = X(dp.x), y = Y(dp.y);
      ctx.save(); ctx.strokeStyle = css('--fg'); ctx.globalAlpha = 0.8; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.moveTo(x - 4, y); ctx.lineTo(x + 4, y); ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4); ctx.stroke();
      ctx.restore();
      label(x, y - 24, 'real person (demo)', null, css('--muted'));
    }
    // the board
    const bx = X(0), by = Y(0);
    ctx.fillStyle = css('--card'); ctx.strokeStyle = css('--accent'); ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(bx - 9, by - 9, 18, 18, 4) : ctx.rect(bx - 9, by - 9, 18, 18);
    ctx.fill(); ctx.stroke();
    const fl = floorsUsed(), ft = (n) => (fl ? ` · F${floorOf(n)}` : '');
    label(bx, by + 14, (others.length ? 'hub' : 'board') + ft(HUB), null, css('--accent'));
    for (const b of others) {                      // the other boards: squares too, in their state's colour
      const n = M.nodes.get(b.key);
      if (!n) continue;
      const x = X(n.x), y = Y(n.y), mv = b.state === 'motion';
      ctx.fillStyle = css('--card'); ctx.strokeStyle = mv ? motionC : b.state === 'gone' ? css('--muted') : css('--accent');
      ctx.lineWidth = mv ? 3.5 : 2.5;
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x - 9, y - 9, 18, 18, 4) : ctx.rect(x - 9, y - 9, 18, 18);
      ctx.fill(); ctx.stroke();
      if (!n.placed) { ctx.strokeStyle = alpha(css('--accent'), 0.7); ctx.lineWidth = 1.2; ctx.setLineDash([3, 3]);
        ctx.strokeRect(x - 14, y - 14, 28, 28); ctx.setLineDash([]); }
      label(x, y + 14, b.label + ft(n), { motion: 'MOTION', still: 'still', calibrating: 'calibrating', gone: 'not heard' }[b.state] || b.state, mv ? motionC : css('--fg'));
    }
    drawFacing(HUB);
    for (const b of others) { const n = M.nodes.get(b.key); if (n) drawFacing(n); }
    const drawn = new Set();                      // a router two boards share: one dot, its own name
    for (const { r, n, color } of L) {
      if (n === HUB || n.board || drawn.has(n.key)) continue;   // boards are squares, drawn above
      drawn.add(n.key);
      const x = X(n.x), y = Y(n.y);
      const act = r.rate !== undefined ? Math.min(1, r.rate / 90) : 0.6;    // how active: its glow
      if (fx.quality > 0 && !weak(r)) { ctx.shadowColor = color; ctx.shadowBlur = 6 + 14 * act; }
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2);
      ctx.fillStyle = weak(r) ? alpha(color, 0.45) : color; ctx.fill();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = css('--bg'); ctx.lineWidth = 2.5; ctx.stroke();
      glyph(x, y, r, n);
      if (!n.placed) {                        // a guess from its signal: dashed ring
        ctx.beginPath(); ctx.arc(x, y, 14, 0, Math.PI * 2);
        ctx.strokeStyle = alpha(color, 0.8); ctx.lineWidth = 1.2; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
      }
      const sub = readOnly ? '' : [r.rssi !== null && r.rssi !== undefined ? `${r.rssi} dBm` : '',
        r.breathing ? `breathing ${Math.round(r.breathing.rate)}/min` : ''].filter(Boolean).join(' · ');
      label(x, y + 15, (r.board ? n.name : r.label) + ft(n), r.board ? null : sub || null, css('--fg'));
    }
    drawLight(t);
    lightWaves(t);
    drawWalk(t);
    const keys = [['hub', X(0), Y(0)]];
    for (const { n } of L) if (n !== HUB) keys.push([n.key, X(n.x), Y(n.y)]);
    ripples(t, keys);
    $('#map-msg').textContent = L.length ? '' : 'Waiting for live links: the router appears once the board hears it.';
  }
  // A router: how sure you are of where it is. Exactly (placed), roughly
  // (a circle you drag: auto-placement and the tracking stay inside it), or
  // not at all (auto, anywhere its signals fit).
  const SURE = [['auto', 0], ['r10', 3], ['r30', 9], ['r60', 18], ['exact', -1]];
  function whereSure(n) {
    const sel = el('select', { className: 'small', title: 'How sure you are of where this router is' });
    for (const [v, r] of SURE)
      sel.append(el('option', { value: v, textContent: v === 'auto' ? 'Where: automatic (anywhere its signal fits)'
        : v === 'exact' ? 'Where: exactly here' : `Where: roughly here (within ${fmtLen(r)})` }));
    sel.value = n.placed ? 'exact' : n.hint ? (SURE.find(([, r]) => Math.abs(r - n.hint.r) < 0.5) || ['r30'])[0] : 'auto';
    sel.onchange = () => {
      const r = SURE.find(([v]) => v === sel.value)[1];
      if (r === -1) { n.placed = true; n.hint = null; }
      else if (r === 0) { n.placed = false; n.hint = null; }
      else { n.hint = { x: n.x, y: n.y, r }; n.placed = false; }
      n.auto = null; locAt = 0; heatKey = ''; dirty = true; renderList(); scheduleSave();
    };
    return el('label', { className: 'map-floor-row' }, sel,
      el('span', { className: 'muted small', textContent: n.hint ? 'drag it to move the circle' : '' }));
  }
  function drawHints() {
    ctx.save();
    ctx.setLineDash([6, 5]);
    const done = new Set();
    for (const { n } of links()) {
      if (!n.hint || n === HUB || done.has(n)) continue;
      done.add(n);
      ctx.beginPath();
      ctx.arc(X(n.hint.x), Y(n.hint.y), Math.max(n.hint.r * M.view.s, 8), 0, Math.PI * 2);
      ctx.strokeStyle = alpha(css('--accent'), 0.55); ctx.lineWidth = 1.5; ctx.stroke();
      ctx.fillStyle = alpha(css('--accent'), 0.04); ctx.fill();
    }
    ctx.restore();
  }
  // Where an auto-placed node probably is: a soft ellipse (about 1 sigma)
  function drawLikely() {
    ctx.save();
    for (const [key, n] of M.nodes) {
      if (n.placed || !n.auto || key === 'light') continue;
      if (showOnly && !links().some(({ n: m }) => m === n)) continue;
      ctx.beginPath();
      ctx.ellipse(X(n.x), Y(n.y), Math.max(n.auto.a * M.view.s, 6), Math.max(n.auto.b * M.view.s, 6), n.auto.rot, 0, Math.PI * 2);
      ctx.fillStyle = alpha(css('--accent'), 0.06); ctx.fill();
      ctx.strokeStyle = alpha(css('--accent'), 0.35); ctx.lineWidth = 1; ctx.setLineDash([2, 4]); ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.restore();
  }
  // Each link's excess activity: its strength over its own idle level (a slow
  // low envelope: falls at once, rises over ~1 min), so 0 = idle, 1 = clearly moving
  function excess(r, now) {
    const st = strength(r);
    if (st === null) return null;
    let b = track.base.get(r.key);
    if (b === undefined || st < b) b = st;
    else b += (st - b) * Math.min(1, (now - (track.baseAt || now)) / 60000);
    track.base.set(r.key, b);
    return Math.max(0, st - b) / Math.max(0.3, 1 - b);
  }
  // One step (2 a second): find the people, then carry each one's track on.
  //  1. every link's sensitivity on the grid, once
  //  2. person 1: the spot (and how much they move) that best explains every
  //     link's excess activity; take what they explain away from each link
  //  3. what is left: is another person there? Only if one explains the rest
  //     clearly better than noise would (MULTI_GAIN), up to three
  //  4. each found person joins the nearest track (a Bayes filter each: last
  //     estimate spread by how far a person walks meanwhile, times now), or starts one
  const MAX_PEOPLE = 3, MULTI_GAIN = 4, MULTI_LEFT = 1.5, MULTI_SEP = 2.5, KEEP_MS = 2500;
  const PERSON_C = ['--motion', '--warn', '#b48cff'];
  // Which end is the motion at? Links that share an end all react to motion
  // there; the others don't. So: the end whose links react much more than the
  // rest. No position needed for it: a router is just the end its links share.
  function nearEnd(obs, t) {
    const ends = new Map();
    for (const o of obs)
      for (const m of [o.A, o.N]) {
        const e = ends.get(m) || { m, sum: 0, n: 0, hot: 0, router: m !== HUB && !m.board && o.ap && m === o.N };
        e.sum += o.a; e.n++; if (o.a >= 0.6) e.hot++;
        ends.set(m, e);
      }
    const total = obs.reduce((s2, o) => s2 + o.a, 0);
    let best = null;
    for (const e of ends.values()) {
      if (e.n < 2) continue;
      const inside = e.sum / e.n, rest = obs.length > e.n ? (total - e.sum) / (obs.length - e.n) : 0;
      if (inside < 0.8 || e.hot / e.n < 0.67 || rest > 0.5 * inside) continue;
      const score = inside - rest;
      if (!best || score > best.score) best = { ...e, score };
    }
    if (best) track.near = { m: best.m, router: best.router, at: t,
      name: best.m === HUB ? 'the hub' : best.router ? (best.m.name || 'the router') : boardWord(best.m.name || '') };
    else if (track.near && t - track.near.at > 2500) track.near = null;
  }
  function stepTrack(t) {
    if (t - track.at < 500) return;
    const dt = Math.min(5, (t - track.at) / 1000);
    track.at = t;
    const L = links().filter(({ d, r }) => d >= 0.3 && !weak(r) && judged(r));
    const obs = L.map(({ a, n, r }) => ({ a: excess(r, t), ends: endSamples(a, n), A: a, N: n, ap: !!r.ap })).filter((o) => o.a !== null);
    track.baseAt = t;
    nearEnd(obs, t);
    const active = obs.length >= 2 && obs.some((o) => o.a >= 0.6);
    // tracks not seen lately end
    track.people = track.people.filter((p) => t - p.seen < KEEP_MS);
    if (!active) { if (track.people.length) dirty = true; track.est = track.people[0] ? track.people[0].est : null; return; }
    let x0 = 0, x1 = 0, y0 = 0, y1 = 0;
    for (const o of obs) for (const [a, n] of o.ends) for (const m of [a, n]) {
      x0 = Math.min(x0, m.x); x1 = Math.max(x1, m.x); y0 = Math.min(y0, m.y); y1 = Math.max(y1, m.y);
    }
    x0 = Math.floor(x0 - 2); y0 = Math.floor(y0 - 2); x1 = Math.ceil(x1 + 2); y1 = Math.ceil(y1 + 2);
    let cs = CELL, gw = Math.ceil((x1 - x0) / cs), gh = Math.ceil((y1 - y0) / cs);
    while (gw * gh > 12000) { cs *= 1.5; gw = Math.ceil((x1 - x0) / cs); gh = Math.ceil((y1 - y0) / cs); }
    const g = track.grid, same = g && g.x0 === x0 && g.y0 === y0 && g.gw === gw && g.gh === gh;
    if (!same) { track.grid = { x0, y0, gw, gh, cs }; for (const p of track.people) p.post = null; }
    const N = gw * gh, lam = lambda(), K = obs.length;
    const S = obs.map(() => new Float32Array(N));          // 1. sensitivities
    for (let j = 0; j < gh; j++) {
      const wy = y0 + (j + 0.5) * cs;
      for (let i = 0; i < gw; i++) {
        const wx = x0 + (i + 0.5) * cs, c = j * gw + i;
        obs.forEach((o, k) => {
          let v = 0;
          for (const [ea, en] of o.ends) v += sensitivity(wx, wy, ea, en, dist3(ea, en), lam);
          S[k][c] = v / o.ends.length;
        });
      }
    }
    const w = obs.map((o) => 1 / (SIG0 + SIG1 * o.a) ** 2);
    const G0 = 1.2, WG = 1 / 1.5 ** 2;
    // misfit of "one person at c" for these activities: [R per cell, g per cell]
    const fitOne = (act) => {
      const R = new Float32Array(N), Gc = new Float32Array(N);
      for (let c = 0; c < N; c++) {
        let num = G0 * WG, den = WG;
        for (let k = 0; k < K; k++) { const v = S[k][c]; num += w[k] * act[k] * v; den += w[k] * v * v; }
        const gg = Math.max(0, num / den);
        let r = (gg - G0) ** 2 * WG;
        for (let k = 0; k < K; k++) r += w[k] * (act[k] - gg * S[k][c]) ** 2;
        R[c] = r; Gc[c] = gg;
      }
      return [R, Gc];
    };
    const misfit0 = (act) => act.reduce((sum, a, k) => sum + w[k] * a * a, 0);   // nobody there
    let act = obs.map((o) => o.a);
    const found = [];
    for (let p = 0; p < MAX_PEOPLE; p++) {
      if (!act.some((a) => a >= 0.6)) break;
      const [R, Gc] = fitOne(act);
      let best = 0;
      for (let c = 1; c < N; c++) if (R[c] < R[best]) best = c;
      // another person: some link still clearly moving that person 1 doesn't
      // explain, and a second spot that explains it (not just noise)
      if (p > 0 && (Math.max(...act) < MULTI_LEFT || misfit0(act) - R[best] < MULTI_GAIN)) break;
      const bx = x0 + ((best % gw) + 0.5) * cs, by = y0 + (Math.floor(best / gw) + 0.5) * cs;
      if (found.some((f) => Math.hypot(f.x - bx, f.y - by) < MULTI_SEP)) break;   // the same person's leftovers
      found.push({ R, Rmin: R[best], x: bx, y: by });
      act = act.map((a, k) => Math.max(0, a - Gc[best] * S[k][best]));
    }
    // 4. tracks: the nearest free track within 3 m, else a new one
    const used = new Set();
    for (const f of found) {
      let bi = -1, bd = 1e9;
      // where this detection peaks
      let pk = 0;
      for (let c = 1; c < N; c++) if (f.R[c] < f.R[pk]) pk = c;
      const px = x0 + ((pk % gw) + 0.5) * cs, py = y0 + (Math.floor(pk / gw) + 0.5) * cs;
      track.people.forEach((tp, i) => {
        if (used.has(i) || !tp.est) return;
        const dd = Math.hypot(tp.est.x - px, tp.est.y - py);
        if (dd < bd) { bd = dd; bi = i; }
      });
      // far from every track: a new person only if there are more people than tracks,
      // else the nearest free track jumps (an estimate can leap a few metres)
      if (bi < 0 || (bd > 3 && found.length > track.people.length)) {
        if (track.people.length >= MAX_PEOPLE) continue;
        track.people.push({ id: ++track.nextId, post: null, est: null, trail: [], seen: t, speed: 0, heading: null,
          img: document.createElement('canvas') });
        bi = track.people.length - 1;
      }
      used.add(bi);
      updatePerson(track.people[bi], f, t, dt);
    }
    // tracks no detection matched this step: gone once not seen for a second
    track.people = track.people.filter((tp, i) => used.has(i) || t - tp.seen < 1000);
    track.people.sort((p1, p2) => p1.id - p2.id);
    track.people.forEach((p, i) => { p.color = PERSON_C[i % PERSON_C.length]; });
    track.est = track.people[0] ? track.people[0].est : null;
    dirty = true;
  }
  function updatePerson(p, f, t, dt) {
    const { x0, y0, gw, gh, cs } = track.grid, N = gw * gh;
    const prior = p.post ? blur(p.post, gw, gh, Math.max(1, Math.round(1.0 * dt / cs))) : null;
    const post = new Float32Array(N);
    let sum = 0;
    for (let c = 0; c < N; c++) {
      post[c] = Math.exp(-(f.R[c] - f.Rmin) / 2) * (prior ? 0.85 * prior[c] + 0.15 / N : 1);
      sum += post[c];
    }
    for (let c = 0; c < N; c++) post[c] /= sum;
    const sm = blur(post, gw, gh, Math.max(1, Math.round(0.5 / cs)));
    let best = 0, mx = 0;
    for (let c = 0; c < N; c++) { if (sm[c] > sm[best]) best = c; if (post[c] > mx) mx = post[c]; }
    const sx = x0 + ((best % gw) + 0.5) * cs, sy = y0 + (Math.floor(best / gw) + 0.5) * cs;
    const byDist = [];
    for (let c = 0; c < N; c++)
      if (post[c] > 1e-7) byDist.push([Math.hypot(x0 + ((c % gw) + 0.5) * cs - sx, y0 + (Math.floor(c / gw) + 0.5) * cs - sy), post[c]]);
    byDist.sort((q1, q2) => q1[0] - q2[0]);
    let acc = 0, r68 = cs;
    for (const [dd, pp] of byDist) { acc += pp; r68 = dd; if (acc >= 0.68) break; }
    p.post = post;
    p.est = { x: sx, y: sy, r: Math.hypot(Math.max(cs, r68), MODEL_ERR) };
    p.seen = t;
    p.trail.push({ x: sx, y: sy, t });
    while (p.trail.length && t - p.trail[0].t > 12000) p.trail.shift();
    // speed and heading: over the last ~2 s of the track, smoothed
    const back = p.trail.find((q) => t - q.t <= 2200);
    if (back && t - back.t >= 900) {
      const dx = sx - back.x, dy = sy - back.y, sp = Math.hypot(dx, dy) / ((t - back.t) / 1000);
      p.speed = 0.6 * p.speed + 0.4 * Math.min(sp, 4);
      if (Math.hypot(dx, dy) > 0.4) p.heading = Math.atan2(dy, dx);
    }
    const c = p.img, rgb = rgbOf(p.color && p.color.startsWith('--') ? css(p.color) : p.color || css('--motion'));
    c.width = gw; c.height = gh;
    const im = c.getContext('2d').createImageData(gw, gh);
    for (let k = 0; k < N; k++) {
      const q = post[k] / mx;
      if (q < 0.08) continue;
      im.data[4 * k] = rgb[0]; im.data[4 * k + 1] = rgb[1]; im.data[4 * k + 2] = rgb[2];
      im.data[4 * k + 3] = 235 * Math.min(1, q ** 0.8);
    }
    c.getContext('2d').putImageData(im, 0, 0);
  }
  // a box blur, r cells, applied twice (close to a Gaussian)
  function blur(src, gw, gh, r) {
    let a = src, b = new Float32Array(src.length);
    for (let pass = 0; pass < 2; pass++) {
      for (let j = 0; j < gh; j++) {
        let acc = 0;
        for (let i = -r; i <= r; i++) acc += a[j * gw + Math.min(gw - 1, Math.max(0, i))];
        for (let i = 0; i < gw; i++) {
          b[j * gw + i] = acc / (2 * r + 1);
          acc += a[j * gw + Math.min(gw - 1, i + r + 1)] - a[j * gw + Math.max(0, i - r)];
        }
      }
      const c = new Float32Array(src.length);
      for (let i = 0; i < gw; i++) {
        let acc = 0;
        for (let j = -r; j <= r; j++) acc += b[Math.min(gh - 1, Math.max(0, j)) * gw + i];
        for (let j = 0; j < gh; j++) {
          c[j * gw + i] = acc / (2 * r + 1);
          acc += b[Math.min(gh - 1, j + r + 1) * gw + i] - b[Math.max(0, j - r) * gw + i];
        }
      }
      a = c;
    }
    return a;
  }
  // Each person: their probability haze, a fading trail, a dot with a ring
  // (68% + the model's error) and, once they move, an arrow for their heading;
  // labelled with their speed. Several people: each in their own colour.
  const boardWord = (label) => (/^\d+$/.test(String(label)) ? `board ${label}` : String(label));
  // A person's spot in words: near a board, or between the two nearest
  function nearestBoard(est) {
    if (!est) return null;
    const bs = [[HUB, 'the hub'], ...others.map((b) => [M.nodes.get(b.key), boardWord(b.label)])].filter(([m]) => m);
    bs.sort((p, q) => Math.hypot(p[0].x - est.x, p[0].y - est.y) - Math.hypot(q[0].x - est.x, q[0].y - est.y));
    if (!bs.length) return null;
    const d0 = Math.hypot(bs[0][0].x - est.x, bs[0][0].y - est.y);
    if (bs.length > 1) {
      const d1 = Math.hypot(bs[1][0].x - est.x, bs[1][0].y - est.y);
      if (d1 < 1.4 * d0 && d0 > 1) return `between ${bs[0][1]} and ${bs[1][1]}`;
    }
    return d0 < 4 ? `near ${bs[0][1]}` : `${fmtLen(d0)} from ${bs[0][1]}`;
  }
  function drawNear(t) {
    const nr = track.near;
    if (!nr || !nr.m) return;
    const px = X(nr.m.x), py = Y(nr.m.y), ph = (t % 1600) / 1600;
    ctx.save();
    ctx.beginPath();
    ctx.arc(px, py, 16 + 26 * ph, 0, Math.PI * 2);
    ctx.strokeStyle = alpha(css('--motion'), 0.7 * (1 - ph)); ctx.lineWidth = 3; ctx.stroke();
    ctx.font = '600 12px system-ui, sans-serif'; ctx.textAlign = 'center';
    const txt = `motion near ${nr.name}`;
    const w = ctx.measureText(txt).width + 12;
    ctx.fillStyle = alpha(css('--bg') || '#000', 0.75);
    ctx.fillRect(px - w / 2, py - 44, w, 18);
    ctx.fillStyle = css('--motion'); ctx.fillText(txt, px, py - 31);
    ctx.restore();
    dirty = true;
  }
  function drawTrack(t) {
    const g = track.grid;
    if (!g || !track.people.length) return;
    const many = track.people.length > 1;
    track.people.forEach((p, i) => {
      const e = p.est;
      if (!e) return;
      const col = p.color.startsWith('--') ? css(p.color) : p.color;
      const fade = Math.max(0, 1 - Math.max(0, t - p.seen - 800) / (KEEP_MS - 800));
      ctx.save();
      ctx.imageSmoothingEnabled = true;
      ctx.globalAlpha = 0.9 * fade;
      ctx.drawImage(p.img, X(g.x0), Y(g.y0), g.gw * g.cs * M.view.s, g.gh * g.cs * M.view.s);
      ctx.globalAlpha = fade;
      const tr = p.trail;
      for (let k = 1; k < tr.length; k++) {
        const age = (t - tr[k].t) / 12000;
        ctx.strokeStyle = alpha(col, 0.7 * (1 - age)); ctx.lineWidth = 1 + 3 * (1 - age); ctx.lineCap = 'round';
        ctx.beginPath(); ctx.moveTo(X(tr[k - 1].x), Y(tr[k - 1].y)); ctx.lineTo(X(tr[k].x), Y(tr[k].y)); ctx.stroke();
      }
      const x = X(e.x), y = Y(e.y), rr = Math.max(12, e.r * M.view.s);
      const pulse = reduced.matches ? 0 : 0.5 + 0.5 * Math.sin(t / 300 + i);
      ctx.shadowColor = col; ctx.shadowBlur = 12 + 10 * pulse;
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
      if (p.heading !== null && p.speed > 0.25) {     // heading: an arrow out of the dot
        const a = p.heading, L2 = 14 + 10 * Math.min(1, p.speed);
        ctx.strokeStyle = col; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(x + 7 * Math.cos(a), y + 7 * Math.sin(a)); ctx.lineTo(x + L2 * Math.cos(a), y + L2 * Math.sin(a)); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x + (L2 + 5) * Math.cos(a), y + (L2 + 5) * Math.sin(a));
        ctx.lineTo(x + L2 * Math.cos(a + 0.5) , y + L2 * Math.sin(a + 0.5));
        ctx.lineTo(x + L2 * Math.cos(a - 0.5), y + L2 * Math.sin(a - 0.5));
        ctx.closePath(); ctx.fill();
      }
      ctx.strokeStyle = alpha(col, 0.85); ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.arc(x, y, rr, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
      const who = many ? `person ${i + 1}` : 'someone here';
      const sp = p.speed > 0.25 ? ` · ${M.unit === 'ft' ? (p.speed / FT).toFixed(1) + ' ft/s' : p.speed.toFixed(1) + ' m/s'}` : ' · still-ish';
      label(x, i % 2 ? y - rr - 34 : y + rr + 4, who, `within ${fmtLen(e.r)}${sp}`, col);   // alternate: below, above
    });
  }

  // A walk: a glowing line through the boards in the order their motion
  // started, an arrow at each step and a dot running along it; fades in ~8 s
  const WALK_S = 8000;
  function walkPts(keys) {
    return keys.map((k) => (k === 'hub' ? HUB : M.nodes.get(k))).filter(Boolean);
  }
  function drawWalk(t) {
    const w = fx.walk;
    if (!w) return;
    const age = t - w.t0;
    if (age > WALK_S) { fx.walk = null; return; }
    const pts = walkPts(w.keys);
    if (pts.length < 2) return;
    const fade = reduced.matches ? 1 : Math.min(1, (WALK_S - age) / 1500);
    const col = css('--accent');
    ctx.save();
    ctx.globalAlpha = fade;
    ctx.lineCap = ctx.lineJoin = 'round';
    for (const [lw, a] of [[10, 0.12], [3, 0.85]]) {
      ctx.strokeStyle = alpha(col, a); ctx.lineWidth = lw;
      ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y)))); ctx.stroke();
    }
    ctx.fillStyle = col;
    for (let i = 1; i < pts.length; i++) {        // an arrow at each step's middle
      const a = pts[i - 1], b = pts[i], mx = (X(a.x) + X(b.x)) / 2, my = (Y(a.y) + Y(b.y)) / 2;
      const ang = Math.atan2(Y(b.y) - Y(a.y), X(b.x) - X(a.x));
      ctx.save(); ctx.translate(mx, my); ctx.rotate(ang);
      ctx.beginPath(); ctx.moveTo(8, 0); ctx.lineTo(-6, -6); ctx.lineTo(-6, 6); ctx.closePath(); ctx.fill();
      ctx.restore();
    }
    if (!reduced.matches) {                       // the walker: a dot along the path, over and over
      const seg = pts.slice(1).map((b, i) => Math.hypot(X(b.x) - X(pts[i].x), Y(b.y) - Y(pts[i].y)));
      const total = seg.reduce((x, y) => x + y, 0) || 1;
      let d = ((age / 2200) % 1) * total, i = 0;
      while (i < seg.length - 1 && d > seg[i]) d -= seg[i++];
      const a = pts[i], b = pts[i + 1], f = seg[i] ? d / seg[i] : 0;
      const x = X(a.x) + (X(b.x) - X(a.x)) * f, y = Y(a.y) + (Y(b.y) - Y(a.y)) * f;
      ctx.shadowColor = col; ctx.shadowBlur = 14;
      ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
  }
  function showPath(keys) {
    if (!keys || keys.length < 2) return false;
    fx.walk = { keys, t0: performance.now() };
    dirty = true;
    return focusKey(keys[keys.length - 1]);
  }
  function label(x, y, text, sub, color) {
    ctx.font = '600 12px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    ctx.lineWidth = 3; ctx.strokeStyle = css('--bg'); ctx.strokeText(text, x, y);
    ctx.fillStyle = color; ctx.fillText(text, x, y);
    if (sub) {
      ctx.font = '11px ui-monospace, monospace';
      ctx.strokeText(sub, x, y + 15); ctx.fillStyle = css('--muted'); ctx.fillText(sub, x, y + 15);
    }
  }
  function loop(t) {
    if (!visible) { raf = 0; return; }
    budget(t);
    // Always animating (the particles are the links' real flow), except with
    // reduced motion: then only on a change. Lowest quality: every other frame
    const live = !reduced.matches || fx.waves.length;
    fx.skip = fx.quality === 0 && !fx.skip;
    if ((dirty || live) && !fx.skip) { draw(t); dirty = false; }
    raf = requestAnimationFrame(loop);
  }

  // ---------- the list beside the map ----------
  // A router's address, from its key: 'r:<bssid>' or the hub's 'ap:N'
  function bssidOf(key) {
    if (key.startsWith('r:')) return key.slice(2);
    const m = key.match(/^ap:(\d)$/);
    const r = m && routersInfo.find((x) => x.idx === +m[1]);
    return r ? r.bssid : null;
  }
  // The list has three parts: the board picker, the floors panel and the
  // links. Only the first and last are rebuilt on every update; the floors
  // panel stays in the page (moving it closes an open picker on a phone).
  let listParts = null;
  function renderList() {
    const root = $('#map-links');
    if (!listParts || listParts.top.parentNode !== root) {
      listParts = { top: el('div'), floors: el('div'), list: el('div') };
      root.replaceChildren(listParts.top, listParts.floors, listParts.list);
    }
    let box = listParts.top;
    box.replaceChildren();
    if (others.length) {                       // which board's links to show
      const seg = el('div', { className: 'seg map-show' });
      for (const [k, name] of [[null, 'All'], ['hub', 'Hub'], ...others.map((b) => [b.key, b.label])]) {
        const b = el('button', { type: 'button', textContent: name, className: showOnly === k ? 'on' : '' });
        b.onclick = () => {
          showOnly = k;                          // this page only: the layout itself lives on the PC
          heatKey = ''; dirty = true; renderList();
        };
        seg.append(b);
      }
      box.append(el('div', { className: 'muted small', textContent: 'Show the links of' }), seg);
    }
    const fp = floorsPanel();
    if (listParts.floors.firstChild !== fp) listParts.floors.replaceChildren(fp);
    box = listParts.list;
    box.replaceChildren();
    const L = links();
    if (!L.length) box.append(el('p', { className: 'muted small', textContent: 'No live links yet.' }));
    for (const { r, n, color, d } of L) {
      const mv = moving(r);
      const st = r.board ? { motion: 'MOTION', still: 'still', calibrating: 'calibrating', silent: 'not heard' }[r.state] || r.state :
        r.key === 'ap:0' ? (boardMotion ? 'MOTION' : 'still') :
        r.state ? { motion: 'MOTION', still: 'still', calibrating: 'calibrating', weak: 'too weak to judge', silent: 'no signal' }[r.state] || r.state
          : 'turn on per-link analysis (Links page) to judge it';
      const dot = el('span', { className: 'map-dot' }); dot.style.background = color;
      const head = el('div', { className: 'map-link-head' }, dot, el('b', { textContent: r.label }),
        r.via ? el('span', { className: 'muted small', textContent: `on ${r.via}` }) : null,
        el('span', { className: 'chip' + (mv ? ' motion' : ''), textContent: st }));
      const rows = [];
      if (r.rssi !== null && r.rssi !== undefined) {
        const exp = expected(r, d), diff = Math.round(r.rssi - exp);
        const q = r.rssi <= LIMIT_WEAK ? 'too weak: below ackwave’s ' + LIMIT_WEAK + ' dBm limit'
          : r.rssi <= LIMIT_DROP ? 'weak: below ' + LIMIT_DROP + ' dBm' : r.rssi > -60 ? 'strong' : 'good';
        rows.push(r.rate !== undefined ? `${r.rssi} dBm (${q}) · ${r.rate} answers a second` : `${r.rssi} dBm (${q}), as that board hears it`);
        rows.push(`Distance ${fmtLen(d)} · motion zone ${fmtLen(2 * zoneR(1, d / 2, d / 2))} wide in the middle`);
        rows.push(Math.abs(diff) <= 3 ? `About what open air would give here (${Math.round(exp)} dBm)`
          : diff < 0 ? `${-diff} dB weaker than open air would give here (${Math.round(exp)} dBm): walls, furniture or people in between`
            : `${diff} dB stronger than expected here (${Math.round(exp)} dBm): it may be closer than placed, or a strong device`);
      }
      if (n !== HUB && r.ap && !n.board) rows.push(whereSure(n));
      if (n !== HUB && !n.placed && !n.hint) {
        const au = n.auto;
        rows.push(au ? `Placed automatically from ${au.heardBy} board${au.heardBy > 1 ? 's' : ''} that hear it, within about ${fmtLen(au.a)}`
          + (au.heardBy === 1 ? ' (one board gives only a distance: place more boards to pin it down)' : au.heardBy === 2 ? ' (two boards: it could be the mirror spot)' : '')
          + '. Drag it if you know better.'
          : 'Placed from its signal (rough): drag it to where it really is, or place the boards that hear it.');
      } else if (n !== HUB && n.placed && !n.board && !r.ap) {
        const b = el('button', { type: 'button', className: 'ghost small', textContent: 'Place automatically' });
        b.onclick = () => unplace(n.key);
        rows.push(b);
      }
      if (weak(r)) rows.push(`Weak (${WEAK_DBM} dBm or less): drawn faint; its board relies on it little.`);
      const bssid = r.ap ? bssidOf(n.key || '') : null;
      if (bssid) {                               // name a router once, every page and board uses it
        const nm = el('button', { type: 'button', className: 'ghost small', textContent: 'Name this router' });
        nm.onclick = () => {
          const name = prompt(`A name for the router ${bssid} (e.g. Main, Main upstairs, Secondary). Empty: back to the default.`, n.name || '');
          if (name !== null) window.ackCmd({ cmd: 'router_name', bssid, name });
        };
        rows.push(nm);
      }
      box.append(el('div', { className: 'map-link' + (mv ? ' moving' : '') }, head,
        ...rows.map((t) => (typeof t === 'string' ? el('div', { className: 'muted small', textContent: t }) : t))));
    }
    $('#map-wave').textContent = `Channel ${channel} · ${(freq() / 1e6).toFixed(0)} MHz · wavelength ${M.unit === 'ft' ? (lambda() / IN).toFixed(1) + '″' : (lambda() * 100).toFixed(1) + ' cm'}`;
  }

  // What the boards measured about the house, and placements that don't fit
  function envNote() {
    const box = el('div', { className: 'map-env-fit' });
    if (!envFit) {
      box.append(el('p', { className: 'muted small', textContent:
        'Place the hub and at least two boards: their signals to each other then measure how this house fades a signal (instead of the environment picked above).' }));
      return box;
    }
    const f = envFit;
    box.append(el('p', { className: 'small', textContent:
      `Measured from ${f.pairs} board-to-board signals: signal fades with n = ${f.n.toFixed(1)} here` +
      (f.measuredLf ? `, a floor costs about ${Math.round(f.lf)} dB.` : ` (a floor counted as ${FLOOR_LOSS_DB} dB until boards on two floors hear each other).`) }));
    const nm = (k) => (k === 'hub' ? 'Hub' : (others.find((o) => o.key === k) || {}).label || k);
    for (const c of f.checks.slice(0, 3))
      box.append(el('p', { className: 'small warn-text', textContent:
        `${nm(c.by)} hears ${nm(c.key)} ${Math.abs(Math.round(c.off))} dB ${c.off < 0 ? 'weaker' : 'stronger'} than their places suggest: ` +
        (c.off < 0 ? 'a wall or appliance between them, or one is farther than placed.' : 'one may be closer than placed (or on another floor).') }));
    return box;
  }
  // Which floor each thing is on: the hub, the boards, then routers and devices
  // (those can be left to the signals). Floors are M.floorH apart.
  let floorsOpen = false, floorsEl = null, floorsSig = '', floorsTouched = 0;
  // Someone is using the panel: a picker or box has focus, or it was touched
  // in the last few seconds (a phone's picker may not keep the focus).
  const floorsBusy = () => !!floorsEl && (floorsEl.contains(document.activeElement) || performance.now() - floorsTouched < 4000);
  function floorsPanel() {
    const sig = JSON.stringify([M.hubFacing, others.map((b) => (M.nodes.get(b.key) || {}).facing), envFit && [envFit.n.toFixed(1), Math.round(envFit.lf), envFit.checks.map((c) => c.key + c.by)],
      M.hubFloor, M.floorH, others.map((b) => [b.key, b.label]),
      links().map(({ n }) => [n.key, n.name, n.floor, n.autoFloor]), [...M.nodes.values()].map((n) => n.floor)]);
    if (floorsEl && (sig === floorsSig || floorsBusy())) return floorsEl;   // unchanged, or in use: keep it
    floorsSig = sig;
    const det = el('details', { className: 'map-floors', open: floorsOpen });
    det.ontoggle = () => { floorsOpen = det.open; };
    for (const ev of ['pointerdown', 'touchstart', 'focusin', 'input', 'change'])
      det.addEventListener(ev, () => { floorsTouched = performance.now(); }, { passive: true });
    det.append(el('summary', { textContent: 'Floors, facing and signal' }));
    const row = (name, val, auto, set) => {
      const sel = el('select', { className: 'small' });
      if (auto !== null) sel.append(el('option', { value: '0', textContent: auto ? `auto (${auto})` : 'auto' }));
      for (let f = 1; f <= 4; f++) sel.append(el('option', { value: String(f), textContent: `Floor ${f}` }));
      sel.value = String(val || 0);
      sel.onchange = () => { set(+sel.value); locAt = 0; heatKey = ''; dirty = true; floorsEl = null; renderList(); scheduleSave(); };
      return el('label', { className: 'map-floor-row' }, el('span', { textContent: name }), sel);
    };
    const aim = (m, clear) => {                    // facing: shown, and clearable
      const f = facingOf(m);
      if (typeof f !== 'number') return el('span', { className: 'muted small', textContent: 'facing: drag its arrow' });
      const compass = ['east', 'south-east', 'south', 'south-west', 'west', 'north-west', 'north', 'north-east'];
      const i = ((Math.round(f / (Math.PI / 4)) % 8) + 8) % 8;
      const b = el('button', { type: 'button', className: 'ghost small', textContent: `faces ${compass[i]} · clear` });
      b.onclick = () => { clear(); heatKey = ''; locAt = 0; dirty = true; floorsEl = null; renderList(); scheduleSave(); };
      return b;
    };
    det.append(row('Hub', M.hubFloor, null, (f) => { M.hubFloor = f || 1; }), aim(HUB, () => { M.hubFacing = null; }));
    for (const b of others) {
      const n = M.nodes.get(b.key);
      if (n) det.append(row(b.label, n.floor || M.hubFloor, null, (f) => { n.floor = f; }), aim(n, () => { n.facing = undefined; }));
    }
    const seen = new Set();
    for (const { n, r } of links()) {
      if (n === HUB || n.board || n.key.startsWith('sat:') || seen.has(n.key)) continue;
      seen.add(n.key);
      det.append(row(r.link ? r.link.name || n.name : r.label, n.floor, n.autoFloor ? `Floor ${n.autoFloor}` : '', (f) => { n.floor = f; }));
    }
    const h = el('input', { type: 'number', min: '2', max: '6', step: '0.1', value: String(M.floorH), className: 'small' });
    h.onchange = () => { const v = +h.value; if (v >= 2 && v <= 6) { M.floorH = v; locAt = 0; heatKey = ''; dirty = true; scheduleSave(); } };
    det.append(el('label', { className: 'map-floor-row' }, el('span', { textContent: 'Floor to floor (m)' }), h),
      el('p', { className: 'muted small', textContent: 'Distances count the height between floors. A router or device on auto gets the floor its signals fit best.' }),
      envNote());
    floorsEl = det;
    return det;
  }

  // ---------- touch / mouse ----------
  function hit(px, py) {
    const ln = M.nodes.get('light');
    if (ln && lightInfo && lightInfo.enabled && Math.hypot(X(ln.x) - px, Y(ln.y) - py) <= 18) return ln;
    for (const b of others) {
      const n = M.nodes.get(b.key);
      if (n && Math.hypot(X(n.x) - px, Y(n.y) - py) <= 18) return n;
    }
    for (const { n } of links().reverse()) if (Math.hypot(X(n.x) - px, Y(n.y) - py) <= 18) return n;
    return null;
  }
  function pos(e) { const b = cv.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; }
  function down(e) {
    try { cv.setPointerCapture(e.pointerId); } catch (err) { /* a synthetic or ended pointer: fine without */ }
    const [x, y] = pos(e);
    ptrs.set(e.pointerId, { x, y });
    if (ptrs.size === 2) {                         // pinch
      const [a, b] = [...ptrs.values()];
      drag = { pinch: true, dist: Math.hypot(a.x - b.x, a.y - b.y) };
      return;
    }
    if (readOnly) {                                // Home: pan only
      autoFit = false; homeTouched = true;
      drag = { pan: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y };
      return;
    }
    if (tool && tool.kind === 'move') {
      const p = plan(tool.id);
      drag = { layer: p, x0: x, y0: y, lx: p.x, ly: p.y };
      return;
    }
    const fh = tool ? null : facingHit(x, y);       // a board's arrow: aim it
    if (fh) { drag = { facing: fh }; autoFit = false; return; }
    const n = tool ? null : hit(x, y);              // while scaling or aligning, taps mark points
    if (!n && !tool && Math.hypot(X(0) - x, Y(0) - y) <= 18) {
      // the board: everything is measured from it, so moving it shifts the rest the other way
      drag = { board: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y,
        nodes: [...M.nodes.values()].map((m) => [m, m.x, m.y]), plans: M.plans.map((p) => [p, p.x, p.y]) };
      return;
    }
    drag = n ? { key: n.key } : { pan: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y, tap: true };
    autoFit = false; viewByUser = true;
    if (readOnly) homeTouched = true;
  }
  function move(e) {
    if (!ptrs.has(e.pointerId)) return;
    const [x, y] = pos(e);
    ptrs.set(e.pointerId, { x, y });
    if (!drag) return;
    if (drag.pinch && ptrs.size === 2) {
      const [a, b] = [...ptrs.values()], dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (drag.dist > 0) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, dist / drag.dist);
      drag.dist = dist;
    } else if (drag.facing) {
      const m = drag.facing, deg = Math.round(Math.atan2(y - Y(m.y), x - X(m.x)) * 180 / Math.PI / 5) * 5;
      const f = deg * Math.PI / 180;                 // 5-degree steps
      if (m === HUB) M.hubFacing = f; else m.facing = f;
      heatKey = ''; locAt = 0; dirty = true;
    } else if (drag.key) {
      const n = M.nodes.get(drag.key), [wx, wy] = toWorld(x, y);
      n.x = wx; n.y = wy;
      if (n.hint) { n.hint.x = wx; n.hint.y = wy; n.auto = null; locAt = 0; }   // the rough spot moves with it
      else n.placed = true;
      dirty = true;
      renderList();
    } else if (drag.board) {
      const dx = (x - drag.x0) / M.view.s, dy = (y - drag.y0) / M.view.s;
      for (const [m, mx, my] of drag.nodes) { m.x = mx - dx; m.y = my - dy; }
      for (const [p, px, py] of drag.plans) { p.x = px - dx; p.y = py - dy; }
      M.view.x = drag.vx - dx; M.view.y = drag.vy - dy;   // the rest stays put on screen
      dirty = true;
    } else if (drag.layer) {
      drag.layer.x = drag.lx + (x - drag.x0) / M.view.s;
      drag.layer.y = drag.ly + (y - drag.y0) / M.view.s;
      dirty = true;
    } else if (drag.pan) {
      if (Math.hypot(x - drag.x0, y - drag.y0) > 6) drag.tap = false;   // a drag, not a tap
      M.view.x = drag.vx - (x - drag.x0) / M.view.s;
      M.view.y = drag.vy - (y - drag.y0) / M.view.s;
      dirty = true;
    }
  }
  function up(e) {
    ptrs.delete(e.pointerId);
    if (drag && drag.pan && drag.tap && tool && e.type === 'pointerup') toolTap(...toWorld(...pos(e)));
    if (!readOnly && drag && (drag.key || drag.pan || drag.layer || drag.board || drag.facing)) { scheduleSave(); if (drag.facing) renderList(); }
    drag = ptrs.size === 1 ? { pan: true, x0: [...ptrs.values()][0].x, y0: [...ptrs.values()][0].y, vx: M.view.x, vy: M.view.y } : null;
  }

  // ---------- saving ----------
  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1000);
  }
  async function save() {
    const body = { unit: M.unit, env: M.env, nodes: [...M.nodes.values()].slice(0, 64).map((n) =>
      Object.assign({ key: n.key, name: String(n.name).slice(0, 64), x: n.x, y: n.y, placed: !!n.placed },
        n.floor ? { floor: n.floor } : {}, typeof n.facing === 'number' ? { facing: n.facing } : {},
        n.hint ? { hint: [n.hint.x, n.hint.y, n.hint.r] } : {})),
      hub_floor: M.hubFloor, floor_h: M.floorH };
    if (typeof M.hubFacing === 'number') body.hub_facing = M.hubFacing;
    if (M.view && viewByUser) body.view = M.view;
    body.plans = M.plans.map((p) => ({ id: p.id, name: String(p.name).slice(0, 64), x: p.x, y: p.y, w: p.w,
      rot: p.rot, opacity: p.opacity, hidden: !!p.hidden }));
    try {
      await fetch('api/map', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ackwave': '1' }, body: JSON.stringify(body) });
    } catch (e) { /* the next change tries again */ }
  }
  function apply(m) {
    if (m.unit) M.unit = m.unit;
    if (m.env) M.env = m.env;
    if (m.view) { M.view = m.view; autoFit = false; viewByUser = true; }
    if (m.hub_floor) M.hubFloor = m.hub_floor;
    if (m.floor_h) M.floorH = m.floor_h;
    if (typeof m.hub_facing === 'number') M.hubFacing = m.hub_facing;
    for (const n of m.nodes || []) M.nodes.set(n.key, { key: n.key, name: n.name || n.key, x: n.x, y: n.y, placed: !!n.placed, floor: n.floor || 0,
      facing: typeof n.facing === 'number' ? n.facing : undefined,
      hint: Array.isArray(n.hint) && n.hint.length === 3 ? { x: n.hint[0], y: n.hint[1], r: n.hint[2] } : null });
    if (m.plans) {
      M.plans = m.plans.map((p) => Object.assign({ name: p.id, rot: 0, opacity: 1, hidden: false }, p));
      for (const p of M.plans) loadPicture(p);
    }
  }
  async function loadPicture(p) {
    try {
      const r = await fetch(`api/plan?id=${p.id}`, { headers: { 'X-Ackwave': '1' } });
      if (!r.ok) throw new Error(r.status);
      p.img = await createImageBitmap(await r.blob());
      p.iw = p.img.width; p.ih = p.img.height;
    } catch (e) { p.img = null; p.missing = true; }
    dirty = true;
    renderLayers();
  }

  // ---------- picture layers: add, move, scale, align ----------
  const plan = (id) => M.plans.find((p) => p.id === id);
  function hint(text) {
    $('#map-hint').hidden = !text;
    $('#map-hint-text').textContent = text || '';
  }
  function setTool(t) {
    tool = t;
    const p = t && plan(t.id), nm = p ? `“${p.name}”` : '';
    hint(!t ? '' : t.kind === 'move' ? `Drag to slide ${nm}. Tap Done when it lines up.`
      : t.kind === 'scale' ? (t.pts.length ? 'Now tap the second point.' : `Scale ${nm}: tap a point on it whose distance to another you know (a wall, a fence, the house).`)
        : ['Align: tap a house corner on the plan.', 'Tap a second corner on the plan, far from the first.',
          'Now tap where the first corner is on the map below.', 'And where the second corner is.'][t.pts.length]);
    dirty = true;
    renderLayers();
  }
  function toolTap(wx, wy) {
    const p = plan(tool.id);
    if (!p || !p.img) { setTool(null); return; }
    tool.pts.push([wx, wy]);
    if (tool.kind === 'scale' && tool.pts.length === 2) {
      const [[ax, ay], [bx, by]] = tool.pts, now = Math.hypot(bx - ax, by - ay);
      const unit = M.unit === 'ft' ? 'feet' : 'metres';
      const got = prompt(`The real distance between those two points, in ${unit}:`, '');
      const real = parseFloat(got) * (M.unit === 'ft' ? FT : 1);
      if (now > 0 && real > 0 && Number.isFinite(real)) {
        const k = real / now;                       // grow or shrink around the first point
        p.x = ax + (p.x - ax) * k; p.y = ay + (p.y - ay) * k; p.w *= k;
        fit();                                        // show all of it at its real size
        scheduleSave();
      }
      setTool(null);
      return;
    }
    if (tool.kind === 'align' && tool.pts.length === 4) {
      // two corners in the picture's pixels, and where they belong on the map
      const a1 = toPlan(p, ...tool.pts[0]), a2 = toPlan(p, ...tool.pts[1]);
      const [d1, d2] = [tool.pts[2], tool.pts[3]];
      const la = Math.hypot(a2[0] - a1[0], a2[1] - a1[1]), ld = Math.hypot(d2[0] - d1[0], d2[1] - d1[1]);
      if (la > 1 && ld > 0.05) {
        const mpp = ld / la;
        const rot = Math.atan2(d2[1] - d1[1], d2[0] - d1[0]) - Math.atan2(a2[1] - a1[1], a2[0] - a1[0]);
        const c = Math.cos(rot), s = Math.sin(rot);
        p.rot = Math.atan2(s, c); p.w = mpp * p.iw;
        p.x = d1[0] - (a1[0] * c - a1[1] * s) * mpp;
        p.y = d1[1] - (a1[0] * s + a1[1] * c) * mpp;
        scheduleSave();
      }
      setTool(null);
      return;
    }
    setTool(tool);                                   // the next step's hint
  }
  async function addPicture(file) {
    const id = PLAN_IDS.find((i) => !plan(i));
    if (!id) { alert('Four pictures at most: remove one first.'); return; }
    if (file.size > 12 * 1024 * 1024) { alert('That picture is over 12 MB: save it smaller (a screenshot is plenty).'); return; }
    let r;
    try {
      r = await fetch(`api/plan?id=${id}`, { method: 'POST', headers: { 'X-Ackwave': '1' }, body: file });
    } catch (e) { alert('Could not send the picture.'); return; }
    const got = await r.json().catch(() => ({}));
    if (!r.ok) { alert(got.msg || 'The picture was refused.'); return; }
    // first placed to fill most of the current view, centred
    const w = Math.max(1, 0.8 * W / M.view.s);
    const p = { id, name: file.name.replace(/\.[^.]+$/, '').slice(0, 64) || id, x: 0, y: 0, w, rot: 0,
      opacity: M.plans.length ? 0.8 : 1, hidden: false };
    M.plans.push(p);
    await loadPicture(p);
    if (p.img) { p.x = M.view.x - w / 2; p.y = M.view.y - (w * p.ih / p.iw) / 2; }
    scheduleSave();
    setTool({ kind: M.plans.length === 1 ? 'scale' : 'align', id, pts: [] });
  }
  async function removePicture(p) {
    if (!confirm(`Remove the picture “${p.name}”?`)) return;
    try { await fetch(`api/plan?id=${p.id}`, { method: 'POST', headers: { 'X-Ackwave': '1' }, body: '' }); } catch (e) { /* stays on disk */ }
    M.plans = M.plans.filter((x) => x !== p);
    if (tool && tool.id === p.id) setTool(null);
    dirty = true; scheduleSave(); renderLayers();
  }
  function renderLayers() {
    const ul = $('#map-layer-list');
    if (!ul) return;
    if (!M.plans.length) {
      ul.replaceChildren(el('li', { className: 'muted', textContent: 'No pictures yet.' }));
      return;
    }
    ul.replaceChildren(...M.plans.map((p, i) => {
      const btn = (kind, text, title) => el('button', { type: 'button', textContent: text, title,
        className: tool && tool.kind === kind && tool.id === p.id ? 'on' : '',
        disabled: !p.img, onclick: () => setTool(tool && tool.kind === kind && tool.id === p.id ? null : { kind, id: p.id, pts: [] }) });
      const show = el('input', { type: 'checkbox', checked: !p.hidden, onchange: (e) => { p.hidden = !e.target.checked; dirty = true; scheduleSave(); } });
      const op = el('input', { type: 'range', min: 10, max: 100, value: Math.round(p.opacity * 100),
        oninput: (e) => { p.opacity = e.target.value / 100; dirty = true; }, onchange: scheduleSave });
      const rot = el('input', { type: 'range', min: -180, max: 180, step: 0.5, value: Math.round(p.rot * 180 / Math.PI * 2) / 2,
        oninput: (e) => {                            // rotate around the picture's centre
          const cx = p.x + (p.w / 2) * Math.cos(p.rot) - (p.w * p.ih / p.iw / 2) * Math.sin(p.rot);
          const cy = p.y + (p.w / 2) * Math.sin(p.rot) + (p.w * p.ih / p.iw / 2) * Math.cos(p.rot);
          p.rot = e.target.value * Math.PI / 180;
          p.x = cx - (p.w / 2) * Math.cos(p.rot) + (p.w * p.ih / p.iw / 2) * Math.sin(p.rot);
          p.y = cy - (p.w / 2) * Math.sin(p.rot) - (p.w * p.ih / p.iw / 2) * Math.cos(p.rot);
          dirty = true;
        }, onchange: scheduleSave });
      return el('li', {},
        el('span', { className: 'lname', textContent: `${i + 1}. ${p.name}`,
          title: p.missing ? 'the picture file is missing on the PC' : `${fmtLen(p.w)} wide` }),
        el('label', {}, show, 'show'),
        el('label', {}, 'see-through', op),
        el('label', {}, 'turn', rot),
        btn('move', 'Move', 'drag the picture'),
        btn('scale', 'Scale', 'tap two points, then type the real distance'),
        btn('align', 'Align', 'tap two corners on this picture, then the same two on the map'),
        el('button', { type: 'button', className: 'ghost', textContent: 'Remove', onclick: () => removePicture(p) }));
    }));
  }
  async function load() {
    let saved = {};
    try {
      const r = await fetch('api/map', { headers: { 'X-Ackwave': '1' } });
      saved = r.ok ? await r.json() : {};
      apply(saved);
    } catch (e) { /* start fresh */ }
    if (!saved.view) { M.view = null; autoFit = true; viewByUser = false; }   // fit to the real layout, not first guesses
    else if (readOnly && !homeTouched) {        // Home: its own fit; the saved view is the Map page's
      pageView = { ...M.view }; M.view = null; autoFit = true; homeFit = false;
    }
    loaded = true;
    syncControls();
    dirty = true;
    renderList();
  }

  // ---------- controls ----------
  function syncControls() {
    for (const b of $('#map-unit').children) b.classList.toggle('on', b.dataset.u === M.unit);
    $('#map-env').value = M.env;
  }
  function init() {
    cv = $('#map-cv'); ctx = cv.getContext('2d');
    for (const [k, v] of Object.entries(ENVS)) $('#map-env').append(el('option', { value: k, textContent: v.label }));
    $('#map-env').onchange = (e) => { M.env = e.target.value; dirty = true; renderList(); scheduleSave(); };
    for (const b of $('#map-unit').children) b.onclick = () => { M.unit = b.dataset.u; syncControls(); dirty = true; renderList(); scheduleSave(); };
    $('#map-fit').onclick = () => { fit(); viewByUser = true; scheduleSave(); };
    $('#map-file').onchange = (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) addPicture(f); };
    $('#map-hint-done').onclick = () => setTool(null);
    $('#map-auto').onclick = () => {           // routers and devices back to their signals; boards stay
      if (!confirm('Let ackwave place every router and device from the signals your boards hear? (Boards stay where you put them.)')) return;
      for (const [key, n] of M.nodes) if (!n.board && !key.startsWith('sat:') && key !== 'light') { n.placed = false; n.auto = null; }
      locAt = 0; dirty = true; renderList(); scheduleSave();
    };
    renderLayers();
    $('#map-reset').onclick = () => {
      if (!confirm('Put every dot back where its signal suggests (and forget where you placed them)?')) return;
      M.nodes.clear(); M.view = null; dirty = true; renderList(); scheduleSave();
    };
    cv.addEventListener('pointerdown', down);
    cv.addEventListener('pointermove', move);
    cv.addEventListener('pointerup', up);
    cv.addEventListener('pointercancel', up);
    cv.addEventListener('wheel', (e) => { e.preventDefault(); const [x, y] = pos(e); zoomAt(x, y, Math.exp(-e.deltaY * 0.0015)); }, { passive: false });
    window.addEventListener('resize', () => { if (visible) resize(); });
    // the map moves between Home and the Map page, and cards settle after layout:
    // redraw at the box's real size whenever it changes, so it never stretches
    if (window.ResizeObserver) new ResizeObserver(() => { if (visible) resize(); }).observe(cv.parentElement);
  }

  // Fly to a node (the live stream's entries): centre it, ripple it
  function focusKey(key) {
    const n = key === 'hub' ? HUB : M.nodes.get(key);
    if (!n || !M.view) return false;
    const from = { x: M.view.x, y: M.view.y }, t0 = performance.now(), dur = reduced.matches ? 1 : 450;
    const step = (t) => {
      const p = Math.min(1, (t - t0) / dur), e = 1 - (1 - p) ** 3;
      M.view.x = from.x + (n.x - from.x) * e; M.view.y = from.y + (n.y - from.y) * e;
      dirty = true;
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    fx.seen.set(key === 'hub' ? 'hub' : key, performance.now());       // its rings again
    return true;
  }

  // The hub's own verdict (the badge may show another board's motion)
  const hubMoving = (h) => !!(h && h.badge && (h.badge.hub !== undefined ? h.badge.hub : h.badge.word === 'MOTION'));

  let started = false;
  return {
    // Put the map in another box (Home, view only), or back on the Map page (null)
    mount(sel, ro) {
      if (!started) { init(); started = true; }
      const stage = cv.parentElement;
      if (!stageMark) { stageMark = document.createComment('map stage'); stage.after(stageMark); }
      const box = sel ? document.querySelector(sel) : null;
      if (box && stage.parentElement !== box) box.append(stage);
      if (!box && stage.nextSibling !== stageMark) stageMark.before(stage);
      if (ro && !readOnly) {                     // Home: its own view, fitted only the first time
        pageView = M.view && { ...M.view };
        if (homeView) M.view = { ...homeView }; else homeFit = true;
      }
      if (!ro && readOnly) {                     // back: the page's view
        homeView = M.view && { ...M.view };
        if (pageView) { M.view = pageView; pageView = null; }
      }
      readOnly = !!ro;
      if (readOnly && tool) setTool(null);
      dirty = true;
    },
    show() {
      if (!started) { init(); started = true; }
      visible = true;
      resize();
      if (homeFit && loaded) { fit(); homeFit = false; }
      if (!loaded) load();
      if (!raf) raf = requestAnimationFrame(loop);
    },
    hide() { visible = false; },
    // headless snapshots: the layout handed in directly, drawn at once
    snapshot(m, slow, header) {
      if (!started) { init(); started = true; }
      apply(m || {}); loaded = true; visible = true;
      syncControls(); resize();
      radio = slow.radio || []; others = slow.map_boards || []; routersInfo = slow.routers || []; if (slow.channel) channel = slow.channel;
      boardMotion = hubMoving(header);
      renderList(); draw(performance.now());
    },
    // slow payload (once a second) and the header (the board's verdict)
    update(slow, header) {
      if (slow) { radio = slow.radio || []; others = slow.map_boards || []; routersInfo = slow.routers || []; hearings = slow.hearings || {}; demoPeople = slow.demo_people || []; if (slow.channel) channel = slow.channel; }
      if (header) {
        boardMotion = hubMoving(header);
        hubAct = header.activity && header.activity.pct !== undefined ? header.activity.pct / 100 : null;
      }
      if (header && header.light) {
        lightInfo = header.light;
        const on = lightInfo.state === 'on';
        if (on && fx.lightWas === false) {      // just switched on: from the boards that saw it
          const froms = (slow && slow.house && slow.house.by || fx.by || []).map((name) => {
            if (name === 'Hub') return HUB;
            const b = others.find((o) => o.label === name);
            return b ? M.nodes.get(b.key) : null;
          }).filter(Boolean);
          for (const f of froms.length ? froms : [HUB]) fx.waves.push({ from: { x: f.x, y: f.y }, t0: performance.now() });
        }
        fx.lightWas = on;
      }
      if (slow && slow.house) fx.by = slow.house.by;
      if (slow && slow.events) {                 // a new (or longer) walk: draw it
        const w = slow.events.filter((e) => e.type === 'travel').pop();
        const sig = w ? `${w.id}:${(w.keys || []).length}` : '';
        if (w && sig !== fx.walkSeen) {
          if (fx.walkSeen) fx.walk = { keys: w.keys, t0: performance.now() };   // not one from before the page opened
          fx.walkSeen = sig;
        }
        if (!w && !fx.walkSeen) fx.walkSeen = 'none';
      }
      if (!visible || !loaded) return;
      dirty = true;
      renderList();
    },
    focus: focusKey,
    showPath,
    _likely: () => track.est,
    _people: () => track.people.map((p) => ({ id: p.id, est: p.est, speed: p.speed })),
    // For Home's status line: how many people, and where the motion is
    status: () => ({ people: track.people.length, near: track.near ? track.near.name : null,
      moving: track.people.some((p) => (p.speed || 0) > 0.3),
      where: track.people.map((p) => nearestBoard(p.est)).filter(Boolean) }),
    _screenOfWorld: (x, y) => [X(x), Y(y)],
    _view: () => ({ view: M.view, nodes: [...M.nodes.values()].map((n) => [n.key.slice(-5), +n.x.toFixed(1), +n.y.toFixed(1), !!n.placed, n.auto ? +n.auto.a.toFixed(1) : null]) }),
    _truth: () => demoPeople[0] || null,
    _truths: () => demoPeople,
    fmtLen,                                    // for tests in the browser
    _env: () => ({ fit: envFit, hearings, nodes: [...M.nodes.values()].map(({ key, x, y, placed, board }) => ({ key, x, y, placed, board })) }),
    _layers: () => M.plans.map(({ img, ...p }) => p),
    _screenOf(id, u, v) {                      // where picture pixel (u, v) is on the canvas
      const p = plan(id), k = p.w / p.iw, c = Math.cos(p.rot), s = Math.sin(p.rot);
      return [X(p.x + (u * c - v * s) * k), Y(p.y + (u * s + v * c) * k)];
    },
  };
})();
