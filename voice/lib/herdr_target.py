"""The terminal a dictation goes to when Herdr is the front window (the user's decision of 2026-11-12).

Pasting with Cmd+V goes wherever the keyboard focus is; in Herdr that can be a panel or a peek
over the tab, and the text was lost without a trace. Instead the dictation is sent straight into
the terminal of the focused pane (`hc send`), whatever holds the keyboard.

Prints the terminal's address (t2, t1v…) and exits 0, or prints nothing and exits 1 when there is
no terminal to send to (the focused pane is a mod, Herdr does not answer). Read-only.
"""
import json
import subprocess
import sys


def herdr(*args):
    out = subprocess.run(["herdr-app", *args], capture_output=True, text=True, timeout=3)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None


def terminal_of(pane, layout):
    for space in (layout or {}).get("spaces", []):
        for tab in space.get("tabs", []):
            for term in tab.get("terminals", []):
                if term.get("pane") == pane:
                    return term.get("terminal")
    return None


def main():
    try:
        status = herdr("status")
        if not status:
            return 1
        target = terminal_of(status.get("focused_pane"), herdr("ls")) or status.get("agent")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 1
    if not target:
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
