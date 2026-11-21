// Deal Radar front end: no build step, no dependencies.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const PAGE = 60;
const state = { v: 'car', source: '', kind: '', tiers: { hot: 1e9, good: 1e9 }, offset: 0, total: 0, items: [], loading: false, defaults: null, prefs: null, seq: 0 };

// ------------------------------------------------------------------ formatting
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
const num = (n) => nf.format(n).replace(/,/g, ' ');
const eur = (n) => `${num(n)} €`;
const pct = (x, d = 0) => `${(x * 100).toFixed(d)}%`;
const titleCase = (s) => String(s || '').replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
const prettyRegion = (s) => titleCase(String(s || '').replace(/-and-reg$/, ' & region'));
const dmy = (iso) => { if (!iso) return ''; const d = new Date(iso); return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}.${d.getFullYear()}`; };
const ago = (days) => days < 1 / 24 ? 'just now' : days < 1 ? `${Math.round(days * 24)} h ago` : `${Math.round(days)} d ago`;

const FLAG = {
  damaged: ['Damage wording', 'bad'], for_parts: ['For parts', 'bad'], not_running: ['Not running', 'bad'],
  needs_repair: ['Needs repair', 'warn'], no_papers: ['No papers', 'bad'], scam_wording: ['Scam wording', 'bad'],
  too_good: ['Too good to be true', 'warn'], dilapidated: ['Dilapidated', 'bad'], encumbered: ['Encumbered', 'bad'],
  needs_renovation: ['Needs renovation', 'warn'], share_only: ['Fractional share', 'bad'], missing_basics: ['Missing basics', 'warn'], unfinished: ['Unfinished', 'warn'], service_history: ['Service history', 'good'], first_owner: ['1st owner', 'good'],
  fresh_ta: ['Fresh TA', 'good'], renovated: ['Renovated', 'good'], ready_to_move: ['Move-in ready', 'good'], no_accident_claimed: ['No accidents', 'good'],
};
const FACTOR = { fuel: 'Fuel type', mileage: 'Mileage wear', corrosion: 'Rust risk', inspection: 'Inspection (TA)', wording: 'Ad wording', freshness: 'Freshness', price_drop: 'Price drops' };
const FUEL = { petrol: 'Petrol', diesel: 'Diesel', hybrid: 'Hybrid', electric: 'Electric', gas: 'LPG' };

// ------------------------------------------------------------------------- API
async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== '' && v != null && v !== false) url.searchParams.set(k, v);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ----------------------------------------------------------------- preferences
const PREF_KEY = 'dealradar.prefs.v1';
const SCHEMA = [
  { title: 'Fuel type trust', v: 'car', rows: ['diesel', 'petrol', 'gas', 'hybrid', 'electric'].map((f) => ({ path: `car.fuel.${f}`, label: FUEL[f], min: 0, max: 1, step: 0.05, fmt: (x) => x.toFixed(2) })) },
  { title: 'Mileage wear', v: 'car', rows: [
    { path: 'car.mileage.diesel_threshold', label: 'Diesel wear starts at', min: 0, max: 300000, step: 10000, fmt: (x) => `${num(x)} km` },
    { path: 'car.mileage.other_threshold', label: 'Other fuels start at', min: 0, max: 300000, step: 10000, fmt: (x) => `${num(x)} km` },
    { path: 'car.mileage.rate', label: 'Penalty per 100 000 km', min: 0, max: 0.3, step: 0.01, fmt: (x) => x.toFixed(2) },
    { path: 'car.mileage.importance', label: 'Importance', min: 0, max: 1, step: 0.05, fmt: (x) => pct(x) }] },
  { title: 'Rust-prone models', v: 'car', rows: [{ path: 'car.corrosion_importance', label: 'Importance (Mazda < 2014, Passat B6)', min: 0, max: 1, step: 0.05, fmt: (x) => pct(x) }] },
  { title: 'Inspection (TA), cars over 10 years', v: 'car', rows: [
    { path: 'car.ta.importance', label: 'Importance', min: 0, max: 1, step: 0.05, fmt: (x) => pct(x) },
    { path: 'car.ta.unknown_default', label: 'If the ad does not say', min: 0, max: 1, step: 0.05, fmt: (x) => x.toFixed(2) }] },
  { title: 'Signals', v: 'all', rows: [
    { path: 'common.text_importance', label: 'Ad wording (damage, service history)', min: 0, max: 1, step: 0.05, fmt: (x) => pct(x) },
    { path: 'common.freshness', label: 'Freshness bonus', min: 0, max: 1, step: 0.05, fmt: (x) => pct(x) },
    { path: 'common.price_drop', label: 'Price-drop bonus', min: 0, max: 1, step: 0.05, fmt: (x) => pct(x) }] },
];
const getPath = (o, p) => p.split('.').reduce((a, k) => a?.[k], o);
const setPath = (o, p, v) => { const ks = p.split('.'); const last = ks.pop(); ks.reduce((a, k) => a[k], o)[last] = v; };

function loadPrefs() {
  const base = structuredClone(state.defaults);
  try {
    const saved = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    for (const s of SCHEMA) for (const r of s.rows) { const v = getPath(saved, r.path); if (typeof v === 'number') setPath(base, r.path, v); }
  } catch { /* private mode: defaults are fine */ }
  return base;
}
function savePrefs() { try { localStorage.setItem(PREF_KEY, JSON.stringify(state.prefs)); } catch { /* ignore */ } }

function buildWeights() {
  const body = $('#weightsBody');
  body.innerHTML = SCHEMA.map((s) => `
    <div class="wgroup" data-v="${s.v}"><h3>${esc(s.title)}</h3>
    ${s.rows.map((r) => `<div class="wrow"><label for="w-${r.path}"><span>${esc(r.label)}</span><output id="o-${r.path}"></output></label>
      <input type="range" id="w-${r.path}" data-path="${r.path}" min="${r.min}" max="${r.max}" step="${r.step}"></div>`).join('')}</div>`).join('');
  for (const s of SCHEMA) for (const r of s.rows) {
    const input = $(`[id="w-${r.path}"]`); const out = $(`[id="o-${r.path}"]`);
    const sync = () => { out.textContent = r.fmt(Number(input.value)); };
    input.value = getPath(state.prefs, r.path); sync();
    input.addEventListener('input', () => { setPath(state.prefs, r.path, Number(input.value)); sync(); savePrefs(); debounced(); });
  }
  $('#resetWeights').onclick = () => {
    state.prefs = structuredClone(state.defaults); savePrefs(); buildWeights(); reload();
  };
  applyVertical();
}

// --------------------------------------------------------------------- filters
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const debounced = debounce(() => reload(), 280);

function params() {
  const g = (id) => $(`#${id}`)?.value ?? '';
  const p = {
    vertical: state.v, prefs: JSON.stringify(state.prefs), source: state.source, region: g('region'), q: g('q').trim(),
    price_min: g('price_min'), price_max: g('price_max'), min_conf: g('min_conf'), sort: g('sort'),
    include_too_good: $('#include_too_good').checked, include_flagged: $('#include_flagged').checked,
  };
  if (state.v === 'car') {
    Object.assign(p, { year_min: g('year_min'), year_max: g('year_max'), km_max: g('km_max'), gear: g('gear'),
      fuel: $$('#fuel input:checked').map((c) => c.value).join(',') });
  } else {
    Object.assign(p, { area_min: g('area_min'), area_max: g('area_max'), rooms_min: g('rooms_min'), rooms_max: g('rooms_max'),
      kind: state.v === 'house' ? state.kind : '', yield_min: state.v === 'flat' ? g('yield_min') : '' });
  }
  return p;
}

function applyVertical() {
  document.body.dataset.v = state.v;
  $$('.wgroup').forEach((g) => { g.hidden = g.dataset.v === 'car' && state.v !== 'car'; });
  $$('#tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.v === state.v)));
  $('#regionLabel').textContent = state.v === 'car' ? 'Seller region' : 'City / region';
  const ys = $('#sortYield'); ys.hidden = ys.disabled = state.v !== 'flat';
  if (state.v !== 'flat' && $('#sort').value === 'yield') $('#sort').value = 'score';
}

async function loadRegions() {
  try {
    const regions = await api('/api/regions', { vertical: state.v });
    const sel = $('#region'); const keep = sel.value;
    sel.innerHTML = '<option value="">All</option>' + regions.map((r) => `<option value="${esc(r.region)}">${esc(prettyRegion(r.region))} (${num(r.n)})</option>`).join('');
    sel.value = [...sel.options].some((o) => o.value === keep) ? keep : '';
  } catch { /* regions are optional */ }
}

// -------------------------------------------------------------------- results
function titleOf(d) {
  const a = d.attrs || {};
  if (d.vertical === 'car' || a.make) {
    const make = titleCase(a.make); const model = a.model_label || titleCase(a.model);
    return `${make} ${model}${a.year ? ' · ' + a.year : ''}`;
  }
  return '';
}
function estateTitle(d) {
  const a = d.attrs || {};
  const size = a.area_m2 ? num(a.area_m2) + ' m²' : 'area n/a';
  if (state.v === 'house' && a.kind === 'cottage') return `Summer cottage · ${size}`;
  const kind = state.v === 'house' ? 'house' : 'flat';
  return `${a.rooms ? a.rooms + '-room ' : ''}${kind} · ${size}`;
}
function factsOf(d) {
  const a = d.attrs || {}; const out = [];
  if (state.v === 'car') {
    if (a.mileage_km != null) out.push(`${num(a.mileage_km)} km`);
    out.push(`${FUEL[a.fuel] || 'Petrol'}${a.engine_l ? ' ' + Number(a.engine_l).toFixed(1) : ''}`);
    if (a.gearbox) out.push(a.gearbox === 'auto' ? 'Auto' : 'Manual');
    if (a.ta_date === 'none') out.push('No TA'); else if (a.ta_date) out.push(`TA ${a.ta_date.slice(5)}.${a.ta_date.slice(0, 4)}`);
  } else {
    if (d.district) out.push(esc(d.district));
    if (a.floor != null) out.push(`floor ${a.floor}${a.floors ? '/' + a.floors : ''}`);
    if (a.series) out.push(esc(a.series));
    if (a.plot_m2) out.push(`plot ${num(a.plot_m2)} m²`);
    if (a.area_m2) out.push(`${num(d.price / a.area_m2)} €/m²`);
  }
  return out;
}
function tagsOf(d) {
  const tags = [`<span class="tag src">${d.source === 'ss' ? 'ss.lv' : 'pp.lv'}</span>`];
  if (d.age_days != null && d.age_days < 1.5) tags.unshift(`<span class="tag new">NEW · ${ago(d.age_days)}</span>`);
  else if (d.age_days != null && d.age_days < 10) tags.push(`<span class="tag">${ago(d.age_days)}</span>`);
  if (d.drop_pct > 0) tags.push(`<span class="tag good">↓ ${d.drop_pct.toFixed(0)}% cut</span>`);
  if (d.extra?.yield) tags.push(`<span class="tag ${d.extra.yield >= 0.07 ? 'good' : ''}">${(d.extra.yield * 100).toFixed(1)}% yield</span>`);
  for (const f of d.flags || []) { const [label, kind] = FLAG[f] || [f, '']; tags.push(`<span class="tag ${kind}">${esc(label)}</span>`); }
  return tags.join('');
}
function meterHtml(d, big = false) {
  const x = Math.log(d.price / d.fair); const span = 0.6;
  const pos = Math.max(2, Math.min(98, 50 + (x / span) * 50));
  const w = Math.min(48, (d.sigma ?? 0.12) / span * 50);
  const over = d.price > d.fair ? ' over' : '';
  return `<div class="${big ? 'bigmeter' : 'meter'}" role="img" aria-label="Asking ${eur(d.price)} against fair value ${eur(d.fair)}">
    <span class="band" style="left:${50 - w}%;width:${2 * w}%"></span><span class="mid"></span>
    ${big ? `<span class="lbl" style="left:50%">fair ${eur(d.fair)}</span>` : ''}
    <span class="pt${over}" style="left:${pos}%"></span></div>`;
}
const tier = (s) => (s >= state.tiers.hot ? 'tier-hot' : s >= state.tiers.good ? 'tier-good' : '');

function itemHtml(d) {
  const sub = esc((d.title || '').slice(0, 110));
  const head = state.v === 'car' ? titleOf(d) : estateTitle(d);
  const below = d.discount >= 0;
  return `<li><button class="item" data-id="${d.id}" type="button">
    <div class="badge ${tier(d.score)}"><strong>${d.score.toFixed(d.score >= 10 ? 0 : 1)}</strong><small>score</small></div>
    <div class="main"><h3>${esc(head)}</h3><div class="sub">${sub}</div>
      <div class="facts">${factsOf(d).map((f) => `<span>${f}</span>`).join('')}</div><div class="tags">${tagsOf(d)}</div></div>
    <div class="price"><div class="ask">${eur(d.price)}</div>
      <div class="fair">fair <b>${eur(d.fair)}</b></div>
      <span class="disc${below ? '' : ' over'}">${below ? '−' : '+'}${pct(Math.abs(d.discount))}</span>${meterHtml(d)}</div>
  </button></li>`;
}

async function reload() {
  state.offset = 0; state.items = [];
  const seq = ++state.seq;
  $('#list').innerHTML = '<li class="skeleton"></li><li class="skeleton"></li><li class="skeleton"></li>';
  $('#empty').hidden = true; $('#more').hidden = true;
  await fetchPage(seq);
}
async function fetchPage(seq) {
  state.loading = true;
  try {
    const res = await api('/api/deals', { ...params(), limit: PAGE, offset: state.offset });
    if (seq !== state.seq) return;
    state.total = res.total; state.tiers = res.tiers; state.items.push(...res.items); state.offset += res.items.length;
    $('#list').innerHTML = state.items.map(itemHtml).join('');
    $('#summary').innerHTML = `<b>${num(res.total)}</b> deals match · ${num(res.scored)} scored`;
    $('#more').hidden = state.items.length >= res.total;
    $('#empty').hidden = res.total > 0;
    if (!res.total) emptyState(res.scored);
  } catch (err) {
    $('#list').innerHTML = '';
    $('#empty').hidden = false;
    $('#empty').innerHTML = `<strong>Could not load deals</strong>${esc(err.message)}`;
  } finally { state.loading = false; }
}
function emptyState(scored) {
  const busy = Object.keys(state.status?.scheduler?.running || {}).length > 0;
  $('#empty').innerHTML = scored === 0 && busy
    ? '<strong>First crawl is running</strong>Results appear as data arrives. The first full pass takes roughly 30–60 minutes.'
    : scored === 0 ? '<strong>No data yet</strong>The crawler has not stored any listings for this category.'
    : '<strong>Nothing matches</strong>Loosen the filters, or include ads with damage wording / too-good prices.';
}

// --------------------------------------------------------------------- drawer
function drawerOpen(open) {
  const d = $('#drawer'); d.classList.toggle('open', open); d.setAttribute('aria-hidden', String(!open));
  document.body.style.overflow = open ? 'hidden' : '';
  if (open) $('#closeDrawer').focus();
}
function sparkline(history) {
  if (!history || history.length < 2) return '';
  const xs = history.map((h) => new Date(h.ts).getTime()); const ys = history.map((h) => h.price);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const px = (x) => 6 + ((x - x0) / Math.max(1, x1 - x0)) * 288, py = (y) => 54 - ((y - y0) / Math.max(1, y1 - y0)) * 44;
  const pts = history.map((h) => `${px(new Date(h.ts).getTime()).toFixed(1)},${py(h.price).toFixed(1)}`).join(' ');
  const last = history[history.length - 1];
  return `<div class="block"><h4>Price history</h4>
    <svg class="spark" viewBox="0 0 300 64" preserveAspectRatio="none" role="img" aria-label="Price history">
      <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      <circle cx="${px(new Date(last.ts).getTime())}" cy="${py(last.price)}" r="3.5" fill="var(--accent)"/></svg>
    <p class="note">${history.map((h) => `${dmy(h.ts)}: ${eur(h.price)}`).join(' → ')}</p></div>`;
}
function whyHtml(d) {
  const rows = d.factors.map(([k, v]) => `<tr><td>${esc(FACTOR[k] || k)}</td><td class="${v < 0.995 ? 'neg' : v > 1.005 ? 'pos' : ''}">× ${v.toFixed(2)}</td></tr>`).join('');
  const reasons = { model: `a regression on <b>${d.group_n}</b> comparable ${esc(titleCase(d.group_key.split('/').join(' ')))} listings`,
    make: `a regression across <b>${d.group_n}</b> ${esc(titleCase(d.group_key.split('/')[0]))} listings, with this model's effect shrunk toward the make`,
    house: `a regression on <b>${d.group_n}</b> houses (size, plot, region, district)` }[d.method]
    || `a regression on <b>${d.group_n}</b> ${esc(d.method.replace('flat-', ''))} flats (area, rooms, floor, building series, district)`;
  const agree = d.knn_fair ? Math.abs(Math.log(d.fair / d.knn_fair)) : null;
  return `<div class="block"><h4>Why this score</h4>
    <table class="why">
      <tr><td>Below fair value</td><td>${pct(d.discount, 1)}</td></tr>
      <tr><td>Confidence in that estimate</td><td>× ${d.conf.toFixed(2)}</td></tr>
      <tr><td>Market score</td><td>${d.base.toFixed(1)}</td></tr>${rows}
      <tr class="total"><td>Final deal score</td><td>${d.score.toFixed(1)}</td></tr></table>
    <p class="note">The market score counts a discount in full up to about 30% below fair value, then <em>less</em>: a gap much bigger than that is more often an unfinished flat, a share of a property or a typo than a bargain.</p>
    <p class="note">Fair value comes from ${reasons}, blended with the median of its nearest comparables${d.knn_fair ? ` (${eur(d.knn_fair)}${agree > 0.15 ? ', which disagrees noticeably, so confidence is lowered' : ', in agreement'})` : ''}. Typical scatter is ±${pct(d.sigma)}.</p></div>`;
}
async function openListing(id) {
  const body = $('#sheetBody'); body.innerHTML = '<div class="skeleton" style="height:240px"></div>'; drawerOpen(true);
  try {
    const d = await api(`/api/listing/${id}`, { prefs: JSON.stringify(state.prefs) });
    const a = d.attrs || {}; const head = d.attrs?.make ? titleOf(d) : estateTitle({ ...d, vertical: state.v });
    const comps = (d.comps || []).map((c) => {
      const ca = c.attrs || {};
      const meta = ca.make ? `${ca.year || ''} · ${ca.mileage_km != null ? num(ca.mileage_km) + ' km' : ''} · ${FUEL[ca.fuel] || ''}` : `${ca.area_m2 ? num(ca.area_m2) + ' m²' : ''}${ca.rooms ? ' · ' + ca.rooms + ' rooms' : ''}`;
      return `<li><span><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc((c.title || '').slice(0, 70))}</a><br><span class="sub">${c.source === 'ss' ? 'ss.lv' : 'pp.lv'} · ${esc(meta)}</span></span><b>${eur(c.price)}</b></li>`;
    }).join('');
    body.innerHTML = `
      <h2>${esc(head)}</h2>
      <p class="lead">${esc(d.title || '')}</p>
      <a class="open-link" href="${esc(d.url)}" target="_blank" rel="noopener">Open on ${d.source === 'ss' ? 'ss.lv' : 'pp.lv'} ↗</a>
      <div class="tiles">
        <div class="tile"><span>Asking</span><strong>${eur(d.price)}</strong></div>
        <div class="tile"><span>Fair value</span><strong>${eur(d.fair)}</strong></div>
        <div class="tile ${d.discount >= 0 ? 'good' : ''}"><span>${d.discount >= 0 ? 'Below fair' : 'Above fair'}</span><strong>${pct(Math.abs(d.discount), 1)}</strong></div>
      </div>
      <div class="block">${meterHtml(d, true)}<div class="legend"><span>cheaper</span><span>shaded: typical scatter ±${pct(d.sigma)}</span><span>dearer</span></div>
        <div class="tags" style="margin-top:12px">${tagsOf(d)}</div></div>
      ${whyHtml(d)}
      ${d.extra?.rent ? `<div class="block"><h4>Rental yield</h4>
        <table class="why"><tr><td>Estimated rent</td><td>${eur(d.extra.rent)} / month</td></tr>
        <tr><td>Gross yield on asking price</td><td>${(d.extra.yield * 100).toFixed(1)}%</td></tr></table>
        <p class="note">Median rent per m² of <b>${d.extra.rent_n}</b> comparable rentals (${({ dist_band: 'same district and room count', dist: 'same district', reg_band: 'same city and room count', reg: 'same city' })[d.extra.rent_level]}), times this flat's area. Gross: before taxes, upkeep and vacancy.</p></div>` : ''}
      ${sparkline(d.history)}
      ${comps ? `<div class="block"><h4>Nearest comparables</h4><ul class="comps">${comps}</ul></div>` : ''}
      ${d.description ? `<div class="block"><h4>Seller's text</h4><p class="desc">${esc(d.description)}</p></div>` : ''}
      <p class="note">First seen ${dmy(d.first_seen)}${d.posted_at ? ' · posted ' + dmy(d.posted_at) : ''}${d.views != null ? ' · ' + num(d.views) + ' views' : ''}${d.seller ? ' · ' + (d.seller === 'company' ? 'business seller' : 'private seller') : ''}.</p>`;
  } catch (err) { body.innerHTML = `<p class="note">Could not load this listing: ${esc(err.message)}</p>`; }
}

// --------------------------------------------------------------------- status
async function refreshStatus() {
  try {
    const s = await api('/api/status'); state.status = s;
    const counts = {}; let total = 0;
    for (const c of s.counts) { counts[c.vertical] = (counts[c.vertical] || 0) + c.scored; total += c.n; }
    for (const v of ['car', 'flat', 'house']) $(`#count-${v}`).textContent = counts[v] ? num(counts[v]) : '';
    const running = Object.values(s.scheduler.running);
    const dot = $('#statusDot'); dot.className = 'dot ' + (running.length ? 'busy' : total ? 'live' : '');
    const when = s.scored_at ? ago((new Date(s.now) - new Date(s.scored_at)) / 864e5) : null;
    $('#statusText').textContent = running.length
      ? `Crawling ${running.map((j) => j.replace('_', ' ')).join(' + ')} · ${num(total)} tracked`
      : total ? `${num(total)} listings tracked · scored ${when}` : 'Waiting for the first crawl';
    if (!state.defaults) { state.defaults = s.defaults; state.prefs = loadPrefs(); buildWeights(); reload(); loadRegions(); }
  } catch { $('#statusText').textContent = 'Server unreachable'; $('#statusDot').className = 'dot'; }
}

// ------------------------------------------------------------------------ boot
function wire() {
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b || b.dataset.v === state.v) return;
    state.v = b.dataset.v; history.replaceState(null, '', `#${state.v}`);
    $('#region').value = ''; applyVertical(); loadRegions(); reload();
  });
  $('#source').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    state.source = b.dataset.val; $$('#source button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); reload();
  });
  $('#kind').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    state.kind = b.dataset.val; $$('#kind button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); reload();
  });
  for (const id of ['q', 'price_min', 'price_max', 'year_min', 'year_max', 'km_max', 'area_min', 'area_max', 'rooms_min', 'rooms_max']) $(`#${id}`).addEventListener('input', debounced);
  for (const id of ['region', 'gear', 'sort', 'include_too_good', 'include_flagged']) $(`#${id}`).addEventListener('change', reload);
  $('#fuel').addEventListener('change', reload);
  $('#min_conf').addEventListener('input', () => { $('#confVal').textContent = pct(Number($('#min_conf').value)); debounced(); });
  $('#moreBtn').addEventListener('click', () => { if (!state.loading) fetchPage(state.seq); });
  $('#list').addEventListener('click', (e) => { const b = e.target.closest('.item'); if (b) openListing(b.dataset.id); });
  $('#scrim').addEventListener('click', () => drawerOpen(false));
  $('#closeDrawer').addEventListener('click', () => drawerOpen(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') drawerOpen(false); });
  $('#filtersToggle').addEventListener('click', () => {
    const f = $('#filters'); const open = f.classList.toggle('open'); $('#filtersToggle').setAttribute('aria-expanded', String(open));
  });
}

const hash = location.hash.slice(1);
if (['car', 'flat', 'house'].includes(hash)) state.v = hash;
wire();
refreshStatus();
setInterval(refreshStatus, 30000);
