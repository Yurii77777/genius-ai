// Classic skin: the current Herdr panel (herdr/genius-status) as HTML on the bridge's data.
// Shelves: left — Done today, Models, Requests, Coordinator (the send line and the latest reply);
// middle — Attention («Now», problems, then every task: 👤 / 🔄 / 📋); right — Projects (heavy queue, cards).
// Section names and controls in English; the content in the user's language (the panel's rule).
import { attention, doneToday, requests, today } from './app.js';

const LABELS = { later: 'later', hold: 'hold', paused: 'paused', heavy: 'Heavy queue', resolve: 'resolve', stage2: 'stage 2' };
const TITLES = { done: 'Done today', models: 'Models', requests: 'Requests', attention: 'Attention', projects: 'Projects', coord: 'Coordinator' };
const WORDS = {
  uk: { no_requests: 'запитів немає', agent_done: 'закінчив — подивись результат', ok: '✅ усе спокійно', at: 'оновлено', never: 'ще не оновлено',
    waits: 'чекає', next: 'далі', release_open: 'реліз можна', release_closed: 'реліз', ask: '💬 запитати в чаті', agent_asks: 'агент чекає твоєї відповіді',
    to_agent: 'відкрити термінал агента', pause: 'поставити на паузу', resume: 'зняти з паузи', later_tip: 'відкласти: операція стане на паузу',
    answered: 'відповідь', more: 'натисни, щоб розгорнути', hold_tip: 'нового в роботу не брати', runs: 'працює', queued: 'чекає', min: 'хв',
    quiet: 'екран не змінювався', you_tip: 'чекає тебе', work_tip: 'у роботі', parked_tip: 'відкладено', not_started: 'агент не запущений',
    stage2: 'етап 2: ще не підключено', send_ph: 'повідомлення координатору…', no_reply: 'відповіді сьогодні ще немає',
    sent: 'надіслано', dry: 'dry-run: команду записано, не виконано', failed: 'не вдалося' },
  en: { no_requests: 'no requests', agent_done: 'finished — see the result', ok: '✅ all quiet', at: 'updated', never: 'not updated yet',
    waits: 'waiting for', next: 'next', release_open: 'release open', release_closed: 'release', ask: '💬 ask in the chat', agent_asks: 'the agent waits for your answer',
    to_agent: "open the agent's terminal", pause: 'pause the project', resume: 'resume the project', later_tip: 'put aside: the operation is paused',
    answered: 'answer', more: 'click to unfold', hold_tip: 'take nothing new', runs: 'runs', queued: 'waits', min: 'min',
    quiet: 'screen unchanged', you_tip: 'waits for you', work_tip: 'in work', parked_tip: 'put aside', not_started: 'agent not started',
    stage2: 'stage 2: not connected yet', send_ph: 'message to the coordinator…', no_reply: 'no reply today yet',
    sent: 'sent', dry: 'dry-run: the command is logged, not run', failed: 'failed' },
};
const QUIET_FROM = 3;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hhmm = (ms) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
const minutes = (sec) => (sec ? Math.max(0, Math.floor((Date.now() / 1000 - sec) / 60)) : null);
const short = (name) => String(name).split(' · ').pop();
const STATUS = { working: 'var(--working)', blocked: 'var(--blocked)', done: 'var(--done)', idle: 'var(--idle)' };

export function attach(store, api) {
  const root = document.getElementById('classic');
  const open = new Set();                                   // unfolded rows
  const busy = new Set();                                   // buttons just pressed
  let visible = false, timer = 0, toastT = 0;
  const ops = new Map();
  const agents = new Map();
  const projects = new Map();

  const w = () => WORDS[store.s.lang] ?? WORDS.en;

  function toast(text, bad) {
    let el = root.querySelector('.toast');
    if (!el) { el = document.createElement('div'); root.appendChild(el); }
    el.className = `toast${bad ? ' bad' : ''}`; el.textContent = text;
    clearTimeout(toastT); toastT = setTimeout(() => el.remove(), 4000);
  }
  async function act(key, p) {
    if (busy.has(key)) return;
    busy.add(key); render();
    const r = await p;
    busy.delete(key);
    toast(r.ok ? (r.dry ? w().dry : w().sent) : `${w().failed}: ${r.error || r.msg || ''}`, !r.ok);
    render();
  }

  const shelf = (key, color, count, body, extra = '') => `
    <section class="shelf ${key === 'requests' || key === 'coord' ? 'grow' : ''}" data-shelf="${key}">
      <div class="head"><div class="bar" style="background:${color}"></div><div class="title" style="color:${color}">${TITLES[key]}</div>
        <div class="count">${count}</div><div class="sp"></div>${extra}</div>
      ${body}
    </section>`;

  function doneShelf(s) {
    const done = doneToday(s);
    const body = done.length ? done.map((x) => `<div class="row small dim" title="${esc(x)}"><span class="trunc">✅ ${esc(x)}</span></div>`).join('')
      : '<div class="row muted">—</div>';
    return shelf('done', 'var(--done)', done.length, `<div class="body">${body}</div>`);
  }

  function modelsShelf(s) {
    const rows = (s.models?.providers ?? []).map((p) => {
      const lims = Object.entries(p.limits ?? {});
      const worst = Math.max(0, ...lims.map(([, l]) => l.used ?? 0));
      let color = worst >= 90 ? 'var(--blocked)' : worst >= 70 ? 'var(--working)' : 'var(--done)', text, tip = '';
      if (p.name === 'Ollama') {
        color = p.up ? 'var(--done)' : 'var(--dim)';
        const u = p.cloud?.usage ?? {}, b = p.cloud?.balance ?? {};
        const used = b.left_usd != null && b.allowance_usd ? Math.round((1 - b.left_usd / b.allowance_usd) * 100) : null;
        if (used != null) color = used >= 90 ? 'var(--blocked)' : used >= 70 ? 'var(--working)' : 'var(--done)';
        text = [p.up ? '' : 'down', u.requests != null ? `24h ${u.requests} req · $${(u.usd ?? 0).toFixed(2)}` : '',
          b.left_usd != null ? `$${b.left_usd.toFixed(0)}/${b.allowance_usd}` : '', p.key ? '' : 'no key'].filter(Boolean).join(' · ');
        tip = (p.loaded ?? []).map((m) => `in memory: ${m}`).join('\n');
      } else {
        text = lims.length ? lims.map(([k, l]) => `${k} ${l.used}%`).join(' · ') : 'no data yet';
        if (!lims.length) color = 'var(--dim)';
        tip = lims.map(([k, l]) => `${k}: ${l.used}%${l.resets ? ` → ${hhmm(l.resets * 1000)}` : ''}`).join('\n');
      }
      return `<div class="row" style="align-items:center" title="${esc(tip)}"><span class="dot" style="background:${color}"></span>
        <span style="flex:none;width:52px">${esc(p.name)}</span><span class="grow1 trunc small muted">${esc(text)}</span></div>`;
    });
    return shelf('models', 'var(--accent)', rows.length, `<div class="body">${rows.join('') || '<div class="row muted">…</div>'}</div>`);
  }

  function requestsShelf(s) {
    const rows = requests(s);
    const body = rows.length ? rows.map((a) => `
      <div class="wait" data-act="open" data-addr="${esc(a.address)}" title="${esc(w().to_agent)}">
        <div class="row" style="padding:0"><span class="tag">${esc(a.tag)}</span><span class="grow1 trunc">${a.sub ? '↳ ' : ''}🤖 ${esc(a.name)}</span></div>
        <div class="small ${a.status === 'blocked' ? 'red' : 'blue'}">${esc(a.status === 'blocked' ? (a.msg || w().agent_asks) : w().agent_done)}</div>
      </div>`).join('') : `<div class="row muted">${esc(w().no_requests)}</div>`;
    return shelf('requests', rows.length ? 'var(--blocked)' : 'var(--muted)', rows.length, `<div class="body">${body}</div>`);
  }

  function coordShelf(s) {
    const r = s.reply;
    const st = s.coordinator ? `<span class="dot" style="background:${STATUS[s.coordinator.status] ?? 'var(--unknown)'}" title="${esc(s.coordinator.status)}"></span>` : '';
    const reply = r ? `<div class="reply" title="${esc(r.at)}"><span class="dim">${esc(String(r.at).slice(11, 16))} · </span>${esc(r.text)}</div>`
      : `<div class="reply dim">${esc(w().no_reply)}</div>`;
    return shelf('coord', 'var(--accent)', s.coordinator ? 1 : 0, `<div class="body" style="padding:0">${reply}</div>
      <form class="send" data-form="send"><input name="t" autocomplete="off" placeholder="${esc(w().send_ph)}" ${s.coordinator ? '' : 'disabled'}>
      <button class="btn" ${s.coordinator && !busy.has('send') ? '' : 'disabled'}>send</button></form>`, st);
  }

  function nowBlock(s) {
    const f = s.focus ?? {};
    const mine = f.date === today();
    const work = f.in_work ?? [];
    const steps = mine ? (f.steps ?? []).map((st, i) => ({ ...st, n: i + 1 })).filter((st) => !st.done) : [];
    if (!(mine && f.focus) && !steps.length && !work.length) return '';
    const watchA = new Map((s.agents ?? []).map((a) => [a.name, a]));
    return `<div class="block">
      ${mine && f.focus ? `<div style="font-weight:600;color:var(--accent)">🎯 ${esc(f.focus)}</div>` : ''}
      ${steps.map((st) => `<div class="small ${st.now ? '' : 'muted'}">${st.now ? '▶' : '○'} ${st.n}. ${esc(st.text)}</div>`).join('')}
      ${work.map((x) => {
        const item = typeof x === 'string' ? { text: x } : x;
        const a = watchA.get(item.agent);
        const what = a ? (a.msg || a.status || '') : w().not_started;
        const sub = [item.agent ? `🤖 ${item.agent}` : '', a?.model ? a.model.split(':')[0] : '', what,
          a?.quiet != null && a.quiet >= QUIET_FROM ? `${a.quiet} ${w().min}` : ''].filter(Boolean).join(' · ');
        return `<div class="${a ? 'clickable' : ''}" ${a ? `data-act="open" data-addr="${esc(a.address)}"` : ''} title="${esc(`${item.text}\n${item.waiting_for ?? ''}`)}">
          <div class="small trunc">▶ ${esc(item.text)}</div>
          ${sub ? `<div class="tiny trunc ${a?.status === 'blocked' ? 'red' : 'dim'}" style="padding-left:14px">${esc(sub)}</div>` : ''}</div>`;
      }).join('')}</div>`;
  }

  function attentionShelf(s) {
    const { problems, waits } = attention(s);
    const W = w();
    const rows = [nowBlock(s)];
    if (!problems.length && !waits.length) rows.push(`<div class="row muted">${esc(W.ok)}</div>`);
    for (const p of problems) {
      rows.push(`<div class="row ${p.url ? 'clickable' : ''}" ${p.url ? `data-act="url" data-url="${esc(p.url)}" title="${esc(p.url)}"` : ''}>
        <span class="tag">${esc(p.project.tag)}</span><span class="grow1 red">${esc(p.text)}</span>
        ${p.data?.kind === 'pr_conflict' ? `<button class="btn" disabled title="${esc(W.stage2)}">${LABELS.resolve} · ${LABELS.stage2}</button>` : ''}</div>`);
    }
    for (const o of waits) {
      const key = `${o.project}-${o.id}`;
      const mine = o.group === 'you' || o.group === 'cut';
      const full = open.has(key);
      const mark = o.group === 'cut' ? '⚡' : o.group === 'you' ? '👤' : o.group === 'work' ? '🔄' : '📋';
      const line = mine && o.ask ? o.ask : o.answered ? `↩ ${W.answered}: ${o.answered.text ?? o.answered}` : o.waiting_for;
      const wrap = full || (mine && Boolean(o.ask));
      const color = o.group === 'cut' ? 'red' : mine ? 'amber' : 'dim';
      rows.push(`<div class="wait ${mine ? 'mine' : ''}" data-act="fold" data-key="${esc(key)}" ${full ? '' : `title="${esc(W.more)}"`}>
        <div class="row" style="padding:0"><span class="tag">${esc(o.tag)}</span><span class="grow1 ${full ? '' : 'trunc'}">${esc(o.what)}</span>
          <span class="icon" data-act="ask" data-op="${esc(o.id)}" title="${esc(W.ask)}">💬</span></div>
        ${line ? `<div class="${mine ? '' : 'small'} ${color} ${wrap ? '' : 'trunc'}">${mark} ${esc(line)}</div>` : ''}
        ${full && o.waiting_for && line !== o.waiting_for ? `<div class="small muted">${esc(W.waits)}: ${esc(o.waiting_for)}</div>` : ''}
        ${full && o.next ? `<div class="small dim">${esc(W.next)}: ${esc(o.next)}</div>` : ''}
        ${mine ? `<div class="btns">${(o.answers ?? []).map((a, i) => `<button class="btn" data-act="answer" data-op="${esc(o.id)}" data-i="${i}" ${busy.has(`a-${o.id}`) ? 'disabled' : ''}>${esc(a)}</button>`).join('')}
          <button class="btn dimmed" data-act="later" data-op="${esc(o.id)}" title="${esc(W.later_tip)}" ${busy.has(`l-${o.id}`) ? 'disabled' : ''}>⏸ ${LABELS.later}</button></div>` : ''}
      </div>`);
    }
    const count = problems.length + waits.length;
    return shelf('attention', count ? 'var(--blocked)' : 'var(--done)', count, `<div class="body">${rows.join('')}</div>`);
  }

  function heavyBlock(q) {
    const quiet = q?.quiet?.[0];
    if (!q?.holder && !(q?.waiting ?? []).length && !(q?.ml ?? []).length && !quiet) return '';
    const row = (mark, x, cls, verb) => `<div class="row small" style="padding:0" title="${esc(x.cmd || '')}"><span class="${cls}" style="flex:none">${mark} ${esc(x.who)}</span>
      <span class="grow1 trunc dim">${esc(x.cmd || '')}</span><span class="${cls}" style="flex:none">${verb} ${minutes(x.since) ?? 0} ${w().min}</span></div>`;
    return `<div class="card"><div class="small amber" style="font-weight:600">⚙ ${LABELS.heavy}</div>
      ${quiet ? `<div class="small amber">🔕 ${esc(quiet)} → ${esc(q.quiet[1])}</div>` : ''}
      ${(q.ml ?? []).map((m) => `<div class="row small" style="padding:0" title="${esc(m.cmd)}"><span class="red" style="flex:none">🧠 ${esc(m.gb)} GB</span><span class="grow1 trunc dim">${esc(m.cmd)}</span></div>`).join('')}
      ${(q.holders ?? (q.holder ? [q.holder] : [])).map((x) => row('▶', x, '', w().runs)).join('')}
      ${(q.waiting ?? []).map((x) => row('⏳', x, 'muted', w().queued)).join('')}</div>`;
  }

  function projectsShelf(s) {
    const W = w();
    const cards = (s.projects ?? []).map((p) => {
      const mine = (s.agents ?? []).filter((a) => a.project === p.name);
      const pops = (s.ops ?? []).filter((o) => o.project === p.name);
      const goalOf = (a) => pops.find((o) => o.agent && o.agent === a.name)?.what || a.goal || '';
      const c = p.counts;
      return `<div class="card ${p.paused ? 'paused' : ''}" title="${esc(p.path)}">
        <div class="top"><span class="name">${esc(p.tag)}</span><span class="grow1 trunc dim" style="font-size:12px">${esc(p.spoken)}</span>
          ${p.release ? `<span class="pill ${p.release.open ? 'blue' : 'dim'}">${p.release.open ? `🚀 ${esc(W.release_open)}` : `🚫 ${esc(W.release_closed)} ${esc(p.release.text)}`}</span>` : ''}
          ${!p.paused && p.hold ? `<span class="pill amber" title="${esc(p.hold?.why || W.hold_tip)}">✋ ${LABELS.hold}</span>` : ''}
          ${p.paused ? `<span class="tiny dim">${LABELS.paused}</span>` : ''}
          <span class="icon" data-act="pause" data-name="${esc(p.name)}" title="${esc(p.paused ? W.resume : W.pause)}">${busy.has(`p-${p.name}`) ? '…' : p.paused ? '▶' : '⏸'}</span>
          ${!p.paused ? `<span class="dot" style="background:${p.problems.length ? 'var(--blocked)' : 'var(--done)'}"></span>` : ''}</div>
        ${!p.paused && p.info.length ? `<div class="chips">${p.info.map((x) => x.startsWith('PR ') && p.links?.prs ? `<a href="${esc(p.links.prs)}" target="_blank" rel="noopener">${esc(x)}</a>` : `<span>${esc(x)}</span>`).join('')}</div>` : ''}
        ${p.urls.length ? `<div class="chips">${p.urls.map((u) => `<a href="${esc(u.url)}" target="_blank" rel="noopener" title="${esc(`${u.url} · ${u.code} · ${u.ms} ms`)}" style="display:inline-flex;gap:5px;align-items:center${u.ok ? '' : ';color:var(--blocked)'}"><span class="dot" style="background:${u.ok ? 'var(--done)' : 'var(--blocked)'}"></span>${esc(short(u.name))} ${esc(u.ms)} ms</a>`).join('')}</div>` : ''}
        ${!p.paused && p.notes.length ? p.notes.map((x) => `<div class="small amber">📌 ${esc(x)}</div>`).join('') : ''}
        ${!p.paused && mine.length ? mine.map((a) => {
          const goal = goalOf(a);
          return `<div class="agent" data-act="open" data-addr="${esc(a.address)}" title="${esc(`${a.status} · ${a.name}${goal ? ` · ${goal}` : ''}`)}">
            <span class="dot" style="background:${a.stuck ? 'var(--blocked)' : STATUS[a.status] ?? 'var(--unknown)'}"></span>
            <span class="trunc ${a.status === 'blocked' ? 'red' : 'muted'}" style="flex:none;max-width:150px">${a.sub ? '↳ ' : ''}🤖 ${esc(a.name)}</span>
            ${a.model ? `<span class="model" title="${esc(a.model)}">${esc(a.model.split(/[:-]/).slice(0, 2).join('-'))}</span>` : ''}
            <span class="grow1 trunc tiny dim">${esc(goal)}</span>
            ${a.quiet != null && a.quiet >= QUIET_FROM ? `<span class="tiny ${a.stuck ? 'red' : 'dim'}" title="${esc(W.quiet)}">${a.quiet} ${esc(W.min)}</span>` : ''}</div>`;
        }).join('') : ''}
        ${!p.paused && pops.length ? `<div class="counters">${c.you ? `<span class="amber" title="${esc(W.you_tip)}">👤 ${c.you}</span>` : ''}${c.work ? `<span class="muted" title="${esc(W.work_tip)}">🔄 ${c.work}</span>` : ''}${c.parked ? `<span class="dim" title="${esc(W.parked_tip)}">⏸ ${c.parked}</span>` : ''}</div>` : ''}
      </div>`;
    });
    const when = s.status_at ? `${w().at} ${hhmm(s.status_at * 1000)}` : w().never;
    const errs = Object.entries(s.errors ?? {}).map(([k, v]) => `<div class="err">${esc(k)}: ${esc([].concat(v).join(' '))}</div>`).join('');
    return shelf('projects', 'var(--accent)', (s.projects ?? []).filter((p) => !p.paused).length,
      `${errs}<div class="body">${heavyBlock(s.heavy)}${cards.join('')}</div>`, `<span class="when">${esc(when)}</span>`);
  }

  function render() {
    if (!visible) return;
    const s = store.s;
    ops.clear(); (s.ops ?? []).forEach((o) => ops.set(o.id, o));
    agents.clear(); (s.agents ?? []).forEach((a) => agents.set(a.address, a));
    projects.clear(); (s.projects ?? []).forEach((p) => projects.set(p.name, p));
    // keep the scroll of each shelf and a half-typed message across redraws
    const keep = {};
    root.querySelectorAll('[data-shelf]').forEach((el) => { const b = el.querySelector('.body'); if (b) keep[el.dataset.shelf] = b.scrollTop; });
    const input = root.querySelector('[data-form="send"] input');
    const draft = input?.value ?? '', focused = document.activeElement === input;
    const t = root.querySelector('.toast');
    root.innerHTML = `<div class="grid">
      <div class="side">${doneShelf(s)}${modelsShelf(s)}${requestsShelf(s)}${coordShelf(s)}</div>
      <div class="col">${attentionShelf(s)}</div>
      <div class="col">${projectsShelf(s)}</div></div>`;
    if (t) root.appendChild(t);
    root.querySelectorAll('[data-shelf]').forEach((el) => { const b = el.querySelector('.body'); if (b && keep[el.dataset.shelf]) b.scrollTop = keep[el.dataset.shelf]; });
    const inp = root.querySelector('[data-form="send"] input');
    if (inp) { inp.value = draft; if (focused) inp.focus(); }
  }

  root.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el || !root.contains(el)) return;
    const a = el.dataset.act;
    if (a !== 'fold') e.stopPropagation();
    if (a === 'fold') { const k = el.dataset.key; open.has(k) ? open.delete(k) : open.add(k); render(); return; }
    if (a === 'url') { window.open(el.dataset.url, '_blank', 'noopener'); return; }
    if (a === 'open') { act(`o-${el.dataset.addr}`, api.open(el.dataset.addr)); return; }
    const o = ops.get(el.dataset.op);
    if (a === 'answer' && o) { act(`a-${o.id}`, api.answer(o, o.answers[+el.dataset.i])); return; }
    if (a === 'later' && o) { act(`l-${o.id}`, api.later(o)); return; }
    if (a === 'ask' && o) { act(`k-${o.id}`, api.ask(o)); return; }
    const p = projects.get(el.dataset.name);
    if (a === 'pause' && p) act(`p-${p.name}`, api.pause(p));
  });
  root.addEventListener('submit', (e) => {
    e.preventDefault();
    const inp = e.target.querySelector('input'); const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    act('send', api.send(text));
  });

  store.on(() => render());
  return {
    show() { visible = true; render(); clearInterval(timer); timer = setInterval(render, 30000); },
    hide() { visible = false; clearInterval(timer); },
  };
}
