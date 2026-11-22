"""The knowledge base of genius-ai: what the user said and decided, searchable by meaning. Local, no cloud.

    bin/genius know search "<question>" [--project <name>] [-n 5] [--json]
    bin/genius know add "<text>" [--project <name>] [--kind rule|decision|fact] [--source <where from>]
    bin/genius know ingest [<YYYY-MM-DD> | all]   the log of the day → records (the user's own words;
                                                  default: today). Safe to repeat: a record is added once
    bin/genius know digested <date> [--none]      the day is digested: its log is archived and leaves the work list
                                                  (--none: the day gave no durable knowledge)
    bin/genius know stats                         the counts, and whether search by meaning is on
    bin/genius know reembed                       give a vector to the records that have none (after Ollama is installed)
    bin/genius know hooks on | off | status      the hooks in the USER's Claude Code settings (~/.claude/settings.json):
                                                  every session on this computer, in any folder, is logged and
                                                  gets the knowledge. Without it only sessions in this folder do.
    (hook)  UserPromptSubmit   know.py hook       the request is searched here by the script, and what is found
                                                  goes into the model's context with its sources
                                                  The same hook hears the end of the day («зупиняємося, зберігаємо
                                                  прогрес», «завтра продовжимо», «stop for today»): it adds the
                                                  day's log to the base and hands the day's own words of the user
                                                  to the model, to be turned into rules, decisions and facts
    bin/genius know day [<date>]                  the same material by hand: the user's words of a day, and what
                                                  the digest of that day already holds
    (hook)  PostToolUse        know.py step       the same for every step of the model: its latest words and the
                                                  action it took are searched; what is new for this session is added
    (hook)  Stop               know.py stop       the model ends its turn with a question: the base is searched for
                                                  the answer; when something is found the turn goes on with it
    (hook)  Stop               know.py learn      the day's new records are added (in the background)

The user's decision of 2026-11-17: every request of the user becomes knowledge, so that agents ask less
each day. The search is not an instruction that an agent may forget (the user's decision of 2026-11-17:
«запити у БД на рівні інструкцій не працюють; працює, коли кожен запит скриптом стукає у БД»): a hook
searches on every request and puts what it finds into the context. The command line is for a person and
for scripts (the watcher). The model has questions of its own while it works, so every step of the model
is searched too, not only the user's request (the user's decision of 2026-11-17). A record is given to a
session once.

How it searches — hybrid, like a good RAG: a vector answers «about the same thing», the full text answers
«uses this exact word» (an article number, a ticket key). Both scores are summed; the user's own words and
explicit rules weigh more than an assistant's summary.

Embeddings: a multilingual model run by the local Ollama (default bge-m3; Ukrainian and English).
Nothing leaves this computer. The model is part of the data: every record stores which model made its
vector, and a search compares only vectors of the current model (`know reembed` after a model change).
The Ollama server is started when it is not running; the model unloads after a minute of quiet.

Storage: state/knowledge.db (SQLite; personal data, never in the product repo).

Without Ollama, or without the embedding model, nothing breaks: records are stored without vectors, the
search and the hooks work on exact words (word stems), and `know stats` says what to install. Vectors are
added to the stored records later by `know reembed`.
"""
import array
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
DB = os.path.join(ROOT, "state", "knowledge.db")
LOG_DIR = os.path.join(ROOT, "state", "log")
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("GENIUS_EMBED_MODEL", "bge-m3")
KEEP_ALIVE = "300s"           # the model stays loaded while the user talks; unloads after 5 min of quiet
HOOK_MIN = 0.50               # cosine below this is noise (measured: unrelated ≤ 0.40, related ≥ 0.53 with bge-m3):
                              # nothing is better than a wrong hint
HOOK_TOP = 5
STEP_MIN = 0.55               # a step of the model is searched often: a stricter gate, fewer records
STEP_TOP = 3
SEEN_DIR = os.path.join(ROOT, "state", "know-seen")
# The user ends the day (the user's decision of 2026-11-17: these words start the analysis of the day's log).
END_OF_DAY = re.compile(
    r"зупиня[єю]мо?ся|зберіга\w+ прогрес|збережи прогрес|завтра продовж|продовжимо завтра|на сьогодні (все|досить|вистачить)|"
    r"лягаємо спати|фіксуй чекпоінт|кінець дня|stop for today|continue tomorrow|call it a day|end of (the )?day|save (the )?progress",
    re.I)
HOOK_CHARS = 300
ID_LIKE = re.compile(r"^(?=.*\d)[\w-]{3,}$")     # an article number, a ticket key, a version: an exact hit counts alone
WEIGHT = {"rule": 1.25, "decision": 1.2, "answer": 1.2, "ask": 1.1, "fact": 1.0, "reply": 0.85}
MIN_WORDS = 3                 # «так», «ок» alone say nothing without their question


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, at TEXT, project TEXT, kind TEXT, who TEXT, source TEXT,
            text TEXT, context TEXT, model TEXT, vector BLOB, session TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id UNINDEXED, text, context, tokenize='unicode61');
    """)
    if "session" not in {r[1] for r in c.execute("PRAGMA table_info(entries)")}:
        c.execute("ALTER TABLE entries ADD COLUMN session TEXT")
    return c


def stems(text):
    """Word stems (the first 5 letters of words of 5+ letters; id-like words whole): a cheap match that
    survives Ukrainian and English endings. Used when there are no vectors."""
    out = set()
    for w in re.findall(r"[\w-]{3,}", (text or "").lower()):
        if ID_LIKE.match(w):
            out.add(w)
        elif len(w) >= 5:
            out.add(w[:5])
    return out


def close_enough(query, r):
    """Without vectors: the record shares enough stems with the query to be worth showing."""
    q = stems(query)
    if not q:
        return False
    shared = len(q & stems(r["text"] + " " + (r["context"] or "")))
    return shared >= max(2, (len(q) * 2 + 4) // 5)             # at least 2, and about 40 % of the query's stems


def serve():
    """The local Ollama answers, or is started (detached; it holds no model while idle)."""
    import shutil
    if not shutil.which("ollama"):
        return False
    def up():
        try:
            urllib.request.urlopen(OLLAMA + "/api/version", timeout=2).read()
            return True
        except (OSError, urllib.error.URLError):
            return False
    if up():
        return True
    try:
        log = open(os.path.join(ROOT, "state", "ollama.log"), "a")
        subprocess.Popen(["ollama", "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError:
        return False
    for _ in range(20):
        time.sleep(0.5)
        if up():
            return True
    return False


def embed(texts):
    """Vectors for the texts (unit length), or None when the local model cannot answer."""
    if not texts or not serve():
        return None
    body = json.dumps({"model": MODEL, "input": [t[:6000] for t in texts], "keep_alive": KEEP_ALIVE}).encode()
    req = urllib.request.Request(OLLAMA + "/api/embed", data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            vecs = json.load(r).get("embeddings") or []
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if len(vecs) != len(texts):
        return None
    out = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append(array.array("f", (x / n for x in v)))
    return out


def put(c, rec, vec):
    c.execute("INSERT OR REPLACE INTO entries (id, at, project, kind, who, source, text, context, model, vector, session) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (rec["id"], rec["at"], rec.get("project", ""), rec["kind"], rec.get("who", ""), rec.get("source", ""),
               rec["text"], rec.get("context", ""), MODEL if vec is not None else None,
               vec.tobytes() if vec is not None else None, rec.get("session", "")))
    c.execute("DELETE FROM entries_fts WHERE id = ?", (rec["id"],))
    c.execute("INSERT INTO entries_fts (id, text, context) VALUES (?,?,?)", (rec["id"], rec["text"], rec.get("context", "")))


def store(c, recs):
    """Add the records that are not there yet. Returns (added, without a vector)."""
    have = {r["id"]: r["context"] for r in c.execute("SELECT id, context FROM entries WHERE model = ?", (MODEL,))}
    # new, or its context grew (the answer that followed was not written yet at the last run)
    new = [r for r in recs if r["id"] not in have or (r.get("context") or "") != (have[r["id"]] or "")]
    bare = 0
    for i in range(0, len(new), 16):
        chunk = new[i:i + 16]
        vecs = embed([r["text"] + ("\n" + r["context"] if r.get("context") else "") for r in chunk])
        for j, r in enumerate(chunk):
            put(c, r, vecs[j] if vecs else None)
            bare += vecs is None
        c.commit()
    return len(new), bare


def add(text, project, kind, source):
    rec = {"id": hashlib.sha1(text.encode()).hexdigest()[:16], "at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "project": project, "kind": kind, "who": "user", "source": source or "know add", "text": text.strip()}
    n, bare = store(db(), [rec])
    print(("added" if n else "already there") + (" (no vector: the local model did not answer)" if bare else ""))


def add_later(text, project, kind="decision", source=""):
    """Store one record in the background: a caller that must not wait (a panel button, the watcher).
    The record is embedded with the local model, which takes a second or two: never a button's problem."""
    args = [sys.executable, os.path.realpath(__file__), "add", text, "--kind", kind, "--source", source]
    if project:
        args += ["--project", project]
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def from_log(path):
    """The user's own words of one day, each with what it answered and what came of it."""
    recs, rows = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    for i, r in enumerate(rows):
        if r.get("who") != "user":
            continue
        text = re.sub(r"\[Image[^\]]*\]", "", r["text"]).strip()
        before = next((x for x in reversed(rows[:i]) if x["who"] == "assistant" and x["session"] == r["session"]), None)
        after = next((x for x in rows[i + 1:] if x["who"] == "assistant" and x["session"] == r["session"]), None)
        # a short «так» is knowledge only with the question it answers: the end of the reply before it
        asked = before["text"].strip()[-400:] if before else ""
        if len(text.split()) < MIN_WORDS and not asked:
            continue
        context = ""
        if asked:
            context += "after: …" + asked
        if after:
            context += ("\n" if context else "") + "then: " + after["text"].strip()[:500]
        recs.append({"id": "log-" + r["id"], "at": r["at"], "project": r.get("project", ""), "kind": r["kind"], "who": "user",
                     "session": r.get("session", ""),
                     "source": f"log {os.path.basename(path)[:10]} {r['at'][11:16]}", "text": text, "context": context})
    return recs


def ingest(which):
    if which == "all":
        days = sorted(f for f in os.listdir(LOG_DIR) if f.endswith(".jsonl")) if os.path.isdir(LOG_DIR) else []
    else:
        days = [(which or datetime.now().strftime("%Y-%m-%d")) + ".jsonl"]
    c = db()
    for day in days:
        path = os.path.join(LOG_DIR, day)
        if not os.path.exists(path):
            print(f"{day[:10]}: no log")
            continue
        n, bare = store(c, from_log(path))
        print(f"{day[:10]}: {n} new records" + (f", {bare} without a vector (the local model did not answer)" if bare else ""))


def search(query, project, limit, skip_session=""):
    c = db()
    scores, rows = {}, {}
    qv = embed([query])
    if qv:
        q = qv[0]
        for r in c.execute("SELECT * FROM entries WHERE model = ? AND vector IS NOT NULL", (MODEL,)):
            v = array.array("f")
            v.frombytes(r["vector"])
            if len(v) != len(q):
                continue
            rows[r["id"]] = r
            scores[r["id"]] = sum(a * b for a, b in zip(q, v))
    cosine = dict(scores)
    exact = set()                                                   # records that hold an id-like word of the query
    words = [w for w in re.findall(r"[\w-]{2,}", query.lower()) if len(w) >= 4 or ID_LIKE.match(w)]
    if words:
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
        try:
            hits = c.execute("SELECT id, bm25(entries_fts) AS rank FROM entries_fts WHERE entries_fts MATCH ? ORDER BY rank LIMIT 50",
                             (match,)).fetchall()
        except sqlite3.OperationalError:
            hits = []
        if hits:
            best = min(h["rank"] for h in hits) or -1.0             # bm25: lower (more negative) is better
            for h in hits:
                if h["id"] not in rows:
                    r = c.execute("SELECT * FROM entries WHERE id = ?", (h["id"],)).fetchone()
                    if r is None:
                        continue
                    rows[h["id"]] = r
                scores[h["id"]] = scores.get(h["id"], 0.0) + 0.35 * (h["rank"] / best)
        ids = [w for w in words if ID_LIKE.match(w)]
        if ids:
            try:
                exact = {h["id"] for h in c.execute("SELECT id FROM entries_fts WHERE entries_fts MATCH ?",
                                                    (" OR ".join('"' + w.replace('"', "") + '"' for w in ids),))}
            except sqlite3.OperationalError:
                exact = set()
    out = []
    for rid, s in scores.items():
        r = rows[rid]
        if skip_session and r["session"] == skip_session:
            continue                                                # this session's own words are already in its context
        if skip_session and bool(r["project"]) != bool(project):
            continue    # a hook never mixes the monitored projects with work in other folders (another topic, noise)
        s *= WEIGHT.get(r["kind"], 1.0)
        if project and r["project"] == project:
            s *= 1.15                                               # this project's knowledge first, the rest still counts
        out.append((s, r, cosine.get(rid, 0.0), rid in exact))
    out.sort(key=lambda x: -x[0])
    return out[:limit], qv is not None


USER_SETTINGS = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")
HOOKS = {"UserPromptSubmit": [("daylog.py", "hook", 5), ("know.py", "hook", 20)],
         "PostToolUse": [("know.py", "step", 10)],
         "PreToolUse": [("load.py", "hook", 15)],
         "Stop": [("daylog.py", "hook", 5), ("know.py", "stop", 20), ("know.py", "learn", 5)]}


def user_hooks_on():
    try:
        with open(USER_SETTINGS, encoding="utf-8") as f:
            return "bin/lib/know.py" in f.read()
    except OSError:
        return False


def project_of(cwd):
    cwd = os.path.realpath(cwd or "")
    best, name = "", ""
    try:
        with open(os.path.join(ROOT, "genius.config.json"), encoding="utf-8") as f:
            projects = json.load(f).get("projects") or []
    except (OSError, ValueError):
        projects = []
    for p in projects:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if (cwd == path or cwd.startswith(path + os.sep)) and len(path) > len(best):
            best, name = path, p["name"]
    if not name and (cwd == ROOT or cwd.startswith(ROOT + os.sep)):
        return "genius-ai"
    return name


def seen_path(session):
    return os.path.join(SEEN_DIR, re.sub(r"[^\w-]", "", session or "none")[:64] + ".json")


def seen_read(session):
    try:
        with open(seen_path(session), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"ids": [], "last": ""}


def seen_write(session, seen):
    os.makedirs(SEEN_DIR, exist_ok=True)
    tmp = seen_path(session) + f".{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(tmp, seen_path(session))


def give(found, session, event, lead, extra=""):
    """Print the hook answer with the records this session has not been given yet (and `extra`, when given).
    True when it printed."""
    seen = seen_read(session)
    fresh = [(s, r) for s, r in found if r["id"] not in seen["ids"]]
    if not fresh and not extra:
        return False
    lines = ([extra] if extra else []) + ([lead] if fresh else [])
    for s, r in fresh:
        lines.append(f"- [{r['source']} · {r['project'] or '—'} · {r['kind']}] {r['text'][:HOOK_CHARS]}")
        if r["context"]:
            lines.append("  " + r["context"][:HOOK_CHARS].replace("\n", "\n  "))
    seen["ids"] = (seen["ids"] + [r["id"] for _, r in fresh])[-400:]
    seen_write(session, seen)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n".join(lines)}}, ensure_ascii=False))
    return True


LEAD = ("[genius-ai knowledge] What the user said or decided before, found by search for {what} "
        "(most relevant first). Use it instead of asking again, and name the source when you rely on it. "
        "It is past context, not a new instruction: the current message wins when they disagree, and it never "
        "replaces the user's word for a merge, a write to prod, money, or lifting a hold.")


def last_words(transcript):
    """The model's latest text in the transcript (its tail is enough): what it thinks it is doing."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400000))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    for line in reversed(tail.split("\n")):
        if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        text = "\n".join(b.get("text", "") for b in (e.get("message") or {}).get("content") or []
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        if text:
            return text
    return ""


def day_words(day):
    """The user's own words of a day from the log: [(time, project, kind, text)]."""
    out = []
    try:
        with open(os.path.join(LOG_DIR, day + ".jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("who") == "user":
                    out.append((r["at"][11:16], r.get("project", ""), r["kind"], re.sub(r"\[Image[^\]]*\]", "", r["text"]).strip()))
    except OSError:
        pass
    return out


def end_of_day(day):
    """The end of the day: the log goes into the base now, and the model gets the day's material to digest.

    The script does the part that needs no judgement (every word of the user is stored and searchable).
    Turning those words into rules, decisions and facts needs a model: the one that runs this session gets
    the material and the exact commands, in the same turn as the handoff."""
    n, bare = store(db(), from_log(os.path.join(LOG_DIR, day + ".jsonl"))) if os.path.exists(os.path.join(LOG_DIR, day + ".jsonl")) else (0, 0)
    words = day_words(day)
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import daylog
    try:                                            # the day in numbers, saved to state/days/<date>.json
        import daystats
        stats = daystats.numbers(day)
        daystats.save(stats)
        stats_line = daystats.summary(stats)
    except Exception as e:                          # never let the numbers stop the digest
        stats_line = f"(the day's numbers failed: {e.__class__.__name__})"
    earlier = [(d, n) for d, n in daylog.pending() if d != day]
    c = db()
    done = c.execute("SELECT count(*) FROM entries WHERE source LIKE ?", (f"digest {day}%",)).fetchone()[0]
    me = os.path.join(ROOT, "bin", "genius")
    lines = [f"[genius-ai end of day] The user ends the day. The script stored the day's log in the knowledge base "
             f"({len(words)} messages of the user, {n} new records{', ' + str(bare) + ' without a vector' if bare else ''}; "
             f"the digest of {day} holds {done} records so far). Before the handoff, digest the day — this is part of "
             f"the end of the day, not optional:",
             f"1. From the user's words below, write every durable rule, decision and fact as one record each: "
             f'`{me} know add "<one or two self-contained sentences, in the user\'s language>" --project <config name> '
             f'--kind rule|decision|fact --source "digest {day} <HH:MM>"`. A record must make sense alone, months later: '
             f"name the thing it is about. Take only what the user said or approved; never invent; skip one-off commands "
             f"(«check», «go on») unless they show a lasting preference.",
             "2. A record that corrects an older one says so («replaces: …»).",
             f"3. When the records are written: `{me} know digested {day}` (add `--none` when the day gave no durable "
             f"knowledge). The script checks the records, archives the day's log and clears it from the work list.",
             f"4. Then write state/handoff.md as usual, and tell the user how many records the day gave and the day's "
             f"numbers (saved to state/days/{day}.json; `{me} day trend` compares days): {stats_line}",
             *([f"Earlier days still wait for the same digest (their words: `{me} know day <date>`): "
                + ", ".join(f"{d} ({n})" for d, n in earlier)] if earlier else []),
             f"The user's words of {day}:"]
    budget = 7000
    for at, project, kind, text in words:
        item = f"- {at} [{project or '—'}{', answer' if kind == 'answer' else ''}] {text[:400]}"
        if budget - len(item) < 0:
            lines.append(f"… the rest: `{me} know day {day}`")
            break
        budget -= len(item)
        lines.append(item)
    return "\n".join(lines)


def step(scope):
    """PostToolUse: the model's latest words and the action it just took are searched."""
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    if scope == "project" and user_hooks_on():
        return
    session = h.get("session_id", "")
    ti = h.get("tool_input") or {}
    action = " ".join(str(ti.get(k, "")) for k in ("description", "prompt", "query", "pattern", "command") if ti.get(k))[:600]
    query = (last_words(h.get("transcript_path") or "")[-1200:] + "\n" + action).strip()
    if len(query.split()) < MIN_WORDS * 2:
        return
    seen = seen_read(session)
    digest = hashlib.sha1(query.encode()).hexdigest()
    if seen.get("last") == digest:                  # the same thought, another tool call: already searched
        return
    seen["last"] = digest
    seen_write(session, seen)
    found, vectors = search(query, project_of(h.get("cwd", "")), STEP_TOP * 3, skip_session=session)
    found = [(s, r) for s, r, cos, exact in found if (cos >= STEP_MIN if vectors else close_enough(query, r))][:STEP_TOP]
    give(found, session, "PostToolUse", LEAD.format(what="what you are doing now"))


def stop(scope):
    """Stop: the turn ends with a question to the user → the base is searched for the answer."""
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    if (scope == "project" and user_hooks_on()) or h.get("stop_hook_active"):
        return                                      # one search per stop: never a loop
    said = h.get("last_assistant_message") or last_words(h.get("transcript_path") or "")
    asks = [l.strip() for l in said.split("\n") if "?" in l and not l.lstrip().startswith("🔊")]
    if not asks:
        return
    session = h.get("session_id", "")
    asked = "\n".join(asks[-3:])[:1500]
    found, vectors = search(asked, project_of(h.get("cwd", "")), STEP_TOP * 3, skip_session=session)
    found = [(s, r) for s, r, cos, exact in found if (cos >= STEP_MIN if vectors else close_enough(asked, r))][:STEP_TOP]
    give(found, session, "Stop", LEAD.format(what="the question you just asked the user")
         + " If a record below answers your question, go on with the work and say which record you relied on. "
           "If none does, end the turn again with the same question: do not guess.")


def check_workers():
    """A Herdr restart stops the background jobs and no SessionStart hook runs: revive them from here
    (bin/lib/workers.py; it starts one pass at most once per CHECK_EVERY seconds, in its own process)."""
    try:
        here = os.path.dirname(os.path.realpath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import workers
        workers.spawn(ROOT)
    except Exception:
        pass


def hook(scope):
    """UserPromptSubmit: search the request, and hand what is found to the model as context."""
    check_workers()
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    if scope == "project" and user_hooks_on():
        return                                      # the user-level hook already does it: never twice
    prompt = h.get("prompt") or ""
    if isinstance(prompt, list):
        prompt = "\n".join(b.get("text", "") for b in prompt if isinstance(b, dict))
    if re.match(r"\s*(\[genius-ai live control\]|\[agents-watch\]|<hc[ >]|<task-notification)", prompt):
        return
    # the end of the day is heard only in the genius-ai session (the coordinator writes the handoff)
    here = os.path.realpath(h.get("cwd") or "")
    extra = ""
    if END_OF_DAY.search(prompt) and (here == ROOT or here.startswith(ROOT + os.sep)):
        extra = end_of_day(datetime.now().strftime("%Y-%m-%d"))
    if len(prompt.split()) < MIN_WORDS and not extra:
        return
    project = project_of(h.get("cwd", ""))
    found, vectors = search(prompt[:2000], project, HOOK_TOP * 2, skip_session=h.get("session_id", ""))
    found = [(s, r) for s, r, cos, exact in found
             if exact or (cos >= HOOK_MIN if vectors else close_enough(prompt[:2000], r))][:HOOK_TOP]
    give(found, h.get("session_id", ""), "UserPromptSubmit", LEAD.format(what="this request"), extra)


def learn(scope):
    """Stop: today's new log records go into the base, in the background (the hook returns at once)."""
    if scope == "project" and user_hooks_on():
        return
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "ingest"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def user_hooks(action):
    """The same hooks for every Claude Code session of this user. A backup is kept before the first change."""
    try:
        with open(USER_SETTINGS, encoding="utf-8") as f:
            raw = f.read()
        settings = json.loads(raw)
    except OSError:
        raw, settings = None, {}
    except ValueError:
        sys.exit(f"{USER_SETTINGS} is not valid JSON: left as is.")
    if action == "status":
        print("user-level hooks: " + ("on (every session on this computer)" if user_hooks_on() else "off (only sessions in this folder)"))
        return
    hooks = settings.setdefault("hooks", {})
    for event, items in HOOKS.items():
        groups = hooks.setdefault(event, [])
        for g in groups:                                # drop ours, keep the user's own hooks
            g["hooks"] = [x for x in g.get("hooks", []) if "genius-ai-hook" not in x.get("command", "")]
        groups[:] = [g for g in groups if g.get("hooks")]
        if action == "on":
            groups.append({"hooks": [{"type": "command", "timeout": t,
                                      "command": f'/usr/bin/python3 "{os.path.join(ROOT, "bin", "lib", script)}" {arg} user 2>/dev/null || true  # genius-ai-hook'}
                                     for script, arg, t in items]})
        if not groups:
            del hooks[event]
    if not hooks:
        settings.pop("hooks")
    if raw is not None and not os.path.exists(USER_SETTINGS + ".bak-genius-know"):
        with open(USER_SETTINGS + ".bak-genius-know", "w", encoding="utf-8") as f:
            f.write(raw)
    os.makedirs(os.path.dirname(USER_SETTINGS), exist_ok=True)
    with open(USER_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"user-level hooks {'on' if action == 'on' else 'off'}: {USER_SETTINGS} (new sessions)")


def main():
    a = sys.argv[1:]
    if a and a[0] in ("hook", "step", "stop", "learn") and os.environ.get("GENIUS_JUDGE"):
        return                                      # the gate's judge: no knowledge, no log
    if a and a[0] in ("hook", "step", "stop", "learn"):
        scope = a[1] if len(a) > 1 else "project"
        return {"hook": hook, "step": step, "stop": stop, "learn": learn}[a[0]](scope)
    if len(a) == 2 and a[0] == "hooks" and a[1] in ("on", "off", "status"):
        return user_hooks(a[1])

    def opt(name, default=""):
        if name in a:
            i = a.index(name)
            value = a[i + 1] if i + 1 < len(a) else default
            del a[i:i + 2]
            return value
        return default
    project, kind, source, n = opt("--project"), opt("--kind", "fact"), opt("--source"), int(opt("-n", "5"))
    as_json = "--json" in a
    if as_json:
        a.remove("--json")
    if len(a) == 2 and a[0] == "search":
        found, vectors = search(a[1], project, n)
        if as_json:
            print(json.dumps([{"score": round(s, 3), **{k: r[k] for k in ("at", "project", "kind", "who", "source", "text", "context")}}
                              for s, r, _cos, _exact in found], ensure_ascii=False, indent=2))
            return
        if not vectors:
            print("(the local embedding model did not answer: exact words only)")
        for s, r, _cos, _exact in found:
            print(f"{s:.2f} · {r['kind']} · {r['project'] or '—'} · {r['source']}")
            print("   " + r["text"][:400].replace("\n", "\n   "))
        if not found:
            print("nothing found")
    elif len(a) == 2 and a[0] == "add":
        add(a[1], project, kind, source)
    elif a and a[0] == "ingest" and len(a) <= 2:
        ingest(a[1] if len(a) == 2 else "")
    elif a and a[0] == "day" and len(a) <= 2:
        day = a[1] if len(a) == 2 else datetime.now().strftime("%Y-%m-%d")
        for at, proj, kind_, text in day_words(day):
            print(f"- {at} [{proj or '—'}{', answer' if kind_ == 'answer' else ''}] {text}")
        print(f"\ndigest of {day}:")
        for r in db().execute("SELECT kind, project, text, source FROM entries WHERE source LIKE ? ORDER BY source", (f"digest {day}%",)):
            print(f"  {r['kind']} · {r['project'] or '—'} · {r['text'][:200]}")
    elif a and a[0] == "digested" and len(a) in (2, 3):
        day, none = a[1], "--none" in a
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
            sys.exit("usage: bin/genius know digested <YYYY-MM-DD> [--none]")
        c = db()
        path = os.path.join(LOG_DIR, day + ".jsonl")
        if os.path.exists(path):
            store(c, from_log(path))                # the raw words are in the base before the file goes
        n = c.execute("SELECT count(*) FROM entries WHERE source LIKE ?", (f"digest {day}%",)).fetchone()[0]
        if not n and not none:
            sys.exit(f"no record with the source «digest {day} …»: write them with `know add`, or pass --none "
                     "when the day gave no durable knowledge. The log stays.")
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import daylog
        moved = daylog.archive(day, n)
        dropped = daylog.clean()
        print(f"{day}: digested ({n} records), {moved} log lines archived" + (f"; old archives dropped: {', '.join(dropped)}" if dropped else ""))
        left = [f"{d} ({k})" for d, k in daylog.pending()]
        print("still waiting: " + (", ".join(left) or "nothing"))
    elif a == ["reembed"]:
        c = db()
        rows = [dict(r) for r in c.execute("SELECT * FROM entries WHERE vector IS NULL OR model IS NOT ?", (MODEL,))]
        done = 0
        for i in range(0, len(rows), 16):
            chunk = rows[i:i + 16]
            vecs = embed([r["text"] + ("\n" + r["context"] if r.get("context") else "") for r in chunk])
            if not vecs:
                break
            for r, v in zip(chunk, vecs):
                put(c, r, v)
                done += 1
            c.commit()
        print(f"{done} of {len(rows)} records got a vector of {MODEL}" + ("" if done == len(rows) else " — the local model did not answer"))
    elif a == ["stats"]:
        c = db()
        total = c.execute("SELECT count(*) FROM entries").fetchone()[0]
        print(f"{total} records · model {MODEL} · {os.path.relpath(DB, ROOT)}")
        import shutil
        if not shutil.which("ollama"):
            print("  vectors: off — Ollama is not installed. The search works on exact words. For search by meaning: "
                  f"brew install ollama && ollama pull {MODEL} && bin/genius know reembed")
        elif embed(["ok"]) is None:
            print(f"  vectors: off — the model is missing or the server does not answer: ollama pull {MODEL} && bin/genius know reembed")
        else:
            print("  vectors: on (local, nothing leaves this computer)")
        for r in c.execute("SELECT project, kind, count(*) AS n, sum(vector IS NULL) AS bare FROM entries GROUP BY 1, 2 ORDER BY 1, 2"):
            print(f"  {r['project'] or '—'} · {r['kind']}: {r['n']}" + (f" ({r['bare']} without a vector)" if r["bare"] else ""))
    else:
        sys.exit('usage: bin/genius know search "<question>" [--project p] [-n 5] [--json] | add "<text>" [--project p] '
                 "[--kind rule|decision|fact] [--source s] | ingest [<date>|all] | day [<date>] | digested <date> [--none] | reembed | stats | hooks on|off|status")


if __name__ == "__main__":
    main()
