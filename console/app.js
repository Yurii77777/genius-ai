// genius-ai console: the live data from the bridge (console/server.py) and the skin switch.
// The state comes once from /api/state, then from /events: «full» replaces it, «diff» merges the
// top-level parts that changed. Every action is a POST to /api/action (the bridge runs the command).

const listeners = new Set();
export const store = {
  s: {},
  on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  emit(changed) { for (const fn of listeners) { try { fn(store.s, changed); } catch (e) { console.error(e); } } },
};

export const api = {
  async act(kind, args = {}) {
    try {
      const r = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, ...args }) });
      const j = await r.json().catch(() => ({}));
      return { ok: r.ok && j.ok !== false, ...j };
    } catch (e) { return { ok: false, error: String(e) }; }
  },
  answer: (op, text) => api.act('answer', { id: op.id, text }),
  later: (op) => api.act('later', { id: op.id }),
  ask: (op) => api.act('ask', { id: op.id }),
  open: (address) => api.act('open', { address }),
  pause: (p) => api.act(p.paused ? 'resume' : 'pause', { name: p.name }),
  send: (text) => api.act('send', { text }),
};

// --- derived views, shared by both skins (the panel's own ordering) ---
export const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
export function attention(s) {
  const live = (s.projects ?? []).filter((p) => !p.paused);
  const problems = live.flatMap((p) => p.problems.map((x, i) => ({ id: `${p.name}-p${i}`, project: p, ...x })));
  const order = { cut: 0, you: 1, work: 2, next: 3 };
  const rank = new Map(live.map((p) => [p.name, p.rank]));
  const waits = (s.ops ?? []).filter((o) => !o.paused && !o.project_paused)
    .sort((a, b) => order[a.group] - order[b.group] || (rank.get(a.project) ?? 99) - (rank.get(b.project) ?? 99));
  return { problems, waits };
}
export function doneToday(s) {
  const f = s.focus ?? {};
  const steps = f.date === today() ? (f.steps ?? []).filter((st) => st.done).map((st) => `🎯 ${st.text}`) : [];
  return [...[...(f.done_today ?? [])].reverse(), ...steps.reverse()];
}
export const requests = (s) => (s.agents ?? []).filter((a) => a.status === 'blocked' || a.status === 'done')
  .sort((a, b) => (a.status === 'blocked' ? 0 : 1) - (b.status === 'blocked' ? 0 : 1));

// --- connection ---
let es = null, retry = 0;
function connect() {
  es = new EventSource('/events');
  es.addEventListener('full', (e) => { retry = 0; store.s = JSON.parse(e.data); store.emit(null); setLink(true); });
  es.addEventListener('diff', (e) => { const { changed } = JSON.parse(e.data); Object.assign(store.s, changed); store.emit(changed); setLink(true); });
  es.onerror = () => { setLink(false); es.close(); setTimeout(connect, Math.min(30000, 1000 * 2 ** retry++)); };
}
const linkEl = document.getElementById('link');
function setLink(on) { linkEl.textContent = on ? '' : 'BRIDGE OFFLINE · RECONNECTING'; linkEl.hidden = on; }

// --- skins ---
const SKINS = ['hunter', 'classic'];
const skins = {};
let skin = 'hunter';
try { const v = localStorage.getItem('genius.console.skin'); if (SKINS.includes(v)) skin = v; } catch { /* default */ }

async function load(name) {
  if (skins[name]) return skins[name];
  const m = name === 'hunter' ? await import('./hunter.js') : await import('./classic.js');
  skins[name] = m.attach(store, api);
  return skins[name];
}
async function show(name) {
  skin = name;
  try { localStorage.setItem('genius.console.skin', name); } catch { /* not remembered */ }
  document.body.dataset.skin = name;
  document.querySelectorAll('#skin button').forEach((b) => b.classList.toggle('on', b.dataset.skin === name));
  for (const [k, v] of Object.entries(skins)) if (k !== name) v.hide();
  try { (await load(name)).show(); }
  catch (e) {
    console.error(e);
    if (name === 'hunter') { linkEl.hidden = false; linkEl.textContent = 'HUNTER SKIN FAILED TO LOAD (THREE.JS) · CLASSIC SHOWN'; show('classic'); }
  }
}
document.querySelectorAll('#skin button').forEach((b) => b.addEventListener('click', () => show(b.dataset.skin)));
window.addEventListener('keydown', (e) => {
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.code === 'KeyK') { e.preventDefault(); e.stopImmediatePropagation(); show(skin === 'hunter' ? 'classic' : 'hunter'); }
}, true);

fetch('/api/state').then((r) => r.json()).then((s) => { if (!Object.keys(store.s).length) { store.s = s; store.emit(null); } }).catch(() => {});
connect();
show(skin);
