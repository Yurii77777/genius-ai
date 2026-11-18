"""The background jobs of genius-ai come back by themselves.

    python3 bin/lib/workers.py ensure     start each job that is missing and that the user did not stop
    python3 bin/lib/workers.py status     running / missing / stopped by the user, one line per job
    python3 bin/lib/workers.py selftest   checks of the throttle, the stop mark and the start

The lesson of 2026-11-18: a Herdr restart stopped `genius-agents-watch` and `genius-ops-sweep` («exited
(stopped_by_restart)»). The Claude session survived the restart, so no SessionStart hook ran, and nothing
started the jobs again until a human did. The revival is therefore a script, not an instruction: the hooks
that already run often (bin/lib/statusline.py, bin/lib/know.py hook) call spawn(), which starts one `ensure`
pass in its own process at most once per CHECK_EVERY seconds, so a hook never waits for it.

Rules it keeps:
- never a second copy: a job that runs is left alone (`bin/genius … start` is idempotent too);
- a job the user stopped by hand stays off: `bin/genius agents watch stop` / `ops sweep stop` leave
  state/<label>.off, and only a start clears it;
- nothing runs on a fresh install: the config must be onboarded (the same rule as the SessionStart hook);
- no new daemon, nothing outside this folder, no launchd job.
"""
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
CHECK_EVERY = 180          # one pass at most this often, however many hooks call it
LOCK_STALE = 120           # a lock left by a killed pass holds the next one for no longer than this
WORKERS = [
    {"label": "genius-ai.agents-watch", "args": ["agents", "watch"]},
    {"label": "genius-ai.ops-sweep", "args": ["ops", "sweep"]},
]


def name_of(label):
    """'genius-ai.agents-watch' → 'genius-agents-watch': the Herdr terminal name of the job."""
    return label.replace("genius-ai.", "genius-")


def state(root, file):
    return os.path.join(root, "state", file)


def stopped(worker, root):
    """The user stopped this job by hand: state/<label>.off keeps it off until a start clears the mark."""
    return os.path.exists(state(root, worker["label"] + ".off"))


def running(worker, root):
    """The job runs now: a live Herdr terminal, else the pid of a detached process (see loop_job)."""
    if shutil.which("hc"):
        try:
            r = subprocess.run(["hc", "get", name_of(worker["label"]), "--json"],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and '"state":"live"' in r.stdout.replace(" ", "")
    try:
        with open(state(root, worker["label"] + ".pid"), encoding="utf-8") as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def start(worker, root):
    """`bin/genius <args> start`: it starts a missing job and says «already running» for one that runs."""
    try:
        r = subprocess.run([os.path.join(root, "bin", "genius"), *worker["args"], "start"],
                           capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def onboarded(root):
    try:
        with open(os.path.join(root, "genius.config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("onboarded"))
    except (OSError, ValueError):
        return False


def _stamp_age(path, now):
    try:
        with open(path, encoding="utf-8") as f:
            return now - float(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def ensure(root=ROOT, now=None, running_fn=None, start_fn=None):
    """Start every missing job whose stop mark is absent. Returns the names started (none on most calls)."""
    now = time.time() if now is None else now
    running_fn = running_fn or (lambda w: running(w, root))
    start_fn = start_fn or (lambda w: start(w, root))
    stamp = state(root, "workers.stamp")
    age = _stamp_age(stamp, now)
    if age is not None and age < CHECK_EVERY:
        return []
    lock = state(root, "workers.lock")
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
    except OSError:
        return []
    fd = None
    for attempt in (0, 1):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            try:
                old = now - os.path.getmtime(lock)
            except OSError:
                old = 0
            if attempt == 0 and old >= LOCK_STALE:
                try:
                    os.unlink(lock)              # a pass that was killed: its lock must not hold the next one
                except OSError:
                    return []
                continue
            return []
        except OSError:
            return []
    try:
        os.close(fd)                                  # the stamp is what throttles: the lock only holds the pass
        try:
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(str(now))
        except OSError:
            pass
        if not onboarded(root):
            return []
        started = []
        for w in WORKERS:
            if stopped(w, root) or running_fn(w):
                continue
            if start_fn(w):
                started.append(name_of(w["label"]))
        return started
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def spawn(root=ROOT, now=None):
    """For a hook that must not wait: one `ensure` in its own process, at most once per CHECK_EVERY."""
    now = time.time() if now is None else now
    age = _stamp_age(state(root, "workers.stamp"), now)
    if age is not None and age < CHECK_EVERY:
        return False
    try:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "ensure", root], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, cwd=root)
    except OSError:
        return False
    return True


def status(root=ROOT):
    for w in WORKERS:
        line = ("stopped by the user" if stopped(w, root)
                else "running" if running(w, root) else "missing")
        print(f"{name_of(w['label']):22} {line}")


# ---- self-check ----------------------------------------------------------------------------------------------

def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "state"))
        with open(os.path.join(d, "genius.config.json"), "w", encoding="utf-8") as f:
            f.write('{"onboarded": true}')
        calls = []

        def missing(w):
            return False

        def present(w):
            return True

        def start_fn(w):
            calls.append(name_of(w["label"]))
            return True

        # missing → each job is started once
        assert ensure(d, now=1000, running_fn=missing, start_fn=start_fn) == ["genius-agents-watch", "genius-ops-sweep"]
        assert len(calls) == 2, calls
        # the same moment again (any number of hooks): nothing, the throttle holds
        calls.clear()
        assert ensure(d, now=1000 + CHECK_EVERY - 1, running_fn=missing, start_fn=start_fn) == [] and calls == []
        # present → nothing, even after the throttle
        calls.clear()
        assert ensure(d, now=1000 + 2 * CHECK_EVERY, running_fn=present, start_fn=start_fn) == [] and calls == []
        # a stop mark holds one job off; the other still comes back
        calls.clear()
        open(os.path.join(d, "state", "genius-ai.agents-watch.off"), "w").close()
        assert ensure(d, now=1000 + 3 * CHECK_EVERY, running_fn=missing, start_fn=start_fn) == ["genius-ops-sweep"]
        assert calls == ["genius-ops-sweep"], calls
        # not onboarded → nothing at all
        calls.clear()
        os.unlink(os.path.join(d, "state", "genius-ai.agents-watch.off"))
        with open(os.path.join(d, "genius.config.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        assert ensure(d, now=1000 + 4 * CHECK_EVERY, running_fn=missing, start_fn=start_fn) == []
        # spawn is throttled by the stamp the last pass wrote and starts no process of its own
        assert spawn(d, now=1000 + 4 * CHECK_EVERY + 1) is False
    print("workers selftest: ok")


def main():
    a = sys.argv[1:]
    if a[:1] == ["ensure"]:
        started = ensure(a[1] if len(a) > 1 else ROOT)
        print("workers: started " + (", ".join(started) or "nothing"))
    elif a == ["status"]:
        status()
    elif a == ["selftest"]:
        selftest()
    else:
        sys.exit(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
