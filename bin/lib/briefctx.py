"""The context of every brief: the owner's rules and the project's skills.

    python3 -I bin/lib/briefctx.py show <folder> [<project>] [--role <role>] <<'EOF'
    <the brief>
    EOF
    python3 -I bin/lib/briefctx.py selftest

The owner's goal of 2026-11-20: «головне щоб ці поперечники отримували знання із бази знань та скіли релевантні».
Every brief, on every provider (Codex, an Ollama model, Claude), carries this block:

1. «Owner's rules and decisions (from the knowledge base)» — the records of bin/lib/know.py that bear on the brief,
   at most 6, one line each with its source;
2. «Skills to read first» — the project's `.claude/skills/*/SKILL.md` whose name or frontmatter description names
   the brief's task, at most 5, each as an absolute path with a one-line why, and the instruction to read them.

A Codex or Ollama agent has no Claude Code hooks: this block is its only source of the owner's words and of the
project's skills. A Claude agent is given it too, because a hook can miss. Two callers, no others: `bin/lib/agent.py`
puts it before «Critic notes», and the Agent (Task) hook of `bin/lib/critic.py` puts it before the same notes. Both
pass the evidence they already have, so one search of the knowledge base serves the context and the critic.

A weak match gives nothing: a wrong rule or a wrong skill is worse than none. The block never exceeds 25 lines.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
MAX_RULES = 6                 # the owner's rules that reach one brief
MAX_SKILLS = 5                # the skills that reach one brief
MAX_LINES = 25                # the whole block: the owner's limit
RULE_CHARS = 200              # one rule is one line, cut here
WHY_CHARS = 110               # one skill's why: what it is about, cut here
MIN_SCORE = 2                 # a skill reaches a brief only when its name or its description names the task
WORD = re.compile(r"[A-Za-zА-Яа-яЇїІіЄєҐґ0-9][A-Za-zА-Яа-яЇїІіЄєҐґ0-9'’_-]*")
FM = re.compile(r"^---\s*\n(.*?)(?:\n---\s*(?:\n|$))", re.S)
# The role shapes which skills matter beside the brief's own words: a review needs the repo's review rules, a render
# needs the video rules. The words are stems, matched inside the skill's name and description.
ROLE_HINTS = {
    "review":       ("review", "рев", "checklist", "convention", "quality", "test"),
    "final-review": ("review", "checklist", "quality", "merge", "convention"),
    "research":     ("copy", "текст", "seo", "content", "brand", "бренд"),
    "look":         ("design", "дизайн", "image", "зображенн", "visual", "візуал"),
    "render":       ("video", "відео", "remotion", "image", "lottie", "anim"),
}
EXTRA_STOP = """так ні це цей ця ці вже ще дуже теж лише тільки можна треба потрібно кожен кожна кожне
які який яка яке коли якщо щоб або але весь вся все всі свій своя своє свої має мають буде бути
page pages make made add fix write file files list show need must should would could have been
with from this that these those they them their there here what which when where why how just only
""".split()
_STOP = None


def _lib(name):
    """A module of bin/lib, importable whether this file is run as a script or imported by another one."""
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    return __import__(name)


def stopwords():
    """Function words and task words: they name nothing that a rule or a skill can be about."""
    global _STOP
    if _STOP is None:
        try:
            _STOP = set(_lib("critic").STOP)
        except Exception:
            _STOP = set()
        _STOP |= set(EXTRA_STOP)
    return _STOP


def tokens(text, min_len=3):
    """The words of a text that name something, lowercased, function words out."""
    out = set()
    for w in WORD.findall(text or ""):
        k = w.lower().strip("-_'’")
        if len(k) >= min_len and k not in stopwords():
            out.add(k)
    return out


def shared(a, b):
    """The words of two texts that name the same thing: equal, or a common stem of five letters and no more than
    three letters apart in length («ролик»/«ролика», «активні»/«активи»). Ukrainian endings differ, so equality is
    too strict and a bare prefix too loose."""
    out = set()
    for x in a:
        for y in b:
            if x == y:
                out.add(x)
                break
            n = 0
            for cx, cy in zip(x, y):
                if cx != cy:
                    break
                n += 1
            if n >= 5 and abs(len(x) - len(y)) <= 3:
                out.add(x)
                break
    return out


def one_line(text, limit):
    """One line, no markdown of its own, cut at a limit: this text is read inside another model's brief."""
    return " ".join((text or "").split())[:limit].strip()


def owner_rules(brief, project="", limit=MAX_RULES):
    """[(source, text)]: the owner's words that bear on this brief, from the knowledge base (bin/lib/know.py).
    The search and its weak-match filter are the critic's own (bin/lib/critic.py), so the context and the critic
    never disagree about what the owner said."""
    try:
        return _lib("critic").rules(brief, project, limit)
    except Exception:
        return []


def roots(folder):
    """The folder itself and the main repository it belongs to: in a worktree the skills live at the repo root."""
    out = []
    folder = os.path.realpath(folder) if folder else ""
    if folder and os.path.isdir(folder):
        out.append(folder)
    try:
        repo = _lib("agent").repo_root_of(folder) if folder else ""
    except Exception:
        repo = ""
    if repo and repo not in out and os.path.isdir(repo):
        out.append(repo)
    return out


def skill_files(folder):
    """[SKILL.md]: the skills of the folder and of its main repository, two folders deep at most."""
    out, seen = [], set()
    for base in roots(folder):
        d = os.path.join(base, ".claude", "skills")
        if not os.path.isdir(d):
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            if dirpath[len(d):].count(os.sep) > 2:
                dirnames[:] = []
                continue
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            if "SKILL.md" in filenames:
                path = os.path.realpath(os.path.join(dirpath, "SKILL.md"))
                if path not in seen:
                    seen.add(path)
                    out.append(path)
    return sorted(out)


QUOTED = re.compile(r"«([^»]{1,80})»")


def triggers(desc):
    """The words a skill declares as its own triggers («Тригери: «відео», «ролик»»): the project itself says what
    the skill is for, so a brief that names one of them is a stronger signal than any other word of the text."""
    return set().union(*[tokens(t) for t in QUOTED.findall(desc or "")]) if QUOTED.search(desc or "") else set()


def front(path):
    """(name, description) of a SKILL.md: its frontmatter, or the folder's name and ''. Only the head is read."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(6000)
    except OSError:
        return "", ""
    m = FM.match(head)
    body = m.group(1) if m else head

    def field(key, default=""):
        found = re.search(rf"^{key}:\s*(.*)$", body, re.M)
        return found.group(1).strip().strip("'\"") if found else default

    return field("name", os.path.basename(os.path.dirname(path))), field("description")


def skills(brief, folder="", role="", limit=MAX_SKILLS, every=False, detail=None):
    """[(path, why, score)]: the folder's skills whose name or description names the brief's task. [] when the
    folder holds no skills or none matches. ``every``: every skill the brief's words reach, without the cut;
    ``detail``: a dict filled with the words that reached each skill — both show why a brief got what it got."""
    want = tokens(brief)
    if not want or not folder:
        return []
    base = os.path.basename(os.path.normpath(folder)).lower()
    hints = ROLE_HINTS.get(role) or ()
    files = skill_files(folder)
    rows = []
    for path in files:
        name, desc = front(path)
        skill = os.path.basename(os.path.dirname(path))
        named = shared(want, tokens(skill.replace("-", " ").replace("_", " ")))
        trig = triggers(desc)
        told, declared = shared(want, tokens(desc)), shared(want, trig)
        if skill.lower() and skill.lower() in (brief or "").lower():
            named.add(skill.lower())                  # the brief names the skill outright
        rows.append({"path": path, "skill": skill, "name": name, "desc": desc, "trig": trig,
                     "named": named, "told": told, "declared": declared})
    # Rare words carry the meaning: a word of the brief that most skills mention («бренд», «сторінка») says little,
    # a word that one skill declares («картинка») says almost everything.
    df = {}
    for r in rows:
        for w in r["named"] | r["told"] | r["declared"]:
            df[w] = df.get(w, 0) + 1
    out = []
    for r in rows:
        wt = {w: 3 if df.get(w, 1) <= 2 else 2 if df.get(w, 1) <= 6 else 1 for w in r["named"] | r["told"]}
        if detail is not None:
            detail[r["path"]] = {"named": sorted(r["named"]), "declared": sorted(r["declared"]),
                                 "told": sorted(r["told"]), "triggers": sorted(r["trig"])[:8]}
        # A skill that declares its own triggers («Тригери: «відео», «ролик»») is reached by its name or by one of
        # them, and by nothing else: the trigger list is the project's own statement of when the skill applies,
        # while the rest of a long description matches any brief by a word or two. A skill that declares no
        # triggers is reached by its description as a whole. The brief's words qualify a skill, nothing else does:
        # a wrong skill costs more than a missing one.
        if r["trig"]:
            if not (r["named"] or r["declared"]):
                continue
            score = 3 * sum(wt.get(w, 1) for w in r["named"]) + 2 * sum(wt.get(w, 1) for w in r["declared"])
        else:
            score = 3 * sum(wt.get(w, 1) for w in r["named"]) + sum(wt.get(w, 1) for w in r["told"])
            if score < MIN_SCORE:
                continue
        by_role = bool(hints) and any(h in f"{r['skill']} {r['name']} {r['desc']}".lower() for h in hints)
        if by_role:
            score += 2
        if base and (r["skill"].startswith(base + "-") or r["skill"] == base):
            score += 1                                # this project's own skills first, on equal words
        why = one_line(r["desc"], WHY_CHARS)
        if not why:
            matched = sorted(r["named"] | r["told"])[:3]
            why = ("role " + role if by_role else "matches") + (": " + ", ".join(matched) if matched else "")
        out.append((r["path"], why, score))
    out.sort(key=lambda x: (-x[2], x[0]))
    picked = out if every else out[:limit]
    # A skill that a found one sends you to: the video skill says «тексти в кадрі, голос і підписи — через
    # humanize-ua». That is the task's next rule, and the brief's own words are often too few to reach it.
    names = {os.path.basename(os.path.dirname(p)): p for p in files}
    for path, _, score in list(picked):
        desc = front(path)[1].lower()
        for other, opath in names.items():
            if opath == path or any(q == opath for q, _, _ in picked):
                continue
            if other and re.search(rf"(?<![\w-]){re.escape(other.lower())}(?![\w-])", desc):
                picked.append((opath, f"suggested by {os.path.basename(os.path.dirname(path))}", score - 1))
    picked.sort(key=lambda x: (-x[2], x[0]))
    return picked if every else picked[:limit]


def context(brief, folder="", project="", role="", limit_rules=MAX_RULES, limit_skills=MAX_SKILLS,
            rules_fn=None):
    """{"text", "rules", "skills"}: the block every brief carries, and how much is in it. Never raises."""
    rules_ = []
    if limit_rules:
        try:
            rules_ = list((rules_fn or owner_rules)(brief, project, limit_rules))[:limit_rules]
        except Exception:
            rules_ = []
    try:
        found = skills(brief, folder, role, limit_skills) if (folder and limit_skills) else []
    except Exception:
        found = []
    parts = []
    if rules_:
        lines = [f"- [{one_line(src, 60)}] {one_line(text, RULE_CHARS)}" for src, text in rules_ if one_line(text, 1)]
        if lines:
            parts.append("Owner's rules and decisions (from the knowledge base)\n" + "\n".join(lines))
    if found:
        parts.append("Skills to read first — read these before you start\n" +
                     "\n".join(f"- {path} — {why}" for path, why, _ in found))
    text = "\n\n".join(parts).strip()
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        text = "\n".join(lines[:MAX_LINES]).strip()
    return {"text": text, "rules": len(rules_), "skills": len(found)}


def block(brief, folder="", project="", role="", **kw):
    """The block as the brief carries it. '' when there is nothing: an empty heading is noise."""
    return context(brief, folder, project, role, **kw)["text"]


# --- the self-check: today's briefs, a small skills folder and a small knowledge base, without a model -----------

# Invented examples, no real project: a sponsor-video brief must reach the video and caption skills and the
# sponsor-link decision; an invoices-page brief must reach the landing and image skills and the rules already stated.
FIXTURE_SKILLS = {
    "promo-videos": "Промо-відео бренду в `videos/` (Remotion): заставка, сюжет. Тексти в кадрі, голос і "
                    "підписи — через скіл humanize-ua. Тригери: «відео», «ролик», «заставка», «Remotion».",

    "humanize-ua": "Олюднення публічних текстів: підписи до відео, описи, копірайт. "
                   "Тригери: «підписи», «текст», «копірайт», «олюднити».",
    "demo-product-landing": "Сторінка товару як лендінг: секції, активи, відео товару. "
                             "Тригери: «активи», «сторінка товару», «лендінг».",
    "demo-images": "Зображення на вітрині: кожен <img> має alt і loading. "
                    "Тригери: «зображення», «картинка», «фото», «alt», «lazy loading».",
    "demo-banner-prompts": "Промпт банера й слайдів каруселі: анатомія сильного кадру. "
                            "Тригери: «банер», «слайд каруселі».",
    "db-migrations": "Міграції схеми: нумерація, оновлення, відкат.",
}
FIXTURE_BRIEFS = {
    "sponsor-video": "Спонсор: зробити промо-відео (ролик 50 с) у videos/ на Remotion — сюжет, заставка, "
                     "голос і підписи українською; партнерське посилання example.com/partner в описі.",
    "invoices": "Invoices: на сторінці рахунків показати активні рахунки людини і дати оплатити. "
                "Список активів беремо з профілю; картки брендів — з картинками бренду й alt.",
}
FIXTURE_RULES = [
    ("demo 2026-01-02 · rule",
     "Партнерське посилання example.com/partner — у кожному відео й описі: QR у кадрі 16:9; "
     "у соцмережах лише нікнейм."),
    ("demo 2026-01-03 · decision",
     "Invoices: на сторінці рахунків показувати ≤8 відкритих; решта — за кнопкою «показати всі»."),
    ("demo 2026-01-04 · rule",
     "Invoices: назви контрагентів перевіряємо; невпевнені — пропускаємо, без вигаданих описів."),
]


def _fixture_rules(brief, project="", limit=MAX_RULES):
    """A stand-in for the knowledge base: the fixture records whose words the brief holds."""
    words = tokens(brief, 4)
    out = [(src, text) for src, text in FIXTURE_RULES if shared(words, tokens(text))]
    return out[:limit]


def selftest():
    import shutil
    import tempfile
    fails, checks = [], 0

    def check(name, ok, detail=""):
        nonlocal checks
        checks += 1
        print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="briefctx-selftest-")
    repo = os.path.join(tmp, "demo")
    for skill, desc in FIXTURE_SKILLS.items():
        d = os.path.join(repo, ".claude", "skills", skill)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {skill}\ndescription: {desc}\n---\n\n# {skill}\n")
    empty = os.path.join(tmp, "no-skills")
    os.makedirs(empty, exist_ok=True)
    try:
        # 1) the partner partner-video brief: promo-videos first, humanize-ua beside it
        video = FIXTURE_BRIEFS["sponsor-video"]
        got = skills(video, repo)
        names = [os.path.basename(os.path.dirname(p)) for p, _, _ in got]
        check("the sponsor-video brief reaches promo-videos", names and names[0] == "promo-videos", str(names))
        check("the sponsor-video brief reaches humanize-ua (the captions rule)", "humanize-ua" in names, str(names))
        check("a brief about video does not reach the migrations skill", "db-migrations" not in names, str(names))
        ctx = context(video, repo, "demo", "code", rules_fn=_fixture_rules)
        check("the sponsor-video brief carries the partner-link decision", "Партнерське посилання example.com/partner" in ctx["text"],
              ctx["text"][:200])
        check("the sponsor-video brief says to read the skills before the start",
              "read these before you start" in ctx["text"] and "promo-videos/SKILL.md" in ctx["text"],
              ctx["text"][:200])
        check("the skills are absolute paths of real files",
              all(p.startswith("/") and os.path.isfile(p) for p, _, _ in got), str(got[:2]))

        # 2) the invoices brief: the demo landing and image skills, and the two rules already stated
        invoices = FIXTURE_BRIEFS["invoices"]
        names = [os.path.basename(os.path.dirname(p)) for p, _, _ in skills(invoices, repo)]
        check("the invoices brief reaches the demo landing skill", "demo-product-landing" in names, str(names))
        check("the invoices brief reaches the demo images skill (картинки)", "demo-images" in names, str(names))
        ctx = context(invoices, repo, "demo", "code", rules_fn=_fixture_rules)
        check("the invoices brief carries «≤8 відкритих»", "≤8 відкритих" in ctx["text"], ctx["text"][:300])
        check("the invoices brief carries «перевіряємо; невпевнені — пропускаємо»",
              "перевіряємо" in ctx["text"] and "пропускаємо" in ctx["text"])

        # 3) the limits: five skills, six rules, twenty-five lines, one line per item
        many = context(FIXTURE_BRIEFS["sponsor-video"] + " " + invoices, repo, "demo", "review",
                       rules_fn=lambda b, p, n: FIXTURE_RULES * 4)
        check("at most five skills reach one brief", len(skills(video + " " + invoices, repo, "review")) <= MAX_SKILLS)
        check("at most 25 lines", len(many["text"].splitlines()) <= MAX_LINES, str(len(many["text"].splitlines())))
        check("at most six rules", many["rules"] <= MAX_RULES, str(many["rules"]))
        skill_lines = [l for l in many["text"].splitlines() if "/SKILL.md" in l]
        check("one line per skill, its why on the same line", all(" — " in l for l in skill_lines), str(skill_lines[:2]))

        # 4) nothing to say → no block: an empty heading is noise
        check("a brief with no match gives no block", block("ок", empty, "demo", rules_fn=lambda b, p, n: []) == "")
        check("a folder with no skills gives no skills", skills(video, empty) == [], str(skills(video, empty)))
        check("no folder gives no skills", skills(video, "") == [])

        # 5) the source: the block holds no other heading, so it sits before «Critic notes» untouched
        check("the block has no heading of its own beyond the two", "Critic" not in ctx["text"])
        check("the block is not produced twice for one brief",
              ctx["text"].count("Owner's rules and decisions") == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nbriefctx selftest: {len(fails)} failed of {checks}" if fails else f"\nbriefctx selftest: ok ({checks})")
    return 1 if fails else 0


def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return sys.exit(selftest())
    if a and a[0] == "show":
        rest = [x for x in a[1:] if not x.startswith("--")]
        role = ""
        if "--role" in a:
            i = a.index("--role")
            role = a[i + 1] if i + 1 < len(a) else ""
            rest = [x for x in rest if x != role]
        folder = os.path.realpath(os.path.expanduser(rest[0])) if rest else ""
        project = rest[1] if len(rest) > 1 else ""
        brief = sys.stdin.read()
        ctx = context(brief, folder, project, role)
        print(ctx["text"] or "(nothing: no rule and no skill bears on this brief)")
        print(f"\n({ctx['rules']} rules · {ctx['skills']} skills)", file=sys.stderr)
        if "--scores" in a:
            seen = {}
            for path, why, score in skills(brief, folder, role, every=True, detail=seen):
                d = seen.get(path) or {}
                print(f"{score:4} {path} named={d.get('named')} declared={d.get('declared')}", file=sys.stderr)
        return 0
    return sys.exit(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
