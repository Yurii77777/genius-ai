"""«Done today» fills itself: a Stop hook reads the coordinator's final reply and records finished work.

    (hook)  Stop   python3 bin/lib/done_auto.py hook   — reads the hook JSON on stdin
    python3 -I bin/lib/done_auto.py selftest

The user's decision of 2026-11-18, the second time the same day («не оновлюється статус виконаних
задач знову»): the list is a mechanism, not a step that the coordinator can forget (AGENTS.md
«Focus of the day»). The reply is the report of the turn; the finished work is already written there.

A line is finished work when it carries ✅, or when it says that something was merged, published,
deployed, created (a ticket or a PR) or closed, in the user's language or in English. Questions,
proposals and «not yet» lines are left out, and so is the 🔊 spoken line: it is a digest of the same
work. A row of a table is a record only when it also carries a work word («| ✅ сайт | 200 |» is a
status, not a job). The project comes from the tag at the start of the line, or of its block; tags are read from
projects/<name>/project.json and are never written here. A line with no tag belongs to genius-ai:
only the session started in the genius-ai folder is read. A record is dropped when the day already
holds the same text, or a record with the same PR or ticket key.

Record: {"at": "<ISO>", "project": "<name>", "text": "…", "source": "auto"}
The file is state/done-<YYYY-MM-DD>.jsonl — personal data; `bin/genius done` writes the same file
for corrections and for work that the reply does not name.
"""
import json
import os
import re
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

MARK = re.compile(r"✅|✔️?|☑️?")
WORK = re.compile(
    r"\b(?:злит[оа]|злив|злила|змерджено|змерджений|вмерджено|опублікован\w*|опублікував\w*"
    r"|задеплоєн\w*|задеплоїв\w*|закрит[оа]|закрив|закрила"
    r"|створен[оа]\s+(?:тікет|задач\w+|пр|pr|issue)"
    r"|merged|published|deployed|closed"
    r"|created\s+(?:a\s+)?(?:ticket|pr|issue|pull\s+request))\b", re.I)
SKIP = re.compile(
    r"\b(?:ще\s+не|не\s+злит|не\s+задепло|не\s+опублік|не\s+закри|не\s+готов"
    r"|not\s+yet|not\s+merged|not\s+deployed|not\s+published"
    r"|пропону\w*|пропозиц\w*|запропон\w*|можна\s+(?:було\s+)?б|варто|запланован\w*|плану\w*"
    r"|наступн\w*|todo|would|could|plan(?:ned)?|next\s+step)\b", re.I)
FENCE = "```"
CUT = 200


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def tags(root=ROOT):
    """The tags of the configured projects: {name: [tag, tag without emoji]} — longest first."""
    out = {}
    for p in read_json(os.path.join(root, "genius.config.json"), {}).get("projects") or []:
        name = p.get("name")
        if not name:
            continue
        tag = str((read_json(os.path.join(root, "projects", name, "project.json"), {}) or {}).get("tag") or "").strip()
        if not tag:
            continue
        plain = tag.strip("".join(c for c in tag if ord(c) > 0x2100 or c in " "))   # the emoji and its space off
        plain = " ".join(w for w in plain.split() if w)
        forms = [tag] + ([plain] if plain and plain != tag and len(plain) >= 2 else [])
        out[name] = sorted(set(forms), key=len, reverse=True)
    return out


def clean(line):
    s = line.strip()
    s = re.sub(r"^[-*+•]\s+", "", s)
    s = re.sub(r"^#{1,6}\s+", "", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(s.split()).strip()


def match_tag(line, taglist):
    """(project, rest) when the line starts with a project's tag, else None."""
    for name, forms in taglist.items():
        for form in forms:
            if not line.startswith(form):
                continue
            rest = line[len(form):]
            if rest and rest[0].isalnum():
                continue                        # a word that only begins like the tag
            return name, rest.lstrip(" :·—-|>»").strip()
    return None


def items(text, taglist, fallback="genius-ai"):
    """The finished lines of a reply: [(project, text)], in the order of the reply."""
    out, cur, fence = [], fallback, False
    for raw in (text or "").splitlines():
        if raw.strip().startswith(FENCE):
            fence = not fence
            continue
        line = clean(raw)
        if fence or not line:
            continue
        if line.startswith("🔊"):
            continue                            # spoken digest of the same work, not a second record
        hit = match_tag(line, taglist)
        if hit:
            cur, line = hit
            if not line:
                continue
        if "?" in line or SKIP.search(line):
            continue
        work = bool(WORK.search(line))
        if not (MARK.search(line) or work):
            continue
        if line.startswith("|"):
            if not work:
                continue                        # a status table row («| ✅ сайт | 200 |»), not a work list
            line = " ".join(c for c in (x.strip() for x in line.strip("|").split("|")) if c)
        s = clean(MARK.sub("", line))
        if len(s) < 4:
            continue
        out.append((cur, s[:CUT]))
    return out


def key(text):
    t = re.sub(r"[^\w\s#-]+", " ", text.casefold())
    return " ".join(t.split())


def refs(text):
    return set(re.findall(r"#\d{1,6}\b", text)) | set(re.findall(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b", text))


def same(a, b):
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if ka == kb or refs(a) & refs(b):
        return True
    return len(ka) > 20 and len(kb) > 20 and (ka in kb or kb in ka)


def day_file(day, root=ROOT):
    return os.path.join(root, "state", f"done-{day}.jsonl")


def existing(day, root=ROOT):
    """The texts the day already holds."""
    out = []
    try:
        with open(day_file(day, root), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(str(json.loads(line).get("text", "")))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def new_items(text, taglist, day, root=ROOT, at=None):
    """The records this reply adds: one per new finished line, duplicates of the day left out."""
    seen = existing(day, root)
    now = (at or datetime.now()).astimezone().isoformat(timespec="seconds")
    out = []
    for project, line in items(text, taglist):
        if any(same(line, t) for t in seen):
            continue
        seen.append(line)
        out.append({"at": now, "project": project, "text": line, "source": "auto"})
    return out


def append(records, day, root=ROOT):
    if not records:
        return 0
    path = day_file(day, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def last_reply(path):
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                content = (e.get("message") or {}).get("content")
                t = content if isinstance(content, str) else "\n".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
                if t.strip():
                    last = t
    except OSError:
        pass
    return last


def hook():
    """The Stop hook: append what the coordinator's reply reports as finished. Silent on any error."""
    if os.environ.get("GENIUS_AGENT") or os.environ.get("GENIUS_JUDGE"):
        return                                  # a sub-agent's or the judge's reply is not the coordinator's report
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    if h.get("agent_id"):
        return
    if os.path.realpath(h.get("cwd") or "") != os.path.realpath(ROOT):
        return                                  # only the session started in the genius-ai folder
    reply = h.get("last_assistant_message") or last_reply(h.get("transcript_path") or "")
    day = date.today().isoformat()
    append(new_items(reply, tags(), day), day)


def selftest():
    ok = lambda c, m: print(("ok   " if c else "FAIL ") + m) or c
    r = []
    taglist = {"alpha": ["🅰 A", "A"], "beta": ["🅱 B", "B"]}
    reply = "\n".join([
        "🅰 A · release",
        "- ✅ PR #12 злито в develop",
        "опубліковано сторінку цін",
        "",
        "🅱 B: задеплоєно нову версію",
        "A: створено тікет ABC-7",
        "Чи злити PR #13 у main?",
        "Пропоную задеплоїти завтра",
        "Ще не задеплоєно",
        "🔊 Злито реліз, сторінка живе.",
        "```",
        "✅ це вивід команди, не робота",
        "```",
        "✅ зроблено без тега",
    ])
    got = items(reply, taglist)
    texts = [t for _, t in got]

    r.append(ok(texts[0] == "PR #12 злито в develop", "✅ line is recorded"))
    r.append(ok(got[0][0] == "alpha", "block tag gives the project"))
    r.append(ok("опубліковано сторінку цін" in texts and got[1][0] == "alpha",
                "verb line inside a block keeps the block's project"))
    r.append(ok(("beta", "задеплоєно нову версію") in got, "tag at the start of a line wins"))
    r.append(ok(("alpha", "створено тікет ABC-7") in got, "tag without emoji is matched"))
    r.append(ok(not any("?" in t for t in texts), "a question is left out"))
    r.append(ok(not any("Пропоную" in t for t in texts), "a proposal is left out"))
    r.append(ok(not any("Ще не" in t for t in texts), "«not yet» is left out"))
    r.append(ok(not any("реліз" in t for t in texts), "the spoken line is left out"))
    r.append(ok(not any("вивід команди" in t for t in texts), "a code fence is left out"))
    r.append(ok(("alpha", "зроблено без тега") in got, "a line with no tag keeps its block's project"))
    r.append(ok(items("✅ зроблено без блока", taglist) == [("genius-ai", "зроблено без блока")],
                "a reply with no tag belongs to genius-ai"))
    r.append(ok(items("злито PR #1", {"x": ["🅰 X"]}) == [("genius-ai", "злито PR #1")],
                "an unknown tag is not a project"))
    r.append(ok(items("Merged the release", {}) == [("genius-ai", "Merged the release")], "English verb"))
    r.append(ok(not any("сайт" in t for t in texts) and
                items("| ✅ сайт | 200 OK |", taglist) == [], "a status table row is left out"))
    r.append(ok(items("| ✅ | злито PR #77 |", taglist) == [("genius-ai", "злито PR #77")],
                "a work row of a table is kept, its cells joined"))

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "state"))
        day = "2026-11-18"
        a = new_items(reply, taglist, day, root=tmp)
        r.append(ok(append(a, day, root=tmp) == len(a) and len(a) == 5, "five records appended"))
        b = new_items(reply, taglist, day, root=tmp)
        r.append(ok(b == [], "a second run of the same reply adds nothing (dedupe by text)"))
        c = new_items("🅰 A: ✅ PR #12 злито в develop (повтор)", taglist, day, root=tmp)
        r.append(ok(c == [], "dedupe by PR key, whatever the wording"))
        d = new_items("🅰 A: ✅ PR #99 злито в develop", taglist, day, root=tmp)
        r.append(ok(len(d) == 1, "another PR key is new"))

    import time
    big = reply * 10
    t0 = time.perf_counter()
    items(big, taglist)
    ms = (time.perf_counter() - t0) * 1000
    r.append(ok(ms < 200, f"{len(big.splitlines())} lines in {ms:.0f} ms (limit 200)"))
    r.append(ok(tags(os.devnull + "/none") == {}, "no config never raises"))
    sys.exit(0 if all(r) else 1)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["hook"]:
        try:
            hook()
        except Exception:
            pass                                # a hook never breaks the turn
    elif a == ["selftest"]:
        selftest()
    else:
        sys.exit("usage: python3 -I bin/lib/done_auto.py hook | selftest")
