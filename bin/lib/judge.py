"""The judge: one role, one chain, for every place that must decide an action or a question.

    python3 -I bin/lib/judge.py chain [<judged>]     the links the judge asks now, in order
    python3 -I bin/lib/judge.py ask "<prompt>"       one answer through the chain, with the link that gave it
    python3 -I bin/lib/judge.py selftest             the chain, the family skip and the Claude rule, no model

The judge is the role "judge" of bin/lib/agent.py: kimi-k3.1-pro:cloud → glm-5.3:cloud →
deepseek-v4.1-flash:cloud → codex → claude (sonnet). "roles" in genius.config.json replaces the chain.
The local Ollama answers in about a second and spends no limit of the user. Claude is the LAST link: a judge
call on Claude eats the user's own 5-hour window (the owner's decision of 2026-11-20: 252 calls in 35 minutes
took the window from 13 % to 81 %).

Two callers use it the same way:
- bin/lib/gate.py — an action of an unattended agent that no rule decides;
- bin/lib/agent_ask.py — a question of a task agent, answered only from the user's own words.

A link of the SAME family as the agent judged is skipped: an agent on DeepSeek is never judged by DeepSeek, an
agent on Claude never by Claude. The family is the provider (codex, claude) or the first word of the model name
(«deepseek-v4.1-flash» → «deepseek»). The judged agent names its model through GENIUS_AGENT_MODEL, which
bin/genius agent exports; the watcher names it through the label genius.model.
Claude is never a judge while genius.config.json says "claude_fallback": false.
The judge is off while state/gate-judge.off exists or GENIUS_JUDGE_OFF is set: the caller then decides without it.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
OFF_FILE = os.path.join(ROOT, "state", "gate-judge.off")
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
OLLAMA_BUDGET = 20.0          # seconds for one Ollama link; a cloud model answers in about a second
CODEX_BUDGET = 90.0           # seconds for the Codex link
CLAUDE_BUDGET = 90.0          # seconds for the Claude link
CLAUDE_MODEL = os.environ.get("GENIUS_GATE_JUDGE", "sonnet")


def agent():
    """bin/lib/agent.py: the roles, the providers, and what can run now."""
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import agent as a
    return a


def family(link):
    """The family of a link: its provider for codex and claude, else the first word of the model name.
    One family never judges itself."""
    provider = (link.get("provider") or "").lower()
    model = (link.get("model") or "").lower()
    if provider in ("codex", "claude"):
        return provider
    if model.startswith(("sonnet", "opus", "haiku", "claude-")):
        return "claude"
    return re.split(r"[-.:/@]", model)[0] if model else provider


def judged_of_model(model):
    """A link for the model an agent runs on: «sonnet», «codex», «deepseek-v4.1-flash:cloud». {} when unknown."""
    model = (model or "").strip()
    if not model:
        return {}
    return {"provider": "codex" if model.lower() == "codex" else "ollama", "model": model}


def self_link():
    """The link of the agent that runs this process, as far as its environment says. {} when it says nothing."""
    author = (os.environ.get("GENIUS_AUTHOR") or "").strip().lower()
    model = (os.environ.get("GENIUS_AGENT_MODEL") or "").strip()
    if model:
        return {"provider": "codex" if model.lower() == "codex" else (author or "ollama"), "model": model}
    if author in ("claude", "codex"):
        return {"provider": author}
    model = (os.environ.get("ANTHROPIC_MODEL") or "").strip()
    return {"provider": author or "ollama", "model": model} if model else {}


def claude_fallback():
    """False when genius.config.json says "claude_fallback": false: Claude is the coordinator, never a judge."""
    try:
        with open(os.path.join(ROOT, "genius.config.json"), encoding="utf-8") as f:
            return json.load(f).get("claude_fallback", True) is not False
    except (OSError, ValueError):
        return True


def off():
    """True when the user switched the judge off: state/gate-judge.off, or GENIUS_JUDGE_OFF in the environment."""
    return bool((os.environ.get("GENIUS_JUDGE_OFF") or "").strip()) or os.path.exists(OFF_FILE)


def links(judged=None, rs=None):
    """The judge's links now, in order: the role «judge» without the family of the agent judged, and without
    Claude when the config forbids the Claude fallback. GENIUS_GATE_CHAIN «ollama:<model>,claude:<model>»
    replaces the whole chain (a debugging switch); the family skip and the Claude rule still apply."""
    raw = (os.environ.get("GENIUS_GATE_CHAIN") or "").strip()
    if raw:
        base = []
        for item in raw.split(","):
            provider, _sep, model = item.strip().partition(":")
            base.append({"provider": provider.strip().lower(), "model": model.strip()})
    else:
        base = [dict(l) for l in ((rs if rs is not None else agent().roles()).get("judge") or {}).get("chain") or []]
    skip = family(judged) if judged else ""
    out = []
    for link in base:
        if link.get("provider") == "claude" and not claude_fallback():
            continue
        if skip and family(link) == skip:
            continue
        out.append(link)
    return out


def label(link):
    """«ollama kimi-k3.1-pro:cloud», «codex», «claude sonnet»."""
    return (link.get("provider") or "") + (" " + link["model"] if link.get("model") else "")


def ollama_text(model, prompt, budget=OLLAMA_BUDGET, num_predict=120, temperature=0.0):
    """The judge's words from an Ollama model, or «» when it does not answer. A thinking model gets no budget to
    think here: the answer is one word, and the thinking would eat the allowance and print nothing."""
    for extra in ({"think": False}, {}):                # an older Ollama refuses «think»: the second try drops it
        body = {"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": temperature, "num_predict": num_predict}, **extra}
        req = urllib.request.Request(OLLAMA.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=budget) as r:
                data = json.load(r)
        except (OSError, ValueError):
            return ""
        if data.get("error"):
            continue
        text = ((data.get("message") or {}).get("content") or "").strip()
        if text:
            return text
    return ""


def codex_text(prompt, model="", budget=CODEX_BUDGET):
    """The judge's words from the Codex CLI in a read-only sandbox, or «» when it cannot be asked."""
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["GENIUS_JUDGE"] = "1"                            # our own hooks stay out of the judge's run
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=budget, cwd=ROOT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def claude_text(prompt, model="", budget=CLAUDE_BUDGET):
    """The judge's words from Claude Code on this computer, or «» when it cannot be asked."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    env["GENIUS_JUDGE"] = "1"                            # our own hooks stay out of the judge's session
    try:
        r = subprocess.run(["claude", "-p", "--model", model or CLAUDE_MODEL, "--max-turns", "1", "--tools", "", "--",
                            prompt], capture_output=True, text=True, timeout=budget, env=env, cwd=ROOT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip()


def ask(prompt, judged=None, budget=0.0, num_predict=120, temperature=0.0):
    """(text, link, notes): the words of the first link that answers, and «» with None when none does.
    ``judged`` is the link of the agent that asks: a link of its own family is skipped."""
    notes = []
    if off():
        return "", None, ["the judge is off (state/gate-judge.off or GENIUS_JUDGE_OFF)"]
    for link in links(judged):
        provider = link.get("provider")
        if provider == "ollama":
            text = ollama_text(link["model"], prompt, budget or OLLAMA_BUDGET, num_predict, temperature)
        elif provider == "codex":
            text = codex_text(prompt, link.get("model", ""), budget or CODEX_BUDGET)
        elif provider == "claude":
            text = claude_text(prompt, link.get("model", ""), budget or CLAUDE_BUDGET)
        else:
            notes.append(f"{label(link)}: no such provider")
            continue
        if text:
            return text, link, notes
        notes.append(f"{label(link)}: no answer")
    return "", None, notes


def selftest():
    """The role, the order of its links, the family skip, the Claude rule and the off switch. No model."""
    fails = []

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    global claude_fallback, off
    real_cf, real_off = claude_fallback, off
    rs = agent().roles()
    chain = [l for l in (rs.get("judge") or {}).get("chain") or []]
    check("the role «judge» exists with a chain", bool(chain), str(chain))
    check("it is the local Ollama first", bool(chain) and chain[0]["provider"] == "ollama", str(chain[:1]))
    check("and Claude last, with a sonnet model",
          bool(chain) and chain[-1]["provider"] == "claude" and chain[-1].get("model") == "sonnet", str(chain[-1:]))
    check("it holds the Codex link as the one before Claude",
          bool(chain) and chain[-2]["provider"] == "codex", str(chain[-2:]))
    check("the family of an Ollama model is its vendor",
          family({"provider": "ollama", "model": "deepseek-v4.1-flash:cloud"}) == "deepseek"
          and family({"provider": "ollama", "model": "glm-5.3:cloud"}) == "glm"
          and family({"provider": "ollama", "model": "kimi-k3.1-pro:cloud"}) == "kimi")
    check("the family of a Claude model is claude", family({"provider": "claude", "model": "sonnet"}) == "claude"
          and family({"provider": "ollama", "model": "sonnet"}) == "claude")
    check("a link names the family of its provider", family({"provider": "codex"}) == "codex")
    deepseek = judged_of_model("deepseek-v4.1-flash:cloud")
    names = [label(l) for l in links(judged=deepseek, rs=rs)]
    check("an agent on DeepSeek is never judged by DeepSeek",
          all(family(l) != "deepseek" for l in links(judged=deepseek, rs=rs)), str(names))
    kimi = [label(l) for l in links(judged=judged_of_model("kimi-k3.1-pro:cloud"), rs=rs)]
    check("an agent on Kimi is never judged by Kimi", all("kimi" not in n for n in kimi), str(kimi))
    same = [label(l) for l in links(judged=judged_of_model("sonnet"), rs=rs)]
    check("an agent on Claude is never judged by Claude", all(not n.startswith("claude") for n in same), str(same))
    check("an agent of an unknown family loses no link",
          len(links(judged={}, rs=rs)) == len(links(rs=rs)) and not judged_of_model(""))
    claude_fallback = lambda: False
    off = lambda: False
    try:
        no_claude = [label(l) for l in links(rs=rs)]
        check("«claude_fallback»: false leaves no Claude link",
              all(not n.startswith("claude") for n in no_claude) and bool(no_claude), str(no_claude))
        off = lambda: True
        text, link, notes = ask("test")
        check("the off switch answers nothing", text == "" and link is None and "off" in notes[0], str(notes))
    finally:
        claude_fallback, off = real_cf, real_off
    print(f"\njudge selftest: {len(fails)} failed" if fails else "\njudge selftest: ok")
    return 1 if fails else 0


def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return selftest()
    if a and a[0] == "chain":
        judged = judged_of_model(a[1]) if len(a) > 1 else None
        for link in links(judged):
            print(label(link))
        return 0
    if a and a[0] == "ask" and len(a) > 1:
        text, link, notes = ask(a[1])
        print(f"{label(link) if link else 'no judge'}: {text[:400]}")
        for n in notes:
            print(f"note: {n}")
        return 0 if text else 1
    sys.exit("usage: python3 -I bin/lib/judge.py chain [<model>] | ask \"<prompt>\" | selftest")


if __name__ == "__main__":
    sys.exit(main())
