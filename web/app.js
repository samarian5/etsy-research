/* Etsy research board — static SPA talking straight to Supabase's PostgREST.
 *
 * No bundler, no CDN, no build step: GitHub Pages serves these three files and
 * the browser speaks to the database directly. Reads use the anon key, which
 * RLS confines to SELECT; writes go through four token-gated RPCs, so the key
 * that can mutate data never reaches the browser.
 */
'use strict';

const CFG = window.ETSY_CONFIG || {};
const BUILD = window.__BUILD || (document.querySelector('meta[name="build"]') || {}).content || 'dev';

/* Anything that fails to load must say so on the page — a blank board is not a
 * diagnosis, and a toast that fades is not evidence. */
function showBanner(msg, bad = true) {
  const el = $('banner');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('bad', bad);
  el.hidden = false;
}
function hideBanner() {
  const el = $('banner');
  if (el) el.hidden = true;
}
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

/* ── session ─────────────────────────────────────────────────────────── */
/* The board is private now: every read and every write carries the owner's
 * session token, and the database refuses anything else. Sign-in is a magic
 * link to the single permitted address. */

/* HTML and JS are cached independently by Pages, so they can be one deploy out
 * of step. Never let a missing element throw at load time: a mismatch must
 * degrade to "that control is absent", not "the page is dead". */
const on = (id, ev, fn) => {
  const el = typeof id === 'string' ? $(id) : id;
  if (el) el.addEventListener(ev, fn);
  return el;
};

const SESSION_KEY = 'etsy_session';
const OWNER_EMAIL = 'pityailon@gmail.com';
let SESSION = null;

function loadSession() {
  try { SESSION = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); } catch { SESSION = null; }
  return SESSION;
}

function saveSession(s) {
  SESSION = {
    access_token: s.access_token,
    refresh_token: s.refresh_token || (SESSION && SESSION.refresh_token) || null,
    expires_at: Math.floor(Date.now() / 1000) + Number(s.expires_in || 3600),
    email: (s.user && s.user.email) || (SESSION && SESSION.email) || OWNER_EMAIL,
  };
  localStorage.setItem(SESSION_KEY, JSON.stringify(SESSION));
}

function clearSession() {
  SESSION = null;
  localStorage.removeItem(SESSION_KEY);
  localStorage.removeItem('etsy_token');   // leftovers from the token era
}

function sessionLive() {
  return !!(SESSION && SESSION.access_token && SESSION.expires_at - 30 > Date.now() / 1000);
}

async function authCall(path, { method = 'POST', body } = {}) {
  const res = await fetch(`${BASE}/auth/v1/${path}`, {
    method,
    headers: { apikey: ANON, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.msg || data.error_description || data.error || `auth ${res.status}`);
    err.status = res.status;
    err.code = data.error_code;
    throw err;
  }
  return data;
}

async function refreshSession() {
  if (!SESSION || !SESSION.refresh_token) return false;
  try {
    saveSession(await authCall('token?grant_type=refresh_token', { body: { refresh_token: SESSION.refresh_token } }));
    return true;
  } catch {
    clearSession();
    return false;
  }
}

async function ensureSession() {
  if (sessionLive()) return true;
  return await refreshSession();
}

function notSignedIn() {
  const e = new Error('');
  e.silent = true;
  return e;
}

async function signIn(email) {
  return authCall('otp', { body: { email, create_user: false } });
}

async function signOut() {
  const tok = SESSION && SESSION.access_token;
  clearSession();
  if (tok) {
    fetch(`${BASE}/auth/v1/logout`, { method: 'POST', headers: { apikey: ANON, Authorization: `Bearer ${tok}` } }).catch(() => {});
  }
  wipeBoard();
  showGate(true);
  gateStatus('signed out', false);
}

/* Signing out must leave nothing readable behind — not just hide the board. */
function wipeBoard() {
  ['s-listings', 's-snaps', 's-queue', 's-seen', 'kpi-pages'].forEach(id => {
    const el = $(id);
    if (el) el.textContent = '—';
  });
  const body = $('queue-body');
  if (body) body.innerHTML = '';
  const next = $('next-up');
  if (next) next.textContent = '';
  const bt = $('budget-text');
  if (bt) bt.textContent = '—';
  const dt = $('demand-text');
  if (dt) dt.textContent = '';
  const runs = $('runs');
  if (runs) runs.innerHTML = '';
  const note = $('kpi-pages-note');
  if (note) note.textContent = 'loading budget…';
  const meter = $('kpi-meter');
  if (meter) meter.style.width = '0%';
  const market = $('market-body');
  if (market) market.innerHTML = '';
  if (typeof state !== 'undefined') {
    state.rows = [];
    state.total = 0;
    state.offset = 0;
    const grid = $('grid');
    if (grid) grid.innerHTML = '';
  }
}

/* Supabase hands the session back in the URL fragment after a magic link. */
function absorbLinkSession() {
  const raw = (location.hash || '').replace(/^#/, '') || (location.search || '').replace(/^\?/, '');
  if (!raw) return false;
  const p = new URLSearchParams(raw);
  const why = p.get('error_description') || p.get('error');
  const token = p.get('access_token');
  if (!why && !token) return false;
  history.replaceState(null, '', location.pathname);
  if (why) {
    showGate(true);
    const expired = /invalid|expired/i.test(why);
    gateStatus(expired
      ? 'That link was already used, replaced by a newer sign-in email, or is more than an hour old. Only the most recent email works — send a fresh one below.'
      : `${decodeURIComponent(why).replace(/\+/g, ' ')} — send a fresh link below.`, true);
    return false;
  }
  saveSession({
    access_token: token,
    refresh_token: p.get('refresh_token'),
    expires_in: p.get('expires_in'),
  });
  return true;
}

/* ── transport ───────────────────────────────────────────────────────── */
async function rest(path, { method = 'GET', body, count = false, prefer } = {}) {
  if (!(await ensureSession())) {
    showGate(true);
    throw notSignedIn();
  }
  const headers = {
    apikey: ANON,
    Authorization: `Bearer ${SESSION.access_token}`,
    'Content-Type': 'application/json',
  };
  if (count) headers.Prefer = 'count=exact';
  if (prefer) headers.Prefer = prefer;
  const res = await fetch(`${BASE}/rest/v1/${path}`, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    // the access token expired mid-session; refresh once, then give up loudly
    if (await refreshSession()) return rest(path, { method, body, count, prefer });
    showGate(true);
    throw notSignedIn();
  }
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

async function rpc(fn, args = {}) {
  // the functions still declare p_token (it remains a valid break-glass path),
  // so it must be present in the payload even when the session authorises us
  const { data } = await rest(`rpc/${fn}`, { method: 'POST', body: { p_token: null, ...args } });
  return { data };
}

function fail(e) {
  if (e && e.silent) return;
  toast(e.message === 'cancelled' ? 'cancelled' : `failed: ${e.message}`, true);
}

/* ── the sign-in gate ────────────────────────────────────────────────── */
function gateStatus(text, bad) {
  const el = $('gate-msg');
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
  el.classList.toggle('bad', !!bad);
}

function showGate(on) {
  const gate = $('gate');
  if (!gate) return;
  gate.hidden = !on;
  document.body.dataset.auth = on ? 'out' : 'in';
  if (on) setTimeout(() => { const i = $('gate-email'); if (i && !i.value) i.value = OWNER_EMAIL; i && i.focus(); }, 30);
}

/* The primary flow: email + password. One POST, no email, no hourly cap,
 * no fragment to lose, works identically on every device. */
async function submitGate(ev) {
  ev.preventDefault();
  const email = ($('gate-email').value || '').trim().toLowerCase();
  const pw = ($('gate-pw').value || '').trim();
  const btn = $('gate-signin');
  if (!email || !pw) { gateStatus('enter your email and password', true); return; }
  btn.disabled = true;
  btn.textContent = 'signing in…';
  gateStatus('', false);
  try {
    const data = await authCall(`token?grant_type=password`, { body: { email, password: pw } });
    if (!data || !data.access_token) throw new Error('no session returned');
    saveSession(data);
    $('gate-pw').value = '';
    gateStatus('signed in', false);
    showGate(false);
    $('acct-btn').textContent = (SESSION.email || OWNER_EMAIL).split('@')[0];
    startBoard();
  } catch (e) {
    const msg = e.status === 400 || e.code === 'invalid_grant'
      ? 'that email and password do not match'
      : e.status === 429
        ? 'too many attempts — wait a minute and try again'
        : `could not sign in: ${e.message}`;
    gateStatus(msg, true);
    btn.textContent = 'Sign in';
  } finally {
    btn.disabled = false;
    if (btn.textContent === 'signing in…') btn.textContent = 'Sign in';
  }
}

/* Everything the board needs after a successful sign-in, in one place. */
function startBoard() {
  const board = [
    ['listings', loadListings], ['stats', loadStats], ['queue', loadQueue], ['usage', loadUsage],
    ['market', loadMarket], ['signals', loadSignals], ['briefs', loadBriefs], ['library', loadLibrary],
  ];
  board.forEach(([name, fn], i) => {
    // Twelve requests fired at once is enough to lose one on a cold connection.
    // The two heaviest panels (briefs, library) start a beat later; everything
    // else goes immediately, so the KPIs still paint on the first frame.
    const delay = (name === 'briefs' || name === 'library') ? 350 : 0;
    setTimeout(() => {
      fn().then(hideBanner).catch((e) => showBanner(`${name} failed to load: ${e.message} (build ${BUILD})`));
    }, delay);
  });
  loadRuns();
}

/* The emailed link is a long URL, and mail clients mangle long URLs. If the
 * click lands somewhere odd (a GitHub 404, an in-app browser), the user can
 * paste the link here and we verify it directly against GoTrue. */
function extractToken(raw) {
  const s = String(raw || '').trim();
  const m = /[?&]token=([^&\s]+)/.exec(s);
  if (m) { try { return decodeURIComponent(m[1]); } catch { return m[1]; } }
  if (/^\d{6,8}$/.test(s)) return s;        // the one-time code Supabase emails alongside the link
  if (/^[A-Za-z0-9_-]{20,}$/.test(s)) return s;
  return null;
}

async function verifyPasted(raw) {
  // Mail clients hand back HTML-escaped ampersands and hard-wrapped lines.
  const s = String(raw || '').replace(/&amp;/gi, '&').replace(/[\r\n\t]+/g, '').trim();
  // A whole link: just follow it. That is the exact path a working click takes,
  // including the fragment Supabase hands back, so there is nothing to reimplement.
  if (/^https?:\/\//i.test(s)) {
    location.href = s;
    return true;
  }
  // Otherwise treat it as a bare one-time code.
  if (!/^\d{6,8}$/.test(s)) throw new Error('paste the whole link, or the code from the email');
  const data = await authCall('verify', { body: { type: 'magiclink', token: s, email: OWNER_EMAIL } });
  if (!data || !data.access_token) throw new Error('that code was not accepted — it may already be used. Request a fresh one.');
  saveSession(data);
  return true;
}

function confirmDialog(title, body, okLabel = 'Confirm', danger = false) {
  return new Promise((resolve) => {
    const dlg = $('confirm');
    const scrim = $('confirm-scrim');
    $('confirm-title').textContent = title;
    $('confirm-body').textContent = body;
    const ok = $('confirm-ok');
    ok.textContent = okLabel;
    ok.classList.toggle('danger', !!danger);
    dlg.hidden = false;
    scrim.hidden = false;
    setTimeout(() => ok.focus(), 30);
    const close = (v) => {
      dlg.hidden = true;
      scrim.hidden = true;
      ok.removeEventListener('click', yes);
      $('confirm-cancel').removeEventListener('click', no);
      scrim.removeEventListener('click', no);
      document.removeEventListener('keydown', key);
      resolve(v);
    };
    const yes = () => close(true);
    const no = () => close(false);
    const key = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      if (e.key === 'Enter') { e.preventDefault(); close(true); }
    };
    ok.addEventListener('click', yes);
    on('confirm-cancel', 'click', no);
    scrim.addEventListener('click', no);
    document.addEventListener('keydown', key);
  });
}

/* ── helpers ─────────────────────────────────────────────────────────── */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (v) => (v === null || v === undefined ? '—' : `$${Number(v).toFixed(2)}`);
const num = (v) => (v === null || v === undefined ? '—' : Intl.NumberFormat().format(v));
/* The crawler stores a shop name, not a shop URL; Etsy shop slugs are the name. */
const shopLink = (r) => {
  const url = r.shop_url || (r.shop_name ? `https://www.etsy.com/shop/${encodeURIComponent(String(r.shop_name).replace(/\s+/g, ''))}` : '');
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(r.shop_name || 'visit shop')} ↗</a>` : '—';
};

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

/* ── queue / schedule ─────────────────────────────────────────────────
 * Every keyword carries interval_hours (1–24) and due_at. The runner asks the
 * database "what is due?" on each tick, so the board's job is only to make that
 * schedule legible: what it will do next, how long a full lap takes, and whether
 * the daily budget can actually pay for what has been asked.
 */
const PAGES_PER_SLICE = 2;          // default pages_per_run; per-keyword value wins
const PAGE_LISTINGS = 48;           // Etsy shows ~48 results per search page
let QUEUE = [];
let USAGE = { pages_used: 0, pages_budget: 24 };

function fillIntervals(sel, value = 2) {
  sel.innerHTML = Array.from({ length: 24 }, (_, i) => {
    const h = i + 1;
    return `<option value="${h}"${h === value ? ' selected' : ''}>${ivLabel(h)}</option>`;
  }).join('');
}
function ivLabel(h) {
  if (h === 1) return 'every hour';
  if (h === 24) return 'once a day';
  return `every ${h} hours`;
}
function dueLabel(k) {
  if (k.status === 'paused') return '<span class="muted">paused</span>';
  if (k.status === 'blocked') return '<span class="warn">parked</span>';
  const t = k.due_at ? new Date(k.due_at).getTime() - Date.now() : 0;
  if (t <= 60000) return '<b class="now-tag">now</b>';
  if (t < 3600e3) return `in ${Math.round(t / 60000)} min`;
  const h = t / 3600e3;
  return h < 48 ? `in ${h.toFixed(h < 10 ? 1 : 0)} h` : `in ${Math.round(h / 24)} d`;
}
const lapSlices = (k) => Math.ceil(k.depth / (k.pages_per_run || PAGES_PER_SLICE));
const lapHours = (k) => lapSlices(k) * k.interval_hours;

function demandPerDay() {
  return QUEUE.filter((k) => k.status !== 'paused' && k.status !== 'blocked')
    .reduce((sum, k) => sum + (24 / k.interval_hours) * (k.pages_per_run || PAGES_PER_SLICE), 0);
}

async function loadQueue() {
  const { data } = await rest('keywords?select=keyword,depth,next_page,pass_no,status,pages_per_run,interval_hours,due_at,listings_seen,last_run_at&order=added_at.asc');
  QUEUE = data || [];
  $('queue-empty').hidden = QUEUE.length > 0;
  $('queue-body').innerHTML = QUEUE.map((k) => {
    const pages = k.pages_per_run || PAGES_PER_SLICE;
    const done = Math.min(k.next_page - 1, k.depth);
    const pct = Math.round((done / k.depth) * 100);
    const pill = k.status === 'running' ? 'running' : k.status === 'paused' ? 'paused' : k.status === 'blocked' ? 'blocked' : 'queued';
    const paused = k.status === 'paused';
    return `<tr data-kw="${esc(k.keyword)}">
      <td class="chk"><input type="checkbox" class="pick" aria-label="Select ${esc(k.keyword)}"></td>
      <td class="kw-name">
        <b>${esc(k.keyword)}</b>
        <span class="pill ${pill}">${paused ? 'paused' : k.status === 'blocked' ? 'parked' : k.status === 'running' ? 'crawling' : 'scheduled'}</span>
      </td>
      <td><select class="iv" aria-label="Interval for ${esc(k.keyword)}" data-cur="${k.interval_hours}"></select></td>
      <td><input class="dp" type="number" min="1" max="100" value="${k.depth}" aria-label="Depth for ${esc(k.keyword)}"></td>
      <td class="prog">
        <span>page ${Math.min(k.next_page, k.depth)} of ${k.depth}</span>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <span class="tiny">${pct}% · lap ≈ ${lapHours(k) < 48 ? lapHours(k) + ' h' : Math.round(lapHours(k) / 24) + ' d'} · ${pages} pages/wake-up</span>
      </td>
      <td class="due">${dueLabel(k)}</td>
      <td class="last">${ago(k.last_run_at)}</td>
      <td class="acts">
        <button data-act="toggle" title="${paused ? 'Resume' : 'Pause'}">${paused ? '▶' : '❙❙'}</button>
        <button data-act="del" title="Remove from queue">✕</button>
      </td>
    </tr>`;
  }).join('');

  $('queue-body').querySelectorAll('.iv').forEach((sel) => fillIntervals(sel, Number(sel.dataset.cur)));
  syncBulk();
  renderNextUp();
  renderBudget();
}

function renderNextUp() {
  const live = QUEUE.filter((k) => k.status !== 'paused' && k.status !== 'blocked');
  const el = $('next-up');
  if (!live.length) { el.innerHTML = '<span class="muted">nothing scheduled</span>'; return; }
  el.innerHTML = live.slice().sort((a, b) => new Date(a.due_at) - new Date(b.due_at))
    .slice(0, 2)
    .map((k) => `<span class="next-chip"><span>${esc(k.keyword)}</span><span class="sep">·</span><b>${dueLabel(k)}</b></span>`)
    .join('');
}

function renderBudget() {
  const used = Number(USAGE.pages_used || 0);
  const budget = Number(USAGE.pages_budget || 24);
  const demand = demandPerDay();
  const pct = Math.min(100, Math.round((used / budget) * 100));
  $('budget-text').innerHTML = `<b>${used}</b> of ${budget} search pages used today`;
  $('budget-bar').style.width = pct + '%';
  $('budget-bar').className = pct >= 100 ? 'full' : pct > 75 ? 'high' : '';
  $('budget-input').value = budget;

  const slicesLeft = Math.floor((budget - used) / PAGES_PER_SLICE);
  let msg;
  if (demand > budget) {
    const need = Math.ceil(demand / 2) * 2;
    msg = `<span class="warn">Your schedule asks for ${Math.round(demand)} pages/day but the budget allows ${budget}. `
        + `Only ${Math.floor(budget / PAGES_PER_SLICE)} wake-ups fit, so slower keywords will drift. `
        + `Raise the budget to ${need} or lengthen an interval.</span>`;
  } else if (used >= budget) {
    msg = `<span class="warn">Budget spent — nothing runs until midnight UTC. The schedule resumes by itself.</span>`;
  } else {
    msg = `<span class="ok">Schedule asks for ${Math.round(demand)} of ${budget} pages/day · `
        + `${slicesLeft} wake-up${slicesLeft === 1 ? '' : 's'} still available today.</span>`;
  }
  $('demand-text').innerHTML = msg;

  // The same numbers at the top of the page, where they are read first.
  const kp = $('kpi-pages'), kn = $('kpi-pages-note'), km = $('kpi-meter');
  if (kp) kp.textContent = `${used} / ${budget}`;
  if (kn) {
    kn.textContent = used >= budget
      ? 'budget spent · resets at midnight UTC'
      : `${slicesLeft} wake-up${slicesLeft === 1 ? '' : 's'} left today`;
  }
  if (km) {
    km.style.width = pct + '%';
    km.className = pct >= 100 ? 'full' : pct > 75 ? 'high' : '';
  }
}

function renderExplainers() {
  const depth = Math.max(1, Math.min(100, Number($('kw-depth').value) || 20));
  const iv = Number($('kw-interval').value) || 2;
  const slices = Math.ceil(depth / PAGES_PER_SLICE);
  const lap = slices * iv;
  const lapTxt = lap < 48 ? `${lap} hours` : `${(lap / 24).toFixed(lap % 24 ? 1 : 0)} days`;
  $('depth-explain').innerHTML = `
    <p><b>Depth is how far down the search results we walk for one keyword.</b>
    Etsy returns about ${PAGE_LISTINGS} listings per page, so <b>depth ${depth}</b> means
    pages 1–${depth} — roughly <b>${num(depth * PAGE_LISTINGS)} listings</b> before the crawler
    starts over at page 1.</p>
    <p>Pages 1–3 are the shops everyone already sees. The deeper pages are where newer
    listings with rising favourite counts hide, which is usually the interesting part.
    Depth 2–3 is a quick look; 20 is a solid default; 40+ chases the long tail and takes
    proportionally longer to complete.</p>
    <p class="tiny">At depth ${depth} the crawler makes ${slices} wake-ups per full lap. At
    “${ivLabel(iv)}” that is ${lapTxt} per lap — and about
    ${Math.round((24 / iv) * PAGES_PER_SLICE)} pages of your daily budget.</p>`;

  $('interval-explain').innerHTML = `
    <p><b>“Check every” is how often we come back for that keyword’s next two pages.</b>
    It does not mean “crawl everything hourly”: one wake-up always fetches two search pages,
    so an hourly keyword still walks through its depth two pages at a time.</p>
    <p>Hourly is the most responsive — a brand-new listing can be seen within the hour and
    its favourite count gets a fine-grained history. Once a day is enough for stable niches
    and costs twelve times less budget.</p>
    <p class="tiny">Total demand = (24 ÷ interval) × 2 pages for each keyword. That total has to
    fit inside the daily crawl budget below, which is what keeps Etsy from refusing us.</p>`;
}

function selectedKeywords() {
  return [...document.querySelectorAll('#queue-body .pick:checked')]
    .map((c) => c.closest('tr').dataset.kw);
}

function syncBulk() {
  const n = selectedKeywords().length;
  $('bulk').hidden = n === 0;
  $('bulk-count').textContent = `${n} selected`;
  const all = document.querySelectorAll('#queue-body .pick');
  $('check-all').checked = n > 0 && n === all.length;
}

on('queue-body', 'change', async (ev) => {
  const el = ev.target;
  const row = el.closest('tr');
  if (!row) return;
  const kw = row.dataset.kw;
  if (el.classList.contains('pick')) { syncBulk(); return; }
  try {
    if (el.classList.contains('iv')) {
      await rpc('update_keyword', { p_keyword: kw, p_interval_hours: Number(el.value) });
      toast(`“${kw}” ${ivLabel(Number(el.value))}`);
    } else if (el.classList.contains('dp')) {
      const d = Math.max(1, Math.min(100, Number(el.value) || 20));
      await rpc('update_keyword', { p_keyword: kw, p_depth: d });
      toast(`“${kw}” depth set to ${d}`);
    }
    await loadQueue();
  } catch (e) {
    fail(e);
    await loadQueue();
  }
});

on('queue-body', 'click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const kw = btn.closest('tr').dataset.kw;
  try {
    if (btn.dataset.act === 'del') {
      const ok = await confirmDialog(`Remove “${kw}”?`,
        'It stops being crawled. Listings already collected stay on the board and can still be browsed.',
        'Remove', true);
      if (!ok) return;
      await rpc('delete_keyword', { p_keyword: kw });
      toast(`“${kw}” removed from the queue`);
    } else {
      const paused = btn.textContent.trim() === '▶';
      await rpc('set_keyword_status', { p_keyword: kw, p_status: paused ? 'queued' : 'paused' });
      toast(`“${kw}” ${paused ? 'resumed' : 'paused'}`);
    }
    await Promise.all([loadQueue(), loadStats()]);
  } catch (e) {
    if (e && e.silent) return;
    fail(e);
  }
});

on('check-all', 'change', (e) => {
  document.querySelectorAll('#queue-body .pick').forEach((c) => { c.checked = e.target.checked; });
  syncBulk();
});
on('bulk-clear', 'click', () => {
  document.querySelectorAll('#queue-body .pick').forEach((c) => { c.checked = false; });
  syncBulk();
});
on('bulk-apply', 'click', async () => {
  const kws = selectedKeywords();
  if (!kws.length) return;
  const h = Number($('bulk-interval').value);
  try {
    const { data } = await rpc('set_interval_bulk', { p_keywords: kws, p_interval_hours: h });
    toast(`${kws.length} keyword${kws.length === 1 ? '' : 's'} set to ${ivLabel(h)}` + (typeof data === 'number' ? ` (${data} updated)` : ''));
    await loadQueue();
  } catch (e) {
    if (e && e.silent) return;
    fail(e);
  }
});

on('kw-form', 'submit', async (ev) => {
  ev.preventDefault();
  const kw = $('kw').value.trim();
  if (kw.length < 2) return;
  const depth = Math.max(1, Math.min(100, Number($('kw-depth').value) || 20));
  const interval = Number($('kw-interval').value) || 2;
  const btn = ev.target.querySelector('button[type=submit]');
  btn.disabled = true;
  try {
    await rpc('add_keyword', { p_keyword: kw, p_depth: depth, p_interval_hours: interval });
    $('kw').value = '';
    toast(`“${kw}” queued — ${ivLabel(interval)}, depth ${depth}`);
    await Promise.all([loadQueue(), loadStats(), loadUsage()]);
  } catch (e) {
    fail(e);
  } finally {
    btn.disabled = false;
  }
});

on('budget-save', 'click', async () => {
  const v = Math.max(6, Math.min(200, Number($('budget-input').value) || 24));
  try {
    await rpc('set_page_budget', { p_pages_per_day: v });
    toast(`Daily budget set to ${v} pages`);
    await loadUsage();
  } catch (e) {
    if (e && e.silent) return;
    fail(e);
  }
});

on('kw-depth', 'input', renderExplainers);
on('kw-interval', 'change', renderExplainers);

async function loadUsage() {
  try {
    const { data } = await rest('usage_today?select=*');
    USAGE = (data && data[0]) || USAGE;
  } catch (e) { /* the meter is advisory; never break the board for it */ }
  renderBudget();
}

/* ── listings ────────────────────────────────────────────────────────── */
function buildQuery() {
  const p = new URLSearchParams();
  p.set('select', 'etsy_id,title,url,shop_name,shop_url,price_usd,prev_price,original_price_usd,discount_pct,favorites,prev_favorites,fav_gain,reviews,rating,shop_reviews,shop_rating,listing_date,image_count,availability,quantity,shipping_origin,is_ad,image_url,category_path,starred,obs_count,last_seen,first_seen,first_keyword');
  p.set('order', SORTS[state.sort] || SORTS.favorites);
  p.set('limit', state.limit);
  p.set('offset', state.offset);
  const q = state.q.replace(/[*,()'"]/g, '').trim();
  if (q) p.set('or', `(title.ilike.*${q}*,shop_name.ilike.*${q}*,description.ilike.*${q}*)`);
  if (state.status === 'rising') p.set('fav_gain', 'gt.0');
  if (state.status === 'new') p.set('obs_count', 'eq.1');
  if (state.status === 'starred') p.set('starred', 'is.true');
  // "No ads": sponsored cards are interleaved with organic ones, so a rank read
  // off an unfiltered list is partly a read of Etsy's ad auction.
  if (state.status === 'organic') p.set('is_ad', 'not.is.true');
  return p.toString();
}

function listingAge(iso) {
  if (!iso) return '';
  const days = Math.round((Date.now() - new Date(iso + 'T00:00:00Z').getTime()) / 86400000);
  if (!isFinite(days) || days < 0) return '';
  if (days < 31) return days <= 1 ? 'listed today' : `listed ${days}d`;
  if (days < 365) return `listed ${Math.round(days / 30)}mo`;
  return `listed ${(days / 365).toFixed(1)}y`;
}

function card(r) {
  const gain = r.fav_gain == null ? '' : r.fav_gain > 0
    ? `<span class="delta up">+${num(r.fav_gain)}</span>`
    : r.fav_gain < 0 ? `<span class="delta down">${num(r.fav_gain)}</span>` : '';
  const was = r.prev_price != null && r.price_usd != null && Number(r.prev_price) !== Number(r.price_usd)
    ? `<span class="delta ${Number(r.price_usd) < Number(r.prev_price) ? 'down' : 'up'}">was ${money(r.prev_price)}</span>` : '';
  const age = r.listing_date ? listingAge(r.listing_date).replace(/^listed /, '') : '—';
  const tags = [
    r.reviews ? `<span class="tag">${num(r.reviews)} reviews</span>` : '',
    r.rating ? `<span class="tag">★ ${r.rating}</span>` : '',
    r.discount_pct ? `<span class="tag">−${Math.round(Number(r.discount_pct))}% off</span>` : '',
    r.shop_reviews ? `<span class="tag quiet">shop ${num(r.shop_reviews)}</span>` : '',
    r.obs_count > 1 ? `<span class="tag quiet">${r.obs_count} readings</span>` : '<span class="tag quiet">first reading</span>',
    r.first_keyword ? `<span class="tag quiet">${esc(r.first_keyword)}</span>` : '',
  ].join('');
  return `<article class="card" data-id="${esc(r.etsy_id)}">
    <div class="thumb" data-act="open">
      ${r.image_url ? `<img loading="lazy" src="${esc(r.image_url)}" alt="" onerror="this.style.display='none'">` : ''}
      ${r.is_ad ? '<span class="badge-ad">Ad</span>' : ''}
      <button class="star" data-act="star" data-on="${r.starred}" title="Shortlist" aria-label="Shortlist this listing">★</button>
    </div>
    <div class="body">
      <div class="shop">${esc(r.shop_name || 'unknown shop')}</div>
      <h3>${esc(r.title || '(untitled listing)')}</h3>
      <div class="metrics">
        <div><b>${money(r.price_usd)}</b>${was}<span>price</span></div>
        <div><b>${num(r.favorites)}</b>${gain}<span>favourites</span></div>
        <div><b>${age}</b><span>listed</span></div>
      </div>
      <div class="tags">${tags}</div>
    </div>
    <div class="actions">
      <button data-act="open">Details</button>
      <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open on Etsy ↗</a>
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
on('q', 'input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value; loadListings().catch(fail); }, 260);
});

on('seg', 'click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  [...$('seg').children].forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  state.status = b.dataset.status;
  loadListings().catch(fail);
});

on('sort', 'change', (e) => { state.sort = e.target.value; loadListings().catch(fail); });
on('more', 'click', () => { state.offset += state.limit; loadListings(true).catch(fail); });

on('grid', 'click', async (e) => {
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
  $('drawer-body').innerHTML = '<p class="tiny" style="padding:24px 0">Loading…</p>';
  const closeBtn = $('drawer-close');
  if (closeBtn) setTimeout(() => closeBtn.focus(), 30);
  try {
    const { data } = await rest(`listings?etsy_id=eq.${encodeURIComponent(id)}&select=*`);
    const r = (data && data[0]) || null;
    if (!r) throw new Error('not found');
    const { data: hist } = await rest(`snapshots?etsy_id=eq.${encodeURIComponent(id)}&select=captured_at,favorites,price_usd&order=captured_at.desc&limit=40`);
    const pts = (hist || []).slice().reverse();
        $('drawer-body').innerHTML = `
      ${r.image_url ? `<img class="hero" src="${esc(r.image_url)}" alt="">` : ''}
      <h2>${esc(r.title || '(untitled)')}</h2>
      <div class="shop">${esc(r.shop_name || 'unknown shop')}${r.is_ad ? ' · sponsored slot' : ''}</div>

      <h4>The listing</h4>
      <dl class="kv">
        <dt>Price</dt><dd>${money(r.price_usd)}${r.prev_price != null && Number(r.prev_price) !== Number(r.price_usd) ? ` <span class="delta">was ${money(r.prev_price)}</span>` : ''}</dd>
        <dt>Favourites</dt><dd>${num(r.favorites)}${r.prev_favorites != null ? ` <span class="delta">was ${num(r.prev_favorites)}</span>` : ''}</dd>
        <dt>Listed</dt><dd>${r.listing_date ? `${esc(r.listing_date)} · ${listingAge(r.listing_date)}` : '—'}</dd>
        <dt>Category</dt><dd>${esc(r.category_path || '—')}</dd>
        <dt>First seen</dt><dd>${r.first_seen ? new Date(r.first_seen).toLocaleDateString() : '—'} · ${r.obs_count} reading${r.obs_count === 1 ? '' : 's'}</dd>
        <dt>Found by</dt><dd>${esc(r.first_keyword || '—')}</dd>
      </dl>

      <h4>The offer</h4>
      <dl class="kv">
        <dt>List price</dt><dd>${r.original_price_usd ? `${money(r.original_price_usd)}${r.discount_pct ? ` · ${Number(r.discount_pct)}% off` : ''}` : 'no discount shown'}</dd>
        <dt>Stock</dt><dd>${r.availability ? esc(r.availability) : '—'}${r.quantity ? ` · ${num(r.quantity)} available` : ''}</dd>
        <dt>Ships from</dt><dd>${esc(r.shipping_origin || '—')}</dd>
        <dt>Images</dt><dd>${r.image_count ? num(r.image_count) : '—'}</dd>
      </dl>

      <h4>The seller</h4>
      <dl class="kv">
        <dt>Listing reviews</dt><dd>${num(r.reviews)}${r.rating ? ` · ★ ${r.rating}` : ''}</dd>
        <dt>Shop reviews</dt><dd>${r.shop_reviews ? `${num(r.shop_reviews)}${r.shop_rating ? ` · ★ ${r.shop_rating}` : ''}` : '—'}</dd>
        <dt>Shop</dt><dd>${shopLink(r)}</dd>
      </dl>

      <h4>Favourite history</h4>
      <p class="tiny">${pts.length} reading${pts.length === 1 ? '' : 's'}${pts.length ? ` · ${pts[0].captured_at ? new Date(pts[0].captured_at).toLocaleDateString() : ''} → today` : ''}</p>
      <p style="margin:8px 0 0">${pts.length ? sparkline(pts.map((p) => p.favorites)) : '<span class="delta">not enough history yet</span>'}</p>

      <p style="margin-top:22px"><a class="btn" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open on Etsy ↗</a></p>
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
on('drawer-close', 'click', closeDrawer);
on('scrim', 'click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  closeDrawer();
  [['acct', 'acct-scrim'], ['pw', 'pw-scrim']].forEach(([id, sc]) => {
    const m = $(id);
    if (m && !m.hidden) {
      m.hidden = true;
      const s2 = $(sc); if (s2) s2.hidden = true;
      // focus must go back to the control that opened the dialog
      const back = id === 'acct' ? $('acct-btn') : $('acct-pw');
      if (back) back.focus();
    }
  });
});

/* ── market: one row per keyword, priced and scored ──────────────────────
 * The decision this table supports: is there money here, and is the field
 * weak enough to enter? It is the only composite view on the page, which is
 * why the listings stay below it in plain, unfiltered form. */
function shareBar(v) {
  if (v === null || v === undefined) return '<span class="delta">—</span>';
  const pct = Math.max(0, Math.min(100, Math.round(Number(v) * 100)));
  // High is good when it means "field is weak" — pass the tone in, not a colour.
  const tone = pct >= 60 ? ' high' : pct <= 25 ? ' low' : '';
  return `<span class="share${tone}"><i><b style="width:${pct}%"></b></i><span class="pct">${pct}%</span></span>`;
}

const statusWord = (s) => (s === 'running' ? 'crawling'
  : s === 'paused' ? 'paused' : s === 'blocked' ? 'parked' : s ? 'scheduled' : 'not scheduled');

function marketRow(m) {
  const d = m.d || {}, k = m.k || {};
  return `<tr>
    <td data-label="Keyword"><b>${esc(m.name)}</b>${k.status === 'paused' || k.status === 'blocked' ? ` <span class="tag quiet">${statusWord(k.status)}</span>` : ''}</td>
    <td data-label="Listings seen">${d.listings != null ? num(d.listings) : '—'}</td>
    <td data-label="Median price"><b>${money(d.median_price)}</b></td>
    <td data-label="Top 10% favourites">${num(d.p90_fav)}</td>
    <td data-label="Weak sellers">${shareBar(d.share_low_reviews)}</td>
    <td data-label="Listed ≤ 90 days">${shareBar(d.share_listed_90d)}</td>
    <td data-label="Next check">${dueLabel(k)}</td>
  </tr>`;
}

async function loadMarket() {
  const [daily, kws] = await Promise.all([
    rest('keyword_daily?select=keyword,day,listings,median_price,p90_fav,share_low_reviews,share_listed_90d,shops&order=day.desc&limit=120'),
    rest('keywords?select=keyword,depth,next_page,pass_no,status,pages_per_run,interval_hours,due_at,listings_seen,last_run_at&order=added_at.asc'),
  ]);
  const latest = {};
  (daily.data || []).forEach((d) => { if (!latest[d.keyword]) latest[d.keyword] = d; });
  const schedule = {};
  (kws.data || []).forEach((k) => { schedule[k.keyword] = k; });
  const rows = [...new Set([...Object.keys(schedule), ...Object.keys(latest)])]
    .map((name) => ({ name, d: latest[name] || null, k: schedule[name] || null }))
    .sort((a, b) => (b.d?.listings || 0) - (a.d?.listings || 0));
  $('market-body').innerHTML = rows.map(marketRow).join('');
  $('market-empty').hidden = rows.length > 0;
}

/* ── signals ───────────────────────────────────────────────────────────
 * Everything on this board comes from columns the crawler already stores:
 * listings.listing_date, snapshots.favorites, listing_keywords.best_rank and
 * listings.is_ad. No extra page loads — which matters, because a page load is
 * the one thing here that costs a captcha.
 */
const SIG = { kw: '', entry: [], cohorts: [], curve: [], winners: [], phrases: [], observed: {}, moverSource: 'age' };

const COHORTS = ['0-7d', '8-30d', '31-90d', '90d+'];
const COHORT_LABEL = { '0-7d': '0–7 days', '8-30d': '8–30 days', '31-90d': '31–90 days', '90d+': '90+ days' };
const perDay = (v) => (v === null || v === undefined ? '—'
  : Number(v) >= 100 ? num(Math.round(Number(v))) : Number(v).toFixed(1));

async function loadSignals() {
  const [entry, cohorts, winners, curve, phrases] = await Promise.all([
    rest('keyword_entry_mv?select=*&order=entry_score.desc'),
    rest('age_cohorts?select=*'),
    rest('new_winners?select=keyword,etsy_id,title,shop_name,price_usd,favorites,age_days,fav_per_day,best_rank,is_ad,seller_reviews&order=fav_per_day.desc&limit=300'),
    rest('launch_curve?select=*&order=week.asc'),
    rest('phrase_stats_mv?select=*&order=median_favorites.desc&limit=800'),
  ]);
  SIG.entry = entry.data || [];
  SIG.cohorts = cohorts.data || [];
  SIG.winners = winners.data || [];
  SIG.curve = curve.data || [];
  SIG.phrases = phrases.data || [];
  if (!SIG.kw || !SIG.entry.some((e) => e.keyword === SIG.kw)) {
    // The board exists to answer questions about these two; open on the main one
    // when it has data, rather than on whichever keyword happens to score highest.
    const wanted = SIG.entry.find((e) => e.keyword === 'tshirt');
    SIG.kw = wanted?.keyword || SIG.entry[0]?.keyword || '';
  }
  const sel = $('sig-kw');
  sel.innerHTML = SIG.entry.map((e) => `<option value="${esc(e.keyword)}">${esc(e.keyword)}</option>`).join('');
  sel.value = SIG.kw;
  renderSignals();
  loadObservedMovers(SIG.kw).then(renderSignals).catch(() => {});
}

function renderSignals() {
  const kw = SIG.kw;

  /* 1. entry score — one row per keyword */
  $('entry-body').innerHTML = SIG.entry.map((e) => {
    const score = Math.max(0, Math.min(100, Math.round(Number(e.entry_score) || 0)));
    const tone = score >= 60 ? ' high' : score <= 35 ? ' low' : '';
    return `<tr class="${e.keyword === kw ? 'focus-row' : ''}">
      <td data-label="Keyword"><b>${esc(e.keyword)}</b></td>
      <td data-label="Entry score"><span class="score${tone}"><i><b style="width:${score}%"></b></i><span class="pct">${score}</span></span></td>
      <td data-label="Sample">${num(e.listings)}</td>
      <td data-label="Median price">${money(e.median_price)}</td>
      <td data-label="Top 10% favourites">${num(e.p90_fav)}</td>
      <td data-label="Weak sellers">${shareBar(e.weak_seller_share)}</td>
      <td data-label="New in top 30">${shareBar((Number(e.pct_new_in_top30) || 0) / 100)}</td>
      <td data-label="Shop HHI">${e.shop_hhi == null ? '—' : Number(e.shop_hhi).toFixed(3)}</td>
    </tr>`;
  }).join('');

  /* 2. age cohorts for the focus keyword */
  const rows = COHORTS.map((c) => SIG.cohorts.find((r) => r.keyword === kw && r.cohort === c)).filter(Boolean);
  $('cohort-body').innerHTML = rows.length ? rows.map((r) => `<tr>
      <td data-label="Age">${esc(COHORT_LABEL[r.cohort] || r.cohort)}</td>
      <td data-label="Top-20 share">${shareBar((Number(r.pct_in_top20) || 0) / 100)}</td>
      <td data-label="Median favourites/day">${perDay(r.median_fav_per_day)}</td>
      <td data-label="Listings">${num(r.listings)}</td>
    </tr>`).join('')
    : '<tr><td colspan="4" class="tiny">No age data yet — this fills in as crawls land.</td></tr>';

  /* 3. launch curve — bars scaled to the peak week */
  const curve = SIG.curve.filter((r) => r.keyword === kw).slice(-14);
  // One viral week (thousands of favourites a day) would flatten every other bar
  // to a line, so the scale ignores the single highest week and clips it instead.
  const vals = curve.map((r) => Number(r.median_fav_per_day) || 0).sort((a, b) => a - b);
  const peak = Math.max(1, vals.length > 2 ? vals[vals.length - 2] : (vals[vals.length - 1] || 1));
  $('curve-body').innerHTML = curve.length ? curve.map((r) => {
    const h = Math.min(100, Math.max(2, Math.round(((Number(r.median_fav_per_day) || 0) / peak) * 100)));
    const label = new Date(r.week).toLocaleDateString([], { month: 'short', day: 'numeric' });
    return `<div class="curve-col" title="${label} · ${perDay(r.median_fav_per_day)} favourites/day · ${r.listings} listings">
      <b class="curve-val">${perDay(r.median_fav_per_day)}</b>
      <span class="curve-bar"><i style="height:${h}%"></i></span>
      <span class="curve-week">${label}</span>
    </div>`;
  }).join('') : '<p class="tiny">Not enough dated listings yet.</p>';

  /* 4. fastest movers — published-date ranking when we have dates, otherwise the
   *    honest fallback: favourites gained per day since we started reading the
   *    listing. The two are labelled differently on purpose; they are not the
   *    same measurement and must never look like it. */
  const byAge = SIG.winners.filter((w) => w.keyword === kw).slice(0, 8);
  const obs = SIG.observed[kw] || [];
  const moverRows = byAge.length ? byAge.map((w) => ({
    etsy_id: w.etsy_id, title: w.title, shop: w.shop_name, price: w.price_usd,
    favourites: w.favorites, rank: w.best_rank, rate: w.fav_per_day,
    note: w.age_days == null ? 'age unknown' : `${num(w.age_days)} days old`, exact: true,
  })) : obs.slice(0, 8).map((o) => ({
    etsy_id: o.etsy_id, title: o.title, shop: o.shop_name, price: o.price_usd,
    favourites: o.favorites, rank: o.best_rank, rate: o.fav_per_day_observed,
    note: `watched ${Number(o.tracked_days || 0).toFixed(1)} days`, exact: false,
  }));
  $('movers-body').innerHTML = moverRows.length ? moverRows.map((w, i) => `<li class="mover">
      <span class="rank">${i + 1}</span>
      <div class="mover-main">
        <a href="https://www.etsy.com/listing/${esc(w.etsy_id)}" target="_blank" rel="noopener">${esc(w.title || '(no title)')}</a>
        <div class="mover-meta">
          ${w.shop ? `<span>${esc(w.shop)}</span>` : ''}
          <span>${esc(w.note)}</span>
          <span>rank ${w.rank ?? '—'}</span>
          <span>${money(w.price)}</span>
          <span>${num(w.favourites)} favourites</span>
        </div>
      </div>
      <div class="mover-rate"><b>${perDay(w.rate)}</b><span>${w.exact ? 'fav / day' : 'fav / day watched'}</span></div>
    </li>`).join('')
    : '<li class="tiny">Nothing measured for this keyword yet.</li>';

  /* 5. title phrases */
  const phrases = SIG.phrases.filter((p) => p.keyword === kw && Number(p.listings) >= 3).slice(0, 14);
  $('phrase-body').innerHTML = phrases.length ? phrases.map((p) => `<tr>
      <td data-label="Phrase"><b>${esc(p.phrase)}</b>${Number(p.words) === 2 ? ' <span class="tag quiet">2 words</span>' : ''}</td>
      <td data-label="Listings">${num(p.listings)}</td>
      <td data-label="Median favourites">${num(p.median_favorites)}</td>
      <td data-label="Top 10%">${num(p.p90_favorites)}</td>
      <td data-label="Median price">${money(p.median_price)}</td>
      <td data-label="New (30 d)">${shareBar((Number(p.pct_listed_last_30d) || 0) / 100)}</td>
    </tr>`).join('')
    : '<tr><td colspan="6" class="tiny">No phrase data yet.</td></tr>';
}

async function loadObservedMovers(kw) {
  if (!kw || SIG.observed[kw]) return;
  try {
    const links = await rest(`listing_keywords?select=etsy_id&keyword=eq.${encodeURIComponent(kw)}&limit=300`);
    const ids = (links.data || []).map((r) => r.etsy_id).filter(Boolean);
    if (!ids.length) { SIG.observed[kw] = []; return; }
    const q = ids.map((i) => `"${i}"`).join(',');
    const stats = await rest('listing_stats?select=etsy_id,title,shop_name,price_usd,favorites,best_rank,fav_per_day_observed,tracked_days'
      + `&etsy_id=in.(${encodeURIComponent(q)})&fav_per_day_observed=not.is.null`
      + '&order=fav_per_day_observed.desc&limit=60');
    SIG.observed[kw] = stats.data || [];
  } catch (e) {
    SIG.observed[kw] = [];      // the panel degrades to the age-based list
  }
}

on('sig-kw', 'change', async () => {
  SIG.kw = $('sig-kw').value;
  renderSignals();               // instant: age-based or empty
  await loadObservedMovers(SIG.kw);
  renderSignals();               // then the observed fallback, if it adds anything
});

/* ── design briefs ─────────────────────────────────────────────────────
 * The reverse-engineering half of the board: group the shirts that are
 * already selling into niches, score demand against saturation, and put the
 * winning designs on screen so the next design decision is visual, not
 * statistical.
 */
/* ── design library ────────────────────────────────────────────────────────
 * Filled by scripts/design_dna.py: OCR of the artwork gives the phrase, pixel
 * statistics give composition, background and palette. Nothing here is inferred
 * by the browser — it renders what the DNA pass measured, and says "not read"
 * where the pass found nothing rather than filling the gap. */
const LIB = { rows: [], patterns: [], kw: 'tshirt', niche: 'all', bg: 'all', only: 'all' };

async function loadLibrary() {
  const [lib, pat] = await Promise.all([
    rest('design_library?select=*&order=demand.desc&limit=600'),
    rest('design_patterns?select=*&order=listings.desc&limit=80'),
  ]);
  LIB.rows = lib.data || [];
  LIB.patterns = pat.data || [];

  const kws = [...new Set(LIB.rows.map((r) => r.keyword).filter(Boolean))];
  if (!kws.length) {
    $('lib-count').textContent = 'No design has been read yet — the DNA pass has not run.';
    return;
  }
  if (!LIB.wired) {
    LIB.wired = true;
    const bind = (id, key) => $(id).addEventListener('change', (e) => { LIB[key] = e.target.value; renderLibrary(); });
    bind('lib-kw', 'kw'); bind('lib-niche', 'niche'); bind('lib-bg', 'bg'); bind('lib-only', 'only');
    $('lib-csv').addEventListener('click', libCsv);
  }
  if (!kws.includes(LIB.kw)) LIB.kw = kws.includes('tshirt') ? 'tshirt' : kws[0];
  $('lib-kw').innerHTML = kws.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
  $('lib-kw').value = LIB.kw;
  renderLibrary();
}

function libRows() {
  let rows = LIB.rows.filter((r) => r.keyword === LIB.kw);
  if (LIB.niche !== 'all') rows = rows.filter((r) => r.niche === LIB.niche);
  if (LIB.bg !== 'all') rows = rows.filter((r) => r.design_bg === LIB.bg);
  if (LIB.only === 'text') rows = rows.filter((r) => r.design_text);
  if (LIB.only === 'proven') rows = rows.filter((r) => r.is_bestseller || r.is_star_seller);
  // A design you can read is worth more than one you cannot: rank legible phrases
  // first, then demand. Reading a wall of unreadable mockups is how research dies.
  return rows.slice().sort((a, b) =>
    (Boolean(b.design_text) - Boolean(a.design_text)) || (Number(b.demand) - Number(a.demand)));
}

function libCard(r, i) {
  const phrase = r.design_text
    ? `<span class="lib-phrase">${esc(r.design_text)}</span>`
    : '<span class="lib-phrase quiet">no readable text</span>';
  const colours = (r.design_colours || []).slice(0, 3)
    .map((c) => `<i class="swatch" style="background:${esc(c)}" title="${esc(c)}"></i>`).join('');
  const badges = [
    r.is_bestseller ? '<span class="chip hot">bestseller</span>' : '',
    r.is_star_seller ? '<span class="chip">star</span>' : '',
    r.bought_24h ? `<span class="chip hot">${num(r.bought_24h)} bought / 24h</span>` : '',
    r.carts ? `<span class="chip">${num(r.carts)} in carts</span>` : '',
  ].join('');
  return `<a class="lib-card" href="https://www.etsy.com/listing/${esc(r.etsy_id)}" target="_blank" rel="noopener">
    <span class="lib-img"><img loading="lazy" src="${esc(thumb(r.image_url))}" alt="">
      <span class="lib-rank">${i + 1}</span></span>
    <span class="lib-body">
      ${phrase}
      <span class="lib-title">${esc(r.title || '')}</span>
      <span class="lib-meta">
        <span class="lib-demand"><b>${num(r.demand)}</b> demand</span>
        <span>${num(r.favorites)} fav</span>
        <span>${money(r.price_usd)}</span>
      </span>
      <span class="lib-tags">
        ${r.niche ? `<span class="chip">${esc(r.niche)}</span>` : ''}
        ${r.design_layout ? `<span class="chip quiet">${esc(String(r.design_layout).replace('_', '+'))}</span>` : ''}
        ${r.design_bg ? `<span class="chip quiet">${esc(r.design_bg)}</span>` : ''}
        <span class="lib-colours">${colours}</span>
      </span>
      <span class="lib-badges">${badges}</span>
    </span>
  </a>`;
}

function renderLibrary() {
  const rows = libRows();

  const niches = [...new Set(LIB.rows.filter((r) => r.keyword === LIB.kw).map((r) => r.niche).filter(Boolean))].sort();
  const cur = niches.includes(LIB.niche) ? LIB.niche : 'all';
  LIB.niche = cur;
  $('lib-niche').innerHTML = `<option value="all">Any niche (${LIB.rows.filter((r) => r.keyword === LIB.kw).length})</option>`
    + niches.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  $('lib-niche').value = cur;

  const read = LIB.rows.filter((r) => r.keyword === LIB.kw);
  const withText = read.filter((r) => r.design_text).length;
  $('lib-count').textContent = `${num(rows.length)} designs shown · ${num(read.length)} read for `
    + `“${LIB.kw}” · ${withText} with a legible phrase (${Math.round(100 * withText / Math.max(1, read.length))}%)`;

  const shown = rows.slice(0, 120);
  $('lib-grid').innerHTML = shown.length
    ? shown.map(libCard).join('')
    : '<p class="tiny">Nothing matches these filters yet.</p>';
  if (rows.length > shown.length) {
    $('lib-count').textContent += ` · showing the top ${shown.length} by demand`;
  }

  const pats = LIB.patterns.slice(0, 40);
  $('pat-body').innerHTML = pats.length
    ? pats.map((p) => `<tr>
        <td data-label="Phrase"><b>${esc(p.phrase_norm)}</b></td>
        <td data-label="Shirts">${num(p.listings)}</td>
        <td data-label="Shops">${num(p.shops)}</td>
        <td data-label="Demand">${num(p.demand_sum)}</td>
        <td data-label="Niches">${esc((p.niches || []).join(', ') || '—')}</td>
        <td data-label="Median price">${money(p.price_median)}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="tiny">No phrase has appeared on two read designs yet — the DNA pass is still filling.</td></tr>';
}

/* CSV straight from the browser: the library is the deliverable, so it must be
 * liftable into a spreadsheet without going back through me. */
function libCsv() {
  const rows = libRows();
  const cols = ['etsy_id', 'title', 'shop_name', 'niche', 'demand', 'favorites', 'carts',
                'bought_24h', 'price_usd', 'design_text', 'phrase_norm', 'design_layout',
                'design_bg', 'design_colours', 'age_days', 'is_bestseller', 'is_star_seller', 'image_url'];
  const cell = (v) => {
    if (v == null) return '';
    const s = Array.isArray(v) ? v.join(' ') : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(',')].concat(rows.map((r) => cols.map((c) => cell(r[c])).join(','))).join('\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `designs-${LIB.kw.replace(/\s+/g, '-')}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

const BRIEFS = { kw: '__cases', rows: [], examples: [], production: [], niche: '' };

// A request that is allowed to fail without taking the panel down with it.
async function softRest(path, tries = 1) {
  let last;
  for (let i = 0; i < tries; i++) {
    try { return (await rest(path)).data || []; }
    catch (e) { last = e; if (i < tries - 1) await new Promise((r) => setTimeout(r, 600 * (i + 1))); }
  }
  if (window.console) console.warn('[soft]', path.split('?')[0], last && last.message);
  return [];
}

async function loadBriefs() {
  // Two waves, not one. The shirt briefs are the panel's reason to exist, so they go
  // first; the case trio follows in a second wave with retries. Firing all six at once
  // on a cold connection lost a request often enough that the panel silently fell back
  // to the shirt keywords.
  const [briefs, examples, production] = await Promise.all([
    rest('design_briefs?select=*&order=opportunity.desc'),
    rest('niche_examples?select=*&limit=900'),
    rest('production_stats?select=*&order=listings.desc'),
  ]);
  const [cBriefs, cExamples, cProduction] = await Promise.all([
    softRest('case_briefs?select=*&order=opportunity.desc', 3),
    softRest('case_examples?select=*&limit=900', 3),
    softRest('case_production?select=*,pct_of_shirts:pct_of_cases&order=listings.desc', 3),
  ]);
  BRIEFS.rows = (briefs.data || []).concat(cBriefs);
  BRIEFS.examples = (examples.data || []).concat(cExamples);
  BRIEFS.production = (production.data || []).concat(cProduction);
  if (!cBriefs.length) console.warn('[briefs] phone-case briefs unavailable this load');
  renderBriefs();
}

function renderBriefs() {
  const rows = BRIEFS.rows.filter((r) => r.keyword === BRIEFS.kw);
  if (!rows.some((r) => r.niche === BRIEFS.niche)) BRIEFS.niche = rows[0]?.niche || '';
  $('brief-body').innerHTML = rows.length
    ? rows.map(briefRow).join('')
    : '<tr><td colspan="8" class="tiny">No niche crossed the 8-listing floor for this keyword.</td></tr>';

  const label = (rows.find((r) => r.niche === BRIEFS.niche) || {}).label || BRIEFS.niche;
  const head = $('designs-head');
  if (head) head.textContent = `Designs in “${label || 'this niche'}”`;

  const ex = BRIEFS.examples.filter((e) => e.keyword === BRIEFS.kw && e.niche === BRIEFS.niche).slice(0, 6);
  $('design-grid').innerHTML = ex.length
    ? ex.map(designCard).join('')
    : '<p class="tiny">No thumbnails stored for this niche yet.</p>';

  const prod = BRIEFS.production.filter((p) => p.keyword === BRIEFS.kw);
  $('production-body').innerHTML = prod.length
    ? prod.map((p) => `<tr>
        <td data-label="Blank / method"><b>${esc(p.label)}</b></td>
        <td data-label="Share of shirts">${shareBar((Number(p.pct_of_shirts) || 0) / 100)}</td>
        <td data-label="Median favourites">${num(p.median_fav)}</td>
      </tr>`).join('')
    : '<tr><td colspan="3" class="tiny">No descriptions captured for this keyword yet.</td></tr>';
}

on('db-kw', 'change', () => { BRIEFS.kw = $('db-kw').value; BRIEFS.niche = ''; renderBriefs(); });

/* Row click selects that niche. Delegated, because the rows are re-rendered. */
on('brief-body', 'click', (ev) => {
  const tr = ev.target.closest('tr[data-niche]');
  if (!tr) return;
  BRIEFS.niche = tr.getAttribute('data-niche');
  renderBriefs();
  $('designs-head')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
});
on('brief-body', 'keydown', (ev) => {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const tr = ev.target.closest('tr[data-niche]');
  if (!tr) return;
  ev.preventDefault();
  BRIEFS.niche = tr.getAttribute('data-niche');
  renderBriefs();
});

/* ── runs ────────────────────────────────────────────────────────────── */
async function loadRuns() {
  try {
    const { data } = await rest('runs?select=started_at,keyword,worker,page_from,page_to,status,listings,challenges,stopped_reason&order=started_at.desc&limit=12');
    $('runs').innerHTML = (data || []).map((r) => {
      const ok = r.status === 'succeeded';
      const why = r.stopped_reason ? ` title="stopped: ${esc(r.stopped_reason)}"` : '';
      const when = r.started_at
        ? new Date(r.started_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        : '—';
      return `<tr>
        <td>${when}</td>
        <td><b>${esc(r.keyword || '—')}</b>${r.worker ? ` <span class="tag quiet">${esc(r.worker)}</span>` : ''}</td>
        <td>${r.page_from ?? '—'}–${r.page_to ?? '—'}</td>
        <td><span class="pill ${ok ? 'ok' : 'bad'}"${why}>${esc(r.status || '—')}</span></td>
        <td>${num(r.listings)}</td>
        <td>${num(r.challenges)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="6" class="tiny">No runs yet.</td></tr>';
  } catch (e) { /* runs table is decoration; never break the page for it */ }
}

/* ── boot ────────────────────────────────────────────────────────────── */

on('gate-form', 'submit', submitGate);
window.__gateReady = true;   // tells the inline fallback in index.html that the app is live

on('pw-cancel', 'click', () => { $('pw').hidden = true; $('pw-scrim').hidden = true; });
on('pw-scrim', 'click', () => { $('pw').hidden = true; $('pw-scrim').hidden = true; });
on('pw-save', 'click', async () => {
  const a = $('pw-new').value, b = $('pw-new2').value;
  const msg = $('pw-msg');
  if (a.length < 6) { msg.textContent = 'use at least 6 characters'; return; }
  if (a !== b) { msg.textContent = 'the two passwords do not match'; return; }
  const btn = $('pw-save');
  btn.disabled = true; btn.textContent = 'saving…';
  try {
    await changePassword(a);
    msg.textContent = 'password changed — use it next time you sign in';
    $('pw-new').value = ''; $('pw-new2').value = '';
  } catch (e) {
    msg.textContent = `could not change: ${e.message}`;
  } finally {
    btn.disabled = false; btn.textContent = 'Save password';
  }
});

on('gate-send', 'click', async () => {
  const email = ($('gate-email').value || '').trim().toLowerCase();
  const btn = $('gate-send');
  btn.disabled = true;
  btn.textContent = 'sending…';
  try {
    await signIn(email);
    gateStatus(`Link sent to ${email}. You can also use the code in that email.`, false);
    btn.textContent = 'sent ✓';
  } catch (e) {
    gateStatus(e.status === 429
      ? 'the hourly email limit is used up — use your password instead, or try later'
      : `could not send: ${e.message}`, true);
    btn.textContent = 'Email me a link instead';
  } finally {
    btn.disabled = false;
  }
});
on('gate-verify', 'click', async () => {
  const btn = $('gate-verify');
  const raw = $('gate-paste').value;
  if (!raw.trim()) return;
  btn.disabled = true;
  btn.textContent = 'checking…';
  try {
    await verifyPasted(raw);
    gateStatus('signed in', false);
    showGate(false);
    $('acct-btn').textContent = (SESSION.email || OWNER_EMAIL).split('@')[0];
    loadListings().catch(fail);
    loadStats().catch(fail);
    loadQueue().catch(fail);
    loadUsage().catch(fail);
    loadRuns();
  } catch (e) {
    const m = /rate limit|429/i.test(e.message || '')
      ? 'the email limit for this hour is used up — try again in a little while, or use the link from an earlier email'
      : e.message;
    gateStatus(`Could not sign in: ${m}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in with this';
  }
});


on('acct-btn', 'click', () => {
  const who = (SESSION && SESSION.email) || OWNER_EMAIL;
  const txt = $('acct-who');
  if (txt) txt.textContent = who;
  if ($('acct')) {
    $('acct').hidden = false;
    $('acct-scrim').hidden = false;
    // a dialog nobody can reach with the keyboard is a dead end
    setTimeout(() => { const b = $('acct-close'); if (b) b.focus(); }, 30);
  }
});

on('acct-close', 'click', () => { if ($('acct')) { $('acct').hidden = true; $('acct-scrim').hidden = true; } });
on('acct-scrim', 'click', () => { if ($('acct')) { $('acct').hidden = true; $('acct-scrim').hidden = true; } });
on('acct-pw', 'click', () => { if ($('acct')) { $('acct').hidden = true; $('acct-scrim').hidden = true; } openPasswordDialog(); });
on('acct-out', 'click', async () => {
  if ($('acct')) { $('acct').hidden = true; $('acct-scrim').hidden = true; }
  await signOut();
});

async function changePassword(next) {
  if (!SESSION) throw new Error('not signed in');
  const res = await fetch(`${BASE}/auth/v1/user`, {
    method: 'PUT',
    headers: { apikey: ANON, Authorization: `Bearer ${SESSION.access_token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: next }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.msg || data.error_description || `auth ${res.status}`);
  return true;
}

function openPasswordDialog() {
  const dlg = $('pw');
  const scrim = $('pw-scrim');
  if (!dlg) return;
  $('pw-msg').textContent = '';
  $('pw-new').value = '';
  $('pw-new2').value = '';
  dlg.hidden = false;
  scrim.hidden = false;
  $('pw-new').focus();
}

window.addEventListener('hashchange', async () => {
  if (!absorbLinkSession()) return;
  showGate(false);
  Promise.all([loadQueue(), loadStats(), loadUsage()]).catch(fail);
  loadListings().catch(fail);
});

(async function boot() {
  try {
  // stamp first: if someone is stuck on the gate, this is the one fact that
  // distinguishes "old cached bundle" from "new bundle, real error".
  const stampEl = document.getElementById('build-tag');
  if (stampEl) stampEl.textContent = BUILD;
  if (!BASE || !ANON) {
    document.body.innerHTML = '<p style="padding:40px;font:15px system-ui">Missing <code>web/config.js</code> — it must define supabaseUrl and anonKey.</p>';
    return;
  }
  loadSession();
  const freshLink = absorbLinkSession();
  if (!(await ensureSession())) {
    showGate(true);
    if (!document.getElementById('gate-msg').innerText) {
      gateStatus('not signed in in this browser yet — enter the email and password from your setup email.', false);
    }
    return;
  }
  if (freshLink) {
    // A link can carry a token the server no longer honours (already rotated,
    // revoked, or simply malformed). Say so instead of showing a blank gate.
    try {
      await rest('rpc/page_budget', { method: 'POST', body: {} });
    } catch (e) {
      // The probe failed: either the token is not honoured or the network dropped.
      // A stale session is useless either way, so clear it and say what happened.
      clearSession();
      showGate(true);
      const why = /not signed in/i.test(e.message || '') ? 'the session was rejected' : (e.message || 'it could not be verified');
      gateStatus(`That sign-in link did not work — ${why}. Send yourself a fresh one.`, true);
      return;
    }
  }
  showGate(false);
  if ($('acct-btn')) {
    $('acct-btn').textContent = (SESSION.email || OWNER_EMAIL).split('@')[0];
    $('acct-btn').title = `signed in as ${SESSION.email || OWNER_EMAIL}`;
  }
  if ($('kw-interval')) fillIntervals($('kw-interval'), 2);
  if ($('bulk-interval')) fillIntervals($('bulk-interval'), 2);
  renderExplainers();

  document.querySelectorAll('[data-help]').forEach((b) => {
    b.addEventListener('click', (ev) => {
      ev.preventDefault();
      const el = $(`explain-${b.dataset.help}`);
      el.open = true;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  });

  startBoard();
  setInterval(loadStats, 60000);
  setInterval(() => { loadQueue().catch(() => {}); loadUsage().catch(() => {}); }, 60000);
  } catch (e) {
    // never leave the page in neither-state: show the gate with the real reason
    showGate(true);
    gateStatus(`the page failed to start: ${e.message} (build ${BUILD})`, true);
    showBanner(`startup error: ${e.message}`, true);
    throw e;
  }
})();
