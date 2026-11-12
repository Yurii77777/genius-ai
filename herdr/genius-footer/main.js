// genius-footer — Herdr's status bar (machine, branch, agents) plus live CPU and RAM of this Mac.
// Herdr's own workspace-status is read-only, so this is our copy of it; the layout `genius` shows it.
// CPU: iostat over 1 s (user + system). RAM: vm_stat (active + wired + compressed) of hw.memsize,
// the same as Activity Monitor's "Memory Used". Its color is macOS's memory pressure, not the amount:
// a Mac with a loaded model is full and fine, so red means the system really is short (level 4),
// amber a warning (2). Every 5 s; redraws only when a number changes.
// macOS only: elsewhere the two numbers are simply not shown.
// From the genius-ai folder named in root.json (written by the installer), every 5 s too:
//   voice  🔊/🔇 from genius.config.json → voice.enabled; a click runs voice/bin/speak on | off
//   Claude model and context of the ACTIVE space's session (state/claude/<folder>.json), and the
//          account's usage limits from the latest report of any session (state/claude-status.json);
//          both written by bin/lib/statusline.py. A space without an agent shows the limits only.

import { View, div, svg } from "gpui-kit";
import { h_flex } from "gpui-base";
import { spaces, agents, panes, theme, run, window, keys, exec, readTextFile, appHome } from "herdr";

const EVERY = 5000;
const PROBE = "iostat -n 0 -c 2 -w 1 | tail -1; vm_stat; sysctl -n kern.memorystatus_vm_pressure_level hw.memsize";

function parse(out) {
  const lines = out.split("\n");
  const cpu = lines[0].trim().split(/\s+/).map(Number);           // us sy id 1m 5m 15m
  const page = Number(/page size of (\d+)/.exec(out)?.[1] ?? 16384);
  const pages = (name) => Number(new RegExp(`${name}:\\s+(\\d+)`).exec(out)?.[1] ?? 0);
  const tail = lines.filter(Boolean);
  const total = Number(tail.pop());
  const pressure = Number(tail.pop());                              // 1 normal, 2 warning, 4 critical
  const used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page;
  if (!(cpu.length >= 3) || !total) return null;
  return { cpu: Math.round(cpu[0] + cpu[1]), used: used / 2 ** 30, total: total / 2 ** 30, pressure };
}

export default class GeniusFooter extends View {
  init(_props, cx) {
    this.stats = null;
    this.genius = {};
    cx.spawn(async (cx) => {
      try { this.root = JSON.parse((await readTextFile(`${appHome()}/mods/genius-footer/root.json`)) ?? "").root; } catch { return; }
      const read = (cx) => this.readGenius(cx);
      cx.timer.every(EVERY, read);
      read(cx);
    });
    if (window()?.platform !== "macos") return;
    const probe = (cx) => cx.spawn(async (cx) => {
      try {
        const r = await exec("sh", ["-c", PROBE], { timeout: 10000 });
        const s = r.code === 0 ? parse(r.stdout) : null;
        const key = s && `${s.cpu}|${s.used.toFixed(1)}|${s.pressure}`;
        if (key !== this.key) { this.key = key; this.stats = s; cx.notify(); }
      } catch { /* leave the last numbers */ }
    });
    cx.timer.every(EVERY, probe);
    probe(cx);
  }

  readGenius(cx) {
    cx.spawn(async (cx) => {
      const json = async (path) => { try { return JSON.parse((await readTextFile(`${this.root}/${path}`)) ?? "null"); } catch { return null; } };
      const cfg = await json("genius.config.json");
      const latest = await json("state/claude-status.json");
      // the session of the active space: its agent's folder, or a parent of it (the session's own folder)
      let session = null;
      for (let dir = this.sessionFolder(), up = 0; dir && dir !== "/" && up < 6 && !session; dir = dir.slice(0, dir.lastIndexOf("/")), up++) {
        session = await json(`state/claude/${dir.replace(/[^A-Za-z0-9]/g, "-")}.json`);
      }
      const claude = latest && { model: session?.model, context_window: session?.context_window, rate_limits: latest.rate_limits };
      const next = { uk: cfg?.language === "uk", voice: cfg?.voice ? cfg.voice.enabled !== false : null, claude };
      const key = JSON.stringify([next.voice, latest?._saved, session?._saved, session?.cwd]);
      if (key !== this.geniusKey) { this.geniusKey = key; this.genius = next; cx.notify(); }
    });
  }

  // On: the voice server and Whisper load at once; ⏳ stays until the voice server answers (≈30 s),
  // so the click never looks hung. Off: everything of the voice stops at once.
  // Where the agent of the active space runs: the focused agent's terminal, else the space's first agent.
  sessionFolder() {
    const space = spaces().find((s) => s.active);
    const mine = agents().filter((a) => a.space === space?.id);
    const agent = mine.find((a) => a.focused) ?? mine[0];
    return agent ? panes().find((p) => p.pane === agent.pane)?.cwd ?? null : null;
  }

  toggleVoice(cx) {
    if (this.voiceBusy) return;
    const on = this.genius.voice;
    const speak = `${this.root}/voice/bin/speak`;
    this.voiceBusy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        await exec(speak, [on ? "off" : "on"], { cwd: this.root, timeout: 60000 });
        for (let i = 0; !on && i < 30; i++) {
          const r = await exec("sh", ["-c", `sleep 3; "${speak}" status`], { cwd: this.root, timeout: 20000 });
          if (r.stdout.includes("tts-server: loaded")) break;
        }
      } catch { /* shown by the next read */ }
      this.voiceBusy = false;
      this.readGenius(cx);
      cx.notify();
    });
  }

  // "Fable 5.2 · ctx 22% · 5h 40% · 7d 37%": the limits Claude Code reported last, the fullest decides the color.
  claude(t) {
    const c = this.genius.claude;
    if (!c) return null;
    const limits = [["5h", c.rate_limits?.five_hour], ["7d", c.rate_limits?.seven_day]].filter(([, l]) => l?.used_percentage != null);
    const worst = Math.max(0, ...limits.map(([, l]) => l.used_percentage));
    const reset = (l) => l.resets_at ? new Date(l.resets_at * 1000).toLocaleString() : "";
    const parts = [c.model?.display_name, c.context_window?.used_percentage != null && `ctx ${Math.round(c.context_window.used_percentage)}%`,
      ...limits.map(([name, l]) => `${name} ${Math.round(l.used_percentage)}%`)].filter(Boolean);
    return div().id("claude").text_color(worst >= 90 ? t.blocked : worst >= 75 ? t.working : t.muted)
      .tooltip(limits.map(([name, l]) => `${name}: ${Math.round(l.used_percentage)}% → ${reset(l)}`).join("\n") || (c.model?.display_name ?? ""))
      .child(parts.join(" · "));
  }

  // The active space changed: read its session at once, not at the next 5 s tick.
  follow(cx) {
    const now = this.sessionFolder();
    if (now === this.followed || !this.root) return;
    this.followed = now;
    this.readGenius(cx);
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

  render(cx) {
    this.follow(cx);
    const t = theme();
    const space = spaces().find((s) => s.active);
    const list = agents();
    const needs = list.filter((a) => a.status === "blocked").length;
    const s = this.stats;
    const level = (pct) => (pct >= 90 ? t.blocked : pct >= 75 ? t.working : t.muted);
    const label = (icon, text) => h_flex().gap(6).items_center().child(svg(`mods/workspace-chrome/${icon}.svg`).size(13)).child(text);
    const meter = (id, name, pct, text, color = level(pct)) => h_flex().id(id).gap(6).items_center()
      .child(div().text_color(t.dim).child(name))
      .child(div().w(36).h(5).rounded(3).bg(t.raised).child(div().h_full().rounded(3).w(Math.max(2, 36 * Math.min(pct, 100) / 100)).bg(color)))
      .child(div().text_color(color).child(text));
    const pressure = s?.pressure >= 4 ? t.blocked : s?.pressure >= 2 ? t.working : t.muted;
    return h_flex().size_full().px(14).gap(16).items_center().bg(t.chrome).border_t(1).border_color(t.border).text_size(11.8).text_color(t.muted)
      .child(label(space?.machine ? "server" : "monitor", space?.machine ?? "This computer"))
      .when(Boolean(space?.branch), (el) => el.child(label("branch", space.branch)))
      .child(h_flex().gap(6).items_center().child(div().size(6).rounded(3).bg(t.dim)).child(`${list.length} agents`)
        .when(needs > 0, (el) => el.child(div().text_color(t.blocked).child(`· ${needs} needs you`))))
      .child(div().flex_1())
      .when(Boolean(this.claude(t)), (el) => el.child(this.claude(t)))
      .when(this.genius.voice != null, (el) => el.child(div().id("voice").px(4).rounded(4).cursor_pointer()
        .hover((st) => st.bg(t.hover)).text_color(this.genius.voice ? t.foreground : t.dim)
        .tooltip(this.genius.uk ? (this.genius.voice ? "голос і диктування увімкнено — клік вимикає" : "голос і диктування вимкнено — клік вмикає")
                                : (this.genius.voice ? "voice and dictation on — click turns them off" : "voice and dictation off — click turns them on"))
        .on_click((_e, cx) => this.toggleVoice(cx)).child(this.voiceBusy ? "⏳" : this.genius.voice ? "🔊" : "🔇")))
      .when(Boolean(s), (el) => el
        .child(meter("cpu", "CPU", s.cpu, `${s.cpu}%`))
        .child(meter("ram", "RAM", 100 * s.used / s.total, `${s.used.toFixed(1)}/${Math.round(s.total)} GB`, pressure)))
      .child(div().id("palette").text_color(t.dim).on_click(() => run("app.palette")).child(this.hint()));
  }
}
