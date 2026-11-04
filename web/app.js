/* Etsy research board — static SPA talking straight to Supabase's PostgREST.
 *
 * No bundler, no CDN, no build step: GitHub Pages serves these three files and
 * the browser speaks to the database directly. Reads use the anon key, which
 * RLS confines to SELECT; writes go through four token-gated RPCs, so the key
 * that can mutate data never reaches the browser.
 */
'use strict';

const CFG = window.ETSY_CONFIG || {};
const BASE = String(CFG.supabaseUrl || '').replace(/\/+$/, '');
const ANON = CFG.anonKey || '';

const SORTS = {
  favorites: 'favorites.desc.nullslast',
  momentum: 'fav_gain.desc.nullslast,favorites.desc',
  newest: 'first_seen.desc',
  price_asc: 'price_usd.asc.nullslast',
  price_desc: 'price_usd.desc.nullslast',
  reviews: 'reviews.desc.nullslast',
};

const state = { q: '', status: 'all', sort: 'favorites', limit: 48, offset: 0, total: 0, rows: [] };
const $ = (id) => document.getElementById(id);

/* ── transport ───────────────────────────────────────────────────────── */
async function rest(path, { method = 'GET', body, count = false, prefer } = {}) {
  const headers = {
    apikey: ANON,
    Authorization: `Bearer ${ANON}`,
    'Content-Type': 'application/json',
  };
  if (count) headers.Prefer = 'count=exact';
  if (prefer) headers.Prefer = prefer;
  const res = await fetch(`${BASE}/rest/v1/${path}`, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    const err = new Error(`${res.status} ${text.slice(0, 300)}`);
    err.status = res.status;
    throw err;
  }
  const type = res.headers.get('content-type') || '';
  const data = type.includes('json') ? await res.json() : null;
  return { data, res };
}

function writeToken(force = false) {
  let t = localStorage.getItem('etsy_token');
  if (!t || force) {
    t = window.prompt('Write token — the SUPABASE_WRITE_TOKEN value from your .env.\nIt is stored only in this browser.');
    if (!t) throw new Error('cancelled');
    localStorage.setItem('etsy_token', t.trim());
    t = t.trim();
  }
  return t;
}

async function rpc(fn, args) {
  try {
    return await rest(`rpc/${fn}`, { method: 'POST', body: { ...args, p_token: writeToken() } });
  } catch (e) {
    if (e.status === 401) return rest(`rpc/${fn}`, { method: 'POST', body: { ...args, p_token: writeToken(true) } });
    throw e;
  }
}

/* ── helpers ─────────────────────────────────────────────────────────── */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (v) => (v === null || v === undefined ? '—' : `$${Number(v).toFixed(2)}`);
const num = (v) => (v === null || v === undefined ? '—' : Intl.NumberFormat().format(v));

function ago(iso) {
  if (!iso) return '—';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function toast(msg, bad = false) {
  const el = $('toast');
  el.textContent = msg;
  el.style.background = bad ? 'var(--down)' : '';
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 3200);
}

/* ── header ──────────────────────────────────────────────────────────── */
async function loadStats() {
  try {
    const { data } = await rest('stats?select=*');
    const s = (data && data[0]) || {};
    $('s-listings').textContent = num(s.listings);
    $('s-snaps').textContent = num(s.snapshots);
    $('s-queue').textContent = num(s.active_keywords);
    $('s-seen').textContent = ago(s.last_seen);
    const fresh = s.last_seen && Date.now() - new Date(s.last_seen).getTime() < 3 * 3600e3;
    $('s-dot').className = 'dot' + (fresh ? '' : s.last_seen ? ' warn' : ' off');
  } catch (e) {
    $('s-dot').className = 'dot off';
  }
}

/* ── queue ───────────────────────────────────────────────────────────── */
async function loadKeywords() {
  const { data } = await rest('keywords?select=keyword,depth,next_page,pass_no,status,pages_per_run,listings_seen,last_run_at&order=added_at.asc');
  const box = $('chips');
  if (!data || !data.length) { box.innerHTML = '<span class="meta" style="color:var(--muted);font-size:13px">Queue is empty — add a keyword to start.</span>'; return; }
  box.innerHTML = data.map((k) => {
    const running = k.status === 'running';
    const page = Math.min(k.next_page, k.depth);
    return `<span class="kw" data-kw="${esc(k.keyword)}">
      <span class="name">${esc(k.keyword)}</span>
      <span class="pill ${esc(k.status)}">${esc(k.status)}</span>
      <span class="meta">p${page}/${k.depth} · pass ${k.pass_no} · ${num(k.listings_seen)} seen</span>
      <button data-act="toggle" title="${running ? 'Pause' : 'Resume'}">${running ? '❙❙' : '▶'}</button>
      <button data-act="del" title="Remove from queue">✕</button>
    </span>`;
  }).join('');
}

$('chips').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  const kw = btn.closest('.kw').dataset.kw;
  const act = btn.dataset.act;
  try {
    if (act === 'del') {
      if (!window.confirm(`Remove "${kw}" from the queue? Listings already collected stay.`)) return;
      await rpc('delete_keyword', { p_keyword: kw });
      toast(`"${kw}" removed`);
    } else {
      const paused = btn.textContent.trim() === '▶';
      await rpc('set_keyword_status', { p_keyword: kw, p_status: paused ? 'queued' : 'paused' });
      toast(`"${kw}" ${paused ? 'resumed' : 'paused'}`);
    }
    await Promise.all([loadKeywords(), loadStats()]);
  } catch (e) {
    toast(e.message === 'cancelled' ? 'cancelled' : `failed: ${e.message}`, true);
  }
});

$('kw-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const kw = $('kw').value.trim();
  if (kw.length < 2) return;
  const depth = Math.max(1, Math.min(100, Number($('depth').value) || 20));
  const ppr = Math.max(1, Math.min(5, Number($('ppr').value) || 2));
  const btn = ev.target.querySelector('button[type=submit]');
  btn.disabled = true;
  try {
    await rpc('add_keyword', { p_keyword: kw, p_depth: depth, p_pages_per_run: ppr });
    $('kw').value = '';
    toast(`"${kw}" queued — next run picks it up within two hours`);
    await Promise.all([loadKeywords(), loadStats()]);
  } catch (e) {
    toast(e.message === 'cancelled' ? 'cancelled' : `failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
});

/* ── listings ────────────────────────────────────────────────────────── */
function buildQuery() {
  const p = new URLSearchParams();
  p.set('select', 'etsy_id,title,url,shop_name,price_usd,prev_price,favorites,prev_favorites,fav_gain,reviews,rating,image_url,category_path,starred,obs_count,last_seen,first_seen,first_keyword');
  p.set('order', SORTS[state.sort] || SORTS.favorites);
  p.set('limit', state.limit);
  p.set('offset', state.offset);
  const q = state.q.replace(/[*,()'"]/g, '').trim();
  if (q) p.set('or', `(title.ilike.*${q}*,shop_name.ilike.*${q}*,description.ilike.*${q}*)`);
  if (state.status === 'rising') p.set('fav_gain', 'gt.0');
  if (state.status === 'new') p.set('obs_count', 'eq.1');
  if (state.status === 'starred') p.set('starred', 'is.true');
  return p.toString();
}

function card(r) {
  const delta = r.fav_gain == null ? '' : r.fav_gain > 0
    ? `<span class="delta up">+${num(r.fav_gain)}</span>`
    : r.fav_gain < 0 ? `<span class="delta down">${num(r.fav_gain)}</span>` : '';
  const priceDelta = r.prev_price != null && r.price_usd != null && Number(r.prev_price) !== Number(r.price_usd)
    ? `<span class="delta ${Number(r.price_usd) < Number(r.prev_price) ? 'down' : 'up'}">was ${money(r.prev_price)}</span>` : '';
  const tags = [
    r.obs_count > 1 ? `<span class="tag">${r.obs_count} looks</span>` : '<span class="tag">new</span>',
    r.reviews ? `<span class="tag">${num(r.reviews)} reviews</span>` : '',
    r.rating ? `<span class="tag">★ ${r.rating}</span>` : '',
    r.first_keyword ? `<span class="tag">${esc(r.first_keyword)}</span>` : '',
  ].join('');
  return `<article class="card" data-id="${esc(r.etsy_id)}">
    <div class="thumb" data-act="open">
      ${r.image_url ? `<img loading="lazy" src="${esc(r.image_url)}" alt="" onerror="this.style.display='none'">` : ''}
      <button class="star" data-act="star" data-on="${r.starred}" title="Shortlist">★</button>
    </div>
    <div class="body">
      <h3>${esc(r.title || '(untitled)')}</h3>
      <div class="shop">${esc(r.shop_name || '')}</div>
      <div class="row"><span class="price">${money(r.price_usd)}</span>${priceDelta}</div>
      <div class="row"><span class="delta">♥ ${num(r.favorites)}</span>${delta}<span class="delta" style="margin-left:auto">${ago(r.last_seen)}</span></div>
      <div class="tags">${tags}</div>
    </div>
    <div class="actions">
      <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Etsy ↗</a>
      <button data-act="open">Details</button>
    </div>
  </article>`;
}

async function loadListings(append = false) {
  if (!append) { state.offset = 0; state.rows = []; }
  const { data, res } = await rest(`listings?${buildQuery()}`, { count: true });
  const rows = data || [];
  state.rows = append ? state.rows.concat(rows) : rows;

  const range = res.headers.get('content-range') || '';
  state.total = Number((range.split('/')[1] || '0')) || rows.length;

  $('grid').innerHTML = state.rows.map(card).join('');
  $('empty').hidden = state.rows.length > 0;
  $('more-wrap').hidden = state.rows.length >= state.total;
  $('count').textContent = `${num(state.total)} result${state.total === 1 ? '' : 's'}`;
}

/* ── interactions ────────────────────────────────────────────────────── */
let searchTimer;
$('q').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value; loadListings().catch(fail); }, 260);
});

$('seg').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  [...$('seg').children].forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  state.status = b.dataset.status;
  loadListings().catch(fail);
});

$('sort').addEventListener('change', (e) => { state.sort = e.target.value; loadListings().catch(fail); });
$('more').addEventListener('click', () => { state.offset += state.limit; loadListings(true).catch(fail); });

$('grid').addEventListener('click', async (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const cardEl = el.closest('.card');
  const id = cardEl.dataset.id;
  if (el.dataset.act === 'star') {
    e.stopPropagation();
    try {
      const on = el.dataset.on !== 'true';
      await rpc('set_star', { p_etsy_id: id, p_on: on });
      el.dataset.on = String(on);
    } catch (err) { toast(err.message, true); }
    return;
  }
  openDrawer(id);
});

async function openDrawer(id) {
  $('scrim').classList.add('open');
  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  $('drawer-body').innerHTML = '<p style="color:var(--muted)">Loading…</p>';
  try {
    const { data } = await rest(`listings?etsy_id=eq.${encodeURIComponent(id)}&select=*`);
    const r = (data && data[0]) || null;
    if (!r) throw new Error('not found');
    const { data: hist } = await rest(`snapshots?etsy_id=eq.${encodeURIComponent(id)}&select=captured_at,favorites,price_usd&order=captured_at.desc&limit=40`);
    const pts = (hist || []).slice().reverse();
    $('drawer-body').innerHTML = `
      ${r.image_url ? `<img class="hero" src="${esc(r.image_url)}" alt="">` : ''}
      <h2>${esc(r.title || '(untitled)')}</h2>
      <div class="shop">${esc(r.shop_name || '')}</div>
      <dl class="kv">
        <dt>Price</dt><dd>${money(r.price_usd)}${r.prev_price != null && Number(r.prev_price) !== Number(r.price_usd) ? ` <span class="delta">was ${money(r.prev_price)}</span>` : ''}</dd>
        <dt>Favourites</dt><dd>${num(r.favorites)}${r.prev_favorites != null ? ` <span class="delta">was ${num(r.prev_favorites)}</span>` : ''}</dd>
        <dt>Reviews</dt><dd>${num(r.reviews)}${r.rating ? ` · ★ ${r.rating}` : ''}</dd>
        <dt>Category</dt><dd>${esc(r.category_path || '—')}</dd>
        <dt>First seen</dt><dd>${r.first_seen ? new Date(r.first_seen).toLocaleDateString() : '—'} · ${r.obs_count} observation${r.obs_count === 1 ? '' : 's'}</dd>
        <dt>First keyword</dt><dd>${esc(r.first_keyword || '—')}</dd>
        <dt>Observations</dt><dd>${pts.length ? sparkline(pts.map((p) => p.favorites)) : '—'}</dd>
      </dl>
      <p><a class="btn sm" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open on Etsy ↗</a></p>
      <div class="desc">${esc(r.description || 'No description captured.')}</div>`;
  } catch (err) {
    $('drawer-body').innerHTML = `<p style="color:var(--down)">${esc(err.message)}</p>`;
  }
}

function sparkline(vals) {
  if (!vals || vals.length < 2) return '<span class="delta">not enough history yet</span>';
  const w = 160, h = 34, min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const d = vals.map((v, i) => `${(i / (vals.length - 1)) * w},${h - ((v - min) / span) * (h - 6) - 3}`).join(' ');
  return `<svg width="${w}" height="${h}" style="overflow:visible"><polyline points="${d}" fill="none" stroke="#2c6a45" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
}

function closeDrawer() {
  $('scrim').classList.remove('open');
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
}
$('drawer-close').addEventListener('click', closeDrawer);
$('scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

/* ── runs ────────────────────────────────────────────────────────────── */
async function loadRuns() {
  try {
    const { data } = await rest('runs?select=started_at,keyword,page_from,page_to,status,listings,challenges,egress_ip&order=started_at.desc&limit=10');
    $('runs').innerHTML = (data || []).map((r) => `<tr>
      <td>${r.started_at ? new Date(r.started_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
      <td>${esc(r.keyword || '—')}</td>
      <td>${r.page_from ?? '—'}–${r.page_to ?? '—'}</td>
      <td class="${r.status === 'succeeded' ? 'ok' : 'bad'}">${esc(r.status || '—')}</td>
      <td>${num(r.listings)}</td>
      <td>${num(r.challenges)}</td>
      <td>${esc(r.egress_ip || '—')}</td>
    </tr>`).join('') || '<tr><td colspan="7" style="color:var(--muted)">No runs yet.</td></tr>';
  } catch (e) { /* runs table is decoration; never break the page for it */ }
}

/* ── boot ────────────────────────────────────────────────────────────── */
function fail(e) { toast(e.message, true); }

$('token-btn').addEventListener('click', () => {
  writeToken(true);
  toast('token saved in this browser');
});

(async function boot() {
  if (!BASE || !ANON) {
    document.body.innerHTML = '<p style="padding:40px;font:15px system-ui">Missing <code>web/config.js</code> — it must define supabaseUrl and anonKey.</p>';
    return;
  }
  loadListings().catch(fail);
  loadStats().catch(fail);
  loadKeywords().catch(fail);
  loadRuns();
  setInterval(loadStats, 60000);
})();
