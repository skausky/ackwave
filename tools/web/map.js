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
  const M = { unit: 'ft', env: 'home', view: null, nodes: new Map(), plans: [] };   // nodes: key -> {key, name, x, y, placed}
  // Picture layers, bottom first: {id, name, x, y (top-left corner, metres), w (width, metres), rot (radians),
  // opacity, hidden, img (once loaded), iw, ih (its pixels)}. A picture pixel (u, v) lands at
  // (x, y) + rotate(rot) * (u, v) * w / iw.
  const PLAN_IDS = ['p1', 'p2', 'p3', 'p4'];
  let tool = null;                          // {kind: 'move'|'scale'|'align', id, pts: [], step}
  let readOnly = false;                     // on Home: look, pan and zoom; nothing is moved or saved
  let stageMark = null;                     // where the map lives on the Map page (it visits Home)
  let pageView = null, homeFit = false;     // the Map page's view, kept while Home fits its own
  let radio = [], channel = 6, boardMotion = false;
  let others = [];                          // the other boards (slow.map_boards): each a point with its router
  let routersInfo = [];                     // the hub's routers (slow.routers): idx -> bssid, for naming
  let showOnly = null;                      // null = every board's links; 'hub' or 'sat:<mac>' = one board's
  const WEAK_DBM = -80;                     // links weaker than this are drawn faint
  // ---------- life: every effect shows something real ----------
  //  particles along a link   its pokes and answers (their rate: the answers a second)
  //  a bright band running    that link sees motion
  //  a node's glow            how active it is; dim when its board isn't heard
  //  rings once               something heard for the first time
  //  a wave to the light      the light was switched on, from the board(s) that saw it
  const reduced = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const fx = { seen: new Map(), quality: 2, dtAvg: 16, lastT: 0, skip: false, waves: [], lightWas: null, by: [] };
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
  const envN = () => ENVS[M.env].n;
  const eirpOf = (r) => (r.ap ? EIRP.ap : EIRP.device);
  // What the board should hear at d metres in this environment (no walls)
  const expected = (r, d) => eirpOf(r) + BOARD.gainDbi - pl1() - 10 * envN() * Math.log10(Math.max(d, 0.3));
  // A first guess at the distance from the signal (rough: walls and furniture make it look farther)
  const guessDist = (r) => Math.min(40, Math.max(1, 10 ** ((eirpOf(r) - pl1() - r.rssi) / (10 * envN()))));
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
  function nodeFor(r, k) {
    let n = M.nodes.get(r.key);
    if (!n) {                                // first time heard: place it from its signal
      const d = r.rssi !== null && r.rssi !== undefined ? guessDist(r) : 5;
      const ang = r.key === 'ap:0' ? 0 : (-0.7 + 0.6 * k);
      n = { key: r.key, name: r.label, x: d * Math.cos(ang), y: d * Math.sin(ang), placed: false };
      M.nodes.set(r.key, n);
      scheduleSave();
    }
    n.name = r.label;
    return n;
  }
  const HUB = { x: 0, y: 0 };                // positions are metres from the hub
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
  function links() {
    const live = [...radio].sort((a, b) => (a.key === 'ap:0' ? -1 : b.key === 'ap:0' ? 1 : a.src - b.src));
    const out = live.map((r, k) => {
      const bk = others.findIndex((o) => o.key === 'sat:' + r.key);   // a board the hub pokes: its square
      const n = bk >= 0 ? boardNode(others[bk], bk) : nodeFor(r, k);
      return { r, a: HUB, n, color: PALETTE[k % PALETTE.length], d: Math.hypot(n.x, n.y) };
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
        out.push({ r, a, n, color: PALETTE[(live.length + 3 * k + j) % PALETTE.length], d: Math.hypot(n.x - a.x, n.y - a.y) });
      });
    });
    if (!showOnly) return out;
    return out.filter(({ a }) => (showOnly === 'hub' ? a === HUB : a.key === showOnly));
  }
  const weak = (r) => r.rssi !== null && r.rssi !== undefined && r.rssi <= WEAK_DBM;
  const moving = (r) => (r.link ? !!r.link.moving && r.board.state !== 'gone'
    : r.board ? r.board.state === 'motion' : r.key === 'ap:0' ? boardMotion : !!r.moving);

  // ---------- sensitivity: where a person changes each link ----------
  // An estimate, not a measurement. A body changes a link most inside its
  // first Fresnel zone (path through the point at most lambda/2 longer than
  // the direct one); multipath carries the change well beyond it, fading over
  // the outer zones; and a body next to either end changes every path at
  // once. Walls and furniture bend all of this: the walk test measures it.
  function sensitivity(px, py, a, n, d, lam) {
    const d1 = Math.hypot(px - a.x, py - a.y), d2 = Math.hypot(px - n.x, py - n.y);
    const zones = (d1 + d2 - d) / (lam / 2);           // which Fresnel zone the point is in
    const along = 1 / (1 + 0.22 * zones);               // ~0.8 at the first zone's edge, ~0.2 at 1.5 m out
    const ends = 0.55 * Math.exp(-Math.min(d1, d2) / 0.7);
    return Math.min(1, along * along + ends);
  }
  const heatCv = document.createElement('canvas'), heatMv = document.createElement('canvas');
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
    const geo = JSON.stringify([w, h, channel, L.map(({ a, n, color, r }) => [a.x, a.y, n.x, n.y, color, moving(r)])]);
    const key = geo + '|' + vk;
    if (key === heatKey) return heatView;
    if (heatView && geo === heatKey.split('|')[0] && t - viewMovedAt < 200) return heatView;   // still moving
    heatKey = key;
    heatView = { ...M.view };
    const lam = lambda(), motionRgb = rgbOf(css('--motion'));
    const still = new ImageData(w, h), mov = new ImageData(w, h);
    const spec = L.filter(({ d }) => d >= 0.05).map(({ a, n, color, d, r }) =>
      ({ a, n, d, mv: moving(r), rgb: rgbOf(color), w: weak(r) ? 0.35 : 1 }));
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
    const pts = [[0, 0], ...links().flatMap(({ a, n }) => [[a.x, a.y], [n.x, n.y]])];
    for (const p of M.plans) {                       // and every picture's corners
      if (p.hidden || !p.img) continue;
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
    W = box.clientWidth; H = box.clientHeight;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
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
    const cnt = links().length;                    // something new appeared: show it (a saved view stays at load)
    if (!M.view || (cnt > nodeCount && (nodeCount > 0 || autoFit))) fit();
    nodeCount = cnt;
    drawPlans();
    grid();
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
      const a = (d + lam / 2) / 2, b = Math.sqrt(Math.max(a * a - d * d / 4, 0));
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
      ctx.fillStyle = css('--fg'); ctx.fillText(fmtLen(d), 0, -4);
      ctx.restore();
    }
    particles(L, t, motionC);
    // the board
    const bx = X(0), by = Y(0);
    ctx.fillStyle = css('--card'); ctx.strokeStyle = css('--accent'); ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(bx - 9, by - 9, 18, 18, 4) : ctx.rect(bx - 9, by - 9, 18, 18);
    ctx.fill(); ctx.stroke();
    label(bx, by + 14, others.length ? 'hub' : 'board', null, css('--accent'));
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
      label(x, y + 14, b.label, { motion: 'MOTION', still: 'still', calibrating: 'calibrating', gone: 'not heard' }[b.state] || b.state, mv ? motionC : css('--fg'));
    }
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
      const sub = [r.rssi !== null && r.rssi !== undefined ? `${r.rssi} dBm` : '',
        r.breathing ? `breathing ${Math.round(r.breathing.rate)}/min` : ''].filter(Boolean).join(' · ');
      label(x, y + 15, r.board ? n.name : r.label, r.board ? null : sub || null, css('--fg'));
    }
    drawLight(t);
    lightWaves(t);
    const keys = [['hub', X(0), Y(0)]];
    for (const { n } of L) if (n !== HUB) keys.push([n.key, X(n.x), Y(n.y)]);
    ripples(t, keys);
    $('#map-msg').textContent = L.length ? '' : 'Waiting for live links: the router appears once the board hears it.';
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
  function renderList() {
    const box = $('#map-links');
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
      if (!n.placed && !n.board && n !== HUB) rows.push('Placed from its signal (rough): drag the dot to where it really is.');
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
    cv.setPointerCapture(e.pointerId);
    const [x, y] = pos(e);
    ptrs.set(e.pointerId, { x, y });
    if (ptrs.size === 2) {                         // pinch
      const [a, b] = [...ptrs.values()];
      drag = { pinch: true, dist: Math.hypot(a.x - b.x, a.y - b.y) };
      return;
    }
    if (readOnly) {                                // Home: pan only
      drag = { pan: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y };
      return;
    }
    if (tool && tool.kind === 'move') {
      const p = plan(tool.id);
      drag = { layer: p, x0: x, y0: y, lx: p.x, ly: p.y };
      return;
    }
    const n = tool ? null : hit(x, y);              // while scaling or aligning, taps mark points
    if (!n && !tool && Math.hypot(X(0) - x, Y(0) - y) <= 18) {
      // the board: everything is measured from it, so moving it shifts the rest the other way
      drag = { board: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y,
        nodes: [...M.nodes.values()].map((m) => [m, m.x, m.y]), plans: M.plans.map((p) => [p, p.x, p.y]) };
      return;
    }
    drag = n ? { key: n.key } : { pan: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y, tap: true };
    autoFit = false; viewByUser = true;
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
    } else if (drag.key) {
      const n = M.nodes.get(drag.key), [wx, wy] = toWorld(x, y);
      n.x = wx; n.y = wy; n.placed = true;
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
    if (!readOnly && drag && (drag.key || drag.pan || drag.layer || drag.board)) scheduleSave();
    drag = ptrs.size === 1 ? { pan: true, x0: [...ptrs.values()][0].x, y0: [...ptrs.values()][0].y, vx: M.view.x, vy: M.view.y } : null;
  }

  // ---------- saving ----------
  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1000);
  }
  async function save() {
    const body = { unit: M.unit, env: M.env, nodes: [...M.nodes.values()].slice(0, 64).map((n) =>
      ({ key: n.key, name: String(n.name).slice(0, 64), x: n.x, y: n.y, placed: !!n.placed })) };
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
    for (const n of m.nodes || []) M.nodes.set(n.key, { key: n.key, name: n.name || n.key, x: n.x, y: n.y, placed: !!n.placed });
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
    if (readOnly && visible) fit();                 // Home: show the whole property once it loads
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
    try {
      const r = await fetch('api/map', { headers: { 'X-Ackwave': '1' } });
      apply(r.ok ? await r.json() : {});
    } catch (e) { /* start fresh */ }
    loaded = true;
    syncControls();
    if (readOnly) fit();
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
      if (ro && !readOnly) { pageView = M.view && { ...M.view }; homeFit = true; }   // Home: its own view
      if (!ro && readOnly && pageView) { M.view = pageView; pageView = null; }       // back: the page's view
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
      if (slow) { radio = slow.radio || []; others = slow.map_boards || []; routersInfo = slow.routers || []; if (slow.channel) channel = slow.channel; }
      if (header) boardMotion = hubMoving(header);
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
      if (!visible || !loaded) return;
      dirty = true;
      renderList();
    },
    focus: focusKey,
    fmtLen,                                    // for tests in the browser
    _layers: () => M.plans.map(({ img, ...p }) => p),
    _screenOf(id, u, v) {                      // where picture pixel (u, v) is on the canvas
      const p = plan(id), k = p.w / p.iw, c = Math.cos(p.rot), s = Math.sin(p.rot);
      return [X(p.x + (u * c - v * s) * k), Y(p.y + (u * s + v * c) * k)];
    },
  };
})();
