/* Sensors: what each board saw, from when to when, and how sure (tools/sensors.py).
   - cards: one per board: its state now, how sure (sure / likely / possible, with the
     evidence), since when, its router link's quality, and whether the settings you
     gave it are confirmed by the board (not just sent)
   - the log: a strip per board over the last hours (moving / still / empty / quiet /
     weak / not heard) and every period in words, newest first
   Uses app.js's helpers (el, cmd, fmtSpan, clock, slow). Text only, never markup:
   names come from the network. */
(function () {
  'use strict';
  const SVG = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs = {}, ...kids) => {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) e.setAttribute(k, v);
    for (const k of kids) if (k) e.append(k);
    return e;
  };
  const $s = (q) => document.querySelector(q);
  const WORD = { moving: 'Moving', still: 'Someone still', empty: 'Empty', quiet: 'Quiet', weak: 'Weak link',
    calibrating: 'Calibrating', gone: 'Not heard' };
  const HELP = {
    moving: 'movement on this board\'s links',
    still: 'no movement, but someone seems to be there: from its learned model or breathing (a guess, not a measurement)',
    empty: 'no movement, and its learned model says nobody is there (a guess, not a measurement)',
    quiet: 'no movement. Without a learned model a board can\'t tell a still person from an empty room',
    weak: 'its router answers too rarely or too faintly to judge movement',
    calibrating: 'measuring its quiet level: keep still for about 10 s',
    gone: 'no report reaches the hub: power or range?',
  };
  const SET_NAME = { SENS: 'sensitivity', LEDSET: 'LED', SOUND: 'buzzer', OUT: 'light output', DEVHZ: 'poke rate', POLICY: 'poke policy' };
  const SET_HOW = {
    verified: ['✓', 'the board reports this value itself'],
    done: ['✓', 'the board confirmed it got it'],
    pending: ['…', 'sent, not confirmed yet'],
    failed: ['✗', 'the board never confirmed it'],
    unknown: ['?', 'this board\'s firmware doesn\'t report it (reflash over USB for the check)'],
  };
  const STATE_ORDER = ['moving', 'still', 'empty', 'quiet', 'weak', 'calibrating', 'gone'];
  const WDN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const zpart = (t) => (typeof zp === 'function' ? zp(t) : (() => { const d = new Date(t * 1000); return { y: d.getFullYear(), mo: d.getMonth() + 1, d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), wd: d.getDay() }; })());
  const dayKey = (t) => { const z = zpart(t); return `${z.y}-${z.mo}-${z.d}`; };
  const dayLabel = (t) => { const z = zpart(t); return `${WDN[z.wd]} ${z.d}`; };
  // whole hours between t0 and t1 in the PC's zone (as the times on hover): [t, hour]
  function hourMarks(t0, t1) {
    const out = [];
    for (let t = Math.ceil(t0 / 1800) * 1800; t <= t1; t += 1800) {
      const z = zpart(t);
      if (z.mi === 0) out.push([t, z.h]);
    }
    return out;
  }
  const hhmm = (t) => (typeof clock === 'function' ? clock(t, true) : new Date(t * 1000).toLocaleTimeString());
  const span = (s) => (typeof fmtSpan === 'function' ? fmtSpan(s) : `${Math.round(s)} s`);

  function quality(b) {
    if (b.state === 'gone') return { q: 'none', text: 'not heard' };
    const a = b.acks || 0, r = b.router_rssi || -100;
    const q = b.weak || a < 20 || r < -85 ? 'poor' : a >= 60 && r >= -75 ? 'good' : 'fair';
    return { q, text: `${a}/s answers · ${b.router_rssi || '–'} dBm · ${q}` };
  }

  // ------------------------------------------------------------ cards
  function card(b, s, st, cmdv) {
    const nowS = Date.now() / 1000;
    const state = s ? s.state : b.state;
    const top = el('div', { className: 'sn-top' },
      el('b', { textContent: b.name }),
      el('span', { className: `sn-state ${state}`, textContent: WORD[state] || state }));
    const conf = s ? el('span', { className: `sn-conf ${s.grade}`, title: 'how sure: sure ≥ 80 %, likely ≥ 55 %, possible below',
      textContent: `${s.grade} ${s.conf}%` }) : null;
    const since = s && s.since ? `since ${hhmm(s.since)} (${span(nowS - s.since)})` : '';
    const lines = [];
    if (s && s.pending && s.pending !== state) lines.push(el('div', { className: 'small muted', textContent: `changing to ${(WORD[s.pending] || s.pending).toLowerCase()}…` }));
    for (const w of ((s && s.why) || []).slice(0, 3)) lines.push(el('div', { className: 'small', textContent: w }));
    if (state === 'moving' && b.near_me) lines.push(el('div', { className: 'small sn-where', textContent: 'right by this board (most of its links react)' }));
    else if (state === 'moving' && b.by && b.by.length) lines.push(el('div', { className: 'small sn-where', textContent: `along: ${b.by.join(', ')}` }));
    const qv = quality(b);
    const wires = (b.wires || []).map((x) => el('span', { className: `ov-wchip ${x.state}`, title: `level ${x.level} (1 = its threshold)`, textContent: x.to }));
    const sets = Object.entries(st || {}).filter(([, v]) => !(b.reports_cfg === false && v.state === 'unknown')).map(([k, v]) => {
      const [mark, how] = SET_HOW[v.state] || SET_HOW.unknown;
      return el('span', { className: `sn-set ${v.state}`, title: `${v.line}: ${how}`, textContent: `${mark} ${SET_NAME[k] || k}` });
    });
    const inflight = cmdv && ['sent', 'queued'].includes(cmdv.state)
      ? el('div', { className: 'small muted', textContent: `sending ${cmdv.what} (${cmdv.age} s)${cmdv.waiting ? ` · ${cmdv.waiting} more waiting` : ''}` }) : null;
    return el('div', { className: `sn-card ${state}`, title: HELP[state] || '' },
      top,
      el('div', { className: 'sn-sub' }, conf, el('span', { className: 'muted small', textContent: since })),
      ...lines,
      wires.length ? el('div', { className: 'ov-wchips' }, ...wires) : null,
      el('div', { className: `small sn-q ${qv.q}`, textContent: `Router link: ${qv.text}${b.via === 'usb' ? ' · on USB' : ''}` }),
      b.pokes && b.pokes.length ? el('div', { className: 'small muted', textContent: `pokes: ${b.pokes.join(', ')}` }) : null,
      b.policy ? el('div', { className: 'small sn-policy', textContent: `being brought in line: ${b.policy}` }) : null,
      sets.length || b.reports_cfg === false ? el('div', { className: 'sn-sets' }, ...sets,
        b.reports_cfg === false && b.state !== 'gone' ? el('span', { className: 'sn-set unknown',
          title: 'Older firmware: it doesn\'t report its settings or what each slot pokes, so they can\'t be checked on the board. Reflash it over USB.',
          textContent: '⚠ older firmware' }) : null) : null,
      inflight);
  }

  function cards(sl) {
    const now = Object.fromEntries(((sl.sensors || {}).now || []).map((s) => [s.key, s]));
    const boards = Object.fromEntries((sl.boards || []).map((b) => [`sat:${b.mac}`, b]));
    return (sl.bstates || []).map((b) => {
      const mac = b.key.startsWith('sat:') ? b.key.slice(4) : null;
      const extra = mac ? boards[b.key] || {} : {};
      return card(Object.assign({}, b, { pokes: extra.pokes, policy: extra.policy, reports_cfg: mac ? extra.reports_cfg : undefined }), now[b.key],
        mac ? (sl.sat_settings || {})[mac] : null, mac ? (sl.sat_cmds || {})[mac] : null);
    });
  }

  // ------------------------------------------------------------ the log
  const cache = {};                     // hours -> { at, log } (Home asks for 12 h, the Log page for its range)
  const loading = {};
  let hist = { at: 0, log: [] };
  async function load(hours, force) {
    const c = cache[hours];
    hist = c || { at: 0, log: [] };
    if (loading[hours] || (!force && c && Date.now() - c.at < 60000)) return;
    loading[hours] = true;
    try {
      const r = await fetch(`api/sensors?hours=${hours}`, { headers: { 'X-Ackwave': '1' } });
      if (r.ok) cache[hours] = Object.assign(await r.json(), { at: Date.now() });
    } catch (e) { /* offline: keep the last */ }
    loading[hours] = false;
    hist = cache[hours] || hist;
  }
  function periods(sl, hours) {
    const t0 = Date.now() / 1000 - hours * 3600;
    const seen = new Set();
    const out = [];
    for (const e of [...((sl && sl.sensors && sl.sensors.log) || []), ...(hist.log || [])]) {
      const id = `${e.key}|${e.t0}|${e.state}`;
      if (seen.has(id) || e.t1 < t0) continue;
      seen.add(id); out.push(e);
    }
    return out.sort((a, b) => b.t0 - a.t0);
  }
  function strips(svgSel, sl, hours) {
    const svg = $s(svgSel);
    if (!svg) return;
    const nowS = Date.now() / 1000, t0 = nowS - hours * 3600;
    const names = new Map();
    for (const b of (sl.bstates || [])) names.set(b.key, b.name);
    const all = periods(sl, hours);
    for (const e of all) if (!names.has(e.key)) names.set(e.key, e.name);
    const keys = [...names.keys()];
    const narrow = (svg.clientWidth || 1000) < 600;      // a phone: fewer units across, so the text stays readable
    const W = narrow ? 500 : 1000, LW = narrow ? 90 : 130, RH = 26, H = keys.length * RH + 22;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const x = (t) => LW + (W - LW - 10) * (Math.max(t0, Math.min(nowS, t)) - t0) / (nowS - t0);
    const g = sv('g');
    keys.forEach((k, i) => {
      const y = i * RH + 4;
      g.append(sv('text', { x: 4, y: y + 15, class: 'sn-lbl' }, document.createTextNode(names.get(k))));
      g.append(sv('rect', { x: LW, y, width: W - LW - 10, height: RH - 8, class: 'sn-bg' }));
      const open = ((sl.sensors || {}).now || []).filter((s) => s.key === k).map((s) => ({ state: s.state, t0: s.since, t1: nowS, grade: s.grade, conf: s.conf, by: s.by }));
      for (const e of [...all.filter((e) => e.key === k), ...open]) {
        if (e.state === 'quiet') continue;        // the background is quiet
        const a = x(e.t0), b = Math.max(x(e.t1), a + 1.5);
        g.append(sv('rect', { x: a, y, width: b - a, height: RH - 8, class: `sn-p ${e.state} ${e.grade || ''}` },
          sv('title', {}, document.createTextNode(`${names.get(k)}: ${(WORD[e.state] || e.state).toLowerCase()} ${hhmm(e.t0)} → ${hhmm(e.t1)} (${span(e.t1 - e.t0)}, ${e.grade || ''} ${e.conf != null ? e.conf + '%' : ''})${e.by && e.by.length ? ' · on ' + e.by.join(', ') : ''}`))));
      }
    });
    const step = (hours <= 6 ? 1 : hours <= 24 ? 3 : hours <= 72 ? 12 : 24) * (narrow ? 2 : 1);
    for (const [t, hr] of hourMarks(t0, nowS)) {
      if (hr % step) continue;
      g.append(sv('line', { x1: x(t), x2: x(t), y1: 0, y2: H - 18, class: 'sn-tick' }));
      g.append(sv('text', { x: x(t), y: H - 4, 'text-anchor': 'middle', class: 'sn-tlbl' }, document.createTextNode(`${String(hr).padStart(2, '0')}:00`)));
    }
    svg.replaceChildren(g);
  }
  function row(e) {
    return el('div', { className: `sn-row ${e.state} ${e.grade || ''}` },
      el('span', { className: 'muted sn-when', textContent: `${hhmm(e.t0)} → ${hhmm(e.t1)}` }),
      el('b', { textContent: e.name }),
      el('span', { className: `sn-state ${e.state}`, textContent: (WORD[e.state] || e.state).toLowerCase() }),
      el('span', { textContent: span(e.t1 - e.t0) }),
      el('span', { className: `sn-conf ${e.grade}`, textContent: `${e.grade} ${e.conf}%` }),
      el('span', { className: 'muted small', textContent: [...(e.by && e.by.length ? [`on ${e.by.join(', ')}`] : []), ...(e.why || []).slice(0, 1)].join(' · ') }));
  }

  function homeLog(sl) {
    load(12);
    const box0 = $s('#sn-home-list');
    const sig = JSON.stringify([hist.at, (sl.sensors || {}).log && sl.sensors.log.length && sl.sensors.log[0].t0,
      ((sl.sensors || {}).now || []).map((x) => x.state + x.since), Math.floor(Date.now() / 15000)]);
    if (!box0 || box0.dataset.sig === sig) return;
    box0.dataset.sig = sig;
    strips('#sn-home-strips', sl, 12);
    const box = $s('#sn-home-list');
    if (!box) return;
    const list = periods(sl, 12).filter((e) => e.state !== 'quiet').slice(0, 8);
    box.replaceChildren(...(list.length ? list.map(row) : [el('p', { className: 'muted', textContent: 'No periods yet: the log fills as the boards report.' })]));
  }

  const LOG = { hours: 24, key: '', state: 'notquiet' };
  function logPage(sl) {
    if (!sl) return;
    load(LOG.hours);
    wiresChart(LOG.hours);
    routine();
    const all = periods(sl, LOG.hours);
    const keys = new Map((sl.bstates || []).map((b) => [b.key, b.name]));
    for (const e of all) if (!keys.has(e.key)) keys.set(e.key, e.name);   // boards only in the history too
    if (LOG.key && !keys.has(LOG.key)) LOG.key = '';
    const selK = $s('#sn-f-key');
    const ksig = JSON.stringify([...keys]);
    if (selK && selK.dataset.sig !== ksig) {
      selK.dataset.sig = ksig;
      selK.replaceChildren(el('option', { value: '', textContent: 'every board' }),
        ...[...keys].map(([k, n]) => el('option', { value: k, textContent: n })));
      selK.value = LOG.key;
    }
    // the page renders every second: redrawn only when something changed, or every 15 s (the strips
    // move with time) — a redraw drops the hover text and the list's scroll
    const open = ((sl.sensors || {}).now || []).map((x) => `${x.key}:${x.state}:${x.since}`).join();
    const sig = JSON.stringify([hist.at, all.length, all.length && all[0].t0, open, LOG, Math.floor(Date.now() / 15000)]);
    const listBox = $s('#sn-log-list');
    if (listBox.dataset.sig === sig) return;
    listBox.dataset.sig = sig;
    strips('#sn-log-strips', sl, LOG.hours);
    const list = all.filter((e) => (!LOG.key || e.key === LOG.key)
      && (LOG.state === 'all' || (LOG.state === 'notquiet' ? e.state !== 'quiet' : e.state === LOG.state)));
    const tot = {};
    for (const e of list) tot[e.state] = (tot[e.state] || 0) + (e.t1 - e.t0);
    $s('#sn-log-sum').textContent = STATE_ORDER.filter((s) => tot[s]).map((s) => `${(WORD[s] || s).toLowerCase()} ${span(tot[s])}`).join(' · ') || 'nothing in this range';
    $s('#sn-log-list').replaceChildren(...(list.length ? list.slice(0, 400).map(row) : [el('p', { className: 'muted', textContent: 'Nothing in this range.' })]));
  }
  // ------------------------------------------------------------ where and when: every link
  let wh = { at: 0, hours: 0, data: null }, whLoading = false;
  async function loadWires(hours) {
    if (whLoading || (wh.hours === hours && Date.now() - wh.at < 120000)) return;
    whLoading = true;
    try {
      const r = await fetch(`api/wires?hours=${Math.max(1, hours)}`, { headers: { 'X-Ackwave': '1' } });
      if (r.ok) wh = { at: Date.now(), hours, data: await r.json() };
    } catch (e) { /* keep the last */ }
    whLoading = false;
  }
  function wiresChart(hours) {
    const svg = $s('#sn-wires');
    if (!svg) return;
    loadWires(hours).then(() => drawWires());
    drawWires();
  }
  let whDrawn = -1;
  function drawWires() {
    const svg = $s('#sn-wires'), d = wh.data;
    if (!svg || (d && whDrawn === wh.at)) return;   // drawn once per load (the page renders every second)
    if (d) whDrawn = wh.at;
    if (!d || !d.wires.length) {
      svg.setAttribute('viewBox', '0 0 1000 40');
      svg.replaceChildren(sv('text', { x: 10, y: 24, class: 'sn-tlbl' }, document.createTextNode(d ? 'No link history yet.' : 'Loading the link history… (a few seconds on the Pi)')));
      return;
    }
    const wires = d.wires.filter((w) => w.cells.some((c) => c !== null && c > 0));
    const W = 1000, LW = 250, RH = 13, top = 6;
    const rows = [{ name: 'House (the alert verdict)', cells: d.house || [], house: true }, ...wires.map((w) => ({ name: `${w.board_name} → ${w.far}`, cells: w.cells, board: w.board }))];
    const H = top + rows.length * RH + 24;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const cw = (W - LW - 10) / d.n;
    const g = sv('g');
    let lastBoard = null;
    rows.forEach((r, i) => {
      const y = top + i * RH;
      if (r.board && r.board !== lastBoard) { g.append(sv('line', { x1: 0, x2: W, y1: y - 0.5, y2: y - 0.5, class: 'sn-wsep' })); lastBoard = r.board; }
      g.append(sv('text', { x: 4, y: y + RH - 3, class: 'sn-tlbl' }, document.createTextNode(r.name.length > 40 ? r.name.slice(0, 39) + '…' : r.name)));
      r.cells.forEach((c, j) => {
        if (c === null) return;
        const x = LW + j * cw;
        const t0 = d.t0 + j * d.bin;
        g.append(sv('rect', { x, y: y + 1, width: Math.max(cw - 0.3, 0.6), height: RH - 2, class: c > 0 ? 'sn-cell' : 'sn-cell none', style: c > 0 ? `opacity:${0.12 + 0.88 * c}` : null },
          sv('title', {}, document.createTextNode(`${r.name} · ${hhmm(t0)}–${hhmm(t0 + d.bin)}: ${Math.round(100 * c)}% of the time above its threshold`))));
      });
    });
    const t1 = d.t0 + d.n * d.bin, hours = (t1 - d.t0) / 3600;
    const step = hours <= 6 ? 1 : hours <= 24 ? 3 : hours <= 72 ? 12 : 24;
    for (const [t, hr] of hourMarks(d.t0 + 1, t1 - 1)) {
      if (hr % step) continue;
      const x = LW + (t - d.t0) / d.bin * cw;
      g.append(sv('line', { x1: x, x2: x, y1: top, y2: H - 18, class: 'sn-tick' }));
      g.append(sv('text', { x, y: H - 5, 'text-anchor': 'middle', class: 'sn-tlbl' },
        document.createTextNode(hr === 0 ? dayLabel(t) : `${String(hr).padStart(2, '0')}:00`)));
    }
    svg.replaceChildren(g);
  }

  // ------------------------------------------------------------ routine: hour x day per board
  const rt = { at: 0, data: null, loading: false };
  async function routine() {
    const box = $s('#sn-routine');
    if (!box) return;
    if (Date.now() - rt.at > 600000 && !rt.loading) {
      rt.loading = true;
      try {
        const r = await fetch('api/timeline?hours=168', { headers: { 'X-Ackwave': '1' } });
        if (!r.ok) throw new Error(String(r.status));
        rt.data = await r.json();
        rt.at = Date.now();
      } catch (e) {
        rt.at = Date.now() - 540000;               // try again in a minute
        if (!rt.data) box.replaceChildren(el('p', { className: 'muted', textContent: "Couldn't load the last 7 days; trying again in a minute." }));
      }
      rt.loading = false;
    }
    const d = rt.data;
    if (!d || !Array.isArray(d.lanes) || box.dataset.drawn === String(rt.at)) return;
    box.dataset.drawn = String(rt.at);
    const lanes = d.lanes.filter((l) => l.key === 'house' || l.key === 'board' || (l.key.startsWith('sat:') && !l.key.includes('>')));
    // the last 7 days as the PC's clock counts them (a DST day has 23 or 25 hours)
    const days = [];
    for (let t = d.t1; days.length < 7 && t > d.t1 - 8 * 86400; t -= 3600) {
      const k = dayKey(t);
      if (!days.some((x) => x.k === k)) days.unshift({ k, t });
    }
    const label = (l) => (l.key === 'board' ? 'Hub' : l.name);
    box.replaceChildren(...lanes.map((l) => {
      const m = days.map(() => new Array(24).fill(0));
      for (const [a, b, k] of d.spans) {
        if (k !== l.key) continue;
        for (let t = a; t < b;) {
          const e = Math.min(b, (Math.floor(t / 900) + 1) * 900);   // quarter hours: right in half-hour zones too
          const z = zpart(t), di = days.findIndex((x) => x.k === `${z.y}-${z.mo}-${z.d}`);
          if (di >= 0) m[di][z.h] += (e - t) / 60;
          t = e;
        }
      }
      const W = 330, LW = 46, cw = (W - LW) / 24, rh = 14, H = days.length * rh + 16;
      const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'sn-strips' });
      m.forEach((row, i) => {
        svg.append(sv('text', { x: 0, y: i * rh + 11, class: 'sn-tlbl' }, document.createTextNode(dayLabel(days[i].t))));
        row.forEach((mins, h) => svg.append(sv('rect', { x: LW + h * cw, y: i * rh, width: cw - 1, height: rh - 2, class: mins > 0.05 ? 'sn-cell' : 'sn-cell none',
          style: mins > 0.05 ? `opacity:${0.15 + 0.85 * Math.min(1, mins / 20)}` : null },
          sv('title', {}, document.createTextNode(`${label(l)}, ${dayLabel(days[i].t)} ${String(h).padStart(2, '0')}:00: ${Math.round(mins)} min of movement`)))));
      });
      for (const h of [0, 6, 12, 18]) svg.append(sv('text', { x: LW + h * cw, y: H - 2, class: 'sn-tlbl' }, document.createTextNode(`${h}h`)));
      return el('div', {}, el('h3', { textContent: label(l) }), svg);
    }));
  }

  function bindLog() {
    const h = $s('#sn-f-hours'), k = $s('#sn-f-key'), st = $s('#sn-f-state');
    if (!h || h.dataset.bound) return;
    h.dataset.bound = '1';
    h.onchange = () => { LOG.hours = Number(h.value) || 24; load(LOG.hours).then(() => logPage(slow)); };
    k.onchange = () => { LOG.key = k.value; logPage(slow); };
    st.onchange = () => { LOG.state = st.value; logPage(slow); };
  }

  window.Sensors = { cards, homeLog, logPage, bindLog, quality };
}());
