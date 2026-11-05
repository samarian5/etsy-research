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

/* ── session ─────────────────────────────────────────────────────────── */
/* The board is private now: every read and every write carries the owner's
 * session token, and the database refuses anything else. Sign-in is a magic
 * link to the single permitted address. */

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
  ['s-listings', 's-snaps', 's-queue', 's-seen'].forEach(id => {
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

async function submitGate(ev) {
  ev.preventDefault();
  const email = ($('gate-email').value || '').trim().toLowerCase();
  const btn = $('gate-send');
  if (!email) return;
  btn.disabled = true;
  btn.textContent = 'sending…';
  gateStatus('', false);
  try {
    await signIn(email);
    gateStatus(`Link sent to ${email}. Open it on this device — it signs you in and brings you straight back here.`, false);
    btn.textContent = 'sent ✓';
  } catch (e) {
    const msg = e.status === 429
      ? 'Too many sign-in emails in the last hour (the free tier allows two). Wait a bit, or use the link already in your inbox.'
      : e.status === 422
        ? `${email} is not the account this board belongs to.`
        : `Could not send the link: ${e.message}`;
    gateStatus(msg, true);
    btn.textContent = 'Sign in';
  } finally {
    btn.disabled = false;
    if (btn.textContent === 'sending…') btn.textContent = 'Sign in';
  }
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
    $('confirm-cancel').addEventListener('click', no);
    scrim.addEventListener('click', no);
    document.addEventListener('keydown', key);
  });
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
    .map((k) => `<span class="next-chip">${esc(k.keyword)} <b>${dueLabel(k)}</b></span>`)
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

$('queue-body').addEventListener('change', async (ev) => {
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

$('queue-body').addEventListener('click', async (ev) => {
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

$('check-all').addEventListener('change', (e) => {
  document.querySelectorAll('#queue-body .pick').forEach((c) => { c.checked = e.target.checked; });
  syncBulk();
});
$('bulk-clear').addEventListener('click', () => {
  document.querySelectorAll('#queue-body .pick').forEach((c) => { c.checked = false; });
  syncBulk();
});
$('bulk-apply').addEventListener('click', async () => {
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

$('kw-form').addEventListener('submit', async (ev) => {
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

$('budget-save').addEventListener('click', async () => {
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

$('kw-depth').addEventListener('input', renderExplainers);
$('kw-interval').addEventListener('change', renderExplainers);

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

$('gate-form').addEventListener('submit', submitGate);
$('gate-verify').addEventListener('click', async () => {
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


$('acct-btn').addEventListener('click', async () => {
  const who = (SESSION && SESSION.email) || OWNER_EMAIL;
  const ok = await confirmDialog('Sign out?', `This browser is signed in as ${who}. Signing out hides the board until you open a new sign-in link.`, 'Sign out', true);
  if (ok) await signOut();
});

window.addEventListener('hashchange', async () => {
  if (!absorbLinkSession()) return;
  showGate(false);
  Promise.all([loadQueue(), loadStats(), loadUsage()]).catch(fail);
  loadListings().catch(fail);
});

(async function boot() {
  if (!BASE || !ANON) {
    document.body.innerHTML = '<p style="padding:40px;font:15px system-ui">Missing <code>web/config.js</code> — it must define supabaseUrl and anonKey.</p>';
    return;
  }
  loadSession();
  const freshLink = absorbLinkSession();
  if (!(await ensureSession())) {
    showGate(true);
    if (!document.getElementById('gate-msg').innerText) {
      gateStatus('not signed in in this browser yet — use the code from the email, or send a link.', false);
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
  $('acct-btn').textContent = (SESSION.email || OWNER_EMAIL).split('@')[0];
  $('acct-btn').title = `signed in as ${SESSION.email || OWNER_EMAIL}`;
  fillIntervals($('kw-interval'), 2);
  fillIntervals($('bulk-interval'), 2);
  renderExplainers();

  document.querySelectorAll('[data-help]').forEach((b) => {
    b.addEventListener('click', (ev) => {
      ev.preventDefault();
      const el = $(`explain-${b.dataset.help}`);
      el.open = true;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  });

  loadListings().catch(fail);
  loadStats().catch(fail);
  loadQueue().catch(fail);
  loadUsage().catch(fail);
  loadRuns();
  setInterval(loadStats, 60000);
  setInterval(() => { loadQueue().catch(() => {}); loadUsage().catch(() => {}); }, 60000);
})();
