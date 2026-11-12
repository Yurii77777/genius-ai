"""Watch every sub-agent in Herdr, so no agent waits unseen (the user's decision of 2026-11-12:
«ці всі правила до пизди… потрібен воркер над оцім всім агентами»).

    bin/genius agents watch                    one pass (the background job runs it every 30 s)
    bin/genius agents watch start|stop|status  the background job (a background loop from the terminal session)

For each sub-agent (a Herdr terminal with a parent) one pass does this:

- The agent waits, and its input holds text unchanged for SEND_AFTER s: that is the user's own
  answer (typed or dictated with F5) → press Enter, so it is sent. A permission or trust dialog
  is never answered.
- The agent waits with an empty input for WAIT_AFTER s (it asks, or it finished): its last
  message is saved to state/agents/<name>.md, an operation with "needs": "user" goes to
  state/operations.json (Herdr «Attention» + toast), the voice says it, and the parent session
  gets one line, so it reads the report. Once per turn of the agent.
- A permission or trust dialog → the same alert, never an answer.
- The agent works, and its screen did not change for HUNG_AFTER s → one alert «hung».
- An API error (the Mac slept mid-answer) → it is told once per turn to continue.
- The agent works again or is gone → its watch operation is closed.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STATE = os.path.join(ROOT, "state", "agents-watch.json")
OPS = os.path.join(ROOT, "state", "operations.json")
AGENTS_DIR = os.path.join(ROOT, "state", "agents")
LABEL = "genius-ai.agents-watch"
EVERY = 30
SEND_AFTER = 60
WAIT_AFTER = 120
HUNG_AFTER = 15 * 60
RESUME = ("The previous answer broke off (API error, e.g. the Mac slept). Continue the task from where you "
          "stopped: first check the branch, the tests and the running processes.")

WORDS = {
    "uk": {"waits": "агент {n} чекає на вашу відповідь або закінчив", "hung": "агент {n} не рухається {m} хвилин",
           "dialog": "агент {n} чекає на дозвіл", "sent": "надіслала агенту {n} вашу відповідь"},
    "en": {"waits": "agent {n} waits for your answer or has finished", "hung": "agent {n} has not moved for {m} minutes",
           "dialog": "agent {n} waits for a permission", "sent": "sent your answer to agent {n}"},
}


def read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def sh(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def project_of(cwd, cfg):
    """The config project whose path holds the agent's folder: its name, tag and spoken name."""
    best = None
    for p in cfg.get("projects") or []:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if cwd == path or cwd.startswith(path + os.sep):
            if best is None or len(path) > len(best[1]):
                best = (p["name"], path)
    if best is None:
        return None, "", ""
    meta = read(os.path.join(ROOT, "projects", best[0], "project.json"), {})
    return best[0], meta.get("tag", ""), meta.get("spoken", best[0])


def draft_of(screen):
    """The text in Claude Code's input box: the «❯ …» line between the last two rules. None when
    the bottom of the screen is a dialog (permission, trust, a numbered choice)."""
    lines = screen.rstrip("\n").split("\n")[-14:]
    tail = "\n".join(lines)
    if re.search(r"Do you want|trust this folder|❯\s*\d+\.\s", tail):
        return None
    rules = [i for i, l in enumerate(lines) if l.strip().startswith("────")]
    if len(rules) < 2:
        return None
    box = lines[rules[-2] + 1:rules[-1]]
    if not box or not box[0].lstrip().startswith("❯"):
        return None
    text = " ".join([box[0].lstrip()[1:].strip()] + [l.strip() for l in box[1:]]).strip()
    return text


def last_message(screen):
    """The agent's last message: from its last «⏺» line that is not a tool call, to the input box."""
    lines = screen.rstrip("\n").split("\n")
    start = 0
    for i, l in enumerate(lines):
        if l.startswith("⏺") and not re.match(r"⏺ (Bash|Read|Write|Edit|Update|Search|Agent|Skill)\(", l):
            start = i
    end = next((i for i in range(len(lines) - 1, start, -1) if lines[i].strip().startswith("────")), len(lines))
    rules = [i for i in range(start, len(lines)) if lines[i].strip().startswith("────")]
    if len(rules) >= 2:
        end = rules[-2]
    return "\n".join(lines[start:end]).strip()


def set_op(name, project, what, needs_user):
    ops = read(OPS, [])
    oid = f"watch-{name}"
    ops = [o for o in ops if o.get("id") != oid]
    if what:
        ops.append({"id": oid, "project": project or "genius-ai", "what": what,
                    "waiting_for": "user" if needs_user else "agent", **({"needs": "user"} if needs_user else {}),
                    "check": f"hc get {name} --json", "done_when": f"! hc get {name} --json >/dev/null 2>&1",
                    "next": f"прочитати state/agents/{name}.md і відповісти агенту",
                    "started": datetime.now().astimezone().isoformat(timespec="minutes"), "by": "agents-watch"})
    write(OPS, ops)


def alert(term, text, spoken, cfg):
    """One alert: the voice (it stays silent when sound is off), and one line to the parent session."""
    voice = os.path.join(ROOT, "voice", "bin", "speak")
    if os.path.exists(voice) and (cfg.get("voice") or {}).get("enabled"):
        subprocess.Popen([voice, f"{spoken}: {text}" if spoken else text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if term.get("parent"):
        sh(["hc", "send", term["parent"], f"[agents-watch] {term.get('name')}: {text}"], timeout=60)


def watch():
    cfg = read(os.path.join(ROOT, "genius.config.json"), {})
    W = WORDS.get(cfg.get("language"), WORDS["en"])
    st = read(STATE, {})
    now = time.time()
    data = json.loads(sh(["hc", "ls", "--json"]) or "{}")
    seen = set()
    for t in data.get("terminals", []):
        name = t.get("name")
        if not name or not t.get("parent") or t.get("agent") != "claude-code" or t.get("state") != "live":
            continue
        seen.add(name)
        s = st.setdefault(name, {})
        status = t.get("status")                    # `hc ls` gives a word; `hc get` an object
        status = status.get("state") if isinstance(status, dict) else status
        screen = sh(["hc", "read", name])
        if not screen:
            continue
        project, tag, spoken = project_of(os.path.realpath(t.get("cwd", "")), cfg)
        digest = hashlib.sha1(screen.encode()).hexdigest()
        if digest != s.get("hash"):
            s["hash"], s["changed"] = digest, now
        tail = "\n".join(screen.rstrip().split("\n")[-10:])
        own_shell = re.search(r"\bshells? still running|still running", tail) is not None
        draft = draft_of(screen)

        if status == "working":
            s.pop("idle", None); s.pop("draft", None)
            if s.pop("alerted", None):
                set_op(name, project, None, False)
            if now - s.get("changed", now) > HUNG_AFTER and not s.get("hung"):
                s["hung"] = True
                alert(t, W["hung"].format(n=name, m=HUNG_AFTER // 60), spoken, cfg)
            elif now - s.get("changed", now) <= HUNG_AFTER:
                s.pop("hung", None)
            continue

        # An API error (the Mac slept mid-answer, a dropped connection): resume once per turn.
        if status == "error" and re.search(r"API Error|went to sleep", tail) and s.get("resumed") != t.get("turn"):
            s["resumed"] = t.get("turn")
            sh(["hc", "send", name, RESUME], timeout=60)
            print(f"{datetime.now().isoformat(timespec='seconds')} resumed {name} after an API error")
            continue

        if draft is None and re.search(r"Do you want|trust this folder|❯\s*\d+\.\s", tail):
            if s.get("alerted") != f"dialog-{t.get('turn')}":
                s["alerted"] = f"dialog-{t.get('turn')}"
                set_op(name, project, f"{tag} {name}: {W['dialog'].format(n=name)}".strip(), True)
                alert(t, W["dialog"].format(n=name), spoken, cfg)
            continue

        if draft:                                   # the user's own answer waits in the input
            if s.get("draft") != draft:
                s["draft"], s["draft_since"] = draft, now
            elif now - s["draft_since"] >= SEND_AFTER:
                sh(["hc", "keys", name, "enter"])
                print(f"{datetime.now().isoformat(timespec='seconds')} sent {name}: {draft[:80]}")
                alert(t, W["sent"].format(n=name), "", cfg)
                s.pop("draft", None); s.pop("idle", None)
            continue
        s.pop("draft", None)

        if own_shell:                               # it waits for its own background command
            continue
        s.setdefault("idle", now)
        if now - s["idle"] >= WAIT_AFTER and s.get("alerted") != f"turn-{t.get('turn')}":
            s["alerted"] = f"turn-{t.get('turn')}"
            os.makedirs(AGENTS_DIR, exist_ok=True)
            with open(os.path.join(AGENTS_DIR, f"{name}.md"), "w") as f:
                f.write(f"# {name} — {datetime.now().isoformat(timespec='minutes')}\n\n{last_message(screen)}\n")
            set_op(name, project, f"{tag} {name}: {W['waits'].format(n=name)}".strip(), True)
            alert(t, W["waits"].format(n=name) + f" (state/agents/{name}.md)", spoken, cfg)

    for name in list(st):                           # gone: close its watch operation
        if name not in seen:
            if st[name].get("alerted"):
                set_op(name, None, None, False)
            del st[name]
    write(STATE, st)


def job(action):
    loop_job(action, LABEL, ["agents","watch"], EVERY, os.path.join(ROOT, "state", "agents-watch.log"))


def loop_job(action, label, args, every, log):
    """A background loop started from this Mac's terminal session, not by launchd: a launchd job
    may not read ~/Desktop (macOS privacy: «Operation not permitted»), a child of the terminal may.
    Its pid is in state/<label>.pid; a reboot ends it and the SessionStart hook starts it again."""
    import signal
    pidfile = os.path.join(ROOT, "state", f"{label}.pid")
    def alive():
        try:
            pid = int(open(pidfile).read())
            os.kill(pid, 0)
            return pid
        except Exception:
            return None
    subprocess.run(["launchctl", "remove", label], capture_output=True)   # the old launchd job, if any
    pid = alive()
    if action == "start":
        if pid:
            print(f"{label}: already running (pid {pid})")
            return
        me = os.path.join(ROOT, "bin", "genius")
        cmd = " ".join(f'"{a}"' for a in [me] + args)
        loop = f'while true; do {cmd} >> "{log}" 2>&1; sleep {every}; done'
        p = subprocess.Popen(["/bin/zsh", "-c", loop], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
        open(pidfile, "w").write(str(p.pid))
        print(f"started {label}: every {every} s (pid {p.pid}), log {os.path.relpath(log, ROOT)}")
    elif action == "stop":
        if pid:
            os.killpg(pid, signal.SIGTERM)
        print(f"stopped {label}")
    else:
        print(f"running (pid {pid})" if pid else "not running")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["watch"]:
        watch()
    elif len(a) == 2 and a[0] == "watch" and a[1] in ("start", "stop", "status"):
        job(a[1])
    else:
        sys.exit("usage: bin/genius agents watch [start|stop|status]")
