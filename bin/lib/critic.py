"""Brief critic: before an agent starts, a model of ANOTHER provider reads its brief.

    python3 -I bin/lib/critic.py notes <folder> [<project>] <<<'<brief>'   one critique, printed
    python3 -I bin/lib/critic.py hook        the Claude Code PreToolUse hook of the Agent (Task) tool
    python3 -I bin/lib/critic.py selftest    the fixture checks of today's briefs, without a model

The owner's rule of 2026-11-20: «кожен спавн агента фільтрується через суддю ІНШОГО провайдера». The briefs of this
folder are written by Claude (the coordinator), so the critic never runs on Claude: Codex → Ollama
kimi-k3.1-pro:cloud → Ollama deepseek-v4.1-flash:cloud, the first that answers. The chain is the role "critic" of
`bin/lib/agent.py` (DEFAULT_ROLES), and "roles" in the config replaces it. The provider that wrote the brief is
always skipped: a critic of the same provider proves nothing.

The critic never blocks a start. It gets the brief, the target folder, the project, the user's earlier words that
bear on the brief (the knowledge base) and a cheap scan of the configured projects (ripgrep for the brief's key
nouns: path, line, one line of text). It answers in at most 10 lines in three parts:

    1) ALREADY EXISTS — code or a feature to reuse instead of writing it again (path:line)
    2) HUMAN OUTPUT  — what the result must look like for the person who reads or uses it
    3) OWNER RULES   — the user's earlier words that apply and the brief does not mention

The answer is appended to the brief under «Critic notes (read first)». No answer within 180 s → the agent starts
without notes, and the log says so. Every critique is one line in `state/brief-critic.jsonl`: the project, the
folder, who wrote the brief, which provider judged, the time it took, and why there are no notes.

Before the notes stands the context of every brief (bin/lib/briefctx.py): the owner's rules from the knowledge
base and the project's skills. A brief started through `bin/genius agent` gets it from bin/lib/agent.py; an
internal subagent of this session gets it from this hook. Self-check: `python3 -I bin/lib/critic.py selftest`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
LOG = os.path.join(ROOT, "state", "brief-critic.jsonl")
BUDGET = 180.0                # seconds: a judge from another provider must read the brief and the code (owner 20.11: «він не у контексті, йому потрібно вичитати і зрозуміти суть»)
MAX_LINES = 12                # the critic answers in 10 lines; a longer answer is cut here
MAX_HITS = 12                 # repo-scan lines given to the critic
MAX_RULES = 6                 # knowledge records given to the critic
SCAN_BUDGET = 8.0             # seconds for the ripgrep scan of every configured project


def agent():
    """bin/lib/agent.py: the roles, the providers, and what can run now."""
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import agent as a
    return a


def author_of():
    """Who wrote the brief: the provider of the agent that spawned this one, Claude when nobody says otherwise."""
    return os.environ.get("GENIUS_AUTHOR") or "claude"


def chain(author="", roles=None):
    """The critic's chain without the provider that wrote the brief."""
    roles = roles if roles is not None else agent().roles()
    author = author or author_of()
    links = (roles.get("critic") or {}).get("chain") or []
    return [l for l in links if l.get("provider") != author]


def label(link):
    return link["provider"] + (" " + link["model"] if link.get("model") else "")


def usable(link):
    """'' when this critic can answer now, else why not (the same checks as bin/genius agent)."""
    a = agent()
    p = link["provider"]
    missing = a.installed(p)
    if missing:
        return f"{label(link)}: {missing}"
    if p == "ollama":
        a.ensure_ollama()
        if not a.answers(link):
            return f"{label(link)}: no answer (not signed in, no subscription, quota, or servers)"
    elif p in ("claude", "codex") and a.spent(p):
        return f"{label(link)}: limits nearly spent"
    return ""


def pick(author="", links=None, available=None):
    """(link, notes): the first critic of another provider that can answer now. (None, notes) when none can."""
    available = available or usable
    notes = []
    for link in (links if links is not None else chain(author)):
        if link.get("provider") == (author or author_of()):
            notes.append(f"{label(link)}: wrote the brief, so it cannot judge it")
            continue
        why = available(link)
        if why:
            notes.append(why)
            continue
        return dict(link), notes
    return None, notes


STOP = set("""a an and the or but if then than that this these those there here it its is are was were be been being
of in on at to for with from by as about into over under again more most some any all no not only own same so too
very can will just must should would could may might do does did done have has had you your we our they their them
he she his her i me my us what which who whom when where why how all also when while before after both each few
other such nor own same so than too s t don now new old use used using make made add added fix fixed test tests
work works write writes wrote read reads file files folder folders line lines code task brief agent project
котрі який яка яке які це цей ця ці щоб якщо також вже ще для при про після перед між через дуже треба можна
зробити зроби зробити перевір перевірити додати додай файл файли папка папці код рядок тест тести задача
проєкт агент бриф правило правила""".split())


def nouns(brief, limit=5):
    """The brief's key words for the repo scan. Identifiers first, then the Latin words of code, then the rest:
    code, paths and identifiers of a repository are Latin, whatever language the brief is written in."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_/-]{2,}|[А-Яа-яЇїІіЄєҐґ][А-Яа-яЇїІіЄєҐґ0-9_/-]{3,}", brief)
    seen, out = set(), []
    for w in words:
        k = w.lower()
        if k in seen or k in STOP or len(k) < 4:
            continue
        seen.add(k)
        shaped = bool(re.search(r"[/_-]|(?<=[a-z])[A-Z]", w))
        latin = bool(re.fullmatch(r"[A-Za-z0-9_/-]+", w))
        out.append((0 if shaped else 1 if latin else 2, -len(k), w))
    out.sort()
    return [w for _, _, w in out[:limit]]


def rules(brief, project="", limit=MAX_RULES):
    """The user's earlier words that bear on the brief. ([], when the base is not there or gives nothing)."""
    try:
        here = os.path.dirname(os.path.realpath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import know
        found, vectors = know.search(brief, project, limit * 2)
    except Exception:
        return []
    out, seen = [], set()
    for score, r, cos, exact in found:
        if vectors and cos < 0.55 and not exact:
            continue                      # a weak match is worse than none: no hint beats a wrong one
        text = (r["text"] or "").strip().replace("\n", " ")
        if not text or text in seen:
            continue
        seen.add(text)
        out.append((r["source"] or "", text[:300]))
        if len(out) >= limit:
            break
    return out


def folders(folder="", limit=4):
    """The target folder first, then the folders of the configured projects: where the code may already live."""
    paths = []
    if folder and os.path.isdir(folder):
        paths.append(os.path.realpath(folder))
    for p in (read_config().get("projects") or [])[:limit]:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if os.path.isdir(path) and path not in paths:
            paths.append(path)
    return paths[:limit]


SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".nuxt", "vendor", "worktrees", "__pycache__"}


def rg_binary():
    """ripgrep as a program. In this shell `rg` can be a shell function of Claude Code — not a file to run."""
    for path in (shutil.which("rg"), "/opt/homebrew/bin/rg", "/usr/local/bin/rg"):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def scan(brief, folders_, cap=MAX_HITS, budget=SCAN_BUDGET):
    """[(where, line, text)]: code that may already do the task. A cheap search, capped in lines and seconds."""
    words = nouns(brief)
    if not words or not folders_:
        return []
    rg = rg_binary()
    hits, seen = [], set()
    stop = time.time() + budget
    for folder in folders_:
        left = max(1.0, stop - time.time())
        name = os.path.basename(folder)
        if rg:
            cmd = [rg, "--no-heading", "--line-number", "--ignore-case", "--max-count", "1", "--max-filesize", "2M"]
            for d in sorted(SKIP_DIRS):
                cmd += ["--glob", f"!{d}"]
            for w in words:
                cmd += ["-e", w]
            cmd.append(folder)
        else:                                  # BSD grep: no --exclude-dir; the skipped folders are filtered here
            cmd = ["grep", "-rInm1", "-i", "-e", "\\|".join(re.escape(w) for w in words), folder]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=left)
        except (OSError, subprocess.TimeoutExpired):
            r = None
        for row in (r.stdout.splitlines() if r else []):
            parts = row.split(":", 2)
            if len(parts) < 3:
                continue
            path, line, text = parts
            rel = os.path.relpath(path, folder) if os.path.isabs(path) else path
            if seen_path(rel) or any(f"/{d}/" in f"/{rel}" for d in SKIP_DIRS):
                continue
            seen.add(rel)
            hits.append((f"{name}/{rel}", line, " ".join(text.split())[:90]))
            if len(hits) >= cap:
                return hits
        for rel in path_hits(folder, words, cap - len(hits), stop):
            if seen_path(rel) or rel in seen:
                continue
            seen.add(rel)
            hits.append((f"{name}/{rel}", "", ""))
            if len(hits) >= cap:
                return hits
    return hits


def seen_path(rel):
    return "/." in "/" + rel or rel.startswith(".")


def path_hits(folder, words, cap, deadline):
    """[rel]: files whose path itself names a key word — «logs/digest.ts» is found before any text is read."""
    out = []
    if cap <= 0:
        return out
    low = [w.lower() for w in words]
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), folder)
            if any(w in rel.lower() for w in low):
                out.append(rel)
                if len(out) >= cap:
                    return out
        if time.time() > deadline:
            return out
    return out


def read_config():
    try:
        with open(os.path.join(ROOT, "genius.config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def evidence(brief, folder="", project="", rules_fn=None, scan_fn=None):
    """What the critic is told besides the brief: the user's words, and code that may already do the task."""
    return {"rules": (rules_fn or rules)(brief, project), "code": (scan_fn or scan)(brief, folders(folder))}


def build_prompt(brief, folder, project, ev):
    """The critic's words: the brief, the evidence, and the three parts of the answer."""
    rules_txt = "\n".join(f"- [{s}] {t}" for s, t in ev["rules"]) or "- none found"
    code_txt = "\n".join(f"- {p}:{n} · {t}" for p, n, t in ev["code"]) or "- none found"
    return (
        "You critique a brief before an unattended coding agent starts on it. Nobody can ask you a question: "
        "answer now, from the evidence below.\n\n"
        f"THE BRIEF\n{brief.strip()[:6000]}\n\n"
        f"THE TARGET FOLDER\n{folder or '(not given)'}\n\n"
        f"THE PROJECT\n{project or '(not a configured project)'}\n\n"
        "EVIDENCE, checked on this Mac just now.\n"
        "The user's earlier words that bear on this brief (the knowledge base):\n"
        f"{rules_txt}\n\n"
        "Code that may already do the task (ripgrep for the brief's key words in the configured projects):\n"
        f"{code_txt}\n\n"
        "ANSWER in at most 10 lines, these three parts and nothing else:\n"
        "1) ALREADY EXISTS — path:line of the code or the feature the brief should reuse instead of writing it "
        "again. «none found» when the evidence has nothing.\n"
        "2) HUMAN OUTPUT — what the result must look like to the person who reads or uses it: format, language, "
        "«no filler». Only what the brief leaves silent.\n"
        "3) OWNER RULES — the lines of the user's earlier words that apply and the brief does not mention; "
        "«none» when none apply.\n"
        "No preamble, no praise, no restating the brief.\n"
    )


def call(prompt, link, budget):
    """The critic's text, or '' when it does not answer in time. Never Claude: another provider judges."""
    p = link["provider"]
    if p == "codex":
        return codex(prompt, link, budget)
    if p == "ollama":
        return ollama(prompt, link, budget)
    return ""


def codex(prompt, link, budget):
    cmd = ["codex", "exec", "--skip-git-repo-check"]
    if link.get("model"):
        cmd += ["-m", link["model"]]
    cmd.append(prompt)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["GENIUS_CRITIC"] = "1"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=budget, cwd=ROOT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def ollama(prompt, link, budget):
    """The critic's answer from an Ollama model. A thinking model gets no budget to think here: the notes are ten
    lines, and the thinking would eat the whole allowance and print nothing."""
    for extra in ({"think": False}, {}):        # an older Ollama refuses «think»: the second try drops it
        body = {"model": link["model"], "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0.2, "num_predict": 800}, **extra}
        req = urllib.request.Request(agent().OLLAMA + "/api/chat", data=json.dumps(body).encode(),
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


def trim(text, lines=MAX_LINES):
    out = [l.rstrip() for l in text.strip().splitlines() if l.strip()]
    return "\n".join(out[:lines])


def log(rec):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def result(rec):
    return {"notes": rec.get("notes") or "", "provider": rec["provider"], "ms": rec["ms"], "why": rec["why"]}


def notes_for(brief, folder="", project="", author="", budget=BUDGET, links=None, available=None,
              judge=None, rules_fn=None, scan_fn=None, ev=None):
    """{notes, provider, ms, why}: the critic's notes for one brief. Never raises, never blocks.
    ``ev``: evidence already gathered (`evidence`), so the caller's context and the critic share one search."""
    t0 = time.time()
    author = author or author_of()
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "project": project, "folder": folder,
           "author": author, "provider": "", "ms": 0, "lines": 0, "why": "", "notes": ""}
    if ev is None:
        try:
            ev = evidence(brief, folder, project, rules_fn=rules_fn, scan_fn=scan_fn)
        except Exception as e:                              # the evidence is best effort: the brief still starts
            ev = {"rules": [], "code": []}
            rec["why"] = f"evidence: {type(e).__name__}"
    link, skipped = pick(author, links, available)
    if not link:
        rec["why"] = "no critic of another provider can answer: " + "; ".join(skipped)
        rec["ms"] = int((time.time() - t0) * 1000)
        log(rec)
        return result(rec)
    rec["provider"] = label(link)
    prompt = build_prompt(brief, folder, project, ev)
    left = max(8.0, budget - (time.time() - t0))
    try:
        text = (judge or call)(prompt, link, left)
    except Exception as e:
        text = ""
        rec["why"] = f"critic: {type(e).__name__}"
    rec["ms"] = int((time.time() - t0) * 1000)
    if not (text or "").strip():
        rec["why"] = rec["why"] or (f"no answer within {int(budget)} s" if rec["ms"] >= budget * 1000 * 0.95
                                    else "no answer")
        log(rec)
        return result(rec)
    notes = trim(text)
    rec["notes"], rec["lines"] = notes, len(notes.splitlines())
    log(rec)
    return result(rec)


def block(notes):
    """The notes as the brief carries them. '' when there is nothing to say."""
    if not (notes or "").strip():
        return ""
    return "Critic notes (read first)\n" + notes.strip() + "\n"


def hook_output(brief, folder, project, notes, rules_fn=None):
    """«additionalContext» of the Agent (Task) tool: the context of every brief (bin/lib/briefctx.py) — the owner's
    rules and the project's skills — then the critic's notes. '' when neither has anything to say."""
    import briefctx
    ctx = briefctx.context(brief, folder, project, rules_fn=rules_fn)
    parts = [p for p in (ctx["text"], block(notes)) if (p or "").strip()]
    return "\n\n".join(parts)


def hook():
    """The Agent (Task) tool: the context of the brief and the same critic on tool_input.prompt, both added to the
    tool call — the skills and the owner's rules reach an agent this session starts, on any provider."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if (data.get("tool_name") or "") not in ("Agent", "Task"):
        return 0
    ti = data.get("tool_input") or {}
    brief = (ti.get("prompt") or ti.get("description") or "").strip()
    if not brief:
        return 0                                  # no brief at all: there is nothing to read
    folder = os.path.realpath(data.get("cwd") or os.getcwd())
    project = agent().project_of(folder).get("name", "")
    res = notes_for(brief, folder, project, author=author_of())
    text = hook_output(brief, folder, project, res["notes"])
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": text}}))
    return 0


# --- the self-check: today's two briefs, without a model and without the real knowledge base ---------------------

# The owner's cases of 2026-11-20: the LW logs brief must surface the digest that already exists, and a Renew page
# brief must surface the rules the owner had already stated. The fixtures below are those two briefs, a small repo
# that holds the digest, and a two-record knowledge base.
FIXTURE_REPO = {
    "backend/src/core/logs/log-digest.ts":
        "// digest of the logs of one day: counts by level and the slowest routes\n"
        "export function digestLogs(rows: LogRow[]): LogDigest {\n",
    "backend/src/core/logs/logs.service.ts": "import { digestLogs } from './log-digest';\n",
    "frontend/src/pages/renew.tsx": "export function RenewPage() { return <RenewList limit={12} />; }\n",
}
FIXTURE_BRIEFS = {
    "lw-logs": "LW: на сторінці /logs зробити зведення за останню добу — скільки помилок, які повільні роути. "
               "Бекенд backend, фронт сторінка logs.",
    "renew": "Renew: на сторінці продовження показати активні абонементи людини і дати продовжити. "
             "Список активів беремо з профілю.",
}
FIXTURE_RULES = [
    ("власник 2026-11-14 10:02", "Renew page: показувати ≤8 actives; решта — за кнопкою «показати всі»."),
    ("власник 2026-11-19 16:40", "Renew: картки брендів гуглимо; невпевнені — скіпаємо, без вигаданих описів."),
    ("власник 2026-11-17 12:18", "Список «Зроблено сьогодні» поповнюється тією ж відповіддю, де робота закінчена."),
]


def _fixture_rules(brief, project="", limit=MAX_RULES):
    """A stand-in for know.search: the fixture records whose words the brief holds."""
    words = {w.lower() for w in re.findall(r"[\w]{4,}", brief)}
    out = []
    for source, text in FIXTURE_RULES:
        if words & {w.lower() for w in re.findall(r"[\w]{4,}", text + " " + source)}:
            out.append((source, text))
    return out[:limit]


def selftest():
    import tempfile
    fails, checks = [], 0

    def check(name, ok, detail=""):
        nonlocal checks
        checks += 1
        print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="critic-selftest-")
    repo = os.path.join(tmp, "live-website-nextjs")
    for rel, text in FIXTURE_REPO.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    try:
        # 1) the critic is never the provider that wrote the brief, and never Claude for a Claude brief
        links = chain("claude", {"critic": {"chain": [{"provider": "codex"},
                                                      {"provider": "ollama", "model": "kimi-k3.1-pro:cloud"},
                                                      {"provider": "ollama", "model": "deepseek-v4.1-flash:cloud"},
                                                      {"provider": "claude"}]}})
        check("a Claude brief is never judged by Claude", all(l["provider"] != "claude" for l in links),
              str(links))
        default = (agent().roles().get("critic") or {}).get("chain") or []
        check("the default critic chain exists and holds no Claude link",
              bool(default) and all(l["provider"] != "claude" for l in default), str(default))
        got, notes = pick("claude", [{"provider": "claude", "model": "sonnet"}, {"provider": "codex"}],
                          available=lambda l: "")
        check("pick skips the author's provider", got and got["provider"] == "codex", str(got))
        got, _ = pick("claude", [{"provider": "claude"}], available=lambda l: "")
        check("pick gives no Claude critic at all", got is None, str(got))
        got, _ = pick("codex", [{"provider": "codex"}, {"provider": "claude"}], available=lambda l: "")
        check("a Codex brief may be judged by Claude", got and got["provider"] == "claude", str(got))
        got, why = pick("claude", [{"provider": "codex"}], available=lambda l: "codex: not installed")
        check("an unavailable critic is skipped, not waited for", got is None and "not installed" in why[0], str(why))

        # 2) today's LW brief: the digest that already exists is found by the repo scan
        hits = scan(FIXTURE_BRIEFS["lw-logs"], [repo])
        check("the LW logs brief surfaces log-digest.ts",
              any("backend/src/core/logs/log-digest.ts" in h[0] for h in hits), str(hits[:3]))
        check("the scan of a brief with no key words is empty", scan("ок", [repo]) == [], "")

        # 3) today's Renew brief: the owner's rules reach the critic, in the prompt it reads
        seen = {}

        def fake_judge(prompt, link, budget):
            seen["prompt"], seen["link"], seen["budget"] = prompt, link, budget
            return ("1) ALREADY EXISTS — none found\n"
                    "2) HUMAN OUTPUT — список українською, ліміт 8, без води\n"
                    "3) OWNER RULES — ≤8 actives; гуглимо; невпевнені — скіпаємо")

        res = notes_for(FIXTURE_BRIEFS["renew"], repo, "demo", author="claude", budget=BUDGET,
                        links=[{"provider": "ollama", "model": "kimi-k3.1-pro:cloud"}],
                        available=lambda l: "", judge=fake_judge, rules_fn=_fixture_rules,
                        scan_fn=lambda b, f, **k: scan(b, [repo]))
        check("the Renew brief surfaces «≤8 actives»", "≤8 actives" in seen.get("prompt", ""),
              seen.get("prompt", "")[:200])
        check("the Renew brief surfaces «гуглимо; невпевнені — скіпаємо»",
              "гуглимо" in seen.get("prompt", "") and "скіпаємо" in seen.get("prompt", ""))
        check("the critic is told which project and folder", "demo" in seen.get("prompt", "")
              and repo in seen.get("prompt", ""))
        check("the notes carry the three parts", res["notes"].startswith("1) ALREADY EXISTS")
              and "OWNER RULES" in res["notes"], res["notes"][:80])
        check("the notes are appended under «Critic notes (read first)»",
              block(res["notes"]).startswith("Critic notes (read first)\n1) ALREADY EXISTS"))
        check("the judging provider is logged", res["provider"] == "ollama kimi-k3.1-pro:cloud", res["provider"])

        # 4) a critic that does not answer never blocks the start
        t0 = time.time()
        silent = notes_for(FIXTURE_BRIEFS["renew"], repo, "demo", author="claude", links=[{"provider": "codex"}],
                           available=lambda l: "", judge=lambda p, l, b: "", rules_fn=_fixture_rules,
                           scan_fn=lambda b, f, **k: [])
        check("no answer → no notes, the start goes on", silent["notes"] == "" and "no answer" in silent["why"],
              silent["why"])
        check("no answer → the brief gets no block", block(silent["notes"]) == "")
        check("the log has the line of every critique",
              os.path.exists(LOG) and sum(1 for l in open(LOG, encoding="utf-8")
                                          if '"demo"' in l or '"project": "demo"' in l) >= 2, LOG)
        check("a long answer is cut to 12 lines", len(trim("\n".join(f"{i}) x" for i in range(40))).splitlines()) == 12)

        # 5) the hook: only the Agent and Task tools, and a real brief
        import io
        saved, saved_out = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"},
                                                "cwd": ROOT}))
            sys.stdout = io.StringIO()
            rc = hook()
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = saved, saved_out
        check("the hook ignores other tools", rc == 0 and out == "", out)
        saved, saved_out = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps({"tool_name": "Agent", "cwd": ROOT, "tool_input": {}}))
            sys.stdout = io.StringIO()
            rc = hook()
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = saved, saved_out
        check("the hook leaves a call with no brief alone (and asks no model)", rc == 0 and out == "", out)
        import inspect
        check("the hook keeps no switch for a short brief", "len(" not in inspect.getsource(hook),
              inspect.getsource(hook)[:120])

        # 6) the Agent-tool hook carries the context of the brief before the notes (the owner's goal of 2026-11-20)
        skill = os.path.join(repo, ".claude", "skills", "renew-page", "SKILL.md")
        os.makedirs(os.path.dirname(skill), exist_ok=True)
        with open(skill, "w", encoding="utf-8") as f:
            f.write("---\nname: renew-page\ndescription: Сторінка продовження: активні абонементи, ліміт 8. "
                    "Тригери: «продовження», «абонементи».\n---\n")
        out = hook_output(FIXTURE_BRIEFS["renew"], repo, "demo", "1) ALREADY EXISTS — none found",
                          rules_fn=lambda b, p, n: [("власник · rule", "Renew page: ≤8 actives")])
        check("the Agent hook carries the owner's rules", "≤8 actives" in out, out[:200])
        check("the Agent hook carries the project's skills", "renew-page/SKILL.md" in out, out[:300])
        check("the Agent hook says to read the skills before the start", "read these before you start" in out)
        check("the context stands before «Critic notes»",
              out.find("Owner's rules") < out.find("Critic notes (read first)")
              < out.find("1) ALREADY EXISTS"), out[:300])
        check("one line per skill in the Agent hook",
              all(" — " in l for l in out.splitlines() if "/SKILL.md" in l))
        check("the Agent hook says nothing when neither has anything",
              hook_output(FIXTURE_BRIEFS["renew"], "", "demo", "", rules_fn=lambda b, p, n: []) == "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\ncritic selftest: {len(fails)} failed of {checks}" if fails else f"\ncritic selftest: ok ({checks})")
    return 1 if fails else 0


def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return sys.exit(selftest())
    if a == ["hook"]:
        return sys.exit(hook())
    if a and a[0] == "notes":
        folder = os.path.realpath(os.path.expanduser(a[1])) if len(a) > 1 else ""
        project = a[2] if len(a) > 2 else (agent().project_of(folder).get("name", "") if folder else "")
        brief = sys.stdin.read()
        res = notes_for(brief, folder, project)
        print(block(res["notes"]) or f"(no notes: {res['why']})")
        return
    sys.exit(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
