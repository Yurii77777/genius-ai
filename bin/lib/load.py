"""Load guard: no local stack is started while the computer is busy with local model work.

    bin/genius load                       what runs now: local model work, stacks, free memory, the mode
    bin/genius load one-stack [why]       the user's «one stack at a time»: a stack starts only when none runs
    bin/genius load normal                back to the usual rules
    (hook)  PreToolUse   load.py hook     every Bash command of every agent; a command that starts a stack is
                                          denied, with the reason, when the guard says no

The user's decision of 2026-11-17: the user trains and runs local models (Qwen, Gemma, FLUX, MiniMax) on this
computer, sometimes for hours; then only one stack may run, or none. Agents and sub-agents must check before
they start a local stack — by script, not by memory: the hook checks every command that starts one.

What counts as local model work: a process of a known model runtime (mflux, mlx_lm, mlx_vlm, ai-toolkit,
torch/diffusers training, ollama runner with a model loaded…) that holds at least ML_MIN_GB of memory. A cloud
job driven from here (a small local process) does not count. Patterns: ML_PATTERNS below, plus
"load": {"ml_patterns": [...]} in genius.config.json for the user's own tools.

Quiet windows (the user's decision of 2026-11-17: a daily meeting at 11:00 Kyiv must run without lags):
"load": {"quiet": [{"from": "10:55", "to": "11:30", "tz": "Europe/Kyiv", "days": "mon-sun", "why": "daily"}]}
in genius.config.json. Inside a window no stack starts and the heavy queue holds every command; the watcher
asks agents with a running dev server to stop it when the window begins.

A command starts a stack when it starts containers, a dev server, a browser test run or a local build server
(STACK_START). The guard says no when: local model work runs; or free memory is below MIN_FREE_GB; or the mode
is one-stack and a stack already runs. It never stops anything: it says what runs and what to stop.
bin/heavy waits while local model work runs (the heavy queue respects it too).
"""
import json
import os
import re
import subprocess
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
MODE = os.path.join(ROOT, "state", "load.json")
ML_MIN_GB = 1.5
MIN_FREE_GB = 4.0
ML_PATTERNS = [r"\bmflux\b", r"mflux-generate", r"\bmlx_lm\b", r"mlx-lm", r"\bmlx_vlm\b", r"mlx_vlm_server",
               r"ai-toolkit/(run|toolkit)", r"\btrain(_network|_lora)?\.py\b", r"\bdiffusers\b", r"\btorchrun\b",
               r"\bollama (runner|llama)", r"\bcomfyui\b", r"\bllama-server\b", r"\bwhisper\b.*--model"]
STACK_START = re.compile(
    r"\bdocker\s+(compose\s+(\S+\s+)*up\b|start\b|run\b)|\bdocker-compose\s+up\b|\bopen\s+-a\s+OrbStack\b|"
    r"\b(pnpm|npm|yarn|bun)\s+(run\s+)?(dev|start|serve|preview|storybook|stack:\w+|docker:up|verify:stack)\b|"
    r"\b(next|astro|vite|nuxt|remix)\s+(dev|start|preview)\b|\bturbo\s+(run\s+)?dev\b|"
    r"\bplaywright\s+test\b|\b(pnpm|npm|npx)\s+(exec\s+)?playwright\s+test\b|\bnode\s+\S*shoot\S*\.m?js\b")
STACK_PORTS = re.compile(r":(3000|3080|43\d\d|4432|5173|6006|8081|808[2-9]|57331|57332)\s*\(LISTEN\)")


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sh(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ml_jobs():
    """Local model work now: [(pid, GB, short command)]."""
    extra = (read(os.path.join(ROOT, "genius.config.json"), {}).get("load") or {}).get("ml_patterns") or []
    pat = re.compile("|".join(ML_PATTERNS + extra), re.I)
    out = []
    for line in sh(["ps", "-axo", "pid=,rss=,command="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not pat.search(parts[2]):
            continue
        gb = int(parts[1]) / 1048576
        if gb >= ML_MIN_GB:
            out.append((int(parts[0]), round(gb, 1), parts[2][:90]))
    return out


def free_gb():
    out = sh(["vm_stat"])
    size = int((re.search(r"page size of (\d+)", out) or [0, 16384])[1])
    pages = sum(int(m.group(1)) for m in re.finditer(r"Pages (?:free|inactive|speculative|purgeable):\s+(\d+)", out))
    return round(pages * size / 2 ** 30, 1) if pages else None


def stacks():
    """What already runs: containers and dev servers. ['container x', 'port 4300 (node)']."""
    out = [f"container {n}" for n in sh(["docker", "ps", "--format", "{{.Names}}"]).split()]
    seen = set()
    for line in sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]).splitlines():
        m = STACK_PORTS.search(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(f"port {m.group(1)} ({line.split()[0]})")
    return out


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def quiet_now():
    """(why, until) of the quiet window in force now, else ('', '')."""
    from datetime import datetime
    for q in (read(os.path.join(ROOT, "genius.config.json"), {}).get("load") or {}).get("quiet") or []:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(q.get("tz") or "Europe/Kyiv"))
        except Exception:
            now = datetime.now()
        days = q.get("days", "mon-sun")
        if "-" in days:
            a, b = days.split("-")
            allowed = DAYS[DAYS.index(a):DAYS.index(b) + 1]
        else:
            allowed = [d.strip() for d in days.split(",")]
        hm = now.strftime("%H:%M")
        if DAYS[now.weekday()] in allowed and q.get("from", "00:00") <= hm < q.get("to", "00:00"):
            return q.get("why") or "quiet window", q.get("to")
    return "", ""


def verdict(cmd):
    """'' when the command may run, else why not. Only commands that start a stack are judged."""
    # what the command RUNS, not the text it carries: here-docs and quoted strings (a brief, a message, a
    # commit text) may name a dev server or containers without starting anything
    bare = re.sub(r"<<-?\s*'?(\w+)'?.*?^\1\s*$", " ", cmd or "", flags=re.S | re.M)
    bare = re.sub(r"\"(?:\\.|[^\"\\])*\"|'[^']*'", " ", bare)
    if not STACK_START.search(bare):
        return ""
    why, until = quiet_now()
    if why:
        return (f"quiet window «{why}» until {until} (the user's meeting: the computer must stay light). "
                "Start nothing heavy now; wait until it ends, or prepare the work without running it")
    jobs = ml_jobs()
    if jobs:
        return ("local model work runs (" + "; ".join(f"pid {p}, {g} GB: {c}" for p, g, c in jobs[:3]) +
                "). The user's rule: no local stack while training or generation runs locally. Wait for it to end, "
                "or report this as a blocker; do not stop the user's model work")
    free = free_gb()
    if free is not None and free < MIN_FREE_GB:
        return f"only {free} GB of memory is free (at least {MIN_FREE_GB} needed). Stop what is unused first: {', '.join(stacks()[:6]) or 'nothing of ours runs'}"
    mode = read(MODE, {})
    if mode.get("mode") == "one-stack":
        running = stacks()
        if running:
            return (f"the user asked for one stack at a time ({mode.get('why') or 'since ' + mode.get('since', '?')}), and this runs: "
                    + ", ".join(running[:6]) + ". Reuse it, or wait until it stops")
    return ""


def hook():
    if os.environ.get("GENIUS_JUDGE"):
        return
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    if h.get("tool_name") != "Bash":
        return
    why = verdict((h.get("tool_input") or {}).get("command", ""))
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": f"genius-ai load guard: {why}."}}, ensure_ascii=False))


def status():
    why, until = quiet_now()
    if why:
        print(f"QUIET: «{why}» until {until}")
    mode = read(MODE, {})
    print(f"mode: {mode.get('mode', 'normal')}" + (f" ({mode.get('why')}, since {mode.get('since')})" if mode.get("mode") else ""))
    jobs = ml_jobs()
    print("local model work: " + ("; ".join(f"pid {p} {g} GB {c}" for p, g, c in jobs) if jobs else "none"))
    print("stacks: " + (", ".join(stacks()) or "none"))
    print(f"free memory: {free_gb()} GB")


def main():
    a = sys.argv[1:]
    if a == ["hook"] or a[:1] == ["hook"]:
        return hook()
    if not a:
        return status()
    if a[0] == "one-stack":
        os.makedirs(os.path.dirname(MODE), exist_ok=True)
        with open(MODE, "w", encoding="utf-8") as f:
            json.dump({"mode": "one-stack", "since": date.today().isoformat(), "why": " ".join(a[1:])}, f, ensure_ascii=False)
        return status()
    if a == ["normal"]:
        try:
            os.remove(MODE)
        except OSError:
            pass
        return status()
    if a[0] == "check" and len(a) == 2:
        print(verdict(a[1]) or "ok")
        return
    sys.exit("usage: bin/genius load [one-stack [why] | normal | check '<command>']   (hook: load.py hook)")


if __name__ == "__main__":
    main()
