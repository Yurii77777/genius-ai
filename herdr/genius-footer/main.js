// genius-footer — Herdr's status bar (machine, branch, agents) plus live CPU and RAM of this Mac.
// Herdr's own workspace-status is read-only, so this is our copy of it; the layout `genius` shows it.
// CPU: iostat over 1 s (user + system). RAM: vm_stat (active + wired + compressed) of hw.memsize,
// the same as Activity Monitor's "Memory Used". Every 5 s; redraws only when a number changes.
// macOS only: elsewhere the two numbers are simply not shown.

import { View, div, svg } from "gpui-kit";
import { h_flex } from "gpui-base";
import { spaces, agents, theme, run, window, keys, exec } from "herdr";

const EVERY = 5000;
const PROBE = "iostat -n 0 -c 2 -w 1 | tail -1; vm_stat; sysctl -n hw.memsize";

function parse(out) {
  const lines = out.split("\n");
  const cpu = lines[0].trim().split(/\s+/).map(Number);           // us sy id 1m 5m 15m
  const page = Number(/page size of (\d+)/.exec(out)?.[1] ?? 16384);
  const pages = (name) => Number(new RegExp(`${name}:\\s+(\\d+)`).exec(out)?.[1] ?? 0);
  const total = Number(lines.filter(Boolean).pop());
  const used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page;
  if (!(cpu.length >= 3) || !total) return null;
  return { cpu: Math.round(cpu[0] + cpu[1]), used: used / 2 ** 30, total: total / 2 ** 30 };
}

export default class GeniusFooter extends View {
  init(_props, cx) {
    this.stats = null;
    if (window()?.platform !== "macos") return;
    const probe = (cx) => cx.spawn(async (cx) => {
      try {
        const r = await exec("sh", ["-c", PROBE], { timeout: 10000 });
        const s = r.code === 0 ? parse(r.stdout) : null;
        const key = s && `${s.cpu}|${s.used.toFixed(1)}`;
        if (key !== this.key) { this.key = key; this.stats = s; cx.notify(); }
      } catch { /* leave the last numbers */ }
    });
    cx.timer.every(EVERY, probe);
    probe(cx);
  }

  // The palette key as actually bound (as in Herdr's own status bar).
  hint() {
    const mac = window()?.platform === "macos";
    const mod = { cmd: mac ? "⌘" : "Ctrl+", ctrl: mac ? "⌃" : "Ctrl+", shift: mac ? "⇧" : "Shift+", alt: mac ? "⌥" : "Alt+", option: mac ? "⌥" : "Alt+" };
    const b = keys().find((k) => k.section === "global" && k.action === "app.palette");
    const parts = b && b.keys.endsWith("--") ? [...b.keys.slice(0, -2).split("-"), "-"] : b?.keys.split("-");
    const key = parts && parts.map((p) => mod[p.toLowerCase()] ?? p.toUpperCase()).join("");
    return key ? `${key}  commands` : "commands";
  }

  render() {
    const t = theme();
    const space = spaces().find((s) => s.active);
    const list = agents();
    const needs = list.filter((a) => a.status === "blocked").length;
    const s = this.stats;
    const level = (pct) => (pct >= 90 ? t.blocked : pct >= 75 ? t.working : t.muted);
    const label = (icon, text) => h_flex().gap(6).items_center().child(svg(`mods/workspace-chrome/${icon}.svg`).size(13)).child(text);
    const meter = (id, name, pct, text) => h_flex().id(id).gap(6).items_center()
      .child(div().text_color(t.dim).child(name))
      .child(div().w(36).h(5).rounded(3).bg(t.raised).child(div().h_full().rounded(3).w(Math.max(2, 36 * Math.min(pct, 100) / 100)).bg(level(pct))))
      .child(div().text_color(level(pct)).child(text));
    return h_flex().size_full().px(14).gap(16).items_center().bg(t.chrome).border_t(1).border_color(t.border).text_size(11.8).text_color(t.muted)
      .child(label(space?.machine ? "server" : "monitor", space?.machine ?? "This computer"))
      .when(Boolean(space?.branch), (el) => el.child(label("branch", space.branch)))
      .child(h_flex().gap(6).items_center().child(div().size(6).rounded(3).bg(t.dim)).child(`${list.length} agents`)
        .when(needs > 0, (el) => el.child(div().text_color(t.blocked).child(`· ${needs} needs you`))))
      .child(div().flex_1())
      .when(Boolean(s), (el) => el
        .child(meter("cpu", "CPU", s.cpu, `${s.cpu}%`))
        .child(meter("ram", "RAM", 100 * s.used / s.total, `${s.used.toFixed(1)}/${Math.round(s.total)} GB`)))
      .child(div().id("palette").text_color(t.dim).on_click(() => run("app.palette")).child(this.hint()));
  }
}
