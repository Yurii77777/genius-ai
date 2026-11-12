"""Pending operations: a careful, regular cleanup (the user's decision of 2026-11-12).

    bin/genius ops review [--days N]     every operation: its age, days since its last update, and the
                                         output of its read-only `check`; marks the ones to look at
    bin/genius ops close <id> <why>      move one operation to state/operations.closed.json, with the why
    bin/genius ops touch <id>            record that an operation was looked at today (its `updated`)

`review` never closes, changes or writes anything: the agent reads it, closes only what the check proves
finished, and asks the user about the rest (AGENTS.md «Pending operations»). Paused operations are listed
but not checked. A check runs with a timeout; one that fails or hangs is reported as such.
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
OPS = os.path.join(ROOT, "state", "operations.json")
CLOSED = os.path.join(ROOT, "state", "operations.closed.json")
STALE_DAYS = 3


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def days_since(stamp):
    try:
        return (date.today() - datetime.fromisoformat(str(stamp)[:10]).date()).days
    except ValueError:
        return None


def run_check(cmd):
    if not cmd or cmd.strip() in ("echo manual", "echo deferred"):
        return "—", "no check"
    try:
        r = subprocess.run(["/bin/zsh", "-lc", cmd], cwd=ROOT, capture_output=True, text=True, timeout=45)
        out = " ".join((r.stdout or r.stderr).split())[:160]
        return ("ok" if r.returncode == 0 else f"exit {r.returncode}"), out or "(no output)"
    except subprocess.TimeoutExpired:
        return "timeout", "the check did not answer in 45 s"


def review(stale_days):
    ops = read(OPS, [])
    for o in sorted(ops, key=lambda x: (x.get("project", ""), x.get("started", ""))):
        age = days_since(o.get("started", ""))
        idle = days_since(o.get("updated") or o.get("started", ""))
        flags = []
        if o.get("paused"):
            flags.append("paused")
        elif idle is not None and idle >= stale_days:
            flags.append(f"STALE {idle}d")
        if o.get("needs") == "user":
            flags.append("waits for the user")
        print(f"## {o.get('project', '?')} · {o['id']} · age {age}d · {', '.join(flags) or 'fresh'}")
        print(f"   what: {o.get('what', '')[:150]}")
        print(f"   waits: {o.get('waiting_for', '')[:150]}")
        if not o.get("paused"):
            state, out = run_check(o.get("check", ""))
            print(f"   check [{state}]: {out}")
    print(f"\n{len(ops)} operations. Close only what a check proves finished: bin/genius ops close <id> <why>.")


def close(op_id, why):
    ops = read(OPS, [])
    hit = [o for o in ops if o["id"] == op_id]
    if not hit:
        sys.exit(f"no operation «{op_id}»")
    archive = read(CLOSED, [])
    archive.append({**hit[0], "closed": date.today().isoformat(), "closed_why": why})
    write(CLOSED, archive)
    write(OPS, [o for o in ops if o["id"] != op_id])
    print(f"closed: {op_id} — {why}")


def touch(op_id):
    ops = read(OPS, [])
    for o in ops:
        if o["id"] == op_id:
            o["updated"] = date.today().isoformat()
            write(OPS, ops)
            print(f"touched: {op_id}")
            return
    sys.exit(f"no operation «{op_id}»")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "review":
        days = int(args[args.index("--days") + 1]) if "--days" in args else STALE_DAYS
        review(days)
    elif args[0] == "close" and len(args) >= 3:
        close(args[1], " ".join(args[2:]))
    elif args[0] == "touch" and len(args) == 2:
        touch(args[1])
    else:
        sys.exit("usage: bin/genius ops review [--days N] | close <id> <why> | touch <id>")
