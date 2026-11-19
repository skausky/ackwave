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
  const M = { unit: 'ft', env: 'home', view: null, nodes: new Map() };   // key -> {key, name, x, y, placed}
  let radio = [], channel = 6, boardMotion = false;
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
  function links() {                        // [{r, n, color, d}] for the live links, router first
    const live = [...radio].sort((a, b) => (a.key === 'ap:0' ? -1 : b.key === 'ap:0' ? 1 : a.src - b.src));
    return live.map((r, k) => {
      const n = nodeFor(r, k);
      return { r, n, color: PALETTE[k % PALETTE.length], d: Math.hypot(n.x, n.y) };
    });
  }
  const moving = (r) => (r.key === 'ap:0' ? boardMotion : !!r.moving);

  // ---------- view ----------
  const X = (x) => W / 2 + (x - M.view.x) * M.view.s;
  const Y = (y) => H / 2 + (y - M.view.y) * M.view.s;
  const toWorld = (px, py) => [M.view.x + (px - W / 2) / M.view.s, M.view.y + (py - H / 2) / M.view.s];
  function fit() {
    const pts = [[0, 0], ...[...M.nodes.values()].filter((n) => radio.some((r) => r.key === n.key)).map((n) => [n.x, n.y])];
    let x0 = Math.min(...pts.map((p) => p[0])), x1 = Math.max(...pts.map((p) => p[0]));
    let y0 = Math.min(...pts.map((p) => p[1])), y1 = Math.max(...pts.map((p) => p[1]));
    const sx = (Math.max(W, 200) - 110) / Math.max(x1 - x0, 2), sy = (Math.max(H, 200) - 110) / Math.max(y1 - y0, 2);
    M.view = { x: (x0 + x1) / 2, y: (y0 + y1) / 2 + 0.3, s: Math.min(2000, Math.max(2, Math.min(sx, sy))) };   // room for labels
    dirty = true;
  }
  function zoomAt(px, py, k) {
    const [wx, wy] = toWorld(px, py);
    M.view.s = Math.min(2000, Math.max(2, M.view.s * k));
    M.view.x = wx - (px - W / 2) / M.view.s;
    M.view.y = wy - (py - H / 2) / M.view.s;
    dirty = true;
    scheduleSave();
  }

  // ---------- drawing ----------
  function resize() {
    dpr = window.devicePixelRatio || 1;
    const box = cv.parentElement;
    W = box.clientWidth; H = box.clientHeight;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    dirty = true;
  }
  function grid() {
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
    // scale bar, bottom left
    const barM = step * major, bx = 14, by = H - 16, bw = barM * M.view.s;
    if (bw < W * 0.6) {
      ctx.strokeStyle = css('--fg'); ctx.fillStyle = css('--fg'); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(bx, by - 4); ctx.lineTo(bx, by); ctx.lineTo(bx + bw, by); ctx.lineTo(bx + bw, by - 4); ctx.stroke();
      ctx.font = '11px ui-sans-serif, system-ui, sans-serif'; ctx.textBaseline = 'bottom';
      ctx.fillText(fmtLen(barM), bx + 4, by - 5);
    }
  }
  function ellipse(cx, cy, a, b, rot) {
    ctx.beginPath();
    ctx.ellipse(X(cx), Y(cy), Math.max(a * M.view.s, 0.5), Math.max(b * M.view.s, 0.5), rot, 0, Math.PI * 2);
  }
  function alpha(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
  }
  function draw(t) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!M.view) fit();
    grid();
    const L = links(), lam = lambda(), motionC = css('--motion');
    const pulse = 0.5 + 0.5 * Math.sin(t / 260);
    for (const { r, n, color, d } of L) {          // zones first, dots on top
      if (d < 0.05) continue;
      const rot = Math.atan2(n.y, n.x), cx = n.x / 2, cy = n.y / 2;
      for (const z of [2, 1]) {
        const a = (d + z * lam / 2) / 2, b = Math.sqrt(Math.max(a * a - d * d / 4, 0));
        ellipse(cx, cy, a, b, rot);
        if (z === 1) {
          const mv = moving(r);
          ctx.fillStyle = mv ? alpha(motionC, 0.18 + 0.32 * pulse) : alpha(color, 0.16);
          ctx.fill();
          ctx.strokeStyle = mv ? motionC : alpha(color, 0.9); ctx.lineWidth = mv ? 2 : 1.4; ctx.setLineDash([]);
          ctx.stroke();
        } else {
          ctx.strokeStyle = alpha(color, 0.45); ctx.lineWidth = 1; ctx.setLineDash([5, 5]); ctx.stroke(); ctx.setLineDash([]);
        }
      }
      ctx.strokeStyle = alpha(color, 0.55); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(X(0), Y(0)); ctx.lineTo(X(n.x), Y(n.y)); ctx.stroke();
      // the distance, at the middle, kept upright
      ctx.save();
      ctx.translate(X(cx), Y(cy));
      let ang = rot; if (ang > Math.PI / 2 || ang < -Math.PI / 2) ang += Math.PI;
      ctx.rotate(ang);
      ctx.font = '11.5px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      ctx.fillStyle = css('--fg'); ctx.fillText(fmtLen(d), 0, -4);
      ctx.restore();
    }
    // the board
    const bx = X(0), by = Y(0);
    ctx.fillStyle = css('--card'); ctx.strokeStyle = css('--accent'); ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(bx - 9, by - 9, 18, 18, 4) : ctx.rect(bx - 9, by - 9, 18, 18);
    ctx.fill(); ctx.stroke();
    label(bx, by + 14, 'board', null, css('--accent'));
    for (const { r, n, color } of L) {
      const x = X(n.x), y = Y(n.y);
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      ctx.strokeStyle = css('--bg'); ctx.lineWidth = 2.5; ctx.stroke();
      if (!n.placed) {                        // a guess from its signal: dashed ring
        ctx.beginPath(); ctx.arc(x, y, 14, 0, Math.PI * 2);
        ctx.strokeStyle = alpha(color, 0.8); ctx.lineWidth = 1.2; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
      }
      label(x, y + 15, r.label, r.rssi !== null && r.rssi !== undefined ? `${r.rssi} dBm` : null, css('--fg'));
    }
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
    const anyMotion = links().some(({ r }) => moving(r));
    if (dirty || anyMotion) { draw(t); dirty = false; }
    raf = requestAnimationFrame(loop);
  }

  // ---------- the list beside the map ----------
  function renderList() {
    const box = $('#map-links');
    box.replaceChildren();
    const L = links();
    if (!L.length) box.append(el('p', { className: 'muted small', textContent: 'No live links yet.' }));
    for (const { r, n, color, d } of L) {
      const mv = moving(r);
      const st = r.key === 'ap:0' ? (boardMotion ? 'MOTION' : 'still') :
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
        rows.push(`${r.rssi} dBm (${q}) · ${r.rate} answers a second`);
        rows.push(`Distance ${fmtLen(d)} · motion zone ${fmtLen(2 * zoneR(1, d / 2, d / 2))} wide in the middle`);
        rows.push(Math.abs(diff) <= 3 ? `About what open air would give here (${Math.round(exp)} dBm)`
          : diff < 0 ? `${-diff} dB weaker than open air would give here (${Math.round(exp)} dBm): walls, furniture or people in between`
            : `${diff} dB stronger than expected here (${Math.round(exp)} dBm): it may be closer than placed, or a strong device`);
      }
      if (!n.placed) rows.push('Placed from its signal (rough): drag the dot to where it really is.');
      box.append(el('div', { className: 'map-link' + (mv ? ' moving' : '') }, head,
        ...rows.map((t) => el('div', { className: 'muted small', textContent: t }))));
    }
    $('#map-wave').textContent = `Channel ${channel} · ${(freq() / 1e6).toFixed(0)} MHz · wavelength ${M.unit === 'ft' ? (lambda() / IN).toFixed(1) + '″' : (lambda() * 100).toFixed(1) + ' cm'}`;
  }

  // ---------- touch / mouse ----------
  function hit(px, py) {
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
    const n = hit(x, y);
    drag = n ? { key: n.key } : { pan: true, x0: x, y0: y, vx: M.view.x, vy: M.view.y };
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
    } else if (drag.pan) {
      M.view.x = drag.vx - (x - drag.x0) / M.view.s;
      M.view.y = drag.vy - (y - drag.y0) / M.view.s;
      dirty = true;
    }
  }
  function up(e) {
    ptrs.delete(e.pointerId);
    if (drag && (drag.key || drag.pan)) scheduleSave();
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
    if (M.view) body.view = M.view;
    try {
      await fetch('/api/map', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ackwave': '1' }, body: JSON.stringify(body) });
    } catch (e) { /* the next change tries again */ }
  }
  function apply(m) {
    if (m.unit) M.unit = m.unit;
    if (m.env) M.env = m.env;
    if (m.view) M.view = m.view;
    for (const n of m.nodes || []) M.nodes.set(n.key, { key: n.key, name: n.name || n.key, x: n.x, y: n.y, placed: !!n.placed });
  }
  async function load() {
    try {
      const r = await fetch('/api/map', { headers: { 'X-Ackwave': '1' } });
      apply(r.ok ? await r.json() : {});
    } catch (e) { /* start fresh */ }
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
    $('#map-fit').onclick = () => { fit(); scheduleSave(); };
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
  }

  let started = false;
  return {
    show() {
      if (!started) { init(); started = true; }
      visible = true;
      resize();
      if (!loaded) load();
      if (!raf) raf = requestAnimationFrame(loop);
    },
    hide() { visible = false; },
    // headless snapshots: the layout handed in directly, drawn at once
    snapshot(m, slow, header) {
      if (!started) { init(); started = true; }
      apply(m || {}); loaded = true; visible = true;
      syncControls(); resize();
      radio = slow.radio || []; if (slow.channel) channel = slow.channel;
      boardMotion = header && header.badge && header.badge.word === 'MOTION';
      renderList(); draw(performance.now());
    },
    // slow payload (once a second) and the header (the board's verdict)
    update(slow, header) {
      if (slow) { radio = slow.radio || []; if (slow.channel) channel = slow.channel; }
      if (header) boardMotion = header.badge && header.badge.word === 'MOTION';
      if (!visible || !loaded) return;
      dirty = true;
      renderList();
    },
    fmtLen,                                    // for tests in the browser
  };
})();
