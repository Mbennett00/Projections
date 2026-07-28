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
    // pick: { sport, label, detail, line }
    const list = all();
    const entry = Object.assign({
      id: 'p_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
      loggedAt: new Date().toISOString(),
      result: null, // null = pending, 'win' | 'loss' | 'push'
    }, pick);
    list.unshift(entry);
    save(list);
    return entry;
  }
  function setResult(id, result){
    const list = all();
    const p = list.find(x => x.id === id);
    if (p){ p.result = result; p.settledAt = new Date().toISOString(); save(list); }
    return p;
  }
  function remove(id){
    save(all().filter(x => x.id !== id));
  }
  function clearAll(){
    save([]);
  }
  function stats(list){
    const settled = list.filter(p => p.result === 'win' || p.result === 'loss');
    const wins = settled.filter(p => p.result === 'win').length;
    const losses = settled.length - wins;
    return { wins, losses, total: settled.length, pct: settled.length ? Math.round(wins / settled.length * 100) : null };
  }

  return { all, log, setResult, remove, clearAll, stats, KEY };
})();

// Injects a small "+ Log" button into every rendered play card so a pick can
// be saved with one tap, without touching each page's own render templates.
// Safe to call after every re-render — cards that already have a button are
// skipped, and a fresh call after a full re-render just re-adds them.
function decoratePicks(sport){
  document.querySelectorAll('.play[data-key]').forEach(el => {
    if (el.querySelector('.pick-btn')) return;
    const edgeBox = el.querySelector('.p-edge');
    if (!edgeBox) return;
    const player = (el.querySelector('.p-player')?.textContent || '').trim();
    const meta = (el.querySelector('.p-meta')?.textContent || '').trim();
    const edgeVal = (el.querySelector('.edge-val')?.textContent || '').trim();
    if (!player) return;

    const btn = document.createElement('button');
    btn.className = 'pick-btn';
    btn.type = 'button';
    btn.textContent = '+ Log';
    btn.style.cssText = 'margin-top:6px;font-size:9.5px;font-weight:700;padding:4px 9px;' +
      'border-radius:99px;border:1px solid rgba(147,224,110,.4);background:rgba(147,224,110,.14);' +
      'color:#93E06E;cursor:pointer;font-family:inherit;';
    btn.onclick = (e) => {
      e.stopPropagation();
      Picks.log({ sport, label: player, detail: meta, line: edgeVal });
      btn.textContent = '✓ Logged';
      btn.disabled = true;
      btn.style.opacity = '.6';
      btn.style.cursor = 'default';
    };
    edgeBox.appendChild(btn);
  });
}
