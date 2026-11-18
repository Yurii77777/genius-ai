"""The gate: decides every action of an unattended agent, so nobody has to answer its permission prompts.

    (hook)  PreToolUse   python3 bin/lib/gate.py      — reads the hook JSON on stdin, answers allow or deny
    bin/genius gate log [-n 20]                       the latest decisions
    bin/genius gate test <tool> '<input json>' [cwd]  what the gate would answer, without the judge's cache

The user's decision of 2026-11-17: agents on other models (DeepSeek through Ollama) must not wait for the
user, and «auto» mode cannot work for them (its safety check runs on Anthropic's servers). bin/genius agent
gives such an agent this hook (`claude --settings`); the agent itself runs in the «default» mode, so every
action reaches the gate first, and the gate always answers — never «ask», so no prompt ever waits.

How it decides:
1. Rules, at once. Deny: pushing, merging, releasing, prod and remote hosts (ssh, scp, deploy commands),
   secrets (~/.ssh, .env files, keychains), deleting outside the work folder, privilege, wiping volumes,
   disabling TLS. Allow: reading, searching, editing inside the work folder or the scratch space, local
   git that changes no remote, tests, builds, linters, the heavy queue, the knowledge base, web search.
2. Anything else goes to the judge: Claude (`claude -p`, the user's own Claude Code login, default model
   «sonnet», GENIUS_GATE_JUDGE to change), with the action, the folder and the rules above. It answers
   ALLOW or DENY and one reason. The same action in the same folder is judged once (state/gate-cache.json).
   No judge, or no clear answer → deny: a stopped agent is cheaper than a wrong action.
Every decision goes to state/gate.jsonl; the agent gets the reason with a deny, and can report it.

The gate never decides what only the user decides (a merge, a write to prod, money, lifting a hold): those
are denied by rule, and the agent reports them as questions.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
LOG = os.path.join(ROOT, "state", "gate.jsonl")
CACHE = os.path.join(ROOT, "state", "gate-cache.json")
HOME = os.path.expanduser("~")
JUDGE_MODEL = os.environ.get("GENIUS_GATE_JUDGE", "sonnet")

DENY_BASH = [
    (r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\$HOME|\.\.)", "deleting outside the work folder"),
    (r"\bgit\s+push\b", "push: the coordinator and the user decide"),
    (r"\bgh\s+pr\s+(merge|ready|review\s+--approve)\b", "merge or approval of a PR: the user's word"),
    (r"\bgh\s+(release|repo\s+(delete|edit|rename)|secret|variable|workflow\s+run|run\s+(rerun|cancel))\b", "a change on GitHub"),
    (r"\bgh\s+api\b.*\s-X\s*(POST|PUT|PATCH|DELETE)\b|\bgh\s+api\b.*--method\s+(POST|PUT|PATCH|DELETE)\b", "a write through the GitHub API"),
    (r"(^|[\s;&|(])(ssh|scp|sftp|rsync)\s", "a remote host"),
    (r"\b(vercel|netlify|fly|flyctl|heroku|doctl|aws|gcloud|az|kubectl|terraform|tofu|pulumi)\b", "a cloud or deploy tool"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bnpm\s+(adduser|login|token)\b", "publishing a package"),
    (r"\bsudo\b|\bsu\s|\bchown\b|\bchmod\s+[0-7]*7[0-7]*\s+/", "privilege"),
    (r"\bdocker\s+(compose\s+)?.*(down\s+(-v|--volumes)|volume\s+(rm|prune)|system\s+prune)", "wiping containers' data"),
    (r"\bcurl\b[^|;]*\s(-k|--insecure|-sk|-ks)\b", "TLS switched off"),
    (r"(\.ssh/|id_rsa|id_ed25519|\.pem\b|\.p12\b|\bsecurity\s+(find|dump)|keychain|\.netrc|\.aws/|\.kube/|\.docker/config)", "secrets"),
    (r"(^|[\s/])\.env(\.[\w.-]+)?(\s|$|['\"])", "a secrets file (.env)"),
    (r"\b(launchctl|crontab|defaults\s+write|osascript)\b", "the user's system"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|restore\s+--source)|\bgit\s+branch\s+-D\b", "discarding work"),
    (r"\bgit\s+(remote\s+(add|set-url|remove)|config\s+--global)\b", "git settings"),
    (r"\b(shutdown|reboot|killall|pkill)\b|\bkill\s+-9\s+-1\b", "the user's processes"),
]
SAFE_BASH = re.compile(
    r"^\s*(ls|pwd|cat|head|tail|wc|grep|rg|find|fd|tree|stat|file|du|df|which|type|echo|printf|date|sort|uniq|cut|tr|"
    r"jq|diff|cmp|basename|dirname|realpath|readlink|test|true|false|sleep|env|whoami|uname|sw_vers|nproc|"
    r"git\s+(status|diff|log|show|branch|rev-parse|ls-files|grep|blame|stash|add|commit|checkout|switch|restore|"
    r"worktree|fetch|merge-base|cherry-pick|rebase|tag\s+-l|describe|config\s+--get|mv|rm)|"
    r"gh\s+(pr|issue|run|repo)\s+(view|list|diff|checks|status)|gh\s+api\s+(?!.*(-X|--method))|"
    r"node|npx\s+(vitest|jest|playwright|tsc|eslint|prettier|biome|turbo)|tsc|eslint|prettier|vitest|jest|"
    r"(npm|pnpm|yarn|bun)\s+(vitest|jest|playwright|tsc|eslint|prettier|biome|turbo|run|test|exec|install|i|ci|add|remove|why|ls|list|outdated|audit|lint|typecheck|build|dlx)|"
    r"python3?|pytest|ruff|mypy|uv\s+(run|pip|sync)|pip3?\s+(install|show|list)|go\s+(test|build|vet|run)|cargo\s+(test|build|check|clippy)|"
    r"make|mkdir|touch|cp|mv|sed|awk|tee|xargs|curl|wget|tar|unzip|zip|gzip|shasum|md5|sips|ffmpeg|ffprobe|magick|convert|"
    r"docker\s+(ps|logs|images|inspect|compose\s+(ps|logs|stop|start|up|config))|hc\s+(ls|get|read|wait)|"
    r"[\w./~-]*bin/heavy|[\w./~-]*bin/genius\s+(know|models|log|focus|load|gate\s+(log|test))|[\w./~-]*bin/status)\b")
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "TodoRead", "Task", "Agent", "AskUserQuestion",
              "WebSearch", "WebFetch", "ToolSearch", "Skill", "ExitPlanMode", "BashOutput", "KillShell", "Monitor"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SECRET_PATH = re.compile(r"(/\.ssh/|/\.aws/|/\.kube/|/\.docker/config|/\.netrc|/\.gnupg/|(^|/)\.env(\.[\w.-]+)?$|\.pem$|\.p12$|"
                         r"/Library/Keychains/|/\.claude/\.env$|/\.config/gh/)")


def inside(path, base):
    path = os.path.realpath(os.path.expanduser(path))
    base = os.path.realpath(base)
    return path == base or path.startswith(base + os.sep)


def workspaces(cwd, h):
    # the agent's own report and drafts in genius-ai are part of its work (every brief asks for a report there)
    spaces = [cwd, "/tmp", "/private/tmp", os.environ.get("TMPDIR", "/tmp"),
              os.path.join(ROOT, "state", "agents"), os.path.join(ROOT, "state", "drafts")]
    if h.get("scratchpad_dir"):
        spaces.append(h["scratchpad_dir"])
    return [s for s in spaces if s]


def split_parts(cmd):
    """Split a shell command at && || ; | and newlines, but never inside quotes."""
    parts, cur, q, i = [], "", "", 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            cur += c
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                cur += cmd[i + 1]; i += 1
            elif c == q:
                q = ""
        elif c in "'\"":
            q = c; cur += c
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            parts.append(cur); cur = ""; i += 1
        elif c in ";|\n":
            parts.append(cur); cur = ""
        elif c == "\\" and i + 1 < len(cmd) and cmd[i + 1] == "\n":
            i += 1                                       # a line continuation
        else:
            cur += c
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def rules(tool, ti, cwd, h):
    """('allow'|'deny', reason) by rule, or (None, '') for the judge."""
    if tool in WRITE_TOOLS or tool in ("Read", "NotebookRead"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if path and SECRET_PATH.search(os.path.realpath(os.path.expanduser(path))):
            return "deny", "a secrets file"
        if tool in WRITE_TOOLS:
            if not path:
                return None, ""
            if any(inside(path, s) for s in workspaces(cwd, h)):
                return "allow", "an edit inside the work folder"
            return "deny", f"an edit outside the work folder ({cwd})"
        return "allow", "reading"
    if tool in READ_TOOLS:
        return "allow", "reading or searching"
    if tool != "Bash":
        return None, ""
    cmd = re.sub(r"\bgit\s+-C\s+\S+", "git", ti.get("command", ""))   # «git -C <dir> push» is a push too
    for pattern, why in DENY_BASH:
        if re.search(pattern, cmd, re.I):
            return "deny", why
    # every part of a pipeline or a chain must be safe on its own; quoted text (a grep pattern with «\|»,
    # a message) is not split, and loop/compound keywords are not commands
    for p in split_parts(cmd):
        p = re.sub(r"^\s*(\w+=\S+\s+)+", "", p)        # leading VAR=value
        p = re.sub(r"^\s*(cd\s+\S+)\s*$", "true", p)
        p = re.sub(r"^\s*(do|then|else|fi|done|for\s+\w+\s+in\b.*|while\b.*|if\b\s*)\s*", "", p) or "true"
        p = re.sub(r"^\s*git\s+-C\s+\S+", "git", p)      # git -C <dir> <subcommand>
        if not SAFE_BASH.match(p):
            return None, ""
        if re.match(r"^\s*(rm|mv|cp|tee|touch|mkdir)\b", p) or re.search(r"(^|\s)>>?\s*\S", p):
            targets = re.findall(r"(?:>>?\s*|\s)(/[^\s;|&]+|~[^\s;|&]*)", " " + p)
            if any(not any(inside(t, s) for s in workspaces(cwd, h)) for t in targets):
                return None, ""                          # writing outside: let the judge look
    if re.search(r"\brm\s+-[a-z]*r[a-z]*f?\b.*(\s/|\s~|\$HOME|\.\.)", cmd):
        return "deny", "deleting outside the work folder"
    return "allow", "a safe command inside the work folder"


def judge(tool, ti, cwd):
    """Claude answers ALLOW or DENY for one action. None when it cannot be asked."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_") and k not in ("CLAUDE_CODE_SUBAGENT_MODEL",)}
    env["GENIUS_JUDGE"] = "1"                            # our own hooks stay out of the judge's session
    precedent = ""
    try:                                                # the user's earlier decisions about the same kind of action
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import know
        found, vectors = know.search(f"{tool} {json.dumps(ti, ensure_ascii=False)[:400]}", "", 4)
        rows = [r for s, r, cos, exact in found if r["who"] == "user" and (cos >= 0.55 if vectors else exact)]
        if rows:
            precedent = ("\nThe user's earlier words that may bear on it (follow them when they clearly cover this action):\n"
                         + "\n".join(f"- [{r['source']}] {r['text'][:300]}" for r in rows[:3]) + "\n")
    except Exception:
        precedent = ""
    prompt = (
        "You are the gate for an unattended coding agent (another model) working in the folder "
        f"{cwd} (allowed folders: {', '.join(workspaces(cwd, {}))}; reading any project file on this computer that "
        "is not a secret is allowed too). Decide if this one action may run without asking the user. The FIRST WORD of "
        "your answer is ALLOW or DENY, then one short reason. Do not think aloud.\n"
        "ALLOW: work inside that folder and the temp space — reading, editing, running tests, builds, linters, local "
        "dev servers, installing the project's dependencies, local git that changes no remote, web research.\n"
        "DENY: anything that pushes, merges, releases, deploys, writes to prod or another host, reads or prints "
        "secrets (keys, tokens, .env), changes the user's system or settings outside the folder, deletes data outside "
        "the folder, spends money, or that you cannot understand. When in doubt: DENY.\n\n"
        + ("Note: this input was copied from a terminal screen. Long lines are wrapped there, so a word, a path or a token "
           "may be split by a line break; judge what the command does, not the split.\n" if ti.get("from_screen") else "")
        + f"{precedent}Tool: {tool}\nInput: {json.dumps({k: v for k, v in ti.items() if k != 'from_screen'}, ensure_ascii=False)[:3000]}")
    try:
        r = subprocess.run(["claude", "-p", "--model", JUDGE_MODEL, "--max-turns", "1", "--tools", "", "--", prompt],
                           capture_output=True, text=True, timeout=90, env=env, cwd=ROOT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (r.stdout or "").strip()
    m = re.match(r"\W*(ALLOW|DENY)\b[\s:.-]*(.*)", out, re.S)
    if not m:                                           # it thought aloud: its last verdict counts
        verdicts = re.findall(r"\b(ALLOW|DENY)\b", out)
        if not verdicts:
            return None
        return ("allow" if verdicts[-1] == "ALLOW" else "deny"), "judge: " + " ".join(out.split())[-200:]
    return ("allow" if m.group(1) == "ALLOW" else "deny"), "judge: " + " ".join(m.group(2).split())[:200]


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def decide(tool, ti, cwd, h, cached=True):
    decision, why = rules(tool, ti, cwd, h)
    via = "rule"
    if decision is None:
        key = hashlib.sha1(f"{cwd}|{tool}|{json.dumps(ti, sort_keys=True)}".encode()).hexdigest()
        cache = read(CACHE, {})
        if cached and key in cache:
            decision, why, via = cache[key]["d"], cache[key]["why"], "cache"
        else:
            got = judge(tool, ti, cwd)
            decision, why, via = (got[0], got[1], "judge") if got else ("deny", "the judge did not answer: denied to be safe", "judge")
            if got:
                cache[key] = {"d": decision, "why": why, "at": int(time.time())}
                if len(cache) > 2000:
                    cache = dict(sorted(cache.items(), key=lambda kv: kv[1]["at"])[-1500:])
                os.makedirs(os.path.dirname(CACHE), exist_ok=True)
                with open(CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
    return decision, why, via


def hook():
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    tool, ti, cwd = h.get("tool_name", ""), h.get("tool_input") or {}, h.get("cwd") or os.getcwd()
    decision, why, via = decide(tool, ti, cwd, h)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "session": h.get("session_id", ""),
                            "cwd": cwd, "tool": tool, "input": json.dumps(ti, ensure_ascii=False)[:300],
                            "decision": decision, "via": via, "why": why}, ensure_ascii=False) + "\n")
    reason = f"genius-ai gate ({via}): {why}"
    if decision == "deny":
        reason += ". Do not try to get around it; if the task needs this, stop and put it in your report as a question."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        hook()
    elif a[0] == "log":
        n = int(a[2]) if len(a) > 2 and a[1] == "-n" else 20
        try:
            lines = open(LOG, encoding="utf-8").read().splitlines()[-n:]
        except OSError:
            lines = []
        for l in lines:
            r = json.loads(l)
            print(f"{r['at'][11:19]} {r['decision']:5} {r['via']:5} {r['tool']:6} {r['input'][:70]} — {r['why'][:80]}")
    elif a[0] == "test" and len(a) >= 3:
        cwd = os.path.realpath(a[3]) if len(a) > 3 else os.getcwd()
        print(*decide(a[1], json.loads(a[2]), cwd, {}, cached=False))
    else:
        sys.exit("usage: bin/genius gate log [-n N] | gate test <tool> '<input json>' [cwd]   (hook: gate.py)")
