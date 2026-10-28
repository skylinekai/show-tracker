'use strict';

// ---------------------------------------------------------------- helpers
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ''));
  return el;
}
const $app = document.getElementById('app');
let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.style.display = 'none'), 5000);
}
async function api(url, opts = {}) {
  opts.headers = { ...(opts.headers || {}), 'X-Requested-With': 'proposal-app' };
  opts.credentials = 'same-origin';
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) { const e = new Error((body && body.error) || `Request failed (${r.status})`); e.status = r.status; throw e; }
  return body;
}
const num = v => { const n = parseFloat(String(v ?? '').replace(/[^0-9.\-]/g, '')); return Number.isFinite(n) ? n : 0; };
const money = (v, cur) => `${v < 0 ? '-' : ''}${cur}${Math.abs(v).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
// money boxes: only digits and one decimal point can be typed, thousands commas appear as you type, and the box always settles as 1,234.50
const curSigns = [];
function fmtTyping(raw) {
  const t = String(raw).replace(/[^0-9.]/g, ''), i = t.indexOf('.');
  const int = (i < 0 ? t : t.slice(0, i)).replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const dec = i < 0 ? null : t.slice(i + 1).replace(/\./g, '').slice(0, 2);
  return dec === null ? int : `${int || '0'}.${dec}`;
}
const fmtFinal = raw => { const n = parseFloat(String(raw).replace(/,/g, '')); return Number.isFinite(n) ? n.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : ''; };
const setMoney = (input, v) => { input.value = v === '' || v == null ? '' : fmtFinal(v); };
function moneyBox(input, symbol) {
  input.setAttribute('inputmode', 'decimal'); input.setAttribute('autocomplete', 'off'); input.setAttribute('placeholder', '0.00');
  input.addEventListener('input', () => {
    const before = input.value.slice(0, input.selectionStart ?? input.value.length).replace(/[^0-9.]/g, '').length;
    const v = fmtTyping(input.value); input.value = v;
    let seen = 0, pos = 0; while (pos < v.length && seen < before) { if (/[0-9.]/.test(v[pos])) seen++; pos++; }
    try { input.setSelectionRange(pos, pos); } catch { /* not every browser allows it */ }
  });
  input.addEventListener('blur', () => { input.value = fmtFinal(input.value); });
  const sign = h('span', { class: 'cur-sign' }, symbol); curSigns.push(sign);
  return h('span', { class: 'money' }, sign, input);
}
const fmtSize = b => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');


// ---------------------------------------------------------------- page order: every page of a finished proposal, to drag into the order wanted
function pageOrderPanel(pid) {
  let pages = [], order = [], v = 0, dragK = null, removed = 0;
  const grid = h('div', { class: 'page-grid' }), msg = h('div');
  const el = h('div', { class: 'page-order' }, h('h3', {}, 'Page order'),
    h('p', { class: 'hint', style: 'margin:4px 0 10px' }, 'Drag the pages into the order you want, or use the arrows. Use ✕ to take a page out. The PDF is made in this order.'), msg, grid);
  const dirty = () => removed > 0 || order.some((n, k) => n !== k);
  function move(from, to) {
    if (to < 0 || to >= order.length || from === to) return;
    const [x] = order.splice(from, 1); order.splice(to, 0, x); draw();
  }
  function draw() {
    grid.replaceChildren(...order.map((n, k) => {
      const p = pages[n];
      const card = h('div', { class: 'page-card', draggable: 'true' },
        h('div', { class: 'page-thumb' }, h('img', { src: `/api/proposals/${pid}/page/${n}?v=${v}`, alt: '', draggable: 'false', loading: 'lazy' }), h('span', { class: 'page-num' }, String(k + 1))),
        h('div', { class: 'page-label', title: p.label }, p.label),
        h('div', { class: 'page-btns' },
          h('button', { type: 'button', class: 'btn link', disabled: k === 0, 'aria-label': `Move page ${k + 1} earlier`, onclick: () => move(k, k - 1) }, '◀'),
          h('button', { type: 'button', class: 'btn link', disabled: k === order.length - 1, 'aria-label': `Move page ${k + 1} later`, onclick: () => move(k, k + 1) }, '▶'),
          h('button', { type: 'button', class: 'btn link danger', disabled: order.length < 2, title: 'Delete this page', 'aria-label': `Delete page ${k + 1}`, onclick: () => {
            if (!confirm(`Delete page ${k + 1} (${p.label}) from this proposal?`)) return;
            order.splice(k, 1); removed++; draw(); } }, '✕')));
      card.addEventListener('dragstart', e => { dragK = k; card.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', String(k)); } catch { /* old browsers */ } });
      card.addEventListener('dragend', () => { dragK = null; grid.querySelectorAll('.page-card').forEach(c => c.classList.remove('dragging', 'over')); });
      card.addEventListener('dragover', e => { if (dragK !== null) { e.preventDefault(); card.classList.add('over'); } });
      card.addEventListener('dragleave', () => card.classList.remove('over'));
      card.addEventListener('drop', e => { e.preventDefault(); const f = dragK; dragK = null; if (f !== null) move(f, k); });
      return card;
    }));
  }
  async function load() {
    msg.replaceChildren(h('div', { class: 'msg info' }, 'Preparing the page previews… this can take up to a minute.'));
    try { const r = await api(`/api/proposals/${pid}/pages`); pages = r.pages; v = r.v; order = pages.map(p => p.n); removed = 0; msg.replaceChildren(); draw(); }
    catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
  }
  async function apply() {   // save a changed order; resolves when the proposal is in that order
    if (!pages.length || !dirty()) return;
    await api(`/api/proposals/${pid}/order`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order }) });
    await load();
  }
  load();
  return { el, apply };
}
async function downloadOrdered(panel, id, fmt, btn) {
  const label = btn.textContent; btn.disabled = true; btn.textContent = 'Putting the pages in order…';
  try { await panel.apply(); } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = label; return; }
  btn.disabled = false; btn.textContent = label;
  await download(id, fmt, btn);
}

function openOrderModal(id, title) {
  const panel = pageOrderPanel(id);
  const overlay = h('div', { class: 'overlay', onclick: e => { if (e.target === overlay) overlay.remove(); } },
    h('div', { class: 'modal wide' }, h('div', { class: 'modal-head' }, h('h2', {}, title), h('button', { class: 'btn ghost', onclick: () => overlay.remove() }, 'Close')),
      panel.el, h('div', { class: 'actions', style: 'margin-top:14px' },
        ...(user.is_admin ? [h('button', { class: 'btn', onclick: e => downloadOrdered(panel, id, 'pptx', e.target) }, 'Download PowerPoint')] : []),
        h('button', { class: user.is_admin ? 'btn ghost' : 'btn', onclick: e => downloadOrdered(panel, id, 'pdf', e.target) }, 'Download PDF'))));
  document.body.append(overlay);
}
async function download(id, fmt, btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = fmt === 'pdf' ? 'Preparing PDF…' : 'Downloading…';
  try {
    const r = await fetch(`/api/proposals/${id}/download/${fmt}`, { credentials: 'same-origin' });
    if (!r.ok) { let m = 'Download failed.'; try { m = (await r.json()).error || m; } catch { /* ignore */ } throw new Error(m); }
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)/.exec(cd);
    const a = h('a', { href: URL.createObjectURL(blob), download: m ? decodeURIComponent(m[1]) : `proposal.${fmt}` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = label; }
}

// ---------------------------------------------------------------- annotate a render: white labels and bright arrows on the page
const ANN_COLOURS = [['#000000', 'Black'], ['#FFFFFF', 'White']];
const PAGE_RATIO = 10691813 / 7559675;
function annotateRender(url, startNotes, onDone) {
  let notes = JSON.parse(JSON.stringify(startNotes || [])), sel = -1, colour = ANN_COLOURS[1][0];
  const stage = h('div', { class: 'ann-stage', style: `aspect-ratio:${PAGE_RATIO}` });
  const img = h('img', { src: url, alt: '', draggable: 'false', class: 'ann-img' });
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg'); svg.setAttribute('class', 'ann-svg');
  const layer = h('div', { class: 'ann-labels' });
  stage.append(img, svg, layer);
  const text = h('textarea', { rows: '2', maxlength: '160', placeholder: 'Label text', 'aria-label': 'Label text', class: 'ann-text' });
  const textRow = h('div', { class: 'ann-textrow', style: 'display:none' }, text);
  const del = h('button', { type: 'button', class: 'btn ghost', disabled: true, onclick: () => remove() }, 'Delete');
  const swatches = ANN_COLOURS.map(([c, name]) => h('button', { type: 'button', class: 'ann-sw', title: name, 'aria-label': name, style: `background:${c}`, onclick: () => { colour = c; if (sel >= 0) notes[sel].c = c; draw(); } }));
  const hint = h('div', { class: 'hint', style: 'margin-top:8px' }, 'Add a label or a line, then drag it into place. Drag the ends of a line to point it. Click a label to change its words.');
  const clamp = v => Math.min(1, Math.max(0, v));
  function remove() { if (sel < 0) return; notes.splice(sel, 1); sel = -1; draw(); }
  function select(i) { sel = i; draw(); if (i >= 0 && notes[i].t === 'label') { text.value = notes[i].text; } }
  function drag(ev, onMove) {
    ev.preventDefault(); ev.stopPropagation();
    const r = stage.getBoundingClientRect(), x0 = ev.clientX, y0 = ev.clientY;
    const mv = e => onMove((e.clientX - x0) / r.width, (e.clientY - y0) / r.height);
    const up = () => { document.removeEventListener('pointermove', mv); document.removeEventListener('pointerup', up); document.removeEventListener('pointercancel', up); };
    document.addEventListener('pointermove', mv); document.addEventListener('pointerup', up); document.addEventListener('pointercancel', up);
  }
  function draw() {
    const W = stage.clientWidth || 800, H = W / PAGE_RATIO, sw = 1.5 / 841.9 * W;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.replaceChildren(); layer.replaceChildren();
    notes.forEach((n, i) => {
      if (n.t === 'arrow') {
        const x1 = n.x1 * W, y1 = n.y1 * H, x2 = n.x2 * W, y2 = n.y2 * H;
        const g = document.createElementNS(svgNS, 'g'); g.setAttribute('class', 'ann-arrow' + (i === sel ? ' sel' : ''));
        const mk = (tag, at) => { const e = document.createElementNS(svgNS, tag); for (const k in at) e.setAttribute(k, at[k]); g.append(e); return e; };
        // a fine, flowing line from the label to the feature, with a gentle S in it and no head (same shape is drawn in the PDF/PowerPoint)
        const L = Math.hypot(x2 - x1, y2 - y1) || 1, ux = (x2 - x1) / L, uy = (y2 - y1) / L, vx = -uy, vy = ux;
        const c1 = [x1 + ux * L * 0.3 + vx * L * 0.16, y1 + uy * L * 0.3 + vy * L * 0.16], c2 = [x2 - ux * L * 0.3 - vx * L * 0.16, y2 - uy * L * 0.3 - vy * L * 0.16];
        const f = p => p[0].toFixed(1) + ' ' + p[1].toFixed(1);
        const dPath = `M${x1.toFixed(1)} ${y1.toFixed(1)} C${f(c1)} ${f(c2)} ${x2.toFixed(1)} ${y2.toFixed(1)}`;
        // an open, slightly uneven arrowhead at the far end, following the way the line is travelling
        let tx = x2 - c2[0], ty = y2 - c2[1]; const tl = Math.hypot(tx, ty) || 1; tx /= tl; ty /= tl;
        const hl = Math.min(Math.max(0.2 * L, 0.022 * W), 0.04 * W, L * 0.6), rot = (a, k) => [-tx * Math.cos(a) + ty * Math.sin(a) * k, -ty * Math.cos(a) - tx * Math.sin(a) * k];
        const lft = rot(0.5, -1), rgt = rot(0.5, 1);
        const pl = [x2 + lft[0] * hl, y2 + lft[1] * hl], pr = [x2 + rgt[0] * hl * 0.85, y2 + rgt[1] * hl * 0.85];
        mk('path', { d: dPath + ` M${f(pl)} L${x2.toFixed(1)} ${y2.toFixed(1)} L${f(pr)}`, fill: 'none', stroke: n.c, 'stroke-width': sw, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
        const hit = mk('path', { d: dPath, fill: 'none', stroke: 'transparent', 'stroke-width': Math.max(18, sw * 4), class: 'ann-hit' });
        hit.addEventListener('pointerdown', ev => { select(i); const a0 = { ...n }; drag(ev, (dx, dy) => {
          const mx = Math.max(-Math.min(a0.x1, a0.x2), Math.min(1 - Math.max(a0.x1, a0.x2), dx)), my = Math.max(-Math.min(a0.y1, a0.y2), Math.min(1 - Math.max(a0.y1, a0.y2), dy));
          n.x1 = a0.x1 + mx; n.x2 = a0.x2 + mx; n.y1 = a0.y1 + my; n.y2 = a0.y2 + my; draw(); }); });
        if (i === sel) [[1, x1, y1], [2, x2, y2]].forEach(([end, hx, hy]) => {
          const hd = mk('circle', { cx: hx, cy: hy, r: 9, fill: '#fff', stroke: n.c, 'stroke-width': 3, class: 'ann-handle' });
          hd.addEventListener('pointerdown', ev => { const p0 = [n['x' + end], n['y' + end]]; drag(ev, (dx, dy) => { n['x' + end] = clamp(p0[0] + dx); n['y' + end] = clamp(p0[1] + dy); draw(); }); });
        });
        svg.append(g);
      } else {
        const el = h('div', { class: 'ann-label' + (i === sel ? ' sel' : '') }, n.text);
        el.style.left = n.x * 100 + '%'; el.style.top = n.y * 100 + '%'; el.style.color = n.c || '#000000';
        el.addEventListener('pointerdown', ev => { ev.preventDefault(); ev.stopPropagation(); select(i);
          const me = layer.children[notes.slice(0, i).filter(x => x.t === 'label').length]; if (!me) return;   // the screen was redrawn: find this label again
          const p0 = [n.x, n.y], bw = me.offsetWidth / W, bh = me.offsetHeight / H;
          drag(ev, (dx, dy) => { n.x = Math.min(1 - bw, Math.max(0, p0[0] + dx)); n.y = Math.min(1 - bh, Math.max(0, p0[1] + dy)); me.style.left = n.x * 100 + '%'; me.style.top = n.y * 100 + '%'; }); });
        layer.append(el);
      }
    });
    del.disabled = sel < 0; textRow.style.display = sel >= 0 && notes[sel].t === 'label' ? '' : 'none';
    swatches.forEach((b, k) => b.classList.toggle('on', ANN_COLOURS[k][0] === (sel >= 0 ? (notes[sel].c || '#000000') : colour)));
  }
  text.addEventListener('input', () => { if (sel >= 0 && notes[sel].t === 'label') { notes[sel].text = text.value || ' '; const el = layer.children[notes.slice(0, sel).filter(x => x.t === 'label').length]; if (el) el.textContent = notes[sel].text; } });
  const addLabel = () => { notes.push({ t: 'label', x: 0.38, y: 0.42, text: 'Label', c: colour }); select(notes.length - 1); text.focus(); text.select(); };
  const addArrow = () => { notes.push({ t: 'arrow', x1: 0.40, y1: 0.35, x2: 0.55, y2: 0.50, c: colour }); select(notes.length - 1); };
  const close = ok => { document.removeEventListener('keydown', key); overlay.remove(); if (ok) onDone(notes.filter(n => n.t !== 'label' || n.text.trim())); };
  const key = e => { if ((e.key === 'Delete' || e.key === 'Backspace') && sel >= 0 && document.activeElement !== text) { e.preventDefault(); remove(); } else if (e.key === 'Escape') close(false); };
  document.addEventListener('keydown', key);
  stage.addEventListener('pointerdown', ev => { if (ev.target === stage || ev.target === img || ev.target === svg) select(-1); });
  // clicking anywhere that isn't a control or a mark lets go of the selected line or label
  document.addEventListener('pointerdown', function away(ev) {
    if (!overlay.isConnected) { document.removeEventListener('pointerdown', away); return; }
    if (ev.target.closest && ev.target.closest('button, input, textarea, select, .ann-label, .ann-hit, .ann-handle, .ann-textrow')) return;
    select(-1);
  });
  const overlay = h('div', { class: 'overlay top', onclick: e => { if (e.target === overlay) close(false); } },
    h('div', { class: 'modal wide ann-modal' }, h('div', { class: 'modal-head' }, h('h2', {}, 'Add labels and lines'),
      h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn ghost', onclick: () => close(false) }, 'Cancel'), h('button', { type: 'button', class: 'btn', onclick: () => close(true) }, 'Done'))),
      h('div', { class: 'ann-tools' }, h('button', { type: 'button', class: 'btn ghost', onclick: addLabel }, '+ Label'), h('button', { type: 'button', class: 'btn ghost', onclick: addArrow }, '+ Line'),
        h('span', { class: 'ann-swatches', role: 'group', 'aria-label': 'Arrow colour' }, ...swatches), del), textRow, stage, hint));
  document.body.append(overlay);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => draw()).observe(stage);
  img.addEventListener('load', draw); draw();
}

// ---------------------------------------------------------------- file picker with ordering
// a picture shown large over the page; click anywhere or press Esc to close
function viewLarge(url, name = '') {
  const onKey = e => { if (e.key === 'Escape') close(); };
  const close = () => { o.remove(); document.removeEventListener('keydown', onKey); };
  const o = h('div', { class: 'overlay top', onclick: close }, h('div', { class: 'lightbox' }, h('img', { src: url, alt: name }), h('div', {}, name)));
  document.addEventListener('keydown', onKey);
  document.body.append(o);
}

function fileField({ label, hint, multiple = false, required = false, pdf = true, onChange = null, firstPage = false, hideDrop = false, annotate = false }) {
  let items = [];
  const input = h('input', { type: 'file', hidden: true, accept: pdf ? 'image/*,.pdf' : 'image/*', ...(multiple ? { multiple: true } : {}) });
  const list = h('ul', { class: 'files' });
  let open = false;   // the list of pages is collapsed until asked for, so a long PDF doesn't make the form very long
  const toggle = h('button', { type: 'button', class: 'btn ghost files-toggle', 'aria-expanded': 'false', onclick: () => { open = !open; sync(); } });
  function sync() {
    const show = !multiple || open;
    list.style.display = show && items.length ? '' : 'none';
    toggle.style.display = multiple && items.length ? '' : 'none';
    toggle.setAttribute('aria-expanded', String(open));
    toggle.textContent = `${open ? '▾ Hide' : '▸ Show'} ${items.length} page${items.length === 1 ? '' : 's'}`;
  }
  const drop = h('div', { class: 'drop', tabindex: '0', role: 'button' }, h('strong', {}, multiple ? 'Choose files' : 'Choose file'), ' or drop here');
  function draw() {
    list.replaceChildren(...items.map((it, i) => h('li', {},
      it.file.type.startsWith('image/') ? h('img', { class: 'thumb zoomable', src: it.url, alt: '', title: 'Click to view large', tabindex: '0', role: 'button', onclick: () => viewLarge(it.url, it.file.name),
        onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); viewLarge(it.url, it.file.name); } } }) : h('span', { class: 'thumb' }, 'PDF'),
      h('span', { class: 'nm' }, it.file.name, h('small', {}, fmtSize(it.file.size))),
      multiple && items.length > 1 ? [
        h('button', { type: 'button', title: 'Move up', disabled: i === 0, onclick: () => move(i, -1) }, '▲'),
        h('button', { type: 'button', title: 'Move down', disabled: i === items.length - 1, onclick: () => move(i, 1) }, '▼')] : '',
      annotate && it.url && it.file.type.startsWith('image/') ? h('button', { type: 'button', class: 'files-annot', title: 'Add labels and lines to this picture', onclick: () => annotateRender(it.url, it.notes, n => { it.notes = n; draw(); }) },
        it.notes && it.notes.length ? `Edit labels (${it.notes.length})` : 'Add labels') : '',
      h('button', { type: 'button', title: 'Remove', onclick: () => remove(i) }, '✕'))));
    drop.style.display = hideDrop || (!multiple && items.length) ? 'none' : '';
    sync();
    if (onChange) onChange();
  }
  const note = h('div');
  const isPdf = f => /\.pdf$/i.test(f.name) || f.type === 'application/pdf';
  const b64File = (b64, name) => { const bin = atob(b64); const bytes = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i); return new File([bytes], name, { type: 'image/jpeg' }); };
  async function add(files) {
    for (const f of files) {
      if (multiple && pdf && isPdf(f)) {   // a PDF becomes one picture per page, each of which can be moved or removed
        note.replaceChildren(h('div', { class: 'msg info' }, `Splitting ${f.name} into pages\u2026 a big PDF can take a minute.`));
        try {
          const fd = new FormData(); fd.append('file', f);
          const r = await api('/api/split-pdf', { method: 'POST', body: fd });
          r.pages.forEach(p => { const pf = b64File(p.data, p.name); items.push({ file: pf, url: URL.createObjectURL(pf) }); });
          note.replaceChildren(r.note ? h('div', { class: 'msg info' }, r.note) : '');
        } catch (e) {
          items.push({ file: f, url: '' });   // keep the PDF as it is; the server will still split it
          note.replaceChildren(h('div', { class: 'msg error' }, `${f.name}: ${e.message} It will be split when the proposal is built.`));
        }
        draw(); continue;
      }
      if (!multiple && firstPage && pdf && isPdf(f)) {   // a PDF plan: its first page is used
        note.replaceChildren(h('div', { class: 'msg info' }, `Reading ${f.name}\u2026`));
        try {
          const fd = new FormData(); fd.append('file', f); fd.append('first', '1');
          const r = await api('/api/split-pdf', { method: 'POST', body: fd });
          const pf = b64File(r.pages[0].data, r.pages[0].name);
          items.forEach(i => URL.revokeObjectURL(i.url)); items = [{ file: pf, url: URL.createObjectURL(pf) }];
          note.replaceChildren(r.total > 1 ? h('div', { class: 'msg info' }, 'Only the first page of the PDF is used.') : '');
        } catch (e) { note.replaceChildren(h('div', { class: 'msg error' }, `${f.name}: ${e.message}`)); }
        draw(); continue;
      }
      if (!multiple) { items.forEach(i => URL.revokeObjectURL(i.url)); items = []; }
      items.push({ file: f, url: f.type.startsWith('image/') ? URL.createObjectURL(f) : '' });
    }
    draw();
  }
  function move(i, d) { [items[i], items[i + d]] = [items[i + d], items[i]]; draw(); }
  function remove(i) { URL.revokeObjectURL(items[i].url); items.splice(i, 1); draw(); }
  input.addEventListener('change', () => { add([...input.files]); input.value = ''; });
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); add([...e.dataTransfer.files]); });
  const el = h('div', { class: 'f' },
    label ? h('label', { class: 'f' }, label, required ? h('span', { class: 'req' }, ' *') : '', hint ? [' ', h('span', { class: 'hint' }, hint)] : '') : '',
    drop, input, note, toggle, list);
  sync();
  if (hideDrop) drop.style.display = 'none';
  return { el, pick() { input.click(); }, put(file, notes) { return add([file]).then(() => { if (notes && notes.length && items.length) { items[items.length - 1].notes = notes; draw(); } }); }, get notes() { return items.map(i => (i.notes && i.notes.length ? i.notes : null)); }, clear() { items.forEach(i => URL.revokeObjectURL(i.url)); items = []; draw(); }, get files() { return items.map(i => i.file); } };
}

// ---------------------------------------------------------------- show dates: two date pickers, always written the same way
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function formatDateRange(from, to) {   // '1 - 2 January 2027', '31 January - 1 February 2027', '31 December 2026 - 1 January 2027'
  const p = s => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || ''); return m ? { y: +m[1], m: +m[2] - 1, d: +m[3] } : null; };
  let a = p(from), b = p(to) || a;
  if (!a) return '';
  if (new Date(b.y, b.m, b.d) < new Date(a.y, a.m, a.d)) [a, b] = [b, a];
  if (a.y === b.y && a.m === b.m && a.d === b.d) return `${a.d} ${MONTHS[a.m]} ${a.y}`;
  if (a.y === b.y && a.m === b.m) return `${a.d} - ${b.d} ${MONTHS[a.m]} ${a.y}`;
  if (a.y === b.y) return `${a.d} ${MONTHS[a.m]} - ${b.d} ${MONTHS[b.m]} ${a.y}`;
  return `${a.d} ${MONTHS[a.m]} ${a.y} - ${b.d} ${MONTHS[b.m]} ${b.y}`;
}
function parseDateRange(text) {   // the reverse, for proposals opened for editing; null when it is in some other style
  const mi = n => MONTHS.findIndex(x => x.toLowerCase() === String(n).toLowerCase() || x.slice(0, 3).toLowerCase() === String(n).toLowerCase());
  const iso = (y, m, d) => (mi(m) < 0 ? null : `${y}-${String(mi(m) + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`);
  const t = String(text || '').trim().replace(/\s*[–—-]\s*/g, ' - ');
  let m;
  if ((m = /^(\d{1,2}) ([A-Za-z]+) (\d{4})$/.exec(t))) { const v = iso(m[3], m[2], m[1]); return v ? [v, v] : null; }
  if ((m = /^(\d{1,2}) - (\d{1,2}) ([A-Za-z]+) (\d{4})$/.exec(t))) { const a = iso(m[4], m[3], m[1]), b = iso(m[4], m[3], m[2]); return a && b ? [a, b] : null; }
  if ((m = /^(\d{1,2}) ([A-Za-z]+) - (\d{1,2}) ([A-Za-z]+) (\d{4})$/.exec(t))) { const a = iso(m[5], m[2], m[1]), b = iso(m[5], m[4], m[3]); return a && b ? [a, b] : null; }
  if ((m = /^(\d{1,2}) ([A-Za-z]+) (\d{4}) - (\d{1,2}) ([A-Za-z]+) (\d{4})$/.exec(t))) { const a = iso(m[3], m[2], m[1]), b = iso(m[6], m[5], m[4]); return a && b ? [a, b] : null; }
  return null;
}
function dateRangeField(label, { required = false, single = false } = {}) {
  let legacy = '';
  const typed = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || ''); return m ? `${m[3]}/${m[2]}/${m[1]}` : ''; };
  const toIso = t => {   // 'dd/mm/yyyy' (also d/m/yy, with - or . between) to 'yyyy-mm-dd', or null if it isn't a real date
    const m = /^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2}|\d{4})$/.exec(String(t).trim()); if (!m) return null;
    const y = m[3].length === 2 ? 2000 + +m[3] : +m[3], mo = +m[2], d = +m[1], dt = new Date(y, mo - 1, d);
    return dt.getFullYear() === y && dt.getMonth() === mo - 1 && dt.getDate() === d ? `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}` : null;
  };
  const mask = (el, ev) => {   // typing 31012027 (or 31/1/27) gives 31/01/2027: slashes are added as you type
    if (/^\d{3,8}$/.test(el.value)) {   // pasted or auto-filled digits, e.g. 01012027
      const d = el.value; el.value = d.length > 4 ? `${d.slice(0, 2)}/${d.slice(2, 4)}/${d.slice(4)}` : `${d.slice(0, 2)}/${d.slice(2)}`; return;
    }
    const parts = el.value.split(/[\/.\-]/).slice(0, 3).map((p, k) => p.replace(/\D/g, '').slice(0, k < 2 ? 2 : 4));
    let v = parts.join('/');
    const adding = !(ev && /^delete/.test(ev.inputType || ''));
    if (adding && parts.length < 3 && parts[parts.length - 1].length === 2) v += '/';
    el.value = v;
  };
  const preview = h('div', { class: 'hint date-preview' }), note = h('div', { class: 'hint' });
  const mk = (name, ph) => {
    const input = h('input', { type: 'text', inputmode: 'numeric', autocomplete: 'off', placeholder: ph, maxlength: '10', 'aria-label': `${label}: ${name}` });
    const cal = h('input', { type: 'date', tabindex: '-1', 'aria-hidden': 'true', class: 'date-native' });   // the calendar, for anyone who prefers to click
    const btn = h('button', { type: 'button', class: 'date-cal', title: 'Pick from a calendar', 'aria-label': `Open the calendar for ${name}` },
      (() => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', '18'); s.setAttribute('height', '18');
        s.innerHTML = '<g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></g>'; return s; })());
    btn.addEventListener('click', () => { cal.value = toIso(input.value) || ''; try { cal.showPicker(); } catch { cal.focus(); cal.click(); } });
    cal.addEventListener('change', () => { if (cal.value) { input.value = typed(cal.value); input.dispatchEvent(new Event('input')); input.dispatchEvent(new Event('change')); } });
    return { input, el: h('div', { class: 'date-box' }, input, btn, cal) };
  };
  const from = mk(single ? 'date' : 'first day', 'dd/mm/yyyy'), to = mk('last day', 'dd/mm/yyyy');
  const sync = (changed) => {
    if (single) to.input.value = from.input.value;
    const a = toIso(from.input.value), b = toIso(to.input.value);
    if (changed === from.input && a && (!to.input.value || !b || b < a)) { to.input.value = from.input.value; }
    const a2 = toIso(from.input.value), b2 = toIso(to.input.value);
    if (a2) { legacy = ''; note.textContent = ''; }
    const bad = el => el.classList.toggle('bad', !!el.value && !(el.value.length >= 8 && toIso(el.value)));
    if (document.activeElement !== from.input) bad(from.input);
    if (document.activeElement !== to.input) bad(to.input);
    const v = formatDateRange(a2, b2 || a2);
    preview.textContent = v ? `Shows as: ${v}` : '';
  };
  for (const f of [from, to]) {
    f.input.addEventListener('input', ev => { mask(f.input, ev); f.input.classList.remove('bad'); sync(f.input); });
    f.input.addEventListener('blur', () => sync(null));
    f.input.addEventListener('change', () => sync(f.input));
  }
  const el = h('div', { class: 'f date-range' }, h('label', { class: 'f' }, label, required ? h('span', { class: 'req' }, ' *') : ''),
    h('div', single ? { class: 'date-pair', style: 'grid-template-columns:1fr' } : { class: 'date-pair' }, h('div', {}, from.el), single ? '' : h('div', {}, to.el)), preview, note);
  return { el, get value() { return formatDateRange(toIso(from.input.value), toIso(to.input.value) || toIso(from.input.value)) || legacy; },
    set(text) { const r = parseDateRange(text); if (r) { from.input.value = typed(r[0]); to.input.value = typed(r[1]); legacy = ''; note.textContent = ''; }
      else { from.input.value = ''; to.input.value = ''; legacy = String(text || ''); note.textContent = legacy ? `Previously entered as "${legacy}". Type the dates above to change it.` : ''; } sync(null); } };
}

// stand size as two numbers and a unit, always written the same way: "8m x 8m"
function standField(label) {
  const mk = ph => h('input', { type: 'text', inputmode: 'decimal', placeholder: ph, autocomplete: 'off', 'aria-label': ph });
  const hi = mk('Width'), wi = mk('Depth');
  const unit = h('select', { 'aria-label': 'Unit' }, h('option', { value: 'm' }, 'm'), h('option', { value: 'ft' }, 'ft'));
  for (const i of [hi, wi]) i.addEventListener('input', () => { i.value = i.value.replace(/[^0-9.,]/g, '').replace(',', '.'); });
  const clean = v => String(v).trim().replace(/\.0+$/, '');
  const el = h('div', { class: 'f' }, label, h('div', { class: 'stand-row' }, hi, h('span', { class: 'stand-x' }, '\u00d7'), wi, unit));
  return {
    el, hi, wi, unit,
    get value() { const a = clean(hi.value), b = clean(wi.value); return a && b ? `${a}${unit.value} x ${b}${unit.value}` : ''; },
    set(v) {
      const m = /^\s*([\d.]+)\s*(m|ft)?\s*x\s*([\d.]+)\s*(m|ft)?/i.exec(String(v || ''));
      if (m) { hi.value = m[1]; wi.value = m[3]; unit.value = (m[4] || m[2] || 'm').toLowerCase(); } else { hi.value = ''; wi.value = ''; unit.value = 'm'; }
    },
  };
}

function textField(label, { required = false, hint = '', placeholder = '', type = 'text', value = '' } = {}) {
  const input = h('input', { type, placeholder, value, autocomplete: 'off' });
  const el = h('label', { class: 'f' }, label, required ? h('span', { class: 'req' }, ' *') : '', hint ? [' ', h('span', { class: 'hint' }, hint)] : '', input);
  return { el, input, get value() { return input.value.trim(); } };
}

// ---------------------------------------------------------------- login
function showLogin(message = '') {
  const email = textField('Email', { type: 'email' });
  const pw = textField('Password', { type: 'password' });
  const msg = h('div');
  if (message) msg.append(h('div', { class: 'msg info' }, message));
  const btn = h('button', { class: 'btn big', type: 'submit' }, 'Sign in');
  const form = h('form', { class: 'card login' }, h('div', { class: 'brand' }, h('img', { src: '/skyline-logo.png', alt: 'Skyline Whitespace', class: 'logo' })), h('p', { class: 'sub' }, 'Sign in to create proposals.'), msg,
    h('div', { class: 'grid', style: 'grid-template-columns:1fr' }, email.el, pw.el), h('p', {}, btn));
  form.addEventListener('submit', async e => {
    e.preventDefault(); btn.disabled = true; msg.replaceChildren();
    try { await api('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.value, password: pw.input.value }) }); boot(); }
    catch (ex) { msg.replaceChildren(h('div', { class: 'msg error' }, ex.message)); btn.disabled = false; }
  });
  $app.replaceChildren(form);
  email.input.focus();
}

// ---------------------------------------------------------------- main shell
let user = null, tab = 'new', editing = null;   // editing: the id of a proposal being changed
const feat = k => !!(user && user.features && user.features[k]);
function signOutIcon() {   // a small 'log out' icon (arrow leaving a door), shown instead of the words on phones
  const ns = 'http://www.w3.org/2000/svg', s = document.createElementNS(ns, 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'so-icon'); s.setAttribute('aria-hidden', 'true');
  s.innerHTML = '<g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></g>';
  return s;
}
function shell(content) {
  const tabs = h('nav', { class: 'tabs' },
    h('button', { class: tab === 'new' ? 'on' : '', onclick: () => { tab = 'new'; render(); } }, 'New Proposal'),
    h('button', { class: tab === 'list' ? 'on' : '', onclick: () => { tab = 'list'; render(); } }, 'Proposals'),
    user.is_admin && feat('mood_board') ? h('button', { class: tab === 'library' ? 'on' : '', onclick: () => { tab = 'library'; render(); } }, 'Library') : '',
    user.is_admin && feat('materials') ? h('button', { class: tab === 'materials' ? 'on' : '', onclick: () => { tab = 'materials'; render(); } }, 'Materials') : '',
    user.is_admin ? h('button', { class: tab === 'company' ? 'on' : '', onclick: () => { tab = 'company'; render(); } }, 'Company') : '',
    user.is_admin ? h('button', { class: tab === 'references' ? 'on' : '', onclick: () => { tab = 'references'; render(); } }, 'References') : '',
    user.is_admin ? h('button', { class: tab === 'terms' ? 'on' : '', onclick: () => { tab = 'terms'; render(); } }, 'Terms') : '',
    user.is_admin ? h('button', { class: tab === 'users' ? 'on' : '', onclick: () => { tab = 'users'; render(); } }, 'Users') : '',
    h('button', { class: tab === 'profile' ? 'on' : '', onclick: () => { tab = 'profile'; render(); } }, user.is_admin ? 'My details' : 'User'));
  $app.replaceChildren(
    h('header', { class: 'top' }, h('div', { class: 'wrap' }, h('div', { class: 'brand' }, h('img', { src: '/skyline-logo.png', alt: 'Skyline Whitespace', class: 'logo' }), h('h1', {}, 'Proposal Builder')),
      h('div', { class: 'who' }, h('span', { class: 'who-email' }, user.email),
        h('button', { class: 'btn ghost signout', title: `Sign out (${user.email})`, 'aria-label': 'Sign out', onclick: async () => { await api('/api/logout', { method: 'POST' }); user = null; tab = 'new'; showLogin(); } },
          h('span', { class: 'so-label' }, 'Sign out'), signOutIcon())))),
    h('div', { class: 'wrap' }, tabs, content));
}
function render() {
  if ((tab === 'library' && !feat('mood_board')) || (tab === 'materials' && !feat('materials'))) tab = 'new';
  tab === 'new' ? renderForm() : tab === 'profile' ? renderProfile() : tab === 'users' ? renderUsers() : tab === 'terms' ? renderTerms() : tab === 'library' ? renderLibrary('work') : tab === 'materials' ? renderLibrary('materials') : tab === 'company' ? renderLibrary('company') : tab === 'references' ? renderLibrary('references') : renderList(); }

// ---------------------------------------------------------------- proposals list
async function renderList() {
  const holder = h('div', {}, h('p', { class: 'hint' }, 'Loading…'));
  shell(holder);
  try {
    const rows = await api('/api/proposals');
    if (!rows.length) { holder.replaceChildren(h('div', { class: 'card' }, 'No proposals yet. Create one from the New Proposal tab.')); return; }
    const admin = user.is_admin;
    holder.replaceChildren(h('table', { class: 'list stack' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Proposal'), h('th', {}, 'Created'), h('th', {}, 'Total (ex VAT)'), h('th', {}, ''))),
      h('tbody', {}, rows.map(r => {
        const cur = r.summary.currency;
        const totals = Object.keys(r.totals).filter(k => k.startsWith('show')).map(k => `${(r.summary.shows || [])[+k.slice(4)] || 'Show'}: ${money(r.totals[k], cur)}`).concat(r.totals.grand != null ? [`Total: ${money(r.totals.grand, cur)}`] : []);
        const pptx = h('button', { class: 'btn ghost', onclick: e => download(r.id, 'pptx', e.target) }, 'PowerPoint');
        const pdf = h('button', { class: 'btn ghost', onclick: e => download(r.id, 'pdf', e.target) }, 'PDF');
        const mats = h('button', { class: 'btn ghost', onclick: () => { editing = r.id; tab = 'new'; render(); } }, 'Edit');
        const ord = h('button', { class: 'btn ghost', onclick: () => openOrderModal(r.id, r.title) }, 'Page order');
        const del = h('button', { class: 'btn link danger', onclick: async () => {
          if (!confirm('Delete this proposal and its files?')) return;
          try { await api(`/api/proposals/${r.id}`, { method: 'DELETE' }); renderList(); } catch (ex) { toast(ex.message); }
        } }, 'Delete');
        return h('tr', {},
          h('td', {}, h('strong', {}, r.title), h('div', { class: 'hint' }, `${r.summary.renders} render${r.summary.renders === 1 ? '' : 's'}${admin ? ' · ' + r.owner : ''}`)),
          h('td', {}, new Date(r.created).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })),
          h('td', {}, totals.length ? totals.map(t => h('div', {}, t)) : h('span', { class: 'hint' }, 'No quotation')),
          h('td', { style: 'white-space:nowrap' }, ...(user.is_admin ? [pptx, ' '] : []), pdf, ' ', ord, ' ', mats, ' ', del));
      }))));
  } catch (e) { holder.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
}


// ---------------------------------------------------------------- library picker + selection editor
// Drag a picture inside its frame to choose which part shows; use a slider to zoom (or hold Ctrl/⌘ and scroll). pos = [x, y, zoom], x and y from 0 to 1.
function attachPanZoom(d, img, arBox, pos, onDone) {
  const dims = () => {
    const arImg = (img.naturalWidth || 1) / (img.naturalHeight || 1);
    const hs = Math.max(1, arBox / arImg) * pos[2];
    return { hs, ws: hs * arImg };
  };
  const apply = () => {
    if (!img.naturalWidth) return;
    const { hs, ws } = dims();
    img.style.width = (ws / arBox * 100) + '%'; img.style.height = (hs * 100) + '%';
    img.style.left = (-(ws - arBox) * pos[0] / arBox * 100) + '%'; img.style.top = (-(hs - 1) * pos[1] * 100) + '%';
  };
  img.addEventListener('load', apply); apply();
  const setZoom = z => { pos[2] = Math.min(4, Math.max(1, z)); apply(); if (onDone) onDone(); };
  d.style.cursor = 'grab'; d.title = 'Drag to choose which part of the picture shows. Use the zoom slider to zoom (or hold Ctrl/⌘ and scroll).';
  d.addEventListener('wheel', e => { if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); setZoom(pos[2] * (e.deltaY < 0 ? 1.08 : 1 / 1.08)); }, { passive: false });   // only with Ctrl/⌘ held (or a trackpad pinch), so ordinary scrolling never zooms
  d.addEventListener('pointerdown', ev => {
    ev.preventDefault(); d.setPointerCapture(ev.pointerId); d.style.cursor = 'grabbing';
    const r = d.getBoundingClientRect(), { hs, ws } = dims();
    const ox = (ws - arBox) / arBox * r.width, oy = (hs - 1) * r.height, x0 = ev.clientX, y0 = ev.clientY, p0 = [...pos];
    const move = e => {
      pos[0] = ox > 1 ? Math.min(1, Math.max(0, p0[0] - (e.clientX - x0) / ox)) : p0[0];
      pos[1] = oy > 1 ? Math.min(1, Math.max(0, p0[1] - (e.clientY - y0) / oy)) : p0[1];
      apply();
    };
    const up = () => { d.removeEventListener('pointermove', move); d.removeEventListener('pointerup', up); d.style.cursor = 'grab'; if (onDone) onDone(); };
    d.addEventListener('pointermove', move); d.addEventListener('pointerup', up);
  });
  return { apply, setZoom };
}

// A live picture of the Materials & Furnishings page. With interactive=true, furniture and lights can be dragged and resized.
function materialsPreview(getBody, { interactive = false, touched = {}, hooks = null } = {}) {
  const box = h('div', { class: 'mp' + (interactive ? ' edit' : '') });
  const wrap = h('div', { class: 'mp-wrap' }, box, h('div', { class: 'mp-note hint' }, ''));
  // touched: id -> [x, y, w, h] for pieces moved or resized by hand (shared with whoever made the preview)
  let timer = null, seq = 0, selected = null, cropping = false;
  const zoomIn = h('input', { type: 'range', min: '1', max: '4', step: '0.05', value: '1', 'aria-label': 'Feature wall zoom', oninput: () => setZoom(parseFloat(zoomIn.value)) });
  const zoomRow = h('div', { class: 'mp-order', style: 'display:none' }, h('span', { class: 'hint' }, 'Feature wall zoom'), ' ', zoomIn);
  const cur = {}, els = {};   // id -> current box (with z), id -> element
  const bring = interactive ? h('div', { class: 'mp-order' }, h('button', { type: 'button', class: 'btn ghost', disabled: true, onclick: () => reorder(1) }, 'Bring forward'), ' ',
    h('button', { type: 'button', class: 'btn ghost', disabled: true, onclick: () => reorder(-1) }, 'Send back'), ' ', h('button', { type: 'button', class: 'btn ghost', disabled: true, onclick: () => flip() }, 'Flip horizontal'), ' ', h('button', { type: 'button', class: 'btn ghost mp-cropbtn', disabled: true, onclick: () => { cropping = !cropping; showSel(); } }, 'Crop'), ' ', h('button', { type: 'button', class: 'btn ghost mp-bgbtn', disabled: true, onclick: () => toggleBackground() }, 'Remove background'), ' ', h('span', { class: 'hint mp-sel' }, 'Click a piece to select it')) : '';
  function showSel() { if (!interactive) return; bring.querySelectorAll('button').forEach(b => { b.disabled = !selected; }); { const c = selected && cur[selected]; bring.querySelector('.mp-cropbtn').disabled = !(c && c.crop_ok); bring.querySelector('.mp-bgbtn').disabled = !(c && c.crop_ok); bring.querySelector('.mp-bgbtn').textContent = c && c.has_cut ? 'Restore background' : 'Remove background'; if (c && !c.crop_ok) cropping = false; } bring.querySelector('.mp-sel').textContent = selected && cur[selected] ? cur[selected].name : 'Click a piece to select it'; box.querySelectorAll('.mp-item').forEach(n => n.classList.toggle('sel', n.dataset.id === selected)); box.classList.toggle('cropping', cropping && !!selected); bring.querySelector('.mp-cropbtn').textContent = cropping && selected ? 'Done cropping' : 'Crop'; }
  if (bring) { wrap.insertBefore(bring, wrap.lastChild); wrap.insertBefore(zoomRow, wrap.lastChild); }
  if (interactive) {   // clicking anywhere else lets go of the selected piece
    const away = e => {
      if (!box.isConnected) { document.removeEventListener('pointerdown', away, true); return; }
      if (e.target.closest && (e.target.closest('.mp-item') || e.target.closest('.mp-order'))) return;
      if (selected) { selected = null; cropping = false; showSel(); }
    };
    document.addEventListener('pointerdown', away, true);
  }
  async function toggleBackground() {
    const c = cur[selected]; if (!c) return;
    const btn = bring.querySelector('.mp-bgbtn'); btn.disabled = true; btn.textContent = 'Working…';
    try {
      await api(`/api/library/${selected}/background`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ remove: !c.has_cut }) });
      await refresh();
    } catch (e) { toast(e.message); }
    showSel();
  }
  const pack = c => [c.x, c.y, c.w, c.h, c.z, c.flip ? 1 : 0, ...(c.crop || [0, 0, 0, 0])];
  function flip() {
    const c = cur[selected]; if (!c || !els[selected]) return;
    const pre = hooks ? hooks.snap() : null;
    c.flip = c.flip ? 0 : 1;
    els[selected].querySelector('.mp-clip').style.transform = c.flip ? 'scaleX(-1)' : '';
    touched[selected] = pack(c);
    if (hooks) hooks.push(pre);
  }
  function reorder(dir) {
    const ids = Object.keys(cur).sort((p, q) => cur[p].z - cur[q].z);
    const i = ids.indexOf(selected), j = i + dir;
    if (i < 0 || j < 0 || j >= ids.length) return;
    const pre = hooks ? hooks.snap() : null;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    ids.forEach((id, k) => { const c = cur[id]; c.z = k + 1; touched[id] = pack(c); if (els[id]) els[id].style.zIndex = c.z; });
    if (hooks) hooks.push(pre);
  }
  const pct = v => (v * 100).toFixed(3) + '%';
  function place(el, b) { el.style.left = pct(b.x); el.style.top = pct(b.y); el.style.width = pct(b.w); el.style.height = pct(b.h); }
  function movable(it, aspect) {
    const img = h('img', { src: it.url, alt: it.name, draggable: 'false' });
    const clip = h('div', { class: 'mp-clip' }, img);
    const el = h('div', { class: 'mp-item', title: it.name }, clip);
    const b = { x: it.x, y: it.y, w: it.w, h: it.h, z: it.z, name: it.name, flip: it.flip ? 1 : 0, crop_ok: !!it.crop_ok, has_cut: !!it.has_cut, crop:  (it.crop && it.crop.length === 4) ? [...it.crop] : [0, 0, 0, 0] };
    if (b.flip) clip.style.transform = 'scaleX(-1)';
    const showCrop = () => {   // the picture is drawn full size inside the box and the box hides the trimmed edges
      const [l, t, r, bt] = b.crop, fw = 1 - l - r, fh = 1 - t - bt;
      img.style.width = (100 / fw) + '%'; img.style.height = (100 / fh) + '%'; img.style.left = (-l / fw * 100) + '%'; img.style.top = (-t / fh * 100) + '%';
    };
    showCrop();
    cur[it.id] = b; els[it.id] = el; el.dataset.id = it.id; el.style.zIndex = b.z;
    place(el, b);
    if (!interactive) return el;
    const handles = ['tl', 'tr', 'bl', 'br'].map(k => h('span', { class: 'mp-handle ' + k, title: 'Drag to resize' }));
    el.append(...handles);
    const edges = (it.crop_ok ? ['cl', 'cr', 'ct', 'cb'] : []).map(k => h('span', { class: 'mp-edge ' + k, title: 'Drag to trim this edge' }));
    el.append(...edges);
    const save = () => { touched[it.id] = pack(b); };
    const drag = (ev, mode) => {
      ev.preventDefault(); ev.stopPropagation();
      el.setPointerCapture(ev.pointerId);
      selected = it.id; showSel();
      const pre = hooks ? hooks.snap() : null;
      const r = box.getBoundingClientRect(), x0 = ev.clientX, y0 = ev.clientY, o = { ...b, crop: [...b.crop] }, ratio = o.h / o.w;   // keeps its shape when resized
      const move = e => {
        const dx = (e.clientX - x0) / r.width, dy = (e.clientY - y0) / r.height;
        if (mode === 'move') { b.x = o.x + dx; b.y = o.y + dy; }
        else if (mode[0] === 'c') {   // trim an edge: the rest of the picture stays exactly where it is
          const horiz = mode === 'cl' || mode === 'cr', fl = b.flip && horiz;
          const idx = { cl: fl ? 2 : 0, cr: fl ? 0 : 2, ct: 1, cb: 3 }[mode], other = { 0: 2, 2: 0, 1: 3, 3: 1 }[idx];
          const full = horiz ? o.w / (1 - o.crop[0] - o.crop[2]) : o.h / (1 - o.crop[1] - o.crop[3]);
          const d = (horiz ? dx : dy) / full * (mode === 'cl' || mode === 'ct' ? 1 : -1);
          const c = Math.max(0, Math.min(o.crop[idx] + d, 0.9 - o.crop[other]));
          const applied = (c - o.crop[idx]) * full;
          b.crop = [...o.crop]; b.crop[idx] = c;
          if (mode === 'cl') { b.x = o.x + applied; b.w = o.w - applied; }
          else if (mode === 'cr') { b.w = o.w - applied; }
          else if (mode === 'ct') { b.y = o.y + applied; b.h = o.h - applied; }
          else { b.h = o.h - applied; }
          showCrop();
        } else {   // resize from any corner, the opposite corner stays where it is
          const left = mode === 'tl' || mode === 'bl', top = mode === 'tl' || mode === 'tr';
          b.w = Math.max(0.03, o.w + (left ? -dx : dx)); b.h = o.h * (b.w / o.w);
          if (left) b.x = o.x + o.w - b.w;
          if (top) b.y = o.y + o.h - b.h;
        }
        place(el, b);
      };
      const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); save(); if (hooks) hooks.push(pre); };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    };
    el.addEventListener('pointerdown', e => drag(e, 'move'));
    handles.forEach((hd, i) => hd.addEventListener('pointerdown', e => drag(e, ['tl', 'tr', 'bl', 'br'][i])));
    edges.forEach((ed, i) => ed.addEventListener('pointerdown', e => drag(e, ['cl', 'cr', 'ct', 'cb'][i])));
    return el;
  }
  // the feature wall: drag the picture to choose which part shows, and zoom in on it (its size and place on the page stay the same)
  let wallCtl = null, zoomPre = null, zoomTimer = null;
  function panWall(d, o, aspect) {
    const img = d.querySelector('img'), pos = [...(o.pos || [0.5, 0.5])];
    if (pos.length < 3) pos[2] = 1;
    const arBox = (o.w * aspect) / o.h;   // the box's width over its height
    const dims = () => {   // the picture, scaled to fill the box and then zoomed, in units of the box's height
      const arImg = (img.naturalWidth || 1) / (img.naturalHeight || 1);
      const hs = Math.max(1, arBox / arImg) * pos[2];
      return { hs, ws: hs * arImg };
    };
    const apply = () => {
      if (!img.naturalWidth) return;
      const { hs, ws } = dims();
      img.style.width = (ws / arBox * 100) + '%'; img.style.height = (hs * 100) + '%';
      img.style.left = (-(ws - arBox) * pos[0] / arBox * 100) + '%'; img.style.top = (-(hs - 1) * pos[1] * 100) + '%';
    };
    img.addEventListener('load', apply); apply();
    wallCtl = { pos, apply, save: () => { touched.wall = [pos[0], pos[1], pos[2]]; } };
    if (!interactive) return;
    d.style.cursor = 'grab'; d.title = 'Drag to choose which part of the picture shows. Use the zoom slider to zoom (or hold Ctrl/⌘ and scroll).';
    d.addEventListener('wheel', e => { if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); setZoom(pos[2] * (e.deltaY < 0 ? 1.08 : 1 / 1.08)); }, { passive: false });   // only with Ctrl/⌘ held (or a trackpad pinch), so ordinary scrolling never zooms
    d.addEventListener('pointerdown', ev => {
      ev.preventDefault(); d.setPointerCapture(ev.pointerId); d.style.cursor = 'grabbing';
      const pre = hooks ? hooks.snap() : null;
      const r = d.getBoundingClientRect(), { hs, ws } = dims();
      const ox = (ws - arBox) / arBox * r.width, oy = (hs - 1) * r.height, x0 = ev.clientX, y0 = ev.clientY, p0 = [...pos];
      const move = e => {
        pos[0] = ox > 1 ? Math.min(1, Math.max(0, p0[0] - (e.clientX - x0) / ox)) : p0[0];
        pos[1] = oy > 1 ? Math.min(1, Math.max(0, p0[1] - (e.clientY - y0) / oy)) : p0[1];
        apply();
      };
      const up = () => { d.removeEventListener('pointermove', move); d.removeEventListener('pointerup', up); d.style.cursor = 'grab'; wallCtl.save(); if (hooks) hooks.push(pre); };
      d.addEventListener('pointermove', move); d.addEventListener('pointerup', up);
    });
    zoomRow.style.display = ''; zoomIn.value = String(pos[2]);
  }
  function setZoom(z) {
    if (!wallCtl) return;
    z = Math.min(4, Math.max(1, z));
    if (!zoomPre && hooks) zoomPre = hooks.snap();
    clearTimeout(zoomTimer); zoomTimer = setTimeout(() => { if (hooks) hooks.push(zoomPre); zoomPre = null; }, 600);
    wallCtl.pos[2] = z; zoomIn.value = String(z); wallCtl.apply(); wallCtl.save();
  }
  async function refresh() {
    const my = ++seq;
    let r;
    try {
      const body = getBody();
      body.layout = { ...(body.layout || {}), ...touched };
      r = await api('/api/materials/layout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (e) { wrap.querySelector('.mp-note').textContent = e.message; return; }
    if (my !== seq) return;   // a newer request is on its way
    box.style.aspectRatio = String(r.aspect);
    for (const k of Object.keys(cur)) delete cur[k];
    zoomRow.style.display = 'none'; wallCtl = null;
    const bits = [h('div', { class: 'mp-title' }, 'Materials & Furnishings')];
    const flat = (o, cls) => { const d = h('div', { class: 'mp-flat ' + cls }, h('img', { src: o.url, alt: '', draggable: 'false' })); place(d, o); if (cls === 'wall') panWall(d, o, r.aspect); return d; };
    (r.slots || []).forEach(o => { const d = h('div', { class: 'slot-empty' + (o.round ? ' round' : '') }, o.label ? h('span', {}, o.label) : ''); place(d, o); bits.push(d); });
    if (r.wall) bits.push(flat(r.wall, 'wall'));
    if (r.flooring) bits.push(flat(r.flooring, 'floor'));
    r.swatches.forEach(o => bits.push(flat(o, 'sw')));
    (r.colours || []).forEach(o => { const d = h('div', { class: 'mp-dot' }); place(d, o); d.style.background = o.c; bits.push(d); });
    r.furniture.forEach(it => bits.push(movable(it, r.aspect)));
    r.lighting.forEach(it => bits.push(movable(it, r.aspect)));
    box.replaceChildren(...bits);
    if (selected && !cur[selected]) selected = null;
    showSel();
    wrap.querySelector('.mp-note').textContent = interactive ? (r.furniture.length || r.lighting.length ? 'Drag a piece to move it. Drag its corner dot to make it bigger or smaller. Hover a piece to see its dot.' : 'Nothing to arrange yet: choose furniture or lighting first.') : '';
  }
  function soon() { clearTimeout(timer); timer = setTimeout(refresh, 150); }
  refresh();
  return { el: wrap, selectedId: () => selected, refresh: soon, layout: () => ({ ...touched }), reset: () => { for (const k of Object.keys(touched)) delete touched[k]; refresh(); } };
}

// Choose pictures from the library. limit > 1 lets you tick several and add them in one go. A live page preview sits alongside.
async function pickFromLibrary(category, title, { limit = 1, taken = [], getBody = null, layoutRef = {} } = {}) {
  const lib = await api('/api/library');
  const multi = limit > 1;
  return new Promise(resolve => {
    const close = v => { overlay.remove(); resolve(v); };
    const chosen = [];
    const search = h('input', { type: 'text', placeholder: 'Search by name or tag…', 'aria-label': 'Search' });
    const grid = h('div', { class: 'pick-grid big' });
    const wholeLib = category === 'additional';
    const extraCats = ['flooring', 'finishings'];
    let all = wholeLib ? lib.items.filter(i => extraCats.includes(i.category)) : lib.items.filter(i => i.category === category && !i.only_additional);   // some pictures (plants) only appear under Additional
    const filterCat = wholeLib ? h('select', { 'aria-label': 'Show' }, h('option', { value: 'all' }, 'Flooring and materials'), ...lib.categories.filter(c => extraCats.includes(c.key)).map(c => h('option', { value: c.key }, c.label))) : null;
    const cut = (category === 'furniture' || category === 'lighting' || wholeLib) ? '?cut=1' : '';
    const isOne = category === 'flooring' || category === 'lifestyle';
    const bodyNow = () => {
      const b = getBody ? getBody() : {};
      const ids = [...taken, ...chosen.map(c => c.id)];
      b[category] = isOne ? (ids[ids.length - 1] || null) : ids;
      return b;
    };
    const preview = getBody ? materialsPreview(bodyNow, { interactive: true, touched: layoutRef }) : null;
    const countBtn = h('button', { type: 'button', class: 'btn', disabled: true }, 'Add selected');
    const updateCount = () => { countBtn.disabled = !chosen.length; countBtn.textContent = chosen.length ? `Add ${chosen.length} selected` : 'Add selected'; if (preview) preview.refresh(); };
    const view = i => {   // full-size picture
      const o = h('div', { class: 'overlay top', onclick: () => o.remove() }, h('div', { class: 'lightbox' }, h('img', { src: `/api/library/${i.id}/image` + cut, alt: i.name }), h('div', {}, i.name)));
      document.body.append(o);
    };
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const rows = all.filter(i => (!filterCat || filterCat.value === 'all' || i.category === filterCat.value) && (!q || (i.name + ' ' + i.tags.join(' ')).toLowerCase().includes(q)));
      grid.replaceChildren(...(rows.length ? rows.map(i => {
        const already = taken.includes(i.id), on = chosen.some(c => c.id === i.id);
        const t = h('div', { class: 'pick' + (on ? ' on' : '') + (already ? ' used' : '') },
          h('button', { type: 'button', class: 'pick-img', title: already ? 'Already added' : multi ? 'Click to tick or untick' : 'Click to use this', onclick: () => {
            if (already) return;
            if (!multi) { close(i); return; }
            const at = chosen.findIndex(c => c.id === i.id);
            if (at >= 0) chosen.splice(at, 1); else if (chosen.length < limit) chosen.push(i); else { toast(`You can add up to ${limit} here.`); return; }
            draw(); updateCount();
          } }, h('img', { src: `/api/library/${i.id}/image` + cut, alt: '', loading: 'lazy' }), on ? h('b', { class: 'tick' }, '✓') : ''),
          h('div', { class: 'pick-cap' }, h('span', {}, i.name + (i.height_cm && (category === 'furniture' || category === 'lighting') ? ` · ${i.height_cm} cm` : '')),
            h('button', { type: 'button', class: 'btn link', onclick: () => view(i) }, 'View larger')));
        return t;
      }) : [h('p', { class: 'hint' }, all.length ? 'Nothing matches that search.' : 'Nothing in the library for this yet. Drop a picture above to add the first one.')]));
    };
    search.addEventListener('input', draw); if (filterCat) filterCat.addEventListener('change', draw); draw();
    // not in the library? drop pictures here: they are added to the library under this heading and chosen straight away
    const upMsg = h('div');
    const fileIn = h('input', { type: 'file', hidden: true, accept: 'image/*', ...(multi ? { multiple: true } : {}) });
    const drop = h('div', { class: 'drop', tabindex: '0', role: 'button', 'aria-label': 'Add pictures to the library' },
      h('strong', {}, wholeLib ? 'Anything else?' : 'Not in the library?'), wholeLib ? ' Drop pictures here or click to choose. They are added to the library as materials and placed on the page.' : ` Drop ${multi ? 'pictures' : 'a picture'} here or click to choose. ${multi ? 'They are' : 'It is'} added to the library and used in this proposal.`);
    async function upload(files) {
      files = [...files].filter(f => /^image\//.test(f.type) || /\.(png|jpe?g|webp)$/i.test(f.name));
      if (!files.length) { upMsg.replaceChildren(h('div', { class: 'msg error' }, 'Please use PNG or JPG pictures.')); return; }
      if (!multi) files = files.slice(0, 1);
      for (const [n, file] of files.entries()) {
        upMsg.replaceChildren(h('div', { class: 'msg info' }, `Adding ${n + 1} of ${files.length}: ${file.name}…`));
        try {
          const fd = new FormData(); fd.append('category', wholeLib ? (filterCat.value !== 'all' ? filterCat.value : 'finishings') : category); fd.append('file', file); if (filterCat) filterCat.value = 'all';
          const r = await api('/api/library', { method: 'POST', body: fd });
          if (!multi) { close(r.item); return; }
          all = [r.item, ...all];
          if (chosen.length < limit) chosen.push(r.item);
        } catch (e) { upMsg.replaceChildren(h('div', { class: 'msg error' }, `${file.name}: ${e.message}`)); draw(); updateCount(); return; }
      }
      upMsg.replaceChildren(h('div', { class: 'msg info' }, 'Added. They are ticked below: click Add selected when you are ready.'));
      draw(); updateCount();
    }
    drop.addEventListener('click', () => fileIn.click());
    drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileIn.click(); } });
    fileIn.addEventListener('change', () => upload(fileIn.files));
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => upload(e.dataTransfer.files));
    const left = h('div', { class: 'pick-left' }, drop, fileIn, upMsg, ...(filterCat ? [h('label', { class: 'f' }, 'Show', filterCat)] : []), search, grid);
    const overlay = h('div', { class: 'overlay', onclick: e => { if (e.target === overlay) close(null); } },
      h('div', { class: 'modal xwide' }, h('div', { class: 'modal-head' }, h('h2', {}, title),
        h('span', {}, multi ? countBtn : '', ' ', h('button', { class: 'btn ghost', onclick: () => close(null) }, 'Cancel'))),
        h('div', { class: 'pick-cols' + (preview ? '' : ' one') }, left, preview ? h('div', { class: 'pick-right' }, h('div', { class: 'hint', style: 'margin-bottom:6px' }, 'The page as it will look. You can move and resize the furniture and lighting here too.'), preview.el) : '')));
    countBtn.addEventListener('click', () => close(chosen));
    document.body.append(overlay);
  });
}

function bodyOfState(state, heights, layout) {
  const ids = k => state[k].map(i => i.id);
  return { flooring: state.flooring[0]?.id || null, finishings: ids('finishings'), furniture: ids('furniture'), lighting: ids('lighting'),
    lifestyle: state.lifestyle[0]?.id || null, additional: state.additional ? ids('additional') : [], heights: { ...heights }, layout: { ...layout }, colours: (state.colours || []).filter(Boolean) };
}
async function addToGroup(g, state, getBody, layoutRef) {
  const room = g.max - state[g.key].length;
  const r = await pickFromLibrary(g.key, `Add: ${g.label}`, { limit: Math.max(1, room), taken: state[g.key].map(x => x.id), getBody, layoutRef }).catch(e => { toast(e.message); return null; });
  const got = Array.isArray(r) ? r : r ? [r] : [];
  for (const p of got) if (state[g.key].length < g.max && !state[g.key].some(x => x.id === p.id)) state[g.key].push(p);
  return got.length > 0;
}
async function swapInGroup(g, i, state, getBody, layoutRef) {
  const r = await pickFromLibrary(g.key, `Choose: ${g.label}`, { limit: 1, taken: state[g.key].filter((_, k) => k !== i).map(x => x.id), getBody, layoutRef }).catch(e => { toast(e.message); return null; });
  if (r) { state[g.key][i] = r; return true; }
  return false;
}
const GROUPS = [
  { key: 'previous_work', label: 'Mood board photos', hint: '', max: 6 },
  { key: 'flooring', label: 'Flooring', hint: '', max: 1 },
  { key: 'finishings', label: 'Materials', hint: '', max: 4 },
  { key: 'furniture', label: 'Furniture', hint: '', max: 6 },
  { key: 'lighting', label: 'Lighting', hint: '', max: 3 },
  { key: 'lifestyle', label: 'Feature wall', hint: '', max: 1 },
  { key: 'additional', label: 'Additional', hint: '', max: 8 },
];
function selectionEditor(pid, sel, onSaved) {
  const groups = GROUPS.filter(g => (g.key === 'previous_work' ? feat('mood_board') : feat('materials')));
  const state = Object.fromEntries(GROUPS.map(g => [g.key, [...(sel[g.key] || [])]]));
  const heights = { ...(sel.heights || {}) };
  state.colours = [...(sel.colours || [])];   // brand colour circles: kept as they are
  const layout = { ...(sel.layout || {}) };   // moved/resized pieces, shared with the page preview
  const wrap = h('div', { class: 'seled' });
  const msg = h('div');
  const suggestMsg = h('div');
  const preview = feat('materials') ? materialsPreview(() => bodyOfState(state, heights, layout), { interactive: true, touched: layout }) : null;
  function tiles(g) {
    const big = g.key === 'previous_work';
    return h('div', { class: 'tiles' + (big ? ' big' : '') },
      ...state[g.key].map((it, i) => h('div', { class: 'tile' },
        h('button', { type: 'button', class: 'tileimg', title: 'Click to swap for another', onclick: async () => {
          if (await swapInGroup(g, i, state, () => bodyOfState(state, heights, layout), layout)) draw(); } }, h('img', { src: it.thumb, alt: it.name })),
        h('span', { class: 'cap' }, big && it.reason ? it.reason : it.name),
        h('button', { type: 'button', class: 'x', title: 'Remove', onclick: () => { state[g.key].splice(i, 1); draw(); } }, '✕'))),
      state[g.key].length < g.max ? h('button', { type: 'button', class: 'tile add', onclick: async () => {
        if (await addToGroup(g, state, () => bodyOfState(state, heights, layout), layout)) draw(); } }, '+ Add') : '');
  }
  function group(g) {
    const head = h('div', { class: 'selhead' }, h('strong', {}, g.label), ' ', h('span', { class: 'hint' }, g.hint));
    if (g.key === 'previous_work') {
      const b = h('button', { type: 'button', class: 'btn ghost', style: 'margin-left:12px' }, 'Suggest different photos');
      b.addEventListener('click', async () => {
        b.disabled = true; b.textContent = 'Finding photos…'; suggestMsg.replaceChildren();
        try {
          const r = await api(`/api/proposals/${pid}/mood-board/suggest`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ exclude: state.previous_work.map(i => i.id) }) });
          state.previous_work = r.photos; draw();
          suggestMsg.replaceChildren(h('div', { class: 'hint' }, 'New photos suggested. Click "Rebuild with these choices" to put them in the proposal.'));
        } catch (e) { suggestMsg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
        b.disabled = false; b.textContent = 'Suggest different photos';
      });
      head.append(b);
    }
    return h('div', { class: 'selgroup' }, head, tiles(g));
  }
  function draw() {
    const mood = groups.find(g => g.key === 'previous_work'), rest = groups.filter(g => g.key !== 'previous_work');
    wrap.replaceChildren(...(mood ? [group(mood), suggestMsg] : []),
      ...(rest.length ? [mood ? h('details', { style: 'margin-top:8px' }, h('summary', {}, 'Materials & furnishings (flooring, materials, furniture, lighting)'), h('div', { style: 'margin-top:10px' }, rest.map(group)))
                              : h('div', {}, rest.map(group))] : []));
    if (preview) preview.refresh();
  }
  draw();
  const btn = h('button', { type: 'button', class: 'btn' }, 'Rebuild with these choices');
  btn.addEventListener('click', async () => {
    btn.disabled = true; btn.textContent = 'Rebuilding…'; msg.replaceChildren();
    try {
      const body = { previous_work: state.previous_work.map(i => i.id), flooring: state.flooring[0]?.id || null, finishings: state.finishings.map(i => i.id),
        furniture: state.furniture.map(i => i.id), lighting: state.lighting.map(i => i.id), lifestyle: state.lifestyle[0]?.id || null, additional: state.additional.map(i => i.id), heights, layout, colours: state.colours,
        mood_reasons: Object.fromEntries(state.previous_work.filter(i => i.reason).map(i => [i.id, i.reason])) };
      const r = await api(`/api/proposals/${pid}/selection`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      msg.replaceChildren(h('div', { class: 'msg info' }, 'Rebuilt. Your downloads now include these choices.' + (r.note ? ' ' + r.note : '')));
      if (onSaved) onSaved(r.selection);
    } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    btn.disabled = false; btn.textContent = 'Rebuild with these choices';
  });
  const previewBox = preview ? h('div', { style: 'margin-top:14px' }, h('strong', {}, 'Move and resize the furniture and lighting'), preview.el) : '';
  return h('div', {}, wrap, previewBox, msg, h('p', {}, btn));
}

// the Materials & Furnishings choices on the new-proposal form
const PICK_GROUPS = [
  { key: 'flooring', label: 'Flooring', hint: '', max: 1 },
  { key: 'finishings', label: 'Materials', hint: '', max: 4 },
  { key: 'furniture', label: 'Furniture', hint: '', max: 6 },
  { key: 'lighting', label: 'Lighting', hint: '', max: 3 },
  { key: 'lifestyle', label: 'Feature wall', hint: '', max: 1 },
  { key: 'additional', label: 'Additional', hint: '', max: 8 },
];

// ---------------------------------------------------------------- "About the company" pages: tick the ones to include, drag to put them in order
function aboutPicker(cat = 'about', tabName = 'Company') {
  let items = [], chosen = [], dragFrom = null, ready = false;
  const avail = h('div', { class: 'pick-grid big about-grid' });
  const order = h('div', { class: 'about-order' });
  const el = h('div', {},
    h('div', { class: 'selhead' }, h('strong', {}, 'Available pages'), ' ', h('span', { class: 'hint' }, 'Click a page to add it to your proposal, click again to remove it.')), avail,
    h('div', { class: 'selhead', style: 'margin-top:18px' }, h('strong', {}, 'Pages in your proposal'), ' ', h('span', { class: 'hint' }, 'Drag a page to move it, or use the arrows. They print in this order, after the Terms.')), order);
  const byId = id => items.find(i => i.id === id);
  function move(from, to) {
    if (to < 0 || to >= chosen.length || from === to) return;
    const [x] = chosen.splice(from, 1); chosen.splice(to, 0, x); draw();
  }
  function draw() {
    if (!ready) { avail.replaceChildren(h('p', { class: 'hint' }, 'Loading\u2026')); return; }
    if (!items.length) { avail.replaceChildren(h('p', { class: 'hint' }, `No pages yet. They are added on the ${tabName} tab.`)); order.replaceChildren(); return; }
    avail.replaceChildren(...items.map(i => {
      const on = chosen.includes(i.id);
      return h('button', { type: 'button', class: 'pick about-tile' + (on ? ' on' : ''), 'aria-pressed': String(on),
        onclick: () => { chosen = on ? chosen.filter(x => x !== i.id) : [...chosen, i.id]; draw(); } },
        h('img', { src: i.thumb, alt: '', loading: 'lazy' }), h('span', {}, (on ? '\u2713 ' : '') + i.name));
    }));
    if (!chosen.length) { order.replaceChildren(h('p', { class: 'hint' }, 'None chosen. Leave it like this to leave the section out.')); return; }
    order.replaceChildren(...chosen.map((id, k) => {
      const i = byId(id);
      const row = h('div', { class: 'about-row', draggable: 'true' },
        h('span', { class: 'about-grip', 'aria-hidden': 'true', title: 'Drag to move' }, '\u283F'), h('span', { class: 'about-num' }, String(k + 1)),
        h('img', { src: i.thumb, alt: '', draggable: 'false' }), h('span', { class: 'about-name' }, i.name),
        h('button', { type: 'button', class: 'btn link', 'aria-label': `Move ${i.name} up`, disabled: k === 0, onclick: () => move(k, k - 1) }, '\u2191'),
        h('button', { type: 'button', class: 'btn link', 'aria-label': `Move ${i.name} down`, disabled: k === chosen.length - 1, onclick: () => move(k, k + 1) }, '\u2193'),
        h('button', { type: 'button', class: 'btn link danger', 'aria-label': `Remove ${i.name}`, onclick: () => { chosen.splice(k, 1); draw(); } }, '\u2715'));
      row.addEventListener('dragstart', e => { dragFrom = k; row.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', String(k)); } catch { /* old browsers */ } });
      row.addEventListener('dragend', () => { dragFrom = null; order.querySelectorAll('.about-row').forEach(r => r.classList.remove('dragging', 'over')); });
      row.addEventListener('dragover', e => { if (dragFrom === null) return; e.preventDefault(); e.dataTransfer.dropEffect = 'move'; row.classList.add('over'); });
      row.addEventListener('dragleave', () => row.classList.remove('over'));
      row.addEventListener('drop', e => { e.preventDefault(); const from = dragFrom; dragFrom = null; if (from !== null) move(from, k); });
      return row;
    }));
  }
  draw();
  api('/api/library').then(lib => { items = lib.items.filter(i => i.category === cat); chosen = chosen.filter(id => byId(id)); ready = true; draw(); })
    .catch(() => { ready = true; items = []; draw(); });
  return { el, value: () => chosen.slice(), set: ids => { chosen = (ids || []).slice(); if (ready) chosen = chosen.filter(id => byId(id)); draw(); } };
}

// a section title with its on/off switch on the right of the same line (anything in `extra` sits just left of the switch)
function titleRow(title, sw, extra, below) {   // extra sits left of the switch, or under it when `below` is set
  return h('div', { class: 'title-row' }, title, h('div', { class: 'title-right' + (below ? ' stack' : '') }, ...(extra && !below ? [extra] : []), h('label', { class: 'switch-row title-switch' }, sw, h('span', { class: 'slider' })), ...(extra && below ? [extra] : [])));
}


// ---------------------------------------------------------------- Booth Location page: the floorplan, with a red box to mark the booth
const LOC_AREA = [0.05, 0.1797, 0.90, 0.7434];       // where the floorplan sits on the page (fractions of the page)
const LOC_RATIO = 10691813 / 7559675;             // page width / height
function boothEditor(getFile) {
  let box = null, natural = null, shownFile = null, shownUrl = '', view = [1, 0, 0], sel = false;   // view: zoom, shift x, shift y (page fractions)
  const zoomOut = h('input', { type: 'range', min: 1, max: 6, step: 0.05, value: 1, 'aria-label': 'Zoom the floorplan' });
  const zoomLbl = h('span', { class: 'hint', style: 'min-width:44px;text-align:right' }, '100%');
  const setZoom = z => { z = Math.min(6, Math.max(1, z)); const k = z / view[0]; view = [z, view[1] * k, view[2] * k]; if (z === 1) view = [1, 0, 0]; draw(); };
  zoomOut.addEventListener('input', () => setZoom(parseFloat(zoomOut.value)));
  const zoomBox = h('div', { class: 'loc-zoom' },
    h('button', { type: 'button', class: 'btn ghost', onclick: () => setZoom(view[0] - 0.5), 'aria-label': 'Zoom out' }, '−'), zoomOut,
    h('button', { type: 'button', class: 'btn ghost', onclick: () => setZoom(view[0] + 0.5), 'aria-label': 'Zoom in' }, '+'), zoomLbl,
    h('button', { type: 'button', class: 'btn ghost', onclick: () => { view = [1, 0, 0]; draw(); } }, 'Reset zoom'));
  const page = h('div', { class: 'mp edit', style: `aspect-ratio:${LOC_RATIO}` });
  const note = h('div', { class: 'mp-note hint' }, 'Drag the red box over the booth; drag its edges and corners to change its size and shape. Use the zoom to enlarge the floorplan, and drag the floorplan to move it. It stays inside the frame.');
  const reset = h('button', { type: 'button', class: 'btn ghost', onclick: () => { box = defaultBox(); draw(); } }, 'Reset the box');
  const wrap = h('div', { class: 'selgroup', style: 'display:none' }, h('div', { class: 'selhead' }, h('strong', {}, 'The page')), h('div', { class: 'mp-wrap' }, page), h('div', { style: 'margin-top:8px' }, zoomBox), h('div', { style: 'margin-top:8px;display:flex;gap:12px;align-items:center;flex-wrap:wrap' }, reset, note));
  function imgRect() {   // the picture, fitted inside the area and centred
    const [ax, ay, aw, ah] = LOC_AREA;
    if (!natural) return { x: ax, y: ay, w: aw, h: ah };
    const ar = natural[0] / natural[1], areaR = (aw * LOC_RATIO) / ah;
    const w = ar >= areaR ? aw : ah * ar / LOC_RATIO, hh = ar >= areaR ? aw * LOC_RATIO / ar : ah;
    return { x: ax + (aw - w) / 2, y: ay + (ah - hh) / 2, w, h: hh };
  }
  function defaultBox() {   // a square, placed in the middle of the picture
    const r = imgRect(), w = Math.min(0.2 * r.w, 0.2 * r.h / LOC_RATIO * 1.0), hh = w * LOC_RATIO;
    return [r.x + r.w / 2 - w / 2, r.y + r.h / 2 - hh / 2, w, hh];
  }
  function draw() {
    const f = getFile();
    wrap.style.display = f ? '' : 'none';
    if (!f) { box = null; shownFile = null; return; }
    if (f !== shownFile) {
      shownFile = f; natural = null; box = null; view = [1, 0, 0];
      if (shownUrl) URL.revokeObjectURL(shownUrl);
      shownUrl = URL.createObjectURL(f);
      const im = new Image(); im.onload = () => { if (shownFile === f) { natural = [im.naturalWidth, im.naturalHeight]; if (!box) box = defaultBox(); draw(); } }; im.src = shownUrl;
    }
    const r = imgRect(), bits = [h('div', { class: 'mp-title' }, 'Booth Location')];
    const [ax, ay, aw, ah] = LOC_AREA, z = view[0];
    const frame = h('div', { class: 'loc-frame' });
    frame.style.cssText = `left:${ax * 100}%;top:${ay * 100}%;width:${aw * 100}%;height:${ah * 100}%`;
    const pic = h('img', { src: shownUrl, alt: '', draggable: 'false', class: 'loc-img' });
    const pw = r.w * z, ph = r.h * z, cx = ax + aw / 2 + view[1], cy = ay + ah / 2 + view[2];
    pic.style.cssText = `left:${(cx - pw / 2 - ax) / aw * 100}%;top:${(cy - ph / 2 - ay) / ah * 100}%;width:${pw / aw * 100}%;height:${ph / ah * 100}%`;
    frame.append(pic);
    frame.addEventListener('pointerdown', ev => {
      ev.preventDefault(); frame.setPointerCapture(ev.pointerId); frame.style.cursor = 'grabbing';
      const pr = page.getBoundingClientRect(), x0 = ev.clientX, y0 = ev.clientY, v0 = [...view];
      const move = e => {
        const lim = [(r.w * view[0] + aw) / 2, (r.h * view[0] + ah) / 2];
        view[1] = Math.max(-lim[0], Math.min(lim[0], v0[1] + (e.clientX - x0) / pr.width));
        view[2] = Math.max(-lim[1], Math.min(lim[1], v0[2] + (e.clientY - y0) / pr.height));
        const ncx = ax + aw / 2 + view[1], ncy = ay + ah / 2 + view[2];
        pic.style.left = (ncx - pw / 2 - ax) / aw * 100 + '%'; pic.style.top = (ncy - ph / 2 - ay) / ah * 100 + '%';
      };
      const up = () => { frame.style.cursor = ''; frame.removeEventListener('pointermove', move); frame.removeEventListener('pointerup', up); frame.removeEventListener('pointercancel', up); };
      frame.addEventListener('pointermove', move); frame.addEventListener('pointerup', up); frame.addEventListener('pointercancel', up);
    });
    bits.push(frame);
    zoomOut.value = z; zoomLbl.textContent = Math.round(z * 100) + '%';
    if (box) bits.push(boxEl());
    page.replaceChildren(...bits);
  }
  function boxEl() {
    const el = h('div', { class: 'loc-box' + (sel ? ' sel' : ''), title: 'Click to select, drag to move' });
    el.style.left = box[0] * 100 + '%'; el.style.top = box[1] * 100 + '%'; el.style.width = box[2] * 100 + '%'; el.style.height = box[3] * 100 + '%';
    const start = (ev, mode) => {
      ev.preventDefault(); ev.stopPropagation(); el.setPointerCapture(ev.pointerId); sel = true; el.classList.add('sel');
      const pr = page.getBoundingClientRect(), x0 = ev.clientX, y0 = ev.clientY, b0 = [...box], MIN = 0.02;
      const move = e => {
        const dx = (e.clientX - x0) / pr.width, dy = (e.clientY - y0) / pr.height;
        let [x, y, w, hh] = b0;
        if (mode === 'move') { x = Math.min(1 - w, Math.max(0, x + dx)); y = Math.min(1 - hh, Math.max(0, y + dy)); }
        else {
          let l = x, t = y, rr = x + w, bb = y + hh;
          if (mode.includes('w')) l = Math.min(rr - MIN, Math.max(0, l + dx));
          if (mode.includes('e')) rr = Math.max(l + MIN, Math.min(1, rr + dx));
          if (mode.includes('n')) t = Math.min(bb - MIN, Math.max(0, t + dy));
          if (mode.includes('s')) bb = Math.max(t + MIN, Math.min(1, bb + dy));
          x = l; y = t; w = rr - l; hh = bb - t;
        }
        box = [x, y, w, hh];
        el.style.left = x * 100 + '%'; el.style.top = y * 100 + '%'; el.style.width = w * 100 + '%'; el.style.height = hh * 100 + '%';
      };
      const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    };
    el.addEventListener('pointerdown', ev => { if (ev.target === el) start(ev, 'move'); });
    ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach(m => { const hd = h('span', { class: 'loc-h loc-' + m }); hd.addEventListener('pointerdown', ev => start(ev, m)); el.append(hd); });
    return el;
  }
  page.addEventListener('pointerdown', ev => { if (sel && !ev.target.closest('.loc-box')) { sel = false; const b = page.querySelector('.loc-box'); if (b) b.classList.remove('sel'); } });
  document.addEventListener('pointerdown', ev => {   // clicking anywhere else puts the box down: it stays on the page but is no longer highlighted
    if (!sel || ev.target.closest('.loc-box')) return;
    sel = false; const b = page.querySelector('.loc-box'); if (b) b.classList.remove('sel');
  });
  return { el: wrap, refresh: draw, view: () => (getFile() ? view.map(v => Math.round(v * 10000) / 10000) : null),
    setView(v) { if (Array.isArray(v) && v.length === 3) { view = v.map(Number); draw(); } },
    value: () => (getFile() && box ? box.map(v => Math.round(v * 10000) / 10000) : null),
    set(b) { if (Array.isArray(b) && b.length === 4) { box = b.map(Number); draw(); } } };
}

// the main colours of a logo picture (up to four '#rrggbb'), ignoring the white background
let siteColours = [];   // brand colours read from the client's website
async function logoColours(file) {
  try {
    const url = URL.createObjectURL(file);
    const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; });
    const k = Math.min(1, 80 / Math.max(img.width, img.height)), w = Math.max(1, Math.round(img.width * k)), hgt = Math.max(1, Math.round(img.height * k));
    const cv = document.createElement('canvas'); cv.width = w; cv.height = hgt;
    const ctx = cv.getContext('2d'); ctx.drawImage(img, 0, 0, w, hgt); URL.revokeObjectURL(url);
    const px = ctx.getImageData(0, 0, w, hgt).data, buckets = new Map();
    for (let i = 0; i < px.length; i += 4) {
      const r = px[i], g = px[i + 1], b = px[i + 2], a = px[i + 3];
      if (a < 200 || (r > 232 && g > 232 && b > 232)) continue;
      const key = (r >> 4) * 256 + (g >> 4) * 16 + (b >> 4), e = buckets.get(key) || { n: 0, r: 0, g: 0, b: 0 };
      e.n++; e.r += r; e.g += g; e.b += b; buckets.set(key, e);
    }
    const out = [];
    for (const e of [...buckets.values()].sort((p, q) => q.n - p.n)) {
      const c = [e.r / e.n, e.g / e.n, e.b / e.n];
      if (out.every(o => Math.hypot(o[0] - c[0], o[1] - c[1], o[2] - c[2]) > 70)) out.push(c);
      if (out.length === 4) break;
    }
    return out.map(c => '#' + c.map(v => Math.round(v).toString(16).padStart(2, '0')).join(''));
  } catch { return []; }
}

function materialsPicker(getLogo, getRenders) {
  const state = Object.fromEntries(PICK_GROUPS.map(g => [g.key, []]));
  state.colours = ['', '', '', ''];   // the client's colours, shown as four circles
  const heights = {};
  let dragFrom = null;   // a chosen picture being dragged to a new place in its row
  const layout = {};   // pieces moved or resized on the page preview
  const wrap = h('div', { class: 'seled' });
  // undo (Ctrl+Z) and redo (Ctrl+Y): each step is a copy of the choices and the layout
  const undo = [], redo = [];
  const snap = () => JSON.stringify({ state, heights, layout });
  const push = pre => { if (pre != null && pre !== snap()) { undo.push(pre); if (undo.length > 60) undo.shift(); redo.length = 0; } };
  const restore = str => {
    const o = JSON.parse(str);
    PICK_GROUPS.forEach(g => { state[g.key] = o.state[g.key] || []; });
    state.colours = o.state.colours || ['', '', '', ''];
    for (const k of Object.keys(heights)) delete heights[k]; Object.assign(heights, o.heights || {});
    for (const k of Object.keys(layout)) delete layout[k]; Object.assign(layout, o.layout || {});
    draw();
  };
  function removePiece(id) {
    const pre = snap();
    PICK_GROUPS.forEach(g => { const i = state[g.key].findIndex(x => x.id === id); if (i >= 0) state[g.key].splice(i, 1); });
    delete layout[id]; delete heights[id];
    push(pre); draw();
  }
  function group(g) {
    const t = h('div', { class: 'tiles' },
      ...state[g.key].map((it, i) => h('div', { class: 'tile' + (state[g.key].length > 1 ? ' sortable' : ''), ...(state[g.key].length > 1 ? { draggable: 'true',
          ondragstart: e => { dragFrom = { key: g.key, i }; e.currentTarget.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', String(i)); } catch { /* old browsers */ } },
          ondragend: e => { dragFrom = null; e.currentTarget.classList.remove('dragging'); wrap.querySelectorAll('.tile.over').forEach(x => x.classList.remove('over')); },
          ondragover: e => { if (dragFrom && dragFrom.key === g.key) { e.preventDefault(); e.currentTarget.classList.add('over'); } },
          ondragleave: e => e.currentTarget.classList.remove('over'),
          ondrop: e => { e.preventDefault(); const f = dragFrom; dragFrom = null; if (f && f.key === g.key && f.i !== i) { const pre = snap(); const [x] = state[g.key].splice(f.i, 1); state[g.key].splice(i, 0, x); push(pre); draw(); } } } : {}) },
        h('button', { type: 'button', class: 'tileimg', title: 'Click to swap for another' + (state[g.key].length > 1 ? '. Drag to change the order' : ''), onclick: async () => {
          const pre = snap();
          if (await swapInGroup(g, i, state, () => bodyOfState(state, heights, layout), layout)) { push(pre); draw(); } } }, h('img', { src: it.thumb, alt: it.name, draggable: 'false' })),
        h('span', { class: 'cap' }, it.name),
        h('button', { type: 'button', class: 'x', title: 'Remove', 'aria-label': 'Remove ' + it.name, onclick: () => removePiece(it.id) }, '\u2715'))),
      state[g.key].length < g.max ? h('button', { type: 'button', class: 'tile add', onclick: async () => {
        const pre = snap();
        if (await addToGroup(g, state, () => bodyOfState(state, heights, layout), layout)) { push(pre); draw(); } } }, '+ Choose from library') : '');
    return h('div', { class: 'selgroup' }, h('div', { class: 'selhead' }, h('strong', {}, g.label), ' ', h('span', { class: 'hint' }, g.hint)), t);
  }
  const preview = materialsPreview(() => bodyOfState(state, heights, layout), { interactive: true, touched: layout, hooks: { snap, push } });
  const previewBox = h('div', { class: 'selgroup' }, preview.el);
  // the client's colours: four circles on the page. Click a circle to choose its colour; the logo's colours can fill them in.
  const dotsRow = h('div', { class: 'dots-row' });
  function drawDots() {
    dotsRow.replaceChildren(...state.colours.map((c, i) => {
      const inp = h('input', { type: 'color', value: c || '#cccccc', 'aria-label': `Colour ${i + 1}` });
      inp.addEventListener('change', () => { const pre = snap(); state.colours[i] = inp.value; push(pre); drawDots(); preview.refresh(); });
      return h('span', { class: 'dot-pick' + (c ? ' set' : '') }, h('span', { class: 'dot', style: c ? `background:${c}` : '' }, inp),
        c ? h('button', { type: 'button', class: 'x', title: 'Clear', 'aria-label': `Clear colour ${i + 1}`, onclick: () => { const pre = snap(); state.colours[i] = ''; push(pre); drawDots(); preview.refresh(); } }, '\u2715') : '');
    }), h('button', { type: 'button', class: 'btn ghost', onclick: () => fromLogo(false) }, 'Use the website\u2019s & logo\u2019s colours'));
  }
  async function fromLogo(onlyIfEmpty) {   // the website's colours first, then the logo's
    const f = getLogo && getLogo();
    if (onlyIfEmpty && state.colours.some(Boolean)) return;
    const rgb = c => [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16));
    const found = [...siteColours];
    for (const c of (f ? await logoColours(f) : [])) if (found.every(o => Math.hypot(...rgb(o).map((v, i) => v - rgb(c)[i])) > 60)) found.push(c);
    if (!found.length) { if (!onlyIfEmpty) toast('No clear colours found. Find the logo from the website, or click a circle to choose one.'); return; }
    const pre = snap(); state.colours = [0, 1, 2, 3].map(i => found[i] || ''); push(pre); drawDots(); preview.refresh();
  }
  const colourBox = h('div', { class: 'selgroup' }, h('div', { class: 'selhead' }, h('strong', {}, 'Colours'), ' ', h('span', { class: 'hint' }, 'Shown as circles at the bottom left of the page')), dotsRow);
  // read the renders and suggest flooring, materials, furniture and lighting from the library
  const sugMsg = h('div', { class: 'hint', style: 'margin-top:6px' });
  const sugBtn = h('button', { type: 'button', class: 'btn ghost' }, 'Suggest from my renders');
  sugBtn.addEventListener('click', async () => {
    const r = getRenders ? getRenders() : { files: [] };
    const imgs = (r.files || []).filter(f => f && /^image\//.test(f.type || '')).slice(0, 4);
    if (!imgs.length) { sugMsg.replaceChildren(h('div', { class: 'msg error' }, 'Add the design renders first (in Renders & Technical Drawings), then try again.')); return; }
    sugBtn.disabled = true; sugBtn.textContent = 'Looking at the renders\u2026'; sugMsg.replaceChildren();
    try {
      const fd = new FormData(); imgs.forEach(f => fd.append('renders', f)); fd.append('client', r.client || '');
      const out = await api('/api/materials/suggest', { method: 'POST', body: fd });
      const pre = snap();
      for (const k of ['flooring', 'finishings', 'furniture', 'lighting', 'lifestyle']) {
        for (const old of state[k]) { delete layout[old.id]; delete heights[old.id]; }
        state[k] = out[k] || [];
      }
      push(pre); draw();
      sugMsg.replaceChildren(h('div', {}, (out.note ? out.note + ' ' : '') + 'These are suggestions: click any picture to swap it, or \u2715 to remove it. Ctrl+Z undoes this.'));
    } catch (e) { sugMsg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    sugBtn.disabled = false; sugBtn.textContent = 'Suggest from my renders';
  });
  const sugBox = h('div', { class: 'selgroup' }, h('div', { class: 'selhead' }, h('strong', {}, 'Suggestions'), ' ', h('span', { class: 'hint' }, 'Reads your renders and picks matching pieces from the library')), sugBtn, sugMsg);
  function draw() { wrap.replaceChildren(sugBox, ...PICK_GROUPS.map(group), colourBox, previewBox); drawDots(); preview.refresh(); }
  const keys = e => {
    if (!wrap.isConnected) { document.removeEventListener('keydown', keys); return; }
    if (document.querySelector('.overlay')) return;   // a pop-up is open
    const t = e.target;
    if (t && (t.isContentEditable || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || (t.tagName === 'INPUT' && !['range', 'checkbox', 'file', 'button'].includes(t.type)))) return;
    const mod = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase();
    if (mod && k === 'z' && !e.shiftKey) { if (undo.length) { e.preventDefault(); redo.push(snap()); restore(undo.pop()); } }
    else if (mod && (k === 'y' || (k === 'z' && e.shiftKey))) { if (redo.length) { e.preventDefault(); undo.push(snap()); restore(redo.pop()); } }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && preview.selectedId()) { e.preventDefault(); removePiece(preview.selectedId()); }
  };
  document.addEventListener('keydown', keys);
  draw();
  return { el: wrap, fromLogo, set(sel) { PICK_GROUPS.forEach(g => { state[g.key] = [...(sel[g.key] || [])]; }); state.colours = [0, 1, 2, 3].map(i => (sel.colours || [])[i] || ''); Object.assign(heights, sel.heights || {}); for (const k of Object.keys(layout)) delete layout[k]; Object.assign(layout, sel.layout || {}); undo.length = 0; redo.length = 0; draw(); },
    value: () => { const o = Object.fromEntries(PICK_GROUPS.map(g => [g.key, state[g.key].map(i => i.id)])); o.lifestyle = o.lifestyle[0] || null; o.flooring = o.flooring[0] || null; o.heights = heights; o.layout = layout; o.colours = state.colours.filter(Boolean); return o; } };
}
async function openSelectionModal(id, title) {
  const sel = await api(`/api/proposals/${id}/selection`);
  const overlay = h('div', { class: 'overlay', onclick: e => { if (e.target === overlay) overlay.remove(); } },
    h('div', { class: 'modal wide' }, h('div', { class: 'modal-head' }, h('h2', {}, `Mood board & materials: ${title}`), h('button', { class: 'btn ghost', onclick: () => overlay.remove() }, 'Close')),
      selectionEditor(id, sel)));
  document.body.append(overlay);
}

// ---------------------------------------------------------------- system check card
function systemCard(status) {
  const aiMsg = h('span', {});
  const testBtn = h('button', { type: 'button', class: 'btn ghost', onclick: async e => {
    e.target.disabled = true; aiMsg.textContent = ' Testing…';
    try { await api('/api/status/ai-test', { method: 'POST' }); aiMsg.textContent = ' ✓ Connected'; } catch (ex) { aiMsg.textContent = ' ✗ ' + ex.message; }
    e.target.disabled = false;
  } }, 'Test connection');
  const usesAi = feat('mood_board') || feat('materials');
  return h('section', { class: 'card' }, h('h2', {}, 'System check'),
    usesAi ? h('p', {}, status.ai.configured ? h('span', {}, '✓ Automatic matching and tagging is switched on. ', testBtn, aiMsg)
      : h('span', {}, '✗ Automatic matching and tagging is off. Add your Anthropic API key in Render (Environment > ANTHROPIC_API_KEY), then reload this page.')) : '',
    status.mail ? h('p', {}, status.mail.configured ? `\u2713 Emails (new accounts and password resets) are switched on, sent through ${status.mail.via}.`
      : '\u2717 Emails are off. In Render (Environment) add RESEND_API_KEY and MAIL_FROM, or SMTP_HOST, SMTP_USER, SMTP_PASSWORD and MAIL_FROM, then redeploy.') : '',
    Object.keys(status.titles || {}).length ? h('p', {}, Object.entries(status.titles).map(([n, t]) => h('div', {}, t.ok ? '✓' : '✗', ` PDF titles: ${t.ok ? 'Europa Light with bold added (' + t.using + ')' : 'not using Europa Light, the server picked \u201C' + t.using + '\u201D'}`))) : '',
    status.fonts.length ? h('p', {}, status.fonts.map(f => h('div', {}, f.ok ? '✓' : '✗', ` Font ${f.name}: `, f.ok ? 'installed' : 'not installed, so PDFs use a substitute. Upload the font files (.otf or .ttf) to your GitHub repository.'))) : h('p', { class: 'hint' }, 'No brand fonts are needed by the template.'));
}

// ---------------------------------------------------------------- library admin
let matCat = 'flooring';
async function renderLibrary(kind) {
  const holder = h('div', {}, h('p', { class: 'hint' }, 'Loading…'));
  shell(holder);
  let lib, status;
  try { [lib, status] = await Promise.all([api('/api/library'), api('/api/status')]); }
  catch (e) { holder.replaceChildren(h('div', { class: 'msg error' }, e.message)); return; }

  const aiMsg = h('span', {});
  const testBtn = h('button', { class: 'btn ghost', onclick: async e => {
    e.target.disabled = true; aiMsg.textContent = ' Testing…';
    try { await api('/api/status/ai-test', { method: 'POST' }); aiMsg.textContent = ' ✓ Connected'; } catch (ex) { aiMsg.textContent = ' ✗ ' + ex.message; }
    e.target.disabled = false;
  } }, 'Test connection');
  const check = systemCard(status);
  const isPages = kind === 'company' || kind === 'references', pageCat = kind === 'references' ? 'references' : 'about';
  const cats = kind === 'work' ? lib.categories.filter(c => c.key === 'previous_work') : isPages ? lib.categories.filter(c => c.key === pageCat) : lib.categories.filter(c => !['previous_work', 'about', 'references'].includes(c.key));
  let libCat = kind === 'work' ? 'previous_work' : isPages ? pageCat : matCat;

  const gridHolder = h('div');
  const guide = h('div');
  const chips = h('div', { class: 'chips' });
  const upStatus = h('div');
  let items = lib.items;
  function drawChips() {
    chips.replaceChildren(...(cats.length < 2 ? [] : cats.map(c => h('button', { class: 'chip' + (c.key === libCat ? ' on' : ''), onclick: () => { libCat = c.key; matCat = c.key; drawChips(); drawGrid(); } },
      `${c.label} (${items.filter(i => i.category === c.key).length})`))));
  }
  function drawGrid() {
    const rows = items.filter(i => i.category === libCat);
    if (libCat === 'previous_work') {
      const n = rows.length, enough = n >= status.small_library;
      guide.replaceChildren(h('div', { class: 'msg ' + (enough ? 'info' : 'error') }, `${n} photo${n === 1 ? '' : 's'} of previous work. ` +
        (enough ? 'Every proposal\u2019s mood board is chosen from these to suit the design.' : `Add more: with fewer than ${status.small_library}, mood boards will look alike. Aim for 30 or more, in a range of styles, sizes and materials.`)));
    } else guide.replaceChildren();
    const canSort = kind !== 'work' && rows.length > 1;
    let dragId = null;
    async function saveOrder() {
      try { await api('/api/library/order', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ category: libCat, ids: items.filter(x => x.category === libCat).map(x => x.id) }) }); }
      catch (e) { toast(e.message); }
    }
    function moveItem(fromId, toId) {   // put fromId where toId is, among this category's items
      if (fromId === toId) return;
      const mine = items.filter(x => x.category === libCat), a = mine.findIndex(x => x.id === fromId), b = mine.findIndex(x => x.id === toId);
      if (a < 0 || b < 0) return;
      const [x] = mine.splice(a, 1); mine.splice(b, 0, x);
      const others = items.filter(y => y.category !== libCat);
      items = [...others, ...mine]; drawGrid(); saveOrder();
    }
    const step = (i, d) => { const mine = items.filter(x => x.category === libCat), k = mine.findIndex(x => x.id === i.id), t = mine[k + d]; if (t) moveItem(i.id, t.id); };
    guide.append(...(canSort ? [h('div', { class: 'hint', style: 'margin-bottom:8px' }, 'Drag a picture to a new place (or use the \u2039 \u203a buttons). This is the order they appear in when someone adds them to a proposal.')] : []));
    gridHolder.replaceChildren(rows.length ? h('div', { class: 'lib-grid' }, rows.map(i => h('div', { class: 'lib-card' + (canSort ? ' sortable' : ''), ...(canSort ? { draggable: 'true',
        ondragstart: e => { dragId = i.id; e.currentTarget.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', i.id); } catch { /* old browsers */ } },
        ondragend: e => { dragId = null; e.currentTarget.classList.remove('dragging'); gridHolder.querySelectorAll('.over').forEach(x => x.classList.remove('over')); },
        ondragover: e => { if (dragId) { e.preventDefault(); e.currentTarget.classList.add('over'); } },
        ondragleave: e => e.currentTarget.classList.remove('over'),
        ondrop: e => { e.preventDefault(); const f = dragId; dragId = null; if (f) moveItem(f, i.id); } } : {}) },
      h('img', { src: i.thumb, alt: i.name }),
      h('div', { class: 'lib-meta' }, h('strong', {}, i.name), h('div', { class: 'hint' }, i.tags.slice(0, 6).join(', ') || 'no tags'),
        (i.category === 'furniture' || i.category === 'lighting') && i.height_cm ? h('div', { class: 'hint' }, `Height ${i.height_cm} cm` + (i.height_src === 'manual' ? '' : ' (estimate: check it)')) : '',
        i.cutout_failed ? h('div', { class: 'warn' }, 'Background not removed') : ''),
      h('div', { class: 'lib-actions' }, ...(canSort ? [h('button', { class: 'btn link', title: 'Move earlier', 'aria-label': `Move ${i.name} earlier`, onclick: () => step(i, -1) }, '\u2039'), h('button', { class: 'btn link', title: 'Move later', 'aria-label': `Move ${i.name} later`, onclick: () => step(i, 1) }, '\u203a')] : []), h('button', { class: 'btn link', onclick: () => editItem(i) }, 'Edit'),
        h('button', { class: 'btn link danger', onclick: async () => {
          if (!confirm(`Delete "${i.name}"?`)) return;
          try { await api(`/api/library/${i.id}`, { method: 'DELETE' }); items = items.filter(x => x.id !== i.id); drawChips(); drawGrid(); } catch (e) { toast(e.message); } } }, 'Delete')))))
      : h('p', { class: 'hint' }, 'Nothing here yet. Add some images above.'));
  }
  function editItem(i) {
    const name = textField('Name', { value: i.name });
    const tags = textField('Tags', { value: i.tags.join(', '), hint: '(comma separated; used to match designs)' });
    const cat = h('select', {}, cats.map(c => h('option', { value: c.key, ...(c.key === i.category ? { selected: true } : {}) }, c.label)));
    const height = textField('Height (cm)', { value: i.height_cm != null ? String(i.height_cm) : '', hint: '(the real overall height of the piece. Furniture and lighting are drawn to scale using this, so please check it)' });
    const msg = h('div');
    const save = h('button', { class: 'btn' }, 'Save');
    const retag = status.ai.configured && !isPages ? h('button', { class: 'btn ghost' }, 'Re-tag with AI') : '';
    const overlay = h('div', { class: 'overlay' }, h('div', { class: 'modal' },
      h('div', { class: 'modal-head' }, h('h2', {}, 'Edit item'), h('button', { class: 'btn ghost', onclick: () => overlay.remove() }, 'Close')),
      h('img', { src: i.thumb, alt: '', style: 'max-height:160px;border-radius:8px;margin-bottom:10px' }),
      h('div', { class: 'grid', style: 'grid-template-columns:1fr' }, name.el, h('label', { class: 'f' }, 'Category', cat), tags.el, (i.category === 'furniture' || i.category === 'lighting') ? height.el : ''), msg,
      h('div', { style: 'display:flex;gap:10px;margin-top:14px' }, save, retag)));
    save.addEventListener('click', async () => {
      try {
        const u = await api(`/api/library/${i.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.value, category: cat.value, tags: tags.value, ...((i.category === 'furniture' || i.category === 'lighting') && height.value ? { height_cm: height.value } : {}) }) });
        items = items.map(x => (x.id === i.id ? u : x)); overlay.remove(); drawChips(); drawGrid();
      } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    });
    if (retag) retag.addEventListener('click', async () => {
      retag.disabled = true; retag.textContent = 'Working…';
      try {
        const r = await api(`/api/library/${i.id}/retag`, { method: 'POST' });
        items = items.map(x => (x.id === i.id ? r.item : x)); overlay.remove(); drawChips(); drawGrid(); if (r.note) toast(r.note);
      } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); retag.disabled = false; retag.textContent = 'Re-tag with AI'; }
    });
    document.body.append(overlay);
  }
  const fileInput = h('input', { type: 'file', accept: 'image/*,.pdf,application/pdf', multiple: true, hidden: true });
  const drop = h('div', { class: 'drop', tabindex: '0', role: 'button' }, h('strong', {}, 'Choose images or a PDF'), kind === 'work' ? ' or drop them here to add to the library' : isPages ? ` or drop them here to add to the ${kind === 'references' ? 'References' : 'Company pages'}` : ' or drop them here to add to the selected section');
  const pdfPanel = h('div');
  async function upload(all) {
    all = [...all];
    const pdfs = all.filter(f => /\.pdf$/i.test(f.name) || f.type === 'application/pdf');
    const imgs = all.filter(f => f.type.startsWith('image/'));
    if (pdfs.length) { await reviewPdf(pdfs[0], isPages ? 'pages' : 'images'); if (pdfs.length > 1) pdfPanel.append(h('div', { class: 'hint' }, 'Only the first PDF was opened. Add the others one at a time afterwards.')); }
    if (imgs.length) await uploadImages(imgs);
  }
  async function reviewPdf(file, mode) {
    pdfPanel.replaceChildren(h('div', { class: 'msg info' }, `Reading ${file.name}… a big PDF can take a minute.`));
    let r;
    try { const fd = new FormData(); fd.append('file', file); fd.append('mode', mode); r = await api('/api/library/pdf', { method: 'POST', body: fd }); }
    catch (e) { pdfPanel.replaceChildren(h('div', { class: 'msg error' }, e.message)); return; }
    const target = (cats.find(c => c.key === libCat) || {}).label || 'the library';
    const chosen = new Set(r.items.map(i => i.id));
    const grid = h('div', { class: 'pdf-grid' }), count = h('strong', {});
    const addBtn = h('button', { type: 'button', class: 'btn' }, '');
    const draw = () => {
      grid.replaceChildren(...r.items.map(i => { const on = chosen.has(i.id);
        return h('button', { type: 'button', class: 'pdf-item' + (on ? ' on' : ''), title: `Page ${i.page}, ${i.width}×${i.height}. Click to ${on ? 'leave out' : 'include'}`,
          onclick: () => { on ? chosen.delete(i.id) : chosen.add(i.id); draw(); } }, h('img', { src: i.thumb, alt: '' }), h('span', { class: 'tick' }, on ? '✓' : ''), h('span', { class: 'pg' }, `p.${i.page}`)); }));
      count.textContent = `${chosen.size} of ${r.items.length} selected`; addBtn.textContent = `Add ${chosen.size} to ${target}`; addBtn.disabled = !chosen.size;
    };
    draw();
    const cancel = async () => { try { await api(`/api/library/pdf/${r.token}`, { method: 'DELETE' }); } catch { /* ignore */ } pdfPanel.replaceChildren(); };
    addBtn.addEventListener('click', async () => {
      pdfPanel.replaceChildren(h('div', { class: 'msg info' }, 'Adding the pictures…'));
      let res;
      try { res = await api('/api/library/pdf/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: r.token, category: libCat, ids: [...chosen] }) }); }
      catch (e) { pdfPanel.replaceChildren(h('div', { class: 'msg error' }, e.message)); return; }
      items.push(...res.items); drawChips(); drawGrid();
      if (isPages || !status.ai.configured || !res.items.length) { pdfPanel.replaceChildren(h('div', { class: 'msg info' }, `Added ${res.items.length} picture${res.items.length === 1 ? '' : 's'}.` + (kind === 'company' ? ' Use Edit to give each page a clear name.' : status.ai.configured ? '' : ' Automatic tagging is off, so they have file-style names and no tags.'))); return; }
      let failed = 0;
      for (const [n, it] of res.items.entries()) {
        pdfPanel.replaceChildren(h('div', { class: 'msg info' }, `Added ${res.items.length} pictures. Naming and tagging them with AI: ${n + 1} of ${res.items.length}…`));
        try { const t = await api(`/api/library/${it.id}/retag`, { method: 'POST' }); items = items.map(x => (x.id === it.id ? t.item : x)); if (t.note) failed++; } catch { failed++; }
        drawGrid();
      }
      pdfPanel.replaceChildren(h('div', { class: 'msg info' }, `Done. ${res.items.length} pictures added and tagged.` + (failed ? ` ${failed} couldn't be tagged automatically; you can use Edit > Re-tag with AI on those.` : '')));
    });
    pdfPanel.replaceChildren(h('div', { class: 'brandbox' },
      h('strong', {}, `Pictures found in ${r.name}`), h('div', { class: 'hint', style: 'margin:4px 0 8px' }, `${r.items.length} picture${r.items.length === 1 ? '' : 's'} from ${r.pages} page${r.pages === 1 ? '' : 's'}. Click a picture to leave it out. Logos repeated on every page, tiny icons and plain backgrounds were skipped already.`),
      r.note ? h('div', { class: 'msg info', style: 'margin:0 0 8px' }, r.note) : '',
      h('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px' }, count,
        h('button', { type: 'button', class: 'btn link', onclick: () => { r.items.forEach(i => chosen.add(i.id)); draw(); } }, 'Select all'),
        h('button', { type: 'button', class: 'btn link', onclick: () => { chosen.clear(); draw(); } }, 'Select none'),
        r.mode === 'images' && !isPages ? h('button', { type: 'button', class: 'btn link', onclick: async () => { await api(`/api/library/pdf/${r.token}`, { method: 'DELETE' }).catch(() => {}); reviewPdf(file, 'pages'); } }, 'Use whole pages instead') : ''),
      grid, h('div', { style: 'display:flex;gap:10px;margin-top:12px' }, addBtn, h('button', { type: 'button', class: 'btn ghost', onclick: cancel }, 'Cancel'))));
  }
  async function uploadImages(files) {
    if (!files.length) return;
    const notes = [];
    for (const [n, f] of files.entries()) {
      upStatus.replaceChildren(h('div', { class: 'msg info' }, `Adding ${n + 1} of ${files.length}: ${f.name}${status.ai.configured ? ' (tagging with AI…)' : ''}`));
      const fd = new FormData(); fd.append('category', libCat); fd.append('file', f);
      try {
        const r = await api('/api/library', { method: 'POST', body: fd });
        items.push(r.item); if (r.note) notes.push(`${f.name}: ${r.note}`);
      } catch (e) { notes.push(`${f.name}: ${e.message}`); }
      drawChips(); drawGrid();
    }
    upStatus.replaceChildren(h('div', { class: 'msg info' }, `Done. ${files.length} file${files.length === 1 ? '' : 's'} processed.`, ...notes.map(t => h('div', { class: 'hint' }, t))));
  }
  fileInput.addEventListener('change', () => { upload(fileInput.files); fileInput.value = ''; });
  drop.addEventListener('click', () => fileInput.click());
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files); });

  const aiLine = h('div', { class: 'msg ' + (status.ai.configured ? 'info' : 'error') }, status.ai.configured
    ? '✓ Automatic tagging is on: each image you add is named and tagged for you.' : '✗ Automatic tagging is off, so images are added with their file name only. See the System check on the Library tab.');
  const intro = kind === 'work'
    ? ['Library', 'Photos of your previous work. Each proposal\u2019s mood board (the Design Vision & Inspiration page) is chosen from these to suit the design. Add real photos of finished stands, in a good range of styles. You can also add a PDF full of examples: the pictures inside it (including close-ups of details) are pulled out for you to review.']
    : kind === 'references' ? ['References', 'Examples of our work: project pages from the company profile. When a salesperson switches on \u201cReferences\u201d in a proposal, they choose from these and put them in order after the Company pages. Add a PDF to bring in every page as its own page, or add images (landscape pages like your slides work best). Use Edit to give each page a clear name.']
    : kind === 'company' ? ['Company', 'The company information pages (who we are, our facilities, our team). When a salesperson switches on \u201cCompany\u201d in a proposal, they choose from these and put them in order after the Terms. Add a PDF to bring in every page as its own page, or add images (landscape pages like your slides work best). Use Edit to give each page a clear name.']
    : ['Materials', 'Pictures for the Materials & Furnishings page. Choose a section, then add images: flooring, materials, furniture, lighting and feature walls (a curtain, slatted wall and so on). Furniture and lighting work best as a photo on a plain background or a transparent PNG. Furniture works best as a photo on a plain background or a transparent PNG.'];
  holder.replaceChildren(kind === 'work' ? check : isPages ? '' : aiLine,
    h('section', { class: 'card' }, h('h2', {}, intro[0]), h('p', { class: 'sub' }, intro[1]),
      chips, guide, drop, fileInput, upStatus, pdfPanel, h('div', { style: 'margin-top:16px' }, gridHolder)));
  drawChips(); drawGrid();
}

// ---------------------------------------------------------------- Terms: a preview for everyone, and (admins) the wording
function termsImages(urls) { return urls.map(u => h('img', { src: u, alt: 'Terms page', class: 'terms-img' })); }
async function renderTerms() {
  const holder = h('div', {}, h('p', { class: 'hint' }, 'Loading\u2026'));
  shell(holder);
  let data;
  try { data = await api('/api/terms'); } catch (e) { holder.replaceChildren(h('div', { class: 'msg error' }, e.message)); return; }
  let pages = JSON.parse(JSON.stringify(data.pages));
  const msg = h('div'), prev = h('div'), editors = h('div');
  const json = { 'Content-Type': 'application/json' };
  function col(label, key, p) {
    const ta = h('textarea', { class: 'terms-ta', spellcheck: 'true', 'aria-label': label }); ta.value = p[key] || '';
    ta.addEventListener('input', () => { p[key] = ta.value; });
    return h('label', { class: 'f' }, label, ta);
  }
  function draw() {
    editors.replaceChildren(...pages.map((p, i) => h('section', { class: 'card' },
      h('div', { class: 'title-row' }, h('h2', {}, `Page ${i + 1}`), pages.length > 1 ? h('button', { type: 'button', class: 'btn link danger', onclick: () => { pages.splice(i, 1); draw(); } }, 'Remove page') : ''),
      h('div', { class: 'terms-cols' }, col('Left column', 'left', p), col('Right column', 'right', p)))));
    add.style.display = pages.length >= 4 ? 'none' : '';
  }
  const busy = (b, t, on) => { b.disabled = on; b.textContent = on ? t : b.dataset.label; };
  const mkBtn = (label, cls, fn) => { const b = h('button', { type: 'button', class: cls }, label); b.dataset.label = label; b.addEventListener('click', () => fn(b)); return b; };
  const add = mkBtn('Add a page', 'btn ghost', () => { pages.push({ left: '', right: '' }); draw(); });
  const show = (urls) => prev.replaceChildren(...(urls ? termsImages(urls) : [h('div', { class: 'msg error' }, 'The preview could not be made.')]));
  const previewBtn = mkBtn('Preview', 'btn ghost', async b => {
    msg.replaceChildren(); busy(b, 'Making the preview\u2026', true);
    try { const r = await api('/api/terms/preview', { method: 'POST', headers: json, body: JSON.stringify({ pages }) }); show(r.urls); } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    busy(b, '', false);
  });
  const saveBtn = mkBtn('Save for everyone', 'btn', async b => {
    msg.replaceChildren(); busy(b, 'Saving\u2026', true);
    try { const r = await api('/api/terms', { method: 'PUT', headers: json, body: JSON.stringify({ pages }) }); msg.replaceChildren(h('div', { class: 'msg info' }, 'Saved. Every proposal made or edited from now on uses this wording.')); show(r.urls); } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    busy(b, '', false);
  });
  const resetBtn = mkBtn('Put back the original wording', 'btn link', () => {
    if (!confirm('Replace what is in the boxes with the original wording? Nothing changes for anyone until you press Save.')) return;
    pages = JSON.parse(JSON.stringify(data.default)); draw(); prev.replaceChildren();
  });
  holder.replaceChildren(
    h('section', { class: 'card' }, h('h2', {}, 'Terms'),
      h('p', { class: 'sub' }, 'The Payment Terms and Terms & Conditions pages that go into proposals. Change the wording here and everyone\u2019s proposals use it. Each page has two columns, laid out like the printed page. Press Preview to see how it will look before you save.'),
      h('div', { class: 'terms-help' },
        h('div', {}, h('code', {}, '# Main heading'), ' a capitalised section heading'),
        h('div', {}, h('code', {}, '## Sub-heading'), ' a smaller heading, followed by its paragraph on the next line'),
        h('div', {}, 'A blank line starts a new paragraph.'),
        h('div', {}, h('code', {}, '| Lead time | 1^st^ stage | 2^nd^ stage |'), ' a table row (the first row is the header). ', h('code', {}, '^st^'), ' makes small raised letters.')),
      data.updated ? h('p', { class: 'hint' }, `Last saved ${data.updated}.`) : h('p', { class: 'hint' }, 'Showing the original wording; nothing has been changed yet.')),
    editors,
    h('div', { class: 'terms-actions' }, add, previewBtn, saveBtn, resetBtn), msg, prev);
  draw();
}

// ---------------------------------------------------------------- your own details and password
function renderProfile() {
  const holder = h('div');
  shell(holder);
  const json = { 'Content-Type': 'application/json' };
  const n = textField('Name', { required: true }), jb = textField('Job title', { required: true }), ph = textField('Contact number', { required: true, type: 'tel' });
  n.input.value = user.name || ''; jb.input.value = user.job_title || ''; ph.input.value = user.phone || '';
  const msg = h('div'), save = h('button', { type: 'button', class: 'btn' }, 'Save details');
  save.addEventListener('click', async () => {
    msg.replaceChildren(); save.disabled = true;
    try {
      const r = await api('/api/me', { method: 'PATCH', headers: json, body: JSON.stringify({ name: n.value, job_title: jb.value, phone: ph.value }) });
      user = { ...user, ...r }; msg.replaceChildren(h('div', { class: 'msg info' }, 'Your details are saved.'));
    } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    save.disabled = false;
  });
  const cur = textField('Current password', { type: 'password' }), np = textField('New password', { type: 'password', hint: '(at least 5 characters)' }), np2 = textField('New password again', { type: 'password' });
  for (const f of [cur, np, np2]) f.input.setAttribute('autocomplete', f === cur ? 'current-password' : 'new-password');
  const pmsg = h('div'), change = h('button', { type: 'button', class: 'btn' }, 'Change password');
  change.addEventListener('click', async () => {
    pmsg.replaceChildren();
    if (!cur.input.value) { pmsg.replaceChildren(h('div', { class: 'msg error' }, 'Type your current password.')); return; }
    if (np.input.value !== np2.input.value) { pmsg.replaceChildren(h('div', { class: 'msg error' }, 'The two new passwords don\u2019t match.')); return; }
    change.disabled = true;
    try {
      await api('/api/me/password', { method: 'POST', headers: json, body: JSON.stringify({ current: cur.input.value, password: np.input.value }) });
      cur.input.value = np.input.value = np2.input.value = ''; pmsg.replaceChildren(h('div', { class: 'msg info' }, 'Your password has been changed. Use it next time you sign in.'));
    } catch (e) { pmsg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    change.disabled = false;
  });
  holder.replaceChildren(
    h('section', { class: 'card' }, h('h2', {}, 'Your details'), h('p', { class: 'sub' }, `You sign in as ${user.email}.`),
      h('div', { class: 'grid' }, n.el, jb.el, ph.el), h('div', { style: 'margin-top:14px' }, save), msg),
    h('section', { class: 'card' }, h('h2', {}, 'Change password'),
      h('div', { class: 'grid' }, cur.el, np.el, np2.el), h('div', { style: 'margin-top:14px' }, change), pmsg));
}

// ---------------------------------------------------------------- users (admin)
function randomPassword() {
  const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const buf = new Uint32Array(14); crypto.getRandomValues(buf);
  return [...buf].map(n => chars[n % chars.length]).join('');
}
async function renderUsers() {
  const holder = h('div', {}, h('p', { class: 'hint' }, 'Loading…'));
  shell(holder);
  let status = null;
  try { status = await api('/api/status'); } catch { /* the system check just won't show */ }
  const mailOn = !!(status && status.mail && status.mail.configured);
  const email = textField('Email', { type: 'email', placeholder: 'name@company.com' });
  const uName = textField('Name', { required: true }), uJob = textField('Job title', { required: true }), uPhone = textField('Contact number', { required: true, type: 'tel' });
  const pw = textField('Password', { hint: '(at least 5 characters)' });
  const admin = h('input', { type: 'checkbox' });
  const mailBox = h('input', { type: 'checkbox' }); mailBox.checked = true;
  const msg = h('div');
  const gen = h('button', { type: 'button', class: 'btn ghost', onclick: () => { pw.input.value = randomPassword(); } }, 'Generate password');
  const add = h('button', { type: 'button', class: 'btn' }, 'Add user');
  add.addEventListener('click', async () => {
    msg.replaceChildren(); add.disabled = true;
    try {
      const res = await api('/api/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.value, password: pw.input.value, is_admin: admin.checked, name: uName.value, job_title: uJob.value, phone: uPhone.value, send_email: mailOn && mailBox.checked }) });
      msg.replaceChildren(h('div', { class: 'msg info' }, res.emailed ? `Account created. An email with their login details has been sent to ${email.value}.`
        : res.email_problem ? `Account created, but the email couldn\u2019t be sent (${res.email_problem}). Send ${email.value} their password yourself: ${pw.input.value}`
        : `Account created. Send ${email.value} their password: ${pw.input.value}`));
      email.input.value = ''; pw.input.value = ''; uName.input.value = ''; uJob.input.value = ''; uPhone.input.value = ''; admin.checked = false; loadUsers();
    } catch (e) { msg.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
    add.disabled = false;
  });
  const listEl = h('div');
  async function loadUsers() {
    try {
      const rows = await api('/api/users');
      listEl.replaceChildren(h('div', { class: 'table-scroll' }, h('table', { class: 'list' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Name'), h('th', {}, ''))),
        ...rows.map(r => {
          const body = h('tbody', { class: 'user-row' });
          const label = () => r.name || r.email;
          const show = () => {
            body.replaceChildren(h('tr', {},
              h('td', {}, h('strong', {}, label()), h('div', { class: 'hint', style: 'margin-top:2px' }, r.last_login ? 'Last login ' + new Date(r.last_login).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : 'Never signed in')),
              h('td', { class: 'user-actions' },
                h('button', { class: 'btn ghost', onclick: edit }, 'Edit'), ' ',
                h('button', { class: 'btn ghost', onclick: async () => {
                  const np = prompt(`New password for ${label()} (at least 5 characters):`, randomPassword());
                  if (!np) return;
                  try { await api(`/api/users/${r.id}/password`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: np, send_email: mailOn }) }).then(res => toast(res.emailed ? `Password changed and emailed to ${label()}.` : `Password changed${res.email_problem ? ' (the email couldn\u2019t be sent: ' + res.email_problem + ')' : ''}. Tell ${label()}: ${np}`)); } catch (e) { toast(e.message); }
                } }, 'Reset password'), ' ',
                h('button', { class: 'btn link danger', onclick: async () => {
                  if (!confirm(`Remove ${label()}? They will no longer be able to sign in. Their proposals will be moved to your account.`)) return;
                  try { await api(`/api/users/${r.id}`, { method: 'DELETE' }); loadUsers(); } catch (e) { toast(e.message); }
                } }, 'Remove'))));
          };
          const edit = () => {
            const n = textField('Name', { required: true }), jb = textField('Job title', { required: true }), ph = textField('Contact number', { required: true, type: 'tel' });
            n.input.value = r.name || ''; jb.input.value = r.job_title || ''; ph.input.value = r.phone || '';
            const err = h('div');
            body.replaceChildren(h('tr', {}, h('td', { colspan: '2' },
              h('strong', {}, label()), h('div', { class: 'hint', style: 'margin:2px 0 10px' }, `${r.email} \u00b7 ${r.is_admin ? 'Admin' : 'Sales'}`),
              h('div', { class: 'grid' }, n.el, jb.el, ph.el),
              h('div', { style: 'display:flex;gap:10px;margin-top:14px' },
                h('button', { class: 'btn', onclick: async () => {
                  try {
                    await api(`/api/users/${r.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: n.value, job_title: jb.value, phone: ph.value }) });
                    r.name = n.value; r.job_title = jb.value; r.phone = ph.value; show();
                  } catch (e) { err.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
                } }, 'Save'), h('button', { class: 'btn ghost', onclick: show }, 'Cancel')), err)));
          };
          show(); return body;
        }))));
    } catch (e) { listEl.replaceChildren(h('div', { class: 'msg error' }, e.message)); }
  }
  holder.replaceChildren(
    h('section', { class: 'card' }, h('h2', {}, 'Add a salesperson'), h('p', { class: 'sub' }, 'They sign in with this email and password. Their proposals are private to them.'),
      h('div', { class: 'grid' }, uName.el, uJob.el, uPhone.el, email.el, pw.el), h('div', { style: 'display:flex;gap:12px;align-items:center;margin-top:14px;flex-wrap:wrap' }, gen, h('label', { style: 'display:flex;gap:6px;align-items:center' }, admin, 'Can manage users and see all proposals'), ...(mailOn ? [h('label', { style: 'display:flex;gap:6px;align-items:center' }, mailBox, 'Email them their login details')] : []), add), msg, ...(mailOn ? [] : [h('p', { class: 'hint', style: 'margin-top:10px' }, 'Emails aren\u2019t set up yet, so you\u2019ll need to send each person their password yourself. See the System check below.')])),
    listEl);
  loadUsers();
  if (status) holder.append(systemCard(status));
}

// ---------------------------------------------------------------- the new-proposal form
const LINES = [['rental_structure', 'Rental Structure'], ['graphics', 'Graphics'], ['custom_rental', 'Custom Rental'], ['flooring', 'Flooring'],
  ['audio_visual', 'Audio Visual'], ['furniture', 'Furniture'], ['installation_dismantle', 'Installation & Dismantle'], ['project_management', 'Project Management'], ['logistics_handling', 'Logistics & Handling']];
const DETAIL_KEYS = ['custom_rental', 'flooring', 'audio_visual', 'furniture'];   // lines that list their contents as bullet points on the quote
const DATES = [['show_start', 'Show start date'], ['concept_signoff', 'Design & engagement concept sign off'], ['quote_signoff', 'Stand submission to organiser'],
  ['artwork_deadline', 'Final graphics artwork deadline'], ['rush_after', 'Rush charges may be applied after'], ['preshow_prep', 'Stand pre-show preparation'], ['dispatch', 'Dispatch date']];

function renderForm() {
  const editId = editing; editing = null;
  const form = { shows: [] };
  const cur = h('select', { 'aria-label': 'Currency' }, ...['£', '€', '$'].map(c => h('option', { value: c }, c)));
  let critCard = null, termsCard = null, moodCard = null, matCard = null, aboutCard = null, refCard = null;  // built further down
  const curLabels = [];  // "Prices in £" notes on the later shows' quotes

  // client
  const cName = textField('Client name', { required: true });
  const cSite = textField('Client website', { type: 'url', placeholder: 'https://', hint: '(we\u2019ll look for the logo here)' });
  const cLogo = fileField({ label: 'Client logo', required: true, pdf: false, hint: 'PNG or JPG. Found automatically from the website if possible; you can upload your own instead.' });
  let logoFromSite = false;
  const brandOut = h('div');
  const brandBtn = h('button', { type: 'button', class: 'btn ghost' }, 'Find logo');
  { const row = h('div', { class: 'site-row' }); cSite.el.append(row); row.append(cSite.input, brandBtn); }
  function dataUrlToFile(dataUrl, name) {
    const bin = atob(dataUrl.split(',')[1]); const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: 'image/png' });
  }
  async function findBrand() {
    if (!cSite.value) { brandOut.replaceChildren(h('div', { class: 'msg error' }, 'Enter the client\u2019s website address first.')); return; }
    brandBtn.disabled = true; brandBtn.textContent = 'Reading the website…';
    brandOut.replaceChildren(h('div', { class: 'msg info' }, 'Reading the website. This can take up to half a minute…'));
    try {
      const r = await api('/api/brand', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: cSite.value }) });
      siteColours = r.colours || [];
      const autoLogo = !cLogo.files.length || logoFromSite;
      const tiles = r.logos.map((l, i) => h('button', { type: 'button', class: 'logo-opt', title: 'Use this logo', onclick: () => {
        cLogo.clear(); cLogo.put(dataUrlToFile(l.data_url, 'website-logo.png')); logoFromSite = true;
        tiles.forEach((t, k) => t.classList.toggle('on', k === i));
      } }, h('img', { src: l.data_url, alt: 'Logo option ' + (i + 1) }), h('span', {}, `${l.width}×${l.height}` + (l.light ? ' · very light' : ''))));
      if (autoLogo && r.logos.length) tiles[0].click();
      brandOut.replaceChildren(h('div', { class: 'brandbox' },
        h('strong', {}, r.site ? `Found on ${r.site}` : 'Found on the website'),
        siteColours.length ? h('div', { class: 'hint', style: 'margin:6px 0 0;display:flex;align-items:center;gap:6px;flex-wrap:wrap' }, 'Website colours:', ...siteColours.map(c => h('span', { title: c, style: `width:18px;height:18px;border-radius:50%;background:${c};border:1px solid #d5d5e5;display:inline-block` }))) : '',
        r.logos.length ? h('div', {}, h('div', { class: 'hint', style: 'margin:6px 0 4px' }, r.logos.length > 1 ? 'Logo options: click one to use it' : 'Logo found'), h('div', { class: 'logo-opts' }, tiles)) : '',
        ...r.warnings.map(w => h('div', { class: 'msg info', style: 'margin:8px 0 0' }, w))));
    } catch (e) {
      brandOut.replaceChildren(h('div', { class: 'msg error' }, e.message + ' You can upload the logo yourself below.'));
    }
    brandBtn.disabled = false; brandBtn.textContent = 'Find logo';
    try { if (swMat.checked) mats.fromLogo(true); } catch { /* the materials section isn't built yet */ }
  }
  brandBtn.addEventListener('click', findBrand);
  cSite.input.addEventListener('change', () => { if (cSite.value) findBrand(); });
  const clientCard = h('section', { class: 'card' }, h('h2', {}, 'Client'),
    h('div', { class: 'grid' }, cName.el, cSite.el), brandOut, h('div', { class: 'nowrap-hint', style: 'margin-top:14px' }, cLogo.el));

  // shows: every show is a "Show Details" card followed by its own "Renders & Technical Drawings" card
  const showsFlow = h('div'), detailsFlow = h('div');
  function addShow() {
    const s = {
      name: textField('Show name', { required: true, placeholder: 'e.g. IBC' }),
      location: textField('Venue / location', { required: true, placeholder: 'e.g. Rai Amsterdam' }),
      dates: dateRangeField('Dates', { required: true }),
      stand: standField('Stand size'),
      floorplan: fileField({ label: '', firstPage: true, onChange: () => s.booth && s.booth.refresh() }),
      renders: fileField({ label: '', multiple: true, required: true, annotate: !!(user && user.is_admin) }),
    };
    s.booth = boothEditor(() => s.floorplan.files[0]);
    s.dHead = h('h2', {}, ''); s.rHead = h('h2', {}, ''); s.rSub = h('p', { class: 'sub' }, ''); s.qHead = h('h2', {}, '');
    s.q = {}; s.qTotal = h('strong', {}, ''); s.qSub = h('span', {}, ''); s.cRows = []; s.oRows = [];
    s.discount = h('input', { type: 'text', 'aria-label': 'Discount' }); s.qd = {};
    s.custom = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Custom quote fields' });
    s.recalc = () => {
      const src = s.custom.checked ? s.cRows.map(r => r.amount) : Object.values(s.q);
      const sub = src.reduce((a, i) => a + num(i.value), 0), d = Math.abs(num(s.discount.value));
      curSigns.forEach(sp => { sp.textContent = cur.value; });
      s.qSub.textContent = money(sub, cur.value);
      s.qTotal.textContent = money(sub - Math.min(d, Math.max(sub, 0)), cur.value);
    };
    s.discount.addEventListener('input', s.recalc);
    const qGrid = h('div', { class: 'grid' }, LINES.map(([k, l]) => {
      const i = h('input', { type: 'text' });
      i.addEventListener('input', s.recalc); s.q[k] = i;
      let det = '';
      if (DETAIL_KEYS.includes(k)) { det = h('textarea', { rows: '2', maxlength: '700', placeholder: 'Bullet points shown on the quote, one per line (optional)', 'aria-label': `${l} details`, class: 'qdetail' }); s.qd[k] = det; }
      return h('label', { class: 'f' }, l, moneyBox(i, cur.value), det);
    }));
    // custom quote fields: the salesperson writes their own lines
    const cHolder = h('div');
    function addCustomRow(name = '', amount = '') {
      const r = { name: h('input', { type: 'text', placeholder: 'Field name, e.g. Reception counter', maxlength: 70, value: name, 'aria-label': 'Field name' }),
        desc: h('input', { type: 'text', placeholder: 'Short description (optional)', maxlength: 100, 'aria-label': 'Field description' }),
        amount: h('input', { type: 'text', 'aria-label': 'Amount' }) };
      setMoney(r.amount, amount);
      r.amount.addEventListener('input', s.recalc);
      r.el = h('div', { class: 'crow' }, h('div', { class: 'cnames' }, r.name, r.desc), moneyBox(r.amount, cur.value),
        h('button', { type: 'button', class: 'btn link danger', title: 'Remove this field', onclick: () => { s.cRows.splice(s.cRows.indexOf(r), 1); r.el.remove(); s.recalc(); } }, '✕'));
      s.cRows.push(r); cHolder.append(r.el);
    }
    s.addCustomRow = addCustomRow;
    // options: free-text extras the client can choose, each with a + or - value (not part of the total)
    const oHolder = h('div');
    function addOption(text = '', sign = '+', amount = '') {
      const r = { text: h('input', { type: 'text', placeholder: 'Option, e.g. Upgrade flooring to timber', maxlength: 80, value: text, 'aria-label': 'Option' }),
        sign: h('select', { 'aria-label': 'Add or subtract' }, h('option', { value: '+' }, '+'), h('option', { value: '-' }, '\u2212')),
        amount: h('input', { type: 'text', 'aria-label': 'Option value' }) };
      setMoney(r.amount, amount);
      r.sign.value = sign === '-' ? '-' : '+';
      r.el = h('div', { class: 'orow' }, r.text, r.sign, moneyBox(r.amount, cur.value),
        h('button', { type: 'button', class: 'btn link danger', title: 'Remove this option', onclick: () => { s.oRows.splice(s.oRows.indexOf(r), 1); r.el.remove(); } }, '\u2715'));
      s.oRows.push(r); oHolder.append(r.el);
    }
    s.addOption = addOption;
    const cPanel = h('div', { style: 'display:none' }, h('p', { class: 'hint' }, 'These start as the standard fields (with any prices you\u2019ve already typed). Rename or remove any of them, and add new fields of your own. Each field needs a name and an amount to appear; the description is optional and shows under the name. Up to 16 fields.'), cHolder,
      h('p', {}, h('button', { type: 'button', class: 'btn ghost', onclick: () => { if (s.cRows.length < 16) addCustomRow(); else toast('That\u2019s the maximum of 16 fields.'); } }, '+ Add new field')));
    s.custom.addEventListener('change', () => {
      if (s.custom.checked && !s.cSeeded) { LINES.forEach(([k, l]) => addCustomRow(l, s.q[k].value)); s.cSeeded = true; }
      qGrid.style.display = s.custom.checked ? 'none' : ''; cPanel.style.display = s.custom.checked ? '' : 'none'; s.recalc();
    });
    s.qCur = form.shows.length === 0
      ? h('label', { class: 'f cur-row' }, h('span', {}, 'Currency'), cur)
      : (() => { const sp = h('span', { class: 'hint' }, ''); curLabels.push(sp); return sp; })();
    s.qOn = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include the Quotation page' });
    const qBody = h('div', { style: 'display:none' },
      h('label', { class: 'switch-row' }, s.custom, h('span', { class: 'slider' }), h('span', {}, 'Custom quote fields')),
      qGrid, cPanel,
      h('div', { class: 'qsum' },
        h('div', { class: 'qsum-row' }, h('span', {}, 'Subtotal'), s.qSub),
        h('div', { class: 'qsum-row' }, h('span', {}, 'Discount'), moneyBox(s.discount, cur.value)),
        h('div', { class: 'total-row' }, h('span', {}, 'Quotation total (excluding VAT)'), s.qTotal)),
      h('h3', { class: 'opt-head' }, 'Options'),
      h('p', { class: 'hint' }, 'Extras or alternatives shown under the quotation, each with a value added (+) or taken off (\u2212). They are not included in the total. Up to 8.'),
      oHolder, h('p', {}, h('button', { type: 'button', class: 'btn ghost', onclick: () => { if (s.oRows.length < 8) addOption(); else toast('That\u2019s the maximum of 8 options.'); } }, '+ Add option')));
    s.qOn.addEventListener('change', () => { qBody.style.display = s.qOn.checked ? '' : 'none'; s.qCur.style.display = s.qOn.checked ? '' : 'none'; });
    s.qCur.style.display = 'none';
    s.qCard = h('section', { class: 'card' }, titleRow(s.qHead, s.qOn, s.qCur, true), qBody);
    s.recalc();
    s.rm = h('button', { type: 'button', class: 'btn link danger', onclick: () => { form.shows.splice(form.shows.indexOf(s), 1); s.dCard.remove(); s.fCard.remove(); s.rCard.remove(); s.qCard.remove(); renumber(); } }, 'Remove this show');
    s.dCard = h('section', { class: 'card' }, h('div', { style: 'display:flex;justify-content:space-between;align-items:flex-start;gap:12px' }, s.dHead, s.rm),
      h('div', { class: 'grid grid2', style: 'margin-top:10px' }, s.name.el, s.location.el, s.dates.el, s.stand.el));
    s.fOn = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include the floorplan page' });
    s.fHead = h('h2', {}, '');
    const fBody = h('div', { style: 'display:none;margin-top:12px' }, s.floorplan.el, s.booth.el);
    s.fOn.addEventListener('change', () => { fBody.style.display = s.fOn.checked ? '' : 'none'; });
    s.fCard = h('section', { class: 'card' }, titleRow(s.fHead, s.fOn), fBody);
    s.rCard = h('section', { class: 'card' }, s.rHead, s.rSub, s.renders.el);
    s.name.input.addEventListener('input', renumber);
    form.shows.push(s); detailsFlow.append(s.dCard, s.fCard, s.rCard); showsFlow.append(s.qCard); renumber();
  }
  function renumber() {
    const many = form.shows.length > 1;
    clientCard.querySelector('h2').textContent = `Client`;
    form.shows.forEach((s, i) => {   // each show's details are followed by its renders
      s.dHead.textContent = `${many ? `Show ${i + 1} Details` : 'Show Details'}`;
      s.fHead.textContent = many ? `Show ${i + 1} Floorplan` : 'Floorplan';
      s.rHead.textContent = `${many ? `Show ${i + 1} Renders & Technical Drawings` : 'Renders & Technical Drawings'}`;
      s.rSub.textContent = (many && s.name.value ? `For ${s.name.value}. ` : '') + 'Add the design renders and technical drawings. PDFs are split into one page each.';
      s.rm.style.display = i === 0 ? 'none' : '';
    });
    moodCard.querySelector('h3').textContent = `Design Vision & Inspiration`;
    if (matCard) matCard.querySelector('h3').textContent = `Materials & Furnishings`;
    form.shows.forEach((s, i) => { s.qHead.textContent = `${many ? `Show ${i + 1} Quotation` : 'Quotation'}`; });
    curLabels.forEach(sp => { sp.textContent = `Prices in ${cur.value}`; });
    if (critCard) critCard.querySelector('h2').textContent = `Critical Dates`;
    if (termsCard) termsCard.querySelector('h2').textContent = `Payment Terms`;
    if (aboutCard) aboutCard.querySelector('h2').textContent = `Company`;
    if (refCard) refCard.querySelector('h2').textContent = `References`;
  }
  const addShowRow = h('div', { style: 'margin:-4px 0 16px' }, h('button', { type: 'button', class: 'btn ghost', onclick: () => { if (form.shows.length < 6) addShow(); } }, '+ Add another show'),
    ' ', h('span', { class: 'hint' }, 'Only if the same proposal covers more than one show. You\u2019ll add its details and renders next.'));

  cur.addEventListener('change', () => { curSigns.forEach(sp => { sp.textContent = cur.value; }); form.shows.forEach(s => s.recalc()); curLabels.forEach(sp => { sp.textContent = `Prices in ${cur.value}`; }); });

  // critical dates
  const dateInputs = {};
  const swCrit = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include Critical Dates page' });
  const swPay = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include Payment Terms pages' });
  const datesGrid = h('div', { class: 'grid', style: 'display:none;margin-top:10px' }, DATES.map(([k, l]) => { const t = dateRangeField(l, { single: true }); dateInputs[k] = t; return t.el; }));
  swCrit.addEventListener('change', () => { datesGrid.style.display = swCrit.checked ? '' : 'none'; });
  critCard = h('section', { class: 'card' }, titleRow(h('h2', {}, 'Critical Dates'), swCrit),
    datesGrid);
  termsCard = h('section', { class: 'card' }, titleRow(h('h2', {}, 'Payment Terms'), swPay));

  const about = aboutPicker();
  const swAbout = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include company pages' });
  const aboutBody = h('div', { style: 'display:none;margin-top:12px' }, about.el);
  swAbout.addEventListener('change', () => { aboutBody.style.display = swAbout.checked ? '' : 'none'; });
  aboutCard = h('section', { class: 'card' }, titleRow(h('h2', {}, 'Company'), swAbout), aboutBody);
  const refs = aboutPicker('references', 'References');
  const swRefs = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include references pages' });
  const refsBody = h('div', { style: 'display:none;margin-top:12px' }, refs.el);
  swRefs.addEventListener('change', () => { refsBody.style.display = swRefs.checked ? '' : 'none'; });
  refCard = h('section', { class: 'card' }, titleRow(h('h2', {}, 'References'), swRefs), refsBody);

  // the Design Vision & Inspiration page: one or two pictures, each can be moved around and zoomed inside its frame
  const MOOD_BOXES = { 1: [[0, 203, 488, 721]], 2: [[0, 203, 488, 721], [518, 203, 486, 721]] };   // px of the 1308 x 924 page
  const moodView = [[0.5, 0.5, 1], [0.5, 0.5, 1]], moodLast = [null, null], moodUrls = new WeakMap();
  const mood1 = fileField({ label: '', pdf: false, hideDrop: true, onChange: () => drawMood() });
  const mood2 = fileField({ label: '', pdf: false, hideDrop: true, onChange: () => drawMood() });
  const moodBox = h('div', { class: 'mp edit', style: 'aspect-ratio:1308/924' });
  const moodZoom = h('div', {});
  const moodPrev = h('div', { class: 'selgroup', style: 'display:none' }, h('div', { class: 'mp-wrap' }, moodBox, moodZoom));
  function drawMood() {
    const slots = [mood1, mood2], present = [0, 1].filter(i => slots[i].files[0]);
    [0, 1].forEach(i => { const f = slots[i].files[0] || null; if (f !== moodLast[i]) { moodLast[i] = f; moodView[i] = [0.5, 0.5, 1]; } });
    moodPrev.style.display = '';   // the page is always shown, with empty frames where the pictures will go
    const bits = [h('div', { class: 'mp-title' }, 'Design Vision & Inspiration')], rows = [];
    present.forEach((slot, k) => {
      const f = slots[slot].files[0];
      if (!moodUrls.has(f)) moodUrls.set(f, URL.createObjectURL(f));
      const [x, y, w, hh] = MOOD_BOXES[present.length][k];
      const img = h('img', { src: moodUrls.get(f), alt: '', draggable: 'false' });
      const d = h('div', { class: 'mp-flat pz' }, img);
      d.style.left = (x / 13.08) + '%'; d.style.top = (y / 9.24) + '%'; d.style.width = (w / 13.08) + '%'; d.style.height = (hh / 9.24) + '%';
      const ctl = attachPanZoom(d, img, w / hh, moodView[slot], () => { slider.value = String(moodView[slot][2]); });
      const slider = h('input', { type: 'range', min: '1', max: '4', step: '0.05', value: String(moodView[slot][2]), 'aria-label': `Zoom image ${slot + 1}`, oninput: () => ctl.setZoom(parseFloat(slider.value)) });
      bits.push(d); rows.push(h('div', { class: 'mp-order' }, h('span', { class: 'hint' }, `Image ${slot + 1} zoom`), ' ', slider));
    });
    if (!present.length) MOOD_BOXES[2].forEach(([x, y, w, hh], k) => {
      const slotF = [mood1, mood2][k];
      const ph = h('div', { class: 'mp-flat mood-empty', role: 'button', tabindex: '0', title: 'Choose a picture, or drop one here' }, h('span', {}, h('b', {}, `Image ${k + 1}`), h('small', {}, 'Choose file or drop here')));
      ph.addEventListener('click', () => slotF.pick());
      ph.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); slotF.pick(); } });
      ph.addEventListener('dragover', e => { e.preventDefault(); ph.classList.add('over'); });
      ph.addEventListener('dragleave', () => ph.classList.remove('over'));
      ph.addEventListener('drop', e => { e.preventDefault(); ph.classList.remove('over'); const f = [...e.dataTransfer.files].find(x => x.type.startsWith('image/')); if (f) slotF.put(f); });
      ph.style.left = (x / 13.08) + '%'; ph.style.top = (y / 9.24) + '%'; ph.style.width = (w / 13.08) + '%'; ph.style.height = (hh / 9.24) + '%';
      bits.push(ph);
    });
    moodBox.replaceChildren(...bits); moodZoom.replaceChildren(...rows, present.length ? h('div', { class: 'mp-note hint' }, 'Drag a picture to move it inside its frame, and use the slider to zoom in (or hold Ctrl/⌘ and scroll).') : h('div', { class: 'mp-note hint' }, 'Click a frame to choose a picture, or drop one onto it.'),
      ...(present.length === 1 ? [h('button', { type: 'button', class: 'btn ghost', style: 'margin-top:8px', onclick: () => [mood1, mood2][present[0] === 0 ? 1 : 0].pick() }, '+ Add a second picture')] : []));
  }
  const swMood = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include Design Vision & Inspiration page' });
  const moodBody = h('div', { style: 'display:none;margin-top:12px' }, moodPrev, h('div', { class: 'grid mood-files', style: 'margin-top:8px' }, h('div', {}, mood1.el), h('div', {}, mood2.el)));
  drawMood();
  swMood.addEventListener('change', () => { moodBody.style.display = swMood.checked ? '' : 'none'; });
  moodCard = h('div', { class: 'subsection' }, titleRow(h('h3', { class: 'sub-title' }, 'Design Vision & Inspiration'), swMood), moodBody);

  const mats = materialsPicker(() => cLogo.files[0], () => ({ files: (form.shows || []).flatMap(s => s.renders.files), client: cName.value }));
  const swMat = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Include Materials & Furnishings page' });
  const matBody = h('div', { style: 'display:none;margin-top:12px' }, mats.el);
  swMat.addEventListener('change', () => { matBody.style.display = swMat.checked ? '' : 'none'; if (swMat.checked) mats.fromLogo(true); });
  if (feat('materials')) matCard = h('div', { class: 'subsection' }, titleRow(h('h3', { class: 'sub-title' }, 'Materials & Furnishings'), swMat), matBody);

  // submit
  const status = h('div');
  const go = h('button', { type: 'button', class: 'btn big' }, editId ? 'Save changes' : 'Create proposal');
  go.addEventListener('click', submit);
  function fail(m) { status.replaceChildren(h('div', { class: 'msg error', role: 'alert' }, m)); status.scrollIntoView({ block: 'center', behavior: 'smooth' }); }

  function submit() {
    if (!cName.value) return fail('Please enter the client name.');
    if (!cLogo.files.length) return fail('Please upload the client\u2019s logo.');
    for (const [i, s] of form.shows.entries()) {
      const w = form.shows.length > 1 ? `Show ${i + 1}: ` : '';
      if (!s.name.value || !s.location.value || !s.dates.value) return fail(`${w}please fill in the show name, venue and dates.`);
      if (!s.renders.files.length) return fail(`${w}please upload at least one render or drawing.`);
    }
    const payload = {
      currency: cur.value,
      client: { name: cName.value, website: cSite.value },
      shows: form.shows.map(s => ({ name: s.name.value, location: s.location.value, dates: s.dates.value, stand_size: s.stand.value, booth_box: s.fOn.checked ? s.booth.value() : null, render_notes: s.renders.notes, floorplan_view: s.fOn.checked ? s.booth.view() : null,
        quote: !s.qOn.checked ? {} : { custom: s.custom.checked, lines: Object.fromEntries(Object.entries(s.q).map(([k, i]) => [k, i.value])),
          custom_lines: s.cRows.map(r => ({ label: r.name.value, description: r.desc.value, amount: r.amount.value })),
          details: Object.fromEntries(Object.entries(s.qd).map(([k, t]) => [k, t.value])),
          discount: s.discount.value, options: s.oRows.map(r => ({ text: r.text.value, sign: r.sign.value, amount: r.amount.value })) } })),
      materials: feat('materials') && swMat.checked ? mats.value() : {},
      pages: { critical_dates: swCrit.checked, payment_terms: swPay.checked },
      about: swAbout.checked ? about.value() : [],
      references: swRefs.checked ? refs.value() : [],
      critical_dates: Object.fromEntries(Object.entries(dateInputs).map(([k, t]) => [k, t.value])),
    };
    const moodOn = swMood.checked;
    payload.mood_view = moodOn ? [0, 1].filter(i => [mood1, mood2][i].files[0]).map(i => moodView[i]) : [];
    const fd = new FormData();
    fd.append('data', JSON.stringify(payload));
    fd.append('client_logo', cLogo.files[0]);
    if (moodOn && mood1.files[0]) fd.append('mood_1', mood1.files[0]);
    if (moodOn && mood2.files[0]) fd.append('mood_2', mood2.files[0]);
    form.shows.forEach((s, i) => {
      if (s.fOn.checked && s.floorplan.files[0]) fd.append(`show${i}_floorplan`, s.floorplan.files[0]);
      s.renders.files.forEach(f => fd.append(`show${i}_renders`, f));
    });
    send(fd);
  }

  function send(fd) {
    go.disabled = true;
    const label = h('span', {}, 'Uploading…');
    const fill = h('i');
    status.replaceChildren(h('div', { class: 'msg info' }, label, h('div', { class: 'bar' }, fill)));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', editId ? `/api/proposals/${editId}` : '/api/proposals');
    xhr.setRequestHeader('X-Requested-With', 'proposal-app');
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const p = Math.round((e.loaded / e.total) * 100);
      fill.style.width = p + '%';
      label.textContent = p < 100 ? `Uploading… ${p}%` : 'Building your proposal… this can take a minute for large uploads.';
    };
    xhr.onerror = () => { go.disabled = false; fail('The upload failed. Please check your connection and try again.'); };
    xhr.onload = () => {
      go.disabled = false;
      let body = null; try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status === 401) return showLogin('Your session has expired. Please sign in again.');
      if (xhr.status !== 200 || !body) return fail((body && body.error) || `Something went wrong (${xhr.status}).`);
      const panel = pageOrderPanel(body.id);
      const pptx = h('button', { class: 'btn', onclick: e => downloadOrdered(panel, body.id, 'pptx', e.target) }, 'Download PowerPoint');
      const pdf = h('button', { class: user.is_admin ? 'btn ghost' : 'btn', onclick: e => downloadOrdered(panel, body.id, 'pdf', e.target) }, 'Download PDF');
      status.replaceChildren(h('div', { class: 'card done' }, h('h2', {}, editId ? 'Your changes are saved' : 'Your proposal is ready'), h('div', {}, body.title), panel.el,
        h('div', { class: 'actions' }, ...(user.is_admin ? [pptx] : []), pdf, h('button', { class: 'btn ghost', onclick: () => { editing = body.id; render(); } }, 'Edit again'),
          h('button', { class: 'btn ghost', onclick: () => { tab = editId ? 'list' : 'new'; render(); } }, editId ? 'Back to proposals' : 'Start another'))));
      status.scrollIntoView({ block: 'center', behavior: 'smooth' });
    };
    xhr.send(fd);
  }

  addShow();
  const note = h('span', { class: 'hint' }, 'Loading it…');
  const banner = editId ? h('div', { class: 'card' }, h('strong', {}, 'Editing a saved proposal'), ' ', note, ' ',
    h('button', { type: 'button', class: 'btn link', onclick: () => { tab = 'list'; render(); } }, 'Cancel')) : '';
  if (editId) { go.disabled = true; loadExisting().finally(() => { go.disabled = false; }); }
  async function fetchFile(a) {
    const r = await fetch(a.url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error('A picture from this proposal could not be loaded.');
    const blob = await r.blob();
    return new File([blob], a.name, { type: blob.type || (/\.png$/i.test(a.name) ? 'image/png' : 'image/jpeg') });
  }
  async function loadExisting() {
    try {
      const d = await api(`/api/proposals/${editId}/edit`);
      cName.input.value = d.client.name; cSite.input.value = d.client.website || ''; cur.value = d.currency;
      if (d.client.logo) cLogo.put(await fetchFile(d.client.logo));
      for (let i = 1; i < d.shows.length; i++) addShow();
      for (const [i, sd] of d.shows.entries()) {
        const s = form.shows[i];
        s.name.input.value = sd.name; s.location.input.value = sd.location; s.dates.set(sd.dates); s.stand.set(sd.stand_size);
        if (sd.floorplan) { s.floorplan.put(await fetchFile(sd.floorplan)); s.fOn.checked = true; s.fOn.dispatchEvent(new Event('change')); }
        if (sd.booth_box || sd.floorplan_view) setTimeout(() => { if (sd.floorplan_view) s.booth.setView(sd.floorplan_view); if (sd.booth_box) s.booth.set(sd.booth_box); }, 400);
        for (const [ri, a] of sd.renders.entries()) await s.renders.put(await fetchFile(a), (sd.render_notes || [])[ri]);
        const q = sd.quote;
        if (q) {
          s.qOn.checked = true; s.qOn.dispatchEvent(new Event('change'));
          if (q.custom) {
            s.cSeeded = true; s.custom.checked = true; s.custom.dispatchEvent(new Event('change'));
            q.custom_lines.forEach(r => { s.addCustomRow(r.label, r.amount); s.cRows[s.cRows.length - 1].desc.value = r.description || ''; });
          } else Object.entries(q.lines).forEach(([k, v]) => { const kk = k === 'av_furniture' ? 'audio_visual' : k; if (s.q[kk]) setMoney(s.q[kk], v); });   // older proposals had one AV & Furniture line
          Object.entries(q.details || {}).forEach(([k, v]) => { if (s.qd[k]) s.qd[k].value = v; });
          setMoney(s.discount, q.discount);
          (q.options || []).forEach(o => s.addOption(o.text, o.sign, o.amount));
        }
        s.recalc();
      }
      if (d.mood[0]) mood1.put(await fetchFile(d.mood[0]));
      if (d.mood[1]) mood2.put(await fetchFile(d.mood[1]));
      (d.mood_view || []).forEach((v, k) => { const slot = d.mood.length === 1 ? 0 : k; if (v && slot < 2) moodView[slot] = [...v]; });
      drawMood();
      if (feat('materials')) { mats.set(d.materials); const m = d.materials || {}; swMat.checked = ['flooring', 'finishings', 'furniture', 'lighting', 'additional', 'lifestyle', 'colours'].some(k => (m[k] || []).length); swMat.dispatchEvent(new Event('change')); }
      swMood.checked = !!(d.mood && d.mood.length); swMood.dispatchEvent(new Event('change'));
      swCrit.checked = !!d.pages.critical_dates; swCrit.dispatchEvent(new Event('change'));
      swPay.checked = !!d.pages.payment_terms;
      about.set(d.about); swAbout.checked = !!(d.about && d.about.length); swAbout.dispatchEvent(new Event('change'));
      refs.set(d.references || []); swRefs.checked = !!(d.references && d.references.length); swRefs.dispatchEvent(new Event('change'));
      Object.entries(d.critical_dates || {}).forEach(([k, v]) => { if (dateInputs[k]) dateInputs[k].set(v); });
      renumber();
      note.textContent = d.title + '. Change anything you like, then press Save changes at the bottom.';
    } catch (e) { note.textContent = ''; note.replaceChildren(h('span', { class: 'msg error' }, e.message)); }
  }
  shell(h('div', {}, ...(banner ? [banner] : []), clientCard, detailsFlow, ...(feat('multi_show') ? [addShowRow] : []), h('section', { class: 'card moodboard' }, h('h2', {}, 'Moodboard'), moodCard, ...(matCard ? [matCard] : [])), showsFlow, critCard, termsCard, aboutCard, refCard, status, h('p', {}, go)));
}

// ---------------------------------------------------------------- boot
async function boot() {
  try { user = await api('/api/me'); tab = 'new'; render(); }
  catch (e) { showLogin(); }
}
boot();
