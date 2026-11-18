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
- A permission dialog of a task agent (label genius.role) → the gate decides it, like any other action
  of that agent (the user's decision of 2026-11-17: «потрібно думати як мене розвантажити»): the action on
  the screen goes to bin/lib/gate.py (its rules, the user's earlier decisions in the knowledge base, Claude
  as the judge). Allow → the watcher picks the dialog's one-time «Yes»; deny or unclear → the alert to the
  user, as before. Every answer is written to state/gate.jsonl ("via": "watcher"). A task agent's trust
  dialog in a folder of a configured project is answered «trust» (the user's decision of 2026-11-17); a
  trust dialog anywhere else, and a dialog of any other agent, is never answered.
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


def interrupted(name, info, cfg, W):
    """A sub-agent that a restart stopped: keep its last screen, and ask the user on the panel."""
    project, tag, spoken = project_of(os.path.realpath(info.get("cwd") or ""), cfg)
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
    alert({"name": name, "parent": info.get("parent")}, W["cut"], spoken, cfg)
    print(f"{stamp} interrupted {name}: report saved, the user is asked")


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
            return False
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
        box = [l.strip()[1:].strip() for l in tail if l.strip().startswith("│")]
        if not any("Run shell command" in l for l in tail) or not box:
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
        return False
    # the cursor is on «1. Yes» (❯): Enter picks it; else the digit moves to it and picks it
    sh(["hc", "keys", name, "enter" if "❯" in yes else "1"])
    print(f"{datetime.now().isoformat(timespec='seconds')} answered {name}: {tool} {arg[:80]} — {why[:80]}")
    return True


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
        if not name or not t.get("parent") or t.get("state") != "live":
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
                project = project_of(os.path.realpath(t.get("cwd", "")), cfg)[0]
                answer_dialog(name, t, screen, project)
            continue
        seen.add(name)
        s = st.setdefault(name, {})
        s["turn"] = t.get("turn")
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
            # waiting in the heavy queue (the owner's meeting, local model work, another run) is not «hung»
            if now - s.get("changed", now) > HUNG_AFTER and not s.get("hung") and not queued(name):
                s["hung"] = True
                alert(t, W["hung"].format(n=name, m=HUNG_AFTER // 60), spoken, cfg)
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
            if s.get("alerted") != f"dialog-{t.get('turn')}":
                s["alerted"] = f"dialog-{t.get('turn')}"
                set_op(name, project, f"{tag} {name}: {W['dialog'].format(n=name)}".strip(), True)
                alert(t, W["dialog"].format(n=name), spoken, cfg)
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
                sh(["hc", "send", t["parent"], f"[agents-watch] {name} finished its turn: the report is in "
                    f"state/agents/{name}.md (its screen: state/agents/{name}.screen.md, hc read {name}). Read it, check it, tell the user, then stop the agent."],
                   timeout=60)

    parent = next((t.get("parent") for t in data.get("terminals", []) if t.get("parent")), None)
    servers(st, seen, parent)
    for name in list(st):                           # gone: close its watch operation
        if name.startswith("_"):
            continue
        if name not in seen:
            if st[name].get("alerted"):
                set_op(name, None, None, False)
            try:                                    # stopped by a restart, not by us: do not lose its work
                info = json.loads(sh(["hc", "get", name, "--json"]) or "{}")
            except ValueError:
                info = {}
            if info.get("reason") == "stopped_by_restart":
                interrupted(name, info, cfg, W)
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
    else:
        sys.exit("usage: bin/genius agents watch [start|stop|status]")
