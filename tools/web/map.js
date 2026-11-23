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
  const M = { unit: 'ft', env: 'home', view: null, nodes: new Map(), plans: [], zones: [], hubFloor: 1, floorH: 3.0, hubFacing: null, trackOff: new Set() };
  // boards you left out of the map's tracking (Floors panel): links touching them don't place people
  const offMap = (m) => (m === HUB ? M.trackOff.has('hub') : !!(m && m.key && M.trackOff.has(m.key)));
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
  let watchFramed = false;                  // Home's Watch view framed once (boards + the house)
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
  // A router your boards link to: one of the watched house's (others: a neighbour's,
  // or elsewhere on the property, are left out)
  function targetRouter(key) {
    return others.some((b) => (b.links || []).some((l) => l.key === key && l.router)) || radio.some((r) => r.ap && r.node === key);
  }
  function solveOne(key, n) {
    const obs = [];
    for (const [by] of hearings[key] || []) {
      const a = anchorOf(by);
      if (a && a !== n) obs.push([a, heardAvg.get(key + '|' + by), zOf(a), floorOf(a)]);
    }
    let hint = n.hint;                           // a rough spot: only inside it
    // a router in the watched house: automatic means somewhere in that house, never outside it
    const wz0 = watchZone();
    const inHouse = wz0 && key.startsWith('r:') && targetRouter(key);
    if (inHouse) n.inHouse = true;
    const box = inHouse ? { x0: wz0.x, x1: wz0.x + wz0.w, y0: wz0.y, y1: wz0.y + wz0.h } : null;
    if (inHouse && !hint) hint = { x: wz0.x + wz0.w / 2, y: wz0.y + wz0.h / 2, r: Math.hypot(wz0.w, wz0.h) / 2 };
    if (!obs.length) return hint ? { x: hint.x, y: hint.y, a: hint.r * 0.7, b: hint.r * 0.7, rot: 0, heardBy: 0, floor: n.floor || M.hubFloor } : null;
    // in the watched house with every board outside it: signal can't say where inside, so
    // the middle of your circle (or of the house) until the Pi's fit has an answer
    if (box && !obs.some(([a]) => inZone(wz0, a)))
      return { x: hint.x, y: hint.y, a: Math.min(hint.r, wz0.w / 2), b: Math.min(hint.r, wz0.h / 2), rot: 0, heardBy: obs.length, floor: n.floor || M.hubFloor };
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
          if (box && (gx < box.x0 || gx > box.x1 || gy < box.y0 || gy > box.y1)) continue;   // inside the watched house
          let sp = 0;
          const ls = obs.map(([a, r, az, af]) => {
            const l = nn * Math.log10(Math.max(0.5, Math.hypot(gx - a.x, gy - a.y, gz - az))) + lf * Math.abs(fl - af)
              - gainDb(a, gx, gy);                 // a router behind a board reads weaker
            sp += (r + l) * wm; return l;
          });
          const P = (sp + P0 * wp) / (obs.length * wm + wp);
          let J = (P - P0) ** 2 * wp;
          obs.forEach(([, r], i) => { J += (r - (P - ls[i])) ** 2 * wm; });
          if (box) {                               // in the watched house: signal from one side only says how far, softly; the middle unless it's clear
            J *= 0.35;
            J += ((gx - (box.x0 + box.x1) / 2) / ((box.x1 - box.x0) / 2)) ** 2 + ((gy - (box.y0 + box.y1) / 2) / ((box.y1 - box.y0) / 2)) ** 2;
          }
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
      if (n.fit && (!n.hint || Math.hypot(n.fit.x - n.hint.x, n.fit.y - n.hint.y) <= n.hint.r + 0.5)) {   // the Pi fitted it (signal + movement)
        n.x += (n.fit.x - n.x) * (n.auto ? 0.3 : 1); n.y += (n.fit.y - n.y) * (n.auto ? 0.3 : 1);
        n.auto = { x: n.fit.x, y: n.fit.y, a: 1, b: 1, rot: 0, heardBy: 0, floor: n.floor || M.hubFloor }; moved = true; continue;
      }
      if (n.hint && n.hint.r <= 1) { n.x = n.hint.x; n.y = n.hint.y; n.auto = null; continue; }   // your spot
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
  const track = { grid: null, people: [], cands: [], nextId: 0, est: null, base: new Map(), at: 0, ghosts: [] };
  // a track that ended keeps its trail on the map, fading over TRAIL_MS
  function keepGhosts(before, after, t) {
    for (const p of before) if (!after.includes(p) && p.trail.length > 1) track.ghosts.push({ trail: p.trail, color: p.color, end: t });
    track.ghosts = track.ghosts.filter((g) => t - g.end < TRAIL_MS).slice(-6);
  }
  let zoneIn = {};                                  // zone id -> people inside, from the Pi's tracking
  let breathing = {};                               // 'hub' / 'sat:<mac>' -> breathing near that board
  let learned = {};                                 // 'hub' / 'sat:<mac>' -> its learned model now: {motion, p: [empty, still, moving] %}
  let todayMap = null;                              // where people were today (the Pi's tracking): {cs, cells}
  let homeDetails = false, showToday = false;       // Home: the technical layers; today's heat (the page remembers them)
  const simple = () => readOnly && !homeDetails;   // Home's clean view
  const CELL = 0.25;
  const SIG0 = 0.45, SIG1 = 0.25, MODEL_ERR = 1.2;          // a link's scatter: idle noise, plus more the stronger it reacts
  let modelErr = MODEL_ERR;                         // the circle's floor: the walk check's measured error (from the Pi)
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
    const wz = simple() && watchZone();
    if (wz) {                                        // the Watch view: your boards and the watched house, nothing else
      const pw = [[wz.x, wz.y], [wz.x + wz.w, wz.y + wz.h]];
      for (const b of others) { const n = M.nodes.get(b.key); if (n && !offMap(n)) pw.push([n.x, n.y]); }
      if (!offMap(HUB)) pw.push([0, 0]);
      const x0 = Math.min(...pw.map((p) => p[0])), x1 = Math.max(...pw.map((p) => p[0]));
      const y0 = Math.min(...pw.map((p) => p[1])), y1 = Math.max(...pw.map((p) => p[1]));
      const sx = (Math.max(W, 200) - 90) / Math.max(x1 - x0, 2), sy = (Math.max(H, 200) - 110) / Math.max(y1 - y0, 2);
      M.view = { x: (x0 + x1) / 2, y: (y0 + y1) / 2, s: Math.min(2000, Math.max(2, Math.min(sx, sy))) };
      dirty = true; return;
    }
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
    labelBoxes = [];
    if (!loaded) return;                           // the saved layout first: guesses would set the view
    const cnt = links().length;                    // something new appeared: show it (a saved view stays at load)
    if (!M.view || (cnt > nodeCount && (readOnly ? !homeTouched : autoFit && !viewByUser)) || (readOnly && !homeTouched && simple() && watchZone() && !watchFramed)) { fit(); watchFramed = readOnly && simple() && !!watchZone(); }   // never over a view you set
    nodeCount = cnt;
    locate(t);
    stepTrack(t);
    const watchOnly = simple() && !!watchZone();   // the Watch view: a clean diagram, no pictures
    if (!watchOnly) drawPlans();
    else drawYourSide();
    if (!watchOnly && simple() && M.plans.some((p) => !p.hidden && p.img)) {   // the pictures a little darker: people and boards stand out
      ctx.fillStyle = alpha(css('--bg') || '#000000', 0.28); ctx.fillRect(0, 0, W, H);
    }
    drawToday();
    drawAcc();
    drawZones();
    if (!simple()) { grid(); drawLikely(); drawHints(); }
    // The Watch view (Home, with a watched area): beams from your boards into the
    // watched house that glow when they see motion, a cloud where they agree, and
    // nothing drawn for your own house. No pinpoint the data can't back.
    const wz = watchZone(), wv = simple() && !!wz;
    const L = wv ? links().filter(({ a, n }) => (inZone(wz, a) || inZone(wz, n)) && !offMap(a) && !offMap(n)) : links();
    const lam = lambda(), motionC = css('--motion');
    const pulse = 0.5 + 0.5 * Math.sin(t / 260);
    if (wv) drawWatchArea(wz, t);
    const hv = wv ? null : heat(L, t);             // where each link is sensitive (estimate)
    ctx.imageSmoothingEnabled = true;
    if (hv) {                                      // drawn where it was computed, moved with the view
      const k = M.view.s / hv.s;
      ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (W / 2 - (W / 2) * k + (hv.x - M.view.x) * M.view.s),
                       dpr * (H / 2 - (H / 2) * k + (hv.y - M.view.y) * M.view.s));
      if (!simple()) ctx.drawImage(heatCv, 0, 0, W, H);   // the clean view: only where motion is now
      ctx.globalAlpha = 0.45 + 0.55 * pulse;
      ctx.drawImage(heatMv, 0, 0, W, H);
      ctx.globalAlpha = 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    if (wv) drawBeams(L, t);
    for (const { r, a: e, n, color, d } of (wv ? [] : L)) {    // zones first, dots on top
      if (d < 0.05) continue;
      const rot = Math.atan2(n.y - e.y, n.x - e.x), cx = (e.x + n.x) / 2, cy = (e.y + n.y) / 2;
      const mv = moving(r), wk = weak(r) && !mv;
      if (simple()) {                              // the clean view: a thin line, lit while it sees motion
        ctx.strokeStyle = mv ? motionC : alpha(color, wk ? 0.18 : 0.35); ctx.lineWidth = mv ? 2.5 : 1;
        ctx.beginPath(); ctx.moveTo(X(e.x), Y(e.y)); ctx.lineTo(X(n.x), Y(n.y)); ctx.stroke();
        continue;
      }
      const A = (d + lam / 2) / 2, b = Math.sqrt(Math.max(A * A - d * d / 4, 0));
      const a = Math.max(A * Math.hypot(n.x - e.x, n.y - e.y) / d, b);   // seen from above
      ellipse(cx, cy, a, b, rot);                  // the first Fresnel zone: a thin outline
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
    if (!wv) particles(L, t, motionC);
    drawTrack(t);
    drawBreathing(t);
    drawNear(t);
    drawWalkCheck();
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
    for (const pl of personLabels) label(...pl);    // people's names last: they find room around everything else
    personLabels = [];
    $('#map-msg').textContent = !watchZone() ? 'Draw the house you want to watch: Map page, Draw a zone, then drag a rectangle over it.'
      : L.length ? '' : 'Waiting for live links: the router appears once the board hears it.';
  }
  // A router: how sure you are of where it is. Exactly (placed), roughly
  // (a circle you drag: auto-placement and the tracking stay inside it), or
  // not at all (auto, anywhere its signals fit).
  const SURE = [['auto', 0], ['r5', 1.524], ['r10', 3], ['r30', 9], ['r60', 18], ['exact', -1]];
  const DRAG_HINT = 3.05;                           // a router you drag: around there (10 ft); exact / 25 ft on its panel
  // A router's spot: automatic (inside the watched house, from how loudly each
  // board hears it), or where you dragged it (within 5 ft), with a way back
  function routerWhere(n) {
    const wz = watchZone();
    if (!wz) return el('span', { className: 'muted small', textContent: 'Draw the house to watch first (Draw a zone): routers are placed inside it.' });
    if (!targetRouter(n.key)) return el('span', { className: 'muted small', textContent: 'Not linked by your boards: left out (a neighbour\'s, or elsewhere).' });
    if (n.hint || n.placed) {
      const b = el('button', { type: 'button', className: 'ghost small', textContent: 'back to automatic' });
      b.onclick = () => { n.hint = null; n.placed = false; n.auto = null; locAt = 0; heatKey = ''; dirty = true; renderList(); scheduleSave(); };
      const sure = el('div', { className: 'seg small' });
      for (const [lab, r] of [['exact', 0], ['10 ft', 3.05], ['25 ft', 7.6]]) {
        const sb = el('button', { type: 'button', textContent: lab, className: (r === 0 ? n.placed : n.hint && Math.abs(n.hint.r - r) < 0.3) ? 'on' : '' });
        sb.onclick = () => { if (r === 0) { n.placed = true; n.hint = null; } else { n.placed = false; n.hint = { x: n.x, y: n.y, r }; }
          n.auto = null; locAt = 0; heatKey = ''; dirty = true; renderList(); scheduleSave(); };
        sure.append(sb);
      }
      return el('div', { className: 'map-floor-row' }, el('span', { className: 'small', textContent: 'You put it here, within ' }), sure,
        el('span', { className: 'small', textContent: ' · the Pi fine-tunes inside that · ' }), b);
    }
    return el('span', { className: 'small', textContent: n.fit ? `Placed automatically in ${wz.name} (fitted by the Pi from signal and movement). Drag it if you know better.`
      : `Placed automatically in ${wz.name}; the Pi fine-tunes it once it has seen some movement. Drag it if you know better.` });
  }
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
  // A board's learned model as a second opinion on its links (web.py _track_links trust()):
  // 'empty' makes a twitch count less, 'moving' lets it count fully (x0.35 .. x1)
  function trustOf(r) {
    const v = learned[r.board ? r.board.key : 'hub'];     // a satellite's links: its model; the hub's: the hub's
    if (!v || !v.p) return 1;
    return v.motion ? 1 : Math.max(0.35, Math.min(1, 0.35 + 1.3 * v.p[2] / 100));
  }
  function excess(r, now, k = 1) {
    const s0 = strength(r);
    if (s0 === null) return null;
    const st = s0 * k;
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
  //     estimate spread by how far a person walks meanwhile, times now), or
  //     becomes a candidate: a new person counts once found twice (one link's
  //     spike is not a person)
  //  5. a track nothing matched (someone who stopped walking) is held while the
  //     links still react the way a person at its spot makes them react: a
  //     matched filter against the links' noise, averaged over a few seconds
  // Same model and constants as track.py (the Pi's tracker): keep them in step.
  const MAX_PEOPLE = 3, MULTI_GAIN = 4, MULTI_LEFT = 1.5, MULTI_SEP = 2.5, KEEP_MS = 1500;
  const WEAK = 0.1, HOLD_MS = 300000, HOLD_Z = 1.2, HOLD_A = 0.3;   // holding a still person
  const CONFIRM = 2, CAND_MS = 1000;                // a new person: found twice, within a second
  const SPEED = 1.6, SPEED_STILL = 0.3;             // m/s a person may have moved: walking / held
  const JUMP_P = 0.02, MEAN_R = 1.2;                // evidence elsewhere entirely; the dot = the peak's mean
  const median = (v) => { const q = [...v].sort((a, b) => a - b), m = q.length >> 1; return q.length % 2 ? q[m] : (q[m - 1] + q[m]) / 2; };
  const newPerson = (t) => ({ id: 0, post: null, est: null, trail: [], seen: t, walked: t, hits: 0, hold: 4, speed: 0, heading: null,
    img: document.createElement('canvas') });
  const TRAIL_MS = 30000;                           // a person's trail: the last 30 s, fading
  const TRAIL_JUMP = 1.5;                           // m in one step (3 m/s): the estimate jumped, the trail breaks there
  const PERSON_C = ['--motion', '#5ec8ff', '#c79bff'];   // red, sky, violet: none of them the map's amber
  const RING_MAX = 3;                               // m: a wider "within" ring says little, it's drawn as a faint area
  // What every link to one router shares at the same moment is the router's end
  // (someone by the router, or the router itself changing its signal): it is
  // taken out before people are placed, so it never drags a person's dot
  // towards the router or leaves it stuck halfway. Only links from two or more
  // boards to the same router tell; what's left on each link places people.
  function routerShared(obs) {
    const by = new Map();
    for (const o of obs) {
      if (!o.ap || o.N === HUB || o.N.board) continue;
      const g = by.get(o.N) || [];
      g.push(o);
      by.set(o.N, g);
    }
    for (const g of by.values()) {
      if (new Set(g.map((o) => o.A)).size < 2) continue;
      const shared = Math.min(...g.map((o) => o.a));
      if (shared <= 0) continue;
      for (const o of g) o.a = Math.max(0, o.a - shared);
    }
  }
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
  // The watched area (a zone with "watch only here"): people are placed only
  // inside it, from the links with an end inside it (the boards' links to the
  // routers and devices over there); the router end isn't taken out, since
  // motion there is what's watched. Movement around the boards themselves
  // (your own house) then never makes a person.
  const watchZone = () => (M.zones || []).find((z) => z.watch && !z.near) || null;
  const inZone = (z, m) => !!m && m.x >= z.x && m.x <= z.x + z.w && m.y >= z.y && m.y <= z.y + z.h;
  function stepTrack(t) {
    if (t - track.at < 500) return;
    const dt = Math.min(5, (t - track.at) / 1000);
    track.at = t;
    const wz = watchZone();
    if (!wz) { track.people = []; track.near = null; return; }   // nothing to watch yet: draw the house first
    const L = links().filter(({ a, n, d, r }) => d >= 0.3 && !weak(r) && judged(r) && !offMap(a) && !offMap(n)
      && (inZone(wz, a) || inZone(wz, n)));
    const obs = L.map(({ a, n, r }) => ({ a: excess(r, t, trustOf(r)), ends: endSamples(a, n), A: a, N: n, ap: !!r.ap, key: r.key })).filter((o) => o.a !== null);
    track.baseAt = t;
    nearEnd(obs, t);
    if (!wz) routerShared(obs);
    const top = obs.reduce((m, o) => Math.max(m, o.a), 0), active = obs.length >= 2 && top >= 0.6;
    // tracks with no evidence lately end (and anyone held HOLD_MS without walking)
    { const before = track.people; track.people = track.people.filter((p) => t - p.seen < KEEP_MS && t - p.walked < HOLD_MS); keepGhosts(before, track.people, t); }
    const faint = obs.length >= 2 && top >= WEAK && track.people.length > 0;   // someone to hold, and something to hold them on
    if (!active && !faint) { if (track.people.length) dirty = true; track.est = track.people[0] ? track.people[0].est : null; return; }
    let x0 = 0, x1 = 0, y0 = 0, y1 = 0;
    for (const o of obs) for (const [a, n] of o.ends) for (const m of [a, n]) {
      x0 = Math.min(x0, m.x); x1 = Math.max(x1, m.x); y0 = Math.min(y0, m.y); y1 = Math.max(y1, m.y);
    }
    x0 = Math.floor(x0 - 2); y0 = Math.floor(y0 - 2); x1 = Math.ceil(x1 + 2); y1 = Math.ceil(y1 + 2);
    if (wz) { x0 = Math.floor(wz.x); y0 = Math.floor(wz.y); x1 = Math.ceil(wz.x + wz.w); y1 = Math.ceil(wz.y + wz.h); }   // people only in there
    let cs = CELL, gw = Math.ceil((x1 - x0) / cs), gh = Math.ceil((y1 - y0) / cs);
    while (gw * gh > 12000) { cs *= 1.5; gw = Math.ceil((x1 - x0) / cs); gh = Math.ceil((y1 - y0) / cs); }
    const g = track.grid, same = g && g.x0 === x0 && g.y0 === y0 && g.gw === gw && g.gh === gh;
    if (!same) { track.grid = { x0, y0, gw, gh, cs }; for (const p of [...track.people, ...track.cands]) p.post = null; }
    const N = gw * gh, lam = lambda(), K = obs.length;
    // 1. sensitivities: each link's over the grid, computed once and kept while
    // its ends, the grid and the channel stay the same (the heavy part: a phone
    // redid it twice a second)
    const gsig = `${x0},${y0},${gw},${gh},${cs},${lam.toFixed(4)}`;
    const endSig = (o) => o.ends.map(([ea, en]) => `${ea.x.toFixed(2)},${ea.y.toFixed(2)},${zOf(ea)},${facingOf(ea)},${en.x.toFixed(2)},${en.y.toFixed(2)},${zOf(en)},${facingOf(en)}`).join(';');
    if (track.sGrid !== gsig) { track.sCache = new Map(); track.sGrid = gsig; }
    const S = obs.map((o) => {
      const sig = endSig(o), hit = track.sCache.get(o.key);
      if (hit && hit.sig === sig) return hit.S;
      const arr = new Float32Array(N);
      for (let j = 0; j < gh; j++) {
        const wy = y0 + (j + 0.5) * cs;
        for (let i = 0; i < gw; i++) {
          const wx = x0 + (i + 0.5) * cs;
          let v = 0;
          for (const [ea, en] of o.ends) v += sensitivity(wx, wy, ea, en, dist3(ea, en), lam);
          arr[j * gw + i] = v / o.ends.length;
        }
      }
      track.sCache.set(o.key, { sig, S: arr });
      return arr;
    });
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
    // 5. a still person stays while the links still react like someone at their spot
    const hold = (p, act) => {
      if (!p.est) return;
      const ci = Math.min(gw - 1, Math.max(0, Math.floor((p.est.x - x0) / cs))) + Math.min(gh - 1, Math.max(0, Math.floor((p.est.y - y0) / cs))) * gw;
      const med = median(act), noise = Math.max(0.03, 1.4826 * median(act.map((a) => Math.abs(a - med))));
      let dot = 0, ss = 0;                            // the links' shared noise floor out: a pattern, not a level
      for (let k = 0; k < K; k++) { dot += (act[k] - med) * S[k][ci]; ss += S[k][ci] ** 2; }
      const z = dot / (noise * Math.max(1e-6, Math.sqrt(ss)));
      p.hold = (1 - HOLD_A) * p.hold + HOLD_A * Math.min(z, 6);
      if (p.hold < HOLD_Z) return;
      const [R] = fitOne(act);
      let rmin = Infinity;
      for (let c = 0; c < N; c++) if (R[c] < rmin) rmin = R[c];
      updatePerson(p, { R, Rmin: rmin }, t, dt, true);
    };
    const finish = () => {
      track.people.sort((p1, p2) => p1.id - p2.id);
      track.people.forEach((p, i) => { p.color = PERSON_C[i % PERSON_C.length]; });
      track.est = track.people[0] ? track.people[0].est : null;
      dirty = true;
    };
    track.cands = track.cands.filter((c) => t - c.seen <= CAND_MS);
    let act = obs.map((o) => o.a);
    if (!active) {                                    // only faint reactions: each held person follows them a little
      for (const p of track.people) hold(p, act);
      const before = track.people;
      track.people = track.people.filter((p) => t - p.seen < KEEP_MS);
      keepGhosts(before, track.people, t);
      finish();
      return;
    }
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
      // else the nearest free track jumps (an estimate can leap a few metres).
      // A new one is a candidate first: it counts once found again (CONFIRM)
      if (bi < 0 || (bd > 3 && found.length > track.people.length)) {
        if (track.people.length >= MAX_PEOPLE) continue;
        let cand = null, cd = 3;
        for (const c of track.cands) {
          if (c.seen >= t || !c.est) continue;
          const dd = Math.hypot(c.est.x - px, c.est.y - py);
          if (dd < cd) { cd = dd; cand = c; }
        }
        if (!cand) { cand = newPerson(t); track.cands.push(cand); }
        updatePerson(cand, f, t, dt);
        if (++cand.hits >= CONFIRM) {                 // seen again: a person
          track.cands = track.cands.filter((c) => c !== cand);
          cand.id = ++track.nextId;
          track.people.push(cand);
          used.add(track.people.length - 1);
        }
        continue;
      }
      used.add(bi);
      updatePerson(track.people[bi], f, t, dt);
    }
    // tracks no detection matched: held if their spot still reacts, else gone after KEEP_MS
    track.people.forEach((tp, i) => { if (!used.has(i)) hold(tp, act); });
    { const before = track.people; track.people = track.people.filter((tp, i) => used.has(i) || t - tp.seen < KEEP_MS); keepGhosts(before, track.people, t); }
    finish();
  }
  function updatePerson(p, f, t, dt, still = false) {
    const { x0, y0, gw, gh, cs } = track.grid, N = gw * gh;
    const prior = p.post ? blur(p.post, gw, gh, Math.max(1, Math.round((still ? SPEED_STILL : SPEED) * dt / cs))) : null;
    let psum = 0;
    if (prior) for (let c = 0; c < N; c++) psum += prior[c];
    const post = new Float32Array(N);
    let sum = 0;
    for (let c = 0; c < N; c++) {
      post[c] = Math.exp(-(f.R[c] - f.Rmin) / 2) * (prior ? (1 - JUMP_P) * prior[c] / psum + JUMP_P / N : 1);
      sum += post[c];
    }
    for (let c = 0; c < N; c++) post[c] /= sum;
    if (!still) { p.walked = t; p.hold = 4; }        // just found: plenty of evidence
    const sm = blur(post, gw, gh, Math.max(1, Math.round(0.5 / cs)));
    let best = 0, mx = 0;
    for (let c = 0; c < N; c++) { if (sm[c] > sm[best]) best = c; if (post[c] > mx) mx = post[c]; }
    // the dot: the posterior's mean over the peak's own hill, not one cell (no hopping between cells)
    const bx = x0 + ((best % gw) + 0.5) * cs, by = y0 + (Math.floor(best / gw) + 0.5) * cs;
    let ws = 0, wx = 0, wy = 0;
    for (let c = 0; c < N; c++) {
      const cx = x0 + ((c % gw) + 0.5) * cs, cy = y0 + (Math.floor(c / gw) + 0.5) * cs;
      if (Math.hypot(cx - bx, cy - by) > MEAN_R) continue;
      ws += post[c]; wx += post[c] * cx; wy += post[c] * cy;
    }
    const sx = ws > 0 ? wx / ws : bx, sy = ws > 0 ? wy / ws : by;
    const byDist = [];
    for (let c = 0; c < N; c++)
      if (post[c] > 1e-7) byDist.push([Math.hypot(x0 + ((c % gw) + 0.5) * cs - sx, y0 + (Math.floor(c / gw) + 0.5) * cs - sy), post[c]]);
    byDist.sort((q1, q2) => q1[0] - q2[0]);
    let acc = 0, r68 = cs;
    for (const [dd, pp] of byDist) { acc += pp; r68 = dd; if (acc >= 0.68) break; }
    p.post = post;
    p.est = { x: sx, y: sy, r: Math.hypot(Math.max(cs, r68), modelErr) };
    p.seen = t;
    p.trail.push({ x: sx, y: sy, t });
    while (p.trail.length && t - p.trail[0].t > TRAIL_MS) p.trail.shift();
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
  // Someone still, breathing near a board: a slow blue swell around it (4 s, a breath)
  function drawBreathing(t) {
    for (const [k, b] of Object.entries(breathing)) {
      const m = k === 'hub' ? HUB : M.nodes.get(k);
      if (!m) continue;
      const ph = 0.5 - 0.5 * Math.cos((t % 4000) / 4000 * 2 * Math.PI), r = 16 + 14 * ph;
      ctx.save();
      ctx.beginPath(); ctx.arc(X(m.x), Y(m.y), r, 0, Math.PI * 2);
      ctx.fillStyle = alpha('#6aa8ff', (b.level === 'clear' ? 0.22 : 0.12) * (0.6 + 0.4 * ph)); ctx.fill();
      ctx.strokeStyle = alpha('#6aa8ff', 0.7); ctx.lineWidth = 1.5; ctx.stroke();
      ctx.font = '600 11px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.fillStyle = '#9cc4ff';
      ctx.fillText(`${b.level === 'clear' ? 'someone still' : 'maybe someone still'} · ${Math.round(b.rate)}/min`, X(m.x), Y(m.y) - 34);
      ctx.restore();
      dirty = true;
    }
  }
  // Where people were today: the Pi counts the tracking's spots on a 0.5 m grid
  const todayCv = document.createElement('canvas');
  let todayKey = '';
  function drawToday() {
    if (!showToday || !todayMap || !todayMap.cells || !todayMap.cells.length) return;
    // drawn once into its own canvas; again only when the data or the view changes
    const key = [todayMap.cells.length, todayMap.cells[0] && todayMap.cells[0][2], M.view.x.toFixed(2), M.view.y.toFixed(2), M.view.s.toFixed(2), W, H].join('|');
    if (key !== todayKey) {
      todayKey = key;
      todayCv.width = Math.max(1, Math.round(W * dpr)); todayCv.height = Math.max(1, Math.round(H * dpr));
      const c2 = todayCv.getContext('2d');
      c2.setTransform(dpr, 0, 0, dpr, 0, 0);
      paintToday(c2);
    }
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(todayCv, 0, 0);
    ctx.restore();
  }
  function paintToday(ctx) {
    const cs = todayMap.cs, mx = Math.max(...todayMap.cells.map((c) => c[2])), r = Math.max(6, cs * 1.6 * M.view.s);
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';         // soft spots that add up where people spend time
    for (const [ix, iy, n] of todayMap.cells) {
      const q = Math.sqrt(n / mx), x = X((ix + 0.5) * cs), y = Y((iy + 0.5) * cs);
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, `rgba(255, ${Math.round(170 - 110 * q)}, 50, ${0.05 + 0.3 * q})`);
      g.addColorStop(1, 'rgba(255, 120, 50, 0)');
      ctx.fillStyle = g;
      ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
    }
    ctx.restore();
  }
  // Draw a zone: the button, then a drag on the map is the zone's rectangle
  let zoneDraw = false;
  function setZoneDraw(on) {
    zoneDraw = on;
    const b = document.getElementById('map-zone');
    if (b) { b.classList.toggle('on', on); b.textContent = on ? 'Drag on the map…' : 'Draw a zone'; }
    accNote(on ? 'Drag a rectangle over the area (e.g. the house with the routers). Then name it and tick its options in the Zones list.' : '');
  }
  function finishZone(d) {
    setZoneDraw(false);
    const x = Math.min(d.wx0, d.wx1), y = Math.min(d.wy0, d.wy1), w = Math.abs(d.wx1 - d.wx0), h = Math.abs(d.wy1 - d.wy0);
    if (w < 0.5 || h < 0.5) return;                  // a tap, not a rectangle
    const name = (prompt(watchZone() ? 'A name for this zone (e.g. Front door, Stairs)' : 'A name for the house you watch (e.g. Other house)',
      watchZone() ? `Zone ${M.zones.length + 1}` : 'Watched house') || '').slice(0, 32)
      || `Zone ${M.zones.length + 1}`;
    const first = !watchZone();                      // the first zone is the house you watch
    M.zones.push({ id: Math.random().toString(36).slice(2, 10), name, x, y, w, h, notify: first, night: false, light: false, watch: first });
    zonesOpen = true; zonesEl = null; dirty = true; renderList(); scheduleSave();
  }
  // Your side in the Watch view: a faint outline round your boards
  function drawYourSide() {
    const pts = others.map((b) => M.nodes.get(b.key)).filter((n) => n && !offMap(n));
    if (!offMap(HUB)) pts.push(HUB);
    if (!pts.length) return;
    const x0 = Math.min(...pts.map((n) => n.x)) - 1.5, x1 = Math.max(...pts.map((n) => n.x)) + 1.5;
    const y0 = Math.min(...pts.map((n) => n.y)) - 1.5, y1 = Math.max(...pts.map((n) => n.y)) + 1.5;
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.18)'; ctx.lineWidth = 1; ctx.setLineDash([3, 5]);
    ctx.strokeRect(X(x0), Y(y0), (x1 - x0) * M.view.s, (y1 - y0) * M.view.s); ctx.setLineDash([]);
    ctx.font = '600 12px system-ui, sans-serif'; ctx.fillStyle = 'rgba(255,255,255,0.45)';
    ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.fillText('Your boards', X(x0) + 4, Y(y1) + 4);
    ctx.restore();
  }
  // The watched house: a soft outline and its name
  function drawWatchArea(z, t) {
    const x = X(z.x), y = Y(z.y), w = z.w * M.view.s, h = z.h * M.view.s, occ = zoneIn[z.id] > 0;
    ctx.save();
    ctx.fillStyle = occ ? alpha(css('--motion'), 0.08) : 'rgba(255,255,255,0.03)';
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = occ ? alpha(css('--motion'), 0.9) : 'rgba(255,255,255,0.45)'; ctx.lineWidth = occ ? 2.5 : 1.5;
    ctx.setLineDash(occ ? [] : [8, 6]); ctx.strokeRect(x, y, w, h); ctx.setLineDash([]);
    ctx.font = '700 14px system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    ctx.fillStyle = occ ? css('--motion') : 'rgba(255,255,255,0.8)';
    ctx.fillText(occ ? `${z.name} · movement` : `${z.name} · quiet`, x + 4, y - 6);
    ctx.restore();
  }
  // A beam per link into the watched house: from the board to the router or
  // device, wider at the far end (where it is only roughly known), glowing with
  // how much that link reacts right now
  function drawBeams(L, t) {
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (const { r, a, n } of L) {
      const s0 = strength(r), lvl = s0 === null ? 0 : Math.min(1, s0 / 1.5);
      const ax = X(a.x), ay = Y(a.y), nx = X(n.x), ny = Y(n.y);
      const len = Math.hypot(nx - ax, ny - ay) || 1, ux = (nx - ax) / len, uy = (ny - ay) / len;
      const far = Math.max(6, ((n.hint && n.hint.r) || (n.placed ? 0.8 : 2.5)) * M.view.s * 0.8);   // its end: as unsure as the device's spot
      const g = ctx.createLinearGradient(ax, ay, nx, ny);
      const c = lvl > 0.5 ? '255, 90, 79' : '120, 200, 255';
      const op = 0.05 + 0.5 * lvl * (0.75 + 0.25 * Math.sin(t / 240));
      g.addColorStop(0, `rgba(${c}, ${op * 0.6})`); g.addColorStop(1, `rgba(${c}, ${op})`);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.moveTo(ax - uy * 2, ay + ux * 2); ctx.lineTo(nx - uy * far, ny + ux * far);
      ctx.lineTo(nx + uy * far, ny - ux * far); ctx.lineTo(ax + uy * 2, ay - ux * 2); ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }
  function drawZones() {
    if (drag && drag.zdraw) {                        // the rectangle being drawn
      const x = X(Math.min(drag.wx0, drag.wx1)), y = Y(Math.min(drag.wy0, drag.wy1));
      const w = Math.abs(drag.wx1 - drag.wx0) * M.view.s, h = Math.abs(drag.wy1 - drag.wy0) * M.view.s;
      ctx.save(); ctx.setLineDash([6, 4]); ctx.strokeStyle = css('--accent'); ctx.lineWidth = 2; ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = alpha(css('--accent'), 0.1); ctx.fillRect(x, y, w, h); ctx.restore();
    }
    for (const z of M.zones || []) {
      if (z.near) {                                  // around its router or board
        const m = z.near === 'hub' ? HUB : M.nodes.get(z.near);
        if (!m) continue;
        const occ = zoneIn[z.id] > 0, r = Math.max(22, 1.5 * M.view.s);
        ctx.save();
        ctx.beginPath(); ctx.arc(X(m.x), Y(m.y), r, 0, Math.PI * 2);
        ctx.setLineDash(occ ? [] : [6, 4]);
        ctx.strokeStyle = occ ? css('--motion') : alpha(css('--accent'), 0.7); ctx.lineWidth = occ ? 2.5 : 1.5; ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = '600 12px system-ui, sans-serif'; ctx.textAlign = 'center';
        ctx.fillStyle = occ ? css('--motion') : css('--accent');
        ctx.fillText(z.name + (occ ? ' · motion' : '') + (z.light ? ' 💡' : '') + (z.notify ? ' 🔔' : ''), X(m.x), Y(m.y) + r + 14);
        ctx.restore();
        continue;
      }
      const occ = zoneIn[z.id] > 0, x = X(z.x), y = Y(z.y), w = z.w * M.view.s, h = z.h * M.view.s;
      ctx.save();
      ctx.fillStyle = occ ? alpha(css('--motion'), 0.16) : alpha(css('--accent'), 0.06);
      ctx.fillRect(x, y, w, h);
      ctx.setLineDash(occ ? [] : [6, 4]);
      ctx.strokeStyle = occ ? css('--motion') : alpha(css('--accent'), 0.7); ctx.lineWidth = occ ? 2 : 1.5;
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
      ctx.font = '600 12px system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      ctx.fillStyle = occ ? css('--motion') : css('--accent');
      ctx.fillText(z.name + (z.watch ? ' · watched' : '') + (occ ? ' · someone here' : '') + (z.light ? ' 💡' : '') + (z.notify ? ' 🔔' : ''), x + 6, y + 5);
      if (!readOnly) {                               // the corner to resize it
        ctx.beginPath(); ctx.arc(x + w, y + h, 6, 0, Math.PI * 2);
        ctx.fillStyle = css('--accent'); ctx.fill();
      }
      ctx.restore();
    }
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
    for (const gh of track.ghosts) {                 // trails of people no longer tracked, still fading out
      const col = gh.color && gh.color.startsWith('--') ? css(gh.color) : gh.color || css('--motion'), tr = gh.trail;
      ctx.save(); ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      for (let k = 1; k < tr.length; k++) {
        const keep = (1 - Math.min(1, (t - tr[k].t) / TRAIL_MS)) ** 1.5;
        if (keep <= 0) continue;
        ctx.strokeStyle = alpha(col, 0.7 * keep); ctx.lineWidth = 1.5 + 3 * keep;
        if (Math.hypot(tr[k].x - tr[k - 1].x, tr[k].y - tr[k - 1].y) > TRAIL_JUMP) continue;   // a jump: not a path walked
        ctx.beginPath(); ctx.moveTo(X(tr[k - 1].x), Y(tr[k - 1].y)); ctx.lineTo(X(tr[k].x), Y(tr[k].y)); ctx.stroke();
      }
      ctx.restore();
      dirty = true;
    }
    if (!g || !track.people.length) return;
    const many = track.people.length > 1;
    track.people.forEach((p, i) => {
      if (!p.est) return;
      // glide: the dot eases towards each new estimate (about a quarter second) instead of jumping
      const k = p.showAt ? 1 - Math.exp(-Math.min(1000, t - p.showAt) / 250) : 1;
      p.show = p.show ? { x: p.show.x + (p.est.x - p.show.x) * k, y: p.show.y + (p.est.y - p.show.y) * k } : { x: p.est.x, y: p.est.y };
      p.showAt = t;
      const e = { ...p.est, x: p.show.x, y: p.show.y };
      if (Math.hypot(p.est.x - p.show.x, p.est.y - p.show.y) > 0.02) dirty = true;
      const col = p.color.startsWith('--') ? css(p.color) : p.color;
      const fade = Math.max(0, 1 - Math.max(0, t - p.seen - 800) / (KEEP_MS - 800));
      const still = t - (p.walked || t) > 1500, vague = e.r > RING_MAX;   // held standing; a wide guess
      ctx.save();
      ctx.imageSmoothingEnabled = true;
      ctx.globalAlpha = (vague ? 0.35 : 0.8) * fade;   // a vague person's cloud stays faint: it isn't a place
      ctx.drawImage(p.img, X(g.x0), Y(g.y0), g.gw * g.cs * M.view.s, g.gh * g.cs * M.view.s);
      ctx.globalAlpha = fade;
      const tr = p.trail;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      for (const [glow, w0] of [[true, 9], [false, 4]]) {   // a soft glow under a bright line, both fading with age
        for (let k = 1; k < tr.length; k++) {
          const age = Math.min(1, (t - tr[k].t) / TRAIL_MS), keep = (1 - age) ** 1.5;
          ctx.strokeStyle = alpha(col, (glow ? 0.18 : 0.9) * keep); ctx.lineWidth = (glow ? w0 : 1.5 + w0 * keep);
          if (Math.hypot(tr[k].x - tr[k - 1].x, tr[k].y - tr[k - 1].y) > TRAIL_JUMP) continue;   // a jump: not a path walked
          ctx.beginPath(); ctx.moveTo(X(tr[k - 1].x), Y(tr[k - 1].y)); ctx.lineTo(X(tr[k].x), Y(tr[k].y)); ctx.stroke();
        }
      }
      for (let k = 0; k < tr.length; k += 4) {            // dots along it, every couple of seconds
        const keep = 1 - Math.min(1, (t - tr[k].t) / TRAIL_MS);
        ctx.fillStyle = alpha(col, 0.8 * keep);
        ctx.beginPath(); ctx.arc(X(tr[k].x), Y(tr[k].y), 1.5 + 2 * keep, 0, Math.PI * 2); ctx.fill();
      }
      const x = X(e.x), y = Y(e.y), rr = Math.max(12, Math.min(e.r, RING_MAX) * M.view.s);
      const pulse = reduced.matches || still ? 0 : 0.5 + 0.5 * Math.sin(t / 300 + i);   // standing still: calm
      const dotR = simple() ? 8 : 5;
      ctx.shadowColor = col; ctx.shadowBlur = still ? 8 : 12 + 10 * pulse;
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(x, y, dotR, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
      if (simple()) { ctx.strokeStyle = 'rgba(255,255,255,.85)'; ctx.lineWidth = 2; ctx.stroke(); }
      if (still) {                                    // standing: a steady halo instead of an arrow
        ctx.strokeStyle = alpha(col, 0.6); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, dotR + 5, 0, Math.PI * 2); ctx.stroke();
      }
      if (!still && p.heading !== null && p.speed > 0.25) {     // heading: an arrow out of the dot
        const a = p.heading, L2 = 20 + 14 * Math.min(1, p.speed);
        ctx.strokeStyle = col; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(x + 7 * Math.cos(a), y + 7 * Math.sin(a)); ctx.lineTo(x + L2 * Math.cos(a), y + L2 * Math.sin(a)); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x + (L2 + 9) * Math.cos(a), y + (L2 + 9) * Math.sin(a));
        ctx.lineTo(x + L2 * Math.cos(a + 0.45), y + L2 * Math.sin(a + 0.45));
        ctx.lineTo(x + L2 * Math.cos(a - 0.45), y + L2 * Math.sin(a - 0.45));
        ctx.closePath(); ctx.fill();
      }
      if (!vague) {                                   // how sure: a ring you can read as "within this"
        ctx.strokeStyle = alpha(col, 0.7); ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
        ctx.beginPath(); ctx.arc(x, y, rr, 0, Math.PI * 2); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.restore();
      const who = many ? `person ${i + 1}` : 'someone here';
      const sp = still ? 'still' : p.speed > 0.25 ? (M.unit === 'ft' ? (p.speed / FT).toFixed(1) + ' ft/s' : p.speed.toFixed(1) + ' m/s') : 'moving';
      personLabels.push([x, y + Math.min(rr, 24) + 6, who, `${vague ? 'roughly here' : 'within ' + fmtLen(e.r)} · ${sp}`, col, true]);
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
  // Labels make room for each other: one that would overlap a label already drawn
  // this frame moves down, up or aside (nearest free spot first)
  let labelBoxes = [], personLabels = [];
  function labelSpot(x, y, w, h) {
    const inside = (lx, ly) => lx - w / 2 >= 2 && lx + w / 2 <= W - 2 && ly >= 2 && ly + h <= H - 2;
    const free = (lx, ly) => inside(lx, ly) && !labelBoxes.some((b) => lx - w / 2 < b.x1 && lx + w / 2 > b.x0 && ly < b.y1 && ly + h > b.y0);
    for (let ring = 0; ring <= 3; ring++)          // the spot asked for, then rings of spots further out
      for (const [dx, dy] of ring ? [[0, ring * (h + 2)], [0, -ring * (h + 2)], [ring * (w * 0.6 + 6), 0], [-ring * (w * 0.6 + 6), 0],
        [ring * (w * 0.6 + 6), ring * (h + 2)], [-ring * (w * 0.6 + 6), -ring * (h + 2)]] : [[0, 0]])
        if (free(x + dx, y + dy)) return [x + dx, y + dy];
    return [Math.min(W - w / 2 - 2, Math.max(w / 2 + 2, x)), Math.min(H - h - 2, Math.max(2, y))];   // crowded: at least on the map
  }
  // pill: a dark backing so the label reads over trails, clouds and pictures (people's labels)
  function label(x, y, text, sub, color, pill = false) {
    ctx.font = '600 12px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    let w = ctx.measureText(text).width;
    if (sub) { ctx.font = '11px ui-monospace, monospace'; w = Math.max(w, ctx.measureText(sub).width); ctx.font = '600 12px ui-sans-serif, system-ui, sans-serif'; }
    const h = sub ? 28 : 15;
    [x, y] = labelSpot(x, y, w + 6, h);
    labelBoxes.push({ x0: x - w / 2 - 3, x1: x + w / 2 + 3, y0: y, y1: y + h });
    if (pill) {
      ctx.save(); ctx.fillStyle = alpha(css('--bg') || '#000000', 0.78); ctx.strokeStyle = alpha(color, 0.55); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x - w / 2 - 6, y - 3, w + 12, h + 5, 6) : ctx.rect(x - w / 2 - 6, y - 3, w + 12, h + 5);
      ctx.fill(); ctx.stroke(); ctx.restore();
      ctx.font = '600 12px ui-sans-serif, system-ui, sans-serif';
    }
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
    // about 30 frames a second is plenty for glows, trails and gliding (half the work of 60)
    if ((dirty || live) && !fx.skip && t - (fx.drawnAt || 0) >= 32) { fx.drawnAt = t; draw(t); dirty = false; }
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
  let listParts = null, forceList = false;
  function renderList() {
    const root = $('#map-links');
    if (!listParts || listParts.top.parentNode !== root) {
      listParts = { top: el('div'), floors: el('div'), zones: el('div'), list: el('div'), touched: 0 };
      root.replaceChildren(listParts.top, listParts.floors, listParts.zones, listParts.list);
      // a picker or button in the list being used: the list waits (a rebuild closes a phone's picker)
      for (const ev of ['pointerdown', 'touchstart', 'focusin', 'input', 'change'])
        root.addEventListener(ev, (e) => { if (listParts.list.contains(e.target) || listParts.top.contains(e.target)) listParts.touched = performance.now(); }, { passive: true });
    }
    const busy = (part) => part.contains(document.activeElement) || performance.now() - listParts.touched < 4000;
    if (!forceList && (busy(listParts.list) || busy(listParts.top))) return;   // the floors and zones panels guard themselves
    forceList = false;
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
    const zp = zonesPanel();
    if (listParts.zones.firstChild !== zp) listParts.zones.replaceChildren(zp);
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
      if (n !== HUB && r.ap && !n.board) rows.push(routerWhere(n));
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
    const sig = JSON.stringify([[...M.trackOff], M.hubFacing, others.map((b) => (M.nodes.get(b.key) || {}).facing), envFit && [envFit.n.toFixed(1), Math.round(envFit.lf), envFit.checks.map((c) => c.key + c.by)],
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
    const useTick = (key) => {                     // this board's links place people, or not
      const cb = el('input', { type: 'checkbox', checked: !M.trackOff.has(key) });
      cb.onchange = () => { if (cb.checked) M.trackOff.delete(key); else M.trackOff.add(key); dirty = true; floorsEl = null; renderList(); scheduleSave(); };
      return el('label', { className: 'check small' }, cb, 'used for the map');
    };
    det.append(row('Hub', M.hubFloor, null, (f) => { M.hubFloor = f || 1; }), aim(HUB, () => { M.hubFacing = null; }), useTick('hub'));
    for (const b of others) {
      const n = M.nodes.get(b.key);
      if (n) det.append(row(b.label, n.floor || M.hubFloor, null, (f) => { n.floor = f; }), aim(n, () => { n.facing = undefined; }), useTick(b.key));
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

  // Zones: areas on the map. The Pi tracks people all the time and says when
  // someone walks into one (the live stream), and can send a notification.
  let zonesOpen = false, zonesEl = null, zonesSig = '';
  function zonesPanel() {
    const ends = [];                                 // what a "near" zone can follow: routers and boards
    const seenE = new Set();
    for (const { a, n, r } of links())
      for (const [m, nm] of [[a, a === HUB ? 'the hub' : a.name], [n, r.link ? r.link.name || n.name : n === HUB ? 'the hub' : n.name]]) {
        const k = m === HUB ? 'hub' : m.key;
        if (!k || seenE.has(k) || !(k === 'hub' || k.startsWith('sat:') || k.startsWith('r:'))) continue;
        seenE.add(k); ends.push([k, nm]);
      }
    const sig = JSON.stringify([(M.zones || []).map((z) => [z.id, z.name, z.notify, z.night, z.light, z.near, z.watch]), ends]);
    if (zonesEl && sig === zonesSig) return zonesEl;
    zonesSig = sig;
    const det = el('details', { className: 'map-floors', open: zonesOpen });
    det.ontoggle = () => { zonesOpen = det.open; };
    det.append(el('summary', { textContent: `Zones${M.zones.length ? ` (${M.zones.length})` : ''}` }));
    for (const z of M.zones) {
      const name = el('button', { type: 'button', className: 'ghost small', textContent: z.name });
      name.onclick = () => { const v = prompt('Name this zone (e.g. Front door, Stairs, Kitchen)', z.name); if (v) { z.name = v.slice(0, 32); dirty = true; zonesEl = null; renderList(); scheduleSave(); } };
      const box = (label, key) => {
        const cb = el('input', { type: 'checkbox', checked: !!z[key] });
        cb.onchange = () => { z[key] = cb.checked; dirty = true; zonesEl = null; renderList(); scheduleSave(); };
        return el('label', { className: 'check small' }, cb, label);
      };
      const del = el('button', { type: 'button', className: 'ghost small', textContent: 'Remove' });
      del.onclick = () => { M.zones = M.zones.filter((q) => q !== z); dirty = true; zonesEl = null; renderList(); scheduleSave(); };
      let watchBox = null;
      if (!z.near) {                                 // one watched area at most: ticking one unticks the others
        const cb = el('input', { type: 'checkbox', checked: !!z.watch });
        cb.onchange = () => { for (const q of M.zones) q.watch = false; z.watch = cb.checked; dirty = true; zonesEl = null; track.people = []; renderList(); scheduleSave(); };
        watchBox = el('label', { className: 'check small', title: 'Place people only in this area (e.g. the house with the routers); movement elsewhere is ignored' }, cb, 'watch only here');
      }
      det.append(el('div', { className: 'map-zone-row' }, name, z.near ? el('span', { className: 'muted small', textContent: 'near it' }) : null,
        watchBox, box('turns on the light', 'light'), box('notify me', 'notify'), box('only at night', 'night'), del));
    }
    const add = el('button', { type: 'button', textContent: 'Draw a zone on the map' });
    add.onclick = () => setZoneDraw(true);
    const nearSel = el('select', { className: 'small' });
    nearSel.append(el('option', { value: '', textContent: 'Add a zone near…' }));
    for (const [k, nm] of ends) nearSel.append(el('option', { value: k, textContent: nm }));
    nearSel.onchange = () => {
      const k = nearSel.value;
      if (!k) return;
      const nm = (ends.find((e) => e[0] === k) || [k, k])[1];
      M.zones.push({ id: Math.random().toString(36).slice(2, 10), name: `Near ${nm}`, near: k, notify: false, night: false, light: false });
      zonesOpen = true; dirty = true; zonesEl = null; renderList(); scheduleSave();
    };
    det.append(add, nearSel, el('p', { className: 'muted small', textContent:
      'A zone near a router or board lights up when the links that meet there react together: motion at that end, from any board, even if you never placed the router. ' +
      'If any zone "turns on the light", the light follows only those zones (alerts keep the whole house).' }), el('p', { className: 'muted small', textContent:
      'Drag a zone to move it, its corner dot to resize it. When someone walks in, the live stream says so; with "notify me", your phone too (Settings, Phone notifications: same wait and daily cap). Tracking is about 2 m accurate, so make zones room-sized.' }));
    zonesEl = det;
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
    if (zoneDraw) {                                  // drawing a zone: this drag is its rectangle
      const [wx, wy] = toWorld(x, y);
      drag = { zdraw: true, wx0: wx, wy0: wy, wx1: wx, wy1: wy };
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
    const zh = !n && !tool ? zoneHit(x, y) : null;  // a zone: its corner resizes it, inside moves it
    if (zh) { const [wx, wy] = toWorld(x, y); drag = { zone: zh.z, corner: zh.corner, wx, wy, zx: zh.z.x, zy: zh.z.y, zw: zh.z.w, zhh: zh.z.h }; return; }
    drag = n ? { key: n.key } : { pan: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y, tap: true };
    autoFit = false; viewByUser = true;
    if (readOnly) homeTouched = true;
  }
  function zoneHit(x, y) {
    for (const z of [...(M.zones || [])].reverse()) {
      if (z.near) continue;                          // a "near" zone follows its router or board
      const cx = X(z.x + z.w), cy = Y(z.y + z.h);
      if (Math.hypot(cx - x, cy - y) <= 14) return { z, corner: true };
      if (x >= X(z.x) && x <= X(z.x + z.w) && y >= Y(z.y) && y <= Y(z.y + z.h)) return { z, corner: false };
    }
    return null;
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
    } else if (drag.zdraw) {
      [drag.wx1, drag.wy1] = toWorld(x, y);
      dirty = true;
    } else if (drag.zone) {
      const [wx, wy] = toWorld(x, y), z = drag.zone;
      if (drag.corner) { z.w = Math.max(0.5, drag.zw + wx - drag.wx); z.h = Math.max(0.5, drag.zhh + wy - drag.wy); }
      else { z.x = drag.zx + wx - drag.wx; z.y = drag.zy + wy - drag.wy; }
      dirty = true;
    } else if (drag.facing) {
      const m = drag.facing, deg = Math.round(Math.atan2(y - Y(m.y), x - X(m.x)) * 180 / Math.PI / 5) * 5;
      const f = deg * Math.PI / 180;                 // 5-degree steps
      if (m === HUB) M.hubFacing = f; else m.facing = f;
      heatKey = ''; locAt = 0; dirty = true;
    } else if (drag.key) {
      const n = M.nodes.get(drag.key), [wx, wy] = toWorld(x, y);
      n.x = wx; n.y = wy;
      if (n.hint) { n.hint.x = wx; n.hint.y = wy; n.auto = null; locAt = 0; }   // the rough spot moves with it
      else if (!n.placed && !n.board && (n.key || '').startsWith('r:')) {   // a router: "within 5 ft" of your spot
        n.hint = { x: wx, y: wy, r: DRAG_HINT }; n.auto = null; n.fit = null; locAt = 0;
      } else n.placed = true;
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
    else if (drag && drag.pan && drag.tap && walk && e.type === 'pointerup') walkMark(...toWorld(...pos(e)));
    if (drag && drag.zdraw) finishZone(drag);
    if (!readOnly && drag && (drag.key || drag.pan || drag.layer || drag.board || drag.facing || drag.zone)) { scheduleSave(); if (drag.facing) renderList(); }
    drag = ptrs.size === 1 ? { pan: true, x0: [...ptrs.values()][0].x, y0: [...ptrs.values()][0].y, vx: M.view.x, vy: M.view.y } : null;
  }

  // ---------- saving ----------
  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1000);
  }
  // Several pages can be open (a phone, a PC): each sends only what changed on
  // it since it last heard from the Pi (edits), the Pi merges, and every page
  // takes up the others' changes every few seconds (pollMap). Baseline: each
  // item as the Pi last had it, to tell what changed here.
  const nodeSer = (n) => Object.assign({ key: n.key, name: String(n.name).slice(0, 64), x: n.x, y: n.y, placed: !!n.placed },
    n.floor ? { floor: n.floor } : {}, typeof n.facing === 'number' ? { facing: n.facing } : {},
    n.hint ? { hint: [n.hint.x, n.hint.y, n.hint.r] } : {},
    !n.placed && n.auto ? { auto: [Math.min(1000, n.auto.a), Math.min(1000, n.auto.b), n.auto.rot] } : {});
  const sections = () => ({
    zones: (M.zones || []).map((z) => Object.assign({ id: z.id, name: String(z.name).slice(0, 32),
      notify: !!z.notify, night: !!z.night, light: !!z.light, watch: !!z.watch && !z.near }, z.near ? { near: z.near } : { x: z.x, y: z.y, w: z.w, h: z.h })),
    track_off: [...M.trackOff].sort(),
    plans: M.plans.map((p) => ({ id: p.id, name: String(p.name).slice(0, 64), x: p.x, y: p.y, w: p.w, rot: p.rot, opacity: p.opacity, hidden: !!p.hidden })),
    unit: M.unit, env: M.env, hub: [M.hubFloor, M.floorH, typeof M.hubFacing === 'number' ? M.hubFacing : null],
    view: M.view && viewByUser ? M.view : null,
  });
  const base = { nodes: new Map(), secs: {} };
  // what a saved layout says, in the same shapes as above (to compare)
  function baselineFrom(m) {
    base.nodes = new Map((m.nodes || []).map((n) => [n.key, JSON.stringify(nodeSer(Object.assign({ placed: false }, n,
      { hint: Array.isArray(n.hint) ? { x: n.hint[0], y: n.hint[1], r: n.hint[2] } : null,
        auto: Array.isArray(n.auto) ? { a: n.auto[0], b: n.auto[1], rot: n.auto[2] } : null })))]));
    base.secs = {
      zones: JSON.stringify((m.zones || []).map((z) => Object.assign({ id: z.id, name: String(z.name).slice(0, 32),
        notify: !!z.notify, night: !!z.night, light: !!z.light, watch: !!z.watch && !z.near }, z.near ? { near: z.near } : { x: z.x, y: z.y, w: z.w, h: z.h }))),
      track_off: JSON.stringify([...(m.track_off || [])].sort()),
      plans: JSON.stringify((m.plans || []).map((p) => ({ id: p.id, name: String(p.name || p.id).slice(0, 64), x: p.x, y: p.y, w: p.w, rot: p.rot || 0, opacity: p.opacity === undefined ? 1 : p.opacity, hidden: !!p.hidden }))),
      unit: JSON.stringify(m.unit || M.unit), env: JSON.stringify(m.env || M.env),
      hub: JSON.stringify([m.hub_floor || 1, m.floor_h || 3.0, typeof m.hub_facing === 'number' ? m.hub_facing : null]),
      view: JSON.stringify(m.view || null),
    };
  }
  function localEdits() {
    const edits = [];
    for (const n of M.nodes.values()) if (JSON.stringify(nodeSer(n)) !== base.nodes.get(n.key)) edits.push('node:' + n.key);
    const sc = sections();
    for (const [k, v] of Object.entries(sc)) if (JSON.stringify(v) !== base.secs[k]) edits.push(k);
    return edits;
  }
  async function save() {
    const sc = sections();
    const body = { unit: M.unit, env: M.env, nodes: [...M.nodes.values()].slice(0, 64).map(nodeSer),
      hub_floor: M.hubFloor, floor_h: M.floorH, track_off: sc.track_off, zones: sc.zones, plans: sc.plans, edits: localEdits() };
    if (typeof M.hubFacing === 'number') body.hub_facing = M.hubFacing;
    if (sc.view) body.view = sc.view;
    try {
      const r = await fetch('api/map', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ackwave': '1' }, body: JSON.stringify(body) });
      const j = await r.json().catch(() => null);
      if (j && j.map) takeRemote(j.map);
    } catch (e) { /* the next change tries again */ }
  }
  // Another page's changes: taken where this page hasn't changed the same item
  function takeRemote(m) {
    if (drag) return;                                  // never under your finger
    const edits = new Set(localEdits());
    for (const n of m.nodes || []) {
      if (edits.has('node:' + n.key)) continue;
      const loc = M.nodes.get(n.key);
      const hint = Array.isArray(n.hint) && n.hint.length === 3 ? { x: n.hint[0], y: n.hint[1], r: n.hint[2] } : null;
      if (!loc) { M.nodes.set(n.key, { key: n.key, name: n.name || n.key, x: n.x, y: n.y, placed: !!n.placed, floor: n.floor || 0,
        facing: typeof n.facing === 'number' ? n.facing : undefined, hint }); continue; }
      loc.fit = Array.isArray(n.fit) ? { x: n.fit[0], y: n.fit[1] } : null;   // the Pi's fitted spot
      if (n.placed || hint || loc.placed || loc.hint) {   // a placement: as the Pi has it
        Object.assign(loc, { x: n.x, y: n.y, placed: !!n.placed, floor: n.floor || 0, hint,
          facing: typeof n.facing === 'number' ? n.facing : undefined });
      }
    }
    if (!edits.has('zones')) M.zones = Array.isArray(m.zones) ? m.zones.map((z) => ({ ...z })) : [];
    if (!edits.has('track_off')) M.trackOff = new Set(Array.isArray(m.track_off) ? m.track_off : []);
    if (!edits.has('hub')) {
      if (m.hub_floor) M.hubFloor = m.hub_floor;
      if (m.floor_h) M.floorH = m.floor_h;
      M.hubFacing = typeof m.hub_facing === 'number' ? m.hub_facing : null;
    }
    baselineFrom(m);
    floorsEl = null; zonesEl = null; locAt = 0; heatKey = ''; dirty = true;
  }
  async function pollMap() {
    if (!loaded || !visible || drag) return;
    try {
      const r = await fetch('api/map', { headers: { 'X-Ackwave': '1' } });
      if (r.ok) { takeRemote(await r.json()); renderList(); }
    } catch (e) { /* next time */ }
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
      hint: Array.isArray(n.hint) && n.hint.length === 3 ? { x: n.hint[0], y: n.hint[1], r: n.hint[2] } : null,
      fit: Array.isArray(n.fit) ? { x: n.fit[0], y: n.fit[1] } : null });
    M.zones = Array.isArray(m.zones) ? m.zones.map((z) => ({ ...z })) : [];
    M.trackOff = new Set(Array.isArray(m.track_off) ? m.track_off : []);
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
  // ---------- accuracy: how precisely each spot can be told apart ----------
  // For a person at p every link changes by its sensitivity there: a vector,
  // one number per link. Spots whose vectors look alike (after scaling: how
  // much someone moves is unknown) can't be told apart, so the error at p is
  // the spread of the spots that look like it. No link reacting: blind.
  const ACC_CELL = 0.5, ACC_SIG = 0.12;
  function accPairs(move) {                         // the links, with one board moved (or added) in thought
    const pos = (m) => (move && m && m.key && m.key === move.key ? { ...m, x: move.x, y: move.y } : m);
    const pairs = links().filter(({ r }) => !weak(r)).map(({ a, n }) => [pos(a), pos(n)]);
    if (move && move.add) {                         // a new board: links to the hub, the other boards and every router
      const nb = { key: '+', x: move.x, y: move.y, board: true, floor: M.hubFloor };
      const ends = new Set([HUB, ...others.map((b) => M.nodes.get(b.key)).filter(Boolean)]);
      for (const { n, r } of links()) if (r.ap && n !== HUB && !n.board) ends.add(n);
      for (const e of ends) pairs.push([nb, e]);
    }
    return pairs.filter(([a, n]) => a && n && dist3(a, n) >= 0.3);
  }
  function accArea() {
    const pts = [[0, 0], ...others.map((b) => M.nodes.get(b.key)).filter(Boolean).map((n) => [n.x, n.y])];
    let x0 = Math.min(...pts.map((p) => p[0])) - 3, x1 = Math.max(...pts.map((p) => p[0])) + 3;
    let y0 = Math.min(...pts.map((p) => p[1])) - 3, y1 = Math.max(...pts.map((p) => p[1])) + 3;
    return { x0, y0, x1, y1 };
  }
  function accMap(pairs, area, cell) {
    const lam = lambda(), K = pairs.length, cs = cell || ACC_CELL;
    const gw = Math.max(1, Math.ceil((area.x1 - area.x0) / cs)), gh = Math.max(1, Math.ceil((area.y1 - area.y0) / cs));
    const N = gw * gh, U = new Float32Array(N * K), mag = new Float32Array(N), err = new Float32Array(N);
    const ds = pairs.map(([a, n]) => dist3(a, n));
    for (let c = 0; c < N; c++) {
      const x = area.x0 + ((c % gw) + 0.5) * cs, y = area.y0 + (Math.floor(c / gw) + 0.5) * cs;
      let m2 = 0;
      for (let k = 0; k < K; k++) { const v = sensitivity(x, y, pairs[k][0], pairs[k][1], ds[k], lam); U[c * K + k] = v; m2 += v * v; }
      const m = Math.sqrt(m2); mag[c] = m;
      if (m > 0) for (let k = 0; k < K; k++) U[c * K + k] /= m;
    }
    for (let c = 0; c < N; c++) {
      if (mag[c] < 0.15) { err[c] = -1; continue; }   // nothing reacts here
      const cx = c % gw, cy = Math.floor(c / gw);
      let sw = 0, sd = 0;
      for (let q = 0; q < N; q++) {
        if (mag[q] < 0.15) continue;
        let d2 = 0;
        for (let k = 0; k < K; k++) { const e = U[c * K + k] - U[q * K + k]; d2 += e * e; }
        if (d2 > 9 * ACC_SIG * ACC_SIG) continue;
        const w = Math.exp(-d2 / (2 * ACC_SIG * ACC_SIG)), dx = (q % gw - cx) * cs, dy = (Math.floor(q / gw) - cy) * cs;
        sw += w; sd += w * (dx * dx + dy * dy);
      }
      err[c] = Math.sqrt(sd / Math.max(sw, 1e-9));
    }
    // smoothed over the neighbouring cells: an estimate, not a cell-exact truth
    const sm = new Float32Array(N);
    for (let c = 0; c < N; c++) {
      if (err[c] < 0) { sm[c] = -1; continue; }
      const cx = c % gw, cy = Math.floor(c / gw);
      let t = 0, n = 0;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const x = cx + dx, y = cy + dy;
        if (x < 0 || y < 0 || x >= gw || y >= gh) continue;
        const e = err[y * gw + x];
        if (e >= 0) { t += e; n++; }
      }
      sm[c] = t / n;
    }
    return { ...area, gw, gh, cs, err: sm };
  }
  const accMean = (am) => {
    let s2 = 0, n = 0, blind = 0;
    for (const e of am.err) { if (e < 0) { blind++; s2 += 4; } else s2 += e; n++; }   // a blind spot counts as 4 m off
    return { mean: s2 / Math.max(n, 1), blind: blind / Math.max(n, 1) };
  };
  let acc = null;                                   // the overlay shown (null: off)
  function accColor(e) {
    if (e < 0) return 'rgba(120,130,140,.35)';      // blind
    return e < 1 ? 'rgba(62,207,142,.30)' : e < 2 ? 'rgba(255,214,79,.28)' : e < 3 ? 'rgba(255,150,60,.30)' : 'rgba(255,90,79,.32)';
  }
  function drawAcc() {
    if (!acc) return;
    const { x0, y0, gw, gh, cs, err } = acc.map;
    for (let c = 0; c < gw * gh; c++) {
      const x = x0 + (c % gw) * cs, y = y0 + Math.floor(c / gw) * cs;
      ctx.fillStyle = accColor(err[c]);
      ctx.fillRect(X(x), Y(y), cs * M.view.s + 0.5, cs * M.view.s + 0.5);
    }
    if (acc.best) {                                  // the suggested spot
      const px = X(acc.best.x), py = Y(acc.best.y);
      ctx.save();
      ctx.beginPath(); ctx.arc(px, py, 12, 0, Math.PI * 2);
      ctx.strokeStyle = css('--accent'); ctx.lineWidth = 3; ctx.setLineDash([4, 3]); ctx.stroke();
      ctx.setLineDash([]); ctx.font = '600 12px system-ui, sans-serif'; ctx.textAlign = 'center';
      ctx.fillStyle = css('--accent'); ctx.fillText(acc.best.label, px, py - 18);
      if (acc.best.from) {
        ctx.beginPath(); ctx.moveTo(X(acc.best.from.x), Y(acc.best.from.y)); ctx.lineTo(px, py);
        ctx.strokeStyle = alpha(css('--accent'), 0.6); ctx.lineWidth = 1.5; ctx.setLineDash([6, 5]); ctx.stroke();
      }
      ctx.restore();
    }
  }
  function accNote(text) { const e = $('#acc-note'); e.hidden = !text; e.textContent = text || ''; }
  function toggleAcc() {
    if (acc) { acc = null; accNote(''); dirty = true; return; }
    const map = accMap(accPairs(null), accArea());
    const m = accMean(map);
    acc = { map };
    accNote(`Green: within ${fmtLen(1)} · yellow: ${fmtLen(2)} · orange: ${fmtLen(3)} · red: more · grey: no link reacts there. ` +
      `Typical error over the area around your boards: about ${fmtLen(m.mean)}` + (m.blind > 0.05 ? `, and ${Math.round(m.blind * 100)}% of it is blind.` : '.'));
    dirty = true;
  }
  // Where would one board help most? Each board in turn tried on a coarse
  // grid (and a new board), scored by the mean error over the area.
  async function suggest() {
    const area = accArea(), base = accMean(accMap(accPairs(null), area, 1.5));
    accNote('Trying spots… (a few seconds)');
    const cands = [];
    for (let y = area.y0 + 1; y < area.y1; y += 2) for (let x = area.x0 + 1; x < area.x1; x += 2) cands.push([x, y]);
    let best = null;
    const tries = [...others.map((b) => ({ b, n: M.nodes.get(b.key) })).filter((t) => t.n), { add: true }];
    for (const t of tries) {
      for (const [x, y] of cands) {
        const mv = t.add ? { add: true, x, y } : { key: t.n.key, x, y };
        const sc = accMean(accMap(accPairs(mv), area, 1.5)).mean;
        if (!best || sc < best.sc) best = { sc, x, y, t };
      }
      await new Promise((ok) => setTimeout(ok, 0));   // keep the page responsive
    }
    if (!best || best.sc > base.mean - 0.15) {
      acc = { map: accMap(accPairs(null), area) };
      accNote(`Your boards are already well placed: no single move gains much (typical error about ${fmtLen(base.mean)}).`);
      dirty = true; return;
    }
    const who = best.t.add ? 'a new board' : (/^\d+$/.test(best.t.b.label) ? `board ${best.t.b.label}` : best.t.b.label);
    const mv = best.t.add ? { add: true, x: best.x, y: best.y } : { key: best.t.n.key, x: best.x, y: best.y };
    acc = { map: accMap(accPairs(mv), area), best: { x: best.x, y: best.y, label: best.t.add ? 'add a board here' : `move ${who} here`,
      from: best.t.add ? null : best.t.n } };
    accNote(`Best single change: ${best.t.add ? 'add a board' : `move ${who}`} to the dashed circle. Typical error ${fmtLen(base.mean)} → ${fmtLen(best.sc)}. ` +
      'The colours show the map after that change. Tap Accuracy to hide it.');
    dirty = true;
  }

  // ---------- walk check: you tap where you really are ----------
  // Walk (or stand) around and tap your real spot now and then: each tap is
  // compared with where the tracking puts you (here, and on the Pi), and saved
  // on the Pi as a labelled example (sessions/walks.jsonl) for learning later.
  let walk = null;                                  // { marks: [{x, y, ex, ey, err}] } while checking
  function walkToggle() {
    walk = walk ? null : { marks: [] };
    $('#map-walk').classList.toggle('on', !!walk);
    $('#map-walk').textContent = walk ? 'Done checking' : 'Walk check';
    accNote(walk ? 'Walk around; every few steps, tap where you really are on the map. Each tap is compared with the tracking.' : '');
    dirty = true;
  }
  async function walkMark(x, y) {
    const near = track.people.filter((p) => p.est).sort((a, b) => Math.hypot(a.est.x - x, a.est.y - y) - Math.hypot(b.est.x - x, b.est.y - y))[0];
    const m = { x, y, ex: near ? near.est.x : null, ey: near ? near.est.y : null };
    m.err = near ? Math.hypot(m.ex - x, m.ey - y) : null;
    walk.marks.push(m);
    let pi = '';
    try {
      const r = await window.ackCmdText({ cmd: 'walk_mark', x, y });
      const g = r && r.ok && /\[pi ([\d.]+)\]/.exec(r.msg || '');
      if (g) m.pi = +g[1];
    } catch (e) { /* the page's own check still counts */ }
    const errs = walk.marks.map((q) => q.err).filter((v) => v !== null).sort((a, b) => a - b);
    const pis = walk.marks.map((q) => q.pi).filter((v) => v !== undefined && v !== null).sort((a, b) => a - b);
    const med = (v) => v[Math.floor(v.length / 2)];
    if (pis.length) pi = ` · on the Pi: ${fmtLen(med(pis))}`;
    accNote(`Walk check: ${walk.marks.length} tap${walk.marks.length > 1 ? 's' : ''}` +
      (errs.length ? `, typical error ${fmtLen(med(errs))}${pi}` : ', nobody tracked at those moments (move a little first)') +
      `${walk.marks.length - errs.length ? ` · ${walk.marks.length - errs.length} with nobody tracked` : ''}. Keep tapping, or Done checking.`);
    dirty = true;
  }
  // Fit routers to the walk check's taps: each router in turn, tried over a
  // grid inside its rough spot (or 12 m around it); the spot where the taps'
  // replayed estimates (the tracker's model, no filter) land closest to where
  // you really were wins. Links that don't touch the router being moved are
  // computed once per tap; each tap is searched within 8 m of where you were
  // (farther counts as 6 m off anyway).
  // selfFit: no taps. Moments of movement (the Pi keeps them); a router in the
  // wrong spot makes them fit badly (no one place explains every link), so the
  // spot with the smallest total misfit wins. Only automatic and rough routers move.
  async function fitWalk(selfFit) {
    let recs = [];
    try {
      const r = await fetch(selfFit ? 'api/snaps' : 'api/walks', { headers: { 'X-Ackwave': '1' } });
      const j = await r.json();
      recs = (selfFit ? j.snaps : j.walks) || [];
    } catch (e) { /* none */ }
    recs = recs.filter((q) => q.links && Object.keys(q.links).length >= 2).slice(-60);
    if (recs.length < (selfFit ? 15 : 4)) {
      accNote(selfFit ? `Not enough movement seen yet to fit on (${recs.length} moments; 15 needed): walk around a bit and try again.`
        : `Fit needs at least 4 walk-check taps from today (have ${recs.length}): use Walk check first.`);
      return;
    }
    const wz = watchZone();                           // watching a house: only it, and the links reaching into it
    const L = links().filter(({ a, n }) => !wz || inZone(wz, a) || inZone(wz, n)), lam = lambda(), CS = 0.75, RAD = 8, G0 = 1.2, WG = 1 / 1.5 ** 2;
    let area = null;
    if (selfFit && wz) {                             // the watched house: people are only ever placed in there
      area = { x0: wz.x, x1: wz.x + wz.w, y0: wz.y, y1: wz.y + wz.h };
    } else if (selfFit) {                            // the house: the boards and 3 m around
      const pts = [[0, 0], ...others.map((b) => M.nodes.get(b.key)).filter(Boolean).map((n) => [n.x, n.y])];
      area = { x0: Math.min(...pts.map((p) => p[0])) - 3, x1: Math.max(...pts.map((p) => p[0])) + 3,
        y0: Math.min(...pts.map((p) => p[1])) - 3, y1: Math.max(...pts.map((p) => p[1])) + 3 };
    }
    const grids = recs.map((q) => {
      const px = [], py = [];
      if (area) for (let y = area.y0; y <= area.y1; y += 1) for (let x = area.x0; x <= area.x1; x += 1) { px.push(x); py.push(y); }
      else for (let dy = -RAD; dy <= RAD; dy += CS) for (let dx = -RAD; dx <= RAD; dx += CS) { px.push(q.x + dx); py.push(q.y + dy); }
      return { px, py };
    });
    const cache = new Map();
    const sens = (qi, link, moving) => {
      const k = qi + '|' + link.r.key;
      if (!moving && cache.has(k)) return cache.get(k);
      const { px, py } = grids[qi], ends = endSamples(link.a, link.n), out = new Float32Array(px.length);
      for (let c = 0; c < px.length; c++) {
        let v = 0;
        for (const [ea, en] of ends) v += sensitivity(px[c], py[c], ea, en, dist3(ea, en), lam);
        out[c] = v / ends.length;
      }
      if (!moving) cache.set(k, out);
      return out;
    };
    const err = (mover) => {
      let total = 0;
      recs.forEach((q, qi) => {
        const use = L.filter(({ r }) => q.links[r.key] !== undefined);
        const act = use.map(({ r }) => q.links[r.key]);
        if (use.length < 2 || Math.max(...act) < 0.3) { total += 6; return; }
        const S = use.map((l) => sens(qi, l, l.a === mover || l.n === mover));
        const w = act.map((a) => 1 / (SIG0 + SIG1 * a) ** 2), { px, py } = grids[qi];
        let best = -1, bR = Infinity;
        for (let c = 0; c < px.length; c++) {
          let num = G0 * WG, den = WG;
          for (let k = 0; k < S.length; k++) { num += w[k] * act[k] * S[k][c]; den += w[k] * S[k][c] * S[k][c]; }
          const g = Math.max(0, num / den);
          let R = (g - G0) ** 2 * WG;
          for (let k = 0; k < S.length; k++) R += w[k] * (act[k] - g * S[k][c]) ** 2;
          if (R < bR) { bR = R; best = c; }
        }
        total += selfFit ? bR / S.length : Math.min(6, Math.hypot(px[best] - q.x, py[best] - q.y));
      });
      return total / recs.length;
    };
    const routers = [...new Set(L.map(({ n }) => n).filter((n) => n !== HUB && (n.key || '').startsWith('r:') && (!selfFit || !n.placed)))];
    if (selfFit && !routers.length) { accNote('Every router is set to "exactly here": set one to automatic or roughly (its Where menu) to let it be fitted.'); return; }
    if (!routers.length) { accNote('No routers on the map to fit.'); return; }
    const before = err(null), saved = routers.map((n) => [n, n.x, n.y, n.placed]);
    for (let pass = 0; pass < 2; pass++)
      for (const [ri, n] of routers.entries()) {
        accNote(`Fitting router ${ri + 1} of ${routers.length} to ${recs.length} taps (pass ${pass + 1} of 2)…`);
        await new Promise((ok) => setTimeout(ok, 0));
        const c0 = n.hint || { x: n.x, y: n.y, r: selfFit ? 8 : 12 }, c = { ...c0, r: selfFit ? Math.min(c0.r, 8) : c0.r };
        const step = Math.max(0.75, c.r / 8);
        let best = [n.x, n.y, Infinity];
        n.placed = true;
        for (let dy = -c.r; dy <= c.r; dy += step)
          for (let dx = -c.r; dx <= c.r; dx += step) {
            if (Math.hypot(dx, dy) > c.r) continue;
            if (wz && !inZone(wz, { x: c.x + dx, y: c.y + dy }) && inZone(wz, c)) continue;   // a router in the watched house stays in it
            n.x = c.x + dx; n.y = c.y + dy;
            const e = err(n);
            if (e < best[2]) best = [n.x, n.y, e];
          }
        n.x = best[0]; n.y = best[1];
        for (const k of [...cache.keys()]) cache.delete(k);   // its links changed for the next router
      }
    const after = err(null);
    const worth = selfFit ? after < 0.9 * before : after <= before - 0.2;
    if (!worth) {                                     // not worth it: back as they were
      for (const [n, x, y, pl] of saved) { n.x = x; n.y = y; n.placed = pl; }
      accNote(selfFit ? `Your routers already fit everyday movement well (checked ${recs.length} moments): nothing moved.`
        : `Your routers already fit the walk well (typical error about ${fmtLen(before)}): nothing moved.`);
    } else if (selfFit) {                             // moved inside their circles; they stay rough
      for (const n of routers) { n.auto = null; n.placed = false; n.hint = { x: n.x, y: n.y, r: n.hint ? n.hint.r : DRAG_HINT }; }
      accNote(`Routers fitted to ${recs.length} moments of everyday movement: they fit ${Math.round((1 - after / before) * 100)}% better. ` +
        'Each keeps a rough circle where it now sits; drag one if it looks wrong.');
      scheduleSave();
    } else {
      for (const n of routers) { n.auto = null; n.hint = null; }
      accNote(`Routers fitted to your walk: typical error ${fmtLen(before)} → ${fmtLen(after)} on ${recs.length} taps. ` +
        'They are now placed exactly; drag one or set it to automatic if it looks wrong.');
      scheduleSave();
    }
    locAt = 0; heatKey = ''; dirty = true; renderList();
  }
  function drawWalkCheck() {
    if (!walk) return;
    ctx.save();
    for (const m of walk.marks) {
      const x = X(m.x), y = Y(m.y);
      if (m.ex !== null) {
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(X(m.ex), Y(m.ey));
        ctx.strokeStyle = alpha(css('--warn'), 0.8); ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.beginPath(); ctx.moveTo(x - 6, y - 6); ctx.lineTo(x + 6, y + 6); ctx.moveTo(x + 6, y - 6); ctx.lineTo(x - 6, y + 6);
      ctx.strokeStyle = css('--warn'); ctx.lineWidth = 2.5; ctx.stroke();
    }
    ctx.restore();
  }

  // ---------- a satellite picture, fetched once by this browser ----------
  // Esri World Imagery tiles (free to view, with credit) around a point, joined
  // into one 1024 px picture, sent to the Pi like any picture and placed
  // north-up with that point at the hub. Nothing but the tiles' numbers goes
  // out, from your own browser; the point itself is never stored.
  const SAT_URL = (z, x, y) => `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`;
  async function addSatellite(lat, lon, across) {
    const S = 1024, cl = Math.cos(lat * Math.PI / 180);
    const z = Math.max(15, Math.min(19, Math.round(Math.log2(156543.034 * cl * S / across))));
    const n = 2 ** z, mpp = 156543.034 * cl / n;
    const px = (lon + 180) / 360 * n * 256;
    const lr = lat * Math.PI / 180;
    const py = (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n * 256;
    const cv2 = document.createElement('canvas');
    cv2.width = S; cv2.height = S;
    const c2 = cv2.getContext('2d');
    const x0 = px - S / 2, y0 = py - S / 2, jobs = [];
    for (let ty = Math.floor(y0 / 256); ty <= Math.floor((y0 + S - 1) / 256); ty++)
      for (let tx = Math.floor(x0 / 256); tx <= Math.floor((x0 + S - 1) / 256); tx++)
        jobs.push(new Promise((ok) => {
          const im = new Image();
          im.crossOrigin = 'anonymous';
          im.onload = () => { c2.drawImage(im, tx * 256 - x0, ty * 256 - y0); ok(true); };
          im.onerror = () => ok(false);
          im.src = SAT_URL(z, ((tx % n) + n) % n, ty);
        }));
    const got = await Promise.all(jobs);
    if (!got.some(Boolean)) throw new Error('no tiles came back (offline, or blocked?)');
    c2.font = '12px sans-serif'; c2.fillStyle = 'rgba(255,255,255,.8)';
    c2.fillText('Imagery © Esri, Maxar, Earthstar Geographics', 8, S - 8);
    const blob = await new Promise((ok) => cv2.toBlob(ok, 'image/jpeg', 0.85));
    blob.name = 'Satellite.jpg';
    const w = S * mpp;
    await addPicture(blob, { x: -w / 2, y: -w / 2, w, bottom: true });
  }
  async function addPicture(file, place) {
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
    const w = place ? place.w : Math.max(1, 0.8 * W / M.view.s);
    const p = { id, name: (file.name || id).replace(/\.[^.]+$/, '').slice(0, 64) || id, x: 0, y: 0, w, rot: 0,
      opacity: M.plans.length && !place ? 0.8 : 1, hidden: false };
    if (place && place.bottom) M.plans.unshift(p); else M.plans.push(p);
    await loadPicture(p);
    if (place) { p.x = place.x; p.y = place.y; }
    else if (p.img) { p.x = M.view.x - w / 2; p.y = M.view.y - (w * p.ih / p.iw) / 2; }
    scheduleSave();
    if (place) { dirty = true; renderLayers(); return; }   // already to scale, north up: drag it to line up
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
      baselineFrom(saved);
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
    setInterval(pollMap, 8000);                       // other open pages' changes (a phone and a PC at once)
    cv = $('#map-cv'); ctx = cv.getContext('2d');
    for (const [k, v] of Object.entries(ENVS)) $('#map-env').append(el('option', { value: k, textContent: v.label }));
    $('#map-env').onchange = (e) => { M.env = e.target.value; dirty = true; renderList(); scheduleSave(); };
    for (const b of $('#map-unit').children) b.onclick = () => { M.unit = b.dataset.u; syncControls(); dirty = true; renderList(); scheduleSave(); };
    $('#map-fit').onclick = () => { fit(); viewByUser = true; scheduleSave(); };
    $('#map-walk').onclick = walkToggle;
    $('#map-zone').onclick = () => setZoneDraw(!zoneDraw);
    $('#map-walkfit').onclick = async () => { $('#map-walkfit').disabled = true; try { await fitWalk(false); } finally { $('#map-walkfit').disabled = false; } };
    $('#map-selffit').onclick = async () => { $('#map-selffit').disabled = true; try { await fitWalk(true); } finally { $('#map-selffit').disabled = false; } };
    $('#map-acc').onclick = () => { toggleAcc(); $('#map-acc').classList.toggle('on', !!acc); };
    $('#map-suggest').onclick = async () => { $('#map-suggest').disabled = true; try { await suggest(); } finally { $('#map-suggest').disabled = false; $('#map-acc').classList.toggle('on', !!acc); } };
    $('#map-file').onchange = (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) addPicture(f); };
    // the satellite picture form
    const satMsg = $('#sat-msg'), satHint = satMsg.textContent;
    $('#sat-open').onclick = () => { $('#sat-form').hidden = !$('#sat-form').hidden; satMsg.textContent = satHint; };
    $('#sat-cancel').onclick = () => { $('#sat-form').hidden = true; };
    $('#sat-here').onclick = () => {
      if (!navigator.geolocation) { satMsg.textContent = 'This browser has no location: type the numbers instead.'; return; }
      satMsg.textContent = 'Finding where this phone is…';
      navigator.geolocation.getCurrentPosition(
        (pos) => { $('#sat-ll').value = `${pos.coords.latitude.toFixed(6)}, ${pos.coords.longitude.toFixed(6)}`;
          satMsg.textContent = `Got it (within about ${Math.round(pos.coords.accuracy)} m). Now "Get the picture".`; },
        () => { satMsg.textContent = 'No location (blocked, or the page isn\'t on https): type the numbers instead.'; },
        { enableHighAccuracy: true, timeout: 15000 });
    };
    $('#sat-form').onsubmit = async (e) => {
      e.preventDefault();
      const m = $('#sat-ll').value.match(/(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)/);
      const lat = m && +m[1], lon = m && +m[2];
      if (!m || Math.abs(lat) > 85 || Math.abs(lon) > 180) { satMsg.textContent = 'Give it as "latitude, longitude", for example 40.7128, -74.0060.'; return; }
      $('#sat-go').disabled = true;
      satMsg.textContent = 'Downloading the picture…';
      try {
        await addSatellite(lat, lon, +$('#sat-area').value);
        $('#sat-ll').value = '';                     // not kept
        $('#sat-form').hidden = true;
      } catch (err) {
        satMsg.textContent = `Couldn't get it: ${err.message}.`;
      } finally { $('#sat-go').disabled = false; }
    };
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
      if (slow && slow.tracked && slow.tracked.model_err) modelErr = slow.tracked.model_err;
      if (slow && slow.tracked) { zoneIn = slow.tracked.zones || {}; breathing = slow.tracked.breathing || {}; todayMap = slow.tracked.today || null; learned = slow.tracked.learned || {}; }
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
    _nodeRef: (k) => M.nodes.get(k),             // tests: one node itself
    setDetails: (on) => { homeDetails = !!on; heatKey = ''; dirty = true; },
    setToday: (on) => { showToday = !!on; dirty = true; },
    viewFlags: () => ({ details: homeDetails, today: showToday }),
    _layers: () => M.plans.map(({ img, ...p }) => p),
    _screenOf(id, u, v) {                      // where picture pixel (u, v) is on the canvas
      const p = plan(id), k = p.w / p.iw, c = Math.cos(p.rot), s = Math.sin(p.rot);
      return [X(p.x + (u * c - v * s) * k), Y(p.y + (u * s + v * c) * k)];
    },
  };
})();
