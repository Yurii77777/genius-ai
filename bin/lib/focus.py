"""The focus of the day: one line and a few steps, on top of the Herdr panel («Now»).

    bin/genius focus "<text>" [step …]   set today's focus and, optionally, its steps (replaces the old one)
    bin/genius focus step "<text>"       add a step
    bin/genius focus done <n>            mark step n done (1 = the first); `undo <n>` takes the mark back
    bin/genius focus now <n>             mark step n as the one in work
    bin/genius focus clear               no focus
    bin/genius done "<text>" [--project <name>]   add a finished job to today's «Done» list (state/done-<date>.jsonl)
    bin/genius focus                     show it
    bin/genius focus --json              the focus, its steps, and the day's work from the board: what was closed
                                         today (✅) and what agents work on now (▶) — the panel's «Now»

The coordinator keeps it, not the user (AGENTS.md «Focus of the day»). File: state/focus.json
{"date", "focus", "steps": [{"text", "done", "now"}]}. The panel shows it only on its own date.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
PATH = os.path.join(ROOT, "state", "focus.json")


def read():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write(value):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, PATH)


def show(f):
    if not f.get("focus") and not f.get("steps"):
        print("no focus")
        return
    print(f"{f.get('date', '')} · {f.get('focus', '')}")
    for i, s in enumerate(f.get("steps") or [], 1):
        print(f"  {i}. {'✅' if s.get('done') else '▶' if s.get('now') else '·'} {s['text']}")


def step(f, n):
    steps = f.get("steps") or []
    if not n.isdigit() or not 1 <= int(n) <= len(steps):
        sys.exit(f"no step {n}: there are {len(steps)}")
    return steps[int(n) - 1]


def _load(name):
    try:
        with open(os.path.join(ROOT, "state", name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def _sh(args, env=None):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20, env=env)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _local_day(iso):
    """'2026-11-17T21:30:00Z' (UTC) or a bare date → (local date string, local datetime string)."""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    if d.tzinfo:
        d = d.astimezone()
    return d.date().isoformat(), d.strftime("%Y-%m-%dT%H:%M:%S")


def merged_today(repo, env, today, cache_path=None):
    """[(mergedAt local, number, title)] of the PRs merged today: ONE `gh pr list` per repo, cached 5 min.
    A gh error gives the last cached answer, or []. It never raises."""
    cache_path = cache_path or os.path.join(ROOT, "state", "merged-cache.json")
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(repo) or {}
    prs = hit.get("prs") if hit.get("day") == today else None
    if prs is None or time.time() - hit.get("at", 0) >= 300:
        since = (date.fromisoformat(today) - timedelta(days=1)).isoformat()   # UTC vs local day: filter again below
        out = _sh(["gh", "pr", "list", "--repo", repo, "--state", "merged", "--limit", "100",
                   "--search", f"merged:>={since}", "--json", "number,title,mergedAt"], env)
        try:
            fresh = json.loads(out) if out else None
        except ValueError:
            fresh = None
        if isinstance(fresh, list):
            prs = fresh
            cache[repo] = {"at": time.time(), "day": today, "prs": prs}
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, ensure_ascii=False)
            except OSError:
                pass
    res = []
    for x in prs or []:
        day, at = _local_day(x.get("mergedAt"))
        if day == today and x.get("number"):
            res.append((at, x["number"], str(x.get("title", ""))))
    return res


def _gh_env(repo, services):
    """The token of the account that the config names for this repo (the login is never switched)."""
    for s in services:
        if s.get("type") == "github" and s.get("account") and repo in (s.get("repos") or []):
            token = _sh(["gh", "auth", "token", "--user", s["account"]])
            if token:
                return {**os.environ, "GH_TOKEN": token}
    return None


def combine(closed, manual, merged, tags, today):
    """The day's finished work as strings, OLDEST first (the panel reverses it, so it shows newest first).
    closed: operations closed today; manual: {at, project, text}; merged: [(at, tag, number, title)].
    A closed operation that names a merged PR number («PR #12», «#12») is shown once, as the PR."""
    nums = {str(n) for _, _, n, _ in merged}
    items = []
    for o in closed:
        text = (o.get("what") or o.get("id", ""))
        if any(re.search(rf"(?:PR\s*|#){n}\b", text) for n in nums):
            continue
        items.append((str(o.get("closed", "")), (tags.get(o.get("project"), "") + " " + text).strip()[:90]))
    for m in manual:
        text = str(m.get("text", ""))
        if any(re.search(rf"(?:PR\s*|#){n}\b", text) for n in nums):
            continue                            # the same merge, already listed from gh (a done_auto record)
        items.append((str(m.get("at", "")), (tags.get(m.get("project"), "") + " " + text).strip()[:90]))
    for at, tag, n, title in merged:
        items.append((at, f"{tag} PR #{n} {title}".strip()[:90]))
    items.sort(key=lambda x: x[0])
    return [t for _, t in items if t]


def day_work():
    """The board's own record of the day: what was finished today (operations closed today, merged PRs,
    lines added by `bin/genius done`), and operations an agent works on now."""
    today = date.today().isoformat()
    tags, repos, services = {}, [], []
    try:
        with open(os.path.join(ROOT, "genius.config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        services = cfg.get("services") or []
        for p in cfg.get("projects") or []:
            meta = _load(os.path.join("..", "projects", p["name"], "project.json")) or {}
            tags[p["name"]] = meta.get("tag", "") if isinstance(meta, dict) else ""
            repos.append((p["name"], os.path.expanduser(p.get("path", ""))))
    except (OSError, ValueError, KeyError):
        pass
    closed = [o for o in _load("operations.closed.json") if str(o.get("closed", "")).startswith(today)
              and not str(o.get("id", "")).startswith(("watch-", "interrupted-"))]
    manual = []
    try:
        with open(os.path.join(ROOT, "state", f"done-{today}.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    manual.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    merged = []
    for name, path in repos:
        try:
            url = _sh(["git", "-C", path, "remote", "get-url", "origin"]).removesuffix(".git")
            repo = url.split("github.com")[-1].lstrip(":/") if "github.com" in url else ""
            if repo:
                merged += [(at, tags.get(name, ""), n, t) for at, n, t in merged_today(repo, _gh_env(repo, services), today)]
        except Exception:
            pass                                   # a failed PR lookup never breaks the panel
    done = combine(closed, manual, merged, tags, today)
    work = [{"text": (tags.get(o.get("project"), "") + " " + (o.get("what") or o.get("id", ""))).strip()[:90],
             "agent": o.get("agent", ""), "waiting_for": (o.get("waiting_for") or "")[:300]}
            for o in _load("operations.json") if o.get("agent") and not o.get("paused") and o.get("needs") != "user"]
    return {"done_today": done[-60:], "in_work": work[:8]}


def add_done(text, project=""):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    path = os.path.join(ROOT, "state", f"done-{date.today().isoformat()}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now, "project": project, "text": text}, ensure_ascii=False) + "\n")
    print(f"done: {text}")


def selftest():
    today = "2026-11-17"
    tags = {"a": "🅰 A"}
    # date filtering: a merge at 23:30 on the 16th (local) is not today
    ok = lambda c, m: print(("ok   " if c else "FAIL ") + m) or c
    r = []
    d1, _ = _local_day("2026-11-17")
    r.append(ok(d1 == "2026-11-17", "bare date"))
    r.append(ok(_local_day("garbage") == ("", ""), "bad date → empty"))
    r.append(ok(merged_today("x/y", None, today, cache_path=os.devnull + "/none") is not None, "gh failure never raises"))
    closed = [{"id": "o1", "project": "a", "what": "release PR #12 checked", "closed": "2026-11-17T09:00:00"},
              {"id": "o2", "project": "a", "what": "pack approved", "closed": "2026-11-17T10:00:00"}]
    manual = [{"at": "2026-11-17T12:00:00", "project": "a", "text": "version activated"}]
    merged = [("2026-11-17T11:00:00", "🅰 A", 12, "fix prices"), ("2026-11-17T13:00:00", "🅰 A", 13, "add pages")]
    out = combine(closed, manual, merged, tags, today)
    r.append(ok(out == ["🅰 A pack approved", "🅰 A PR #12 fix prices", "🅰 A version activated", "🅰 A PR #13 add pages"],
                "dedupe by PR number + order by time (oldest first; the panel shows newest first)"))
    r.append(ok(len([x for x in out if "#12" in x]) == 1, "PR #12 once"))
    dup = manual + [{"at": "2026-11-17T12:30:00", "project": "a", "text": "✅ PR #13 злито в develop"}]
    r.append(ok(combine(closed, dup, merged, tags, today)
                == ["🅰 A pack approved", "🅰 A PR #12 fix prices", "🅰 A version activated", "🅰 A PR #13 add pages"],
                "a done record naming a merged PR is not a second line"))
    r.append(ok(combine([], [], [], tags, today) == [], "empty"))
    sys.exit(0 if all(r) else 1)


def main():
    a = sys.argv[1:]
    f = read()
    if a == ["--json"]:
        out = dict(f) if f.get("date") == date.today().isoformat() else {}
        out.update(day_work())
        print(json.dumps(out, ensure_ascii=False))
        return
    if a == ["--selftest"]:
        return selftest()
    if a and a[0] == "add-done":
        text, project = [], ""
        rest = a[1:]
        while rest:
            if rest[0] == "--project" and len(rest) > 1:
                project, rest = rest[1], rest[2:]
            else:
                text.append(rest[0])
                rest = rest[1:]
        if not text:
            sys.exit('usage: bin/genius done "<text>" [--project <name>]')
        return add_done(" ".join(text), project)
    if not a:
        return show(f)
    if a == ["clear"]:
        f = {}
    elif a[0] == "step" and len(a) == 2:
        f.setdefault("date", date.today().isoformat())
        f.setdefault("steps", []).append({"text": a[1], "done": False})
    elif a[0] in ("done", "undo", "now") and len(a) == 2:
        s = step(f, a[1])
        if a[0] == "now":
            for other in f["steps"]:
                other.pop("now", None)
            s["now"] = True
        else:
            s["done"] = a[0] == "done"
            s.pop("now", None)
    else:
        f = {"date": date.today().isoformat(), "focus": a[0], "steps": [{"text": t, "done": False} for t in a[1:]]}
    write(f)
    show(f)


if __name__ == "__main__":
    main()
