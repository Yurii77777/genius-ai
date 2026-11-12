// genius-status — genius-ai's live status on the Herdr screen: one shelf per section.
// Installed by `bin/genius herdr install` (copied, not linked: Herdr's hot reload does not see
// through links). The layout `genius` shows three copies in its right column:
//   props.section = "attention"  problems of every project (failed CI, URL down, path missing)
//   props.section = "projects"   one card per project: tag, branch, uncommitted, PRs; click = its space
//   props.section = "ops"        pending operations (state/operations.json) and what each waits for
// Data: `bin/status --json` of the genius-ai folder named in root.json (written by the installer).
// The "projects" copy refreshes every 5 min and when state/ changes; ↻ on any shelf refreshes now.
// Read-only: it never changes a project, a service or the config.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, readTextFile, watchFolder, appHome, spaces, panes, run, theme, shared, setShared, props } from "herdr";

const KEY = "genius.status";
const EVERY = 5 * 60 * 1000;
const WORDS = {
  uk: { attention: "Увага", projects: "Проєкти", ops: "Операції", ok: "✅ проблем немає", none: "нічого не чекає",
        loading: "оновлюю…", never: "ще не оновлено", at: "оновлено", noroot: "не знайдено genius-ai: запустіть bin/genius herdr install",
        waits: "чекає" },
  en: { attention: "Attention", projects: "Projects", ops: "Operations", ok: "✅ no problems", none: "nothing is waiting",
        loading: "updating…", never: "not updated yet", at: "updated", noroot: "genius-ai not found: run bin/genius herdr install",
        waits: "waiting for" },
};

const hhmm = (ms) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };

// The space that works in this project: a terminal of it in the project's folder, else its own folder.
function spaceOf(path) {
  const inside = (dir) => Boolean(dir) && (dir === path || dir.startsWith(path + "/"));
  const ps = panes();
  return spaces().find((s) => {
    const tabs = new Set((s.tabs ?? []).map((t) => t.id));
    return ps.some((p) => tabs.has(p.tab) && inside(p.cwd)) || inside(s.folder);
  });
}

export default class GeniusStatus extends View {
  init(_props, cx) {
    this.section = props()?.section ?? "projects";
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
        for (;;) { await watchFolder(`${this.root}/state`); this.refresh(cx); }
      });
    });
  }

  refresh(cx) {
    const now = shared(KEY) ?? {};
    if (!this.root || (now.busy && Date.now() - now.busy < 120000)) return;
    setShared(KEY, { ...now, busy: Date.now() });
    cx.spawn(async (cx) => {
      let next;
      try {
        const r = await exec(`${this.root}/bin/status`, ["--json"], { cwd: this.root, timeout: 120000 });
        next = r.code === 0 ? { states: JSON.parse(r.stdout), at: Date.now() }
                            : { ...shared(KEY), error: (r.stderr || `exit ${r.code}`).trim().split("\n").pop() };
      } catch (e) {
        next = { ...shared(KEY), error: String(e?.message ?? e) };
      }
      setShared(KEY, { ...next, busy: 0 });
      cx.notify();
    });
  }

  // Each shelf has its own color and count: attention red (green when empty), projects accent, operations amber.
  header(t, w, data, states) {
    const title = w[this.section];
    const when = data.busy ? w.loading : data.at ? `${w.at} ${hhmm(data.at)}` : w.never;
    const count = this.section === "attention" ? states.reduce((n, s) => n + s.problems.length, 0)
      : this.section === "ops" ? states.reduce((n, s) => n + s.ops.length, 0) : states.length;
    const color = this.section === "attention" ? (count ? t.blocked : t.done)
      : this.section === "ops" ? t.working : t.accent;
    return h_flex().px(12).py(8).gap(8).items_center().border_b(1).border_color(t.border)
      .child(div().w(3).h(14).rounded(2).bg(color))
      .child(div().text_size(11.5).font_semibold().text_color(color).child(title.toUpperCase()))
      .child(div().px(6).rounded(8).bg(t.raised).text_size(11).text_color(t.foreground).child(String(count)))
      .child(div().flex_1())
      .when(this.section === "projects", (el) => el.child(div().text_size(11).text_color(t.dim).child(when)))
      .child(div().id(`refresh-${this.section}`).px(4).rounded(4).text_size(12).text_color(t.dim).cursor_pointer()
        .hover((s) => s.bg(t.hover).text_color(t.foreground)).tooltip(when)
        .on_click((_e, cx) => this.refresh(cx)).child("↻"));
  }

  attention(t, w, states) {
    const rows = states.flatMap((s) => s.problems.map((p, i) => ({ id: `${s.name}-${i}`, tag: s.tag, text: p })));
    if (!rows.length) return [div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.ok)];
    return rows.map((r) => h_flex().id(`a-${r.id}`).px(12).py(5).gap(8).text_size(12.5)
      .child(div().flex_none().text_color(t.foreground).child(r.tag))
      .child(div().flex_1().min_w(0).text_color(t.blocked).child(r.text)));
  }

  projects(t, w, states) {
    return states.map((s) => {
      const space = spaceOf(s.path);
      const open = () => space ? run("space.select", { id: space.id })
                               : run("space.new", { name: s.tag, folder: s.path });
      return v_flex().id(`p-${s.name}`).mx(8).my(3).px(10).py(7).gap(3).rounded(6).cursor_pointer()
        .bg(space?.active ? t.selected : t.surface).hover((st) => st.bg(t.hover))
        .tooltip(space ? s.path : `${s.path} · + space`).on_click(open)
        .child(h_flex().gap(8).items_center()
          .child(div().text_size(13).font_semibold().text_color(t.foreground).child(s.tag))
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.dim).child(s.spoken))
          .when(s.problems.length > 0, (el) => el.child(div().size(7).rounded(4).bg(t.blocked)))
          .when(!s.problems.length, (el) => el.child(div().size(7).rounded(4).bg(t.done))))
        .when(s.info.length > 0, (el) => el.child(div().text_size(11.5).text_color(t.muted).child(s.info.join(" · "))))
        .when(s.ops.length > 0, (el) => el.child(div().text_size(11.5).text_color(t.working).child(`⏳ ${s.ops.length}`)));
    });
  }

  ops(t, w, states) {
    const rows = states.flatMap((s) => s.ops.map((what, i) => ({ id: `${s.name}-${i}`, tag: s.tag, what, waits: (s.waiting ?? [])[i] ?? "" })));
    if (!rows.length) return [div().px(12).py(8).text_size(12.5).text_color(t.muted).child(w.none)];
    return rows.map((r) => v_flex().id(`o-${r.id}`).px(12).py(5).gap(2).border_b(1).border_color(t.border)
      .tooltip(r.waits ? `${w.waits}: ${r.waits}` : r.what)
      .child(h_flex().gap(8).text_size(12)
        .child(div().flex_none().text_color(t.foreground).child(r.tag))
        .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(r.what)))
      .when(Boolean(r.waits), (el) => el.child(div().truncate().text_size(11).text_color(t.dim).child(`⏸ ${r.waits}`))));
  }

  render() {
    const t = theme();
    const w = WORDS[this.lang] ?? WORDS.en;
    const data = shared(KEY) ?? {};
    const states = data.states ?? [];
    const body = this.root === null ? [div().px(12).py(8).text_size(12).text_color(t.blocked).child(w.noroot)]
      : this.section === "attention" ? this.attention(t, w, states)
      : this.section === "ops" ? this.ops(t, w, states)
      : this.projects(t, w, states);
    return v_flex().size_full().rounded(10).bg(t.chrome)
      .child(this.header(t, w, data, states))
      .when(Boolean(data.error), (el) => el.child(div().px(12).py(4).text_size(11).text_color(t.blocked).child(data.error)))
      .child(v_flex().id(`body-${this.section}`).flex_1().min_h(0).py(4).overflow_y_scrollbar().children(body));
  }
}
