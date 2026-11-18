"""The day in numbers: what was done, by whom, at what cost — kept every day, to see how to speed up.

    bin/genius day [<date>]          the numbers of a day (default today), saved to state/days/<date>.json
    bin/genius day trend [<n>]       the last n days side by side (default 7)

The user's decision of 2026-11-17: at the end of the day the count of finished tasks is recorded, so that
later the daily work can be analysed and the process sped up. The end-of-day hook (bin/lib/know.py) runs
this; it can be run any time. Read-only except its own file.

Counted (all from what genius-ai already keeps):
- tasks: operations closed that day, per project (state/operations.closed.json), and the ones still open;
- the user: requests and button answers (state/log, or its archive);
- agents: sub-agents started by bin/genius agent that day, per model (Herdr's terminal list);
- the gate: decisions by rule / judge / watcher, allows and denies (state/gate.jsonl);
- cost: the Ollama cloud usage of the last 24 h (bin/lib/models.py), the Claude and Codex limits at the time.
"""
import gzip
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STATE = os.path.join(ROOT, "state")
DAYS = os.path.join(STATE, "days")


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def log_rows(day):
    rows = []
    live = os.path.join(STATE, "log", day + ".jsonl")
    arch = os.path.join(STATE, "log", "archive", day + ".jsonl.gz")
    for path, opener in ((arch, gzip.open), (live, open)):
        try:
            with opener(path, "rt", encoding="utf-8") as f:
                rows += [json.loads(l) for l in f if l.strip()]
        except (OSError, ValueError):
            pass
    return rows


def numbers(day):
    projects = {p["name"]: read(os.path.join(ROOT, "projects", p["name"], "project.json"), {}).get("tag", p["name"])
                for p in read(os.path.join(ROOT, "genius.config.json"), {}).get("projects") or []}
    closed = [o for o in read(os.path.join(STATE, "operations.closed.json"), [])
              if str(o.get("closed", "")).startswith(day) and not str(o.get("id", "")).startswith(("watch-", "interrupted-"))]
    open_ops = read(os.path.join(STATE, "operations.json"), [])
    rows = log_rows(day)
    said = Counter(f"{r['who']}/{r['kind']}" for r in rows)
    gate = [json.loads(l) for l in open(os.path.join(STATE, "gate.jsonl"), encoding="utf-8")
            if l.startswith("{") and l[8:18] == day] if os.path.exists(os.path.join(STATE, "gate.jsonl")) else []
    agents = Counter()
    try:
        terms = json.loads(subprocess.run(["hc", "ls", "--all", "--json"], capture_output=True, text=True, timeout=15).stdout or "{}")
        for t in terms.get("terminals", []):
            if not t.get("name"):
                continue
            info = json.loads(subprocess.run(["hc", "get", t["address"], "--json"], capture_output=True, text=True, timeout=10).stdout or "{}")
            created = info.get("created_ms")          # `hc ls` does not carry it: `hc get` does
            if created is None or datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d") != day:
                continue
            labels = info.get("labels") or {}
            if labels.get("genius.role"):
                agents[labels.get("genius.model", "?")] += 1
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        models = json.loads(subprocess.run([sys.executable, os.path.join(ROOT, "bin", "lib", "models.py"), "--json"],
                                           capture_output=True, text=True, timeout=40).stdout or "{}").get("providers", [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        models = []
    return {
        "date": day,
        "tasks_closed": len(closed),
        "tasks_closed_by_project": dict(Counter(projects.get(o.get("project"), o.get("project", "?")) for o in closed)),
        "tasks_closed_list": [(projects.get(o.get("project"), "") + " " + (o.get("what") or o["id"]))[:120] for o in closed],
        "tasks_open": len([o for o in open_ops if not o.get("paused")]),
        "tasks_waiting_user": len([o for o in open_ops if o.get("needs") == "user" and not o.get("paused")]),
        "user_requests": said.get("user/ask", 0),
        "user_button_answers": said.get("user/answer", 0),
        "agent_briefs": said.get("agent/brief", 0),
        "agents_by_model": dict(agents),
        "gate": dict(Counter(f"{g['decision']}/{g['via'].split('/')[0]}" for g in gate)),
        "models": {p["name"]: (p.get("limits") or (p.get("cloud") or {}).get("usage")) for p in models},
        "recorded_at": datetime.now().astimezone().isoformat(timespec="minutes"),
    }


def save(n):
    os.makedirs(DAYS, exist_ok=True)
    with open(os.path.join(DAYS, n["date"] + ".json"), "w", encoding="utf-8") as f:
        json.dump(n, f, ensure_ascii=False, indent=2)


def summary(n):
    by = ", ".join(f"{k} {v}" for k, v in sorted(n["tasks_closed_by_project"].items(), key=lambda kv: -kv[1])) or "—"
    ag = ", ".join(f"{k} {v}" for k, v in n["agents_by_model"].items()) or "—"
    g = n["gate"]
    return (f"{n['date']}: tasks closed {n['tasks_closed']} ({by}); open {n['tasks_open']}, waiting for the user "
            f"{n['tasks_waiting_user']}; the user's requests {n['user_requests']} + button answers {n['user_button_answers']}; "
            f"agents {sum(n['agents_by_model'].values())} ({ag}); gate allow {g.get('allow/rule', 0) + g.get('allow/judge', 0) + g.get('allow/watcher', 0)}"
            f", deny {g.get('deny/rule', 0) + g.get('deny/judge', 0)}")


def main():
    a = sys.argv[1:]
    if a[:1] == ["trend"]:
        k = int(a[1]) if len(a) > 1 else 7
        files = sorted(f for f in os.listdir(DAYS) if f.endswith(".json"))[-k:] if os.path.isdir(DAYS) else []
        for f in files:
            print(summary(read(os.path.join(DAYS, f), {})))
        if not files:
            print("no days recorded yet")
        return
    day = a[0] if a else date.today().isoformat()
    n = numbers(day)
    save(n)
    print(summary(n))
    for x in n["tasks_closed_list"]:
        print(f"  ✅ {x}")


if __name__ == "__main__":
    main()
