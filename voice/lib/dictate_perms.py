"""dictate_perms — the macOS permissions that F5 dictation needs, read-only. Used by doctor, dictate-setup, dictate-test.

    python3 voice/lib/dictate_perms.py status [--json]    what is granted, what is missing, the exact fix
    python3 voice/lib/dictate_perms.py problems [--fail-only]   one line per missing permission
    python3 voice/lib/dictate_perms.py open fda|mic|ax|auto   open the System Settings pane (+ show the binary in Finder)
    python3 voice/lib/dictate_perms.py ask                 the listener asks once for the missing ones (system dialogs)
    python3 voice/lib/dictate_perms.py test [--wait 90]    self-test: the user presses F5, says a phrase; report each stage

Whose permission. launchd starts voice/build/dictate-hotkey, so macOS checks THAT binary for what its
children do (ffmpeg → Microphone; osascript → System Events: Automation, and Accessibility for ⌘V
and Enter). A probe from a terminal checks the terminal app instead and proves nothing about F5.
So the listener reports its own state into voice/run/permissions.json (AXIsProcessTrusted,
AVCaptureDevice.authorizationStatus, AEDeterminePermissionToAutomateTarget without a dialog), and
voice/bin/dictate writes the result of the last take into voice/run/last-take.json. This module only
reads those files and `launchctl print`. It records nothing and sends no key.

Full Disk Access is needed only when genius-ai is under ~/Desktop, ~/Documents or ~/Downloads:
launchd then can not open the binary (exit 78). Keep genius-ai outside these folders, and it is not needed.
"""
import json
import os
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.realpath(__file__))
VOICE = os.path.dirname(LIB)
ROOT = os.path.dirname(VOICE)
RUN = os.path.join(VOICE, "run")
BINARY = os.path.join(VOICE, "build", "dictate-hotkey")
LABEL = "genius-ai.voice.hotkey"
PROTECTED = ("Desktop", "Documents", "Downloads")

# System Settings → Privacy & Security panes. The legacy «com.apple.preference.security» anchors still
# open the right pane on macOS 13–26 (System Settings maps them); the newer id is
# «com.apple.settings.PrivacySecurity.extension». Not verified on every macOS version.
PANE = "x-apple.systempreferences:com.apple.preference.security?"
PERMS = {
    "fda": {"anchor": "Privacy_AllFiles", "name": "Full Disk Access / Повний доступ до диска",
            "where": "Privacy & Security → Full Disk Access", "add": True},
    "mic": {"anchor": "Privacy_Microphone", "name": "Microphone / Мікрофон",
            "where": "Privacy & Security → Microphone", "add": False},
    "ax": {"anchor": "Privacy_Accessibility", "name": "Accessibility / Універсальний доступ",
           "where": "Privacy & Security → Accessibility", "add": True},
    "auto": {"anchor": "Privacy_Automation", "name": "Automation → System Events / Автоматизація",
             "where": "Privacy & Security → Automation", "add": False},
}


def sh(*args, timeout=5):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def protected_folder(path=VOICE):
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    for name in PROTECTED:
        if real.startswith(os.path.join(home, name) + os.sep):
            return "~/" + name
    return None


def job():
    """The launchd job of the F5 listener: {program, pid, last_exit} or {} when it is not loaded."""
    rc, out = sh("launchctl", "print", f"gui/{os.getuid()}/{LABEL}")
    if rc != 0:
        return {}
    info = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("program = "):
            info.setdefault("program", line.split(" = ", 1)[1])
        elif line.startswith("pid = "):
            info.setdefault("pid", int(line.split(" = ", 1)[1]))
        elif line.startswith("last exit code = "):
            info.setdefault("last_exit", line.split(" = ", 1)[1])
    return info


def listener_report(j):
    """The listener's own permission report, only when it comes from the running listener of THIS folder."""
    rep = read_json(os.path.join(RUN, "permissions.json"))
    if not j.get("pid") or j.get("program") != BINARY:
        return {}
    return rep if rep.get("pid") == j["pid"] else {}


def fix(perm):
    p = PERMS[perm]
    steps = f"voice/bin/dictate-test --open {perm}   (System Settings → {p['where']}"
    if p["add"]:
        steps += " → + → ⌘⇧G → вставте шлях → Open → увімкніть"
    elif perm == "auto":
        steps += " → dictate-hotkey → увімкніть System Events; якщо його там немає: voice/bin/dictate-test --ask"
    else:
        steps += " → увімкніть dictate-hotkey; якщо його там немає: voice/bin/dictate-test --ask"
    return steps + ")"


def checks():
    """[{id, name, state: ok|fail|warn|unknown, why, fix}] — the F5 chain, in the order the user grants it."""
    j, out = job(), []
    folder = protected_folder()
    other = j.get("program") and j["program"] != BINARY
    running = bool(j.get("pid")) and not other
    rep = listener_report(j) if running else {}

    # 0. the listener itself
    if other:
        out.append({"id": "listener", "name": "F5 listener", "state": "warn",
                    "why": f"F5 belongs to another genius-ai copy: {j['program']}",
                    "fix": "FORCE=1 voice/bin/dictate-setup start   (takes F5 for this copy)"})
    elif running:
        out.append({"id": "listener", "name": "F5 listener", "state": "ok", "why": f"pid {j['pid']}", "fix": ""})
    else:
        out.append({"id": "listener", "name": "F5 listener", "state": "fail",
                    "why": "not running" + (f" (last exit {j['last_exit']})" if j.get("last_exit") else ""),
                    "fix": "voice/bin/dictate-setup start"})

    # 1. Full Disk Access: only for an install under a protected folder
    if not folder:
        out.append({"id": "fda", "name": PERMS["fda"]["name"], "state": "ok",
                    "why": "not needed: genius-ai is outside ~/Desktop, ~/Documents, ~/Downloads", "fix": ""})
    elif str(j.get("last_exit", "")).startswith("78"):
        out.append({"id": "fda", "name": PERMS["fda"]["name"], "state": "fail",
                    "why": f"macOS blocked the listener in {folder} (launchd exit 78)", "fix": fix("fda")})
    elif running:
        out.append({"id": "fda", "name": PERMS["fda"]["name"], "state": "ok",
                    "why": f"the listener runs from {folder}", "fix": ""})
    else:
        out.append({"id": "fda", "name": PERMS["fda"]["name"], "state": "warn",
                    "why": f"genius-ai is in {folder}: the listener needs Full Disk Access (or move genius-ai out)",
                    "fix": fix("fda")})

    # 2–4. what the listener reports about itself
    words = {"ok": "granted", "denied": "denied", "not-asked": "not asked yet", "restricted": "restricted by a profile",
             "unknown": "no answer yet (System Events not running)"}
    for perm, key in (("mic", "microphone"), ("ax", "accessibility"), ("auto", "automation")):
        v = rep.get(key)
        if v is None:
            state, why = "unknown", ("the F5 listener of this folder is not running" if not running else
                                     "the listener is an old build without a report: voice/bin/dictate-setup start")
        else:
            state = "ok" if v == "ok" else "unknown" if v == "unknown" else "fail"
            why = words.get(v, v)
        out.append({"id": perm, "name": PERMS[perm]["name"], "state": state, "why": why,
                    "fix": "" if state == "ok" else fix(perm)})

    # 5. the last real take (voice/bin/dictate): what actually happened
    take = read_json(os.path.join(RUN, "last-take.json"))
    if take:
        bad = [f"{k}: {take[k]}" for k in ("mic", "paste") if take.get(k) not in (None, "ok", "skipped")]
        age = int(time.time() - take.get("time", 0)) // 60
        out.append({"id": "last-take", "name": "last F5 take", "state": "fail" if bad else "ok",
                    "why": (", ".join(bad) if bad else "recorded and pasted") + f" ({age} min ago)",
                    "fix": "voice/bin/dictate-test" if bad else ""})
    return out


MARK = {"ok": "✅", "fail": "⛔", "warn": "⚠️ ", "unknown": "❔"}


def human(items):
    for c in items:
        print(f"  {MARK[c['state']]} {c['name']}: {c['why']}" + (f"\n       → {c['fix']}" if c["fix"] else ""))


def open_pane(perm):
    """Open the pane; for a list with a «+» button, also show the binary in Finder to drag it in."""
    if perm not in PERMS:
        print("usage: dictate-test --open fda|mic|ax|auto")
        return 2
    subprocess.run(["open", PANE + PERMS[perm]["anchor"]])
    print(f"Opened: System Settings → {PERMS[perm]['where']}")
    if PERMS[perm]["add"]:
        if os.path.exists(BINARY):
            subprocess.run(["open", "-R", BINARY])
            print(f"Finder shows the file: {BINARY}\n  drag it into the list (or + → ⌘⇧G → paste the path → Open), then switch it on")
        else:
            print("the listener is not built yet: voice/bin/dictate-setup start")
    else:
        print("This list has no «+»: dictate-hotkey appears there after the system dialog (voice/bin/dictate-test --ask).")
    return 0


def ask():
    """The running listener asks once for the missing permissions (system dialogs)."""
    j = job()
    if not j.get("pid") or j.get("program") != BINARY:
        print("⛔ the F5 listener of this folder is not running: voice/bin/dictate-setup start")
        return 1
    os.makedirs(RUN, exist_ok=True)
    open(os.path.join(RUN, "request-permissions"), "w").close()
    print("The listener asks within 5 s: answer «Allow» / «OK» in each system dialog.")
    print("Accessibility opens System Settings: switch dictate-hotkey on there.")
    return 0


def test(wait):
    """Self-test through the real F5 path. Nothing is pasted into a window and no Enter is pressed:
    in test mode voice/bin/dictate taps only Shift (types nothing) to prove Automation + Accessibility,
    and puts the text into the clipboard for the user to paste with ⌘V where they want."""
    items = checks()
    human(items)
    if not any(c["id"] == "listener" and c["state"] == "ok" for c in items):
        print("\n⛔ start the listener first: voice/bin/dictate-setup start")
        return 1
    if any(c["state"] == "fail" and c["id"] in ("mic", "ax", "auto") for c in items) or \
            any(c["state"] == "unknown" and c["id"] in ("mic", "ax", "auto") for c in items):
        ask()
    os.makedirs(RUN, exist_ok=True)
    marker = os.path.join(RUN, "selftest")
    with open(marker, "w") as f:
        f.write(str(int(time.time()) + wait))
    take_path = os.path.join(RUN, "last-take.json")
    start = time.time()
    print(f"\n👉 Press F5 (🎤), say «тест, один, два» / «test one two», then stop talking.  (waiting {wait} s)")
    print("   Test mode: nothing is typed into any window and Enter is not pressed.")
    try:
        while time.time() - start < wait:
            take = read_json(take_path)
            if take.get("mode") == "test" and take.get("time", 0) >= start:
                break
            time.sleep(1)
        else:
            print("⛔ no F5 press seen: is the key remapped (voice/bin/dictate-setup status)?")
            return 1
    finally:
        try:
            os.remove(marker)
        except OSError:
            pass
    stages = [
        ("F5 → listener", "ok", "the key press reached the listener"),
        ("Microphone", take.get("mic"), {"ok": "sound recorded",
                                         "hung": "no sound in 4 s: macOS blocks the microphone → " + fix("mic"),
                                         "silent": "the mic works, but nothing was said"}),
        ("Whisper", take.get("stt"), {"ok": f"«{take.get('text', '')}»", "fail": "transcription failed: voice/run/last-ff.log"}),
        ("Paste (Automation + Accessibility)", take.get("paste"),
         {"ok": "System Events accepted a key (Shift): ⌘V and Enter will work",
          "automation": "System Events refused (-1743) → " + fix("auto"),
          "accessibility": "keys are not allowed (1002) → " + fix("ax")}),
    ]
    ok = all(state == "ok" for _, state, _ in stages)
    for name, state, why in stages:
        text = why if isinstance(why, str) else why.get(state, f"{state}: {take.get('paste_error', '')}")
        mark = "✅" if state == "ok" else "❔" if state is None else "⛔"
        print(f"  {mark} {name}: {text if state else 'not reached'}")
    print("\n" + ("✅ F5 works. The text is in the clipboard: press ⌘V where you want it." if ok else
                  "⛔ fix the first ⛔ above, then run voice/bin/dictate-test again."))
    return 0 if ok else 1


def main():
    a = sys.argv[1:] or ["status"]
    if a[0] == "status":
        items = checks()
        if "--json" in a:
            json.dump(items, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            human(items)
        return 0
    if a[0] == "problems":                       # one line per missing permission (dictate-setup, SessionStart)
        for c in checks():
            if c["id"] in ("fda", "mic", "ax", "auto") and c["state"] != "ok" and \
                    (c["state"] == "fail" or "--fail-only" not in a):
                print(f"{MARK[c['state']]} F5 · {c['name']}: {c['why']} → {c['fix']}")
        return 0
    if a[0] == "open":
        return open_pane(a[1] if len(a) > 1 else "")
    if a[0] == "ask":
        return ask()
    if a[0] == "test":
        return test(int(a[a.index("--wait") + 1]) if "--wait" in a else 90)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
