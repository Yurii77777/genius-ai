"""genius-ai for Herdr: the status shelves in a right column.

    bin/genius herdr install [--show]   copy the mod, write the layout `genius`, check it; --show switches to it
    bin/genius herdr uninstall          back to the layout `main`, remove the mod and the layout
    bin/genius herdr status             what is installed

Herdr reads mods and layouts only from its own folder (~/.config/herdr-app, or $HERDR_APP_HOME).
The code lives here, in herdr/; install copies it there (Herdr's hot reload does not see through
links), so run install again after a change. Herdr is never restarted.
The layout `genius` is Herdr's `main` with one more column on the right: three shelves of the mod
genius-status (attention, projects, operations). It is made again from `main` on every install,
so it follows Herdr's updates.
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
SRC = os.path.join(ROOT, "herdr", "genius-status")
HERDR = os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")
MOD = os.path.join(HERDR, "mods", "genius-status")
LAYOUT = os.path.join(HERDR, "layouts", "genius.json")
COLUMN_ID = "genius"


def herdr_app(*args):
    try:
        r = subprocess.run(["herdr-app", *args], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def ours_mod():
    return os.path.isfile(os.path.join(MOD, "root.json"))


def ours_layout():
    try:
        with open(LAYOUT, encoding="utf-8") as f:
            return any(c.get("id") == COLUMN_ID for c in json.load(f).get("children", []))
    except (OSError, ValueError):
        return False


def shelf(section, size):
    return {"id": f"genius-{section}", "type": "block", "size": size,
            "content": {"mod": "genius-status", "props": {"section": section}}}


def install(show):
    if not os.path.isdir(HERDR):
        print(f"Herdr not found ({HERDR}): nothing to install.")
        return 0
    if os.path.exists(MOD) and not ours_mod():
        print(f"❌ {MOD} exists and is not genius-ai's: left as is.")
        return 1
    if os.path.exists(LAYOUT) and not ours_layout():
        print(f"❌ {LAYOUT} exists and is not genius-ai's: left as is.")
        return 1
    # 1. the mod: a fresh copy, plus root.json (which genius-ai folder it reads)
    shutil.rmtree(MOD, ignore_errors=True)
    shutil.copytree(SRC, MOD)
    with open(os.path.join(MOD, "root.json"), "w", encoding="utf-8") as f:
        json.dump({"root": ROOT}, f)
        f.write("\n")
    code, out = herdr_app("check", MOD)
    if code != 0:
        print(f"❌ herdr-app check failed:\n{out}")
        return 1
    # 2. the layout: Herdr's main + the right column
    with open(os.path.join(HERDR, "layouts", "main.json"), encoding="utf-8") as f:
        layout = json.load(f)
    layout.pop("preview", None)
    layout["children"] = [c for c in layout.get("children", []) if c.get("id") != COLUMN_ID] + [{
        "id": COLUMN_ID, "type": "column", "size": {"px": 340}, "min": 220, "max": 640,
        "background": "chrome", "divider": {"width": 1, "color": "border"},
        "children": [shelf("attention", {"weight": 1}), shelf("projects", {"weight": 3}), shelf("ops", {"weight": 2})],
    }]
    with open(LAYOUT, "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✅ mod genius-status and layout genius installed in {HERDR}")
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
    if ours_mod():
        shutil.rmtree(MOD)
    print("✅ genius-ai removed from Herdr")
    return 0


def status():
    print(f"Herdr folder: {HERDR}" + ("" if os.path.isdir(HERDR) else "  (not found)"))
    print(f"mod genius-status: {'installed' if ours_mod() else 'not installed'}")
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
    sys.exit("usage: bin/genius herdr install [--show] | uninstall | status")
