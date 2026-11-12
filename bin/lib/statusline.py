"""Claude Code status line for genius-ai: saves what Claude Code reports and prints nothing.

Claude Code pipes a JSON (model, context, cost, rate limits when it has them) into the status
line command. This keeps it for the Herdr footer (herdr/genius-footer):
  state/claude-status.json          the latest one of any session (the account's usage limits)
  state/claude/<folder>.json        the latest one of the session in that folder; <folder> is the
                                    session's folder with every other character than a-z A-Z 0-9 as "-"
Every space has its own session, model and context: the footer shows the one of the active space.
It serves this project (.claude/settings.json) and, after `bin/genius herdr claude on`, every
Claude Code session of the user. It prints nothing, so the terminal looks as before.
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

try:
    data = json.load(sys.stdin)
    data["_saved"] = int(time.time())
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    tmp = os.path.join(ROOT, "state", "claude-status.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(ROOT, "state", "claude-status.json"))
    folder = (data.get("workspace") or {}).get("project_dir") or data.get("cwd") or ""
    if folder:
        os.makedirs(os.path.join(ROOT, "state", "claude"), exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]", "-", folder) + ".json"
        tmp = os.path.join(ROOT, "state", "claude", name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(ROOT, "state", "claude", name))
except Exception:
    pass
