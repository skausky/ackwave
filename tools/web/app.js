'use strict';
// ackwave dashboard: one page with hash routes (#/home, #/graphs, …) sharing a
// single live connection: /events (Server-Sent Events: fast, slow and debug
// payloads), commands to /cmd, history from /api/stats. Switching pages only
// shows and hides sections, so no data is dropped.
// Charts are drawn on canvas by hand: labels live in the margins, never on the data.
// Everything from the board or the network (names, networks, labels) is put on
// the page as text (textContent), never as HTML.

const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const C = {
  grid: css('--grid'), line: css('--line'), muted: css('--muted'), fg: css('--fg'),
  motion: css('--motion'), still: css('--still'), brief: css('--brief'), scan: css('--scan'),
  cal: css('--cal'), long: css('--long'), blue: css('--blue'), card: css('--card'), card2: css('--card2'),
};
const FONT = '11px ui-sans-serif, system-ui, sans-serif';
const ROUTES = ['home', 'activity', 'graphs', 'stats', 'links', 'map', 'debug', 'settings'];

let win = 30, paused = false, es = null;
let link = null;                         // per-link focus for breathing / Doppler (null = router)
let fast = null, slow = null, dbg = null, stats = null;   // latest payloads
let page = 'home';
// Charts glide between updates on a display clock that follows the newest data
// time smoothly: it never runs backwards, never passes the newest data, and
// stands still while the server's window does (the first seconds, before the
// window has filled). A big jump (board restart, clock correction) resets it.
const LAG = 0.35;                         // s behind the newest data: the right edge stays filled
const sync = { off: null, goal: 0, last: -Infinity, at: 0 };
function syncClock() {
  const now = performance.now() / 1000, goal = fast.window[1] - now + LAG;
  if (sync.off === null || Math.abs(goal - sync.off) > 2) {   // first data, or a jump: start over
    Object.assign(sync, { off: goal, goal, last: -Infinity, at: now });
  } else sync.goal = goal;
}
function liveWindow() {
  const [w0, w1] = fast.window;
  if (fast.filling) sync.off = null;                   // standing still: start over once it moves
  if (paused || sync.off === null) return [w0, w1];
  const now = performance.now() / 1000;
  sync.off += (sync.goal - sync.off) * Math.min(1, (now - sync.at) / 2);   // corrections spread over ~2 s
  sync.at = now;
  let t1 = Math.min(now + sync.off - LAG, w1 + 0.5);   // never far past the newest data
  t1 = Math.max(t1, sync.last);                        // never backwards
  sync.last = t1;
  return [t1 - (w1 - w0), t1];
}
let netScanned = false, statsTimer = null, tailFilter = '';

// ---------------------------------------------------------------- small DOM helpers
function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  for (const k of kids) if (k !== null && k !== undefined) e.append(k);
  return e;
}
// Fill a <tbody> with rows of plain-text cells
function fillRows(tbody, rows, empty) {
  tbody.replaceChildren();
  if (!rows.length && empty) {
    const td = el('td', { textContent: empty, className: 'muted' });
    td.colSpan = tbody.closest('table').querySelectorAll('th').length || 1;
    tbody.append(el('tr', {}, td));
  }
  for (const r of rows) {
    const tr = el('tr', r.cls ? { className: r.cls } : {});
    for (const c of r.cells || r) tr.append(c instanceof Node ? el('td', {}, c) : el('td', { textContent: String(c) }));
    tbody.append(tr);
  }
}

// ---------------------------------------------------------------- data link
function connect() {
  if (es) es.close();
  es = new EventSource(`/events?win=${win}${link !== null ? `&link=${link}` : ''}`);
  setLive('connecting');
  es.addEventListener('fast', (e) => { fast = JSON.parse(e.data); syncClock(); setLive(paused ? 'paused' : 'live'); renderFast(); });
  es.addEventListener('slow', (e) => { slow = JSON.parse(e.data); renderSlow(); });
  es.addEventListener('debug', (e) => { dbg = JSON.parse(e.data); if (page === 'debug' && !paused) renderDebug(); });
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

async function loadStats() {
  try {
    const r = await fetch('/api/stats', { headers: { 'X-Ackwave': '1' } });
    if (!r.ok) throw new Error(r.status);
    stats = await r.json();
    $('#stats-state').textContent = `${stats.n_sessions} sessions · updated ${clock(Date.now() / 1000, true)}`;
  } catch (e) {
    $('#stats-state').textContent = 'could not load the sessions';
  }
  if (page === 'stats') renderStats();
}

function setLive(state) {
  const e = $('#live');
  e.className = 'live ' + ({ live: 'on', paused: 'paused', offline: 'off' }[state] || '');
  e.querySelector('span').textContent = state;
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

// ---------------------------------------------------------------- header + home
function setBadge(b) {
  const e = $('#badge');
  e.textContent = b.word;
  e.style.color = b.color;
  e.classList.toggle('long', b.word.length > 8);
  $('#badge-sub').textContent = [b.head, b.sub].filter(Boolean).join(' · ');
  document.title = `${b.head || b.word} · ackwave`;
  const w = $('#home-word');
  w.textContent = b.word;
  w.style.color = b.color;
  w.classList.toggle('long', b.word.length > 8);
  $('#home-dot').style.setProperty('--c', b.color);
  $('#hero').dataset.state = b.word.toLowerCase();
  $('#home-head').textContent = b.head || b.sub || '';
  $('#home-since').textContent = b.head ? b.sub || '' : '';
}

// Activity now, against the motion threshold: the bar's middle is the threshold
function renderMeter(a) {
  const fill = $('#meter-fill');
  $('#meter').classList.toggle('off', !a);
  if (!a) { fill.style.width = '0%'; $('#meter-word').textContent = 'waiting for a quiet level'; $('#meter-val').textContent = ''; return; }
  fill.style.width = `${Math.min(a.pct, 200) / 2}%`;
  fill.style.background = a.pct >= 100 ? C.motion : a.pct >= 50 ? C.brief : C.still;
  $('#meter-word').textContent = a.word;
  $('#meter-val').textContent = `${a.pct}% of the way to the threshold, on ${a.by}`;
  $('#meter').title = `fast (0.5 s) window ×${a.short.toFixed(1)} of the quiet level (threshold ×${a.on_s}); ` +
    `slow (4 s) window ×${a.long.toFixed(1)} (threshold ×${a.on_l})`;
}

function renderHeader(h) {
  setBadge(h.badge);
  renderMeter(h.activity);
  const lk = $('#link');
  lk.querySelector('i').style.background = h.link.color;
  lk.querySelector('b').textContent = `link ${h.link.word}`;
  lk.querySelector('span').textContent = h.link.detail;
  const hl = $('#home-link');
  hl.querySelector('i').style.background = h.link.color;
  hl.querySelector('span').textContent = `${{ GOOD: 'Signal good', WEAK: 'Signal weak', POOR: 'Signal poor' }[h.link.word] || h.link.word} · ${h.link.detail}`;
  for (const [k, id] of [['breathing', '#tile-breathing'], ['doppler', '#tile-doppler'], ['quiet', '#tile-quiet']]) {
    const t = h.tiles[k], e = $(id);
    e.querySelector('.value').textContent = t.value;
    e.querySelector('.value').style.color = t.color;
    e.querySelector('.detail').textContent = t.detail;
  }
  renderRecent(h.recent);
  // Breathing on Home: the router link's, and each link's own (per-link analysis)
  const bt = h.tiles.breathing, per = ((slow && slow.radio) || []).filter((r) => r.breathing && r.key !== 'ap:0');
  $('#home-breath-val').textContent = bt.value;
  $('#home-breath-val').style.color = bt.color;
  $('#home-breath-sub').textContent = [`router · ${bt.detail}`,
    ...per.map((r) => `${r.label} ${Math.round(r.breathing.rate)}/min (${r.breathing.level})`)].join(' · ');
  const al = $('#home-alert'), names = h.alerting || [];
  al.hidden = !names.length;
  if (names.length) al.textContent = `Alert: motion near ${names.join(' + ')} (device link${names.length > 1 ? 's' : ''})`;
  for (const id of ['#counts', '#home-counts']) {
    const counts = $(id);
    counts.replaceChildren();
    for (const [n, label] of [[h.counts.motion, 'motion'], [h.counts.brief, 'brief'], [h.counts.scans, 'scans ignored']])
      counts.append(el('span', { className: 'chip' }, el('b', { textContent: n }), ` ${label}`));
  }
  $('#foot-board').textContent = `board says ${h.board}`;
  $('#foot-dev').textContent = h.devices;
  $('#foot-log').textContent = h.log ? `logging to ${h.log}` : '';
  for (const id of ['#btn-cal', '#net-scan', '#net-default']) $(id).disabled = !h.board_connected;
  renderLight(h.light);
}

// ---------------------------------------------------------------- light on motion
function lightLast(l) {
  if (!l || !l.last) return '';
  const when = clock(l.last.t, true);
  return `last call: "${l.last.what}" at ${when} · ${l.last.ok ? 'OK' : 'failed'} (${l.last.msg})`;
}
function renderLight(l) {
  if (!l) return;
  const hl = $('#home-light');
  hl.hidden = !l.enabled;
  if (l.enabled) {
    const fail = l.last && !l.last.ok;
    hl.textContent = l.live === false ? 'Light: waits for the board (no board, no switching)'
      : `Light: ${l.busy ? 'switching…' : l.state}` + (fail ? ` · last call failed (${l.last.msg})` : '');
    hl.style.color = fail ? 'var(--motion)' : '';
  }
  if (page !== 'settings') return;
  if (document.activeElement !== $('#light-on')) $('#light-on').checked = l.enabled;
  for (const [id, host] of [['#light-on-url', l.on_host], ['#light-off-url', l.off_host]]) {
    const e = $(id);
    e.dataset.saved = host ? `saved · goes to ${host} (paste a new one to replace it)` : '';
    if (!e.value) e.placeholder = e.dataset.saved || e.dataset.hint || e.placeholder;
  }
  const sel = $('#light-after');
  if (document.activeElement !== sel) {
    if (![...sel.options].some((o) => +o.value === l.off_after))
      sel.append(el('option', { value: l.off_after, textContent: `${l.off_after} s` }));
    sel.value = String(l.off_after);
  }
  $('#light-state').textContent = !l.enabled ? '' : l.live === false
    ? '· acts only while the board is connected (tests still work)' : `· light is ${l.state}`;
  $('#light-test-on').disabled = !l.on_host;
  $('#light-test-off').disabled = !l.off_host;
  const last = $('#light-last');
  last.textContent = lightLast(l);
  last.classList.toggle('err', !!(l.last && !l.last.ok));
}
for (const id of ['#light-on-url', '#light-off-url']) $(id).dataset.hint = $(id).placeholder;
$('#light-on').onchange = async (e) => {
  const body = { cmd: 'light', enabled: e.target.checked };
  const on = $('#light-on-url').value.trim(), off = $('#light-off-url').value.trim();
  if (on) body.on_url = on;
  if (off) body.off_url = off;
  if (!(await cmd(body))) e.target.checked = !e.target.checked;
  else { $('#light-on-url').value = ''; $('#light-off-url').value = ''; }
};
$('#light-save').onclick = async () => {
  const body = { cmd: 'light', off_after: +$('#light-after').value };
  const on = $('#light-on-url').value.trim(), off = $('#light-off-url').value.trim();
  if (on) body.on_url = on;
  if (off) body.off_url = off;
  if (await cmd(body)) { $('#light-on-url').value = ''; $('#light-off-url').value = ''; }
};
$('#light-after').onchange = (e) => cmd({ cmd: 'light', off_after: +e.target.value });
$('#light-test-on').onclick = () => cmd({ cmd: 'light_test', what: 'on' });
$('#light-test-off').onclick = () => cmd({ cmd: 'light_test', what: 'off' });
$('#light-clear').onclick = async () => {
  if (await cmd({ cmd: 'light', enabled: false, on_url: '', off_url: '' }))
    for (const id of ['#light-on-url', '#light-off-url']) { $(id).dataset.saved = ''; $(id).placeholder = $(id).dataset.hint; }
};

// Recent motion as short sentences: when, what, how long, where
function renderRecent(rows) {
  const ol = $('#recent');
  ol.replaceChildren();
  if (!rows.length) ol.append(el('li', { className: 'muted empty', textContent: 'No motion yet. Walk between the board and the router to see it appear here.' }));
  for (const r of rows) {
    const cls = r.only ? 'only' : r.going ? 'going' : r.brief ? 'brief' : '';
    const li = el('li', { className: cls });
    li.dataset.strength = r.strength || (r.brief ? 'blip' : '');
    const ago = Math.max(0, (fast ? (fast.header.now || Date.now() / 1000) : Date.now() / 1000) - r.t);
    const what = r.going ? `${r.what}, still going` : r.what;
    const dur = r.going ? r.dur.replace('now ', '') : r.brief ? '' : r.dur;
    li.append(el('span', { className: 'when', textContent: r.at.slice(0, 5) }),
      el('span', { className: 'pip' }),
      el('span', { className: 'what' }, el('b', { textContent: what }),
        el('span', { className: 'muted', textContent: [dur, r.where && r.where !== 'router' ? r.where : ''].filter(Boolean).join(' · ') })),
      el('span', { className: 'ago muted', textContent: r.going ? 'now' : `${fmtAgo(ago)} ago` }));
    if (r.peak !== null) li.title = `peak ×${r.peak.toFixed(1)} of the quiet level (motion threshold ×2.8)`;
    if (r.only) li.title = "Seen by a device link only: the board's own verdict stayed still";
    ol.append(li);
  }
}
function fmtAgo(s) {
  s = Math.round(s);
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min`;
}

// Other ackwave boards: their own verdicts, from the hellos the board on USB hears
function renderBoards() {
  const bs = (slow && slow.boards) || [];
  $('#boards-card').hidden = !bs.length;
  if (!bs.length) return;
  const n = bs.filter((b) => b.state === 'motion').length;
  $('#boards-state').textContent = `${bs.length} heard${n ? ` · ${n} see${n > 1 ? '' : 's'} motion` : ''}`;
  const WORD = { motion: 'Motion', still: 'Still', calibrating: 'Calibrating', gone: 'Not heard', unpaired: 'Needs its key' };
  const nowS = Date.now() / 1000;
  $('#boards').replaceChildren(...bs.map((b) => {
    const pct = Math.max(0, Math.min(100, b.level * 50));            // 1.0 = threshold = middle
    const sub = b.state === 'unpaired' ? 'plug it into this PC once while the dashboard runs: it gets its key, then its reports show here'
      : b.state === 'gone' ? `last heard ${fmtSpan(b.heard_s)} ago`
      : b.state === 'calibrating' ? 'measuring the quiet level'
      : `${b.state === 'motion' ? 'moving' : 'still'} for ${fmtSpan(nowS - b.since)}`;
    const ren = el('button', { type: 'button', className: 'ghost small', textContent: b.nick ? 'Rename' : 'Name it',
      onclick: () => {
        const name = prompt(`A name for ${b.name} (${b.mac}), e.g. its room. Leave empty for "Board ${b.mac.slice(-5)}".`, b.nick);
        if (name !== null) cmd({ cmd: 'board_name', mac: b.mac, name });
      } });
    return el('div', { className: `board-tile ${b.state}` },
      el('div', { className: 'name' }, el('span', {}, el('i', { className: 'dot' }), el('b', { textContent: b.name })),
        el('span', { className: 'word', textContent: WORD[b.state] || b.state })),
      el('div', { className: 'muted small', textContent: sub }),
      el('div', { className: 'bt-meter', title: 'How close its link is to the motion threshold (the mark)' },
        el('div', { className: 'bt-fill', style: `width:${pct}%` })),
      b.state === 'unpaired' ? null : el('div', { className: 'muted small', textContent:
        `router ${b.acks}/s · ${b.router_rssi} dBm${b.weak ? ' · weak link' : ''} · ${b.links} other link${b.links === 1 ? '' : 's'} · ${b.via === 'usb' ? 'on USB' : `heard at ${b.rssi} dBm, sealed`}` }),
      ren);
  }));
}

function renderHome() {
  renderBoards();
  // Where now: the links that see motion (with per-link analysis on)
  const mv = ((slow && slow.radio) || []).filter((r) => r.moving).map((r) => r.label);
  const wn = $('#home-where');
  wn.hidden = !mv.length;
  if (mv.length) wn.textContent = `Seen by ${mv.join(' · ')}`;
  const t = slow && slow.today;
  $('#home-n').textContent = t ? t.periods : '–';
  $('#home-min').textContent = t ? (t.motion_s / 60).toFixed(t.motion_s < 600 ? 1 : 0) : '–';
  $('#home-blips').textContent = t ? t.blips : '–';
}

// ---------------------------------------------------------------- where and when
// The timeline from /api/timeline: one lane per link over the last hours,
// and the same periods as a list (merged across links: one motion, the links
// that saw it). Loaded on Home every 20 s and when the range changes.
const where = { hours: 6, data: null, sel: null, timer: null };
let whereHours = 6;
try { whereHours = +localStorage.getItem('ackwave.where') || 6; } catch (e) { /* private mode */ }
const WHERE_HOURS = [0.008333, 0.083333, 0.5, 1, 6, 24, 168];   // 30 s, 5 min, 30 min, 1 h, 6 h, 24 h, 7 days
where.hours = WHERE_HOURS.includes(whereHours) ? whereHours : 6;
async function loadWhere() {
  try {
    const r = await fetch(`/api/timeline?hours=${where.hours}`, { headers: { 'X-Ackwave': '1' } });
    if (!r.ok) throw new Error(r.status);
    where.data = await r.json();
    $('#where-state').textContent = `updated ${clock(Date.now() / 1000)}`;
  } catch (e) {
    $('#where-state').textContent = 'could not load';
  }
  if (page === 'home') { drawWhere(); renderWhereList(); renderActList(); }
}
function whereRange(h) {
  where.hours = h; where.sel = null;
  try { localStorage.setItem('ackwave.where', String(h)); } catch (e) { /* private mode */ }
  for (const b of $('#where-range').children) b.classList.toggle('on', +b.dataset.h === h);
  loadWhere();
  clearInterval(where.timer);                       // short ranges move quickly: refresh them more often
  if (page === 'home') where.timer = setInterval(loadWhere, h < 0.1 ? 2000 : h <= 1 ? 5000 : 20000);
}
for (const b of $('#where-range').children) b.onclick = () => whereRange(+b.dataset.h);
function whereEvents() {
  // all lanes' periods merged into motions (gaps under 10 s join), newest first
  const d = where.data;
  if (!d) return [];
  const name = Object.fromEntries(d.lanes.map((l) => [l.key, l.name]));
  const ps = d.spans.slice().sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const [a, b, key, going] of ps) {
    if (key === 'activity') continue;              // activities have their own list
    const e = out[out.length - 1];
    if (e && a <= e.t1 + 10) {
      e.t1 = Math.max(e.t1, b); e.keys.add(key); e.going = e.going || going;
    } else out.push({ t0: a, t1: b, keys: new Set([key]), going });
  }
  for (const e of out) e.names = [...e.keys].sort((x, y) => (x === 'board' ? -1 : y === 'board' ? 1 : 0)).map((k) => name[k] || k);
  return out.reverse();
}
function fmtSpan(s) {               // a motion's length: '45 s', '3 min 20 s', '1 h 5 min'
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min${s % 60 && s < 600 ? ` ${s % 60} s` : ''}`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}
function dayClock(t) {                     // "14:32", or "Tue 14:32" when not today
  const z = zp(t), now = zp(Date.now() / 1000);
  return z.y === now.y && z.mo === now.mo && z.d === now.d ? clock(t) : `${DAY[z.wd]} ${clock(t)}`;
}
function renderWhereList() {
  const ev = whereEvents();
  $('#where-hint').hidden = !where.data || where.data.per_link;
  fillList($('#where-list'), ev.slice(0, 60).map((e) => {
    const li = el('li', { className: (e.going ? 'going' : '') + (where.sel === e.t0 ? ' on' : '') },
      el('b', { textContent: dayClock(e.t0) }),
      el('span', { textContent: e.going ? `${fmtSpan(e.t1 - e.t0)}, now` : fmtSpan(e.t1 - e.t0) }),
      el('span', { className: 'who', textContent: e.names.join(' · ') }));
    li.onclick = () => { where.sel = where.sel === e.t0 ? null : e.t0; drawWhere(); renderWhereList(); };
    return li;
  }), where.data ? 'no motion in this time' : 'loading…');
}
function renderActList() {
  const d = where.data;
  const acts = d ? d.spans.filter((p) => p[2] === 'activity').sort((x, y) => y[0] - x[0]) : [];
  fillList($('#home-acts'), acts.slice(0, 60).map(([a, b, , going, name]) =>
    el('li', { className: going ? 'going' : '' }, el('b', { textContent: dayClock(a) }),
      el('span', { textContent: going ? `${fmtSpan(b - a)}, now` : fmtSpan(b - a) }),
      el('span', { className: 'who', textContent: name }))),
  !d ? 'loading…' : 'none in this time (record activities on the Activity page)');
}
function drawWhere() {
  const d = where.data, box = $('#where'), cv = box.querySelector('canvas');
  if (!d) return;
  const dpr = window.devicePixelRatio || 1;
  const lanes = d.lanes, LH = 22, AX = 18, w = box.clientWidth, h = lanes.length * LH + AX;
  const G = Math.min(150, Math.max(84, w * 0.2));             // label gutter
  cv.style.height = `${h}px`;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const t0 = d.t0, t1 = d.t1, X = (t) => G + (t - t0) / (t1 - t0) * (w - G);
  const row = Object.fromEntries(lanes.map((l, i) => [l.key, i]));
  const sel = where.sel !== null ? whereEvents().find((e) => e.t0 === where.sel) : null;
  if (sel) {                                                  // the period picked in the list
    ctx.fillStyle = C.blue; ctx.globalAlpha = .25;
    const xa = Math.max(G, X(sel.t0) - 3), xb = Math.min(w, X(sel.t1) + 3);
    ctx.fillRect(xa, 0, Math.max(6, xb - xa), lanes.length * LH);
    ctx.globalAlpha = 1;
  }
  ctx.font = '12px ui-sans-serif, system-ui, sans-serif';
  ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  lanes.forEach((ln, i) => {
    const y = i * LH;
    ctx.fillStyle = C.grid; ctx.fillRect(G, y + 4, w - G, LH - 8);
    ctx.fillStyle = ln.key === 'board' ? C.fg : C.muted;
    let label = ln.name;
    while (label.length > 3 && ctx.measureText(label).width > G - 10) label = label.slice(0, -2) + '…';
    ctx.fillText(label, 0, y + LH / 2);
  });
  for (const [a, b, key] of d.spans) {
    const i = row[key];
    if (i === undefined) continue;
    const xa = Math.max(G, X(a)), xb = Math.min(w, X(b));
    if (xb < G || xa > w) continue;
    ctx.fillStyle = key === 'board' ? C.motion : key === 'activity' ? C.blue : C.brief;
    ctx.fillRect(xa, i * LH + 4, Math.max(2, xb - xa), LH - 8);   // at least 2 px: a short one still shows
  }
  ctx.font = '11px ui-sans-serif, system-ui, sans-serif'; ctx.fillStyle = C.bg || '#0d1116';
  for (const [a, b, key, , name] of d.spans) {    // an activity's name, where it fits
    if (key !== 'activity' || row[key] === undefined) continue;
    const xa = Math.max(G, X(a)), xb = Math.min(w, X(b));
    if (xb - xa > ctx.measureText(name).width + 8) ctx.fillText(name, xa + 4, row[key] * LH + LH / 2 + 1);
  }
  ctx.font = FONT; ctx.fillStyle = C.muted; ctx.textBaseline = 'top'; ctx.textAlign = 'center';
  const { ticks, step } = timeTicks(t0, t1, w - G);
  for (const t of ticks) {
    const x = X(t), label = step >= 86400 ? dayClock(t) : clock(t, step < 60), tw = ctx.measureText(label).width;
    if (x - tw / 2 >= G && x + tw / 2 <= w) ctx.fillText(label, x, lanes.length * LH + 4);
  }
  ctx.textAlign = 'left';
  hoverable('#where', { r: { l: G, r: w, t: 0, b: lanes.length * LH }, o: { t0, t1, y0: 0, y1: 1 } }, (t) => {
    const tol = (t1 - t0) / (w - G) * 3;                      // 3 px either side: easy to hit with a finger
    const saw = lanes.filter((ln) => ln.key !== 'activity' && d.spans.some(([a, b, k]) => k === ln.key && a - tol <= t && t <= b + tol));
    const act = d.spans.find(([a, b, k]) => k === 'activity' && a <= t && t <= b);
    return [saw.length ? `motion: ${saw.map((l) => l.name).join(', ')}` : 'no motion', act && `activity: ${act[4]}`];
  });
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
// Times are shown in the PC's time zone (the server says which), whatever
// device views the page; the browser's own zone until it is known.
let tzFmt = null, tzName = null, tzMemo = [NaN, null];
function setTimeZone(name) {
  if (name === tzName) return;
  tzName = name; tzMemo = [NaN, null];
  try {
    tzFmt = new Intl.DateTimeFormat('en-US', { timeZone: name || undefined, hourCycle: 'h23', weekday: 'short',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (e) { tzFmt = null; }                    // an unknown zone: the browser's
}
setTimeZone(null);
const WD = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
function zp(t) {                                   // {y, mo, d, h, mi, s, wd} of wall time t in the PC's zone
  const k = Math.floor(t);
  if (tzMemo[0] === k) return tzMemo[1];
  const o = {};
  if (tzFmt) for (const { type, value } of tzFmt.formatToParts(new Date(k * 1000))) o[type] = value;
  const d = new Date(k * 1000);
  const z = tzFmt ? { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour % 24, mi: +o.minute, s: +o.second, wd: WD[o.weekday] }
    : { y: d.getFullYear(), mo: d.getMonth() + 1, d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), s: d.getSeconds(), wd: d.getDay() };
  tzMemo = [k, z];
  return z;
}
function clock(t, withSec) {
  const z = zp(t), p = (n) => String(n).padStart(2, '0');
  return withSec ? `${p(z.h)}:${p(z.mi)}:${p(z.s)}` : `${p(z.h)}:${p(z.mi)}`;
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
    // A mouse leaving hides the readout; a finger lifting keeps it (tap elsewhere hides it)
    box.addEventListener('pointerleave', (e) => { if (e.pointerType !== 'touch') hide(); });
    box.addEventListener('pointercancel', hide);    // the page started scrolling instead
    document.addEventListener('pointerdown', (e) => { if (!box.contains(e.target)) hide(); });
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
function drawScore(id = '#score', compact = false) {
  const { ctx, w, h } = canvasFor(id);
  const msg = $(`${id}-msg`);
  if (!fast || !fast.score) { msg.textContent = 'waiting for data…'; return; }
  const s = fast.score, [t0, t1] = liveWindow();
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
    { v: s.on_s, text: `fast ×${s.on_s.toFixed(1)}`, color: C.motion },
    { v: s.on_l, text: `slow ×${s.on_l.toFixed(1)}`, color: C.long },
    { v: 1, text: 'quiet', color: C.muted },
  ]);
  msg.textContent = s.calibrating && compact ? 'calibrating: keep still' : '';
  if (!compact) {
    const st = $('#score-state');                  // status lives in the header, never on the data
    st.hidden = !s.calibrating;
    st.textContent = 'calibrating · keep still';
    renderLegend(s);
  }
  hoverable(id, f, (t) => {
    const tol = Math.max(s.gap, (t1 - t0) / 150), out = [];
    for (const se of s.series) {
      const i = nearest(t, se.t, tol);
      if (i >= 0) out.push(`${se.label} ×${se.v[i].toFixed(1)}`);
    }
    const i = s.long ? nearest(t, s.long.t, tol) : -1;
    if (i >= 0) out.push(`slow window ×${s.long.v[i].toFixed(1)}`);
    if (!out.length) out.push('no data');
    else out.push(`(motion at ×${s.on_s.toFixed(1)} fast or ×${s.on_l.toFixed(1)} slow)`);
    out.push(inSpan(s.motion, t) && 'motion', inSpan(s.scans, t) && 'router scan (ignored)', inSpan(s.cal, t) && 'calibrating');
    return out;
  });
}

let legendKey = '';
function renderLegend(s) {
  const items = s.series.map((se) => [se.label, se.color, se.dash]);
  items.push(['slow window (4 s)', C.long, 'solid-thick']);
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
  const [t0, t1] = liveWindow(), d = slow && slow.doppler;
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
  const [t0, t1] = liveWindow();
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

function drawTimeline(id = '#timeline', compact = false) {
  const { ctx, w, h } = canvasFor(id);
  const tl = slow && slow.timeline;
  if (!tl) return;
  const ymax = tl.on_s * 1.8;
  const f = frame(ctx, w, h, { t0: tl.start, t1: tl.end, y0: 0, y1: ymax, yticks: compact ? [] : niceTicks(ymax, 3), left: compact ? 8 : 34, right: compact ? 8 : 12 });
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
  if (tl.t && !compact) {
    const gap = Math.max(5, (tl.end - tl.start) / 300);
    ctx.strokeStyle = C.blue; ctx.lineWidth = .9; ctx.globalAlpha = .85;
    polyline(ctx, f, tl.t, tl.s, gap, ymax * .98);
    ctx.globalAlpha = 1; ctx.strokeStyle = C.long; ctx.lineWidth = 1.4;
    polyline(ctx, f, tl.t, tl.l, gap, ymax * .98);
  }
  ctx.restore();
  hoverable(id, f, (t) => {
    const i = tl.t ? nearest(t, tl.t, Math.max(3, (tl.end - tl.start) / 200)) : -1;
    const m = tl.motion.find(([a, b]) => a - 1 <= t && t <= b + 1);
    return [i >= 0 ? `fast ×${tl.s[i].toFixed(1)} · slow ×${tl.l[i].toFixed(1)}` : 'no data',
      m && (m[2] ? 'brief blip' : 'motion'), inSpan(tl.cal, t) && 'calibrating'];
  });
}

// ---------------------------------------------------------------- render loops
// Only the page on screen is drawn (hidden canvases have no size); switching
// pages draws the new one from the latest data at once.
function renderFast() {
  if (!fast) return;
  renderHeader(fast.header);
  renderActHome();
  if (page === 'activity') renderActNow();
  if (page === 'map' || page === 'home') window.AckMap.update(null, fast.header);   // the board's verdict pulses the router link
  if (!paused) drawLive(true);
}

// The moving charts, about 30 times a second while their page is on screen
let lastFrame = 0, lastDop = 0;
function drawLive(force = false) {
  if (!fast || document.hidden) return;
  const now = performance.now();
  if (!force && now - lastFrame < 33) return;
  lastFrame = now;
  if (page === 'home') drawScore('#home-live', true);
  else if (page === 'graphs') {
    drawScore(); drawBreath();
    if (force || now - lastDop > 100) { drawDoppler(); lastDop = now; }   // the heavy one: 10 a second
  } else if (page === 'links' && slow && slow.links) drawLanes();
}
function frameLoop() {
  if (!paused) drawLive();
  requestAnimationFrame(frameLoop);
}

function renderSlow() {
  if (!slow) return;
  setTimeZone(slow.tz || null);
  const n = slow.network;
  $('#net').textContent = n && n.ap_list && n.ap_list.length ? `${n.ap_list.length} chosen access point${n.ap_list.length > 1 ? 's' : ''}`
    : n && n.name ? `${n.name}${n.how === 'default' ? '' : n.how === 'chosen' ? ' · chosen' : ''}` : '';
  if (page === 'home') { renderHome(); drawTimeline('#home-tl', true); drawWhere(); window.AckMap.update(slow, fast && fast.header); }
  else if (page === 'graphs') drawTimeline();
  else if (page === 'links') { renderLinks(); renderRates(); renderGates(); renderRouters(); renderDev(); renderAlerts(); }
  else if (page === 'map') window.AckMap.update(slow, fast && fast.header);
  else if (page === 'debug') renderRaw();
  else if (page === 'activity') renderActCal();
  else if (page === 'settings') { renderNet(); renderSound(); renderSens(); renderThresholds(); renderLed(); }
  if (page !== 'links') syncLinkFocus();
}

function renderPage() {
  renderFast();
  renderSlow();
  if (page === 'debug') renderDebug();
  if (page === 'stats') renderStats();
}

function renderRaw() {
  const d = slow.details, lines = [];
  if (d.channel) lines.push(`channel ${d.channel[0]} (${d.channel[1] || 'start'})   calibration ${d.phase} (${d.why})   quiet ${d.floor ? d.floor.toFixed(4) : '–'}`);
  if (d.stats) {
    const st = d.stats, ratio = st.poke ? Math.round(100 * st.ack / st.poke) : 0;
    lines.push(`pokes ${st.poke}   ACK CSI ${st.ack} (${ratio}%)   tx errors ${st.tx_err}   queue drops ${st.qfull}   lines lost on USB ${st.lost ?? 0}   router replies ${st.reply ?? 0}`);
  }
  $('#raw-text').textContent = lines.join('\n') || 'waiting…';
}

// ---------------------------------------------------------------- buzzer / detection
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
// The board's LED: what it reported (saved on the board); controls being used aren't touched
function renderLed() {
  const l = slow && slow.led;
  $('#led-state').textContent = l ? 'saved on the board' : 'waiting for the board (firmware too old?)';
  $('#led-card').classList.toggle('off', !!l && l.mode === 'off');
  if (!l) return;
  for (const b of $('#led-mode').children) b.classList.toggle('on', b.dataset.m === l.mode);
  for (const b of $('#led-pattern').children) b.classList.toggle('on', b.dataset.p === l.pattern);
  if (!$('#led-bright').dataset.drag) $('#led-bright').value = l.bright;
  if (document.activeElement !== $('#led-color')) $('#led-color').value = l.color;
  showLedValues();
}
function showLedValues() {
  $('#led-bright-out').textContent = `${Math.round($('#led-bright').value / 2.55)}%`;
  $('#led-color-out').textContent = $('#led-color').value;
}
function setLed(change) {
  const l = (slow && slow.led) || { mode: 'status', pattern: 'strength', bright: 40, color: '#ff0000' };
  cmd(Object.assign({ cmd: 'led', mode: l.mode, pattern: l.pattern, bright: +$('#led-bright').value || l.bright,
    color: $('#led-color').value || l.color }, change));
}

function renderSens() {
  const board = slow && slow.sens, want = slow && slow.settings && slow.settings.sens;
  const cur = want || board;
  $('#sens-state').textContent = !board ? 'waiting for the board (firmware too old?)'
    : `×${(board / 100).toFixed(1)} on the board · ${want ? 'kept by this dashboard' : "the firmware's"}`;
  for (const b of $('#sens-level').children) b.classList.toggle('on', +b.dataset.x === cur);
}
function renderThresholds() {
  const t = slow && slow.thresholds;
  if (!t) return;
  const rows = [
    ['Fast window (0.5 s) starts motion at', `×${t.on_short.toFixed(1)}`, 'firmware'],
    ['…and ends it under', `×${t.off_short.toFixed(1)}`, 'firmware'],
    ['…but only while the slow window is over', `×${t.short_gate.toFixed(1)}`, 'firmware'],
    ['Slow window (4 s) starts motion at', `×${t.on_long.toFixed(1)}`, 'Sensitivity above'],
    ['…and ends it under', `×${t.off_long.toFixed(1)}`, 'follows Sensitivity'],
    ['Quiet level now', t.floor > 0 ? `${t.floor.toFixed(4)} (${t.phase})` : 'measuring', 'measured'],
  ];
  fillRows($('#thresholds'), rows.map(([k, v, how]) => ({ cells: [k, v, how] })));
}
function showSoundValues() {
  $('#snd-vol-out').textContent = `${$('#snd-vol').value * 2}%`;
  $('#snd-hz-out').textContent = `${$('#snd-hz').value} Hz`;
}
function setSound(mode) {
  const on = $('#snd-mode .on');
  cmd({ cmd: 'sound', mode: mode || (on ? on.dataset.m : 'beep'), vol: +$('#snd-vol').value, hz: +$('#snd-hz').value });
}

// ---------------------------------------------------------------- links
function syncLinkFocus() {
  const on = !!(slow && slow.settings && slow.settings.per_link);
  if (!on && link !== null) { link = null; connect(); }
  const rows = on ? (slow.links || []) : [];
  const cur = rows.find((r) => r.src === link);
  for (const e of document.querySelectorAll('.which')) e.textContent = cur ? cur.label : 'router';
  for (const e of document.querySelectorAll('.which-tile')) e.textContent = cur ? `· ${cur.label}` : '';
}

function linkCard(src) {
  const name = el('div', { className: 'name' }, el('span'), el('small'));
  const top = el('div', {}, el('span', { className: 'act' }), el('span', { className: 'state' }));
  const spark = el('div', { className: 'spark' }, el('canvas'));
  const meta = el('div', { className: 'meta' }, el('span', { className: 'br' }), el('span', { className: 'dop' }));
  const b = el('button', { type: 'button', className: 'link' }, name, top, spark, meta);
  b.dataset.src = src;
  b.onclick = () => { link = link === src ? null : src; connect(); renderLinks(); };
  return b;
}

function renderLinks() {
  const on = !!(slow.settings && slow.settings.per_link);
  $('#set-per-link').checked = on;
  $('#links-off').hidden = on;
  syncLinkFocus();
  const box = $('#links');
  const rows = on ? slow.links || [] : [];
  if (!on) { box.replaceChildren(); $('#lanes').hidden = $('#lanes-legend').hidden = true; return; }
  const seen = new Set();
  for (const r of rows) {                        // elements (and canvases) kept between updates
    seen.add(String(r.src));
    let e = box.querySelector(`[data-src="${r.src}"]`);
    if (!e) { e = linkCard(r.src); box.append(e); }
    e.classList.toggle('sel', link === r.src);
    e.querySelector('.name span').textContent = r.label + (r.ap ? '' : ' · device');
    e.querySelector('.name small').textContent = `${r.rate}/s ${r.rssi} dBm`;
    const act = e.querySelector('.act'), st = e.querySelector('.state');
    const judged = r.state === 'motion' || r.state === 'still';
    e.classList.toggle('dim', r.state === 'weak' || r.state === 'silent');
    act.textContent = judged ? `×${r.activity.toFixed(1)}` : '—';
    act.title = r.long === null ? '' : `fast (0.5 s) ×${r.activity.toFixed(2)} · slow (4 s) ×${r.long.toFixed(2)} of its quiet level`;
    act.style.color = r.moving ? C.motion : judged && r.activity > 1.6 ? C.brief : judged ? C.fg : C.muted;
    st.textContent = { motion: 'MOTION', still: 'still', calibrating: 'calibrating', weak: 'weak link', silent: 'no signal' }[r.state] || r.state;
    st.title = { weak: 'Too few ACKs a second to judge motion on this link (it needs about 20)', silent: 'Nothing from this device for over 2 s' }[r.state] || '';
    st.className = 'state' + (r.state === 'motion' ? ' motion' : judged ? '' : ' cal');
    const b = r.breathing, d = r.doppler;
    e.querySelector('.br').textContent = 'breathing ' + (!b ? '…' : b.level === 'none' ? '—' : `${b.rate.toFixed(0)}/min (${b.level})`);
    e.querySelector('.dop').textContent = 'Doppler ' + (d === null || d === undefined ? '…' : !Object.keys(d).length ? '—' : d.scan ? 'scan' : `${d.f.toFixed(1)} Hz`);
    drawSpark(e.querySelector('.spark'), r);
  }
  for (const n of [...box.childNodes]) if (!n.dataset || !seen.has(n.dataset.src)) n.remove();
  if (!rows.length) box.textContent = 'collecting: each link needs about a minute of data';
  drawLanes();
}

// Device links that drive the alerts besides the board (ticked here, saved on the PC)
function renderAlerts() {
  const keys = (slow.settings && slow.settings.alerts) || [], perLink = !!(slow.settings && slow.settings.per_link);
  const live = (slow.radio || []).filter((r) => !r.ap);
  const rows = live.map((r) => ({ key: r.key, label: r.label, sub: `${r.rssi} dBm · ${r.rate}/s`, r }))
    .concat(keys.filter((k) => !live.some((r) => r.key === k)).map((k) => ({ key: k, label: k, sub: 'not heard now' })));
  $('#alerts-state').textContent = !perLink ? 'switch on per-link analysis above for these to count'
    : keys.length ? `the board + ${keys.length} device link${keys.length > 1 ? 's' : ''}` : 'the board only';
  const box = $('#alert-list');
  box.replaceChildren();
  if (!rows.length) box.append(el('p', { className: 'muted small', textContent: 'No device links yet: poke a device (below) to add one.' }));
  for (const row of rows) {
    const cb = el('input', { type: 'checkbox', checked: keys.includes(row.key) });
    cb.onchange = () => {
      const next = new Set(keys);
      if (cb.checked) next.add(row.key); else next.delete(row.key);
      cmd({ cmd: 'alerts', keys: [...next] });
    };
    const state = row.r && row.r.alerting ? el('span', { className: 'chip motion', textContent: 'alerting' })
      : row.r && row.r.state ? el('span', { className: 'chip', textContent: row.r.state }) : null;
    box.append(el('label', { className: 'alert-row' }, cb, el('b', { textContent: row.label }),
      el('span', { className: 'muted small', textContent: row.sub }), state));
  }
}

function renderRates() {
  const d = slow.details;
  fillRows($('#rates'), (d.streams || []).map((s) => [s.label, `${s.rate.toFixed(0)}/s`, `${s.rssi} dBm`]), 'nothing heard yet');
}
function renderGates() {
  const why = { ok: 'poked', weak: 'weak: answers rarely', silent: 'silent: no answers', far: 'far: much weaker than the best' };
  fillRows($('#gates'), (slow.gates || []).map((g) => ({
    cls: g.state === 'ok' ? '' : 'dim',
    cells: [`${g.label}${g.ap ? '' : ' · device'}`, `${g.rssi} dBm`, g.answered < 0 ? '–' : `${g.answered}%`, g.sent, why[g.state] || g.state],
  })), 'no GATE lines yet (the board sends them every few seconds)');
}

// Motion by link: one lane per link (plus the board's own verdict) over the
// window shown on Graphs. Names sit in a left gutter, never over the lanes.
function drawLanes() {
  const box = $('#lanes');
  const rows = (slow && slow.links) || [];
  const w0 = fast && fast.window && liveWindow();
  box.hidden = $('#lanes-legend').hidden = !rows.length || !w0;
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
  const [t0, t1] = w0, X = (t) => G + (t - t0) / (t1 - t0) * (w - G);
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

// ---------------------------------------------------------------- stats
const DAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
function dayLabel(iso) {
  const d = new Date(`${iso}T12:00:00`);
  return `${DAY[d.getDay()]} ${d.getDate()}`;
}
function fmtMin(s) { return s < 60 ? `${Math.round(s)} s` : `${(s / 60).toFixed(s < 600 ? 1 : 0)} min`; }
function fmtLen(s) {
  s = Math.round(s);
  return s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')} min`;
}
// Sequential scale: one hue (the motion red), from the card colour up
function rgb(hex) { const n = parseInt(hex.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
function seq(x) {
  const a = rgb(C.card2), b = rgb(C.motion), f = Math.pow(Math.max(0, Math.min(1, x)), 0.6);
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * f)).join(',')})`;
}
function hatch(ctx, x, y, w, h) {
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
  for (let k = -h; k < w; k += 5) { ctx.beginPath(); ctx.moveTo(x + k, y + h); ctx.lineTo(x + k + h, y); ctx.stroke(); }
  ctx.restore();
}
// A bar with a 4px rounded data end, anchored to the baseline
function bar(ctx, x, y, w, h) {
  if (h <= 0) return;
  const r = Math.min(4, w / 2, h);
  ctx.beginPath();
  ctx.moveTo(x, y + h); ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r); ctx.lineTo(x + w, y + h);
  ctx.closePath(); ctx.fill();
}

function renderStats() {
  drawHeat(); drawHours(); drawDurs();
  if (!stats) return;
  fillRows($('#sess-list'), stats.sessions.map((s) => ({
    cls: s.live ? 'cur' : '',
    cells: [`${DAY[zp(s.start).wd]} ${zp(s.start).d} ${clock(s.start)}`, fmtLen(s.end - s.start),
      s.periods, fmtMin(s.motion_s), s.blips,
      s.live ? 'this session' : s.data_stopped ? `${s.name} · data stopped ${clock(s.data_stopped)}` : s.name],
  })), 'no sessions yet');
}

function drawHeat() {
  const { ctx, w } = canvasFor('#heat');
  const msg = $('#heat-msg');
  if (!stats) { msg.textContent = 'loading…'; return; }
  const hm = stats.heatmap, n = hm.days.length;
  if (!n) { msg.textContent = 'no sessions recorded yet'; return; }
  msg.textContent = '';
  const box = $('#heat'), CH = Math.max(14, Math.min(22, 320 / n)), G = 54, T = 4, B = 18;
  box.style.height = `${T + n * CH + B}px`;
  const again = canvasFor('#heat');                // resized: draw on the new size
  const c2 = again.ctx, cw = (again.w - G - 4) / 24;
  let max = 60;
  for (const row of hm.motion_s) for (const v of row) max = Math.max(max, v);
  const cells = [];
  hm.days.forEach((d, i) => {
    const y = T + i * CH;
    c2.fillStyle = C.muted; c2.font = FONT; c2.textAlign = 'right'; c2.textBaseline = 'middle';
    c2.fillText(dayLabel(d), G - 8, y + CH / 2);
    for (let hr = 0; hr < 24; hr++) {
      const x = G + hr * cw, rec = hm.recorded[i][hr], mot = hm.motion_s[i][hr];
      if (rec < 60) hatch(c2, x + 1, y + 1, cw - 2, CH - 2);
      else { c2.fillStyle = seq(mot / max); c2.fillRect(x + 1, y + 1, cw - 2, CH - 2); }
      cells.push({ x, y, d, hr, rec, mot });
    }
  });
  c2.fillStyle = C.muted; c2.textAlign = 'center'; c2.textBaseline = 'top';
  for (let hr = 0; hr < 24; hr += (cw < 22 ? 3 : 2)) c2.fillText(String(hr).padStart(2, '0'), G + (hr + .5) * cw, T + n * CH + 4);
  const lg = $('#heat-legend');
  lg.replaceChildren();
  for (const [x, label] of [[0, '0'], [.5, fmtMin(max / 2)], [1, fmtMin(max)]]) {
    const i = el('i'); i.style.background = seq(x); lg.append(el('span', {}, i, label));
  }
  const ih = el('i', { className: 'hatched' }); lg.append(el('span', {}, ih, 'not recorded'));
  heatHover(cells, cw, CH, G, T, n);
}
function heatHover(cells, cw, CH, G, T, n) {
  const box = $('#heat');
  let tip = box.querySelector('.tip');
  if (!tip) { tip = el('div', { className: 'tip', hidden: true }); box.append(tip); }
  box.onpointermove = (e) => {
    const b = box.getBoundingClientRect(), x = e.clientX - b.left, y = e.clientY - b.top;
    const c = cells.find((k) => x >= k.x && x < k.x + cw && y >= k.y && y < k.y + CH);
    tip.hidden = !c;
    if (!c) return;
    tip.replaceChildren(el('b', { textContent: `${dayLabel(c.d)} · ${String(c.hr).padStart(2, '0')}:00–${String((c.hr + 1) % 24).padStart(2, '0')}:00` }),
      el('div', { textContent: c.rec < 60 ? 'not recorded' : `${fmtMin(c.mot)} of motion` }),
      c.rec >= 60 ? el('div', { textContent: `${fmtMin(c.rec)} recorded` }) : null);
    const left = x < b.width / 2;
    tip.style.cssText = `top:${Math.max(0, y - 10)}px;` + (left ? `left:${x + 14}px` : `right:${b.width - x + 14}px`);
  };
  box.onpointerleave = () => { tip.hidden = true; };
}

// Bars over categories (hours, duration buckets), one series: no legend box
function barChart(id, labels, values, tipFor, opts = {}) {
  const { ctx, w, h } = canvasFor(id);
  const L = 34, R = 8, T = 8, B = 20;
  const max = Math.max(opts.min || 1, ...values.map((v) => v || 0)) * 1.1;
  const ticks = niceTicks(max, 3);
  const Y = (v) => h - B - v / max * (h - B - T);
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.muted; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (const v of ticks) {
    const y = Math.round(Y(v)) + .5;
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
    ctx.fillText(opts.fmt ? opts.fmt(v) : String(v), L - 6, y);
  }
  const bw = (w - L - R) / labels.length;
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  const every = Math.ceil(labels.length / Math.max(1, Math.floor((w - L - R) / 34)));
  labels.forEach((lab, i) => {
    const x = L + i * bw;
    if (values[i] === null) hatch(ctx, x + 1, T, bw - 2, h - B - T);
    else { ctx.fillStyle = opts.color || C.motion; bar(ctx, x + 1, Y(values[i]), bw - 2, h - B - Y(values[i])); }
    if (i % every === 0) { ctx.fillStyle = C.muted; ctx.fillText(lab, x + bw / 2, h - B + 5); }
  });
  const box = $(id);
  let tip = box.querySelector('.tip');
  if (!tip) { tip = el('div', { className: 'tip', hidden: true }); box.append(tip); }
  box.onpointermove = (e) => {
    const b = box.getBoundingClientRect(), x = e.clientX - b.left, i = Math.floor((x - L) / bw);
    tip.hidden = i < 0 || i >= labels.length;
    if (tip.hidden) return;
    tip.replaceChildren(...tipFor(i).filter(Boolean).map((t, k) => el(k ? 'div' : 'b', { textContent: t })));
    const left = x < b.width / 2;
    tip.style.cssText = `top:${T}px;` + (left ? `left:${x + 12}px` : `right:${b.width - x + 12}px`);
  };
  box.onpointerleave = () => { tip.hidden = true; };
}

function drawHours() {
  if (!stats) { canvasFor('#hours'); return; }
  const hrs = stats.by_hour;
  barChart('#hours', hrs.map((r) => String(r.hour).padStart(2, '0')),
    hrs.map((r) => (r.per_hour_s === null ? null : r.per_hour_s / 60)),
    (i) => {
      const r = hrs[i];
      return [`${String(r.hour).padStart(2, '0')}:00–${String((r.hour + 1) % 24).padStart(2, '0')}:00`,
        r.per_hour_s === null ? 'not recorded' : `${(r.per_hour_s / 60).toFixed(1)} min of motion per hour`,
        r.per_hour_s === null ? null : `${r.recorded_h.toFixed(1)} h recorded · ${fmtMin(r.motion_s)} in all`];
    }, { min: 0.5, fmt: (v) => (v > 0 && v < 2 ? v.toFixed(1) : v.toFixed(0)) });
}

function drawDurs() {
  if (!stats) { canvasFor('#durs'); return; }
  const d = stats.durations;
  barChart('#durs', d.map((b) => b.label), d.map((b) => b.n),
    (i) => [d[i].label, `${d[i].n} period${d[i].n === 1 ? '' : 's'}`, i === 0 ? 'brief blips: often noise' : null],
    { color: C.blue, min: 1 });
}

// ---------------------------------------------------------------- debug
function renderDebug() {
  if (!dbg) return;
  drawTrace('#jump', 'jump', [[dbg.glitch, 'glitch limit', C.motion], [dbg.scan_shape, 'near a scan', C.brief]], 0.5, (v) => v.toFixed(2));
  drawTrace('#gap', 'gap', [[dbg.scan_gap_ms, 'scan gap', C.scan], [60, 'blips likelier', C.brief]], 150, (v) => `${v.toFixed(0)} ms`);
  fillRows($('#calibs'), [...dbg.calibs].reverse().map((c) => [clock(c.t, true), c.phase, c.why, c.floor > 0 ? c.floor.toFixed(4) : '–']), 'nothing yet');
  fillRows($('#sent'), [...dbg.sent].reverse().map(([t, c]) => [clock(t, true), c]), 'nothing sent yet');
  fillRows($('#refused'), [...dbg.refused].reverse().map(([t, line, last]) => [clock(t, true), line, last || '–']), 'none refused');
  const pre = $('#tail'), keep = tailFilter ? tailFilter.split(',') : null;
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  pre.textContent = dbg.tail.filter(([, line]) => !keep || keep.some((k) => line.startsWith(k)))
    .map(([t, line]) => `${clock(t, true)}  ${line}`).join('\n') || 'nothing yet';
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

function drawTrace(id, key, refs, ymin, fmt) {
  const { ctx, w, h } = canvasFor(id);
  const msg = $(`${id}-msg`);
  if (!dbg.window || !dbg.traces || !dbg.traces.length) { msg.textContent = 'waiting for router ACKs…'; return; }
  msg.textContent = '';
  const [t0, t1] = dbg.window;
  let ymax = ymin;
  for (const tr of dbg.traces) for (const v of tr[key]) ymax = Math.max(ymax, v * 1.1);
  ymax = Math.min(ymax, key === 'gap' ? 2000 : 3);
  const f = frame(ctx, w, h, { t0, t1, y0: 0, y1: ymax, yticks: niceTicks(ymax, 3), left: 40, right: 96 });
  ctx.save(); ctx.beginPath(); ctx.rect(f.r.l, f.r.t, f.r.r - f.r.l, f.r.b - f.r.t); ctx.clip();
  ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
  for (const [v, , col] of refs) { ctx.strokeStyle = col; ctx.globalAlpha = .7; ctx.beginPath(); ctx.moveTo(f.r.l, f.Y(v)); ctx.lineTo(f.r.r, f.Y(v)); ctx.stroke(); }
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  for (const tr of dbg.traces) {
    ctx.fillStyle = tr.color;
    for (let i = 0; i < tr.t.length; i++) ctx.fillRect(f.X(tr.t[i]) - 1, f.Y(Math.min(tr[key][i], ymax)) - 1, 2, 2);
  }
  ctx.restore();
  marginLabels(ctx, f, refs.map(([v, text, color]) => ({ v, text, color })));
  hoverable(id, f, (t) => {
    const out = [];
    for (const tr of dbg.traces) {
      const i = nearest(t, tr.t, Math.max(0.3, (t1 - t0) / 200));
      if (i >= 0) out.push(`${tr.label}: ${fmt(tr[key][i])}`);
    }
    return out.length ? out : ['no packets'];
  });
}

// ---------------------------------------------------------------- settings: network, devices
function bars(rssi) {
  const n = rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : 1;
  return '▮'.repeat(n) + '▯'.repeat(4 - n);
}

// Access points ticked on the Settings page (kept while the list is redrawn)
const apPick = new Set();
let apPickFrom = null;                   // the board's list the ticks were last set from

function apName(n, mac) {                // "network name · ch 6" for a BSSID heard in the scan
  for (const net of n.nets) for (const a of net.aps || []) if (a.bssid === mac) return { net: net.name, ch: a.ch, rssi: a.rssi };
  return null;
}

function renderNet() {
  const n = slow && slow.network;
  if (!n) return;
  const list = n.ap_list || [];
  const key = JSON.stringify(list);
  if (apPickFrom !== key) {              // the board's list changed: show it ticked
    apPickFrom = key;
    apPick.clear();
    for (const m of list) apPick.add(m);
  }
  $('#net-now').textContent = n.how === 'switching' ? 'Switching: the board restarts…'
    : list.length ? `Watching ${list.length} chosen access point${list.length > 1 ? 's' : ''}` +
      (n.channel ? ` on channel ${n.channel}` : '') + ': ' +
      list.map((m) => { const a = apName(n, m); return a ? `${a.net} (${m.slice(-5)}, ch ${a.ch})` : m; }).join(', ')
    : n.how === 'none' ? 'Not watching any network yet: pick one below.'
    : n.how === '?' ? 'Waiting for the board…'
    : `Watching "${n.name}"${n.how === 'default' ? ' (default)' : ' (chosen here)'}, every access point with that name`;
  $('#aps-clear').hidden = !list.length;
  renderApsBar(n);
  $('#net-state').textContent = n.state === 'scanning' ? 'scanning all channels (about 4 s)…'
    : n.state === 'done' ? `${n.nets.length} networks nearby` : 'press Scan to list the networks nearby';
  const body = $('#net-list');
  body.replaceChildren();
  for (const net of n.nets) {
    const tr = el('tr', net.name === n.name && !list.length ? { className: 'cur' } : {});
    const name = el('td', { textContent: net.name });
    const aps = net.aps || [];
    if (aps.length) {                                // its access points (mesh nodes, extenders)
      const boardCh = slow.details && slow.details.channel ? slow.details.channel[0] : null;
      const box = el('div', { className: 'aps' }, el('div', { textContent: aps.length === 1 ? '1 access point' : `${aps.length} access points` }));
      for (const a of aps) {
        const used = list.length ? list.includes(a.bssid) && a.ch === boardCh
          : net.name === n.name && a.ch === boardCh;
        const cb = el('input', { type: 'checkbox', checked: apPick.has(a.bssid), title: 'combine this access point with others' });
        cb.onchange = () => { if (cb.checked) apPick.add(a.bssid); else apPick.delete(a.bssid); renderApsBar(n); };
        box.append(el('label', { className: used ? 'used' : '' }, cb,
          (routerNick(a.bssid) ? `${routerNick(a.bssid)} · ` : '') + `${a.bssid} · ch ${a.ch} · ${a.rssi} dBm` + (used ? ' · in use' : '')));
      }
      name.append(box);
    }
    const sig = el('td', {}, el('span', { className: 'bars', textContent: bars(net.rssi) + ' ' }), `${net.rssi} dBm`);
    const td = el('td');
    if (net.name !== n.name || list.length) {
      const b = el('button', { type: 'button', textContent: 'Watch' });
      b.onclick = () => {
        if (confirm(`Watch "${net.name}" from now on?\n\nThe board restarts and recalibrates (about 15 s). Only pick your own network.`))
          cmd({ cmd: 'use', name: net.name });
      };
      td.append(b);
    } else td.textContent = 'watching';
    tr.append(name, el('td', { textContent: net.ch }), sig, td);
    body.append(tr);
  }
}

// The button and note under the network list: how many are ticked, and
// whether they share a channel (the board listens on one)
function renderApsBar(n) {
  const picked = [...apPick], chans = new Set();
  for (const m of picked) { const a = apName(n, m); if (a) chans.add(a.ch); }
  const same = JSON.stringify([...picked].sort()) === JSON.stringify([...(n.ap_list || [])].sort());
  const btn = $('#aps-use');
  btn.disabled = !picked.length || picked.length > 8 || same || !(fast && fast.header.board_connected);
  btn.textContent = picked.length ? `Watch the ${picked.length} ticked access point${picked.length > 1 ? 's' : ''}` : 'Watch the ticked access points';
  const note = $('#aps-note');
  note.className = 'small ' + (chans.size > 1 || picked.length > 8 ? 'warn' : 'muted');
  note.textContent = picked.length > 8 ? 'At most 8 access points.'
    : chans.size > 1 ? `These are on channels ${[...chans].sort((a, b) => a - b).join(', ')}. The board listens on one channel: it will use the one with the most of them and skip the rest. Set those routers to the same 2.4 GHz channel to use them all.`
    : picked.length && !same ? 'Each one gets a share of the router pokes (about 94 a second in all); device links are not affected.'
    : !picked.length ? 'Tick access points (any network) to watch several routers at once.' : '';
}

function renderDev() {
  const d = slow && slow.devices;
  if (!d) return;
  const seg = $('#dev-mode');
  if (!seg.childElementCount) {
    const names = { auto: 'Auto', all: 'All active', manual: 'Manual', off: 'Off' };
    for (const m of d.modes) {
      const b = el('button', { type: 'button', textContent: names[m] || m });
      b.dataset.m = m;
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
    const tr = el('tr', { className: r.poked ? 'poked' : ['gone', 'quiet', 'random'].includes(r.status) ? 'dim' : '' });
    const cb = el('input', { type: 'checkbox', checked: r.poked || r.picked, title: 'poke this device' });
    cb.onchange = () => cmd({ cmd: 'toggle', mac: r.mac });
    tr.append(el('td', {}, cb));
    [`${r.name}  ${r.mac}`, r.router || (r.board ? 'another board' : '–'), r.rssi ? `${r.rssi} dBm` : '–', r.rate, r.heard, r.why, r.link]
      .forEach((v, i) => tr.append(el('td', { textContent: v, className: i === 5 ? 'why' : '' })));
    body.append(tr);
  }
}

// Routers the board learned: your name for each (kept on the PC by BSSID),
// its network, signal and the devices that talk through it
function routerNick(bssid) {
  const names = slow && slow.settings && slow.settings.router_names;
  return (names && names[String(bssid).toLowerCase()]) || '';
}
function renderRouters() {
  const rs = slow && slow.routers;
  if (!rs) return;
  $('#routers-state').textContent = rs.length ? `${rs.length} found` : 'waiting for the board…';
  const STATE = { ok: 'good', weak: 'weak', silent: 'not answering', far: 'too far' };
  fillRows($('#routers'), rs.map((r) => {
    // short: the poked ones by name, the rest as a count (the full list on hover)
    const poked = r.devices.filter((d) => d.poked).map((d) => d.name);
    const rest = r.devices.length - poked.length;
    const devs = el('span', { title: r.devices.map((d) => `${d.name} ${d.mac}`).join('\n'),
      textContent: !r.devices.length ? '–' : [poked.length ? `poking ${poked.join(', ')}` : '',
        rest ? `${rest}${poked.length ? ' more' : ''} heard` : ''].filter(Boolean).join(' · ') });
    const btn = el('button', { type: 'button', className: 'ghost', textContent: r.nick ? 'Rename' : 'Name it',
      onclick: () => {
        const name = prompt(`A name for ${r.name} (${r.bssid}). Leave empty to go back to "Router ${r.idx + 1}".`, r.nick);
        if (name !== null) cmd({ cmd: 'router_name', bssid: r.bssid, name });
      } });
    return [el('span', {}, el('b', { textContent: r.name }), el('span', { className: 'muted small', textContent: ` ${r.bssid}` })),
      r.network || '–', r.ch ?? '–', r.rssi ? `${r.rssi} dBm${r.state ? ' · ' + (STATE[r.state] || r.state) : ''}` : '–',
      r.rate || '–', devs, btn];
  }), 'none yet');
}

// ---------------------------------------------------------------- activity
// Activity recognition by example (tools/activity.py): record a short clip of
// each activity; the live verdict is the activity whose clips look closest,
// or "unknown" when none does. ✓ / ✗ keep the last 6 s as another example.
const ACT_STATES = {
  uncalibrated: ['Not calibrated', 'record a clip of each activity below to start'],
  'no-data': ['No data', 'waiting for the router link'],
  unknown: ['Unknown', 'nothing looks like your clips'],
};
const ACT_WHY = {
  'weak link': 'the router link is too weak to judge', starting: 'collecting the first seconds',
  'router glitch': 'the router changed its signal: ignored', 'nothing close': 'nothing looks like your clips',
  changing: 'changing…', 'no states calibrated': 'only moments are calibrated: add a state (empty, still, walking)',
};
let actPicking = false;
function actName(key) {
  const l = slow && slow.act && slow.act.labels.find((x) => x.key === key);
  return l ? l.name : key.replace(/_/g, ' ');
}
function renderActHome() {
  const a = fast.activity, e = $('#home-act');
  const n = a && a.now;
  if (!n || ['uncalibrated', 'no-data'].includes(n.state)) { e.hidden = true; return; }
  e.hidden = false;
  e.replaceChildren('Activity: ', el('b', { textContent: n.name || ACT_STATES[n.state]?.[0] || n.state }),
    n.conf ? ` · ${n.conf} match` : '');
}
function renderActNow() {
  const a = fast.activity;
  if (!a) return;
  const n = a.now || { state: 'uncalibrated' };
  const known = !ACT_STATES[n.state];
  $('#act-now').dataset.state = known ? 'known' : n.state;
  $('#act-word').textContent = known ? (n.name || n.state) : ACT_STATES[n.state][0];
  $('#act-sub').textContent = known
    ? `${n.conf === 'close' ? 'close match' : 'likely'} · ${n.dist}× the usual difference`
    : (ACT_WHY[n.why] || ACT_STATES[n.state][1]);
  $('#act-meta').textContent = n.rate ? `${n.rate} ACK/s · ch ${n.ch}` : '';
  // the two answers: movement (from how the signal moves) and posture (where a still body is)
  const ax = $('#act-axes'), hasAxes = 'movement' in n;
  ax.hidden = !hasAxes;
  if (hasAxes) {
    const PN = { away: 'away', standing: 'standing', sitting: 'sitting', lying: 'lying' };
    ax.replaceChildren('Movement: ', el('b', { textContent: n.movement || '?' }), ' · Posture: ',
      el('b', { textContent: n.posture ? PN[n.posture] : '?' }),
      el('span', { className: 'muted small', textContent: n.posture ? '' : !n.posture_here
        ? '  (no posture clips here yet)' : '  (not sure)' }));
  }
  $('#act-fix').hidden = actPicking || !(known || n.state === 'unknown');
  $('#act-yes').hidden = !known;
  $('#act-pick').hidden = !actPicking;
  fillList($('#act-events'), (a.events || []).slice().reverse().map((ev) =>
    el('li', {}, el('span', { textContent: clock(ev.wall, true) }), ev.name)), 'none yet');
  // recording card
  const r = a.recording, card = $('#act-rec-card');
  card.dataset.phase = r ? r.phase : '';
  $('#act-rec-idle').hidden = !!r;
  $('#act-rec-live').hidden = !r;
  $('#act-cancel').hidden = !r;
  if (r) {
    const info = slow && slow.act && slow.act.labels.find((x) => x.key === r.label);
    $('#act-rec-label').textContent = actName(r.label);
    $('#act-rec-hint').textContent = info ? info.hint : '';
    $('#act-rec-phase').textContent = r.phase === 'countdown' ? 'Get ready…' : 'Recording';
    $('#act-rec-left').textContent = `${Math.ceil(r.left)} s`;
    const frac = r.phase === 'countdown' ? 0 : 1 - r.left / r.total;
    $('#act-rec-bar').style.width = `${Math.round(100 * Math.min(1, Math.max(0, frac)))}%`;
  }
  const sv = a.saved;
  $('#act-saved').textContent = sv && Date.now() / 1000 - sv.t < 60
    ? (sv.ok ? `Saved a clip of ${actName(sv.label).toLowerCase()} (${sv.windows} windows).`
      : `Nothing usable was recorded for ${actName(sv.label).toLowerCase()}: was the router link weak? Try again.`)
    : '';
}
function localDay(t) {                   // YYYY-MM-DD of a wall time, in local time
  const z = zp(t), p = (n) => String(n).padStart(2, '0');
  return `${z.y}-${p(z.mo)}-${p(z.d)}`;
}
function fillList(ul, items, empty) {
  ul.replaceChildren(...(items.length ? items : [el('li', { className: 'muted', textContent: empty })]));
}
function renderActCal() {
  const a = slow.act;
  if (!a) return;
  $('#act-ch').textContent = a.channel ? `router link on channel ${a.channel}` : '';
  // which postures your clips showed can be trusted (tested on your own clips)
  const pc = a.posture_check || {}, keys = Object.keys(pc);
  $('#act-trust').textContent = !keys.length ? 'Postures: record two of sitting, lying, standing or empty (still) to tell them apart.'
    : 'Postures shown: ' + (a.posture_ok.length ? a.posture_ok.join(', ') : 'none yet') +
      (keys.filter((k) => !a.posture_ok.includes(k)).length
        ? ` · need more clips: ${keys.filter((k) => !a.posture_ok.includes(k)).map((k) => `${k} (${pc[k][1] ? Math.round(100 * pc[k][0] / pc[k][1]) + '% right' : 'never picked'})`).join(', ')}` : '');
  const recording = fast && fast.activity && fast.activity.recording;
  $('#act-labels').replaceChildren(...a.labels.map((l) => {
    const chWarn = l.clips && a.channel && l.chs && l.chs.length && !l.chs.includes(a.channel);
    const info = l.clips
      ? `${l.clips} clip${l.clips > 1 ? 's' : ''} · last ${fmtAgo(Date.now() / 1000 - l.last)} ago${chWarn ? ' · on another channel: record again here' : ''}`
      : 'not recorded yet';
    const rec = el('button', { type: 'button', textContent: 'Record', disabled: !!recording,
      onclick: () => cmd({ cmd: 'act_record', label: l.key }) });
    const clr = el('button', { type: 'button', className: 'ghost', textContent: 'Clear', hidden: !l.clips,
      onclick: () => { if (confirm(`Delete every clip of ${l.name.toLowerCase()}?`)) cmd({ cmd: 'act_clear', label: l.key }); } });
    return el('li', { className: l.clips ? 'has' : '' },
      el('div', { className: 'act-name' }, l.name, l.kind === 'event' ? el('i', { textContent: ' · moment' }) : null),
      el('div', { className: 'act-btns' }, rec, clr),
      l.hint ? el('div', { className: 'act-hint', textContent: l.hint }) : null,
      el('div', { className: 'act-info' + (chWarn ? ' warn' : ''), textContent: info }));
  }));
  fillRows($('#act-log'), a.log.slice().reverse().map(([w0, w1, st]) =>
    [clock(w0, true), ACT_STATES[st] ? ACT_STATES[st][0] : actName(st),
      w1 ? fmtSpan(w1 - w0) : `${fmtSpan(Date.now() / 1000 - w0)}, now`]), 'nothing yet');
  fillRows($('#act-clips'), a.clips.map((c) => [`${dayLabel(localDay(c.t))} ${clock(c.t, false)}`,
    c.name, c.ch ?? '–', c.source,
    el('button', { type: 'button', className: 'ghost', textContent: 'Delete',
      onclick: () => cmd({ cmd: 'act_delete', id: c.id }) })]), 'no clips yet');
  // the ✗ list: every activity that has clips
  const have = a.labels.filter((l) => l.clips && l.kind === 'state');
  $('#act-pick-list').replaceChildren(...have.map((l) => el('button', { type: 'button', textContent: l.name,
    onclick: () => { actPicking = false; cmd({ cmd: 'act_fix', label: l.key }); } })),
    el('button', { type: 'button', className: 'ghost', textContent: 'Cancel', onclick: () => { actPicking = false; renderActNow(); } }));
}
$('#act-yes').onclick = () => {
  const n = fast && fast.activity && fast.activity.now;
  if (n && n.state && !ACT_STATES[n.state]) cmd({ cmd: 'act_fix', label: n.state });
};
$('#act-no').onclick = () => { actPicking = true; if (slow) renderActCal(); renderActNow(); };
$('#act-cancel').onclick = () => cmd({ cmd: 'act_cancel' });
$('#act-moved').onclick = () => {
  if (confirm('Did you move the board or the router? Movement keeps working; postures (sitting, lying…) need a short new clip of each here.')) cmd({ cmd: 'act_moved' });
};
$('#act-clear').onclick = () => { if (confirm('Delete every activity clip? It will have to be calibrated again.')) cmd({ cmd: 'act_clear' }); };
$('#act-own').onsubmit = (e) => {
  e.preventDefault();
  const key = $('#act-own-name').value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 24);
  if (!key) { toast('type a name first', true); return; }
  cmd({ cmd: 'act_record', label: key });
};

// ---------------------------------------------------------------- pages
function route() {
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  page = ROUTES.includes(name) ? name : 'home';
  for (const s of document.querySelectorAll('.page')) s.hidden = s.dataset.page !== page;
  for (const a of document.querySelectorAll('#nav a')) {
    const on = a.dataset.route === page;
    a.classList.toggle('on', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  document.body.dataset.page = page;
  // The map you set up on the Map page also shows on Home, view only
  if (page === 'map') { window.AckMap.mount(null, false); window.AckMap.show(); }
  else if (page === 'home') { window.AckMap.mount('#home-map-slot', true); window.AckMap.show(); }
  else window.AckMap.hide();
  clearInterval(statsTimer);
  if (page === 'stats') { loadStats(); statsTimer = setInterval(loadStats, 60000); }
  clearInterval(where.timer);
  if (page === 'home') whereRange(where.hours);
  if (page === 'settings' && !netScanned && fast && fast.header.board_connected &&
      slow && slow.network && slow.network.state === 'idle') { netScanned = true; cmd({ cmd: 'scan' }); }
  renderPage();
}
function go(name) { location.hash = `#/${name}`; }

// Simple mode (default) hides the Graphs, Links and Debug pages from the
// menu; Advanced shows them. Remembered per browser.
function setAdvanced(on, save = true) {
  document.body.classList.toggle('simple', !on);
  $('#btn-adv').setAttribute('aria-pressed', String(on));
  if (save) try { localStorage.setItem('ackwave.advanced', on ? '1' : '0'); } catch (e) { /* private mode */ }
  if (!on && document.querySelector(`#nav a[data-route="${page}"][data-adv]`)) go('home');
}
$('#btn-adv').onclick = () => setAdvanced(document.body.classList.contains('simple'));
let advSaved = null;
try { advSaved = localStorage.getItem('ackwave.advanced'); } catch (e) { /* private mode */ }
setAdvanced(advSaved === '1', false);

// ---------------------------------------------------------------- controls
function setWinButtons(w) {
  win = w;
  $('#home-live-win').textContent = w < 60 ? `last ${w} s` : `last ${w / 60} min`;
  for (const b of $('#win').children) b.classList.toggle('on', +b.dataset.w === w);
}
function setWin(w) { setWinButtons(w); connect(); }
function togglePause() {
  paused = !paused;
  $('#btn-pause').textContent = paused ? 'Live' : 'Pause';
  $('#btn-pause').classList.toggle('on', paused);
  setLive(paused ? 'paused' : 'live');
  if (!paused) renderPage();
}

$('#win').addEventListener('click', (e) => { if (e.target.dataset.w) setWin(+e.target.dataset.w); });
$('#btn-pause').onclick = togglePause;
$('#btn-cal').onclick = () => cmd({ cmd: 'cal' });
$('#btn-help').onclick = () => $('#dlg-help').showModal();
for (const b of $('#sens-level').children) b.onclick = () => cmd({ cmd: 'sens', x100: +b.dataset.x });
$('#sens-default').onclick = () => cmd({ cmd: 'sens', x100: 'default' });
for (const b of $('#snd-mode').children) b.onclick = () => setSound(b.dataset.m);
for (const id of ['#snd-vol', '#snd-hz']) {
  const e = $(id);
  e.oninput = () => { e.dataset.drag = '1'; showSoundValues(); };
  e.onchange = () => { delete e.dataset.drag; setSound(); };
}
$('#snd-test').onclick = () => cmd({ cmd: 'sound', test: true });
for (const b of $('#led-mode').children) b.onclick = () => setLed({ mode: b.dataset.m });
for (const b of $('#led-pattern').children) b.onclick = () => setLed({ pattern: b.dataset.p });
$('#led-bright').oninput = () => { $('#led-bright').dataset.drag = '1'; showLedValues(); };
$('#led-bright').onchange = () => { delete $('#led-bright').dataset.drag; setLed({}); };
$('#led-color').oninput = showLedValues;
$('#led-color').onchange = () => setLed({});
$('#led-default').onclick = () => cmd({ cmd: 'led', mode: 'default' });
$('#snd-default').onclick = () => cmd({ cmd: 'sound', mode: 'default' });
$('#set-per-link').onchange = (e) => cmd({ cmd: 'settings', per_link: e.target.checked });
$('#net-scan').onclick = () => { netScanned = true; cmd({ cmd: 'scan' }); };
$('#aps-use').onclick = () => {
  const macs = [...apPick];
  if (confirm(`Watch these ${macs.length} access point${macs.length > 1 ? 's' : ''} from now on?\n\n${macs.join('\n')}\n\nThe board restarts and recalibrates (about 15 s). Only pick your own routers.`))
    cmd({ cmd: 'aps', macs });
};
$('#aps-clear').onclick = () => {
  if (confirm('Go back to watching every access point of the network by name?\n\nThe board restarts (about 15 s).'))
    cmd({ cmd: 'aps_clear' });
};
$('#net-default').onclick = () => {
  if (confirm('Go back to the network built into the firmware (main/my_network.h)?\n\nThe board restarts (about 15 s).'))
    cmd({ cmd: 'use_default' });
};
$('#tail-filter').addEventListener('click', (e) => {
  if (e.target.dataset.f === undefined) return;
  tailFilter = e.target.dataset.f;
  for (const b of $('#tail-filter').children) b.classList.toggle('on', b === e.target);
  if (dbg) renderDebug();
});
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea') || document.querySelector('dialog[open]') || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  const pages = { h: 'home', a: 'activity', g: 'graphs', s: 'stats', l: 'links', d: 'debug' };
  if (k === ' ' && page === 'graphs') { e.preventDefault(); togglePause(); }
  else if (k === '1' || k === '2' || k === '3') setWin([30, 120, 600][+k - 1]);
  else if (k === 'c') $('#btn-cal').click();
  else if (pages[k]) go(pages[k]);
  else if (k === '?') $('#dlg-help').showModal();
});
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderPage, 80);
});
window.addEventListener('hashchange', route);

// ?shot: render one snapshot synchronously (headless screenshots), on the
// page in the hash; &hover=<0..1> shows the readout that far across every chart
const params = new URLSearchParams(location.search);
if (params.has('shot')) {
  document.body.classList.add('shot');
  setLive('snapshot');
  const get = (url) => {
    const x = new XMLHttpRequest();
    x.open('GET', url, false);
    x.setRequestHeader('X-Ackwave', '1');
    x.send();
    return JSON.parse(x.responseText);
  };
  ({ fast, slow, debug: dbg } = get(`/snapshot?win=${+params.get('win') || 30}${params.get('link') ? `&link=${+params.get('link')}` : ''}`));
  if (location.hash.startsWith('#/stats')) stats = get('/api/stats');
  if (params.get('win')) setWinButtons(+params.get('win'));
  if (params.get('link')) link = +params.get('link');
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  page = ROUTES.includes(name) ? name : 'home';
  for (const s of document.querySelectorAll('.page')) s.hidden = s.dataset.page !== page;
  for (const a of document.querySelectorAll('#nav a')) a.classList.toggle('on', a.dataset.route === page);
  document.body.dataset.page = page;
  renderPage();
  if (page === 'map') window.AckMap.snapshot(get('/api/map'), slow, fast.header);
  if (params.get('hover')) {
    for (const [id, h] of Object.entries(hov)) {
      h.x = h.f.r.l + (h.f.r.r - h.f.r.l) * +params.get('hover'); h.y = (h.f.r.t + h.f.r.b) / 2;
      showHover(id);
    }
  }
} else {
  route();
  window.addEventListener('load', connect);
  requestAnimationFrame(frameLoop);
}
