"""An agent's question, answered from the knowledge base when the user has already decided it.

The user's decision of 2026-11-17 («потрібно думати, як мене розвантажити»; the record «Автономія за
прецедентом»): the user does not answer routine dialogs of agents. So the watcher (bin/lib/agents_watch.py)
takes a question of a task agent (label genius.role) — an AskUserQuestion dialog on its screen, or an idle
agent whose last message ends with a question — and answers it here:

1. Rules first, before any model: a merge, a push, a release, a deploy, prod, money and prices, a hold,
   legal, new product text (NEVER below; `selftest` checks the list). Never answered by the watcher.
2. The question is searched in the knowledge base (bin/lib/know.py; the same project's records first) for
   the user's own words. Nothing found → the user, as before.
3. A judge gets the question, the options and the found records, and returns {"covered", "option", "answer",
   "source"}. It may say «covered» only when a record clearly decides this exact question. The judge is the role
   «judge» of bin/lib/judge.py, the same one the gate asks: the local Ollama first, Claude last. A link of the
   agent's own family is skipped, and the judge is off while state/gate-judge.off exists.
4. Covered → the watcher answers: the option is picked in the dialog (the cursor is read, not assumed), or
   the answer is typed into an agent started without prompt suggestions (label genius.nosuggest=1).
   Not covered, the judge silent, any error → the user, exactly as before.

Every answer: a line in the watcher's log (state/agents-watch.log), a record in state/gate.jsonl
(«who»: «watcher-question»), and an item in state/agent-answers.json. The Herdr panel shows the items of
the day («Answered for you») with an «overrule» button; an answer is never given twice to the same question.

    python3 -I bin/lib/agent_ask.py selftest   the deny list, covered, not covered, the judge's silence, once
    python3 -I bin/lib/agent_ask.py demo       a dry run on a fake screen: both paths, no Herdr, no model
    bin/genius agents overrule <id>            the user's own word on an item of the panel
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import judge as judge_role                           # the role «judge»: the chain and the callers

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STATE = os.environ.get("GENIUS_ASK_STATE") or os.path.join(ROOT, "state")
ANSWERS = os.path.join(STATE, "agent-answers.json")
GATE = os.path.join(STATE, "gate.jsonl")
JUDGE_SECONDS = 90
ASK_MIN = 0.55          # cosine below this does not bear on the question (as bin/lib/gate.py)
KEEP = 50                # items kept in state/agent-answers.json

# What the watcher never answers, whatever the base or the judge says: the user's own word (AGENTS.md
# «Models»: «a merge, prod, money and a hold stay the user's word»). One place, checked by `selftest`.
NEVER = [
    (r"\bmerge|\bapprov|злит|злива|мердж|замердж|схвал", "a merge is the user's word"),
    (r"\bpush\b|запуш|\bпуш|залит|залий|залива", "a push is the coordinator's step"),
    (r"\brelease\b|реліз|випуск|версі|\bversion|\bbump\b|changelog|\bтег\b", "a release is the user's word"),
    (r"\bdeploy|деплой|задепло|викоти|виклад|rollback|відкат|\bpublish\b|публік", "a deploy or a publish is the user's word"),
    (r"\bprod\b|production|проді|бойов", "prod is the user's word"),
    (r"\bhold\b|утрим|холд|take nothing new", "a hold is the user's word"),
    (r"цін[аиуо]|цінник|\bprice|pricing|грош|кошт|оплат|тариф|бюджет|\bbudget|invoice|рахунок|вартість|\bcost\b|"
     r"\busd\b|\$|грн|євро", "money and prices are the user's word"),
    (r"legal|юрид|договір|contract|оферт|ліцензі|terms of service|gdpr|cookie|персональн\w* дан", "legal"),
    (r"копірайт|\bcopy\b|copywriting|wording|слоган|headline|заголов|\btitle\b|назв|напис|банер|banner|"
     r"маркетинг|лендінг|landing|новий текст|new text|продуктов", "new product text is the user's word"),
]
OPTION = re.compile(r"^[❯›>]?\s*(\d+)[.)]\s+(\S.*)$")
BORDER = re.compile(r"^[─═━\-=_*·╭╮╰╯┌┐└┘\s]*$")
DIALOG = re.compile(r"Do you want|Would you like to run|trust this folder|Folder access")


def sh(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def stamp():
    return datetime.now().isoformat(timespec="seconds")


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def bare(line):
    """A line of the dialog without its box: «│ ❯ 1. pnpm        │» → «❯ 1. pnpm»."""
    s = re.sub(r"^[│┃║|]\s?", "", line.strip())
    return re.sub(r"\s?[│┃║|]?$", "", s).strip()


def choice(screen):
    """The choice dialog on the screen: {"question", "options", "cursor"} or None.

    A permission dialog, a trust dialog and a plan approval all start with «Yes»/«No»: bin/lib/gate.py
    answers them, never this module. A multi-select dialog (checkboxes) is never answered: Enter there
    would submit a wrong default."""
    lines = screen.rstrip("\n").split("\n")[-30:]
    if DIALOG.search("\n".join(lines)):
        return None
    marks = [(i, int(m.group(1)), m.group(2).strip()) for i, l in enumerate(lines) for m in [OPTION.match(bare(l))] if m]
    run = []
    for start in range(len(marks) - 1, -1, -1):
        if marks[start][1] != 1:
            continue
        run = [marks[start]]
        for m in marks[start + 1:]:
            if m[1] == run[-1][1] + 1 and m[0] - run[-1][0] <= 5:
                run.append(m)
        if len(run) >= 2:
            break
        run = []
    if len(run) < 2:
        return None
    options = [m[2] for m in run]
    if re.match(r"(?i)^(yes|no|allow|deny|trust|approve|proceed|skip|continue)\b", options[0]):
        return None                                  # a permission or a trust dialog: not a question
    for i, _n, _t in run:                            # a checkpoint of a multi-select: the user's
        if any(re.match(r"^[\[(]\s?[xX ]?\s?[\])]|[☐☒☑◻◼]", b) for b in (bare(l) for l in lines[i:i + 3])):
            return None
    first = run[0][0]
    asked, above = [], 0
    for l in reversed(lines[max(0, first - 12):first]):
        b = bare(l)
        if b.startswith("⏺") or (b and BORDER.fullmatch(b)):
            break
        if b:
            asked.insert(0, b)
            above += 1
            if above >= 3:
                break
    question = " ".join(asked).strip()
    if not question:
        return None
    # the cursor is always visible on the highlighted option; a numbered list of the agent's own output has none,
    # so a draft of the user's that starts with «1. » can never be taken for a dialog
    cursor = next((k for k, (i, _n, _t) in enumerate(run) if re.match(r"^[❯›>]", bare(lines[i]))), None)
    if cursor is None:
        return None
    return {"question": question[:600], "options": options[:8], "cursor": cursor}


def question_of(message):
    """A question an idle agent's last message ends with: its last lines, or ''."""
    blocks = [b for b in re.split(r"\n\s*\n", message or "") if b.strip()]
    keep = [b for b in blocks[-2:] if "?" in b and not b.strip().startswith("🔊")]
    if not keep:
        return ""
    lines = [" ".join(l.split()) for l in keep[-1].split("\n") if l.strip()]
    lines = [l for l in lines if not l.startswith("🔊") and not l.startswith("⏺")]
    q = " ".join(lines[-3:])
    return q[:800].strip() if "?" in q else ""


def never(question, options=()):
    """Why the watcher never answers this question, or '' when it may."""
    text = (question + "\n" + "\n".join(options)).lower()
    for pattern, why in NEVER:
        if re.search(pattern, text, re.I):
            return why
    return ""


def qid_of(name, question, options):
    return hashlib.sha1(f"{name}|{question}|{'|'.join(options)}".encode()).hexdigest()[:16]


def items():
    return read(ANSWERS, [])


def labels_of(name, term):
    labels = (term or {}).get("labels") or {}
    if labels:
        return labels
    try:
        return json.loads(sh(["hc", "get", name, "--json"]) or "{}").get("labels") or {}
    except ValueError:
        return {}


def precedent(question, project):
    """The user's own records that may decide this question (this project's first)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import know
        found, vectors = know.search(question, project, 6)
        return [r for _s, r, cos, exact in found
                if r["who"] == "user" and (exact or (cos >= ASK_MIN if vectors else know.close_enough(question, r)))][:4]
    except Exception:
        return []


def judge(question, options, rows, project, labels=None):
    """{"covered", "option", "answer", "source"} from the judge role, or None when it cannot be asked.
    ``labels`` names the agent that asked, so a link of its own family never judges it."""
    prompt = (
        "You answer a question that an unattended coding agent asked its user. Answer ONLY from the user's own "
        "earlier words below — never from your own opinion, never for a decision the user has not made.\n\n"
        "Reply with one JSON object and nothing else:\n"
        '{"covered": true|false, "option": <the number of the option you pick; 0 when there are no options>, '
        '"answer": "<the answer to type, in the user\'s language, one or two sentences>", '
        '"source": "<the source of the record you used>"}\n\n'
        "covered is true ONLY when a record below clearly decides this exact question: the same action, in the "
        "same project, with the same choices. A similar but different question, a general principle, or silence "
        "→ covered false. A product decision — what the product does, its rules, its prices, its texts — needs "
        "a record that decides exactly it; without one → covered false. When in doubt: covered false.\n\n"
        f"Project: {project or '—'}\nQuestion: {question}\n"
        + ("Options:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(options)) + "\n" if options else "")
        + "\nThe user's own earlier words (source · project · kind):\n"
        + "\n".join(f"- [{r['source']} · {r['project'] or '—'} · {r['kind']}] {r['text'][:400]}" for r in rows))
    text, _link, _notes = judge_role.ask(
        prompt, judged=judge_role.judged_of_model((labels or {}).get("genius.model")),
        budget=JUDGE_SECONDS, num_predict=300, temperature=0.2)
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def index_of(option, options):
    """The option the judge picked, 0-based; None when it picked nothing usable."""
    if not options:
        return None
    if isinstance(option, bool):
        return None
    if isinstance(option, int) or (isinstance(option, str) and option.strip().isdigit()):
        i = int(option) - 1
        return i if 0 <= i < len(options) else None
    if isinstance(option, str):
        hit = next((i for i, t in enumerate(options) if t.strip().lower() == option.strip().lower()), None)
        return hit
    return None


def press(name, index, cursor, question):
    """Pick one option: move the dialog's cursor to it and press Enter. False when the dialog stays."""
    keys = (["down"] * (index - cursor) if index > cursor else ["up"] * (cursor - index)) + ["enter"]
    sh(["hc", "keys", name] + keys)
    time.sleep(2)
    left = choice(sh(["hc", "read", name]))
    return not (left and left["question"] == question)


def send_text(name, text):
    sh(["hc", "send", name, text], timeout=60)
    return True


def settle(name, question, options, cursor, project, kind, labels, cwd="",
           find=None, ask=None, keys=None, send=None):
    """One question decided: True when the watcher answered it. `find`, `ask`, `keys` and `send` are
    replaceable, so `selftest` and `demo` run without Herdr, without the knowledge base and without a model."""
    question = " ".join((question or "").split())
    options = list(options or [])
    if not question or not (labels or {}).get("genius.role"):
        return False
    if not options and (labels or {}).get("genius.nosuggest") != "1":
        return False                                 # typed text needs an agent started without suggestions
    why = never(question, options)
    if why:
        print(f"{stamp()} not answered ({name}): {why} — {question[:100]}")
        return False
    qid = qid_of(name, question, options)
    if any(x.get("id") == qid for x in items()):
        print(f"{stamp()} not answered twice ({name}): {question[:100]}")
        return False
    rows = (find or precedent)(question, project)
    if not rows:
        return False
    got = (ask or judge)(question, options, rows, project, labels)
    if not got or not got.get("covered"):
        return False
    index = index_of(got.get("option"), options)
    answer = " ".join(str(got.get("answer") or "").split())[:600]
    if options:
        if index is None or re.match(r"(?i)^(other|інше|другое|something else)", options[index]):
            return False                             # «Other» would need typed text: the user's
        if not (keys or press)(name, index, cursor or 0, question):
            print(f"{stamp()} not answered ({name}): the dialog stayed after the keys — {question[:100]}")
            return False
        answered = options[index]
    else:
        if not answer or (labels or {}).get("genius.nosuggest") != "1":
            return False                             # typed text needs an agent without suggestions
        if not (send or send_text)(name, answer):
            return False
        answered = answer
    sources = [r["source"] for r in rows]
    source = got.get("source") if got.get("source") in sources else sources[0]
    item = {"id": qid, "at": stamp(), "agent": name, "project": project or "", "kind": kind, "cwd": cwd,
            "question": question[:600], "options": options, "answer": answered[:600],
            "option": index + 1 if options else 0, "source": source}
    write(ANSWERS, [x for x in items() if x.get("id") != qid][-KEEP:] + [item])
    gate(item)
    print(f"{stamp()} answered {name} ({project or '—'}): {question[:80]} → {answered[:80]} — {source}")
    return True


def gate(item):
    """The record of the answer in state/gate.jsonl, beside the gate's own decisions."""
    entry = {"at": item["at"], "session": item["agent"], "cwd": item.get("cwd", ""), "tool": "question",
             "input": (item["question"] + (" / " + " | ".join(item["options"]) if item["options"] else ""))[:300],
             "decision": "answered", "via": "watcher/question", "who": "watcher-question",
             "answer": item["answer"][:300], "source": item["source"],
             "why": f"the knowledge base holds the user's decision: {item['source']}"}
    with open(GATE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def dialog(name, term, screen, project):
    """An AskUserQuestion dialog on the agent's screen. True when the watcher answered it."""
    c = choice(screen)
    return bool(c) and settle(name, c["question"], c["options"], c["cursor"], project, "dialog",
                              labels_of(name, term), (term or {}).get("cwd", ""))


def stopped(name, term, message, project):
    """An idle agent whose last message ends with a question. True when the watcher answered it."""
    q = question_of(message)
    return bool(q) and settle(name, q, [], 0, project, "idle", labels_of(name, term), (term or {}).get("cwd", ""))


def remember(name, project, message, said):
    """The user's own answer to an agent's question becomes knowledge (AGENTS.md «Log of the day and
    knowledge»): the question and the answer in one record, kind decision, with the project. In the
    background: the base embeds a record, and the watcher must never wait for it."""
    question = question_of(message)
    said = " ".join((said or "").split())
    if not question or len(said) < 2:
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import know
        know.add_later(f"{question} → {said}", project, "decision",
                       f"agents-watch {name} {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    except Exception:
        pass


def overrule(qid):
    """The user's own word on an item of the panel: the item is marked, the coordinator is told by the panel."""
    all_ = items()
    hit = [x for x in all_ if x.get("id") == qid]
    if not hit:
        sys.exit(f"no answer «{qid}»")
    hit[0]["overruled"] = {"at": stamp(), "by": "owner"}
    write(ANSWERS, all_)
    print(f"overruled: {hit[0]['agent']}: {hit[0]['question'][:80]} — was answered {hit[0]['answer'][:80]}")


SCREEN = """\
⏺ Питання до вас.

╭──────────────────────────────────────────────────────╮
│ Який менеджер пакетів використати?                   │
│                                                      │
│ ❯ 1. pnpm                                            │
│   2. npm                                             │
│   3. Other                                           │
╰──────────────────────────────────────────────────────╯"""


def selftest():
    """The deny list, covered → answered once, not covered → the user, the judge silent → the user,
    a rule before the judge. No Herdr, no knowledge base, no model."""
    import tempfile
    global ANSWERS, GATE
    tmp = tempfile.mkdtemp(prefix="genius-ask-")
    ANSWERS, GATE = os.path.join(tmp, "agent-answers.json"), os.path.join(tmp, "gate.jsonl")
    fails = []

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    must = ["Should I merge PR #12?", "Чи можна запушити гілку?", "Залити PR у develop?", "Чи робити реліз зараз?",
            "Deploy to prod now?", "Скільки коштує підписка?", "Яку ціну поставити на товар?", "Зняти hold з проєкту?",
            "Підписати договір з постачальником?", "Напиши новий заголовок для банера", "Bump the version?"]
    may = ["Which test runner should I use?", "Запускати тести з --runInBand?", "Використовувати pnpm чи npm?",
           "Де лежить конфіг Astro?"]
    for q in must:
        check(f"never answered: {q[:48]}", bool(never(q)), "the deny list said nothing")
    for q in may:
        check(f"may be answered: {q[:48]}", not never(q), f"the deny list said «{never(q)}»")

    c = choice(SCREEN)
    check("a choice dialog is parsed", bool(c) and c["options"] == ["pnpm", "npm", "Other"] and c["cursor"] == 0,
          str(c))
    check("a permission dialog is not a question",
          choice(" Do you want to proceed?\n ❯ 1. Yes\n   2. No\n   3. No, and tell Claude what to do") is None)
    check("a trust dialog is not a question",
          choice(" Do you trust this folder?\n ❯ 1. Trust and continue\n   2. No") is None)
    check("a multi-select dialog is the user's",
          choice("╭──────────╮\n│ Pick files │\n│ │ [ ] a.ts │\n│ │ [x] b.ts │\n╰──────────╯") is None)
    check("a numbered list of the agent is not a dialog",
          choice("⏺ Готово:\n\n1. Встановити pnpm\n2. Запустити тести\n\n❯ 1. do this") is None)
    check("a question of an idle agent is found",
          question_of("Готово. Тести зелені.\n\nХочете, щоб я залив це в чернетку?") ==
          "Хочете, щоб я залив це в чернетку?")
    check("a finished report without a question is not a question", question_of("Готово. Тести зелені.") == "")

    labels = {"genius.role": "code", "genius.nosuggest": "1"}
    rows = [{"source": "log 2026-11-17 19:40", "project": "genius-ai", "kind": "rule", "who": "user",
             "text": "Для тестів використовувати pnpm, не npm."}]
    keys, sent, asked = [], [], []

    def find(q, project):
        return rows

    def judge_yes(q, options, r, project, labels=None):
        asked.append(q)
        return {"covered": True, "option": 1, "answer": "pnpm", "source": "log 2026-11-17 19:40"}

    def judge_no(q, options, r, project, labels=None):
        asked.append(q)
        return {"covered": False, "option": 0, "answer": "", "source": ""}

    def judge_silent(q, options, r, project, labels=None):
        asked.append(q)
        return None

    def keys_ok(name, index, cursor, question):
        keys.append(index)
        return True

    def keys_stuck(name, index, cursor, question):
        keys.append(index)
        return False

    def send_ok(name, text):
        sent.append(text)
        return True

    ok = settle("ch-test", c["question"], c["options"], c["cursor"], "genius-ai", "dialog", labels,
                find=find, ask=judge_yes, keys=keys_ok, send=send_ok)
    check("covered → answered", ok and keys == [0], f"ok={ok} keys={keys}")
    check("covered → an item for the panel", [x["id"] for x in items()] == [qid_of("ch-test", c["question"], c["options"])],
          str(items()))
    check("covered → a record in gate.jsonl", os.path.exists(GATE) and "watcher-question" in open(GATE).read())
    ok = settle("ch-test", c["question"], c["options"], c["cursor"], "genius-ai", "dialog", labels,
                find=find, ask=judge_yes, keys=keys_ok, send=send_ok)
    check("an answer is never given twice", not ok and keys == [0], f"ok={ok} keys={keys}")
    ok = settle("ch-test", "Який менеджер?", ["pnpm"], 0, "genius-ai", "dialog", labels,
                find=find, ask=judge_no, keys=keys_ok, send=send_ok)
    check("not covered → the user", not ok and len(keys) == 1, f"ok={ok}")
    ok = settle("ch2", "Який менеджер?", ["pnpm"], 0, "genius-ai", "dialog", labels,
                find=find, ask=judge_silent, keys=keys_ok, send=send_ok)
    check("the judge silent → the user", not ok and len(keys) == 1, f"ok={ok}")
    asked.clear()
    ok = settle("ch3", "Залити реліз на прод?", [], 0, "genius-ai", "idle", labels,
                find=find, ask=judge_yes, keys=keys_ok, send=send_ok)
    check("a rule before the judge", not ok and not asked, f"ok={ok} judge called {len(asked)}×")
    ok = settle("ch4", "Хочете, щоб я залив це в чернетку?", [], 0, "genius-ai", "idle", labels,
                find=find, ask=judge_yes, keys=keys_ok, send=send_ok)
    check("an idle question → typed", ok and sent == ["pnpm"], f"ok={ok} sent={sent}")
    ok = settle("ch5", "Хочете, щоб я залив це в чернетку?", [], 0, "genius-ai", "idle",
                {"genius.role": "code"}, find=find, ask=judge_yes, keys=keys_ok, send=send_ok)
    check("no typed text without genius.nosuggest", not ok and len(sent) == 1, f"ok={ok}")
    ok = settle("ch6", "Який менеджер?", ["pnpm", "npm"], 0, "genius-ai", "dialog", labels,
                find=find, ask=judge_yes, keys=keys_stuck, send=send_ok)
    check("the keys did not take the dialog → not answered", not ok and len(items()) == 2, f"items={len(items())}")
    ok = settle("ch7", "Який менеджер?", ["pnpm"], 0, "genius-ai", "dialog", {"role": "code"},
                find=find, ask=judge_yes, keys=keys_ok, send=send_ok)
    check("an agent without genius.role is never answered", not ok)
    before, real_ask = len(keys), judge_role.ask
    judge_role.ask = lambda *a, **k: ("", None, ["no judge in the selftest"])
    try:                                             # the judge itself, through settle: silence is no answer
        ok = settle("ch10", "Який менеджер?", ["pnpm"], 0, "genius-ai", "dialog", labels,
                    find=find, ask=None, keys=keys_ok, send=send_ok)
    finally:
        judge_role.ask = real_ask
    check("the judge silent → the user", not ok and len(keys) == before, f"ok={ok}")
    check("a dialog of an agent without genius.role is not answered", not dialog("ch8", {"labels": {"role": "code"}}, SCREEN, ""))
    check("an agent with no question is not asked", not stopped("ch9", {"labels": labels}, "Готово, тести зелені.", ""))
    print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall checks pass")
    return 1 if fails else 0


def demo():
    """A dry run on a fake screen: the two paths of one question, then a merge. No Herdr, no model."""
    import tempfile
    global ANSWERS, GATE
    tmp = tempfile.mkdtemp(prefix="genius-ask-demo-")
    ANSWERS, GATE = os.path.join(tmp, "agent-answers.json"), os.path.join(tmp, "gate.jsonl")
    labels = {"genius.role": "code", "genius.nosuggest": "1"}
    rows = [{"source": "log 2026-11-17 19:40", "project": "genius-ai", "kind": "rule", "who": "user",
             "text": "Для тестів використовувати pnpm, не npm."}]
    c = choice(SCREEN)
    print("screen →", c)
    print("\n1) the base covers it:")
    print("   answered:", settle("demo-agent", c["question"], c["options"], c["cursor"], "genius-ai", "dialog",
                                 labels, find=lambda q, p: rows,
                                 ask=lambda q, o, r, p, l=None: {"covered": True, "option": 1, "answer": "pnpm",
                                                        "source": "log 2026-11-17 19:40"},
                                 keys=lambda n, i, cur, q: True, send=lambda n, t: True))
    print("\n2) nothing in the base decides it (the judge says not covered):")
    print("   answered:", settle("demo-agent", "Який сервер взяти для бази?", ["Postgres", "SQLite"], 0, "genius-ai",
                                 "dialog", labels, find=lambda q, p: rows,
                                 ask=lambda q, o, r, p, l=None: {"covered": False}, keys=lambda n, i, cur, q: True,
                                 send=lambda n, t: True), "→ the user, as before")
    print("\n3) a push or a merge: the rules deny it before the judge:")
    print("   answered:", settle("demo-agent", "Залити PR у develop?", ["Так", "Ні"], 0, "genius-ai", "dialog",
                                 labels, find=lambda q, p: rows,
                                 ask=lambda q, o, r, p, l=None: {"covered": True, "option": 1, "answer": "Так"},
                                 keys=lambda n, i, cur, q: True, send=lambda n, t: True), "→ the user")
    print("\nitems for the panel:", json.dumps(items(), ensure_ascii=False, indent=2))
    print("gate.jsonl:\n" + (open(GATE).read() if os.path.exists(GATE) else "(none)"))


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["selftest"]:
        sys.exit(selftest())
    elif a == ["demo"]:
        demo()
    elif len(a) == 2 and a[0] == "overrule":
        overrule(a[1])
    else:
        sys.exit("usage: python3 -I bin/lib/agent_ask.py selftest | demo | overrule <id>")
