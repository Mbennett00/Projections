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
  `;
  const el = document.createElement('style');
  el.textContent = css;
  (document.head || document.documentElement).appendChild(el);
})();


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
