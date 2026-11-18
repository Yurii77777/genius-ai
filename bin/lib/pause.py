"""Pause or resume a project: `"paused"` in projects/<name>/project.json.

    bin/genius pause <name|tag>     the project is not checked, not reported, dimmed on the Herdr panel
    bin/genius resume <name|tag>    back to live control
    bin/genius hold <name|tag> [why]   the user's «take nothing new»: `"hold"` in project.json. The project
                                    is still checked and reported; its card shows the mark. Agents finish
                                    what they started; no new task, agent, merge or prod write until unhold.
    bin/genius unhold <name|tag>    lift it

<name> is the project's name in genius.config.json, or the letters of its tag (LW, ch, …).
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


def main():
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
