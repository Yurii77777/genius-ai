"""Claude Code status line for genius-ai: saves what Claude Code reports and prints nothing.

Claude Code pipes a JSON (model, context, cost, rate limits when it has them) into the status
line command. This keeps the latest one in state/claude-status.json for the Herdr footer
(herdr/genius-footer). It prints nothing, so the terminal looks as before.
"""
import json
import os
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
except Exception:
    pass
