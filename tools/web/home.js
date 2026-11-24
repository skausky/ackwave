/* Home: the whole house at a glance, with no map to set up.
   - one sentence: by your boards, away from them (routers / between), or quiet
   - a card per board: ONE state (moving / someone still / empty / quiet), from
     everything that board has (its verdict, every wire, its learned model,
     breathing), so a card never says 'still' while one of its wires moves
   - the web: every wire between your boards and the routers / devices, lit
     where the movement is along it (wires.py: from which wires share an end)
   - alerts: rules by board (one, some, all), where, and what they do
   Uses app.js's helpers (el, cmd, fmtSpan, slow). Text only, never markup: names come
   from the network. */
(function () {
  'use strict';
  const SVG = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs = {}, ...kids) => {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) e.setAttribute(k, v);
    for (const k of kids) if (k) e.append(k);
    return e;
  };
  const $h = (s) => document.querySelector(s);
  const WORD = { moving: 'Moving', still: 'Someone still', empty: 'Empty', quiet: 'Quiet', calibrating: 'Calibrating', gone: 'Not heard' };
  const HELP = {
    moving: 'movement on this board\'s wires',
    still: 'someone there, not moving (learned model or breathing)',
    empty: 'its learned model says nobody is there',
    quiet: 'no movement; no learned model to say whether someone is there',
    calibrating: 'measuring the quiet level: keep still for ~10 s',
    gone: 'no report reaches the hub: power, range?',
  };
  let editing = null;                  // the rule being edited (a copy), or null

  // ---------------------------------------------------------------- headline
  function renderHead(w, bs) {
    const box = $h('#ov-head');
    const side = w ? w.side : 'quiet';
    box.className = `card ov-head ${side}`;
    const moving = bs.filter((b) => b.state === 'moving');
    const sub = !w ? 'waiting for the boards…'
      : w.words.length ? w.words[0].text
      : moving.length ? `${moving.map((b) => b.name).join(', ')} moving`
      : `${bs.filter((b) => b.state !== 'gone').length} of ${bs.length} boards reporting`;
    box.replaceChildren(
      el('div', { className: 'ov-head-dot' }),
      el('div', {}, el('div', { className: 'ov-head-text', textContent: w ? w.headline : 'Starting…' }),
        el('div', { className: 'ov-head-sub', textContent: sub })),
      el('div', { className: 'ov-head-more' }, ...((w && w.words.slice(1, 4)) || []).map((x) => el('div', { textContent: x.text }))));
  }

  // ---------------------------------------------------------------- boards
  function boardCard(b) {
    const nowS = Date.now() / 1000;
    const lines = [];
    if (b.state === 'moving') {
      lines.push(b.by.length ? `on: ${b.by.join(', ')}${b.own && !b.by.length ? '' : ''}` : 'its own router link');
      if (b.near_me) lines.push('right by this board (most of its wires)');
    }
    if (b.model) {
      const p = b.p || [0, 0, 0];
      lines.push(`learned model: ${b.learned} (empty ${p[0]}% · still ${p[1]}% · moving ${p[2]}%)`);
    } else if (!['gone', 'calibrating'].includes(b.state)) lines.push('no learned model on this board: empty vs still unknown');
    if (b.breathing && b.breathing !== 'none') lines.push(`breathing: ${b.breathing}`);
    const wires = (b.wires || []).map((x) => el('span', { className: `ov-wchip ${x.state}`, title: `level ${x.level} (1 = its threshold)`, textContent: x.to }));
    const health = b.state === 'gone' ? 'not heard' : `${b.acks}/s from its router · ${b.router_rssi || '–'} dBm${b.weak ? ' · weak link' : ''}${b.via === 'usb' ? ' · on USB' : ''}`;
    return el('div', { className: `ov-board ${b.state}`, title: HELP[b.state] || '' },
      el('div', { className: 'ov-board-top' },
        el('b', { textContent: b.name }),
        el('span', { className: 'ov-state', textContent: WORD[b.state] || b.state })),
      el('div', { className: 'ov-since muted small', textContent: b.since ? `for ${fmtSpan(nowS - b.since)}` : '' }),
      ...lines.map((t) => el('div', { className: 'small', textContent: t })),
      wires.length ? el('div', { className: 'ov-wchips' }, ...wires) : null,
      el('div', { className: 'muted small ov-health', textContent: health }));
  }

  // ---------------------------------------------------------------- the web
  // Boards along the bottom (your side), the routers and devices along the top
  // (over there), each far end above the boards that reach it (fewest crossings).
  // Nothing has to be placed: this is the wires and what they share, not a map.
  function layout(w, bs) {
    const ends = w.ends || {};
    const boards = bs.map((b) => b.key).filter((k) => ends[k] || bs.length);
    const far = Object.keys(ends).filter((k) => (ends[k] || {}).kind !== 'board');
    const W = 1000, bx = new Map(), fx = new Map();
    boards.forEach((k, i) => bx.set(k, (i + 0.5) * W / Math.max(1, boards.length)));
    const bary = (k) => {
      const xs = w.wires.filter((x) => x.a === k || x.b === k).map((x) => bx.get(x.a === k ? x.b : x.a)).filter((v) => v !== undefined);
      return xs.length ? xs.reduce((s, v) => s + v, 0) / xs.length : W / 2;
    };
    far.sort((a, b) => bary(a) - bary(b) || a.localeCompare(b));
    far.forEach((k, i) => fx.set(k, (i + 0.5) * W / Math.max(1, far.length)));
    const pos = (k) => (bx.has(k) ? [bx.get(k), 330] : fx.has(k) ? [fx.get(k), 70] : null);
    return { W, H: 420, pos, boards, far };
  }
  function renderWeb(w, bs) {
    const svg = $h('#ov-web');
    if (!w || !w.wires.length) {
      svg.replaceChildren(sv('text', { x: 500, y: 210, 'text-anchor': 'middle', class: 'ov-empty' },
        document.createTextNode(bs.length ? 'No wires yet: the boards report their links every few seconds' : 'Waiting for the boards…')));
      return;
    }
    const L = layout(w, bs);
    const g = sv('g');
    const bstate = Object.fromEntries(bs.map((b) => [b.key, b]));
    const nowS = Date.now() / 1000;
    for (const x of w.wires) {
      const p = L.pos(x.a), q = L.pos(x.b);
      if (!p || !q) continue;
      const lit = x.state === 'moving' ? 1 : x.state === 'recent' ? Math.max(0.25, 1 - (x.ago || 0) / 6) : 0;
      // two boards: an arc below them (your side); else a straight wire up
      const both = (w.ends[x.a] || {}).kind === 'board' && (w.ends[x.b] || {}).kind === 'board';
      const d = both ? `M${p[0]},${p[1]} Q${(p[0] + q[0]) / 2},${p[1] + 80} ${q[0]},${q[1]}` : `M${p[0]},${p[1]} L${q[0]},${q[1]}`;
      g.append(sv('path', { d, class: `ov-wire ${x.state}${both ? ' side' : ''}`, style: lit ? `opacity:${0.35 + 0.65 * lit}` : null },
        sv('title', {}, document.createTextNode(`${(w.ends[x.a] || {}).name} – ${(w.ends[x.b] || {}).name}: ${x.state}, level ${x.level}`))));
      if (lit && x.span && !both) {             // where on the wire: that stretch glows
        const [s0, s1] = x.span, at = (t) => [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t];
        const A = at(s0), B = at(s1);
        g.append(sv('line', { x1: A[0], y1: A[1], x2: B[0], y2: B[1], class: 'ov-span', style: `opacity:${lit}` }));
      }
    }
    for (const k of L.far) {
      const e = w.ends[k] || {}, [x, y] = L.pos(k);
      const grp = sv('g', { class: `ov-end ${e.kind}${e.hot ? ' hot' : ''}` });
      if (e.hot) grp.append(sv('circle', { cx: x, cy: y, r: 22, class: 'ov-halo' }));
      grp.append(e.kind === 'router' ? sv('circle', { cx: x, cy: y, r: 11 })
        : sv('rect', { x: x - 8, y: y - 8, width: 16, height: 16, rx: 3, transform: `rotate(45 ${x} ${y})` }));
      grp.append(sv('text', { x, y: y - 20, 'text-anchor': 'middle' }, document.createTextNode(e.name || k)));
      g.append(grp);
    }
    for (const k of L.boards) {
      const b = bstate[k] || {}, e = w.ends[k] || {}, p = L.pos(k);
      if (!p) continue;
      const [x, y] = p;
      const grp = sv('g', { class: `ov-end board ${b.state || ''}${e.hot ? ' hot' : ''}` });
      if (e.hot || b.state === 'moving') grp.append(sv('circle', { cx: x, cy: y, r: 26, class: 'ov-halo' }));
      grp.append(sv('rect', { x: x - 15, y: y - 15, width: 30, height: 30, rx: 7 }));
      grp.append(sv('text', { x, y: y + 34, 'text-anchor': 'middle', class: 'nm' }, document.createTextNode(b.name || e.name || k)));
      grp.append(sv('text', { x, y: y + 51, 'text-anchor': 'middle', class: 'st' },
        document.createTextNode((WORD[b.state] || '') + (b.since && b.state === 'moving' ? '' : b.since ? ` · ${fmtSpan(nowS - b.since)}` : ''))));
      g.append(grp);
    }
    g.append(sv('text', { x: 8, y: 22, class: 'ov-lab' }, document.createTextNode('over there: routers and devices')));
    g.append(sv('text', { x: 8, y: 410, class: 'ov-lab' }, document.createTextNode('your boards')));
    svg.setAttribute('viewBox', `0 0 ${L.W} ${L.H}`);
    svg.replaceChildren(g);
  }

  // ---------------------------------------------------------------- alerts
  const needText = (r, n) => (r.need === 'all' ? (n > 1 ? `all ${n} of them` : '') : r.need === 'any' ? (n > 1 ? 'any of them' : '') : `at least ${r.need}`);
  const whereText = { any: 'anywhere on their wires', over: 'away from the boards (routers, devices, between)', yours: 'right by the boards' };
  function ruleText(r, names) {
    const who = r.boards.length ? r.boards.map((k) => names[k] || k).join(', ') : 'any board';
    const nt = needText(r, r.boards.length);
    return `${who}${nt ? ` · ${nt}` : ''} · ${whereText[r.where]}`;
  }
  function saveRules(list) { cmd({ cmd: 'rules', rules: list }); }
  function renderRules(R, bs) {
    const box = $h('#ov-rules');
    if (!R) return;
    const names = Object.fromEntries(bs.map((b) => [b.key, b.name]));
    const list = R.list || [];
    const rows = list.map((r, i) => {
      const st = (R.state || {})[r.id] || {};
      const flip = (k) => { const c = list.map((x) => ({ ...x })); c[i][k] = !c[i][k]; saveRules(c); };
      const act = (k, label, ready) => el('button', { type: 'button', className: `ov-act${r[k] ? ' on' : ''}`, title: ready === false ? `${label}: set it up in Settings first` : label,
        textContent: label + (ready === false && r[k] ? ' (not set up)' : ''), onclick: () => flip(k) });
      return el('div', { className: `ov-rule${st.on ? ' firing' : ''}${r.on ? '' : ' off'}` },
        el('label', { className: 'switch inline' }, el('input', { type: 'checkbox', checked: r.on, onchange: () => flip('on') }),
          el('span', { className: 'track' }, el('span', { className: 'knob' }))),
        el('div', { className: 'ov-rule-main' }, el('b', { textContent: r.name + (st.on ? ' · ON now' : '') }),
          el('div', { className: 'muted small', textContent: ruleText(r, names) })),
        el('div', { className: 'ov-acts' }, act('notify', 'Phone', R.notify_ready), act('light', 'Light', R.light_ready), act('buzz', 'Board LED + buzzer')),
        el('button', { type: 'button', className: 'ghost small', textContent: 'Edit', onclick: () => { editing = { ...r, boards: [...r.boards] }; renderEditor(bs, list); } }),
        el('button', { type: 'button', className: 'ghost small', textContent: '✕', title: 'Delete this alert',
          onclick: () => { if (confirm(`Delete the alert '${r.name}'?`)) saveRules(list.filter((x) => x.id !== r.id)); } }));
    });
    const add = el('button', { type: 'button', textContent: '+ Add an alert', onclick: () => {
      editing = { id: null, name: '', boards: [], need: 'any', where: 'any', notify: true, light: false, buzz: false, on: true };
      renderEditor(bs, list);
    } });
    const sig = JSON.stringify([list, R.state, R.notify_ready, R.light_ready, bs.map((b) => [b.key, b.name])]);
    if (box.dataset.sig === sig) return;        // a tap on a button being replaced is lost (phones)
    box.dataset.sig = sig;
    box.replaceChildren(...(rows.length ? rows : [el('p', { className: 'muted', textContent: 'No alerts: nothing notifies you or switches the light.' })]), add);
  }
  function renderEditor(bs, list) {
    const box = $h('#ov-editor');
    if (!editing) { box.hidden = true; box.replaceChildren(); return; }
    const r = editing;
    box.hidden = false;
    const boardsBox = el('div', { className: 'ov-ed-boards' }, ...bs.map((b) => el('label', {},
      el('input', { type: 'checkbox', checked: r.boards.includes(b.key), onchange: (e) => {
        r.boards = e.target.checked ? [...new Set([...r.boards, b.key])] : r.boards.filter((k) => k !== b.key);
      } }), el('span', { textContent: b.name }))));
    const sel = (opts, cur, on) => {
      const s = el('select', { onchange: (e) => on(e.target.value) });
      for (const [v, t] of opts) s.append(el('option', { value: v, textContent: t, selected: String(cur) === String(v) }));
      return s;
    };
    const needOpts = [['any', 'any one of them'], ['all', 'all of them (within 3 s)'], ...[2, 3, 4].filter((n) => n <= Math.max(2, bs.length)).map((n) => [n, `at least ${n}`])];
    box.replaceChildren(
      el('h3', { textContent: r.id ? 'Edit alert' : 'New alert' }),
      el('label', { className: 'field' }, el('span', { textContent: 'Name' }),
        el('input', { type: 'text', value: r.name, maxLength: 40, placeholder: 'e.g. Guest house', oninput: (e) => { r.name = e.target.value; } })),
      el('div', { className: 'field' }, el('span', { textContent: 'Boards (none ticked = any board)' }), boardsBox),
      el('label', { className: 'field' }, el('span', { textContent: 'How many must see it' }), sel(needOpts, r.need, (v) => { r.need = /^\d+$/.test(v) ? Number(v) : v; })),
      el('label', { className: 'field' }, el('span', { textContent: 'Where' }), sel(Object.entries(whereText), r.where, (v) => { r.where = v; })),
      el('div', { className: 'ov-ed-acts' }, ...[['notify', 'Phone notification'], ['light', 'Switch the light'], ['buzz', "Hub's LED + buzzer"]].map(([k, t]) =>
        el('label', {}, el('input', { type: 'checkbox', checked: r[k], onchange: (e) => { r[k] = e.target.checked; } }), el('span', { textContent: t })))),
      el('div', { className: 'ov-ed-btns' },
        el('button', { type: 'button', textContent: 'Save', onclick: async () => {
          const out = list.map((x) => ({ ...x }));
          const i = out.findIndex((x) => x.id === r.id);
          const rule = { ...r, name: r.name.trim() || 'Alert', id: r.id || `r${Date.now() % 1e9}` };
          if (i >= 0) out[i] = rule; else out.push(rule);
          if (await cmd({ cmd: 'rules', rules: out })) { editing = null; renderEditor(bs, list); }
        } }),
        el('button', { type: 'button', className: 'ghost', textContent: 'Cancel', onclick: () => { editing = null; renderEditor(bs, list); } })));
  }

  // ---------------------------------------------------------------- recent
  function renderRecent(evs) {
    const box = $h('#ov-recent');
    const keep = (evs || []).filter((e) => ['automation', 'motion', 'health'].includes(e.type)).slice(-10).reverse();
    box.replaceChildren(...(keep.length ? keep.map((e) => el('div', { className: `ov-ev ${e.type}${e.important ? ' imp' : ''}` },
      el('span', { className: 'muted', textContent: clock(e.t, true) }), el('span', { textContent: e.text })))
      : [el('p', { className: 'muted', textContent: 'Nothing yet.' })]));
  }

  window.Overview = {
    render() {
      if (!slow) return;
      const bs = slow.bstates || [], w = slow.wires || null;
      renderHead(w, bs);
      $h('#ov-boards').replaceChildren(...bs.map(boardCard));
      renderWeb(w, bs);
      renderRules(slow.rules, bs);
      renderRecent(slow.events);
    },
  };
}());
