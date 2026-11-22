// genius-status — genius-ai's live status on the Herdr screen: one shelf per section.
// Installed by `bin/genius herdr install` (copied, not linked: Herdr's hot reload does not see
// through links). The layout `genius` shows three copies in its right column:
//   props.section = "attention"  on top «Now»: the focus of the day and its steps (state/focus.json);
//                                then the workflow: problems (failed CI, URL down), ⚡ sub-agents a restart
//                                interrupted, then every task: 👤 waits for the user ("needs": "user") with
//                                its question and answer buttons, 🔄 in work, 📋 next ("queue": "next")
//   props.section = "projects"   on top the heavy queue (bin/heavy: who runs, who waits); one card per
//                                project: branch, PRs, prod URLs, release window, hold mark, the lines of
//                                its own card command, its agents (goal, minutes of quiet), counters
//                                👤 waits for you · 🔄 in work · ⏸ put aside; click = its space
//   props.section = "ops"        pending operations (state/operations.json), folded per project
// and one copy in the left sidebar, under Herdr's agents list:
//   props.section = "models"     what each model provider has left: Claude and Codex (5 h, 7 d), Ollama (up,
//                                in memory, cloud usage); from `bin/genius models --json`, every minute
//   props.section = "done"       what the board closed today, newest first (the left sidebar): the day's
//                                record moves out of «Now», which keeps only the focus and the work going on
//   props.section = "requests"   what agents and sub-agents ask of the user, live from Herdr: an
//                                agent blocked on a question, an agent that finished; click = its terminal
// Data: `bin/status --json` of the genius-ai folder named in root.json (written by the installer).
// The "projects" copy refreshes every 5 min and when state/ changes, and posts a toast when
// something changes (a new problem, a problem gone, a new wait for the user, a release window
// opened). ↻ on any shelf refreshes now. Quiet when all is well: a paused project is dimmed and
// never raises attention or a toast.
// Read-only for projects and services. Its only writes: a question typed into the genius-ai chat
// when the user presses 💬 on an operation; an answer button (bin/genius ops answer, and the same
// text to the chat); «later» (bin/genius ops later); ⏸/▶ on a card (bin/genius pause | resume).

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, readTextFile, watchFolder, appHome, spaces, panes, agents, terminals, run, theme, shared, setShared, props, notify, nextCommand } from "herdr";

const KEY = "genius.status";
const EXTRA = "genius.extra";                   // focus, heavy queue, the watcher's notes: cheap, read often
const EVERY = 5 * 60 * 1000;
const EVERY_EXTRA = 60 * 1000;
const QUIET_FROM = 3;                           // minutes of an unchanged screen worth showing
const QUIET_ALARM = 10;                         // … worth a red mark while the agent says it works
// Section names and every control (a button, a badge) are always English, like Herdr's own «Spaces» and
// «Agents» (the user's decision of 2026-11-17: controls are labelled in English for consistency); the
// content and the tooltips follow the user's language.
const LABELS = { later: "later", hold: "hold", paused: "paused", heavy: "Heavy queue", resolve: "resolve", resolving: "resolving…" };
const MODELS = "genius.models";
const TITLES = { done: "Done today", models: "Models", requests: "Requests", attention: "Attention", projects: "Projects", ops: "Operations" };
const WORDS = {
  uk: { no_requests: "запитів немає", agent_done: "закінчив — подивись результат",
        ok: "✅ усе спокійно", none: "нічого не чекає",
        loading: "оновлюю…", never: "ще не оновлено", at: "оновлено", noroot: "не знайдено genius-ai: запустіть bin/genius herdr install",
        waits: "чекає", next: "далі", you: "чекає тебе", paused: "пауза", release_open: "реліз можна", release_closed: "реліз",
        ask: "💬 запитати в чаті", ask_text: "Розкажи про операцію", no_chat: "чат genius-ai не знайдено в Herdr",
        agent_asks: "агент чекає твоєї відповіді", to_agent: "відкрити термінал агента",
        pause: "поставити на паузу", resume: "зняти з паузи", d: "д",
        fixed: "виправлено", window_open: "вікно релізу відкрите", open_space: "відкрити простір", new_space: "створити простір",
        resolve_tip: "запустити агента: злити базову гілку в PR (база — головна), без push", resolve_fail: "не вдалося запустити агента",
        later_tip: "відкласти: операція стане на паузу", answer_text: "Відповідь на", answered: "відповідь",
        more: "натисни, щоб розгорнути", hold_tip: "нового в роботу не брати", runs: "працює", queued: "чекає", min: "хв",
        quiet: "екран не змінювався", you_tip: "чекає тебе", work_tip: "у роботі", parked_tip: "відкладено" },
  en: { no_requests: "no requests", agent_done: "finished — see the result",
        ok: "✅ all quiet", none: "nothing is waiting",
        loading: "updating…", never: "not updated yet", at: "updated", noroot: "genius-ai not found: run bin/genius herdr install",
        waits: "waiting for", next: "next", you: "waits for you", paused: "paused", release_open: "release open", release_closed: "release",
        ask: "💬 ask in the chat", ask_text: "Tell me about the operation", no_chat: "the genius-ai chat is not in Herdr",
        agent_asks: "the agent waits for your answer", to_agent: "open the agent's terminal",
        pause: "pause the project", resume: "resume the project", d: "d",
        fixed: "fixed", window_open: "release window is open", open_space: "open the space", new_space: "make a space",
        resolve_tip: "start an agent: merge the base branch into the PR (the base is the authority), no push", resolve_fail: "could not start the agent",
        later_tip: "put aside: the operation is paused", answer_text: "Answer to", answered: "answer",
        more: "click to unfold", hold_tip: "take nothing new", runs: "runs", queued: "waits", min: "min",
        quiet: "screen unchanged", you_tip: "waits for you", work_tip: "in work", parked_tip: "put aside" },
};

// genius-ai's own group (bin/status "self": true, tag ⚙️ GA; the user's decision of 2026-11-18): Attention
// draws it like a project; the Projects shelf and its counter leave it out (no git, no space, no pause).
const projectStates = (states) => states.filter((s) => !s.self);

const hhmm = (ms) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
const inside = (dir, path) => Boolean(dir) && (dir === path || dir.startsWith(path + "/"));
const short = (name) => name.split(" · ").pop();
const days = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? Math.floor((Date.now() - t) / 86400000) : null; };                       // "🌐 EX · сайт" → "сайт"
const openUrl = (url) => { if (url) exec("open", [url]); };
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const minutes = (sec) => (sec ? Math.max(0, Math.floor((Date.now() / 1000 - sec) / 60)) : null);
const parse = (raw, fallback) => { try { return JSON.parse(raw ?? "") ?? fallback; } catch { return fallback; } };

// The space that works in this project: a terminal of it in the project's folder, else its own folder.
function spaceOf(path) {
  const ps = panes();
  return spaces().find((s) => {
    const tabs = new Set((s.tabs ?? []).map((t) => t.id));
    return ps.some((p) => tabs.has(p.tab) && inside(p.cwd, path)) || inside(s.folder, path);
  });
}

// Every agent Herdr knows, with its folder: the agents with a pane (agents()) and the sub-agents,
// which are terminals an agent started with `hc run` and have no pane (terminals() with a parent).
function everyAgent() {
  const cwd = new Map(panes().map((p) => [p.pane, p.cwd]));
  const all = terminals();
  const said = new Map(all.map((k) => [k.address, k.report?.msg]));     // what the agent asks, in its own words
  const top = agents().map((a) => ({ address: a.address, pane: a.pane, status: a.status, seq: a.seq, sub: false, cwd: cwd.get(a.pane),
    where: a.space_name, title: a.title || a.program?.name || a.address, msg: said.get(a.address) }));
  const seen = new Set(top.map((a) => a.address));
  const subs = all.filter((k) => k.parent && k.state === "live" && k.status && !seen.has(k.address))
    .map((k) => ({ address: k.address, status: k.status, seq: 0, sub: true, cwd: k.cwd, where: k.name || k.address,
      title: k.name || k.address, goal: k.title && k.title !== k.name ? k.title : "", msg: k.report?.msg,
      model: k.labels?.["genius.model"] ?? "" }));
  return [...top, ...subs];
}

// Open an agent the way Herdr's own agents list does: an agent with a pane gets that pane focused;
// a sub-agent (no pane) is peeked over the tab, with keep-as-tab and close. agent.focus only
// changes an {"agent": "focused"} block, and the layout `genius` has none (2026-11-12).
function openAgent(a) {
  if (a.pane) run("pane.focus", { pane: a.pane });
  else run("terminal.peek", { terminal: a.address });
}

// The agents and sub-agents that work in this folder.
const agentsIn = (path) => everyAgent().filter((a) => inside(a.cwd, path));

// What changed between two reads, as toasts. Paused projects stay silent.
function changes(before, after, w) {
  const out = [];
  const old = new Map(before.map((s) => [s.name, s]));
  for (const s of after) {
    const was = old.get(s.name);
    if (!was || s.paused) continue;
    s.problems.forEach((p, i) => { if (!was.problems.includes(p)) out.push({ title: `${s.tag} · ${p}`, level: "error", url: s.problem_urls?.[i] }); });
    // a problem that became known (an operation now covers it) is not fixed: no toast
    was.problems.forEach((p) => { if (!s.problems.includes(p) && !(s.known ?? []).includes(p)) out.push({ title: `${s.tag} · ${w.fixed}`, body: p, level: "success" }); });
    const known = new Set((was.ops_detail ?? []).filter((o) => o.needs_user).map((o) => o.id));
    for (const o of s.ops_detail ?? []) {
      if (o.needs_user && !o.paused && !known.has(o.id)) out.push({ title: `${s.tag} · ${w.you}`, body: o.waiting_for || o.what, level: "warning" });
    }
    if (s.release?.open && was.release && !was.release.open) out.push({ title: `${s.tag} · ${w.window_open}`, level: "info" });
  }
  return out;
}

export default class GeniusStatus extends View {
  init(_props, cx) {
    this.section = props()?.section ?? "projects";
    this.resolving = {};                           // PR-conflict rows whose «resolve» was just pressed
    this.open = {};                                // ops shelf: unfolded projects and operations
    this.links = [];                               // toast n → the URL its click opens
    cx.spawn(async (cx) => {
      const raw = await readTextFile(`${appHome()}/mods/genius-status/root.json`);
      try { this.root = JSON.parse(raw ?? "").root; } catch { this.root = null; }
      if (this.root) {
        try { this.lang = JSON.parse((await readTextFile(`${this.root}/genius.config.json`)) ?? "{}").language; } catch { /* en */ }
      }
      cx.notify();
      if (this.section === "done" && this.root) {
        cx.timer.every(30000, (cx) => cx.notify());   // the projects copy reads the data; this copy re-renders
        return;
      }
      if (this.section === "models" && this.root) {
        cx.timer.every(EVERY_EXTRA, (cx) => this.models(cx));
        this.models(cx);
        return;
      }
      if (this.section !== "projects" || !this.root) return;
      cx.timer.every(EVERY, (cx) => this.refresh(cx));
      cx.timer.every(EVERY_EXTRA, (cx) => this.extra(cx, true));
      // a reinstall reloads the mod in the middle of a read: its «busy» mark would block the next one
      setShared(KEY, { ...(shared(KEY) ?? {}), busy: 0 });
      this.refresh(cx);
      this.extra(cx, true);
      cx.spawn(async (cx) => {                     // an operation added, advanced or closed shows at once
        const dir = `${this.root}/state`;         // other files there change often (claude-status.json): ignore them
        for (;;) {
          const paths = await watchFolder(dir);
          if (paths.some((p) => p === dir || p.endsWith("operations.json"))) this.refresh(cx);
          if (paths.some((p) => p === dir || /(focus|heavy|agents-watch|operations|operations\.closed)\.json$/.test(p))) this.extra(cx, false);
        }
      });
      cx.spawn(async () => {                       // a click on a toast: "open-<n>"
        for (;;) { const c = await nextCommand(); const m = /^open-(\d+)$/.exec(c ?? ""); if (m) openUrl(this.links[Number(m[1])]); }
      });
    });
  }

  words() { return WORDS[this.lang] ?? WORDS.en; }

  refresh(cx) {
    const now = shared(KEY) ?? {};
    if (!this.root || (now.busy && Date.now() - now.busy < 120000)) return;
    setShared(KEY, { ...now, busy: Date.now() });
    cx.spawn(async (cx) => {
      let next;
      try {
        const r = await exec(`${this.root}/bin/status`, ["--json"], { cwd: this.root, timeout: 120000 });
        if (r.code === 0) {
          const states = JSON.parse(r.stdout);
          if (this.section === "projects" && now.states) this.toast(changes(now.states, states, this.words()));
          next = { states, at: Date.now() };
        } else {
          next = { ...shared(KEY), error: (r.stderr || `exit ${r.code}`).trim().split("\n").pop() };
        }
      } catch (e) {
        next = { ...shared(KEY), error: String(e?.message ?? e) };
      }
      setShared(KEY, { ...next, busy: 0 });
      cx.notify();
    });
  }

  // The cheap data, read from state/ (no network): the focus of the day, the heavy queue, and when
  // each sub-agent's screen last changed (the watcher's file). `clean` asks bin/heavy itself, which
  // drops a holder or a waiter whose process died (after a reboot the file alone would lie).
  extra(cx, clean) {
    if (!this.root) return;
    cx.spawn(async (cx) => {
      const dir = `${this.root}/state`;
      let heavy = null;
      if (clean) {
        try { const r = await exec(`${this.root}/bin/heavy`, ["--json"], { cwd: this.root, timeout: 15000 }); if (r.code === 0) heavy = parse(r.stdout, null); } catch { /* the file below */ }
      }
      heavy = heavy ?? parse(await readTextFile(`${dir}/heavy.json`), {});
      let focus = {};
      try {                                   // the focus plus the day's work from the board (bin/genius focus --json)
        const r = await exec(`${this.root}/bin/genius`, ["focus", "--json"], { cwd: this.root, timeout: 15000 });
        if (r.code === 0) focus = parse(r.stdout, {});
      } catch { focus = parse(await readTextFile(`${dir}/focus.json`), {}); }
      const watch = parse(await readTextFile(`${dir}/agents-watch.json`), {});
      setShared(EXTRA, { heavy, focus, watch });
      cx.notify();
    });
  }

  models(cx) {
    cx.spawn(async (cx) => {
      try {
        const r = await exec(`${this.root}/bin/genius`, ["models", "--json"], { cwd: this.root, timeout: 30000 });
        if (r.code === 0) setShared(MODELS, parse(r.stdout, {}));
      } catch { /* the last answer stays */ }
      cx.notify();
    });
  }

  doneRows(t) {
    const f = ((shared(EXTRA) ?? {}).focus ?? {});
    const steps = f.date === today() ? (f.steps ?? []).filter((st) => st.done).map((st) => `🎯 ${st.text}`) : [];
    const done = [...[...(f.done_today ?? [])].reverse(), ...steps.reverse()];
    if (!done.length) return [div().px(12).py(8).text_size(12).text_color(t.muted).child("—")];
    return done.map((x, i) => div().id(`dn-${i}`).px(12).py(3).text_size(11.5).text_color(t.dim).truncate().tooltip(x).child(`✅ ${x}`));
  }

  // One row per provider: a dot (green: fine; amber: over 70 %; red: over 90 % or down), its name and numbers.
  modelRows(t) {
    const data = shared(MODELS) ?? {};
    const rows = [];
    for (const p of data.providers ?? []) {
      const lims = Object.entries(p.limits ?? {});
      const worst = Math.max(0, ...lims.map(([, l]) => l.used ?? 0));
      let text, tip = "";
      let color = worst >= 90 ? t.blocked : worst >= 70 ? t.working : t.done;
      if (p.name === "Ollama") {
        color = p.up ? t.done : t.dim;
        const u = p.cloud?.usage ?? {};
        const b = p.cloud?.balance ?? {};
        const left = b.left_usd != null && b.allowance_usd ? Math.round((1 - b.left_usd / b.allowance_usd) * 100) : null;
        if (left != null) color = left >= 90 ? t.blocked : left >= 70 ? t.working : t.done;
        text = [p.up ? "" : "down", u.requests != null ? `24h ${u.requests} req · $${(u.usd ?? 0).toFixed(2)}` : "",
          b.left_usd != null ? `$${b.left_usd.toFixed(0)}/${b.allowance_usd}` : "", p.key ? "" : "no key"].filter(Boolean).join(" · ");
        tip = [...(p.loaded ?? []).map((m) => `in memory: ${m}`), u.requests != null ? `24h: ${u.requests} requests, $${(u.usd ?? 0).toFixed(2)}, input ${u.input} (cached ${u.cached}), output ${u.output}` : "",
          b.left_usd != null ? `left $${b.left_usd} of $${b.allowance_usd} until ${b.until}` : "",
          p.key ? "" : "OLLAMA_API_KEY not set: cloud usage unknown"].filter(Boolean).join("\n");
      } else {
        text = lims.length ? lims.map(([k, l]) => `${k} ${l.used}%`).join(" · ") : "no data yet";
        if (!lims.length) color = t.dim;
        tip = lims.map(([k, l]) => `${k}: ${l.used}%${l.resets ? ` → ${hhmm(l.resets * 1000)}` : ""}`).join("\n") || p.model || "";
      }
      rows.push(h_flex().id(`m-${p.name}`).px(12).py(4).gap(8).items_center().when(Boolean(tip), (el) => el.tooltip(tip))
        .child(div().flex_none().size(7).rounded(4).bg(color))
        .child(div().flex_none().w(52).text_size(12.5).text_color(t.foreground).child(p.name))
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.muted).child(text)));
    }
    return rows.length ? rows : [div().px(12).py(8).text_size(12).text_color(t.muted).child("…")];
  }

  // An answer button: the answer is stored in the operation and typed into the genius-ai chat.
  answer(o, text, w, cx) {
    const chat = this.chat();
    if (!chat) { notify({ title: w.no_chat, level: "warning" }); return; }
    cx.spawn(async (cx) => {
      try {
        await exec(`${this.root}/bin/genius`, ["ops", "answer", o.id, text], { cwd: this.root, timeout: 20000 });
        await exec("hc", ["send", chat.address, `${w.answer_text} «${o.ask || o.what}» (${o.id}): ${text}`]);
      } catch { /* the next read shows the truth */ }
      this.refresh(cx);
    });
  }

  // «later»: the operation is paused and leaves Attention; nothing is sent.
  later(o, cx) {
    cx.spawn(async (cx) => {
      try { await exec(`${this.root}/bin/genius`, ["ops", "later", o.id], { cwd: this.root, timeout: 20000 }); } catch { /* the next read shows it */ }
      this.refresh(cx);
    });
  }

  // «resolve» on a PR-conflict row: the same command as in the chat. The row shows «resolving…» at once, then
  // from the operation prfix-<N>; a failure is a toast with the script's own message.
  resolve(r, w, cx) {
    const d = r.data;
    if (!d || this.resolving[r.id]) return;
    this.resolving[r.id] = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const x = await exec(`${this.root}/bin/genius`, ["pr", "resolve", d.repo, String(d.number)], { cwd: this.root, timeout: 120000 });
        if (x.code !== 0) notify({ title: w.resolve_fail, body: (x.stderr || x.stdout || "").trim().slice(0, 300), level: "error" });
      } catch { notify({ title: w.resolve_fail, level: "error" }); }
      delete this.resolving[r.id];
      this.refresh(cx);
    });
  }

  // The genius-ai chat: the agent whose terminal is in the genius-ai folder (not a sub-agent).
  chat() { const all = agentsIn(this.root); return all.find((a) => !a.sub) ?? all[0]; }

  // ⏸/▶ on a card: the same command as in the chat, then a fresh read.
  pause(s, cx) {
    cx.spawn(async (cx) => {
      try { await exec(`${this.root}/bin/genius`, [s.paused ? "resume" : "pause", s.name], { cwd: this.root, timeout: 20000 }); } catch { /* the next read shows it */ }
      this.refresh(cx);
    });
  }

  toast(list) {
    for (const c of list.slice(0, 5)) {
      const n = { title: c.title, body: c.body, level: c.level };
      if (c.url) { this.links.push(c.url); n.action = { name: `genius-status.open-${this.links.length - 1}` }; }
      notify(n);
    }
  }

  ask(o, w) {
    const chat = this.chat();
    if (!chat) { notify({ title: w.no_chat, level: "warning" }); return; }
    exec("hc", ["send", chat.address, `${w.ask_text} «${o.what}» (${o.id})`]);
    openAgent(chat);
  }

  // Each shelf has its own color and count: attention red (green when empty), projects accent, operations amber.
  header(t, w, data, count) {
    const when = data.busy ? w.loading : data.at ? `${w.at} ${hhmm(data.at)}` : w.never;
    const color = this.section === "done" ? t.done
      : this.section === "models" ? t.accent
      : this.section === "requests" ? (count ? t.blocked : t.muted)
      : this.section === "attention" ? (count ? t.blocked : t.done)
      : this.section === "ops" ? t.working : t.accent;
    return h_flex().px(12).py(8).gap(8).items_center().border_b(1).border_color(t.border)
      .child(div().w(3).h(14).rounded(2).bg(color))
      .child(div().text_size(11.5).font_semibold().text_color(color).child(TITLES[this.section]))
      .child(div().px(6).rounded(8).bg(t.raised).text_size(11).text_color(t.foreground).child(String(count)))
      .child(div().flex_1())
      .when(this.section === "projects", (el) => el.child(div().text_size(11).text_color(t.dim).child(when)))
      .when(!["requests", "models", "done"].includes(this.section), (el) => el.child(div().id(`refresh-${this.section}`).px(4).rounded(4).text_size(12).text_color(t.dim).cursor_pointer()
        .hover((s) => s.bg(t.hover).text_color(t.foreground)).tooltip(when)
        .on_click((_e, cx) => this.refresh(cx)).child("↻")));
  }

  // What needs the user: problems, then the waits for the user's own action. Paused projects are left out.
  attentionRows(states) {
    const live = states.filter((s) => !s.paused);
    const problems = live.flatMap((s) => s.problems.map((text, i) => ({ id: `${s.name}-p${i}`, tag: s.tag, text, url: s.problem_urls?.[i],
      // structured problem (bin/status `problem_data`): a PR conflict has the «resolve» button; «resolving…» while its operation prfix-<N> exists
      data: s.problem_data?.[i] ?? null,
      busy: (s.problem_data?.[i]?.kind === "pr_conflict" && (s.ops_detail ?? []).some((o) => o.id === `prfix-${s.problem_data[i].number}`)) || undefined })));
    // Only what waits for the user: 👤 (and ⚡ a restart cut), by the project's rank. Paused stay out.
    const byRank = [...live].sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99));
    const pick = (test, kind) => byRank.flatMap((s) => (s.ops_detail ?? []).filter((o) => !o.paused && test(o))
      .map((o) => ({ id: `${s.name}-${o.id}`, tag: s.tag, op: o, kind })));
    const cut = (o) => o.kind === "interrupted";
    const waits = [
      ...pick((o) => o.needs_user && cut(o), "cut"),                    // a restart stopped the agent: first
      ...pick((o) => o.needs_user && !cut(o), "you"),
      // In work and next tasks are not shown here (the user's decision of 2026-11-18: «якщо просто черга
      // і вона У РОБОТІ і НЕ ЧЕКАЄ мене — не логічно це тут показувати»). They stay on the project cards.
    ];
    return { problems, waits };
  }

  // Requests of agents and sub-agents, live from Herdr: blocked (waits for an answer) first, then
  // done (finished while the user was elsewhere: a result or a question to read).
  requestRows(states) {
    // A sub-agent's "done" is news too: a visible sub-agent (`hc run`) does not report to its
    // parent by itself, so its result and its questions would wait unseen (2026-11-12).
    const rank = { blocked: 0, done: 1 };
    return everyAgent().filter((a) => a.status === "blocked" || a.status === "done")
      .sort((a, b) => rank[a.status] - rank[b.status] || b.seq - a.seq)
      .map((a) => ({ agent: a, tag: projectStates(states).find((s) => inside(a.cwd, s.path))?.tag ?? a.where }));
  }

  requests(t, w, rows) {
    if (!rows.length) return [div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.no_requests)];
    return rows.map((r) => v_flex().id(`r-${r.agent.address}`).px(12).py(5).gap(2).cursor_pointer().hover((s) => s.bg(t.hover))
      .tooltip(w.to_agent).on_click(() => openAgent(r.agent))
      .child(h_flex().gap(8).text_size(12.5)
        .child(div().flex_none().text_color(t.foreground).child(r.tag))
        .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(`${r.agent.sub ? "↳ " : ""}🤖 ${r.agent.title}`)))
      .child(div().text_size(11.5).text_color(r.agent.status === "blocked" ? t.blocked : t.done)
        .child(r.agent.status === "blocked" ? (r.agent.msg || w.agent_asks) : w.agent_done)));
  }

  // «Now»: the focus of the day and its steps. Only on its own date: yesterday's focus is not shown.
  now(t) {
    const f = (shared(EXTRA) ?? {}).focus ?? {};
    const mine = f.date === today();
    const work = f.in_work ?? [];
    const open = mine ? (f.steps ?? []).map((st, i) => ({ ...st, n: i + 1 })).filter((st) => !st.done) : [];
    if (!(mine && f.focus) && !open.length && !work.length) return [];
    const line = (id, text, color) => div().id(id).text_size(12).text_color(color).truncate().tooltip(text).child(text);
    return [v_flex().px(12).py(7).gap(3).border_b(1).border_color(t.border)
      .when(mine && Boolean(f.focus), (el) => el.child(div().text_size(12.5).font_semibold().text_color(t.accent).child(`🎯 ${f.focus}`)))
      // only the steps still open: a finished step moves to «Done today»
      .children(open.map((st) => div().text_size(12).text_color(st.now ? t.foreground : t.muted)
        .child(`${st.now ? "▶" : "○"} ${st.n}. ${st.text}`)))
      // the work going on: the task, and under it its agent — model, what it does right now, how long its
      // screen is quiet; a click opens the agent. What was closed today has its own shelf («Done today»).
      .children(work.map((x, i) => {
        const item = typeof x === "string" ? { text: x } : x;
        const a = everyAgent().find((g) => g.title === item.agent);
        const quiet = minutes(((shared(EXTRA) ?? {}).watch ?? {})[item.agent]?.changed);
        const what = a ? (a.msg || a.status || "") : "агент не запущений";
        const sub = [item.agent ? `🤖 ${item.agent}` : "", a?.model ? a.model.split(/[:]/)[0] : "", what,
          quiet != null && quiet >= QUIET_FROM ? `${quiet} ${this.words().min}` : ""].filter(Boolean).join(" · ");
        return v_flex().id(`nw-${i}`).gap(1).cursor_pointer().hover((s) => s.bg(t.hover)).rounded(4)
          .tooltip(`${item.text}\n${item.waiting_for ?? ""}`)
          .on_click(() => { if (a) openAgent(a); })
          .child(div().text_size(12).text_color(t.foreground).truncate().child(`▶ ${item.text}`))
          .when(Boolean(sub), (el) => el.child(div().pl(14).text_size(11).text_color(a?.status === "blocked" ? t.blocked : t.dim).truncate().child(sub)));
      }))];
  }

  attention(t, w, rows) {
    const focus = this.now(t);
    if (!rows.problems.length && !rows.waits.length) return [...focus, div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.ok)];
    const button = (id, text, tip, color, act) => div().id(id).flex_none().px(8).py(2).rounded(6).bg(t.raised)
      .text_size(11.5).text_color(color).cursor_pointer().hover((s) => s.bg(t.hover).text_color(t.foreground))
      .when(Boolean(tip), (el) => el.tooltip(tip))
      .on_click((_e, cx) => { cx.stop_propagation(); act(cx); }).child(text);
    return [
      ...focus,
      ...rows.problems.map((r) => h_flex().id(`a-${r.id}`).px(12).py(5).gap(8).text_size(12.5)
        .when(Boolean(r.url), (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)).tooltip(r.url).on_click(() => openUrl(r.url)))
        .child(div().flex_none().text_color(t.foreground).child(r.tag))
        .child(div().flex_1().min_w(0).text_color(t.blocked).child(r.text))
        .when(r.data?.kind === "pr_conflict", (el) => (r.busy || this.resolving[r.id])
          ? el.child(div().flex_none().text_size(11.5).text_color(t.working).child(LABELS.resolving))
          : el.child(button(`ar-${r.id}`, LABELS.resolve, w.resolve_tip, t.foreground, (cx) => this.resolve(r, w, cx))))),
      ...rows.waits.map((r) => {
        const o = r.op;
        const mine = r.kind === "you" || r.kind === "cut";             // the user's own move
        const full = Boolean(this.open[r.id]);
        const mark = r.kind === "cut" ? "⚡" : r.kind === "you" ? "👤" : r.kind === "work" ? "🔄" : "📋";
        // the question in full when the operation has one; else what it waits for, one line until unfolded
        const line = mine && o.ask ? o.ask : o.answered ? `↩ ${w.answered}: ${o.answered.text}` : o.waiting_for;
        const wrap = full || (mine && Boolean(o.ask));
        return v_flex().id(`a-${r.id}`).px(12).py(mine ? 7 : 5).gap(mine ? 4 : 2).cursor_pointer().hover((s) => s.bg(t.hover))
          .when(!full, (el) => el.tooltip(w.more))
          .on_click((_e, cx) => { this.open[r.id] = !full; cx.notify(); })
          .child(h_flex().gap(8).text_size(12.5)
            .child(div().flex_none().text_color(t.foreground).child(r.tag))
            .child(div().flex_1().min_w(0).when(!full, (el) => el.truncate()).text_color(t.foreground).child(o.what))
            .child(div().id(`aa-${r.id}`).flex_none().px(4).rounded(4).cursor_pointer().hover((s) => s.bg(t.raised))
              .tooltip(w.ask).on_click((_e, cx) => { cx.stop_propagation(); this.ask(o, w); }).child("💬")))
          .when(Boolean(line), (el) => el.child(div().text_size(mine ? 12 : 11.5).when(!wrap, (e2) => e2.truncate())
            .text_color(r.kind === "cut" ? t.blocked : mine ? t.working : t.dim).child(`${mark} ${line}`)))
          .when(full && Boolean(o.waiting_for) && line !== o.waiting_for, (el) => el.child(div().text_size(11.5).text_color(t.muted)
            .child(`${w.waits}: ${o.waiting_for}`)))
          .when(full && Boolean(o.next), (el) => el.child(div().text_size(11.5).text_color(t.dim).child(`${w.next}: ${o.next}`)))
          .when(mine, (el) => el.child(h_flex().flex_wrap().gap(6).pt(1)
            .children((o.answers ?? []).map((text, i) => button(`ab-${r.id}-${i}`, text, null, t.foreground, (cx) => this.answer(o, text, w, cx))))
            .child(button(`al-${r.id}`, `⏸ ${LABELS.later}`, w.later_tip, t.dim, (cx) => this.later(o, cx)))));
      }),
    ];
  }

  projects(t, w, states) {
    const dot = (color) => div().flex_none().size(7).rounded(4).bg(color);
    const status = { working: t.working, blocked: t.blocked, done: t.done, idle: t.idle };
    const extra = shared(EXTRA) ?? {};
    const watch = extra.watch ?? {};
    return [...this.heavy(t, w, extra.heavy), ...states.map((s) => {
      const space = spaceOf(s.path);
      const mine = agentsIn(s.path);
      const ops = s.ops_detail ?? [];
      const you = ops.filter((o) => o.needs_user && !o.paused).length;
      const work = ops.filter((o) => !o.needs_user && !o.paused).length;
      const parked = ops.filter((o) => o.paused).length;
      const goalOf = (a) => ops.find((o) => o.agent && o.agent === a.title)?.what || a.goal || "";
      const chip = (id, text, color, url) => div().id(id).text_size(11.5).text_color(color)
        .when(Boolean(url), (el) => el.cursor_pointer().hover((st) => st.text_color(t.accent))
          .on_click((_e, cx) => { cx.stop_propagation(); openUrl(url); }))
        .child(text);
      return v_flex().id(`p-${s.name}`).mx(8).my(3).px(10).py(7).gap(4).rounded(6).cursor_pointer()
        .bg(space?.active ? t.selected : t.surface).hover((st) => st.bg(t.hover))
        .when(s.paused, (el) => el.opacity(0.4))
        .tooltip(`${s.path} · ${space ? w.open_space : w.new_space}`)
        .on_click(() => space ? run("space.select", { id: space.id }) : run("space.new", { name: s.tag, folder: s.path }))
        .child(h_flex().gap(8).items_center()
          .child(div().text_size(13).font_semibold().text_color(t.foreground).child(s.tag))
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.dim).child(s.spoken))
          .when(Boolean(s.release), (el) => el.child(div().px(6).rounded(8).text_size(10.5)
            .bg(t.raised).text_color(s.release.open ? t.done : t.dim)
            .child(s.release.open ? `🚀 ${w.release_open}` : `🚫 ${w.release_closed} ${s.release.text}`)))
          .when(!s.paused && Boolean(s.hold), (el) => el.child(div().id(`hold-${s.name}`).px(6).rounded(8).text_size(10.5)
            .bg(t.raised).text_color(t.working).tooltip(s.hold?.why || w.hold_tip).child(`✋ ${LABELS.hold}`)))
          .when(s.paused, (el) => el.child(div().text_size(10.5).text_color(t.dim).child(LABELS.paused)))
          .child(div().id(`pause-${s.name}`).px(4).rounded(4).text_size(11).text_color(t.dim).cursor_pointer()
            .hover((st) => st.bg(t.raised).text_color(t.foreground)).tooltip(s.paused ? w.resume : w.pause)
            .on_click((_e, cx) => { cx.stop_propagation(); this.pause(s, cx); }).child(s.paused ? "▶" : "⏸"))
          .when(!s.paused, (el) => el.child(dot(s.problems.length ? t.blocked : t.done))))
        .when(!s.paused && s.info.length > 0, (el) => el.child(h_flex().flex_wrap().gap_x(8).gap_y(2).children(
          s.info.map((x, i) => chip(`i-${s.name}-${i}`, x, t.muted, x.startsWith("PR ") ? s.links?.prs : null)))))
        .when((s.urls ?? []).length > 0, (el) => el.child(h_flex().flex_wrap().gap_x(10).gap_y(2).children(
          s.urls.map((u, i) => h_flex().id(`u-${s.name}-${i}`).gap(5).items_center().cursor_pointer()
            .tooltip(`${u.url} · ${u.code} · ${u.ms} ms`).on_click((_e, cx) => { cx.stop_propagation(); openUrl(u.url); })
            .child(dot(u.ok ? t.done : t.blocked))
            .child(div().text_size(11.5).text_color(u.ok ? t.muted : t.blocked).child(`${short(u.name)} ${u.ms} ms`))))))
        .when(!s.paused && (s.notes ?? []).length > 0, (el) => el.child(v_flex().gap(1).children(
          s.notes.map((x) => div().text_size(11.5).text_color(t.working).child(`📌 ${x}`)))))
        // one line per agent: its name, its goal, and for how long its screen has not changed
        .when(!s.paused && mine.length > 0, (el) => el.child(v_flex().gap(2).children(
          mine.map((a) => {
            const goal = goalOf(a);
            const quiet = minutes(watch[a.title]?.changed);
            const stuck = a.status === "working" && quiet != null && quiet >= QUIET_ALARM;
            return h_flex().id(`g-${a.address}`).gap(5).items_center().cursor_pointer()
              .tooltip(`${a.status} · ${a.title}${goal ? ` · ${goal}` : ""}`).on_click((_e, cx) => { cx.stop_propagation(); openAgent(a); })
              .child(dot(stuck ? t.blocked : status[a.status] ?? t.unknown))
              .child(div().flex_none().max_w(150).truncate().text_size(11.5).text_color(a.status === "blocked" ? t.blocked : t.muted)
                .child(`${a.sub ? "↳ " : ""}🤖 ${a.title}`))
              .when(Boolean(a.model), (e2) => e2.child(div().flex_none().px(5).rounded(6).bg(t.raised).text_size(10.5)
                .text_color(t.accent).tooltip(a.model).child(a.model.split(/[:-]/).slice(0, 2).join("-"))))
              .child(div().flex_1().min_w(0).truncate().text_size(11).text_color(t.dim).child(goal))
              .when(quiet != null && quiet >= QUIET_FROM, (e2) => e2.child(div().id(`q-${a.address}`).flex_none().text_size(11)
                .text_color(stuck ? t.blocked : t.dim).tooltip(w.quiet).child(`${quiet} ${w.min}`)));
          }))))
        .when(!s.paused && ops.length > 0, (el) => el.child(h_flex().gap(10).text_size(11.5)
          .when(you > 0, (e2) => e2.child(div().id(`cy-${s.name}`).text_color(t.working).tooltip(w.you_tip).child(`👤 ${you}`)))
          .when(work > 0, (e2) => e2.child(div().id(`cw-${s.name}`).text_color(t.muted).tooltip(w.work_tip).child(`🔄 ${work}`)))
          .when(parked > 0, (e2) => e2.child(div().id(`cp-${s.name}`).text_color(t.dim).tooltip(w.parked_tip).child(`⏸ ${parked}`)))));
    })];
  }

  // The heavy queue (bin/heavy): who runs now and for how long, and who waits. Nothing when it is free.
  heavy(t, w, q) {
    const quiet = q?.quiet?.[0];
    if (!q?.holder && !(q?.waiting ?? []).length && !(q?.ml ?? []).length && !quiet) return [];
    const row = (id, mark, x, color, verb) => h_flex().id(id).gap(6).text_size(11.5).tooltip(x.cmd || "")
      .child(div().flex_none().text_color(color).child(`${mark} ${x.who}`))
      .child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(x.cmd || ""))
      .child(div().flex_none().text_color(color).child(`${verb} ${minutes(x.since) ?? 0} ${w.min}`));
    return [v_flex().mx(8).my(3).px(10).py(7).gap(3).rounded(6).bg(t.surface)
      .child(div().text_size(11.5).font_semibold().text_color(t.working).child(`⚙ ${LABELS.heavy}`))
      .when(Boolean(quiet), (el) => el.child(div().text_size(11.5).text_color(t.working).child(`🔕 ${quiet} → ${q.quiet[1]}`)))
      .children((q.ml ?? []).map((m, i) => h_flex().id(`hv-ml${i}`).gap(6).text_size(11.5).tooltip(m.cmd)
        .child(div().flex_none().text_color(t.blocked).child(`🧠 ${m.gb} GB`))
        .child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(m.cmd))))
      .children((q.holders ?? (q.holder ? [q.holder] : [])).map((x, i) => row(`hv-run${i}`, "▶", x, t.foreground, w.runs)))
      .children((q.waiting ?? []).map((x, i) => row(`hv-w${i}`, "⏳", x, t.muted, w.queued)))];
  }

  // Folded per project: a project row with its counters; a click unfolds its operations, a click on
  // an operation unfolds its full text, what it waits for, the next step and 💬.
  ops(t, w, states) {
    const withOps = states.filter((s) => (s.ops_detail ?? []).length);
    if (!withOps.length) return [div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.none)];
    return withOps.flatMap((s) => {
      const list = s.ops_detail;
      const you = s.paused ? 0 : list.filter((o) => o.needs_user && !o.paused).length;
      const paused = s.paused ? list.length : list.filter((o) => o.paused).length;
      const unfolded = Boolean(this.open[s.name]);
      const head = h_flex().id(`oh-${s.name}`).px(12).py(6).gap(8).items_center().cursor_pointer().hover((st) => st.bg(t.hover))
        .when(s.paused, (el) => el.opacity(0.4))
        .on_click((_e, cx) => { this.open[s.name] = !unfolded; cx.notify(); })
        .child(div().w(10).text_size(10).text_color(t.dim).child(unfolded ? "▾" : "▸"))
        .child(div().text_size(12.5).font_semibold().text_color(t.foreground).child(s.tag))
        .child(div().flex_1())
        .when(you > 0, (el) => el.child(div().text_size(11.5).text_color(t.working).child(`👤 ${you}`)))
        .when(paused > 0, (el) => el.child(div().text_size(11.5).text_color(t.dim).child(`⏸ ${paused}`)))
        .child(div().text_size(11.5).text_color(t.muted).child(`⏳ ${list.length}`));
      if (!unfolded) return [head];
      const order = (o) => (o.needs_user && !o.paused ? 0 : o.paused ? 2 : 1);
      const rows = [...list].sort((a, b) => order(a) - order(b)).map((o) => {
        const key = `${s.name}/${o.id}`;
        const full = Boolean(this.open[key]);
        const mark = o.paused ? "⏸" : o.needs_user ? "👤" : "⏳";
        const age = days(o.started);                                   // an old operation must catch the eye
        return v_flex().id(`o-${key}`).pl(30).pr(12).py(5).gap(2).border_t(1).border_color(t.border).cursor_pointer()
          .when(o.paused || s.paused, (el) => el.opacity(s.paused ? 0.4 : 0.6)).hover((st) => st.bg(t.hover))
          .on_click((_e, cx) => { this.open[key] = !full; cx.notify(); })
          .child(h_flex().gap(8)
            .child(div().flex_1().min_w(0).text_size(12).text_color(t.foreground).when(!full, (el) => el.truncate()).child(`${mark} ${o.what}`))
            .when(age != null && age >= 1, (el) => el.child(div().flex_none().text_size(11).text_color(age >= 3 ? t.working : t.dim).child(`${age} ${w.d}`))))
          .when(Boolean(o.waiting_for), (el) => el.child(div().text_size(11).text_color(o.needs_user && !o.paused ? t.working : t.dim)
            .when(!full, (e2) => e2.truncate()).child(`${w.waits}: ${o.waiting_for}`)))
          .when(full && Boolean(o.next), (el) => el.child(div().text_size(11).text_color(t.muted).child(`${w.next}: ${o.next}`)))
          .when(full, (el) => el.child(div().id(`ask-${key}`).mt(2).text_size(11.5).text_color(t.accent)
            .hover((st) => st.text_color(t.foreground)).on_click((_e, cx) => { cx.stop_propagation(); this.ask(o, w); }).child(w.ask)));
      });
      return [head, ...rows];
    });
  }

  render() {
    const t = theme();
    const w = this.words();
    const data = shared(KEY) ?? {};
    const states = data.states ?? [];
    const rows = this.attentionRows(states);
    const asks = this.section === "requests" ? this.requestRows(states) : [];
    const fx = ((shared(EXTRA) ?? {}).focus ?? {});
    const count = this.section === "done" ? (fx.done_count ?? (fx.done_today ?? []).length) + (fx.date === today() ? (fx.steps ?? []).filter((st) => st.done).length : 0)
      : this.section === "models" ? ((shared(MODELS) ?? {}).providers ?? []).length
      : this.section === "requests" ? asks.length
      : this.section === "attention" ? rows.problems.length + rows.waits.length
      : this.section === "ops" ? states.reduce((n, s) => n + (s.paused ? 0 : (s.ops_detail ?? []).length), 0)
      : projectStates(states).filter((s) => !s.paused).length;                    // paused projects are not counted
    const body = this.root === null ? [div().px(12).py(8).text_size(12).text_color(t.blocked).child(w.noroot)]
      : this.section === "done" ? this.doneRows(t)
      : this.section === "models" ? this.modelRows(t)
      : this.section === "requests" ? this.requests(t, w, asks)
      : this.section === "attention" ? this.attention(t, w, rows)
      : this.section === "ops" ? this.ops(t, w, states)
      : this.projects(t, w, projectStates(states));
    const side = ["requests", "models", "done"].includes(this.section);
    return v_flex().size_full().when(!side, (el) => el.rounded(10)).bg(t.chrome)
      .when(side, (el) => el.border_t(1).border_color(t.border))
      .child(this.header(t, w, data, count))
      .when(Boolean(data.error), (el) => el.child(div().px(12).py(4).text_size(11).text_color(t.blocked).child(data.error)))
      .child(v_flex().id(`body-${this.section}`).flex_1().min_h(0).py(4).overflow_y_scrollbar().children(body));
  }
}
