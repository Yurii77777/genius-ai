"""Pending operations: a careful, regular cleanup (the user's decision of 2026-11-12).

    bin/genius ops review [--days N]     every operation: its age, days since its last update, and the
                                         output of its read-only `check`; marks the ones to look at
    bin/genius ops close <id> <why>      move one operation to state/operations.closed.json, with the why
    bin/genius ops touch <id>            record that an operation was looked at today (its `updated`)
    bin/genius ops sweep                 the automatic cleanup (every 15 min, `ops sweep start`): closes an
                                         operation whose `done_when` command succeeds, or whose pull request
                                         (named in `check`) is merged or closed; marks `stale` what waits for
                                         the user with no update for STALE_DAYS days. Never deletes, only
                                         moves to operations.closed.json with the reason.
    bin/genius ops sweep start|stop|status   the background job (a loop from the terminal session, see loop_job)

`review` never closes, changes or writes anything: the agent reads it, closes only what the check proves
finished, and asks the user about the rest (AGENTS.md «Pending operations»). Paused operations are listed
but not checked. A check runs with a timeout; one that fails or hangs is reported as such.
"""
import json
import os
import re
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


SWEEP_LABEL = "genius-ai.ops-sweep"
SWEEP_EVERY = 900
PR_IN_CHECK = re.compile(r"gh (?:-R \S+ )?pr (?:view|checks) (\d+)(?:.*?-R ([\w.-]+/[\w.-]+))?")


def sh(cmd, timeout=45):
    try:
        r = subprocess.run(["/bin/zsh", "-lc", cmd], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, ""


def pr_state(op):
    """The state of the pull request the operation's check names: MERGED, CLOSED, OPEN or None."""
    m = PR_IN_CHECK.search(op.get("check", "") or "")
    if not m:
        return None, None
    num, repo = m.group(1), m.group(2)
    if not repo:
        r = re.search(r"-R ([\w.-]+/[\w.-]+)", op.get("check", ""))
        repo = r.group(1) if r else None
    if not repo:
        return None, None
    account = "Yurii-777" if repo.startswith("solidlogictech/") else "Yurii77777"
    code, out = sh(f'GH_TOKEN=$(gh auth token --user {account}) gh pr view {num} -R {repo} --json state -q .state')
    return (out or None) if code == 0 else None, f"{repo}#{num}"


def sweep():
    ops = read(OPS, [])
    closed, marked = [], []
    for o in list(ops):
        if o.get("paused"):
            continue
        reason = None
        if o.get("done_when"):
            code, _ = sh(o["done_when"])
            if code == 0:
                reason = f"done_when succeeded: {o['done_when'][:120]}"
        if reason is None:
            state, pr = pr_state(o)
            if state in ("MERGED", "CLOSED"):
                reason = f"{pr} is {state.lower()}"
        if reason:
            close(o["id"], f"auto (ops sweep): {reason}")
            closed.append(o["id"])
            continue
        idle = days_since(o.get("updated") or o.get("started", ""))
        if o.get("needs") == "user" and idle is not None and idle >= STALE_DAYS and not o.get("stale"):
            fresh = read(OPS, [])
            for f in fresh:
                if f["id"] == o["id"]:
                    f["stale"] = date.today().isoformat()
            write(OPS, fresh)
            marked.append(o["id"])
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"{stamp} sweep: closed {closed or 'none'}; stale {marked or 'none'}")


def sweep_job(action):
    loop_job(action, SWEEP_LABEL, ["ops","sweep"], SWEEP_EVERY, os.path.join(ROOT, "state", "ops-sweep.log"))


def loop_job(action, label, args, every, log):
    """A background loop in its own Herdr terminal («genius-…», kept alive by Herdr, outlives this
    session). Not launchd: a launchd job may not read ~/Desktop (macOS privacy: «Operation not
    permitted»); a child process of a Claude session dies with that session's cleanup. A Herdr
    terminal has the terminal's own access. Without Herdr: a detached process (pid in state/)."""
    import shutil
    import signal
    name = label.replace("genius-ai.", "genius-")
    me = os.path.join(ROOT, "bin", "genius")
    cmd = " ".join(f'"{a}"' for a in [me] + args)
    loop = f'while true; do {cmd} >> "{log}" 2>&1; sleep {every}; done'
    subprocess.run(["launchctl", "remove", label], capture_output=True)   # the old launchd job, if any
    pidfile = os.path.join(ROOT, "state", f"{label}.pid")
    if shutil.which("hc"):
        live = subprocess.run(["hc", "get", name, "--json"], capture_output=True, text=True)
        running = live.returncode == 0 and '"state":"live"' in live.stdout.replace(" ", "")
        if action == "start":
            if running:
                print(f"{name}: already running"); return
            subprocess.run(["hc", "rm", name], capture_output=True)
            subprocess.run(["hc", "run", "--bg", "--json", "--name", name, "--outlive-parent", "--cwd", ROOT,
                            "--", "/bin/zsh", "-c", loop], capture_output=True, text=True, check=True)
            print(f"started {name} (Herdr terminal): every {every} s, log {os.path.relpath(log, ROOT)}")
        elif action == "stop":
            subprocess.run(["hc", "kill", name], capture_output=True)
            print(f"stopped {name}")
        else:
            print("running" if running else "not running")
        return
    try:
        pid = int(open(pidfile).read()); os.kill(pid, 0)
    except Exception:
        pid = None
    if action == "start":
        if pid:
            print(f"{label}: already running (pid {pid})"); return
        p = subprocess.Popen(["/bin/zsh", "-c", loop], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
        open(pidfile, "w").write(str(p.pid))
        print(f"started {label}: every {every} s (pid {p.pid})")
    elif action == "stop":
        if pid:
            os.killpg(pid, signal.SIGTERM)
        print(f"stopped {label}")
    else:
        print(f"running (pid {pid})" if pid else "not running")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "review":
        days = int(args[args.index("--days") + 1]) if "--days" in args else STALE_DAYS
        review(days)
    elif args[0] == "close" and len(args) >= 3:
        close(args[1], " ".join(args[2:]))
    elif args[0] == "touch" and len(args) == 2:
        touch(args[1])
    elif args[0] == "sweep" and len(args) == 1:
        sweep()
    elif args[0] == "sweep" and args[1] in ("start", "stop", "status"):
        sweep_job(args[1])
    else:
        sys.exit("usage: bin/genius ops review [--days N] | close <id> <why> | touch <id> | sweep [start|stop|status]")
