"""The event queue: every background signal becomes one durable record (shadow mode).

    bin/genius events pending [--json]                   what the coordinator has not acknowledged yet
    bin/genius events log [-n N] [--json]                the folded queue, oldest first
    bin/genius events diff [--minutes N]                 the queue against the lines that `hc send` really sent
    bin/genius events archive [--days N]                 move acknowledged events older than N days out
    bin/genius events ack <id> [--by <consumer>]         one recipient consumed one event
    python3 bin/lib/events.py emit <source> <subject> <transition> [--text …] [--project p] [--to s] [--send "line"]
    python3 -I bin/lib/events.py selftest

The gap it closes (the user's decision of 2026-11-22: «взяти кращі практики firstmate, щоб закрити дірки»):
a background script calls `hc send` and nobody proves that the coordinator processed the line. A lost or
doubled signal stays invisible. The mechanism follows firstmate (github.com/kunchenguid/firstmate @427056c,
MIT): an append-only queue under a lock, a stable id, a dedupe key, and an acknowledgement per recipient.
It is written here from its description; no line of firstmate is copied.

SHADOW MODE: nothing is delivered and no `hc send` call changes. The queue only records, so the record can
be compared with what was really sent before the delivery switches to the queue.

The record: one line of state/events/queue.jsonl, fsynced under a flock.

    {"op": "emit", "id": "000012-4f3a1b2c", "seq": 12, "at": "2026-11-22T15:04:05+02:00", "ts": 1763… ,
     "source": "agents-watch", "subject": "ga-event-queue", "transition": "finished",
     "key": "agents-watch|ga-event-queue|finished", "project": "genius-ai", "text": "…", "to": "genius-ai",
     "send": "[agents-watch] ga-event-queue: …", "data": {}}
    {"op": "bump", "id": "000012-4f3a1b2c", "seq": 13, …}    the same event again: its count grows
    {"op": "ack",  "id": "000012-4f3a1b2c", "seq": 14, …, "by": "genius-ai"}    one recipient consumed it

`id` is the stable name of the record in the queue file, for ever. `seq` grows by one per line, so a lost
line is visible. `key` is the dedupe key: source + subject + transition. An emit of a key whose event is
still unacknowledged and younger than GENIUS_EVENTS_BURST seconds (default 300) is folded into that event
(the burst: one event with a count). A key whose event every recipient acknowledged starts a new event.

`ack` is per recipient and per event id, like firstmate's sequence-bound actor acknowledgement: two sessions
never consume each other's events. The recipient is the constant «coordinator»: an install has one
coordinator. The Herdr terminal name in `to` is information only, never an address (the coordinator's
decision of 2026-11-22). --by or GENIUS_EVENTS_BY names another consumer when one appears.

state/events/sent.jsonl records the `hc send` lines that a source really sent, each with its event id, so
`diff` can prove that no event was lost between the record and the send.

A line that cannot be read (a torn write after a crash) is skipped and counted, never raised: the queue
stays readable whatever a killed writer left behind.

The queue is compacted by `archive` (the ops sweep runs it): an acknowledged event older than two weeks
moves, line for line, to state/events/archive/<date>.jsonl.gz — the same retention as the day log. A `seq`
marker line keeps the highest number ever used, so an id is never given twice.
"""
import fcntl
import gzip
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime

# GENIUS_EVENTS_ROOT points the queue elsewhere: the self-test runs in a temp folder, and the sources of a
# product copy can be tried without their install. Nothing else changes it.
ROOT = os.environ.get("GENIUS_EVENTS_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
DIR = os.path.join(ROOT, "state", "events")
QUEUE = os.path.join(DIR, "queue.jsonl")
SENT = os.path.join(DIR, "sent.jsonl")
MEMO = os.path.join(DIR, "memory.json")
ARCHIVE = os.path.join(DIR, "archive")
LOCK = os.path.join(DIR, "queue.lock")
RECIPIENT = "coordinator"                                     # one coordinator per install
KEEP_DAYS = 14                                                # an acknowledged event leaves the queue after this
BURST = int(os.environ.get("GENIUS_EVENTS_BURST") or 300)     # seconds: the window of one burst
LOCK_WAIT = 15.0


def recipient(by=""):
    """Who consumes an event: the constant «coordinator», unless a caller names another consumer."""
    return by or os.environ.get("GENIUS_EVENTS_BY") or RECIPIENT


# ---- the file ------------------------------------------------------------------------------------------------

def _stamp(at=None):
    """(ISO minute-free stamp, epoch seconds): what every line carries."""
    t = time.time() if at is None else float(at)
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds"), t


@contextmanager
def _lock():
    """One writer at a time: a flock on state/events/queue.lock, one open file per call, so two threads of
    one process exclude each other too. A holder that dies drops its lock with the process."""
    os.makedirs(DIR, exist_ok=True)
    f = open(LOCK, "a+")
    end = time.time() + LOCK_WAIT
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > end:
                    raise TimeoutError(f"state/events/queue.lock is held by another writer for over {LOCK_WAIT} s")
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _append(path, record):
    """Append one line and fsync it: a process killed right after this call leaves the record on disk.

    A torn tail (a writer killed mid-line) is closed first, so the new record never joins it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        if f.tell() > 0:
            try:
                with open(path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    torn = r.read(1) != b"\n"
            except OSError:
                torn = False
            if torn:
                f.write("\n")
        f.write(body)
        f.flush()
        os.fsync(f.fileno())


def _lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _jsonl(path):
    """Every readable record of a file; a torn or foreign line is skipped."""
    out = []
    for raw in _lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def key(source, subject, transition):
    """The dedupe key: one source, one subject, one transition."""
    return f"{source}|{subject}|{transition}"


# ---- the fold ------------------------------------------------------------------------------------------------

def _fold(lines):
    """The queue as it stands: the events by id, in the order they were first emitted."""
    events, order, by_key, seq, bad = {}, [], {}, 0, 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            bad += 1
            continue
        if not isinstance(rec, dict) or not rec.get("op"):
            bad += 1
            continue
        try:
            seq = max(seq, int(rec.get("seq") or 0))
        except (TypeError, ValueError):
            pass
        op, eid = rec["op"], rec.get("id")
        if op == "seq":
            continue                                  # the marker of the highest seq after a compaction
        if op == "emit" and eid:
            ev = {"id": eid, "seq": rec.get("seq"), "at": rec.get("at"), "ts": rec.get("ts") or 0,
                  "source": rec.get("source", ""), "subject": rec.get("subject", ""),
                  "transition": rec.get("transition", ""), "key": rec.get("key", ""),
                  "project": rec.get("project", ""), "text": rec.get("text", ""),
                  "to": rec.get("to", ""), "send": rec.get("send", ""), "data": rec.get("data") or {},
                  "count": int(rec.get("count") or 1), "first": rec.get("at"), "last": rec.get("at"),
                  "last_ts": rec.get("ts") or 0, "acks": {}}
            events[eid] = ev
            order.append(eid)
            by_key[ev["key"]] = eid
        elif op == "bump" and eid in events:
            ev = events[eid]
            ev["count"] += int(rec.get("n") or 1)
            ev["last"] = rec.get("at") or ev["last"]
            ev["last_ts"] = rec.get("ts") or ev["last_ts"]
        elif op == "ack" and eid in events:
            events[eid]["acks"][rec.get("by") or "?"] = rec.get("at")
        else:
            bad += 1                                  # an orphan bump or ack, or an unknown op
    return {"seq": seq, "bad": bad, "events": events, "order": order, "by_key": by_key}


def folded():
    """The queue of this install, as the fold gives it (for a reader that prints or judges)."""
    return _fold(_lines(QUEUE))


# ---- write ---------------------------------------------------------------------------------------------------

def emit(source, subject, transition, text="", project="", to="", send="", data=None, at=None):
    """Append one event, or one bump of the event of the same key still inside its burst. Returns its id."""
    stamp, ts = _stamp(at)
    k = key(source, subject, transition)
    with _lock():
        q = _fold(_lines(QUEUE))
        prev = q["events"].get(q["by_key"].get(k, ""))
        seq = q["seq"] + 1
        if prev and not prev["acks"] and ts - float(prev.get("last_ts") or 0) <= BURST:
            _append(QUEUE, {"op": "bump", "id": prev["id"], "seq": seq, "at": stamp, "ts": ts, "n": 1})
            return prev["id"]
        eid = f"{seq:06d}-{hashlib.sha1(k.encode()).hexdigest()[:8]}"
        _append(QUEUE, {"op": "emit", "id": eid, "seq": seq, "at": stamp, "ts": ts, "source": source,
                        "subject": subject, "transition": transition, "key": k, "project": project,
                        "text": text, "to": to, "send": send, "data": data or {}})
        return eid


def shadow(source, subject, transition, **kw):
    """`emit` for a source of a product path in shadow mode: a failure is never the failure of that path.

    The watcher, the sweep and the status check keep working when the queue cannot be written; the one line
    on stderr is the only trace. This is the way every source calls the queue until delivery switches."""
    try:
        return emit(source, subject, transition, **kw)
    except Exception as e:                        # a full disk, a foreign lock, a broken JSON: never fatal here
        print(f"events: {source}|{subject}|{transition} not recorded: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def ack(event_id, by=""):
    """One recipient consumed one event. False when no such event exists (nothing is written then)."""
    by = recipient(by)
    stamp, ts = _stamp()
    with _lock():
        q = _fold(_lines(QUEUE))
        if event_id not in q["events"]:
            return False
        if by in q["events"][event_id]["acks"]:
            return True                               # already acknowledged: no second line
        _append(QUEUE, {"op": "ack", "id": event_id, "seq": q["seq"] + 1, "at": stamp, "ts": ts, "by": by})
        return True


def mark(source, subject, state):
    """Remember the last state of one subject; return the state before it (None the first time).

    A source that must report a change rather than a level (CI failed, a URL is down) reads this before it
    emits; an unchanged state writes nothing."""
    k = f"{source}|{subject}"
    stamp, _ = _stamp()
    with _lock():
        memo = {}
        try:
            with open(MEMO, encoding="utf-8") as f:
                memo = json.load(f) or {}
        except (OSError, ValueError):
            memo = {}
        prev = (memo.get(k) or {}).get("state")
        if prev != state:
            memo[k] = {"state": state, "at": stamp}
            _write_json(MEMO, memo)
    return prev


def archive(days=None, now=None):
    """Move every acknowledged event older than `days` (default KEEP_DAYS) out of the queue.

    Its lines go, in order and unchanged, to state/events/archive/<date>.jsonl.gz, where <date> is the day of
    the last line of that event — the same shape and retention as state/log/archive. A line that cannot be
    read is archived under today: the queue is the only copy, and it is never dropped in silence. A `seq`
    marker keeps the highest number in the remaining file, so an id is never given twice."""
    days = KEEP_DAYS if days is None else days
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    with _lock():
        lines = _lines(QUEUE)
        q = _fold(lines)
        gone = {eid for eid, ev in q["events"].items()
                if ev["acks"] and float(ev.get("last_ts") or 0) < cutoff}
        if not gone:
            return {"archived": 0, "kept": len(q["order"]), "files": []}
        # one event, one day: the day of its last line, so all its lines land in the same archive
        today = datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d")
        day_of = {eid: datetime.fromtimestamp(float(q["events"][eid].get("last_ts") or now)).astimezone()
                  .strftime("%Y-%m-%d") for eid in gone}
        keep, moved = [], []
        for raw in lines:
            s = raw.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except ValueError:
                rec = None
            if not isinstance(rec, dict) or rec.get("op") not in ("emit", "bump", "ack", "seq"):
                moved.append((today, s))              # unreadable or foreign: kept in the archive, never dropped
            elif rec.get("op") == "seq":
                continue                              # the marker below carries the highest seq
            elif rec.get("id") in gone:
                moved.append((day_of[rec["id"]], s))
            else:
                keep.append(rec)
        days_used = {}
        for day, raw in moved:
            days_used.setdefault(day, []).append(raw)
        os.makedirs(ARCHIVE, exist_ok=True)
        for day, raws in sorted(days_used.items()):
            with gzip.open(os.path.join(ARCHIVE, f"{day}.jsonl.gz"), "at", encoding="utf-8") as f:
                f.write("\n".join(raws) + "\n")
        body = [json.dumps({"op": "seq", "seq": q["seq"]})] + [json.dumps(r, ensure_ascii=False) for r in keep]
        tmp = QUEUE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, QUEUE)
    return {"archived": len(moved), "kept": len(keep), "files": sorted(days_used)}


def note_sent(to, text, source="", event=""):
    """Record one `hc send` line that a source really sent, with the event it carries (the other side of
    `diff`). Called right after the send, which it never changes."""
    stamp, ts = _stamp()
    with _lock():
        q = _fold(_lines(SENT))
        _append(SENT, {"op": "sent", "seq": q["seq"] + 1, "at": stamp, "ts": ts, "to": to or "",
                       "text": text or "", "source": source, "event": event or ""})


# ---- read ----------------------------------------------------------------------------------------------------

def pending(by=""):
    """The events the coordinator (or the consumer `by` names) did not acknowledge yet."""
    by = recipient(by)
    q = _fold(_lines(QUEUE))
    return [q["events"][eid] for eid in q["order"] if by not in q["events"][eid]["acks"]]


def diff(minutes=180):
    """The queue against the `hc send` lines of the same window.

    Four groups tell the truth about the shadow run:
    - `matched`: an event and the send it carries, both present.
    - `missing`: a send that names an event the queue does not hold — a lost record (the delivery would have
      shown the coordinator something the queue never knew).
    - `unsent`: an event with a send field and no send line — the coordinator was never told (the gap the
      queue exists to close).
    - `quiet`: an event with no send by design (an operation closed, a URL down): a state, not a message.
    A send with no event id is `informational` (the user's answer went to its agent, a quiet window): the
    queue never claimed it.
    """
    since = time.time() - minutes * 60
    q = _fold(_lines(QUEUE))
    events = [q["events"][e] for e in q["order"] if float(q["events"][e].get("ts") or 0) >= since]
    sent = [r for r in _jsonl(SENT) if float(r.get("ts") or 0) >= since]

    def h(to, text):
        return hashlib.sha1((to or "").encode() + b"\n" + (text or "").encode()).hexdigest()[:16]

    queued = {h(e["to"], e["send"]): e for e in events if e.get("send")}
    carried = {h(r.get("to"), r.get("text")): r for r in sent if r.get("event")}
    return {"minutes": minutes, "events": len(events), "sent": len(sent),
            "matched": [e for k, e in queued.items() if k in carried],
            "missing": [r for k, r in carried.items() if k not in queued],
            "unsent": [e for k, e in queued.items() if k not in carried],
            "quiet": [e for e in events if not e.get("send")],
            "informational": [r for r in sent if not r.get("event")]}


# ---- print ---------------------------------------------------------------------------------------------------

def line(ev):
    """One event as one line of text: id, time, source, subject, transition, count, who acknowledged it."""
    acked = ",".join(sorted(ev["acks"])) or "—"
    return (f"{ev['id']}  {(ev['at'] or '')[11:16]}  {ev['source']:<12} {ev['subject']:<32} "
            f"{ev['transition']:<11} ×{ev['count']:<3} {acked}")


def main():
    a = sys.argv[1:]
    if a[:1] == ["emit"] and len(a) >= 4:
        o = {}
        i = 4
        while i < len(a):
            if a[i] in ("--text", "--project", "--to", "--send", "--data") and i + 1 < len(a):
                o[a[i][2:]] = a[i + 1]
                i += 2
            else:
                sys.exit(f"unknown option {a[i]}: --text, --project, --to, --send, --data")
        if "data" in o:
            try:
                o["data"] = json.loads(o["data"])
            except ValueError:
                sys.exit("--data takes a JSON object")
        print(emit(a[1], a[2], a[3], **o))
    elif a[:1] == ["pending"]:
        by = recipient(a[a.index("--by") + 1] if "--by" in a else "")
        evs = pending(by)
        if "--json" in a:
            json.dump(evs, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            for ev in evs:
                print(line(ev))
            print(f"{len(evs)} pending for {by}")
    elif a[:1] == ["log"]:
        n = int(a[a.index("-n") + 1]) if "-n" in a else 40
        q = folded()
        evs = [q["events"][e] for e in q["order"]][-n:]
        if "--json" in a:
            json.dump(evs, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            for ev in evs:
                print(line(ev))
            print(f"{len(evs)} of {len(q['order'])} events" + (f", {q['bad']} unreadable line(s)" if q["bad"] else ""))
    elif a[:1] == ["diff"]:
        d = diff(int(a[a.index("--minutes") + 1]) if "--minutes" in a else 180)
        print(f"window {d['minutes']} min · {d['events']} events in the queue · {d['sent']} lines sent by hc send")
        print(f"  matched:       {len(d['matched'])}")
        print(f"  missing:       {len(d['missing'])}  a send whose event the queue does not hold")
        print(f"  unsent:        {len(d['unsent'])}  an event whose send never happened")
        print(f"  quiet:         {len(d['quiet'])}  an event with no send by design (a state, not a message)")
        print(f"  informational: {len(d['informational'])}  a send with no event (the queue never claimed it)")
        for r in d["missing"][:5]:
            print(f"    ! sent, not in the queue: {(r.get('text') or '')[:120]}")
        for e in d["unsent"][:5]:
            print(f"    ! queued, never sent: {e['source']}|{e['subject']}|{e['transition']} {(e.get('send') or '')[:100]}")
        sys.exit(1 if d["missing"] or d["unsent"] else 0)
    elif a[:1] == ["archive"]:
        r = archive(int(a[a.index("--days") + 1]) if "--days" in a else None)
        print(f"archived {r['archived']} line(s) of {r['kept']} kept"
              + (f" → {', '.join(r['files'])}" if r["files"] else ""))
    elif a[:1] == ["ack"] and len(a) >= 2:
        by = a[a.index("--by") + 1] if "--by" in a else ""
        if not ack(a[1], by):
            sys.exit(f"no event «{a[1]}»")
        print(f"ack: {a[1]}")
    elif a[:1] == ["selftest"]:
        sys.exit(selftest())
    else:
        sys.exit(__doc__.strip().split("\n\n")[1])


# ---- self-check ----------------------------------------------------------------------------------------------

def selftest():
    """A temp folder and a fake nothing: no install file is read or written, no Herdr call, no model.

    What it pins: one emit is one event; the same key inside its burst is one event with a count; a different
    transition is a separate event; a recipient acknowledges alone; an unknown id writes nothing; a torn line
    is skipped and counted; a record survives a process killed right after it; two writers lose nothing; a
    change of a subject is told from a level; the two sides of `diff` agree; and the archive keeps what it
    takes out of the queue."""
    import subprocess
    import tempfile
    global ROOT, DIR, QUEUE, SENT, MEMO, LOCK, ARCHIVE
    globals_ = globals()
    keep = {n: globals_[n] for n in ("ROOT", "DIR", "QUEUE", "SENT", "MEMO", "LOCK", "ARCHIVE", "BURST")}
    tmp = tempfile.mkdtemp(prefix="genius-events-")
    ROOT, DIR = tmp, os.path.join(tmp, "state", "events")
    QUEUE, SENT = os.path.join(DIR, "queue.jsonl"), os.path.join(DIR, "sent.jsonl")
    MEMO, LOCK = os.path.join(DIR, "memory.json"), os.path.join(DIR, "queue.lock")
    ARCHIVE = os.path.join(DIR, "archive")
    LIB = os.path.dirname(os.path.realpath(__file__))
    fails = []

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    def child(code):
        return subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {LIB!r})\n" + code],
                              capture_output=True, text=True,
                              env={**os.environ, "GENIUS_EVENTS_ROOT": ROOT, "GENIUS_EVENTS_BURST": "0"})

    try:
        # 1. one emit, one event, one stable id
        a = emit("agents-watch", "agent-a", "finished", text="agent-a finished its turn", project="alpha",
                 to="genius-ai", send="[agents-watch] agent-a: finished")
        evs = pending()
        check("one emit is one pending event", len(evs) == 1 and evs[0]["id"] == a and evs[0]["count"] == 1, evs)
        check("the id names the record in the file", re.fullmatch(r"\d{6}-[0-9a-f]{8}", a) is not None
              and json.loads(_lines(QUEUE)[0])["id"] == a, a)

        # 2. the burst: the same key within the window is one event with a count
        b = emit("agents-watch", "agent-a", "finished", text="agent-a finished its turn")
        evs = pending()
        check("the same key inside the burst is one event with a count",
              b == a and len(evs) == 1 and evs[0]["count"] == 2 and evs[0]["last"] >= evs[0]["first"], evs)

        # 3. a different transition of the same subject is its own event
        c = emit("agents-watch", "agent-a", "hung", text="agent-a has not moved for 15 minutes")
        check("a different transition of the same subject is a separate event",
              c != a and len(pending()) == 2, [e["id"] for e in pending()])

        # 4. the acknowledgement: per recipient, never for an unknown id
        lines_before = len(_lines(QUEUE))
        check("an unknown id is refused", ack("000999-deadbeef") is False)
        check("the refusal writes no line", len(_lines(QUEUE)) == lines_before, _lines(QUEUE)[-1:])
        check("a recipient acknowledges only its own copy",
              ack(a, "genius-ai") and [e["id"] for e in pending("genius-ai")] == [c]
              and len(pending("other-session")) == 2 and len(pending()) == 2,
              {"genius-ai": [e["id"] for e in pending("genius-ai")], "other": len(pending("other-session"))})
        check("a second acknowledgement of the same event writes no line",
              ack(a, "genius-ai") and len(_lines(QUEUE)) == lines_before + 1, len(_lines(QUEUE)))
        check("the recipient is the constant «coordinator»", recipient() == "coordinator", recipient())
        check("an acknowledgement with no name belongs to the coordinator",
              ack(c) and "coordinator" in folded()["events"][c]["acks"], folded()["events"][c]["acks"])

        # 5. a key whose event is acknowledged starts a new event: the same word, a new occurrence
        d = emit("agents-watch", "agent-a", "finished", text="agent-a finished again")
        check("an acknowledged key starts a new event", d != a and d != c, (a, d))
        check("the new event is unacknowledged and counts once",
              next(e for e in pending() if e["id"] == d)["count"] == 1, pending())

        # 6. a torn write is skipped and counted, and the next record is readable
        bad_before = _fold(_lines(QUEUE))["bad"]
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write('{"op": "emit", "id": "torn-line"')          # a writer killed mid-line: no newline, no brace
        e = emit("ops-sweep", "op-1", "closed", text="op-1 closed")
        q = _fold(_lines(QUEUE))
        check("a torn line is skipped, not raised", q["bad"] == bad_before + 1, q["bad"])
        check("the record after a torn line is readable", e in q["events"], e)
        check("the torn line never became an event", "torn-line" not in q["events"], list(q["events"]))

        # 7. a record survives a process killed right after it: the write is fsynced, not buffered
        r = child("import os, signal, events\n"
                  "events.emit('status', 'site-x', 'down', text='site-x is down')\n"
                  "os.kill(os.getpid(), signal.SIGKILL)\n")
        check("a record survives a process killed right after it",
              any(x["subject"] == "site-x" for x in pending()), (r.returncode, r.stderr[-200:]))

        # 8. two writers at once lose nothing and tear nothing
        writer = ("import os, events\n"
                  "for i in range(20):\n"
                  "    events.emit('bulk', f'w{os.getpid()}-{i}', 'x', text='t')\n")
        ps = [subprocess.Popen([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {LIB!r})\n" + writer],
                               env={**os.environ, "GENIUS_EVENTS_ROOT": ROOT, "GENIUS_EVENTS_BURST": "0"},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        errs = [p.communicate()[1] for p in ps]
        q = _fold(_lines(QUEUE))
        bulk = [q["events"][e] for e in q["order"] if q["events"][e]["source"] == "bulk"]
        seqs = [r.get("seq") for r in _jsonl(QUEUE) if r.get("seq")]
        check("two writers at once lose nothing", len(bulk) == 40, (len(bulk), errs))
        check("every event of the two writers has its own id", len({e["id"] for e in bulk}) == 40, len({e["id"] for e in bulk}))
        check("every line has its own seq", len(seqs) == len(set(seqs)), (len(seqs), len(set(seqs))))
        check("no torn line after the two writers", q["bad"] == bad_before + 1, q["bad"])

        # 9. a level is told from a change
        check("the first sighting of a subject has no state before it", mark("status", "alpha/ci/deploy", "ok") is None)
        check("the same state changes nothing", mark("status", "alpha/ci/deploy", "ok") == "ok")
        check("a change returns the state before it", mark("status", "alpha/ci/deploy", "down") == "ok")
        with open(MEMO, encoding="utf-8") as f:
            memo = json.load(f)
        check("the memory of the subject is kept", memo["status|alpha/ci/deploy"]["state"] == "down", memo)

        # 10. the two sides of diff: a tracked send matches, an untracked one is informational
        note_sent("genius-ai", "[agents-watch] agent-a: finished", "agents-watch", a)
        d1 = diff(180)
        check("a sent line and its event match", len(d1["matched"]) == 1 and not d1["missing"] and not d1["unsent"], d1)
        note_sent("genius-ai", "[agents-watch] agent-a: sent your answer", "agents-watch")
        d2 = diff(180)
        check("a send with no event is informational, not a loss",
              len(d2["informational"]) == 1 and not d2["missing"] and not d2["unsent"], d2)
        check("an event without a send is quiet, not unsent",
              any(e["source"] == "ops-sweep" for e in d2["quiet"]) and not d2["unsent"], d2["unsent"])
        # a send that names an event the queue does not hold: the lost-record direction
        note_sent("genius-ai", "[agents-watch] ghost: finished", "agents-watch", "000999-ghost")
        d3 = diff(180)
        check("a send whose event the queue lost is reported", len(d3["missing"]) == 1, d3["missing"])
        # an event whose send never happened: the never-told direction
        f = emit("agents-watch", "agent-f", "interrupted", text="agent-f was interrupted",
                 to="genius-ai", send="[agents-watch] agent-f: interrupted")
        d4 = diff(180)
        check("an event whose send never happened is reported", [e["id"] for e in d4["unsent"]] == [f], d4["unsent"])

        # 11. the printed line of an event carries what a reader needs
        text = line(folded()["events"][a])
        check("one event prints as one line",
              a in text and "agents-watch" in text and "finished" in text and "genius-ai" in text, text)

        # 12. the archive: an acknowledged event older than two weeks leaves the queue, line for line
        old_at = time.time() - 20 * 86400
        old = emit("status", "old-site", "down", text="old-site down", at=old_at)
        mid = emit("status", "new-site", "down", text="new-site down")
        ack(old)
        r = archive()
        day = datetime.fromtimestamp(old_at).astimezone().strftime("%Y-%m-%d")
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        with gzip.open(os.path.join(ARCHIVE, f"{day}.jsonl.gz"), "rt", encoding="utf-8") as f:
            body = f.read()
        check("an acknowledged event older than two weeks leaves the queue",
              old not in folded()["events"] and mid in folded()["events"], list(folded()["events"]))
        check("its lines land in the archive of its own day",
              r["archived"] == 3 and r["files"] == sorted({day, today}), r)
        check("the archived event keeps its own lines", old in body and body.count("\n") == 2, body)
        with gzip.open(os.path.join(ARCHIVE, f"{today}.jsonl.gz"), "rt", encoding="utf-8") as f:
            stray = f.read()
        check("an unreadable line is archived, never dropped", '"id": "torn-line"' in stray, stray)
        check("an unacknowledged event stays in the queue", archive()["archived"] == 0, folded()["order"])
        check("the seq never goes back after a compaction",
              int(emit("status", "after", "down", text="after").split("-")[0]) > int(mid.split("-")[0]),
              (mid, folded()["seq"]))
    finally:
        globals_.update(keep)
    if fails:
        print(f"\nevents selftest: {len(fails)} failed")
        return 1
    print("\nevents selftest: ok")
    return 0


if __name__ == "__main__":
    main()
