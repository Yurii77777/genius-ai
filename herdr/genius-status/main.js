// genius-status — genius-ai's live status on the Herdr screen: one shelf per section.
// Installed by `bin/genius herdr install` (copied, not linked: Herdr's hot reload does not see
// through links). The layout `genius` shows three copies in its right column:
//   props.section = "attention"  the workflow: problems (failed CI, URL down) and the operations
//                                that wait for the user's own action ("needs": "user")
//   props.section = "projects"   one card per project: branch, PRs, prod URLs, release window, its
//                                agents, counters; click = its space
//   props.section = "ops"        pending operations (state/operations.json), folded per project
// and one copy in the left sidebar, under Herdr's agents list:
//   props.section = "requests"   what agents and sub-agents ask of the user, live from Herdr: an
//                                agent blocked on a question, an agent that finished; click = its terminal
// Data: `bin/status --json` of the genius-ai folder named in root.json (written by the installer).
// The "projects" copy refreshes every 5 min and when state/ changes, and posts a toast when
// something changes (a new problem, a problem gone, a new wait for the user, a release window
// opened). ↻ on any shelf refreshes now. Quiet when all is well: a paused project is dimmed and
// never raises attention or a toast.
// Read-only for projects and services. Its only writes: a question typed into the genius-ai chat
// when the user presses 💬 on an operation, and ⏸/▶ on a card (bin/genius pause | resume).

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, readTextFile, watchFolder, appHome, spaces, panes, agents, terminals, run, theme, shared, setShared, props, notify, nextCommand } from "herdr";

const KEY = "genius.status";
const EVERY = 5 * 60 * 1000;
// Section names are always English, like Herdr's own «Spaces» and «Agents»; the content follows the user's language.
const TITLES = { requests: "Requests", attention: "Attention", projects: "Projects", ops: "Operations" };
const WORDS = {
  uk: { no_requests: "запитів немає", agent_done: "закінчив — подивись результат",
        ok: "✅ усе спокійно", none: "нічого не чекає",
        loading: "оновлюю…", never: "ще не оновлено", at: "оновлено", noroot: "не знайдено genius-ai: запустіть bin/genius herdr install",
        waits: "чекає", next: "далі", you: "чекає тебе", paused: "пауза", release_open: "реліз можна", release_closed: "реліз",
        ask: "💬 запитати в чаті", ask_text: "Розкажи про операцію", no_chat: "чат genius-ai не знайдено в Herdr",
        agent_asks: "агент чекає твоєї відповіді", to_agent: "відкрити термінал агента",
        pause: "поставити на паузу", resume: "зняти з паузи", d: "д",
        fixed: "виправлено", window_open: "вікно релізу відкрите", open_space: "відкрити простір", new_space: "створити простір" },
  en: { no_requests: "no requests", agent_done: "finished — see the result",
        ok: "✅ all quiet", none: "nothing is waiting",
        loading: "updating…", never: "not updated yet", at: "updated", noroot: "genius-ai not found: run bin/genius herdr install",
        waits: "waiting for", next: "next", you: "waits for you", paused: "paused", release_open: "release open", release_closed: "release",
        ask: "💬 ask in the chat", ask_text: "Tell me about the operation", no_chat: "the genius-ai chat is not in Herdr",
        agent_asks: "the agent waits for your answer", to_agent: "open the agent's terminal",
        pause: "pause the project", resume: "resume the project", d: "d",
        fixed: "fixed", window_open: "release window is open", open_space: "open the space", new_space: "make a space" },
};

const hhmm = (ms) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
const inside = (dir, path) => Boolean(dir) && (dir === path || dir.startsWith(path + "/"));
const short = (name) => name.split(" · ").pop();
const days = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? Math.floor((Date.now() - t) / 86400000) : null; };                       // "🌐 LW · сайт" → "сайт"
const openUrl = (url) => { if (url) exec("open", [url]); };

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
  const top = agents().map((a) => ({ address: a.address, status: a.status, seq: a.seq, sub: false, cwd: cwd.get(a.pane),
    where: a.space_name, title: a.title || a.program?.name || a.address, msg: said.get(a.address) }));
  const seen = new Set(top.map((a) => a.address));
  const subs = all.filter((k) => k.parent && k.state === "live" && k.status && !seen.has(k.address))
    .map((k) => ({ address: k.address, status: k.status, seq: 0, sub: true, cwd: k.cwd, where: k.name || k.address,
      title: k.name || k.address, msg: k.report?.msg }));
  return [...top, ...subs];
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
    this.open = {};                                // ops shelf: unfolded projects and operations
    this.links = [];                               // toast n → the URL its click opens
    cx.spawn(async (cx) => {
      const raw = await readTextFile(`${appHome()}/mods/genius-status/root.json`);
      try { this.root = JSON.parse(raw ?? "").root; } catch { this.root = null; }
      if (this.root) {
        try { this.lang = JSON.parse((await readTextFile(`${this.root}/genius.config.json`)) ?? "{}").language; } catch { /* en */ }
      }
      cx.notify();
      if (this.section !== "projects" || !this.root) return;
      cx.timer.every(EVERY, (cx) => this.refresh(cx));
      this.refresh(cx);
      cx.spawn(async (cx) => {                     // an operation added, advanced or closed shows at once
        const dir = `${this.root}/state`;         // other files there change often (claude-status.json): ignore them
        for (;;) { const paths = await watchFolder(dir); if (paths.some((p) => p === dir || p.endsWith("operations.json"))) this.refresh(cx); }
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

  // The genius-ai chat: the agent whose terminal is in the genius-ai folder.
  ask(o, w) {
    const chat = agentsIn(this.root)[0];
    if (!chat) { notify({ title: w.no_chat, level: "warning" }); return; }
    exec("hc", ["send", chat.address, `${w.ask_text} «${o.what}» (${o.id})`]);
    run("agent.focus", { terminal: chat.address });
  }

  // Each shelf has its own color and count: attention red (green when empty), projects accent, operations amber.
  header(t, w, data, count) {
    const when = data.busy ? w.loading : data.at ? `${w.at} ${hhmm(data.at)}` : w.never;
    const color = this.section === "requests" ? (count ? t.blocked : t.muted)
      : this.section === "attention" ? (count ? t.blocked : t.done)
      : this.section === "ops" ? t.working : t.accent;
    return h_flex().px(12).py(8).gap(8).items_center().border_b(1).border_color(t.border)
      .child(div().w(3).h(14).rounded(2).bg(color))
      .child(div().text_size(11.5).font_semibold().text_color(color).child(TITLES[this.section]))
      .child(div().px(6).rounded(8).bg(t.raised).text_size(11).text_color(t.foreground).child(String(count)))
      .child(div().flex_1())
      .when(this.section === "projects", (el) => el.child(div().text_size(11).text_color(t.dim).child(when)))
      .when(this.section !== "requests", (el) => el.child(div().id(`refresh-${this.section}`).px(4).rounded(4).text_size(12).text_color(t.dim).cursor_pointer()
        .hover((s) => s.bg(t.hover).text_color(t.foreground)).tooltip(when)
        .on_click((_e, cx) => this.refresh(cx)).child("↻")));
  }

  // What needs the user: problems, then the waits for the user's own action. Paused projects are left out.
  attentionRows(states) {
    const live = states.filter((s) => !s.paused);
    const problems = live.flatMap((s) => s.problems.map((text, i) => ({ id: `${s.name}-p${i}`, tag: s.tag, text, url: s.problem_urls?.[i] })));
    const waits = live.flatMap((s) => (s.ops_detail ?? []).filter((o) => o.needs_user && !o.paused).map((o) => ({ id: `${s.name}-${o.id}`, tag: s.tag, op: o })));
    return { problems, waits };
  }

  // Requests of agents and sub-agents, live from Herdr: blocked (waits for an answer) first, then
  // done (finished while the user was elsewhere: a result or a question to read).
  requestRows(states) {
    // A sub-agent's "done" is not news: it reports to its parent, and the parent brings the result.
    const rank = { blocked: 0, done: 1 };
    return everyAgent().filter((a) => a.status === "blocked" || (a.status === "done" && !a.sub))
      .sort((a, b) => rank[a.status] - rank[b.status] || b.seq - a.seq)
      .map((a) => ({ agent: a, tag: states.find((s) => inside(a.cwd, s.path))?.tag ?? a.where }));
  }

  requests(t, w, rows) {
    if (!rows.length) return [div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.no_requests)];
    return rows.map((r) => v_flex().id(`r-${r.agent.address}`).px(12).py(5).gap(2).cursor_pointer().hover((s) => s.bg(t.hover))
      .tooltip(w.to_agent).on_click(() => run("agent.focus", { terminal: r.agent.address }))
      .child(h_flex().gap(8).text_size(12.5)
        .child(div().flex_none().text_color(t.foreground).child(r.tag))
        .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(`${r.agent.sub ? "↳ " : ""}🤖 ${r.agent.title}`)))
      .child(div().text_size(11.5).text_color(r.agent.status === "blocked" ? t.blocked : t.done)
        .child(r.agent.status === "blocked" ? (r.agent.msg || w.agent_asks) : w.agent_done)));
  }

  attention(t, w, rows) {
    if (!rows.problems.length && !rows.waits.length) return [div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.ok)];
    return [
      ...rows.problems.map((r) => h_flex().id(`a-${r.id}`).px(12).py(5).gap(8).text_size(12.5)
        .when(Boolean(r.url), (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)).tooltip(r.url).on_click(() => openUrl(r.url)))
        .child(div().flex_none().text_color(t.foreground).child(r.tag))
        .child(div().flex_1().min_w(0).text_color(t.blocked).child(r.text))),
      ...rows.waits.map((r) => v_flex().id(`a-${r.id}`).px(12).py(5).gap(2).tooltip(`${r.op.what}\n${w.waits}: ${r.op.waiting_for}`)
        .child(h_flex().gap(8).text_size(12.5)
          .child(div().flex_none().text_color(t.foreground).child(r.tag))
          .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(r.op.what))
          .child(div().id(`aa-${r.id}`).flex_none().px(4).rounded(4).cursor_pointer().hover((s) => s.bg(t.hover))
            .tooltip(w.ask).on_click(() => this.ask(r.op, w)).child("💬")))
        .when(Boolean(r.op.waiting_for), (el) => el.child(div().truncate().text_size(11.5).text_color(t.working).child(`👤 ${r.op.waiting_for}`)))),
    ];
  }

  projects(t, w, states) {
    const dot = (color) => div().flex_none().size(7).rounded(4).bg(color);
    const status = { working: t.working, blocked: t.blocked, done: t.done, idle: t.idle };
    return states.map((s) => {
      const space = spaceOf(s.path);
      const mine = agentsIn(s.path);
      const ops = s.ops_detail ?? [];
      const you = ops.filter((o) => o.needs_user && !o.paused).length;
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
          .when(s.paused, (el) => el.child(div().text_size(10.5).text_color(t.dim).child(w.paused)))
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
        .when(!s.paused && mine.length > 0, (el) => el.child(h_flex().flex_wrap().gap_x(10).gap_y(2).children(
          mine.map((a) => h_flex().id(`g-${a.address}`).gap(5).items_center().max_w(220).cursor_pointer()
            .tooltip(`${a.status} · ${a.title}`).on_click((_e, cx) => { cx.stop_propagation(); run("agent.focus", { terminal: a.address }); })
            .child(dot(status[a.status] ?? t.unknown))
            .child(div().min_w(0).truncate().text_size(11.5).text_color(a.status === "blocked" ? t.blocked : t.muted)
              .child(`${a.sub ? "↳ " : ""}🤖 ${a.title}`))))))
        .when(!s.paused && ops.length > 0, (el) => el.child(h_flex().gap(10).text_size(11.5)
          .child(div().text_color(t.working).child(`⏳ ${ops.length}`))
          .when(you > 0, (e2) => e2.child(div().text_color(t.working).child(`👤 ${you}`)))));
    });
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
    const count = this.section === "requests" ? asks.length
      : this.section === "attention" ? rows.problems.length + rows.waits.length
      : this.section === "ops" ? states.reduce((n, s) => n + (s.paused ? 0 : (s.ops_detail ?? []).length), 0)
      : states.filter((s) => !s.paused).length;                    // paused projects are not counted
    const body = this.root === null ? [div().px(12).py(8).text_size(12).text_color(t.blocked).child(w.noroot)]
      : this.section === "requests" ? this.requests(t, w, asks)
      : this.section === "attention" ? this.attention(t, w, rows)
      : this.section === "ops" ? this.ops(t, w, states)
      : this.projects(t, w, states);
    return v_flex().size_full().when(this.section !== "requests", (el) => el.rounded(10)).bg(t.chrome)
      .when(this.section === "requests", (el) => el.border_t(1).border_color(t.border))
      .child(this.header(t, w, data, count))
      .when(Boolean(data.error), (el) => el.child(div().px(12).py(4).text_size(11).text_color(t.blocked).child(data.error)))
      .child(v_flex().id(`body-${this.section}`).flex_1().min_h(0).py(4).overflow_y_scrollbar().children(body));
  }
}
