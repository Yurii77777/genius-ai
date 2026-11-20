"""Every start of an agent passes the brief critic. This test fails when one does not.

    python3 -I bin/lib/selfcheck.py         (or: bin/genius selfcheck)

The owner's rule of 2026-11-20: «кожен спавн агента фільтрується через суддю ІНШОГО провайдера». A rule in a
document is not a mechanism: this test reads the code and fails on a start path that skips `bin/lib/critic.py`.

It checks:
1. `bin/lib/agent.py` runs the critic in `main()`, so every `bin/genius agent` start carries the notes — and the
   critic comes before the branch that starts the agent, so no provider path (Ollama, Codex, Claude, a local
   model) can slip past it.
2. The critic's chain holds no Claude link: the briefs of this folder are written by Claude.
3. `.claude/settings.json` gives the Agent (Task) tool a PreToolUse hook that runs the same critic.
4. Every other place that starts a model on a brief goes through `bin/genius agent`: a direct `hc run` of
   `claude`/`codex` with a brief is a bypass. Herdr workers (`bin/genius ops sweep`, the watcher) run a shell
   loop, not a brief: their `hc run` is not counted.
5. `bin/genius` routes `selfcheck`, so the test is one word away.

A new start path fails this test until it passes the critic. Read the failure and wire it; do not edit the test
to pass.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
LIB = os.path.join(ROOT, "bin", "lib")
SETTINGS = os.path.join(ROOT, ".claude", "settings.json")
LOOP = re.compile(r"while\s+true|launchctl|\.pid\b")


def text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def starts_agent(path):
    """[(line, snippet)]: places where this file starts a model program with a brief in Herdr."""
    body = text(path)
    out = []
    for m in re.finditer(r'"hc",\s*"run"', body):
        block = body[m.start():m.start() + 900]
        if LOOP.search(block):
            continue                                  # a worker loop, not a brief
        if not re.search(r"claude|codex", block):
            continue                                  # a shell loop or a command, not a model with a brief
        out.append((body[:m.start()].count("\n") + 1, " ".join(block.split())[:120]))
    return out


def settings_hook():
    """The PreToolUse entry that gives the Agent (Task) tool the critic, or None."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    for entry in (data.get("hooks") or {}).get("PreToolUse") or []:
        matcher = entry.get("matcher") or ""
        for h in entry.get("hooks") or []:
            cmd = h.get("command") or ""
            if "critic.py" in cmd and "hook" in cmd and ("Agent" in matcher and "Task" in matcher):
                return h
    return None


def main():
    fails, checks = [], 0

    def check(name, ok, detail=""):
        nonlocal checks
        checks += 1
        print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    # 1) bin/genius agent: the critic runs for every start, before any provider branch
    body = text(os.path.join(LIB, "agent.py"))
    main_src = body[body.find("def main("):]
    at = main_src.find("critic.notes_for(")
    branches = min([i for i in (main_src.find('spec["provider"] == "local"'),
                                main_src.find('spec["provider"] == "codex"'),
                                main_src.find('if not shutil.which("hc")'),
                                main_src.find('"hc", "run"')) if i >= 0] or [-1])
    check("bin/lib/agent.py runs the critic on every start, before any branch that starts the agent",
          at > 0 and branches > at, f"critic at {at}, the first start branch at {branches}")
    check("the critic is appended to the brief with its heading",
          "critic.block(" in body and "brief = brief + " in body)
    check("the critic has no off switch", not re.search(r"no[-_]critic|skip[-_]critic|CRITIC_OFF", body))

    # 2) the critic is never the provider that wrote the brief
    sys.path.insert(0, LIB)
    import critic
    links = ((critic.agent().roles().get("critic") or {}).get("chain") or [])
    check("the critic chain exists", bool(links), str(links))
    check("the critic chain holds no Claude link", all(l.get("provider") != "claude" for l in links), str(links))
    check("the critic skips the author's provider", critic.pick("claude", [{"provider": "claude"},
                                                                          {"provider": "codex"}],
                                                                available=lambda l: "")[0] is not None)

    # 3) the Agent (Task) tool
    h = settings_hook()
    check(".claude/settings.json runs the critic before the Agent and Task tools", h is not None, str(SETTINGS))
    check("that hook has time for the 40 s of the critic", bool(h) and int(h.get("timeout") or 0) > critic.BUDGET,
          f"timeout {h.get('timeout') if h else 0} s")

    # 4) no other start path bypasses it
    bypass, through = [], []
    for name in sorted(os.listdir(LIB)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(LIB, name)
        starts = starts_agent(path)
        if not starts:
            continue
        if name == "agent.py":
            continue
        if re.search(r"critic\.(notes_for|block)\(", text(path)):
            through.append(name)
        else:
            bypass.append((name, starts[0]))
    check("no file starts an agent without the critic", not bypass, str(bypass))
    if through:
        print("     also passes the critic: " + ", ".join(sorted(set(through))))

    # 5) the start paths that hand a brief to bin/genius agent, and so inherit the critic
    for name in ("ticket.py", "prfix.py"):
        src = text(os.path.join(LIB, name))
        direct = starts_agent(os.path.join(LIB, name))
        check(f"bin/lib/{name} starts its agent through bin/genius agent",
              not direct and re.search(r'"agent",\s*"(code|review)"', src) is not None, str(direct))

    # 6) one word away
    check("bin/genius routes selfcheck", "selfcheck" in text(os.path.join(ROOT, "bin", "genius")))

    print(f"\nselfcheck: {len(fails)} failed of {checks}" if fails else f"\nselfcheck: ok ({checks})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
