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
  const SET_NAME = { SENS: 'sensitivity', LEDSET: 'LED', SOUND: 'buzzer', OUT: 'light output', DEVHZ: 'poke rate' };
  const STATE_ORDER = ['moving', 'still', 'empty', 'quiet', 'weak', 'calibrating', 'gone'];
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
    for (const w of (s ? s.why : []).slice(0, 3)) lines.push(el('div', { className: 'small', textContent: w }));
    if (state === 'moving' && b.near_me) lines.push(el('div', { className: 'small sn-where', textContent: 'right by this board (most of its links react)' }));
    else if (state === 'moving' && b.by && b.by.length) lines.push(el('div', { className: 'small sn-where', textContent: `along: ${b.by.join(', ')}` }));
    const qv = quality(b);
    const wires = (b.wires || []).map((x) => el('span', { className: `ov-wchip ${x.state}`, title: `level ${x.level} (1 = its threshold)`, textContent: x.to }));
    const sets = Object.entries(st || {}).map(([k, v]) => el('span', {
      className: `sn-set ${v.state}`, title: `${v.line}: ${v.state === 'done' ? 'confirmed by the board' : v.state === 'failed' ? 'the board never confirmed it' : 'sent, not confirmed yet'}`,
      textContent: `${v.state === 'done' ? '✓' : v.state === 'failed' ? '✗' : '…'} ${SET_NAME[k] || k}` }));
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
      sets.length ? el('div', { className: 'sn-sets' }, ...sets) : null,
      inflight);
  }

  function cards(sl) {
    const now = Object.fromEntries(((sl.sensors || {}).now || []).map((s) => [s.key, s]));
    const boards = Object.fromEntries((sl.boards || []).map((b) => [`sat:${b.mac}`, b]));
    return (sl.bstates || []).map((b) => {
      const mac = b.key.startsWith('sat:') ? b.key.slice(4) : null;
      const extra = mac ? boards[b.key] || {} : {};
      return card(Object.assign({}, b, { pokes: extra.pokes, policy: extra.policy }), now[b.key],
        mac ? (sl.sat_settings || {})[mac] : null, mac ? (sl.sat_cmds || {})[mac] : null);
    });
  }

  // ------------------------------------------------------------ the log
  let hist = { at: 0, hours: 12, log: [] }, loading = false;
  async function load(hours, force) {
    if (loading || (!force && hist.hours === hours && Date.now() - hist.at < 60000)) return;
    loading = true;
    try {
      const r = await fetch(`api/sensors?hours=${hours}`, { headers: { 'X-Ackwave': '1' } });
      if (r.ok) hist = Object.assign(await r.json(), { at: Date.now(), hours });
    } catch (e) { /* offline: keep the last */ }
    loading = false;
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
    const W = 1000, LW = 130, RH = 26, H = keys.length * RH + 22;
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
    const step = hours <= 6 ? 1 : hours <= 24 ? 3 : hours <= 72 ? 12 : 24;
    const d0 = new Date(t0 * 1000); d0.setMinutes(0, 0, 0);
    for (let t = d0.getTime() / 1000; t <= nowS; t += 3600) {
      const hr = new Date(t * 1000).getHours();
      if (t < t0 || hr % step) continue;
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
    const keys = new Map((sl.bstates || []).map((b) => [b.key, b.name]));
    const selK = $s('#sn-f-key');
    if (selK && selK.options.length !== keys.size + 1) {
      selK.replaceChildren(el('option', { value: '', textContent: 'every board' }),
        ...[...keys].map(([k, n]) => el('option', { value: k, textContent: n })));
      selK.value = LOG.key;
    }
    strips('#sn-log-strips', sl, LOG.hours);
    const list = periods(sl, LOG.hours).filter((e) => (!LOG.key || e.key === LOG.key)
      && (LOG.state === 'all' || (LOG.state === 'notquiet' ? e.state !== 'quiet' : e.state === LOG.state)));
    const tot = {};
    for (const e of list) tot[e.state] = (tot[e.state] || 0) + (e.t1 - e.t0);
    $s('#sn-log-sum').textContent = STATE_ORDER.filter((s) => tot[s]).map((s) => `${(WORD[s] || s).toLowerCase()} ${span(tot[s])}`).join(' · ') || 'nothing in this range';
    $s('#sn-log-list').replaceChildren(...(list.length ? list.slice(0, 400).map(row) : [el('p', { className: 'muted', textContent: 'Nothing in this range.' })]));
  }
  function bindLog() {
    const h = $s('#sn-f-hours'), k = $s('#sn-f-key'), st = $s('#sn-f-state');
    if (!h || h.dataset.bound) return;
    h.dataset.bound = '1';
    h.onchange = () => { LOG.hours = Number(h.value) || 24; load(LOG.hours, true).then(() => logPage(slow)); };
    k.onchange = () => { LOG.key = k.value; logPage(slow); };
    st.onchange = () => { LOG.state = st.value; logPage(slow); };
  }

  window.Sensors = { cards, homeLog, logPage, bindLog, quality };
}());
