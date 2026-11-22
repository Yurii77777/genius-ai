"""Pending operations: a careful, regular cleanup (the user's decision of 2026-11-12).

    bin/genius ops add "<what>" [--project p] [--ask "…"] [--answers "a|b"] [--check "cmd"] [--next "…"]
                                         one operation at once (an `ask` makes it wait for the user,
                                         `needs: user`); the handoff guard prints the ready command
    bin/genius ops review [--days N]     every operation: its age, days since its last update, and the
                                         output of its read-only `check`; marks the ones to look at
    bin/genius ops close <id> <why>      move one operation to state/operations.closed.json, with the why
    bin/genius ops touch <id>            record that an operation was looked at today (its `updated`)
    bin/genius ops answer <id> <text>    the user's answer from a button of the Herdr panel: stored in the
                                         operation (`answered`), which then no longer waits for the user;
                                         the panel sends the same text to the genius-ai chat
    bin/genius ops later <id>            the user's «later» from the panel: the operation is paused
    bin/genius ops sweep                 the automatic cleanup (every 15 min, `ops sweep start`): closes an
                                         operation whose `done_when` command succeeds, or whose pull request
                                         (named in `check`) is merged or closed; marks `stale` what waits for
                                         the user with no update for STALE_DAYS days. Never deletes, only
                                         moves to operations.closed.json with the reason.
    bin/genius ops sweep start|stop|status   the background job (a loop from the terminal session, see loop_job)

`review` never closes, changes or writes anything: the agent reads it, closes only what the check proves
finished, and asks the user about the rest (AGENTS.md «Pending operations»). Paused operations are listed
but not checked. A check runs with a timeout; one that fails or hangs is reported as such.

The sweep records each of its two changes in the shadow event queue (bin/lib/events.py, the user's decision
of 2026-11-22): `closed` and `stale`, one record each. It also runs the queue's `archive` (the coordinator's
decision of 2026-11-22): an acknowledged event older than two weeks leaves the queue for
state/events/archive/<date>.jsonl.gz. The queue sends nothing; `bin/genius events diff` compares it with the
lines that really went out.
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
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    import events                               # the shadow event queue (bin/lib/events.py): records, sends nothing
except Exception:                               # a sweep that runs is worth more than its queue
    events = None


def record(subject, transition, project, text):
    """One event of the shadow queue (bin/lib/events.py). No failure here: the sweep closes and marks what it
    must, whatever the queue does."""
    if events:
        events.shadow("ops-sweep", subject, transition, project=project, text=text)


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


def remember(o, text):
    """The user's answer becomes knowledge (AGENTS.md «Log of the day and knowledge»): the question and the
    answer in one record, kind decision, with the project. The day log keeps the raw words of the answer;
    this record names the pair and the project. In the background: a button never waits for a model."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import know
        know.add_later(f"{o.get('ask') or o.get('what') or o['id']} → {text}", o.get("project", ""),
                       "decision", f"ops answer {o['id']}")
    except Exception:
        pass


def answer(op_id, text):
    ops = read(OPS, [])
    for o in ops:
        if o["id"] == op_id:
            o["answered"] = {"text": text, "at": datetime.now().astimezone().isoformat(timespec="minutes")}
            o.pop("needs", None)
            o.pop("stale", None)
            o["updated"] = date.today().isoformat()
            write(OPS, ops)
            remember(o, text)
            print(f"answered: {op_id} — {text}")
            return
    sys.exit(f"no operation «{op_id}»")


def later(op_id):
    ops = read(OPS, [])
    for o in ops:
        if o["id"] == op_id:
            o["paused"] = {"since": date.today().isoformat(), "by": "owner", "why": "later (panel)"}
            write(OPS, ops)
            print(f"later: {op_id}")
            return
    sys.exit(f"no operation «{op_id}»")


def slug(text, fallback="op"):
    """A readable id: «lw ask about the price» → «lw-ask-about-the-price» (ASCII only, never empty)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48].strip("-") or fallback


def unique_id(ops, wanted):
    taken = {o.get("id") for o in ops}
    op_id, n = wanted, 2
    while op_id in taken:
        op_id, n = f"{wanted}-{n}", n + 1
    return op_id


def build(ops, what, project="", ask="", answers=(), check="", nxt="", done_when="",
          op_id="", needs="", queue="", waiting_for=""):
    """One operation as the scripts read it (AGENTS.md «Pending operations»). An operation with `ask`
    waits for the user (`"needs": "user"`) and carries the choices the panel shows as buttons."""
    what = (what or "").strip()
    if not what:
        raise ValueError("ops add needs a «what»: one short line about the work")
    op = {"id": unique_id(ops, slug(op_id or f"{project} {what}")), "project": project,
          "what": what, "waiting_for": (waiting_for or ask).strip(),
          "check": (check or "").strip() or "echo manual", "next": nxt,
          "started": date.today().isoformat(), "updated": date.today().isoformat()}
    if ask.strip():
        op["ask"] = ask.strip()
        needs = needs or "user"
    if needs:
        op["needs"] = needs
    answers = [a.strip() for a in answers if a and a.strip()]
    if answers:
        op["answers"] = answers
    if done_when.strip():
        op["done_when"] = done_when.strip()
    if queue:
        op["queue"] = queue
    return op


def add(what, project="", ask="", answers=(), check="", nxt="", done_when="",
        op_id="", needs="", queue="", waiting_for="", path=None):
    path = path or OPS
    ops = read(path, [])
    op = build(ops, what, project, ask, answers, check, nxt, done_when, op_id, needs, queue, waiting_for)
    ops.append(op)
    write(path, ops)
    print(f"added: {op['id']} — {op['what']}")
    if op.get("ask"):
        print(f"  question: «{op['ask']}»"
              + (f" · answers: {' / '.join(op['answers'])}" if op.get("answers") else ""))
    print(f"  the panel and the handoff quote it as `{op['id']}`")
    return op


SWEEP_LABEL = "genius-ai.ops-sweep"
SWEEP_EVERY = 900
PR_IN_CHECK = re.compile(r"gh (?:-R \S+ )?pr (?:view|checks) (\d+)(?:.*?-R ([\w.-]+/[\w.-]+))?")


def sh(cmd, timeout=45):
    try:
        r = subprocess.run(["/bin/zsh", "-lc", cmd], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, ""


def gh_account_env(repo):
    """`GH_TOKEN="…" ` for the account the config names for this repo (services → github → account), else ''.

    A machine can hold several gh logins and a private repo answers 404 to the wrong one. The active login
    is never switched: the token goes to this one call.
    """
    for s in read(os.path.join(ROOT, "genius.config.json"), {}).get("services") or []:
        if s.get("type") == "github" and s.get("account") and repo in (s.get("repos") or []):
            code, token = sh(f'gh auth token --user "{s["account"]}"')
            if code == 0 and token:
                return f'GH_TOKEN="{token}" '
    return ""


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
    code, out = sh(f'{gh_account_env(repo)}gh pr view {num} -R {repo} --json state -q .state')
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
            record(o["id"], "closed", o.get("project", ""), f"{o['id']} closed: {reason[:120]}")
            closed.append(o["id"])
            continue
        idle = days_since(o.get("updated") or o.get("started", ""))
        if o.get("needs") == "user" and idle is not None and idle >= STALE_DAYS and not o.get("stale"):
            fresh = read(OPS, [])
            for f in fresh:
                if f["id"] == o["id"]:
                    f["stale"] = date.today().isoformat()
            write(OPS, fresh)
            record(o["id"], "stale", o.get("project", ""),
                   f"{o['id']} waits for the user for {STALE_DAYS}+ days")
            marked.append(o["id"])
    try:   # tickets that became ready since the last pass get a critique, a finished one its apply step
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import ticket          # (bin/lib/ticket.py); never breaks the sweep
        started, applied = ticket.sweep()
        critiqued = f"started {started or 'none'}, applied {applied or 'none'}"
    except Exception as e:
        critiqued = f"error {type(e).__name__}"
    archived = ""
    if events:      # the queue is compacted here too: an acknowledged event older than two weeks moves out
        try:
            n = events.archive()["archived"]
            archived = f"; events archived {n}" if n else ""
        except Exception as e:
            archived = f"; events archive error {type(e).__name__}"
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"{stamp} sweep: critiques {critiqued}")
    print(f"{stamp} sweep: closed {closed or 'none'}; stale {marked or 'none'}{archived}")


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
    # «stopped by the user»: the mark keeps bin/lib/workers.py from starting it again until a start clears it
    mark = os.path.join(ROOT, "state", f"{label}.off")
    if action == "start":
        try:
            os.unlink(mark)
        except OSError:
            pass
    elif action == "stop":
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        open(mark, "w").close()
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


PARAMS = {"project", "ask", "answers", "check", "next", "done-when", "id", "needs", "queue", "waiting-for"}


def selftest():
    """No file of the install is touched: build(), unique_id() and one temp file."""
    import tempfile

    op = build([], "Ask about the price", project="lw", ask="Чи підняти ціну з 1 грудня?",
               answers=["yes", "later"], nxt="tell the user the answer")
    assert op["id"] == "lw-ask-about-the-price", op["id"]
    assert op["needs"] == "user" and op["answers"] == ["yes", "later"], op
    assert op["check"] == "echo manual" and op["waiting_for"] == op["ask"], op
    assert op["started"] == date.today().isoformat() and op["next"] == "tell the user the answer", op
    plain = build([op], "Watch the CI", project="lw", check="gh run list")
    assert "needs" not in plain and "ask" not in plain and plain["id"] == "lw-watch-the-ci", plain
    assert unique_id([op], op["id"]) == op["id"] + "-2", unique_id([op], op["id"])
    assert slug("  ") == "op" and slug("PR #12 — merge!") == "pr-12-merge", slug("PR #12 — merge!")
    assert build([], "x", done_when="true", queue="next", needs="user")["done_when"] == "true"
    assert build([], "x", queue="next")["queue"] == "next"
    assert "queue" not in build([], "x", needs="user")
    try:
        build([], "  ")
    except ValueError:
        pass
    else:
        raise AssertionError("an operation without a «what» must be refused")

    # the command line of `ops add` (the handoff guard prints exactly this form)
    o, rest = opts(["Ask", "about", "the", "price", "--project", "lw", "--ask", "Чи підняти ціну?",
                    "--answers", "yes|later", "--next", "tell the user", "--done-when", "true"], PARAMS)
    assert rest == ["Ask", "about", "the", "price"], rest
    assert o["project"] == "lw" and o["answers"] == "yes|later" and o["next"] == "tell the user", o
    assert o["done-when"] == "true" and opts(["--unknown", "x"], PARAMS) == ({}, ["--unknown", "x"])

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "operations.json")
        first = add("Ask about the price", project="lw", ask="Підняти ціну?", path=path)
        second = add("Ask about the price", project="lw", ask="Підняти ціну?", path=path)
        assert [o["id"] for o in read(path, [])] == [first["id"], second["id"]], read(path, [])
        assert second["id"] == first["id"] + "-2", second["id"]
        assert read(path, [])[0]["needs"] == "user"
    print("ops selftest: ok")


def opts(args, names):
    """--name value pairs out of args; the rest stays positional (the «what» of `ops add`)."""
    rest, out, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and a[2:] in names:
            out[a[2:]] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        else:
            rest.append(a)
            i += 1
    return out, rest


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["selftest"]:
        selftest()
    elif not args or args[0] == "review":
        days = int(args[args.index("--days") + 1]) if "--days" in args else STALE_DAYS
        review(days)
    elif args[0] == "close" and len(args) >= 3:
        close(args[1], " ".join(args[2:]))
    elif args[0] == "touch" and len(args) == 2:
        touch(args[1])
    elif args[0] == "answer" and len(args) >= 3:
        answer(args[1], " ".join(args[2:]))
    elif args[0] == "later" and len(args) == 2:
        later(args[1])
    elif args[0] == "add":
        o, rest = opts(args[1:], PARAMS)
        what = " ".join(rest).strip()
        if not what:
            sys.exit("usage: bin/genius ops add \"<what>\" [--project p] [--ask \"…\"] [--answers \"a|b\"] "
                     "[--check \"cmd\"] [--next \"…\"] [--done-when \"cmd\"] [--id x] [--needs user] "
                     "[--queue next] [--waiting-for \"…\"]")
        add(what, project=o.get("project", ""), ask=o.get("ask", ""),
            answers=(o.get("answers") or "").split("|"), check=o.get("check", ""), nxt=o.get("next", ""),
            done_when=o.get("done-when", ""), op_id=o.get("id", ""), needs=o.get("needs", ""),
            queue=o.get("queue", ""), waiting_for=o.get("waiting-for", ""))
    elif args[0] == "sweep" and len(args) == 1:
        sweep()
    elif args[0] == "sweep" and args[1] in ("start", "stop", "status"):
        sweep_job(args[1])
    else:
        sys.exit("usage: bin/genius ops add \"<what>\" [--project p] [--ask \"…\"] [--answers \"a|b\"] [--check \"cmd\"] "
                 "[--next \"…\"] | review [--days N] | close <id> <why> | touch <id> | answer <id> <text> | later <id> | "
                 "sweep [start|stop|status] | selftest")
