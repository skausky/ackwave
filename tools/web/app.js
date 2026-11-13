'use strict';
// ackwave dashboard: live data from /events (Server-Sent Events), commands to /cmd.
// Charts are drawn on canvas by hand: labels live in the margins, never on the data.

const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const C = {
  grid: css('--grid'), line: css('--line'), muted: css('--muted'), fg: css('--fg'),
  motion: css('--motion'), still: css('--still'), brief: css('--brief'), scan: css('--scan'),
  cal: css('--cal'), long: css('--long'), blue: css('--blue'), card: css('--card'),
};
const FONT = '11px ui-sans-serif, system-ui, sans-serif';

let win = 30, paused = false, es = null;
let link = null;                         // per-link focus for breathing / Doppler (null = router)
let fast = null, slow = null;            // latest payloads
let netDlgShown = false;

// ---------------------------------------------------------------- data link
function connect() {
  if (es) es.close();
  es = new EventSource(`/events?win=${win}${link !== null ? `&link=${link}` : ''}`);
  setLive('connecting');
  es.addEventListener('fast', (e) => { fast = JSON.parse(e.data); setLive(paused ? 'paused' : 'live'); if (!paused) renderFast(); });
  es.addEventListener('slow', (e) => { slow = JSON.parse(e.data); renderSlow(); });
  es.onerror = () => {
    setLive('offline');
    setBadge({ word: 'OFFLINE', color: C.motion, sub: 'lost the dashboard server: retrying…' });
  };
}

async function cmd(body) {
  try {
    const r = await fetch('/cmd', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ackwave': '1' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    toast(j.msg || (j.ok ? 'done' : 'failed'), !j.ok);
    return j.ok;
  } catch (e) {
    toast('could not reach the dashboard server', true);
    return false;
  }
}

function setLive(state) {
  const el = $('#live');
  el.className = 'live ' + ({ live: 'on', paused: 'paused', offline: 'off' }[state] || '');
  el.querySelector('span').textContent = state;
}

let toastTimer = null;
function toast(text, err = false) {
  const t = $('#toast');
  t.textContent = text;
  t.classList.toggle('err', !!err);
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 4500);
}

// ---------------------------------------------------------------- header
function setBadge(b) {
  const el = $('#badge');
  el.textContent = b.word;
  el.style.color = b.color;
  el.classList.toggle('long', b.word.length > 8);
  $('#badge-sub').textContent = b.sub || '';
  document.title = `${b.word} · ackwave`;
}

function renderHeader(h) {
  setBadge(h.badge);
  const link = $('#link');
  link.querySelector('i').style.background = h.link.color;
  link.querySelector('b').textContent = `link ${h.link.word}`;
  link.querySelector('span').textContent = h.link.detail;
  for (const [k, id] of [['breathing', '#tile-breathing'], ['doppler', '#tile-doppler'], ['quiet', '#tile-quiet']]) {
    const t = h.tiles[k], el = $(id);
    el.querySelector('.value').textContent = t.value;
    el.querySelector('.value').style.color = t.color;
    el.querySelector('.detail').textContent = t.detail;
  }
  const ol = $('#recent');
  ol.replaceChildren();
  if (!h.recent.length) {
    const li = document.createElement('li'); li.className = 'muted'; li.textContent = 'none yet'; ol.append(li);
  }
  for (const r of h.recent) {
    const li = document.createElement('li');
    li.className = r.only ? 'only' : r.going ? 'going' : r.brief ? 'brief' : '';
    if (r.only) li.title = "Seen by a device link only: the board's own verdict stayed still";
    for (const [txt, cls] of [[r.at, ''], [r.dur, ''], [r.where || '', 'where'], [r.peak === null ? '' : `×${r.peak.toFixed(1)}`, 'peak']]) {
      const s = document.createElement('span'); s.textContent = txt; if (cls) s.className = cls; li.append(s);
    }
    ol.append(li);
  }
  const counts = $('#counts');
  counts.replaceChildren();
  for (const [n, label] of [[h.counts.motion, 'motion'], [h.counts.brief, 'brief'], [h.counts.scans, 'scans ignored']]) {
    const c = document.createElement('span'); c.className = 'chip';
    const b = document.createElement('b'); b.textContent = n; c.append(b, ` ${label}`); counts.append(c);
  }
  $('#foot-board').textContent = `board says ${h.board}`;
  $('#foot-dev').textContent = h.devices;
  $('#foot-log').textContent = h.log ? `logging to ${h.log}` : '';
  for (const id of ['#btn-cal', '#btn-net', '#btn-dev']) $(id).disabled = !h.board_connected;
}

// ---------------------------------------------------------------- canvas helpers
function canvasFor(id) {
  const box = $(id), cv = box.querySelector('canvas');
  const dpr = window.devicePixelRatio || 1;
  const w = box.clientWidth, h = box.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.font = FONT;
  return { ctx, w, h };
}

const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
function timeTicks(t0, t1, px) {
  const want = Math.max(2, Math.floor(px / 95));
  const step = STEPS.find((s) => (t1 - t0) / s <= want) || STEPS[STEPS.length - 1];
  const out = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return { ticks: out, step };
}
function clock(t, withSec) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return withSec ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` : `${p(d.getHours())}:${p(d.getMinutes())}`;
}

// A plot frame: margins for y labels (left), threshold labels (right), time labels (bottom).
function frame(ctx, w, h, o) {
  const r = { l: o.left ?? 40, r: w - (o.right ?? 12), t: 6, b: h - 20 };
  const X = (t) => r.l + (t - o.t0) / (o.t1 - o.t0) * (r.r - r.l);
  const Y = (v) => r.b - (v - o.y0) / (o.y1 - o.y0) * (r.b - r.t);
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.fillStyle = C.muted;
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (const v of o.yticks || []) {
    const y = Math.round(Y(v)) + .5;
    ctx.beginPath(); ctx.moveTo(r.l, y); ctx.lineTo(r.r, y); ctx.stroke();
    ctx.fillText(String(v), r.l - 6, y);
  }
  const { ticks, step } = timeTicks(o.t0, o.t1, r.r - r.l);
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const t of ticks) {
    const x = Math.round(X(t)) + .5;
    ctx.beginPath(); ctx.moveTo(x, r.t); ctx.lineTo(x, r.b); ctx.stroke();
    if (x - r.l < 24 || r.r - x < 24) continue;   // too close to an edge: the label would collide
    ctx.fillText(clock(t, step < 60), x, r.b + 5);
  }
  ctx.strokeStyle = C.line;
  ctx.strokeRect(r.l + .5, r.t + .5, r.r - r.l - 1, r.b - r.t - 1);
  return { r, X, Y, o };
}

// ---------------------------------------------------------------- readout under the cursor
// Every chart shows the time and values under the mouse or finger: a line on
// the chart and a small box beside it (DOM, so the canvas isn't redrawn).
const hov = {};                          // chart id -> { x, y, f, info, line, tip }
function hoverable(id, f, info) {
  const box = $(id);
  let h = hov[id];
  if (!h) {
    h = hov[id] = { x: null, y: 0 };
    h.line = Object.assign(document.createElement('div'), { className: 'cross', hidden: true });
    h.tip = Object.assign(document.createElement('div'), { className: 'tip', hidden: true });
    box.append(h.line, h.tip);
    const move = (e) => {
      const b = box.getBoundingClientRect();
      h.x = e.clientX - b.left; h.y = e.clientY - b.top;
      showHover(id);
    };
    const hide = () => { h.x = null; showHover(id); };
    box.addEventListener('pointermove', move);
    box.addEventListener('pointerdown', move);
    box.addEventListener('pointerleave', hide);
    box.addEventListener('pointercancel', hide);
  }
  h.f = f; h.info = info;
  showHover(id);
}
function showHover(id) {
  const h = hov[id], f = h.f;
  const on = h.x !== null && !!f && h.x >= f.r.l && h.x <= f.r.r;
  h.line.hidden = h.tip.hidden = !on;
  if (!on) return;
  const t = f.o.t0 + (h.x - f.r.l) / (f.r.r - f.r.l) * (f.o.t1 - f.o.t0);
  const v = f.o.y0 + (f.r.b - h.y) / (f.r.b - f.r.t) * (f.o.y1 - f.o.y0);
  h.line.style.cssText = `left:${h.x}px;top:${f.r.t}px;height:${f.r.b - f.r.t}px`;
  h.tip.replaceChildren(Object.assign(document.createElement('b'), { textContent: clock(t, true) }));
  for (const line of h.info(t, v)) if (line) h.tip.append(Object.assign(document.createElement('div'), { textContent: line }));
  const left = h.x < (f.r.l + f.r.r) / 2;      // beside the line, on the roomier side
  const bw = $(id).clientWidth;                // never wider than the room beside the line
  h.tip.style.cssText = `top:${f.r.t + 4}px;max-width:${Math.max(90, (left ? bw - h.x : h.x) - 14)}px;` +
    (left ? `left:${h.x + 10}px` : `right:${bw - h.x + 10}px`);
}
// Index of the value in the sorted array closest to t, or -1 if none within tol
function nearest(t, arr, tol) {
  let lo = 0, hi = arr.length - 1;
  if (hi < 0) return -1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < t) lo = m + 1; else hi = m; }
  if (lo > 0 && t - arr[lo - 1] < arr[lo] - t) lo--;
  return Math.abs(arr[lo] - t) <= tol ? lo : -1;
}
const inSpan = (spans, t) => (spans || []).some(([a, b]) => a <= t && t <= b);

function band(ctx, f, a, b, color, alpha) {
  const x0 = Math.max(f.X(a), f.r.l), x1 = Math.min(f.X(b), f.r.r);
  if (x1 <= x0) return;
  ctx.globalAlpha = alpha; ctx.fillStyle = color;
  ctx.fillRect(x0, f.r.t, x1 - x0, f.r.b - f.r.t);
  ctx.globalAlpha = 1;
}

function polyline(ctx, f, t, v, gap, ymax) {
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i < t.length; i++) {
    if (i && t[i] - t[i - 1] > gap) pen = false;
    const x = f.X(t[i]), y = f.Y(Math.min(v[i], ymax));
    if (x < f.r.l - 1) { pen = false; continue; }
    if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    pen = true;
  }
  ctx.stroke();
}

// Labels in the right margin, pushed apart so they never overlap.
function marginLabels(ctx, f, items) {
  items = items.map((it) => ({ ...it, y: f.Y(it.v) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < items.length; i++) items[i].y = Math.max(items[i].y, items[i - 1].y + 13);
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  for (const it of items) {
    ctx.fillStyle = it.color;
    ctx.fillText(it.text, f.r.r + 6, Math.min(Math.max(it.y, f.r.t + 5), f.r.b - 3));
  }
}

function niceTicks(max, n = 4) {
  const raw = max / n, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw);
  const out = [];
  for (let v = 0; v <= max + 1e-9; v += step) out.push(+v.toFixed(2));
  return out;
}

// ---------------------------------------------------------------- charts
function drawScore() {
  const { ctx, w, h } = canvasFor('#score');
  const msg = $('#score-msg');
  if (!fast || !fast.score) { msg.textContent = 'waiting for data…'; return; }
  const s = fast.score, [t0, t1] = fast.window;
  let ymax = s.on_s * 1.6;
  for (const se of s.series) for (const v of se.v) ymax = Math.max(ymax, v * 1.12);
  if (s.long) for (const v of s.long.v) ymax = Math.max(ymax, v * 1.12);
  ymax = Math.min(ymax, 40);
  const f = frame(ctx, w, h, { t0, t1, y0: 0, y1: ymax, yticks: niceTicks(ymax), left: 34, right: 78 });
  ctx.save();
  ctx.beginPath(); ctx.rect(f.r.l, f.r.t, f.r.r - f.r.l, f.r.b - f.r.t); ctx.clip();
  for (const [a, b] of s.cal) band(ctx, f, a, b, C.cal, .14);
  for (const [a, b] of s.scans) band(ctx, f, a, b, C.scan, .18);
  for (const [a, b] of s.motion) band(ctx, f, a, b, C.motion, .14);
  ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
  for (const [v, col] of [[1, C.muted], [s.on_l, C.long], [s.on_s, C.motion]]) {
    ctx.strokeStyle = col; ctx.globalAlpha = .75;
    ctx.beginPath(); ctx.moveTo(f.r.l, f.Y(v)); ctx.lineTo(f.r.r, f.Y(v)); ctx.stroke();
  }
  ctx.globalAlpha = 1;
  for (const se of s.series) {
    ctx.strokeStyle = se.color; ctx.lineWidth = se.dash === 'solid' ? 1.4 : 1.1;
    ctx.setLineDash(se.dash === 'solid' ? [] : se.dash === 'dash' ? [5, 3] : [1.5, 3]);
    ctx.globalAlpha = se.dash === 'dot' ? .75 : .9;
    polyline(ctx, f, se.t, se.v, s.gap, ymax * 1.5);
  }
  ctx.globalAlpha = 1; ctx.setLineDash([]);
  if (s.long) { ctx.strokeStyle = C.long; ctx.lineWidth = 2.4; polyline(ctx, f, s.long.t, s.long.v, s.gap, ymax * 1.5); }
  for (const hop of s.hops) {                      // channel hop: a line + marker, no text
    const x = f.X(hop);
    ctx.strokeStyle = C.fg; ctx.globalAlpha = .45; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, f.r.t); ctx.lineTo(x, f.r.b); ctx.stroke();
    ctx.globalAlpha = 1; ctx.fillStyle = C.fg;
    ctx.beginPath(); ctx.moveTo(x - 4, f.r.t); ctx.lineTo(x + 4, f.r.t); ctx.lineTo(x, f.r.t + 6); ctx.fill();
  }
  for (const [t, m] of s.board) {                  // the board's own verdict: ticks on the floor
    ctx.fillStyle = m ? C.motion : C.still;
    ctx.fillRect(f.X(t) - 1, f.r.b - 7, 2, 7);
  }
  ctx.restore();
  marginLabels(ctx, f, [
    { v: s.on_s, text: `short ×${s.on_s.toFixed(1)}`, color: C.motion },
    { v: s.on_l, text: `long ×${s.on_l.toFixed(1)}`, color: C.long },
    { v: 1, text: 'quiet', color: C.muted },
  ]);
  msg.textContent = '';
  const st = $('#score-state');                    // status lives in the header, never on the data
  st.hidden = !s.calibrating;
  st.textContent = 'calibrating · keep still';
  renderLegend(s);
  hoverable('#score', f, (t) => {
    const tol = Math.max(s.gap, (t1 - t0) / 150), out = [];
    for (const se of s.series) {
      const i = nearest(t, se.t, tol);
      if (i >= 0) out.push(`${se.label} ×${se.v[i].toFixed(1)}`);
    }
    const i = s.long ? nearest(t, s.long.t, tol) : -1;
    if (i >= 0) out.push(`long window ×${s.long.v[i].toFixed(1)}`);
    if (!out.length) out.push('no data');
    out.push(inSpan(s.motion, t) && 'motion', inSpan(s.scans, t) && 'router scan (ignored)', inSpan(s.cal, t) && 'calibrating');
    return out;
  });
}

let legendKey = '';
function renderLegend(s) {
  const items = s.series.map((se) => [se.label, se.color, se.dash]);
  items.push(['long window (4 s)', C.long, 'solid-thick']);
  if (s.scans.length) items.push(['router scan (ignored)', C.scan, 'band']);
  if (s.hops.length) items.push(['channel hop ▼', C.fg, 'none']);
  items.push(['board verdict ▮', C.still, 'none']);
  const key = JSON.stringify(items);
  if (key === legendKey) return;
  legendKey = key;
  const box = $('#score-legend');
  box.replaceChildren();
  for (const [label, color, dash] of items) {
    const sp = document.createElement('span');
    if (dash !== 'none') {
      const i = document.createElement('i');
      i.style.color = color; i.style.background = color;
      i.className = dash === 'dash' ? 'dash' : dash === 'dot' ? 'dot' : dash === 'band' ? '' : 'line';
      if (dash === 'solid-thick') i.style.height = '4px';
      sp.append(i);
    }
    sp.append(label);
    box.append(sp);
  }
}

// magma-like colour map, 0..20 dB
const MAGMA = [[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164]];
function magma(x) {
  x = Math.max(0, Math.min(1, x)) * (MAGMA.length - 1);
  const i = Math.min(Math.floor(x), MAGMA.length - 2), f = x - i, a = MAGMA[i], b = MAGMA[i + 1];
  return `rgb(${a[0] + (b[0] - a[0]) * f | 0},${a[1] + (b[1] - a[1]) * f | 0},${a[2] + (b[2] - a[2]) * f | 0})`;
}

function drawDoppler() {
  const { ctx, w, h } = canvasFor('#doppler');
  const msg = $('#doppler-msg');
  if (!fast || !fast.window) { msg.textContent = 'waiting for data…'; return; }
  const [t0, t1] = fast.window, d = slow && slow.doppler;
  const fmax = d ? d.fmax : 50;
  const f = frame(ctx, w, h, { t0, t1, y0: 0, y1: fmax, yticks: [0, 10, 20, 30, 40, 50].filter((v) => v <= fmax), left: 34, right: 12 });
  if (!d) { msg.textContent = 'collecting (needs the router\'s ACKs)'; return; }
  msg.textContent = '';
  ctx.save();
  ctx.beginPath(); ctx.rect(f.r.l, f.r.t, f.r.r - f.r.l, f.r.b - f.r.t); ctx.clip();
  ctx.fillStyle = '#05060a'; ctx.fillRect(f.r.l, f.r.t, f.r.r - f.r.l, f.r.b - f.r.t);
  const nb = d.db[0].length, bh = (f.r.b - f.r.t) / nb;
  for (let i = 0; i < d.t.length; i++) {
    const x0 = f.X(d.t[i]), x1 = f.X(d.t[i] + d.step) + 1;
    const row = d.db[i];
    for (let j = 0; j < nb; j++) {
      if (row[j] <= 0) continue;
      ctx.fillStyle = magma(row[j] / 20);
      ctx.fillRect(x0, f.r.b - (j + 1) * bh, x1 - x0, bh + .5);
    }
  }
  if (fast.score) for (const [a, b] of fast.score.scans) band(ctx, f, a, b, C.scan, .25);
  ctx.setLineDash([2, 3]); ctx.strokeStyle = C.muted;
  ctx.beginPath(); ctx.moveTo(f.r.l, f.Y(d.low)); ctx.lineTo(f.r.r, f.Y(d.low)); ctx.stroke();
  ctx.restore();
  hoverable('#doppler', f, (t, hz) => {
    const i = nearest(t - d.step / 2, d.t, d.step), j = Math.floor(hz / fmax * nb);
    if (i < 0 || j < 0 || j >= nb) return ['no data'];
    return [`${hz.toFixed(0)} Hz`, d.db[i][j] > 0 ? `${d.db[i][j]} dB over the quiet room` : 'as the quiet room',
      fast.score && inSpan(fast.score.scans, t) && 'router scan (ignored)'];
  });
}

function drawBreath() {
  const { ctx, w, h } = canvasFor('#breath');
  if (!fast || !fast.window) return;
  const [t0, t1] = fast.window;
  const f = frame(ctx, w, h, { t0, t1, y0: 0, y1: 36, yticks: [0, 12, 24, 36], left: 34, right: 12 });
  ctx.fillStyle = C.still; ctx.globalAlpha = .07;
  ctx.fillRect(f.r.l, f.Y(20), f.r.r - f.r.l, f.Y(12) - f.Y(20));
  ctx.globalAlpha = 1;
  for (const [t, rate, level] of fast.breathing || []) {
    const x = f.X(t);
    if (x < f.r.l) continue;
    if (level === 'none') {
      ctx.fillStyle = C.muted; ctx.globalAlpha = .5;
      ctx.fillRect(x - 1, f.Y(1.2) - 1, 2, 2); ctx.globalAlpha = 1;
      continue;
    }
    ctx.beginPath(); ctx.arc(x, f.Y(rate), level === 'clear' ? 4 : 3.5, 0, Math.PI * 2);
    if (level === 'clear') { ctx.fillStyle = C.still; ctx.fill(); }
    else { ctx.strokeStyle = C.brief; ctx.lineWidth = 1.5; ctx.stroke(); }
  }
  const pts = fast.breathing || [];
  hoverable('#breath', f, (t) => {
    const i = nearest(t, pts.map((p) => p[0]), 3);
    if (i < 0) return ['no reading'];
    return [pts[i][2] === 'none' ? 'no breathing found' : `${pts[i][1].toFixed(0)} a minute (${pts[i][2]})`];
  });
}

function drawTimeline() {
  const { ctx, w, h } = canvasFor('#timeline');
  const tl = slow && slow.timeline;
  if (!tl) return;
  const ymax = tl.on_s * 1.8;
  const f = frame(ctx, w, h, { t0: tl.start, t1: tl.end, y0: 0, y1: ymax, yticks: niceTicks(ymax, 3), left: 34, right: 12 });
  ctx.save();
  ctx.beginPath(); ctx.rect(f.r.l, f.r.t, f.r.r - f.r.l, f.r.b - f.r.t); ctx.clip();
  for (const [a, b] of tl.cal) band(ctx, f, a, b, C.cal, .2);
  for (const [a, b, brief] of tl.motion) {
    if (brief) { ctx.fillStyle = C.brief; ctx.fillRect(f.X(a) - .75, f.Y(ymax * .22), 1.5, f.r.b - f.Y(ymax * .22)); }
    else band(ctx, f, a, Math.max(b, a + (tl.end - tl.start) / 600), C.motion, .33);
  }
  ctx.fillStyle = C.scan;
  for (const a of tl.scans) ctx.fillRect(f.X(a) - .75, f.r.t, 1.5, 10);
  ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
  for (const [v, col] of [[tl.on_l, C.long], [tl.on_s, C.motion]]) {
    ctx.strokeStyle = col; ctx.globalAlpha = .5;
    ctx.beginPath(); ctx.moveTo(f.r.l, f.Y(v)); ctx.lineTo(f.r.r, f.Y(v)); ctx.stroke();
  }
  ctx.globalAlpha = 1; ctx.setLineDash([]);
  if (tl.t) {
    const gap = Math.max(5, (tl.end - tl.start) / 300);
    ctx.strokeStyle = C.blue; ctx.lineWidth = .9; ctx.globalAlpha = .85;
    polyline(ctx, f, tl.t, tl.s, gap, ymax * .98);
    ctx.globalAlpha = 1; ctx.strokeStyle = C.long; ctx.lineWidth = 1.4;
    polyline(ctx, f, tl.t, tl.l, gap, ymax * .98);
  }
  ctx.restore();
  hoverable('#timeline', f, (t) => {
    const i = tl.t ? nearest(t, tl.t, Math.max(3, (tl.end - tl.start) / 200)) : -1;
    const m = tl.motion.find(([a, b]) => a - 1 <= t && t <= b + 1);
    return [i >= 0 ? `short ×${tl.s[i].toFixed(1)} · long ×${tl.l[i].toFixed(1)}` : 'no data',
      m && (m[2] ? 'brief blip' : 'motion'), inSpan(tl.cal, t) && 'calibrating'];
  });
}

// ---------------------------------------------------------------- render loops
function renderFast() {
  if (!fast) return;
  renderHeader(fast.header);
  drawScore(); drawBreath(); drawDoppler();
}

function renderSlow() {
  if (!slow) return;
  drawTimeline();
  renderLinks();
  if (!paused) drawDoppler();
  renderRaw();
  if ($('#dlg-net').open) renderNet();
  if ($('#dlg-dev').open) renderDev();
  if ($('#dlg-set').open) { renderSound(); renderSens(); }
  const n = slow.network;
  $('#net').textContent = n && n.name ? `${n.name}${n.how === 'default' ? '' : n.how === 'chosen' ? ' · chosen' : ''}` : '';
}

function renderRaw() {
  const d = slow.details, lines = [];
  if (d.channel) lines.push(`channel ${d.channel[0]} (${d.channel[1] || 'start'})   calibration ${d.phase} (${d.why})   quiet ${d.floor ? d.floor.toFixed(4) : '–'}`);
  for (const s of d.streams) lines.push(`${s.label.padEnd(26)} ${s.rate.toFixed(0).padStart(4)}/s  ${s.rssi} dBm`);
  if (d.stats) {
    const st = d.stats, ratio = st.poke ? Math.round(100 * st.ack / st.poke) : 0;
    lines.push(`pokes ${st.poke}   ACK CSI ${st.ack} (${ratio}%)   tx errors ${st.tx_err}   queue drops ${st.qfull}   lines lost on USB ${st.lost ?? 0}   router replies ${st.reply ?? 0}`);
  }
  $('#raw-text').textContent = lines.join('\n') || 'waiting…';
}

// ---------------------------------------------------------------- buzzer
// Shows what this dashboard keeps the board at (or, if nothing, what the
// board reported); sliders aren't touched while being dragged.
function renderSound() {
  const board = slow && slow.sound, want = slow && slow.settings && slow.settings.sound;
  const cur = want ? { mode: want[0], vol: want[1], hz: want[2] } : board;
  const st = $('#snd-state');
  if (!board) st.textContent = 'waiting for the board (firmware too old?)';
  else if (!board.buzzer) st.textContent = 'no buzzer on this board (BUZZER_GPIO)';
  else st.textContent = want ? 'kept by this dashboard' : "the firmware's settings";
  for (const b of $('#snd-mode').children) b.classList.toggle('on', !!cur && b.dataset.m === cur.mode);
  $('.snd').classList.toggle('off', !!cur && cur.mode === 'off');
  if (!cur) return;
  for (const [id, v] of [['#snd-vol', cur.vol], ['#snd-hz', cur.hz]])
    if (document.activeElement !== $(id) || !$(id).dataset.drag) $(id).value = v;
  showSoundValues();
}
function renderSens() {
  const board = slow && slow.sens, want = slow && slow.settings && slow.settings.sens;
  const cur = want || board;
  $('#sens-state').textContent = !board ? 'waiting for the board (firmware too old?)'
    : `×${(board / 100).toFixed(1)} on the board · ${want ? 'kept by this dashboard' : "the firmware's"}`;
  for (const b of $('#sens-level').children) b.classList.toggle('on', +b.dataset.x === cur);
}
function showSoundValues() {
  $('#snd-vol-out').textContent = `${$('#snd-vol').value * 2}%`;
  $('#snd-hz-out').textContent = `${$('#snd-hz').value} Hz`;
}
function setSound(mode) {
  const on = $('#snd-mode .on');
  cmd({ cmd: 'sound', mode: mode || (on ? on.dataset.m : 'beep'), vol: +$('#snd-vol').value, hz: +$('#snd-hz').value });
}

// ---------------------------------------------------------------- links (experimental)
function renderLinks() {
  const on = !!(slow.settings && slow.settings.per_link);
  $('#links-card').hidden = !on;
  $('#set-per-link').checked = on;
  const rows = on ? slow.links || [] : [];
  if (!on && link !== null) { link = null; connect(); }
  const cur = rows.find((r) => r.src === link);
  for (const el of document.querySelectorAll('.which')) el.textContent = cur ? cur.label : 'router';
  for (const el of document.querySelectorAll('.which-tile')) el.textContent = cur ? `· ${cur.label}` : '';
  if (!on) return;
  const box = $('#links');
  // keep elements (and their canvases) between updates, keyed by node
  const seen = new Set();
  for (const r of rows) {
    seen.add(String(r.src));
    let el = box.querySelector(`[data-src="${r.src}"]`);
    if (!el) {
      el = document.createElement('button');
      el.type = 'button'; el.className = 'link'; el.dataset.src = r.src;
      el.innerHTML = '<div class="name"><span></span><small></small></div><div><span class="act"></span><span class="state"></span></div>' +
        '<div class="spark"><canvas></canvas></div><div class="meta"><span class="br"></span><span class="dop"></span></div>';
      el.onclick = () => { link = link === r.src ? null : r.src; connect(); renderLinks(); };
      box.append(el);
    }
    el.classList.toggle('sel', link === r.src);
    el.querySelector('.name span').textContent = r.label + (r.ap ? '' : ' · device');
    el.querySelector('.name small').textContent = `${r.rate}/s ${r.rssi} dBm`;
    const act = el.querySelector('.act'), st = el.querySelector('.state');
    const judged = r.state === 'motion' || r.state === 'still';
    el.classList.toggle('dim', r.state === 'weak' || r.state === 'silent');
    act.textContent = judged ? `×${r.activity.toFixed(1)}` : '—';
    act.title = r.long === null ? '' : `fast (0.5 s) ×${r.activity.toFixed(2)} · slow (4 s) ×${r.long.toFixed(2)} of its quiet level`;
    act.style.color = r.moving ? C.motion : judged && r.activity > 1.6 ? C.brief : judged ? C.fg : C.muted;
    st.textContent = { motion: 'MOTION', still: 'still', calibrating: 'calibrating', weak: 'weak link', silent: 'no signal' }[r.state] || r.state;
    st.title = { weak: 'Too few ACKs a second to judge motion on this link (it needs about 20)', silent: 'Nothing from this device for over 2 s' }[r.state] || '';
    st.className = 'state' + (r.state === 'motion' ? ' motion' : judged ? '' : ' cal');
    const b = r.breathing, d = r.doppler;
    el.querySelector('.br').textContent = 'breathing ' + (!b ? '…' : b.level === 'none' ? '—' : `${b.rate.toFixed(0)}/min (${b.level})`);
    el.querySelector('.dop').textContent = 'Doppler ' + (d === null || d === undefined ? '…' : !Object.keys(d).length ? '—' : d.scan ? 'scan' : `${d.f.toFixed(1)} Hz`);
    drawSpark(el.querySelector('.spark'), r);
  }
  for (const n of [...box.childNodes]) if (!n.dataset || !seen.has(n.dataset.src)) n.remove();
  if (!rows.length) box.textContent = 'collecting: each link needs about a minute of data';
  drawLanes();
}

// Motion by link: one lane per link (plus the board's own verdict) over the
// window shown above. Names sit in a left gutter, never over the lanes.
function drawLanes() {
  const box = $('#lanes');
  const rows = (slow && slow.links) || [];
  const win = fast && fast.window;
  box.hidden = !rows.length || !win;
  if (box.hidden) return;
  const lanes = [{ label: 'board verdict', spans: slow.board_spans || [], board: true }]
    .concat(rows.map((r) => ({ label: r.label + (r.ap ? '' : ' · device'), spans: r.spans, weak: r.weak, state: r.state })));
  const cv = box.querySelector('canvas'), dpr = window.devicePixelRatio || 1;
  const LH = 20, AX = 18, w = box.clientWidth, h = lanes.length * LH + AX;
  const G = Math.min(170, Math.max(110, w * 0.22));            // label gutter
  cv.style.height = `${h}px`;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const [t0, t1] = win, X = (t) => G + (t - t0) / (t1 - t0) * (w - G);
  ctx.font = '12px ui-sans-serif, system-ui, sans-serif';
  ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  lanes.forEach((ln, i) => {
    const y = i * LH;
    ctx.fillStyle = C.grid; ctx.fillRect(G, y + 3, w - G, LH - 6);
    for (const [a, b] of ln.weak || []) {          // too weak to judge: no lane, a dashed line
      const xa = Math.max(G, X(a)), xb = Math.min(w, X(b));
      if (xb <= xa) continue;
      ctx.clearRect(xa, y + 3, xb - xa, LH - 6);
      ctx.strokeStyle = C.line; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(xa, y + LH / 2 + .5); ctx.lineTo(xb, y + LH / 2 + .5); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.fillStyle = ln.board || ln.state === 'motion' || ln.state === 'still' ? C.fg : C.muted;
    let label = ln.label;
    while (label.length > 3 && ctx.measureText(label).width > G - 10) label = label.slice(0, -2) + '…';
    ctx.fillText(label, 0, y + LH / 2);
    ctx.fillStyle = ln.board ? C.motion : C.brief;
    for (const [a, b] of ln.spans) {
      const xa = Math.max(G, X(a)), xb = Math.min(w, X(b));
      if (xb > xa) ctx.fillRect(xa, y + 3, Math.max(2, xb - xa), LH - 6);
    }
  });
  ctx.font = FONT; ctx.fillStyle = C.muted; ctx.textBaseline = 'top'; ctx.textAlign = 'center';
  const { ticks, step } = timeTicks(t0, t1, w - G);
  for (const t of ticks) {
    const x = X(t), label = clock(t, step < 60), tw = ctx.measureText(label).width;
    if (x - tw / 2 >= G && x + tw / 2 <= w) ctx.fillText(label, x, lanes.length * LH + 4);
  }
  ctx.textAlign = 'left';
  hoverable('#lanes', { r: { l: G, r: w, t: 0, b: lanes.length * LH }, o: { t0, t1, y0: 0, y1: 1 } }, (t) => {
    const saw = lanes.filter((ln) => inSpan(ln.spans, t)).map((ln) => ln.label);
    const weak = lanes.filter((ln) => inSpan(ln.weak, t)).map((ln) => ln.label);
    return [saw.length ? `motion: ${saw.join(', ')}` : 'no motion', weak.length && `too weak to judge: ${weak.join(', ')}`];
  });
}

function drawSpark(box, r) {
  const cv = box.querySelector('canvas'), dpr = window.devicePixelRatio || 1;
  const w = box.clientWidth, h = box.clientHeight;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const t = r.spark.t, v = r.spark.v;
  if (t.length < 2) return;
  const ymax = Math.max(4, ...v), t0 = t[t.length - 1] - 30, t1 = t[t.length - 1];
  const X = (x) => (x - t0) / (t1 - t0) * w, Y = (y) => h - 2 - Math.min(y, ymax) / ymax * (h - 4);
  ctx.strokeStyle = C.motion; ctx.globalAlpha = .5; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ctx.moveTo(0, Y(2.8)); ctx.lineTo(w, Y(2.8)); ctx.stroke();
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  ctx.strokeStyle = r.moving ? C.motion : css('--accent'); ctx.lineWidth = 1.3;
  ctx.beginPath();
  t.forEach((x, i) => (i ? ctx.lineTo(X(x), Y(v[i])) : ctx.moveTo(X(x), Y(v[i]))));
  ctx.stroke();
}

// ---------------------------------------------------------------- dialogs
function bars(rssi) {
  const n = rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : 1;
  return '▮'.repeat(n) + '▯'.repeat(4 - n);
}

function renderNet() {
  const n = slow && slow.network;
  if (!n) return;
  $('#net-now').textContent = n.how === 'none' ? 'Not watching any network yet: pick one below.'
    : n.how === 'switching' ? `Switching to "${n.name}"…`
    : n.how === '?' ? 'Waiting for the board…'
    : `Watching "${n.name}"${n.how === 'default' ? ' (default)' : ' (chosen here)'}`;
  $('#net-state').textContent = n.state === 'scanning' ? 'scanning all channels (about 4 s)…'
    : n.state === 'done' ? `${n.nets.length} networks nearby` : '';
  const body = $('#net-list');
  body.replaceChildren();
  for (const net of n.nets) {
    const tr = document.createElement('tr');
    if (net.name === n.name) tr.className = 'cur';
    const cells = [net.name, net.ch, `${net.rssi} dBm`];
    cells.forEach((c, i) => { const td = document.createElement('td'); td.textContent = c; tr.append(td); });
    const aps = net.aps || [];
    if (aps.length) {                                // its access points (mesh nodes, extenders)
      const boardCh = slow.details && slow.details.channel ? slow.details.channel[0] : null;
      const box = document.createElement('div');
      box.className = 'aps';
      const head = document.createElement('div');
      head.textContent = aps.length === 1 ? '1 access point' : `${aps.length} access points`;
      box.append(head);
      for (const a of aps) {
        const row = document.createElement('div');
        const used = net.name === n.name && a.ch === boardCh;
        row.textContent = `${a.bssid} · ch ${a.ch} · ${a.rssi} dBm` +
          (net.name === n.name ? (used ? ' · in use' : ' · other channel') : '');
        if (used) row.className = 'used';
        box.append(row);
      }
      tr.children[0].append(box);
    }
    tr.children[2].prepend(Object.assign(document.createElement('span'), { className: 'bars', textContent: bars(net.rssi) + ' ' }));
    const td = document.createElement('td');
    if (net.name !== n.name) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = 'Watch';
      b.onclick = () => {
        if (confirm(`Watch "${net.name}" from now on?\n\nThe board restarts and recalibrates (about 15 s). Only pick your own network.`))
          cmd({ cmd: 'use', name: net.name });
      };
      td.append(b);
    } else td.textContent = 'watching';
    tr.append(td);
    body.append(tr);
  }
}

function renderDev() {
  const d = slow && slow.devices;
  if (!d) return;
  const seg = $('#dev-mode');
  if (!seg.childElementCount) {
    const names = { auto: 'Auto', all: 'All active', manual: 'Manual', off: 'Off' };
    for (const m of d.modes) {
      const b = document.createElement('button'); b.type = 'button'; b.dataset.m = m; b.textContent = names[m] || m;
      b.onclick = () => cmd({ cmd: 'mode', mode: m });
      seg.append(b);
    }
  }
  for (const b of seg.children) b.classList.toggle('on', b.dataset.m === d.mode);
  $('#dev-state').textContent = !d.seen ? 'waiting for the board…'
    : !d.rows.length ? 'no devices heard yet (2.4 GHz only; give it a minute)'
    : `${d.rows.length} heard · poking ${d.poking}/${d.max}`;
  const body = $('#dev-list');
  body.replaceChildren();
  for (const r of d.rows) {
    const tr = document.createElement('tr');
    tr.className = r.poked ? 'poked' : ['gone', 'quiet', 'random'].includes(r.status) ? 'dim' : '';
    const td0 = document.createElement('td');
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = r.poked || r.picked;
    cb.title = 'poke this device'; cb.onchange = () => cmd({ cmd: 'toggle', mac: r.mac });
    td0.append(cb); tr.append(td0);
    const vals = [`${r.name}  ${r.mac}`, r.rssi ? `${r.rssi} dBm` : '–', r.rate, r.heard, r.why, r.link];
    vals.forEach((v, i) => { const td = document.createElement('td'); td.textContent = v; if (i === 4) td.className = 'why'; tr.append(td); });
    body.append(tr);
  }
}

function openNet() {
  $('#dlg-net').showModal();
  renderNet();
  if (!netDlgShown || (slow && slow.network.state === 'idle')) cmd({ cmd: 'scan' });
  netDlgShown = true;
}
function openDev() { $('#dlg-dev').showModal(); renderDev(); }

// ---------------------------------------------------------------- controls
function setWinButtons(w) {
  win = w;
  for (const b of $('#win').children) b.classList.toggle('on', +b.dataset.w === w);
}
function setWin(w) { setWinButtons(w); connect(); }
function togglePause() {
  paused = !paused;
  $('#btn-pause').textContent = paused ? 'Live' : 'Pause';
  $('#btn-pause').classList.toggle('on', paused);
  setLive(paused ? 'paused' : 'live');
  if (!paused) renderFast();
}

$('#win').addEventListener('click', (e) => { if (e.target.dataset.w) setWin(+e.target.dataset.w); });
$('#btn-pause').onclick = togglePause;
$('#btn-cal').onclick = () => cmd({ cmd: 'cal' });
$('#btn-net').onclick = openNet;
$('#btn-dev').onclick = openDev;
$('#btn-help').onclick = () => $('#dlg-help').showModal();
$('#btn-set').onclick = () => { $('#dlg-set').showModal(); renderSound(); renderSens(); };
for (const b of $('#sens-level').children) b.onclick = () => cmd({ cmd: 'sens', x100: +b.dataset.x });
$('#sens-default').onclick = () => cmd({ cmd: 'sens', x100: 'default' });
for (const b of $('#snd-mode').children) b.onclick = () => setSound(b.dataset.m);
for (const id of ['#snd-vol', '#snd-hz']) {
  const el = $(id);
  el.oninput = () => { el.dataset.drag = '1'; showSoundValues(); };
  el.onchange = () => { delete el.dataset.drag; setSound(); };
}
$('#snd-test').onclick = () => cmd({ cmd: 'sound', test: true });
$('#snd-default').onclick = () => cmd({ cmd: 'sound', mode: 'default' });
$('#set-per-link').onchange = (e) => cmd({ cmd: 'settings', per_link: e.target.checked });
$('#net-scan').onclick = () => cmd({ cmd: 'scan' });
$('#net-default').onclick = () => {
  if (confirm('Go back to the network built into the firmware (main/my_network.h)?\n\nThe board restarts (about 15 s).'))
    cmd({ cmd: 'use_default' });
};
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea') || document.querySelector('dialog[open]') || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === ' ') { e.preventDefault(); togglePause(); }
  else if (k === '1' || k === '2' || k === '3') setWin([30, 120, 600][+k - 1]);
  else if (k === 'c') $('#btn-cal').click();
  else if (k === 'n') $('#btn-net').click();
  else if (k === 'v') $('#btn-dev').click();
  else if (k === '?' || k === 'h') $('#dlg-help').showModal();
});
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { renderFast(); drawTimeline(); drawLanes(); }, 80);
});

// ?shot: render one snapshot synchronously (headless screenshots); ?dlg=net|dev|help opens one,
// &hover=<0..1> shows the readout that far across every chart
const params = new URLSearchParams(location.search);
if (params.has('shot')) {
  document.body.classList.add('shot');
  setLive('snapshot');
  const x = new XMLHttpRequest();
  x.open('GET', `/snapshot?win=${+params.get('win') || 30}${params.get('link') ? `&link=${+params.get('link')}` : ''}`, false);
  x.send();
  ({ fast, slow } = JSON.parse(x.responseText));
  if (params.get('win')) setWinButtons(+params.get('win'));
  if (params.get('link')) link = +params.get('link');
  renderFast(); renderSlow();
  const dlg = { net: '#dlg-net', dev: '#dlg-dev', help: '#dlg-help', set: '#dlg-set' }[params.get('dlg')];
  if (dlg) { $(dlg).showModal(); renderNet(); renderDev(); renderSound(); renderSens(); }
  if (params.get('hover')) {                       // &hover=0.6: the readout at 60% across every chart
    for (const [id, h] of Object.entries(hov)) {
      h.x = h.f.r.l + (h.f.r.r - h.f.r.l) * +params.get('hover'); h.y = (h.f.r.t + h.f.r.b) / 2;
      showHover(id);
    }
  }
} else {
  window.addEventListener('load', connect);
}
