// picks.js — shared, local-only pick tracker used across every page.
// Everything lives in this browser's localStorage; nothing is sent anywhere,
// so your picks are private to whichever device/browser you log them on.
const Picks = (() => {
  const KEY = 'projex_picks_v1';

  function all(){
    try { return JSON.parse(localStorage.getItem(KEY)) || []; }
    catch(e){ return []; }
  }
  function save(list){
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch(e){}
  }
  function log(pick){
    // pick: { sport, label, detail, line, logo, key }
    const list = all();
    const entry = Object.assign({
      id: 'p_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
      loggedAt: new Date().toISOString(),
      result: null, // null = pending, 'win' | 'loss' | 'push'
    }, pick);
    list.unshift(entry);
    save(list);
    notify();
    return entry;
  }
  function setResult(id, result){
    const list = all();
    const p = list.find(x => x.id === id);
    if (p){ p.result = result; p.settledAt = new Date().toISOString(); save(list); notify(); }
    return p;
  }
  function remove(id){
    save(all().filter(x => x.id !== id));
    notify();
  }
  function clearAll(){
    save([]);
    notify();
  }
  function stats(list){
    const settled = list.filter(p => p.result === 'win' || p.result === 'loss');
    const wins = settled.filter(p => p.result === 'win').length;
    const losses = settled.length - wins;
    return { wins, losses, total: settled.length, pct: settled.length ? Math.round(wins / settled.length * 100) : null };
  }

  // ── key helpers: let a rendered button know whether it's already on the slip ──
  function hasKey(key){ return !!key && all().some(p => p.key === key); }
  function removeByKey(key){
    save(all().filter(p => p.key !== key));
    notify();
  }
  function count(){ return all().length; }
  function pending(){ return all().filter(p => p.result === null).length; }

  // Anything that cares about slip changes (the floating bar, button states)
  // subscribes here instead of polling localStorage.
  const subs = [];
  function onChange(fn){ subs.push(fn); }
  function notify(){ subs.forEach(fn => { try { fn(); } catch(e){} }); }

  return { all, log, setResult, remove, clearAll, stats, hasKey, removeByKey, count, pending, onChange, KEY };
})();


// ─────────────────────────────────────────────────────────────────────────────
// Bet-slip buttons
//
// Boards build their markup as HTML strings, so the button is produced the same
// way: call pickBtn(...) inside a template literal and the shared click handler
// below takes care of the rest. Buttons are stateless in the markup — on every
// render, syncPickButtons() re-reads the slip and re-applies the "on" state, so
// a re-render never loses track of what's already been added.
// ─────────────────────────────────────────────────────────────────────────────

// Bumped whenever this file changes, so a deployed page can be checked at a
// glance: open the site and look at the console, or run PICKS_VERSION.
const PICKS_VERSION = 'betslip-v1';
try { console.log('picks.js ' + PICKS_VERSION + ' loaded \u2014 add-to-slip buttons active'); } catch(e){}

const PICK_GREEN = '#93E06E';

// American odds: +108 / -126. Returns '' when a price isn't available.
function fmtOdds(v){
  if (v == null || typeof v !== 'number' || isNaN(v)) return '';
  return v > 0 ? '+' + v : String(v);
}

function pickAttrEsc(s){
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Strip any HTML a caller passed through (team chips, badges) so the slip
// stores clean text rather than markup fragments.
function pickText(s){
  return String(s == null ? '' : s).replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
}

/**
 * Returns the HTML for one add-to-slip button.
 * @param {object} o
 *   sport  — 'mlb' | 'nfl' | 'nhl' | 'nba'
 *   label  — headline of the pick, e.g. "Auston Matthews" or "TOR ML"
 *   detail — supporting line, e.g. "Shots vs BOS"
 *   line   — the number, e.g. "3.4"
 *   logo   — optional image URL shown in the tracker
 *   key    — stable identity for toggling; auto-derived when omitted
 *   text   — button face; defaults to "+"
 *   variant— 'chip' (game cards) or 'mini' (player rows)
 */
function pickBtn(o){
  const key = o.key || [o.sport, o.label, o.detail, o.line].map(pickText).join('|');
  const face = o.text != null ? o.text : '+';
  return `<button type="button" class="pick-btn pick-${o.variant || 'mini'}" data-pick="1"` +
    ` data-pick-key="${pickAttrEsc(key)}"` +
    ` data-pick-sport="${pickAttrEsc(o.sport || '')}"` +
    ` data-pick-label="${pickAttrEsc(pickText(o.label))}"` +
    ` data-pick-detail="${pickAttrEsc(pickText(o.detail))}"` +
    ` data-pick-line="${pickAttrEsc(pickText(o.line))}"` +
    ` data-pick-logo="${pickAttrEsc(o.logo || '')}"` +
    ` data-pick-face="${pickAttrEsc(face)}"` +
    ` aria-label="Add to slip">${face}</button>`;
}

/**
 * Builds the spread and total chips from a game's canonical `_lines` block.
 * Both sides of each market are offered, the way a sportsbook grid would.
 * Returns '' when the slate has no priced lines, so callers can fall back to
 * their model-derived chip instead.
 *
 *   sport, away, home — abbreviations used for chip faces
 *   lines             — the game's `_lines` object (may be null)
 *   gkey              — stable per-game id for toggling
 *   logoFor           — optional abbr -> logo URL function
 */
function lineChips(o){
  const L = o.lines || {};
  const book = L.book ? ' \u00b7 ' + L.book : '';
  const matchup = `${o.away} @ ${o.home}`;
  let html = '';

  if (L.spread != null){
    // `spread` is always the HOME number; the away side is its mirror.
    [['away', o.away, -L.spread, L.spread_away_price],
     ['home', o.home,  L.spread, L.spread_home_price]].forEach(([side, team, pts, price]) => {
      const face = `${team} ${pts > 0 ? '+' : ''}${pts}`;
      const px = fmtOdds(price);
      html += pickBtn({
        sport:o.sport, variant:'chip', text:face, label:face,
        detail:`${matchup} \u00b7 spread`, line: px ? px + book : 'spread' + book,
        logo: o.logoFor ? o.logoFor(team) : '',
        key:`${o.sport}|spread|${side}|${o.gkey}`});
    });
  }

  if (L.total != null){
    [['over', 'Over', L.over_price], ['under', 'Under', L.under_price]].forEach(([side, word, price]) => {
      const face = `${word} ${L.total}`;
      const px = fmtOdds(price);
      html += pickBtn({
        sport:o.sport, variant:'chip', text:face, label:face,
        detail:`${matchup} \u00b7 total`, line: px ? px + book : 'total' + book,
        key:`${o.sport}|total|${side}|${o.gkey}`});
    });
  }
  return html;
}

/** Wraps a set of pickBtn() calls in the footer strip used on game cards. */
function pickStrip(html){
  return `<div class="pick-strip">${html}</div>`;
}

/** Re-applies "already on the slip" state after any render. Safe to over-call. */
function syncPickButtons(root){
  (root || document).querySelectorAll('.pick-btn[data-pick]').forEach(btn => {
    const on = Picks.hasKey(btn.dataset.pickKey);
    btn.classList.toggle('on', on);
    const face = btn.dataset.pickFace || '+';
    btn.textContent = on ? (btn.classList.contains('pick-chip') ? '✓ ' + face.replace(/^\+\s*/, '') : '✓') : face;
    btn.setAttribute('aria-label', on ? 'Remove from slip' : 'Add to slip');
  });
}

// Capture-phase listener: player rows have their own onclick that expands the
// detail panel, and that handler would otherwise fire (and re-render the list)
// before a bubbling listener ever saw the tap. Capturing lets the slip button
// claim the click first and stop it from reaching the card underneath.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.pick-btn[data-pick]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();

  const key = btn.dataset.pickKey;
  if (Picks.hasKey(key)){
    Picks.removeByKey(key);
  } else {
    Picks.log({
      sport:  btn.dataset.pickSport,
      label:  btn.dataset.pickLabel,
      detail: btn.dataset.pickDetail,
      line:   btn.dataset.pickLine,
      logo:   btn.dataset.pickLogo || '',
      key,
    });
    btn.classList.add('pop');
    setTimeout(() => btn.classList.remove('pop'), 260);
  }
  syncPickButtons();
}, true);


// ─────────────────────────────────────────────────────────────────────────────
// Floating slip counter — a persistent link back to the tracker showing how
// many picks are currently saved. Hidden at zero so it never clutters an
// untouched board.
// ─────────────────────────────────────────────────────────────────────────────
function mountSlipBar(href){
  if (document.getElementById('slip-bar')) return;
  const a = document.createElement('a');
  a.id = 'slip-bar';
  a.href = href || './picks/';
  a.innerHTML = `<span class="slip-ico">🧾</span><span class="slip-txt">Slip</span><span class="slip-count">0</span>`;
  document.body.appendChild(a);
  const update = () => {
    const n = Picks.count();
    a.querySelector('.slip-count').textContent = n;
    a.classList.toggle('show', n > 0);
  };
  Picks.onChange(update);
  update();
  mountBuildStamp();
}


// ─────────────────────────────────────────────────────────────────────────────
// Styles are injected here so the four board pages don't each need their own
// copy of the same rules.
// ─────────────────────────────────────────────────────────────────────────────
(function injectPickStyles(){
  const css = `
  .pick-btn{
    font-family:inherit;font-weight:700;cursor:pointer;
    border-radius:99px;line-height:1;white-space:nowrap;
    border:1px solid ${PICK_GREEN}55;background:${PICK_GREEN}1A;color:${PICK_GREEN};
    transition:transform .12s ease,background .12s ease,border-color .12s ease;
    -webkit-tap-highlight-color:transparent;
  }
  .pick-btn:active{transform:scale(.94)}
  .pick-btn.on{background:${PICK_GREEN};border-color:${PICK_GREEN};color:#0B1210}
  .pick-btn.pop{animation:pickPop .26s ease}
  @keyframes pickPop{0%{transform:scale(1)}45%{transform:scale(1.22)}100%{transform:scale(1)}}

  /* player rows */
  .pick-mini{
    display:inline-flex;align-items:center;justify-content:center;
    width:26px;height:26px;font-size:14px;margin-top:6px;padding:0;
  }

  /* game cards */
  .pick-strip{
    display:flex;gap:6px;flex-wrap:wrap;align-items:center;
    padding:9px 12px 11px;margin-top:2px;
    border-top:1px solid rgba(255,255,255,.07);
  }
  .pick-strip .pick-slip-lab{
    font-size:8.5px;font-weight:800;letter-spacing:.09em;
    color:rgba(255,255,255,.34);margin-right:1px;text-transform:uppercase;
  }
  .pick-chip{font-size:11px;padding:6px 11px;letter-spacing:.01em}

  /* floating counter */
  #slip-bar{
    position:fixed;right:14px;bottom:16px;z-index:60;
    display:none;align-items:center;gap:7px;
    padding:10px 15px;border-radius:99px;text-decoration:none;
    font-family:inherit;font-size:12.5px;font-weight:700;
    color:${PICK_GREEN};background:rgba(12,20,17,.93);
    border:1px solid ${PICK_GREEN}55;
    box-shadow:0 6px 22px rgba(0,0,0,.45);
    backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);
  }
  #slip-bar.show{display:flex}
  #slip-bar .slip-count{
    min-width:19px;height:19px;padding:0 5px;border-radius:99px;
    display:inline-flex;align-items:center;justify-content:center;
    background:${PICK_GREEN};color:#0B1210;font-size:11px;font-weight:800;
  }
  @media (max-width:420px){ #slip-bar{right:11px;bottom:12px;padding:9px 13px} }

  #board-error{
    position:sticky;top:0;z-index:80;cursor:pointer;
    padding:9px 13px;font-size:12px;font-weight:600;line-height:1.4;
    color:#FFD9D9;background:#7F1D1D;border-bottom:1px solid #B91C1C;
    font-family:inherit;
  }
  #build-stamp{
    position:fixed;left:10px;bottom:12px;z-index:55;
    font-family:inherit;font-size:9px;font-weight:700;letter-spacing:.06em;
    padding:3px 7px;border-radius:99px;opacity:.32;
    color:#93E06E;background:rgba(12,20,17,.75);border:1px solid rgba(147,224,110,.28);
  }
  `;
  const el = document.createElement('style');
  el.textContent = css;
  (document.head || document.documentElement).appendChild(el);
})();


// ─────────────────────────────────────────────────────────────────────────────
// Live game state overlay
//
// The slate JSON is regenerated by GitHub Actions a few times a day, so its
// `game_state` / `inning` / scores are a snapshot from the last model run --
// often hours stale. Model projections SHOULD move slowly; game state should
// not. So the two are split: projections keep coming from the repo JSON, and
// the fast-moving state is pulled straight from ESPN's public scoreboard in
// the browser every 30s and patched onto the already-loaded DATA.
//
// Costs nothing: no Pages build, no Odds API quota. ESPN's endpoint is
// undocumented and unauthenticated, so it can change without notice -- every
// failure path here falls back silently to whatever the JSON already said.
//
// This lives in picks.js only because every board already loads this file.
// If it grows further it deserves its own live.js.
// ─────────────────────────────────────────────────────────────────────────────
const ESPN_PATH = { mlb:'baseball/mlb', nfl:'football/nfl', nhl:'hockey/nhl', nba:'basketball/nba' };

// If ESPN ever blocks cross-origin reads, point this at a Cloudflare Worker
// that proxies the same URL and adds an Access-Control-Allow-Origin header.
const LIVE_PROXY = '';

function liveSport(){
  const m = location.pathname.match(/\/(mlb|nfl|nhl|nba)\//);
  return m ? m[1] : null;
}

function liveKey(s){ return String(s == null ? '' : s).toLowerCase().replace(/[^a-z0-9]/g, ''); }

// Teams are matched on every name ESPN exposes (full name, mascot, abbr)
// crossed with every name the slate uses, because the four models don't agree
// on a single identifier -- NHL/NBA carry abbreviations, MLB/NFL carry full
// names, and edge cases like "Athletics" vs "Oakland Athletics" break any
// single-field match.
function liveTeamKeys(t){
  if (!t) return [];
  return [t.displayName, t.shortDisplayName, t.name, t.abbreviation, t.location]
    .map(liveKey).filter(Boolean);
}

async function fetchLiveState(sport){
  const path = ESPN_PATH[sport];
  if (!path) return null;
  const url = `https://site.api.espn.com/apis/site/v2/sports/${path}/scoreboard`;
  const r = await fetch(LIVE_PROXY ? LIVE_PROXY + encodeURIComponent(url) : url, { cache:'no-store' });
  if (!r.ok) throw new Error('ESPN ' + r.status);
  const data = await r.json();

  const map = new Map();
  for (const ev of data.events || []){
    const comp = (ev.competitions || [])[0];
    if (!comp) continue;
    const st = comp.status || ev.status || {};
    const type = st.type || {};
    const away = (comp.competitors || []).find(c => c.homeAway === 'away');
    const home = (comp.competitors || []).find(c => c.homeAway === 'home');
    if (!away || !home) continue;

    const detail = type.shortDetail || type.detail || type.description || '';
    // MLB reports the half in the status text rather than a dedicated field.
    const half = /bot/i.test(detail) ? 'Bottom' : /top/i.test(detail) ? 'Top'
               : /mid/i.test(detail) ? 'Middle' : /end/i.test(detail) ? 'End' : null;
    const rec = {
      state: type.state === 'in' ? 'Live' : type.state === 'post' ? 'Final' : 'Preview',
      period: typeof st.period === 'number' ? st.period : null,
      half, detail,
      away_score: away.score != null && away.score !== '' ? Number(away.score) : null,
      home_score: home.score != null && home.score !== '' ? Number(home.score) : null,
    };
    // A team pair is NOT unique -- doubleheaders put the same two teams on the
    // slate twice. Every candidate is kept and disambiguated by start time at
    // lookup, rather than the last one parsed silently winning.
    rec.startMs = Date.parse(ev.date || comp.date || '') || null;
    for (const a of liveTeamKeys(away.team))
      for (const h of liveTeamKeys(home.team)){
        const k = a + '@' + h;
        if (!map.has(k)) map.set(k, []);
        if (!map.get(k).includes(rec)) map.get(k).push(rec);
      }
  }
  return map;
}

function lookupLive(map, g){
  const aways = [g.away_team, g.away_abbr].map(liveKey).filter(Boolean);
  const homes = [g.home_team, g.home_abbr].map(liveKey).filter(Boolean);
  const cands = [];
  for (const a of aways) for (const h of homes)
    for (const rec of (map.get(a + '@' + h) || []))
      if (!cands.includes(rec)) cands.push(rec);

  if (!cands.length) return null;
  if (cands.length === 1) return cands[0];

  // Doubleheader: pick the event whose start time is nearest this game's.
  const mine = Date.parse(g.game_time || '');
  if (!mine) return cands[0];
  let best = cands[0], bestGap = Infinity;
  for (const c of cands){
    const gap = c.startMs ? Math.abs(c.startMs - mine) : Infinity;
    if (gap < bestGap){ bestGap = gap; best = c; }
  }
  return best;
}

// Patches DATA in place. Returns {matched, changed, newlyFinal}.
function applyLiveState(map){
  const D = liveData();
  if (!D || !Array.isArray(D.games) || !map) return { matched:0, changed:0, newlyFinal:0 };
  let matched = 0, changed = 0, newlyFinal = 0;
  for (const g of D.games){
    const rec = lookupLive(map, g);
    if (!rec) continue;
    matched++;
    const before = [g.game_state, g.inning, g.inning_half, g.away_score, g.home_score].join('|');
    if (rec.state === 'Final' && g.game_state !== 'Final') newlyFinal++;
    g.game_state = rec.state;
    if (rec.period != null) g.inning = rec.period;
    if (rec.half) g.inning_half = rec.half;
    if (rec.away_score != null) g.away_score = rec.away_score;
    if (rec.home_score != null) g.home_score = rec.home_score;
    g.live_detail = rec.detail;
    if ([g.game_state, g.inning, g.inning_half, g.away_score, g.home_score].join('|') !== before) changed++;
  }
  return { matched, changed, newlyFinal };
}

// The boards declare their slate as `let DATA`. Top-level `let` in a classic
// script goes into the global lexical environment, NOT onto `window` -- so
// `liveData()` is always undefined from here. Reading the bare identifier at
// call time does resolve, because by then the page script has run.
function liveData(){
  try { return (typeof DATA !== 'undefined' && DATA) ? DATA : null; }
  catch (e){ return null; }
}

let _liveTimer = null;
async function refreshLive(){
  const sport = liveSport();
  if (!sport || !liveData()) return;
  try {
    const map = await fetchLiveState(sport);
    const res = applyLiveState(map);
    if (res.changed && typeof renderGames === 'function'){
      renderGames(liveData());
      // Only rebuild the player list when a game actually ended -- renderPlays
      // drops finished games from the pool, but re-rendering also collapses
      // any detail panel the user has open, so it isn't done on every tick.
      if (res.newlyFinal && typeof renderPlays === 'function') renderPlays(liveData());
    }
  } catch (e){
    // Silent by design: the board keeps showing the slate's own values.
    if (!refreshLive._warned){
      refreshLive._warned = true;
      console.warn('live scores unavailable, using slate values:', e && e.message);
    }
  }
}

function startLiveSync(everyMs){
  if (!liveSport()) return;
  const tick = () => refreshLive();
  clearInterval(_liveTimer);
  _liveTimer = setInterval(tick, everyMs || 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  tick();
}


// Self-starting: the board pages don't call this, so poll until their own
// load() has populated DATA, then begin syncing. Gives up after ~30s so a
// page that never loads a slate doesn't leave a timer spinning.
(function bootLiveSync(){
  if (!liveSport()) return;
  let tries = 0;
  const wait = setInterval(() => {
    const d = liveData();
    if (d && Array.isArray(d.games)){
      clearInterval(wait);
      startLiveSync(30000);
    } else if (++tries > 60){
      clearInterval(wait);
    }
  }, 500);
})();

// ─────────────────────────────────────────────────────────────────────────────
// Error surfacing
//
// Each board wraps load() in a catch whose only response was to print
// "Offline". Every failure -- a bad field, a missing script, a thrown
// reference -- therefore rendered an identical blank page with no way to tell
// them apart, and looked exactly like a quiet slate. These make a failure say
// what it was, on screen, without changing how the page recovers.
// ─────────────────────────────────────────────────────────────────────────────
function boardError(err, context){
  const msg = (err && (err.message || err)) || 'unknown error';
  try { console.error('[board] ' + (context || 'error') + ':', err); } catch(e){}
  let bar = document.getElementById('board-error');
  if (!bar){
    bar = document.createElement('div');
    bar.id = 'board-error';
    document.body.insertBefore(bar, document.body.firstChild);
  }
  bar.textContent = `\u26a0\ufe0f ${context || 'Error'}: ${msg}  \u00b7  tap to dismiss`;
  bar.onclick = () => bar.remove();
  return msg;
}

// Uncaught errors and rejected promises never reach a page's own try/catch,
// so they get the same treatment instead of vanishing into the console.
window.addEventListener('error', e => {
  if (e && e.message) boardError(e.message, 'Script error');
});
window.addEventListener('unhandledrejection', e => {
  const r = e && e.reason;
  if (r) boardError(r, 'Unhandled promise');
});

// Build stamp: makes "which version is actually deployed?" answerable at a
// glance instead of by reading source on a phone.
function mountBuildStamp(){
  if (document.getElementById('build-stamp')) return;
  const el = document.createElement('div');
  el.id = 'build-stamp';
  el.textContent = PICKS_VERSION;
  el.title = 'Build currently served to this device';
  document.body.appendChild(el);
}

// Legacy helper kept for the older cards that don't emit pickBtn() markup:
// injects a "+ Log" button into any rendered .play card that lacks one.
function decoratePicks(sport){
  document.querySelectorAll('.play[data-key]').forEach(el => {
    if (el.querySelector('.pick-btn')) return;
    const edgeBox = el.querySelector('.p-edge');
    if (!edgeBox) return;
    const player = (el.querySelector('.p-player')?.textContent || '').trim();
    const meta = (el.querySelector('.p-meta')?.textContent || '').trim();
    const edgeVal = (el.querySelector('.edge-val')?.textContent || '').trim();
    if (!player) return;
    const logoImg = el.querySelector('.p-logo img, .p-headshot img, .p-logo, .p-headshot, img');
    const logo = logoImg ? (logoImg.currentSrc || logoImg.src || '') : '';
    edgeBox.insertAdjacentHTML('beforeend', pickBtn({
      sport, label: player, detail: meta, line: edgeVal, logo,
    }));
  });
  syncPickButtons();
}
