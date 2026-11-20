"""Start a visible sub-agent for a ROLE: the role picks the model, the project's rules limit the providers.

    bin/genius agent <role> <name> <folder> "<brief>"
    bin/genius agent roles                      the mapping in force, and where each model comes from

    <role>    code · review · final-review · research · bulk · watch · look · render   (or a model: deepseek, flash, claude, codex)
    <name>    the terminal name in Herdr, «<tag>-<task>» (1-32 of a-z 0-9 - _)
    <folder>  where it works: a folder Claude Code already trusts (a project's path)

The user's decision of 2026-11-17: the coordinator (Claude, the session in genius-ai) leads the models and
does not do the work itself — its tokens are the most expensive. Code goes to DeepSeek, review to Codex
with Claude as the fallback, the final review before a merge to Claude.

The mapping: DEFAULT_ROLES below, overridden per role by "roles" in genius.config.json
({"code": {"provider": "ollama", "model": "deepseek-v4-pro:cloud"}, …}).

A project may limit the providers its code may go to: "providers": ["claude"] in projects/<name>/project.json.
A role whose provider is not allowed there falls back to Claude, and the script says so.

Providers:
- ollama   Claude Code with every model slot pointed at the local Ollama (ANTHROPIC_BASE_URL). Hooks, the
           watcher, the panel and the knowledge base work unchanged. Cannot see images.
- codex    the Codex CLI. No Claude Code hooks: its brief carries the knowledge search command.
- claude   Claude Code as is (optionally a model: "model": "sonnet").
A provider whose limits are nearly spent (bin/genius models: 5 h or 7 d ≥ 95 %) gives way to its fallback.

Every brief gets the standard ending (ask, do not invent; stop what you start; the heavy queue; the
knowledge base) from the script, so no brief forgets it. Before the critic's notes it also gets the context of
every brief (bin/lib/briefctx.py): the owner's rules from the knowledge base and the project's skills. A Codex or
Ollama agent has no Claude Code hooks, so that block is its only source. The terminal gets the labels
genius.role and genius.model: the panel shows the model beside the agent.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
FAST = "deepseek-v4.1-flash:cloud"
PRO = "deepseek-v4-pro:cloud"
# One main model and two fallbacks per role (the user's decision of 2026-11-17). A link is a provider and,
# for Ollama, a model. The first link that answers is used: an Ollama model is probed with one tiny request;
# Claude and Codex give way when their limits are nearly spent (bin/genius models: 5 h or 7 d ≥ 95 %).
# Probe of 2026-11-17 (one tool call, through Ollama's Anthropic API): deepseek-v4-pro 1.5 s, v4.1-flash 1.4 s,
# glm-5.3 2.1 s, glm-5.3-flash 1.4 s, kimi-k2.7-code 2.2 s, mimo-v2.6-pro 0.7 s — all called the tool right;
# kimi-k3 and minimax-m3 spent 400 tokens thinking without a call. No Ollama model is used where images matter.
O = lambda m: {"provider": "ollama", "model": m}
DEFAULT_ROLES = {
    # DeepSeek v4.1 flash, not v4 pro (2026-11-17): flash scores higher on agentic coding (DeepSWE 74.2 vs 62.7,
    # Terminal-Bench 2.1 90.6 vs 87.9, DeepSeek's tables) and costs about a quarter; DeepSeek kept v4 pro only for
    # compatibility. v4 pro stays reachable as the model «pro», in no default chain.
    "code":         {"chain": [O(FAST), O("glm-5.3:cloud"), O("kimi-k2.7-code:cloud")],
                     "about": "features, fixes, tests"},
    "bulk":         {"chain": [O(FAST), O("glm-5.3-flash:cloud"), O("mimo-v2.6-pro:cloud")],
                     "about": "search the code, refactor by a pattern, compare lists"},
    "research":     {"chain": [O(FAST), O("glm-5.3:cloud"), O("kimi-k3:cloud")],
                     "about": "googling, competitors, docs, draft texts"},
    "watch":        {"chain": [O("glm-5.3-flash:cloud"), O(FAST), O("mimo-v2.6-pro:cloud")],
                     "about": "the watcher: reads agents' screens, answers from the knowledge base"},
    "review":       {"chain": [{"provider": "codex"}, {"provider": "claude"}, O("glm-5.3:cloud")],
                     "about": "review of a PR, the risk before a merge"},
    "final-review": {"chain": [{"provider": "claude"}],
                     "about": "the last review before the user's word to merge: Claude only"},
    "look":         {"chain": [{"provider": "codex"}, {"provider": "claude"}],
                     "about": "see and judge an image: screenshots, mock-ups, design review (no Ollama model sees images)"},
    # The critic of every brief (bin/lib/critic.py, the owner's rule of 2026-11-20: a brief is judged by another
    # provider than the one that wrote it — here Claude, the coordinator). Never Claude: it would prove nothing.
    "critic":       {"chain": [{"provider": "codex"}, O("kimi-k3.1-pro:cloud"), O(FAST)],
                     "about": "the critic of a brief before an agent starts: another provider than the author"},
    # Drawing an image. Claude cannot draw. A local image model is the LAST link, for when Codex has no limits
    # left (the user's decision of 2026-11-17): it is heavy for the computer, so it has a guard. Add it in the
    # config, the product does not know your local model:
    #   "roles": {"render": {"chain": [{"provider": "codex"},
    #       {"provider": "local", "name": "flux", "run": "~/path/to/flux --out {folder} {prompt}",
    #        "needs_free_gb": 10, "no_docker": true}]}}
    "render":       {"chain": [{"provider": "codex"}],
                     "about": "draw an image: Codex; a local image model as the last resort (config)"},
}
ALIASES = {"image": "look"}
MODELS = {"deepseek": {"provider": "ollama", "model": FAST}, "pro": {"provider": "ollama", "model": PRO},
          "flash": {"provider": "ollama", "model": FAST}, "claude": {"provider": "claude"}, "codex": {"provider": "codex"},
          "sonnet": {"provider": "claude", "model": "sonnet"}}


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def roles():
    """The mapping in force: the defaults, with "roles" of the config replacing a role's chain."""
    out = {k: dict(v) for k, v in DEFAULT_ROLES.items()}
    for k, v in (read(os.path.join(ROOT, "genius.config.json"), {}).get("roles") or {}).items():
        out[k] = {**out.get(k, {}), **v}
    return out


def answers(link):
    """The Ollama model answers one tiny request (its account, its quota, its servers)."""
    body = json.dumps({"model": link["model"], "stream": False, "messages": [{"role": "user", "content": "ok"}],
                       "options": {"num_predict": 1}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return not json.load(r).get("error")
    except (OSError, ValueError):
        return False


def repo_root_of(folder):
    """The main repository of a folder: a git worktree outside the project folder maps to the repo it belongs to,
    so a project's provider limits cannot be bypassed by a worktree path (lesson of 2026-11-17)."""
    try:
        r = subprocess.run(["git", "-C", folder, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=10)
        common = r.stdout.strip()
        if r.returncode == 0 and common:
            return os.path.realpath(os.path.dirname(common) if os.path.basename(common) == ".git" else common)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return folder


def project_of(folder):
    best, meta = "", {}
    candidates = {folder, repo_root_of(folder)}
    for p in read(os.path.join(ROOT, "genius.config.json"), {}).get("projects") or []:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if any(f == path or f.startswith(path + os.sep) for f in candidates) and len(path) > len(best):
            best = path
            meta = {"name": p["name"], **read(os.path.join(ROOT, "projects", p["name"], "project.json"), {})}
    return meta


def spent(provider):
    """True when the provider's limits are nearly used up (bin/genius models)."""
    name = {"claude": "Claude", "codex": "Codex"}.get(provider)
    if not name:
        return False
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "lib", "models.py"), "--json"],
                           capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    p = next((x for x in data.get("providers", []) if x.get("name") == name), {})
    return any((l.get("used") or 0) >= 95 for l in (p.get("limits") or {}).values())


def pick(role_name, project):
    """(link, notes): the first link of the role's chain that the project allows and that can work now."""
    rs = roles()
    role_name = ALIASES.get(role_name, role_name)
    if role_name in rs:
        chain = rs[role_name].get("chain") or []
    elif role_name in MODELS:
        chain = [MODELS[role_name]]
    else:
        sys.exit(f"no role or model «{role_name}»: {', '.join(list(rs) + list(MODELS))}")
    notes = []
    allowed = project.get("providers")
    for i, link in enumerate(chain):
        label = link["provider"] + (f" {link['model']}" if link.get("model") else "")
        if allowed and link["provider"] not in allowed:
            notes.append(f"{label}: {project.get('name')} allows only {', '.join(allowed)}")
            continue
        missing = installed(link["provider"])
        if missing:
            notes.append(f"{label}: {missing}")
            continue
        if link["provider"] == "local":
            blocked = guard(link)
            if blocked:
                notes.append(f"local {link.get('name', '')}: {blocked}")
                if i == len(chain) - 1:             # the last resort cannot run: say so, start nothing
                    sys.exit("no model of the role can work now: " + "; ".join(notes))
                continue
            if i:
                notes.append(f"fallback {i}: local {link.get('name', '')}")
            return dict(link), notes
        if link["provider"] == "ollama":
            ensure_ollama()
            if not answers(link):
                notes.append(f"{label}: no answer (not signed in, no subscription, quota, or servers)")
                continue
        elif spent(link["provider"]):
            notes.append(f"{label}: limits nearly spent")
            continue
        if i:
            notes.append(f"fallback {i}: {label}")
        return dict(link), notes
    # Nothing in the chain can work (not installed, no subscription, not allowed for this project): Claude Code
    # itself is the one provider every install has, so the work is never refused — the note says why.
    # A working role on Claude takes the cheaper Claude model: the coordinator's own model is for judgement.
    cheap = {} if role_name in ("review", "final-review", "image") else {"model": "sonnet"}
    notes.append(f"no model of «{role_name}» can work now: claude" + (" (sonnet)" if cheap else ""))
    return {"provider": "claude", **cheap}, notes


def free_gb():
    """Memory that a new program can take now (macOS): free, inactive, speculative and purgeable pages."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    size = int((re.search(r"page size of (\d+)", out) or [0, 16384])[1])
    pages = sum(int(m.group(1)) for m in re.finditer(r"Pages (?:free|inactive|speculative|purgeable):\s+(\d+)", out))
    return pages * size / 2 ** 30


def guard(link):
    """'' when a local model may run now, else why not and what to stop. It never stops anything itself."""
    why = []
    need = link.get("needs_free_gb")
    have = free_gb()
    if need and have is not None and have < need:
        why.append(f"{have:.1f} GB of memory free, {need} needed")
    if link.get("no_docker"):
        try:
            names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=8).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            names = []
        if names:
            why.append("Docker containers run: " + ", ".join(names[:6]) + " — stop them first (docker compose stop)")
    return "; ".join(why)


def installed(provider):
    """'' when the provider's program is here, else the reason it cannot be used."""
    if provider == "ollama" and not shutil.which("ollama"):
        return "Ollama is not installed (brew install ollama; ollama signin)"
    if provider == "codex" and not shutil.which("codex"):
        return "Codex CLI is not installed"
    return ""


def ensure_ollama():
    try:
        urllib.request.urlopen(OLLAMA + "/api/version", timeout=2).read()
    except OSError:
        subprocess.Popen(["ollama", "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            try:
                urllib.request.urlopen(OLLAMA + "/api/version", timeout=1).read()
                return
            except OSError:
                import time
                time.sleep(0.5)


def ending(spec, name):
    me = os.path.join(ROOT, "bin", "genius")
    lines = [
        "",
        "---",
        "Rules of genius-ai for this work (they come from the coordinator's script, every brief has them):",
        "- Do only what is asked. When a requirement is unclear or you would have to guess (behaviour, wording, "
        "prices, which library), stop and ask one exact question. Never replace a tool the user named.",
        f"- Every heavy command (tests, a build, a browser run, an image render) runs through the queue: "
        f"{os.path.join(ROOT, 'bin', 'heavy')} {name} -- <command>. Single-threaded tests.",
        "- Stop what you start: dev servers, containers, browsers — as soon as they are not needed.",
        "- Never push, merge or write to prod: the coordinator and the user decide that.",
        f"- Before you ask: {me} know search \"<question>\" — what the user already said or decided. "
        "Use a found answer and name its source.",
        "- End with a short report: what you did, how you checked it, what is left, your questions.",
    ]
    if spec["provider"] == "ollama":
        lines.append("- You cannot see images: never judge a screenshot or a design; say so and leave it to the coordinator.")
    return "\n".join(lines)


def gate_settings():
    """The settings file that gives an agent the gate: written on every start, so it follows this folder."""
    path = os.path.join(ROOT, "state", "gate-settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    gate = os.path.join(ROOT, "bin", "lib", "gate.py")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f'/usr/bin/python3 "{gate}"', "timeout": 120}]}]}}, f, indent=2)
    return path


def command(spec, brief):
    if spec["provider"] == "ollama":
        model = spec.get("model", PRO)
        env = {"ANTHROPIC_BASE_URL": OLLAMA, "ANTHROPIC_AUTH_TOKEN": "ollama", "ANTHROPIC_MODEL": model,
               "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": FAST,
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": FAST, "CLAUDE_CODE_SUBAGENT_MODEL": FAST,
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false"}
        exports = " ".join(f"{k}={v}" for k, v in env.items())
        # «auto» mode cannot work through Ollama (its safety check runs on Anthropic's servers): the agent runs in
        # «default» mode, and the gate (bin/lib/gate.py, a PreToolUse hook) answers every action, so no prompt waits
        return model, ["zsh", "-lic", f'unset ANTHROPIC_API_KEY; {exports} GENIUS_AGENT=1 GENIUS_AUTHOR=ollama '
                                      f'SKIP_ATTENTION_NOTIFY=1 SESSION_OPEN_NO_DEV=1 exec claude '
                                       f'--permission-mode default --settings "{gate_settings()}" "$0"', brief]
    if spec["provider"] == "codex":
        model = spec.get("model", "")
        flag = f'-m {model} ' if model else ""
        return model or "codex", ["zsh", "-lic", f'GENIUS_AGENT=1 GENIUS_AUTHOR=codex exec codex {flag}"$0"', brief]
    model = spec.get("model", "")
    flag = f"--model {model} " if model else ""
    return model or "claude", ["zsh", "-lic", f'CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false GENIUS_AGENT=1 '
                                              f'GENIUS_AUTHOR=claude SKIP_ATTENTION_NOTIFY=1 SESSION_OPEN_NO_DEV=1 '
                                              f'exec claude {flag}"$0"', brief]


def show_roles():
    """The mapping, each link marked: ✓ can be used here, – its provider is not installed."""
    for k, v in roles().items():
        links = []
        for l in v.get("chain") or []:
            name = (l["model"].replace(":cloud", "") if l["provider"] == "ollama"
                    else f"local {l.get('name', '')}" if l["provider"] == "local" else l["provider"])
            links.append(("– " if installed(l["provider"]) else "") + name)
        print(f"{k:13} {' → '.join(links):62} {v.get('about', '')}")
    gaps = sorted({installed(p) for p in ("ollama", "codex")} - {""})
    for g in gaps:
        print(f"note: {g}. Roles skip it and fall to the next model, at last to Claude.")
    print("lead          claude (this session)                       the coordinator: plans, briefs, checks, decides with the user")


def main():
    a = sys.argv[1:]
    if a == ["roles"]:
        return show_roles()
    if len(a) != 4:
        sys.exit(__doc__.strip().split("\n\n")[1])
    role_name, name, folder, brief = a[0], a[1], os.path.realpath(os.path.expanduser(a[2])), a[3]
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        sys.exit(f"bad name «{name}»: 1-32 of a-z 0-9 - _")
    if not os.path.isdir(folder):
        sys.exit(f"no folder {folder}")
    project = project_of(folder)
    spec, notes = pick(role_name, project)
    # The context of every brief (the owner's goal of 2026-11-20): the owner's rules from the knowledge base and the
    # project's skills, before «Critic notes». It comes from the script, so no provider and no brief misses it; the
    # evidence is the critic's own, so one search of the knowledge base serves both.
    import briefctx
    import critic
    base = brief
    try:
        ev = critic.evidence(base, folder, project.get("name", ""))
    except Exception:
        ev = {"rules": [], "code": []}
    ctx = briefctx.context(base, folder, project.get("name", ""), role_name,
                           rules_fn=lambda b, p, n: ev["rules"][:n])
    if ctx["text"]:
        brief = base + "\n\n" + ctx["text"]
        notes.append(f"context: {ctx['rules']} owner rules · {ctx['skills']} skills")
    else:
        notes.append("context: no rule and no skill bears on this brief")
    # The critic (the owner's rule of 2026-11-20): every brief is read by a model of ANOTHER provider before the
    # agent starts, and its notes go into the brief. It never blocks the start: no answer within 180 s → no notes,
    # and state/brief-critic.jsonl says why. There is no switch: a brief without the critic is not a brief.
    critique = critic.notes_for(base, folder, project.get("name", ""), ev=ev,
                                author=os.environ.get("GENIUS_AUTHOR") or "claude")
    if critique["notes"]:
        brief = brief + "\n\n" + critic.block(critique["notes"])
        notes.append(f"critic: {critique['provider']} · {len(critique['notes'].splitlines())} lines · "
                     f"{int(critique['ms'] / 1000)} s")
    else:
        notes.append(f"critic: no notes ({critique['why']})")
    if not shutil.which("hc"):
        # No Herdr: nothing to show the agent in. The coordinator runs it as an internal subagent instead;
        # this prints what the role resolved to, and the ending every brief must carry.
        for n in notes:
            print(f"note: {n}")
        print(f"Herdr (hc) is not installed: no visible agent. Role {role_name} → {spec['provider']} {spec.get('model', '')}".rstrip())
        print("Start an internal subagent with this brief ending:\n" + ending(spec, name))
        sys.exit(3)
    if spec["provider"] == "local":
        # Not an agent: one heavy command, through the queue, in its own Herdr terminal. The brief is the prompt.
        import shlex
        run = os.path.expanduser(spec["run"]).replace("{folder}", shlex.quote(folder)).replace("{prompt}", shlex.quote(brief))
        cmd = ["hc", "run", "--bg", "--json", "--name", name, "--label", f"genius.role={role_name}",
               "--label", f"genius.model=local-{spec.get('name', 'model')}", "--cwd", folder, "--",
               "zsh", "-lc", f'{shlex.quote(os.path.join(ROOT, "bin", "heavy"))} {shlex.quote(name)} -- {run}']
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(r.stderr.strip() or r.stdout.strip())
        for n in notes:
            print(f"note: {n}")
        print(f"started {name} · role {role_name} · local {spec.get('name', '')} through the heavy queue · output in {folder}")
        return
    model, argv = command(spec, brief + "\n" + ending(spec, name))
    nosuggest = ["--label", "genius.nosuggest=1"] if spec["provider"] != "codex" else []
    # --no-notify: Herdr would type «blocked (permission)» into this session at once, before the watcher (30 s) has
    # had its turn to answer the dialog through the gate — noise for the user. The watcher reports what matters:
    # a finished turn, a dialog the gate does not allow, a hang.
    cmd = ["hc", "run", "--bg", "--json", "--no-notify", "--name", name, *nosuggest, "--label", f"genius.role={role_name}",
           "--label", f"genius.model={model}", "--cwd", folder, "--", *argv]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(r.stderr.strip() or r.stdout.strip())
    try:
        address = json.loads(r.stdout).get("address", "?")
    except ValueError:
        address = "?"
    for n in notes:
        print(f"note: {n}")
    print(f"started {name} ({address}) · role {role_name} · {spec['provider']} {model} · {folder}")


if __name__ == "__main__":
    main()
