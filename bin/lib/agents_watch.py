"""Watch every sub-agent in Herdr, so no agent waits unseen (the user's decision of 2026-11-12:
«ці всі правила до пизди… потрібен воркер над оцім всім агентами»).

    bin/genius agents watch                    one pass (the background job runs it every 30 s)
    bin/genius agents watch start|stop|status  the background job (a background loop from the terminal session)

For each sub-agent (a Herdr terminal with a parent) one pass does this:

- The user's rule of 2026-11-18: the watcher works only while at least one attached project is active
  (in genius.config.json → projects, and not paused). No active project → the pass reads nothing, answers
  nothing, says nothing, and logs one line «idle: no active project» when that state begins. An agent
  outside every active project (a paused project, a folder that is not a project) is never answered,
  never gets an Enter, never raises an alert.
- The agent waits, and its input holds text unchanged for SEND_AFTER s: that is the user's own
  answer (typed or dictated with F5) → press Enter, so it is sent. A permission or trust dialog
  is never answered.
- The agent waits with an empty input for WAIT_AFTER s (it asks, or it finished): its last
  message is saved to state/agents/<name>.md, an operation with "needs": "user" goes to
  state/operations.json (Herdr «Attention» + toast), the voice says it, and the parent session
  gets one line, so it reads the report. Once per turn of the agent.
- A permission dialog of a task agent (label genius.role) → the gate decides it, like any other action
  of that agent (the user's decision of 2026-11-17: «потрібно думати як мене розвантажити»): the action on
  the screen goes to bin/lib/gate.py, its rules only (the user's decision of 2026-11-21: «те, що не
  покривають правила, йде до координатора»). Allow → the watcher picks the dialog's one-time «Yes»; deny
  or no rule → nothing is pressed, and the alert path below runs: the screen is saved, a watch operation
  waits for the user, and one [agents-watch] line goes to the coordinator. Every decision is written to
  state/gate.jsonl ("via": "watcher"). A task agent's trust dialog in a folder of a configured project is
  answered «trust» (the user's decision of 2026-11-17); a trust dialog anywhere else, and a dialog of any
  other agent, is never answered.
- A question of a task agent — an AskUserQuestion dialog on its screen, or an idle agent whose last message
  ends with a question — is answered here when the knowledge base holds the user's own decision on it
  (bin/lib/agent_ask.py): the rules first (never a merge, a push, a release, prod, money, a hold, legal, new
  product text), then the base, then a judge. Not covered, the judge silent, any error → the user, as before.
  An answer goes to state/gate.jsonl, to state/agent-answers.json (the panel's «Answered for you», with
  «overrule»), and the user's own answer to a question becomes a knowledge record (kind decision).
- The agent works, and its screen did not change for HUNG_AFTER s → one alert «hung».
- An API error (the Mac slept mid-answer) → it is told once per turn to continue.
- The agent works again or is gone → its watch operation is closed.
- The agent is gone because Herdr or the Mac restarted (`reason: stopped_by_restart`) → its last screen
  is saved to state/agents/<name>.md and an operation `interrupted-<name>` asks the user: continue or
  not (buttons on the panel). «Continue» is a new sub-agent whose brief points to the saved report.
- Dev servers (the user's decision of 2026-11-15: «потрібно контролювати, щоб агенти вчасно гасили
  після себе»): every listening port 4300–4399 is matched to its agent by state/ports.json (the
  coordinator writes the port block into each brief and into this file). Its agent idle for
  SERVER_IDLE → told once per turn to stop its servers; its agent gone → the server is stopped here;
  a port with no owner → one line to the coordinator.

Every signal of this file (finished, waits, permission, hung, interrupted) is also recorded in the shadow
event queue (bin/lib/events.py, the user's decision of 2026-11-22). The queue sends nothing: each `hc send`
stays as it is, and the line it really sent is written to state/events/sent.jsonl next to the event, so
`bin/genius events diff` can prove that no signal was lost. A queue that cannot be written never stops the
watcher.
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
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pause                                    # the paused mark: the one reading of which projects are active
try:
    import events                               # the shadow event queue (bin/lib/events.py): records, sends nothing
except Exception:                               # a watcher that runs is worth more than its queue
    events = None
STATE = os.path.join(ROOT, "state", "agents-watch.json")
OPS = os.path.join(ROOT, "state", "operations.json")
AGENTS_DIR = os.path.join(ROOT, "state", "agents")
LABEL = "genius-ai.agents-watch"
EVERY = 30
SEND_AFTER = 60
WAIT_AFTER = 60
HUNG_AFTER = 15 * 60
SERVER_IDLE = 5 * 60        # an agent idle this long with its dev server still up → told to stop it
PORTS = os.path.join(ROOT, "state", "ports.json")   # {"4310": "<tag>-<task>", "4300": "coordinator", ...}
RESUME = ("The previous answer broke off (API error, e.g. the Mac slept). Continue the task from where you "
          "stopped: first check the branch, the tests and the running processes.")

WORDS = {
    "uk": {"waits": "агент {n} чекає на вашу відповідь або закінчив", "hung": "агент {n} не рухається {m} хвилин",
           "dialog": "агент {n} чекає на дозвіл", "sent": "надіслала агенту {n} вашу відповідь",
           "draft": "в агента {n} у полі вводу є текст, я його не надсилала",
           "cut": "перерваний перезапуском", "cut_ask": "Агент {n} перерваний перезапуском. Продовжити його роботу?",
           "cut_yes": "continue", "cut_no": "drop"},
    "en": {"waits": "agent {n} waits for your answer or has finished", "hung": "agent {n} has not moved for {m} minutes",
           "dialog": "agent {n} waits for a permission", "sent": "sent your answer to agent {n}",
           "draft": "agent {n} has text in its input; not sent",
           "cut": "interrupted by a restart", "cut_ask": "Agent {n} was interrupted by a restart. Continue its work?",
           "cut_yes": "continue", "cut_no": "drop"},
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


def project_of(cwd, active):
    """The active project whose path holds the agent's folder (a worktree inside it counts): its name,
    tag and spoken name. (None, "", "") for a folder outside every active project."""
    best = None
    for p in active:
        if cwd == p["path"] or cwd.startswith(p["path"] + os.sep):
            if best is None or len(p["path"]) > len(best["path"]):
                best = p
    if best is None:
        return None, "", ""
    return best["name"], best["tag"], best["spoken"]


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


def interrupted(name, info, cfg, W, active):
    """A sub-agent that a restart stopped: keep its last screen, and ask the user on the panel."""
    project, tag, spoken = project_of(os.path.realpath(info.get("cwd") or ""), active)
    report = os.path.join(AGENTS_DIR, f"{name}.md")
    screen = sh(["hc", "read", name])
    os.makedirs(AGENTS_DIR, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="minutes")
    with open(report, "a") as f:
        f.write(f"\n## {name} — {W['cut']}, {stamp}\n\n{last_message(screen) or screen[-4000:]}\n")
    ops = [o for o in read(OPS, []) if o.get("id") != f"interrupted-{name}"]
    title = info.get("title") or ""
    ops.append({"id": f"interrupted-{name}", "project": project or "genius-ai", "kind": "interrupted", "agent": name,
                "what": f"{name}: {W['cut']}" + (f" — {title}" if title else ""),
                "waiting_for": f"state/agents/{name}.md", "needs": "user",
                "ask": W["cut_ask"].format(n=name), "answers": [W["cut_yes"], W["cut_no"]],
                "check": f"tail -30 state/agents/{name}.md",
                "next": f"«{W['cut_yes']}»: a new sub-agent in {info.get('cwd', '?')} whose brief points to "
                        f"state/agents/{name}.md; «{W['cut_no']}»: close this operation",
                "resume": {"cwd": info.get("cwd"), "title": title, "report": f"state/agents/{name}.md"},
                "started": datetime.now().astimezone().isoformat(timespec="minutes"), "by": "agents-watch"})
    write(OPS, ops)
    eid = record(name, "interrupted", project=project or "", to=info.get("parent") or "",
                 text=f"{tag} {name}: {W['cut']}".strip())
    alert({"name": name, "parent": info.get("parent")}, W["cut"], spoken, cfg, eid)
    print(f"{stamp} interrupted {name}: report saved, the user is asked")


def loop_of(name):
    """The fix↔review loop module when `name` is its current agent, else None (bin/lib/review_loop.py).
    A loop agent's finished turn is the loop's business: no operation per round, no answer from the base."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import review_loop
    except Exception:
        return None
    try:
        return review_loop if review_loop.owning(name) else None
    except Exception:
        return None


def queued(name):
    """The agent holds a place in the heavy queue (bin/heavy): it waits for its turn, it does not hang."""
    try:
        q = json.loads(sh([os.path.join(ROOT, "bin", "heavy"), "--json"]) or "{}")
    except ValueError:
        return False
    # only WAITING counts: a holder whose screen stands still holds the whole queue — that one is worth an alert
    return any(w.get("who") == name for w in (q.get("waiting") or []))


ACTION = re.compile(r"⏺\s*(Write|Edit|Update|MultiEdit|NotebookEdit|Bash|Read|WebFetch)\((.*)\)\s*$")


def answer_dialog(name, term, screen, project):
    """A task agent's permission dialog: the gate decides. True when the watcher answered it."""
    labels = term.get("labels") or {}
    if not labels:
        try:
            labels = json.loads(sh(["hc", "get", name, "--json"]) or "{}").get("labels") or {}
        except ValueError:
            labels = {}
    if not labels.get("genius.role"):
        return False
    lines = screen.rstrip("\n").split("\n")
    tail = lines[-30:]
    if re.search(r"[Tt]rust this folder|Folder access", screen[-3000:]):
        # The user's decision of 2026-11-17 («для цього є вартовий, який має мене розвантажувати»): a task agent
        # started by bin/genius agent in a folder of a configured project may trust that folder; anywhere else
        # the dialog stays the user's.
        cwd = os.path.realpath(term.get("cwd") or "")
        first = next((l for l in tail if re.match(r"\s*[›❯]?\s*1\.\s+(Trust and continue|Yes, I trust)", l)), None)
        if not project or not first:
            return False
        with open(os.path.join(ROOT, "state", "gate.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "session": name, "cwd": cwd,
                                "tool": "trust", "input": cwd, "decision": "allow", "via": "watcher/rule",
                                "why": f"a task agent's trust dialog in the folder of the project {project}"}, ensure_ascii=False) + "\n")
        sh(["hc", "keys", name, "enter"])
        print(f"{datetime.now().isoformat(timespec='seconds')} trusted {cwd} for {name} ({project})")
        return True
    # Codex CLI: «Would you like to run the following command?», the command on «$ …» lines, «› 1. Yes, proceed (y)».
    # The gate decides like for Claude Code; «y» approves once (lesson of 2026-11-17: the owner answered a gate dialog).
    # a long command (a heredoc) pushes the question far above the last 30 lines: look further back
    wide = lines[-250:]
    if any("Would you like to run the following command?" in l for l in wide) and any(re.match(r"\s*[›❯]?\s*1\.\s+Yes", l) for l in tail):
        i = max(k for k, l in enumerate(wide) if "Would you like to run the following command?" in l)
        cmd, started = [], False
        for l in wide[i + 1:]:
            s = l.strip()
            if s.startswith("$ "):
                started = True
                cmd.append(s[2:])
            elif started and not re.match(r"[›❯]?\s*\d+\.\s", s) and not s.startswith("Press enter"):
                cmd.append(l.rstrip()[2:] if l.startswith("  ") else l.rstrip())
            elif started:
                break
        if not cmd or not any(re.match(r"\s*[›❯]?\s*1\.\s+Yes", l) for l in tail):
            return False
        cwd = os.path.realpath(term.get("cwd") or "")
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import gate
        # Codex wraps a long one-line command over several screen lines; only a heredoc keeps real newlines
        command = ("\n".join(cmd) if any("<<" in c for c in cmd) else " ".join(c.strip() for c in cmd)).strip()
        decision, why, via = gate.decide("Bash", {"command": command, "from_screen": True}, cwd, {})
        with open(os.path.join(ROOT, "state", "gate.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "session": name, "cwd": cwd,
                                "tool": "Bash", "input": command[:300], "decision": decision,
                                "via": f"watcher/{via}", "why": why + " (a Codex approval dialog)"}, ensure_ascii=False) + "\n")
        if decision != "allow":
            return False                    # deny, or no rule («ask»): nothing is pressed; the alert below asks the user
        sh(["hc", "keys", name, "y"])
        print(f"{datetime.now().isoformat(timespec='seconds')} answered {name} (codex): {command[:80]} — {why[:80]}")
        return True
    yes = next((l for l in tail if re.match(r"\s*❯?\s*1\.\s+Yes\b", l)), None)
    if not yes:
        return False                                # not a yes/no dialog we know
    action = None
    for l in reversed(lines[-80:]):
        m = ACTION.match(l.strip())
        if m:
            action = m
            break
    if action:
        tool, arg = action.group(1), action.group(2).strip()
    else:
        # the newer dialog: «Run shell command», the command in a box of «│ » lines above «Do you want to proceed?»
        # Variants (lesson of 2026-11-19, AE agents): «Run shell command», «This command requires approval»,
        # or a reason box («Compound command contains cd …») under the command box. The command is the last
        # «│» box closed by a ╌ separator before the question; the reason box runs into the question itself.
        near = lines[-120:]
        if not any(k in l for l in tail for k in ("Run shell command", "This command requires approval", "Do you want to proceed?")):
            return False
        ask = max((i for i, l in enumerate(near) if "Do you want to proceed?" in l), default=len(near))
        box, cur = [], []
        for l in near[:ask]:
            s = l.strip()
            if s.startswith("│"):
                cur.append(s[1:].strip())
            elif s.startswith("╌"):
                if cur:
                    box = cur          # a box closed by a separator: the command (a reason box ends at the question)
                cur = []
            else:
                cur = []
        if not box:
            return False
        tool, arg = "Bash", " ".join(box)
    tool = {"Update": "Edit"}.get(tool, tool)
    cwd = os.path.realpath(term.get("cwd") or "")
    if tool == "Bash":
        ti = {"command": arg}
    elif tool == "WebFetch":
        ti = {"url": arg}
    else:
        ti = {"file_path": arg if os.path.isabs(os.path.expanduser(arg)) else os.path.join(cwd, arg)}
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import gate
    decision, why, via = gate.decide(tool, ti, cwd, {})
    with open(os.path.join(ROOT, "state", "gate.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "session": name, "cwd": cwd,
                            "tool": tool, "input": json.dumps(ti, ensure_ascii=False)[:300], "decision": decision,
                            "via": f"watcher/{via}", "why": why + " (a Claude Code permission dialog)"}, ensure_ascii=False) + "\n")
    if decision != "allow":
        # deny, or no rule («ask», the user's decision of 2026-11-21): press nothing. The alert below saves the
        # screen, opens the watch operation and tells the coordinator, which decides with the user.
        return False
    # the cursor is on «1. Yes» (❯): Enter picks it; else the digit moves to it and picks it
    sh(["hc", "keys", name, "enter" if "❯" in yes else "1"])
    print(f"{datetime.now().isoformat(timespec='seconds')} answered {name}: {tool} {arg[:80]} — {why[:80]}")
    return True



def unpushed_branches(cwd):
    """Local branches of the agent's repo whose commits are on no remote branch and in no PR head.
    The lesson of 2026-11-20 («хуйовасто за такі пройоби»): an agent ends with «committed, not
    pushed», the coordinator reads the report and forgets the branch. A script names it every time."""
    if not cwd or not os.path.isdir(cwd):
        return []
    top = sh(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()
    if not top:
        return []
    repo = os.path.dirname(top) if top.endswith("/.git") else top
    out = []
    for line in sh(["git", "-C", repo, "for-each-ref", "--format=%(refname:short) %(committerdate:unix)",
                    "refs/heads"]).splitlines():
        try:
            br, ts = line.rsplit(" ", 1)
        except ValueError:
            continue
        if br in ("develop", "main", "master") or time.time() - int(ts or 0) > 3 * 86400:
            continue
        if sh(["git", "-C", repo, "rev-list", "--count", br, "--not", "--remotes"]).strip() in ("", "0"):
            continue
        # merge commits of work that already landed (a review branch) are not lost work: count only
        # the patches that no remote branch holds (`git cherry`, the same test as bin/status)
        base = next((b for b in ("origin/develop", "origin/main", "origin/master")
                     if sh(["git", "-C", repo, "rev-parse", "-q", "--verify", b]).strip()), None)
        n = str(sum(1 for l in sh(["git", "-C", repo, "cherry", base, br]).splitlines() if l.startswith("+"))) if base else "1"
        if n != "0":
            out.append(f"{br} ({n} commit{'s' if n != '1' else ''} not pushed)")
    return out


def unpushed_branches(cwd):
    """Local branches of the agent's repo whose commits are on no remote branch and in no PR head.
    The lesson of 2026-11-20 («хуйовасто за такі пройоби»): an agent ends with «committed, not
    pushed», the coordinator reads the report and forgets the branch. A script names it every time."""
    if not cwd or not os.path.isdir(cwd):
        return []
    top = sh(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()
    if not top:
        return []
    repo = os.path.dirname(top) if top.endswith("/.git") else top
    out = []
    for line in sh(["git", "-C", repo, "for-each-ref", "--format=%(refname:short) %(committerdate:unix)",
                    "refs/heads"]).splitlines():
        try:
            br, ts = line.rsplit(" ", 1)
        except ValueError:
            continue
        if br in ("develop", "main", "master") or time.time() - int(ts or 0) > 3 * 86400:
            continue
        if sh(["git", "-C", repo, "rev-list", "--count", br, "--not", "--remotes"]).strip() in ("", "0"):
            continue
        # merge commits of work that already landed (a review branch) are not lost work: count only
        # the patches that no remote branch holds (`git cherry`, the same test as bin/status)
        base = next((b for b in ("origin/develop", "origin/main", "origin/master")
                     if sh(["git", "-C", repo, "rev-parse", "-q", "--verify", b]).strip()), None)
        n = str(sum(1 for l in sh(["git", "-C", repo, "cherry", base, br]).splitlines() if l.startswith("+"))) if base else "1"
        if n != "0":
            out.append(f"{br} ({n} commit{'s' if n != '1' else ''} not pushed)")
    return out

def alert(term, text, spoken, cfg, event=None):
    """One alert: the voice (it stays silent when sound is off), and one line to the parent session.
    `event` is the id the shadow queue gave the same signal: the line that really goes out is recorded
    with it, so `bin/genius events diff` compares the two sides. The send itself does not change."""
    voice = os.path.join(ROOT, "voice", "bin", "speak")
    if os.path.exists(voice) and (cfg.get("voice") or {}).get("enabled"):
        subprocess.Popen([voice, f"{spoken}: {text}" if spoken else text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if term.get("parent"):
        line = f"[agents-watch] {term.get('name')}: {text}"
        sh(["hc", "send", term["parent"], line], timeout=60)
        record_sent(term["parent"], line, event)


def record(subject, transition, project="", to="", send="", text=""):
    """One event of the shadow queue (bin/lib/events.py): recorded, never sent. Its id, or None."""
    if not events:
        return None
    return events.shadow("agents-watch", subject, transition, project=project, to=to, send=send, text=text)


def record_sent(to, text, event):
    """The line that really went out, with the event it carries: the other side of `events diff`."""
    if not events:
        return
    try:
        events.note_sent(to, text, "agents-watch", event or "")
    except Exception:
        pass


def watch():
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import agent_ask as ask                         # a question answered from the knowledge base
    cfg = read(os.path.join(ROOT, "genius.config.json"), {})
    W = WORDS.get(cfg.get("language"), WORDS["en"])
    st = read(STATE, {})
    now = time.time()
    active = pause.active(cfg, ROOT)
    if not active:
        # The user's rule of 2026-11-18: the watcher works only while at least one attached project is
        # active. With every project paused nothing is read, answered or said; the line is written once,
        # when this state begins, not every 30 s.
        if not st.get("_idle"):
            st["_idle"] = datetime.now().astimezone().isoformat(timespec="minutes")
            write(STATE, st)
            print(f"{datetime.now().isoformat(timespec='seconds')} idle: no active project")
        return
    st.pop("_idle", None)
    active_names = {p["name"] for p in active}
    data = json.loads(sh(["hc", "ls", "--json"]) or "{}")
    seen = set()
    for t in data.get("terminals", []):
        name = t.get("name")
        if not name or not t.get("parent") or t.get("state") != "live":
            continue
        project, tag, spoken = project_of(os.path.realpath(t.get("cwd") or ""), active)
        if project is None:
            # Outside every active project: a paused project, or a folder that is not a project. No screen
            # read, no Enter, no alert — the watcher unloads the user only for the attached, active projects.
            # Its state stays: when the project is active again the watcher goes on where it stopped, and
            # the same wait is not raised twice.
            seen.add(name)
            continue
        if t.get("agent") not in ("claude-code", "codex"):
            # Herdr names the agent only once it runs: at its first-start trust screen a task agent is still
            # «zsh». Such a terminal is watched only for that trust dialog (lesson of 2026-11-17, ch-bodycode-banner).
            if t.get("status") is not None:
                continue
            try:
                t["labels"] = json.loads(sh(["hc", "get", name, "--json"]) or "{}").get("labels") or {}
            except ValueError:
                continue
            if not t["labels"].get("genius.role"):
                continue
            screen = sh(["hc", "read", name])
            if screen and re.search(r"[Tt]rust this folder|Folder access", screen[-3000:]):
                answer_dialog(name, t, screen, project)
            continue
        seen.add(name)
        s = st.setdefault(name, {})
        s["project"] = project
        s["turn"] = t.get("turn")
        status = t.get("status")                    # `hc ls` gives a word; `hc get` an object
        status = status.get("state") if isinstance(status, dict) else status
        screen = sh(["hc", "read", name])
        if not screen:
            continue
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
            # waiting in the heavy queue (the owner's meeting, local model work, another run) is not «hung»
            if now - s.get("changed", now) > HUNG_AFTER and not s.get("hung") and not queued(name):
                s["hung"] = True
                eid = record(name, "hung", project=project, to=t.get("parent") or "",
                             text=f"{tag} {name}: {W['hung'].format(n=name, m=HUNG_AFTER // 60)}")
                alert(t, W["hung"].format(n=name, m=HUNG_AFTER // 60), spoken, cfg, eid)
            elif now - s.get("changed", now) <= HUNG_AFTER:
                s.pop("hung", None)
            continue

        # An API error (the Mac slept mid-answer, a dropped connection): resume once per turn.
        if status == "error" and s.get("resumed") != t.get("turn"):
            s["resumed"] = t.get("turn")
            sh(["hc", "send", name, RESUME], timeout=60)
            print(f"{datetime.now().isoformat(timespec='seconds')} resumed {name} after an API error")
            continue

        # a Codex dialog with a long command repeats it in option 2: «› 1. Yes» can sit 15+ lines above the bottom
        # (lesson of 2026-11-17, ch-bodycode-banner): look at the last 40 lines for the dialog, not 10
        dtail = "\n".join(screen.rstrip().split("\n")[-40:])
        if (draft is None or t.get("agent") == "codex") and re.search(r"Do you want|Would you like to run|trust this folder|[❯›]\s*\d+\.\s", dtail):
            if s.get("answered") != digest and answer_dialog(name, t, screen, project):
                s["answered"] = digest
                continue
            # a question of the agent (an AskUserQuestion dialog): the base may hold the user's decision.
            # Once per screen: a search and a judge on every pass of 30 s would be waste
            if s.get("asked") != digest:
                s["asked"] = digest
                if ask.dialog(name, t, screen, project):
                    s["answered"] = digest
                    continue
            if s.get("alerted") != f"dialog-{t.get('turn')}":
                s["alerted"] = f"dialog-{t.get('turn')}"
                set_op(name, project, f"{tag} {name}: {W['dialog'].format(n=name)}".strip(), True)
                eid = record(name, "permission", project=project, to=t.get("parent") or "",
                             text=f"{tag} {name}: {W['dialog'].format(n=name)}".strip())
                alert(t, W["dialog"].format(n=name), spoken, cfg, eid)
            continue

        # Claude Code may show its own suggested next prompt in the input, and a screen read cannot
        # tell it from the user's text. Only an agent started without suggestions
        # (CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false, label genius.nosuggest=1) gets its input sent;
        # for any other agent the text only raises an alert.
        if draft and ((t.get("labels") or json.loads(sh(["hc", "get", name, "--json"]) or "{}").get("labels") or {})
                      .get("genius.nosuggest")) != "1":
            if s.get("alerted") != f"draft-{t.get('turn')}":
                s["alerted"] = f"draft-{t.get('turn')}"
                alert(t, W["draft"].format(n=name), spoken, cfg)
            continue
        if draft:                                   # the user's own answer waits in the input
            if s.get("draft") != draft:
                s["draft"], s["draft_since"] = draft, now
            elif now - s["draft_since"] >= SEND_AFTER:
                sh(["hc", "keys", name, "enter"])
                ask.remember(name, project, last_message(screen), draft)   # the user's answer becomes knowledge
                print(f"{datetime.now().isoformat(timespec='seconds')} sent {name}: {draft[:80]}")
                alert(t, W["sent"].format(n=name), "", cfg)
                s.pop("draft", None); s.pop("idle", None)
            continue
        s.pop("draft", None)

        if own_shell:                               # it waits for its own background command
            continue
        s.setdefault("idle", now)
        loop = loop_of(name)
        if now - s["idle"] >= WAIT_AFTER and s.get("asked") != digest and not loop:
            s["asked"] = digest
            if ask.stopped(name, t, last_message(screen), project):
                s["alerted"] = f"turn-{t.get('turn')}"     # the watcher answered: the user is not asked for it
                continue
        if now - s["idle"] >= WAIT_AFTER and s.get("alerted") != f"turn-{t.get('turn')}":
            s["alerted"] = f"turn-{t.get('turn')}"
            # An agent of the fix↔review loop (bin/lib/review_loop.py) is not an agent that waits for the user:
            # its turn moves the loop on, and the coordinator hears once, at the end (goal of 2026-11-20).
            if loop and loop.on_turn_end(name, last_message(screen)):
                continue
            os.makedirs(AGENTS_DIR, exist_ok=True)
            # the agent may write its own report to state/agents/<name>.md: the watcher keeps the screen apart
            with open(os.path.join(AGENTS_DIR, f"{name}.screen.md"), "w") as f:
                f.write(f"# {name} — {datetime.now().isoformat(timespec='minutes')}\n\n{last_message(screen)}\n")
            set_op(name, project, f"{tag} {name}: {W['waits'].format(n=name)}".strip(), True)
            # A task agent (started by `bin/genius agent`: label genius.role) that ends its turn has a
            # report: the coordinator is told at once, once per turn, so the user never has to ask
            # (the user's decision of 2026-11-17: «чому мені приходиться перепитувати?»). Other agents
            # finishing a turn stay quiet (the user's decision of 2026-11-15: no spam in the chat).
            labels = t.get("labels") or {}
            if not labels:
                try:
                    labels = json.loads(sh(["hc", "get", name, "--json"]) or "{}").get("labels") or {}
                except ValueError:
                    labels = {}
            if labels.get("genius.role") and t.get("parent"):
                line = (f"[agents-watch] {name} finished its turn: the report is in "
                        f"state/agents/{name}.md (its screen: state/agents/{name}.screen.md, hc read {name}). Read it, check it, tell the user, then stop the agent."
                        + ("".join(f" UNPUSHED: {b} in {t.get('cwd')} — push it and open the PR in this same reply, or tell the user why it is held."
                                   for b in unpushed_branches(t.get("cwd")))))
                eid = record(name, "finished", project=project, to=t["parent"], send=line,
                             text=f"{tag} {name}: finished its turn, the report is in state/agents/{name}.md")
                sh(["hc", "send", t["parent"], line], timeout=60)
                record_sent(t["parent"], line, eid)
            else:
                # no report to read (not a task agent, or no parent session): the wait is still a signal
                record(name, "waits", project=project, to=t.get("parent") or "",
                       text=f"{tag} {name}: {W['waits'].format(n=name)}".strip())

    parent = next((t.get("parent") for t in data.get("terminals", []) if t.get("parent")), None)
    servers(st, seen, parent)
    for name in list(st):                           # gone: close its watch operation
        if name.startswith("_"):
            continue
        if name in seen:
            continue
        project = st[name].get("project")
        if project not in active_names:
            # Its project is paused: no report and no alert from the watcher. An agent a restart stopped
            # keeps its screen and its mark here, and the «continue or not» question comes when the
            # project is active again. An entry with no project at all (an agent of a folder that is not
            # a project) is dropped: the watcher does not keep it.
            if project is None:
                del st[name]
            continue
        if st[name].get("alerted"):
            set_op(name, None, None, False)
        try:                                        # stopped by a restart, not by us: do not lose its work
            info = json.loads(sh(["hc", "get", name, "--json"]) or "{}")
        except ValueError:
            info = {}
        if info.get("reason") == "stopped_by_restart":
            interrupted(name, info, cfg, W, active)
        del st[name]
    write(STATE, st)


def listening_ports():
    """{port: [pid, …]} for TCP listeners on 4300–4399."""
    out = sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]) or ""
    ports = {}
    for line in out.splitlines()[1:]:
        m = re.search(r":(43\d\d)\s*\(LISTEN\)", line)
        if m:
            ports.setdefault(m.group(1), set()).add(int(line.split()[1]))
    return ports


def servers(st, live, parent):
    owners = read(PORTS, {})
    now = time.time()
    told = st.setdefault("_servers", {})
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import load
        quiet, until = load.quiet_now()
    except Exception:
        quiet, until = "", ""
    for port, pids in sorted(listening_ports().items()):
        owner = owners.get(port)
        if owner == "coordinator":
            continue
        if not owner:
            if not told.get(f"unknown-{port}") and parent:
                told[f"unknown-{port}"] = now
                sh(["hc", "send", parent, f"[agents-watch] port {port}: a dev server with no owner in state/ports.json (pid {', '.join(map(str, sorted(pids)))})"], timeout=60)
            continue
        if owner not in live:                       # its agent is gone: stop the leftover
            for pid in pids:
                sh(["kill", str(pid)])
            print(f"{datetime.now().isoformat(timespec='seconds')} stopped port {port} of gone agent {owner}")
            continue
        if quiet and told.get(f"quiet-{owner}") != until:
            told[f"quiet-{owner}"] = until
            sh(["hc", "send", owner, f"[agents-watch] Тихе вікно «{quiet}» до {until}: у власника зустріч, ноут має бути легким. "
                "Зупини свої dev-сервери, браузери й контейнери зараз; важкі команди почекають у черзі до кінця вікна."], timeout=60)
            continue
        a = st.get(owner, {})
        idle = a.get("idle")
        if idle and now - idle >= SERVER_IDLE and told.get(owner) != a.get("turn"):
            told[owner] = a.get("turn")
            sh(["hc", "send", owner, f"[agents-watch] Твій dev-сервер на порту {port} працює, а ти вже {int((now - idle) // 60)} хв без роботи. Зупини свої сервери й Storybook зараз (правило власника: гасити одразу після показу чи e2e)."], timeout=60)


# ---- self-check ----------------------------------------------------------------------------------------------

def selftest():
    """The gate of the active projects (the user's rule of 2026-11-18). A temp root and a fake `hc`:
    no Herdr terminal is read or pressed, nothing is spoken, the real config and state are not touched."""
    import contextlib
    import io
    import tempfile
    global ROOT, STATE, OPS, AGENTS_DIR, sh, events
    keep = (ROOT, STATE, OPS, AGENTS_DIR, sh, events)
    tmp = tempfile.mkdtemp(prefix="genius-watch-")
    terminals, calls, fails = [], [], []

    def fake_sh(args, timeout=20):
        calls.append(list(args))
        if args[:3] == ["hc", "ls", "--json"]:
            return json.dumps({"terminals": terminals})
        if args[:2] == ["hc", "read"]:
            return "⏺ reading\n\n❯ \n"
        return ""

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    alpha = os.path.realpath(os.path.join(tmp, "alpha"))
    beta = os.path.realpath(os.path.join(tmp, "beta"))
    outside = os.path.realpath(os.path.join(tmp, "scratch"))
    for d in (alpha, beta, outside, os.path.join(tmp, "state")):
        os.makedirs(d)
    ROOT = tmp
    STATE = os.path.join(tmp, "state", "agents-watch.json")
    OPS = os.path.join(tmp, "state", "operations.json")
    AGENTS_DIR = os.path.join(tmp, "state", "agents")
    sh = fake_sh
    events = None       # the queue writes no file of the install here; its own test is bin/lib/events.py selftest
    try:
        def project(name, meta):
            os.makedirs(os.path.join(tmp, "projects", name), exist_ok=True)
            with open(os.path.join(tmp, "projects", name, "project.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)

        cfg = {"language": "en", "projects": [{"name": "alpha", "path": alpha}, {"name": "beta", "path": beta}]}
        with open(os.path.join(tmp, "genius.config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        project("alpha", {"tag": "🌐 AL", "spoken": "Alpha", "paused": {"since": "2026-11-18"}})
        project("beta", {"tag": "🐝 BE", "spoken": "Beta", "paused": {"since": "2026-11-18"}})

        # 1. every project paused: the pass reads no screen, starts nothing, says nothing
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            watch()
            watch()
        check("no active project → no Herdr call at all", calls == [], calls)
        check("no active project → one «idle» line, not one per pass",
              out.getvalue().count("idle: no active project") == 1, repr(out.getvalue()))
        check("no active project → the idle mark is kept", bool(read(STATE, {}).get("_idle")), read(STATE, {}))

        # 2. alpha is active: its agents are watched (a worktree inside the project counts too)
        project("alpha", {"tag": "🌐 AL", "spoken": "Alpha"})
        calls.clear()
        for name, cwd in (("a1", alpha), ("a1w", os.path.join(alpha, ".claude/worktrees/prfix-7")),
                          ("b1", beta), ("c1", outside)):
            terminals.append({"name": name, "parent": "p", "state": "live", "agent": "claude-code",
                              "status": "working", "turn": 1, "cwd": cwd})
        with contextlib.redirect_stdout(io.StringIO()):
            watch()
        reads = [c[2] for c in calls if c[:2] == ["hc", "read"]]
        st = read(STATE, {})
        check("an agent in an active project is watched", reads == ["a1", "a1w"], reads)
        check("an agent of a paused project is not watched", "b1" not in reads and "b1" not in st, st.keys())
        check("an agent outside every project is not watched", "c1" not in reads and "c1" not in st, st.keys())
        check("no line to an agent the watcher does not handle",
              not [c for c in calls if c[:2] == ["hc", "send"]], calls)
        check("the watched agent is marked with its project", st.get("a1", {}).get("project") == "alpha", st.get("a1"))
        check("the idle mark is gone once a project is active", "_idle" not in st, st)

        # 3. an agent that disappeared: a paused project is not asked about, an unknown folder is dropped
        terminals[:] = [t for t in terminals if t["name"] == "a1w"]
        st["b1"] = {"project": "beta", "alerted": "turn-1"}
        st["old"] = {"alerted": "turn-1"}
        write(STATE, st)
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            watch()
        st = read(STATE, {})
        check("a gone agent of a paused project keeps its state and no question", "b1" in st, st.keys())
        check("nothing is opened for it", not read(OPS, []), read(OPS, []))
        check("a gone agent of no project is dropped", "old" not in st, st.keys())
        check("a gone agent of an active project is closed", "a1" not in st, st.keys())

        # 4. the project resumes: its agent is watched at once, and the state it kept is reused
        project("beta", {"tag": "🐝 BE", "spoken": "Beta"})
        calls.clear()
        terminals.append({"name": "b1", "parent": "p", "state": "live", "agent": "claude-code",
                          "status": "working", "turn": 1, "cwd": beta})
        with contextlib.redirect_stdout(io.StringIO()):
            watch()
        check("a resumed project's agent is watched again",
              [c[2] for c in calls if c[:2] == ["hc", "read"]] == ["a1w", "b1"], calls)

        # 5. a permission dialog of a task agent: the gate's rules answer it; no rule → nothing is pressed
        def dialog(cmd):
            return ("⏺ Bash\n╭──────────────╮\n" + "".join(f"│ {l}\n" for l in cmd.split("\n"))
                    + "╌──────────────╯\n  Do you want to proceed?\n  ❯ 1. Yes\n    2. No, and tell Claude what to do differently\n")
        term = {"name": "g1", "cwd": alpha, "labels": {"genius.role": "code"}}
        calls.clear()
        ok = answer_dialog("g1", term, dialog("ls -la src"), "alpha")
        check("a dialog a rule allows is answered with the one-time «Yes»",
              ok and ["hc", "keys", "g1", "enter"] in calls, calls)
        calls.clear()
        ok = answer_dialog("g1", term, dialog("brew install jq"), "alpha")
        check("a dialog no rule covers is not answered", not ok, str(ok))
        check("…and nothing is pressed for it", not [c for c in calls if c[:2] == ["hc", "keys"]], calls)
        rec = [json.loads(l) for l in open(os.path.join(tmp, "state", "gate.jsonl"), encoding="utf-8").read().splitlines()]
        check("…and the gate log says so, with «no rule: to the coordinator»",
              rec and rec[-1]["decision"] == "ask" and rec[-1]["why"].startswith("no rule: to the coordinator"), rec[-1:])
    finally:
        ROOT, STATE, OPS, AGENTS_DIR, sh, events = keep
    if fails:
        print(f"agents-watch selftest: {len(fails)} failed")
        sys.exit(1)
    print("agents-watch selftest: ok")


def job(action):
    loop_job(action, LABEL, ["agents","watch"], EVERY, os.path.join(ROOT, "state", "agents-watch.log"))


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


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["watch"]:
        watch()
    elif len(a) == 2 and a[0] == "watch" and a[1] in ("start", "stop", "status"):
        job(a[1])
    elif a == ["selftest"]:
        selftest()                                      # the active-project gate of this file, then the answers
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import agent_ask
        sys.exit(agent_ask.selftest())
    elif (len(a) == 2 and a[0] == "overrule") or a == ["demo"]:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import agent_ask
        if a[0] == "overrule":
            agent_ask.overrule(a[1])
        else:
            agent_ask.demo()
    else:
        sys.exit("usage: bin/genius agents watch [start|stop|status] | overrule <id> | selftest | demo")
