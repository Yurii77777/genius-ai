"""The log of the day: every request of the user and every answer, as JSON lines.

    (hook)  UserPromptSubmit, Stop      python3 bin/lib/daylog.py hook   — reads the hook JSON on stdin
    bin/genius log                      today's file and its counts
    bin/genius log status               every day: digested or not, what waits, what is archived
    bin/genius log clean                archive the digested, drop archives older than 14 days (the digest calls it)
    bin/genius log import <transcript>  add the requests and answers of a Claude Code transcript (.jsonl)
                                        to the files of their own days; safe to repeat

The user's decision of 2026-11-17: «кожен мій запит конвертується у знання». The log is the raw
material; at the end of the day it is turned into knowledge (bin/genius know, AGENTS.md «Knowledge»).
The user's own words matter most, so each record says who spoke:

    {"at": "<ISO>", "who": "user" | "assistant" | "system", "kind": "ask" | "answer" | "reply" | "event",
     "session": "<id>", "cwd": "<folder>", "project": "<name>", "text": "…"}

    who=user   kind=ask     the user's message
    who=user   kind=answer  an answer the user gave with a button of the Herdr panel
    who=assistant kind=reply  the final text of the turn
    who=system kind=event   what a script typed into the session (live control, the watcher, Herdr)
    who=agent  kind=brief|reply  a sub-agent's session (GENIUS_AGENT set by bin/genius agent): kept, never taken
                            for the user's words

Files: state/log/<YYYY-MM-DD>.jsonl — personal data, never in the product repo. Nothing is sent anywhere.

The log does not grow, and nobody has to remember which file was processed (the user's decision of
2026-11-17). A live file holds ONLY what is not digested yet. When a day is digested into knowledge
(`bin/genius know digested <date>`), its file moves to state/log/archive/<date>.jsonl.gz and the ledger
state/log/index.json records it; words said later that day start a new live file, digested next time.
So: a live file = work to do; no live file = done. Archives are dropped after KEEP_DAYS days: the
knowledge base keeps what mattered.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
DIR = os.path.join(ROOT, "state", "log")
ARCHIVE = os.path.join(DIR, "archive")
INDEX = os.path.join(DIR, "index.json")
KEEP_DAYS = 14
SYSTEM = re.compile(r"^\s*(\[genius-ai live control\]|\[agents-watch\]|<hc[ >]|<task-notification|<system-reminder|<command-name>|<local-command)")
ANSWER = re.compile(r"^\s*(Відповідь на|Answer to) «")
MAX = 20000


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def project_of(cwd):
    """The config project whose folder holds `cwd`; "genius-ai" for this folder itself."""
    cwd = os.path.realpath(cwd or "")
    best = ""
    for p in read(os.path.join(ROOT, "genius.config.json"), {}).get("projects") or []:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if (cwd == path or cwd.startswith(path + os.sep)) and len(path) > len(best):
            best, name = path, p["name"]
    if best:
        return name
    return "genius-ai" if cwd == ROOT or cwd.startswith(ROOT + os.sep) else ""


def classify(text):
    text = re.sub(r"^\s*<pasted_content[^>]*>\s*", "", text)      # a paste of script notes is still a script note
    if SYSTEM.match(text):
        return "system", "event"
    if ANSWER.match(text):
        return "user", "answer"
    return "user", "ask"


def record(who, kind, text, session="", cwd="", at=None):
    at = at or datetime.now().astimezone()
    text = (text or "").strip()
    if not text:
        return False
    rec = {"at": at.isoformat(timespec="seconds"), "who": who, "kind": kind, "session": session,
           "cwd": cwd, "project": project_of(cwd), "text": text[:MAX]}
    rec["id"] = hashlib.sha1(f"{rec['session']}|{rec['who']}|{rec['text']}".encode()).hexdigest()[:16]
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, at.strftime("%Y-%m-%d") + ".jsonl")
    try:                                            # the same record twice (a repeated import, a retried hook): once
        with open(path, encoding="utf-8") as f:
            if any(f'"id": "{rec["id"]}"' in line for line in f):
                return False
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def texts(message):
    """The plain text of a transcript message: a string, or its text blocks (tool results left out)."""
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def last_reply(hook):
    if isinstance(hook.get("last_assistant_message"), str):
        return hook["last_assistant_message"]
    last = ""
    try:
        with open(hook.get("transcript_path") or "", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "assistant" and texts(e.get("message")).strip():
                    last = texts(e.get("message"))
    except OSError:
        pass
    return last


def hook():
    if os.environ.get("GENIUS_JUDGE"):
        return                                      # the gate's judge (bin/lib/gate.py): not a conversation
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    event, session, cwd = h.get("hook_event_name"), h.get("session_id", ""), h.get("cwd", "")
    agent = bool(os.environ.get("GENIUS_AGENT")) or bool(h.get("agent_id"))   # a sub-agent: its brief is not the user's word
    if event == "UserPromptSubmit":
        who, kind = ("agent", "brief") if agent else classify(h.get("prompt", ""))
        record(who, kind, h.get("prompt", ""), session, cwd)
    elif event == "Stop":
        record("agent" if agent else "assistant", "reply", last_reply(h), session, cwd)


def imported(path):
    """A transcript → records: each user message, and the last assistant text before the next one."""
    added = 0
    pending = None                                  # the assistant's latest text of the running turn
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("isSidechain"):
                continue
            try:
                at = datetime.fromisoformat(str(e.get("timestamp", "")).replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            session, cwd = e.get("sessionId", ""), e.get("cwd", "")
            if e.get("isMeta"):
                # a prompt a script typed (the live-control job): not the user's words, but it starts a new
                # turn, so the reply before it belongs to the turn before it
                if e.get("type") == "user" and texts(e.get("message")).strip():
                    if pending:
                        added += record("assistant", "reply", *pending[:3], at=pending[3])
                        pending = None
                    added += record("system", "event", texts(e.get("message")), session, cwd, at=at)
                continue
            att = e.get("attachment") or {}
            if e.get("type") == "attachment" and att.get("type") == "queued_command" and att.get("prompt"):
                # a message the user sent in the middle of a turn: the turn goes on, the words still count
                human = (att.get("origin") or {}).get("kind") == "human"
                said = texts({"content": att["prompt"]})          # a string, or blocks when it carries an image
                who, kind = classify(said) if human else ("system", "event")
                added += record(who, kind, said, session, cwd, at=at)
                continue
            if e.get("type") == "assistant":
                t = texts(e.get("message")).strip()
                if t:
                    pending = (t, session, cwd, at)
            elif e.get("type") == "user":
                t = texts(e.get("message")).strip()
                if not t:
                    continue                        # a tool result: the turn goes on
                if pending:
                    added += record("assistant", "reply", *pending[:3], at=pending[3])
                    pending = None
                who, kind = classify(t)
                added += record(who, kind, t, session, cwd, at=at)
    if pending:
        added += record("assistant", "reply", *pending[:3], at=pending[3])
    return added


def user_count(path):
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                n += '"who": "user"' in line
    except OSError:
        pass
    return n


def pending():
    """The days whose live file still holds words of the user: [(day, count)], oldest first."""
    out = []
    for name in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
        if re.fullmatch(r"\d{4}-\d\d-\d\d\.jsonl", name):
            n = user_count(os.path.join(DIR, name))
            if n:
                out.append((name[:10], n))
    return out


def archive(day, records):
    """A digested day: its live file joins the day's archive, the ledger records it."""
    import gzip
    path = os.path.join(DIR, day + ".jsonl")
    index = read(INDEX, {})
    entry = index.setdefault(day, {"digests": []})
    moved = 0
    if os.path.exists(path):
        os.makedirs(ARCHIVE, exist_ok=True)
        with open(path, "rb") as src, gzip.open(os.path.join(ARCHIVE, day + ".jsonl.gz"), "ab") as dst:
            data = src.read()
            dst.write(data)
            moved = data.count(b"\n")
        os.remove(path)
    entry["digests"].append({"at": datetime.now().astimezone().isoformat(timespec="minutes"), "lines": moved, "records": records})
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return moved


def clean():
    """Drop the archives older than KEEP_DAYS. Returns the days dropped."""
    dropped = []
    today = datetime.now().date()
    for name in sorted(os.listdir(ARCHIVE)) if os.path.isdir(ARCHIVE) else []:
        try:
            age = (today - datetime.strptime(name[:10], "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if age > KEEP_DAYS:
            os.remove(os.path.join(ARCHIVE, name))
            dropped.append(name[:10])
    return dropped


def status():
    index = read(INDEX, {})
    live = dict(pending())
    days = sorted(set(index) | set(live))
    if not days:
        print("no log yet")
    for day in days:
        d = index.get(day, {}).get("digests", [])
        parts = []
        if d:
            parts.append(f"digested {len(d)}× (last {d[-1]['at'][11:16]}, {sum(x['records'] for x in d)} records)")
        if day in live:
            parts.append(f"WAITS: {live[day]} messages of the user not digested")
        if os.path.exists(os.path.join(ARCHIVE, day + ".jsonl.gz")):
            parts.append("archived")
        elif d:
            parts.append("archive dropped")
        print(f"{day} · " + " · ".join(parts))


def show():
    path = os.path.join(DIR, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    counts = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                key = f"{r['who']}/{r['kind']}"
                counts[key] = counts.get(key, 0) + 1
    except (OSError, ValueError):
        pass
    print(os.path.relpath(path, ROOT) + ": " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))


def user_hooks_on():
    path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            return "bin/lib/know.py" in f.read()
    except OSError:
        return False


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["hook"]:
        # scope "user" = the hook of ~/.claude/settings.json (bin/genius know hooks on); the folder's own
        # hook steps aside then (a record is written once anyway: same id)
        if (a[1:2] or ["project"])[0] == "user" or not user_hooks_on():
            hook()
    elif len(a) == 2 and a[0] == "import":
        print(f"{imported(a[1])} records added from {a[1]}")
    elif a == ["status"]:
        status()
    elif a == ["clean"]:
        print("dropped archives: " + (", ".join(clean()) or "none"))
    elif not a:
        show()
    else:
        sys.exit("usage: bin/genius log | log status | log clean | log import <transcript.jsonl>   (hook: daylog.py hook)")
