"""Pause or resume a project: `"paused"` in projects/<name>/project.json.

    bin/genius pause <name|tag>     the project is not checked, not reported, dimmed on the Herdr panel
    bin/genius resume <name|tag>    back to live control
    bin/genius hold <name|tag> [why]   the user's «take nothing new»: `"hold"` in project.json. The project
                                    is still checked and reported; its card shows the mark. Agents finish
                                    what they started; no new task, agent, merge or prod write until unhold.
    bin/genius unhold <name|tag>    lift it
    python3 -I bin/lib/pause.py selftest   checks of the paused reading and of the active list

<name> is the project's name in genius.config.json, or the letters of its tag (EX, dm, …).

The paused mark is not only a label: it is the one reading that every worker takes from here
(`paused`, `active`), so «a paused project is not checked, not reported» holds everywhere.
"""
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def meta(name, root=ROOT):
    """projects/<name>/project.json: the tag, the spoken name, the paused and hold marks."""
    return read(os.path.join(root, "projects", name, "project.json"), {})


def paused(name, root=ROOT):
    return bool(meta(name, root).get("paused"))


def active(cfg, root=ROOT):
    """The config projects that the user did not pause — the only ones any worker touches:
    [{"name", "path", "tag", "spoken"}, …], the longest path matching an agent's folder first (see project_of)."""
    out = []
    for p in cfg.get("projects") or []:
        name = p.get("name")
        if not name or not p.get("path"):
            continue
        m = meta(name, root)
        if m.get("paused"):
            continue
        out.append({"name": name, "path": os.path.realpath(os.path.expanduser(p["path"])),
                    "tag": m.get("tag", ""), "spoken": m.get("spoken", name)})
    return out


def selftest():
    """paused → not in the active list; resumed → back; the tag and the spoken name come with it."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg = {"projects": [{"name": "alpha", "path": d + "/alpha"}, {"name": "beta", "path": d + "/beta"}]}
        for name, m in (("alpha", {"tag": "🌐 AL", "spoken": "Сайт"}), ("beta", {"paused": {"since": "2026-11-18"}})):
            os.makedirs(os.path.join(d, "projects", name), exist_ok=True)
            with open(os.path.join(d, "projects", name, "project.json"), "w", encoding="utf-8") as f:
                json.dump(m, f)
        got = active(cfg, d)
        assert [p["name"] for p in got] == ["alpha"], got
        assert got[0]["tag"] == "🌐 AL" and got[0]["spoken"] == "Сайт", got
        assert got[0]["path"] == os.path.realpath(d + "/alpha"), got
        assert paused("beta", d) and not paused("alpha", d)
        assert meta("beta", d)["paused"]["since"] == "2026-11-18"
        with open(os.path.join(d, "projects", "beta", "project.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        assert [p["name"] for p in active(cfg, d)] == ["alpha", "beta"], active(cfg, d)
        assert active({}, d) == [] and active({"projects": [{"name": "no-path"}]}, d) == []
    print("pause selftest: ok")


def main():
    if sys.argv[1:] == ["selftest"]:
        selftest()
        return
    if len(sys.argv) < 3 or sys.argv[1] not in ("pause", "resume", "hold", "unhold"):
        sys.exit("usage: bin/genius pause|resume|unhold <project name or tag> | hold <project name or tag> [why]")
    action, want = sys.argv[1], sys.argv[2].lower()
    for p in read(os.path.join(ROOT, "genius.config.json"), {}).get("projects") or []:
        path = os.path.join(ROOT, "projects", p["name"], "project.json")
        meta = read(path, {})
        tag = "".join(c for c in meta.get("tag", "") if c.isalnum()).lower()
        if want not in (p["name"].lower(), tag):
            continue
        if action == "pause":
            meta["paused"] = {"since": date.today().isoformat(), "by": "owner"}
        elif action == "hold":
            meta["hold"] = {"since": date.today().isoformat(), "why": " ".join(sys.argv[3:])}
        elif action == "unhold":
            meta.pop("hold", None)
        else:
            meta.pop("paused", None)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
        mark = {"pause": "⏸ paused: ", "resume": "▶ resumed: ", "hold": "✋ on hold: ", "unhold": "▶ hold lifted: "}[action]
        print(mark + f"{meta.get('tag', p['name'])} ({p['name']})")
        return
    sys.exit(f"no project «{sys.argv[2]}» in genius.config.json")


if __name__ == "__main__":
    main()
