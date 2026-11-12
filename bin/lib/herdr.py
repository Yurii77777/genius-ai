"""genius-ai for Herdr: the status shelves in a right column.

    bin/genius herdr install [--show]   copy the mods, write the layout `genius`, check it; --show switches to it
    bin/genius herdr uninstall          back to the layout `main`, remove the mod and the layout
    bin/genius herdr status             what is installed
    bin/genius herdr claude on | off    the footer follows the active space: Claude Code's status line of
                                        the USER (~/.claude/settings.json) → bin/lib/statusline.py
    bin/genius herdr sync               install again only when herdr/ or Herdr's main layout changed (SessionStart hook)

Herdr reads mods and layouts only from its own folder (~/.config/herdr-app, or $HERDR_APP_HOME).
The code lives here, in herdr/; install copies it there (Herdr's hot reload does not see through
links), so run install again after a change. Herdr is never restarted.
The layout `genius` is Herdr's `main` with one more column on the right: three shelves of the mod
genius-status (attention, projects, operations), one more shelf in the left sidebar (requests of
agents), and the status bar genius-footer (Herdr's own plus live CPU and RAM). It is made again from `main` on every install, so it follows Herdr's updates.
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
HERDR = os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")
MODS = ["genius-status", "genius-footer"]                       # folders in herdr/, same names in Herdr
LAYOUT = os.path.join(HERDR, "layouts", "genius.json")
COLUMN_ID = "genius"


def herdr_app(*args):
    try:
        r = subprocess.run(["herdr-app", *args], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def mod_dir(name):
    return os.path.join(HERDR, "mods", name)


def ours_mod(name):
    return os.path.isfile(os.path.join(mod_dir(name), "root.json"))      # root.json marks our copy


def ours_layout():
    try:
        with open(LAYOUT, encoding="utf-8") as f:
            return any(c.get("id") == COLUMN_ID for c in json.load(f).get("children", []))
    except (OSError, ValueError):
        return False


def find(node, node_id):
    if node.get("id") == node_id:
        return node
    for child in node.get("children", []):
        hit = find(child, node_id)
        if hit:
            return hit
    return None


def shelf(section, size):
    return {"id": f"genius-{section}", "type": "block", "size": size, "radius": 10,
            "border": {"width": 1, "color": "border"}, "background": "chrome",
            "content": {"mod": "genius-status", "props": {"section": section}}}


def install(show):
    if not os.path.isdir(HERDR):
        print(f"Herdr not found ({HERDR}): nothing to install.")
        return 0
    for name in MODS:
        if os.path.exists(mod_dir(name)) and not ours_mod(name):
            print(f"❌ {mod_dir(name)} exists and is not genius-ai's: left as is.")
            return 1
    if os.path.exists(LAYOUT) and not ours_layout():
        print(f"❌ {LAYOUT} exists and is not genius-ai's: left as is.")
        return 1
    # 1. the mods: a fresh copy each, plus root.json (which genius-ai folder it reads)
    for name in MODS:
        shutil.rmtree(mod_dir(name), ignore_errors=True)
        shutil.copytree(os.path.join(ROOT, "herdr", name), mod_dir(name))
        with open(os.path.join(mod_dir(name), "root.json"), "w", encoding="utf-8") as f:
            json.dump({"root": ROOT}, f)
            f.write("\n")
        code, out = herdr_app("check", mod_dir(name))
        if code != 0:
            print(f"❌ herdr-app check {name} failed:\n{out}")
            return 1
    # 2. the layout: Herdr's main + the right column
    with open(os.path.join(HERDR, "layouts", "main.json"), encoding="utf-8") as f:
        layout = json.load(f)
    layout.pop("preview", None)
    layout["children"] = [c for c in layout.get("children", []) if c.get("id") != COLUMN_ID] + [{
        "id": COLUMN_ID, "type": "column", "size": {"px": 340}, "min": 220, "max": 640,
        "background": "canvas", "padding": 8, "gap": 8,
        "children": [shelf("attention", {"weight": 2}), shelf("projects", {"weight": 4}), shelf("ops", {"weight": 2})],
    }]
    # the left sidebar: requests of agents under Herdr's agents list
    lists = find(layout, "sidebar-lists")
    if lists is not None:
        lists["children"] = [c for c in lists.get("children", []) if c.get("id") != "genius-requests"] + [{
            "id": "genius-requests", "type": "block", "size": {"weight": 1},
            "content": {"mod": "genius-status", "props": {"section": "requests"}}}]
    footer = find(layout, "status")
    if footer and footer.get("content", {}).get("mod") == "workspace-status":
        footer["content"]["mod"] = "genius-footer"
    with open(LAYOUT, "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✅ mods {', '.join(MODS)} and layout genius installed in {HERDR}")
    if show:
        code, out = herdr_app("do", "layout.switch", "--name", "genius")
        print("✅ layout genius shown" if code == 0 else f"❌ layout.switch: {out}")
    else:
        print("   show it: ctrl-shift-l, or bin/genius herdr install --show")
    return 0


def shown_layout():
    try:
        with open(os.path.join(HERDR, "shared.json"), encoding="utf-8") as f:
            return json.load(f).get("layout")
    except (OSError, ValueError):
        return None


def uninstall():
    if shown_layout() == "genius":
        code, out = herdr_app("do", "layout.switch", "--name", "main")
        if code != 0:
            print(f"⚠️  could not switch to main: {out}")
    if ours_layout():
        os.remove(LAYOUT)
    for name in MODS:
        if ours_mod(name):
            shutil.rmtree(mod_dir(name))
    print("✅ genius-ai removed from Herdr")
    return 0


def stale():
    """Our copy in Herdr differs from herdr/, or Herdr's main layout is newer than the layout genius."""
    for name in MODS:
        src = os.path.join(ROOT, "herdr", name)
        for f in os.listdir(src):
            try:
                with open(os.path.join(src, f), "rb") as a, open(os.path.join(mod_dir(name), f), "rb") as b:
                    if a.read() != b.read():
                        return True
            except OSError:
                return True
    try:
        return os.path.getmtime(os.path.join(HERDR, "layouts", "main.json")) > os.path.getmtime(LAYOUT)
    except OSError:
        return True


def sync():
    if not all(ours_mod(n) for n in MODS[:1]) or not stale():       # only where the panel is already installed
        return 0
    return install(False)


def claude_statusline(turn_on):
    """The user's own Claude Code settings: every session reports its model, context and limits.

    Never replaces a status line the user already has; keeps a backup before the first change.
    """
    path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        settings = json.loads(raw)
    except OSError:
        raw, settings = None, {}
    except ValueError:
        print(f"❌ {path} is not valid JSON: left as is.")
        return 1
    script = os.path.join(ROOT, "bin", "lib", "statusline.py")
    now = (settings.get("statusLine") or {}).get("command", "")
    ours = "bin/lib/statusline.py" in now
    if turn_on:
        if now and not ours:
            print(f"❌ {path} already has a status line: left as is.\n   {now}")
            return 1
        settings["statusLine"] = {"type": "command", "command": f'/usr/bin/python3 "{script}" 2>/dev/null || true'}
    else:
        if not ours:
            print("nothing to remove: the user's status line is not genius-ai's")
            return 0
        settings.pop("statusLine")
    if raw is not None and not os.path.exists(path + ".bak-genius"):
        with open(path + ".bak-genius", "w", encoding="utf-8") as f:
            f.write(raw)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✅ status line {'set' if turn_on else 'removed'} in {path}" + (" (new sessions; open ones after their next reply)" if turn_on else ""))
    return 0


def status():
    print(f"Herdr folder: {HERDR}" + ("" if os.path.isdir(HERDR) else "  (not found)"))
    for name in MODS:
        print(f"mod {name}: {'installed' if ours_mod(name) else 'not installed'}")
    print(f"layout genius:     {'installed' if ours_layout() else 'not installed'}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        sys.exit(install("--show" in sys.argv))
    if cmd == "uninstall":
        sys.exit(uninstall())
    if cmd == "status":
        sys.exit(status())
    if cmd == "claude" and sys.argv[2:3] in (["on"], ["off"]):
        sys.exit(claude_statusline(sys.argv[2] == "on"))
    if cmd == "sync":
        sys.exit(sync())
    sys.exit("usage: bin/genius herdr install [--show] | uninstall | status | sync | claude on|off")
