#!/usr/bin/env python3
"""shelf — the coordinator's own shelf: the live state of every project, built by a script.

The lesson of 2026-11-20: a long session forgets what the user already approved, which step waits after a
deploy, which branch never got a pull request. Memory is not a mechanism; a script is.

    bin/lib/shelf.py build [--seen <session>]   the full shelf → state/shelf.json, and the text
    bin/lib/shelf.py hook                       UserPromptSubmit: the shelf only when it changed for this
                                                session (state/shelf-seen/), never longer than 4 s
    bin/lib/shelf.py refresh                    the slow part alone: bin/status --json → a snapshot; the
                                                hook starts it in the background when the snapshot is old
    bin/lib/shelf.py decided "<question>" [--project p]   shelf 6: what the base answers (the same gate
                                                as the stop hook, bin/lib/know.py stop)
    python3 -I bin/lib/shelf.py selftest        fixtures: today's six misses, each on its own shelf

Six shelves:

  1 🔴 broken on prod           the project's own card command (project.json → card.cmd, run by bin/status)
                                and its failed URL checks
  2 ⏭ after-deploy steps        the lines of a PR body (or of the repo's CHANGELOG) that say «after deploy /
                                після деплою / on the host» for a PR merged into a shipping branch in the
                                last 7 days. Listed until an operation covers it, open or closed: a release
                                never ships with its post-deploy step unseen
  3 ✅ approved, not in code    a knowledge-base record of kind «decision» with «затверджено / approved /
                                обрано» from the last 7 days that no operation, open PR or merged PR names.
                                Matching is conservative: two key words of the record (or its only one)
                                must appear as word stems in the operation or PR text; see names()
  4 📤 branches outside a PR    the unpushed data of bin/status (problem_data «unpushed», «worktree_dirty»)
  5 ⏳ waiting on the coordinator  a running task agent (hc ls), an operation without «needs: user» that
                                nobody touched for two hours
  6 🔁 decided before           not a list: the question a turn ends with is searched in the base by the
                                stop hook; `decided` runs the same gate by hand

Personal data lives in state/ only. A paused project is silent. Nothing here runs on a timer.

Why the hook is cheap: bin/status costs ~5 s (git, gh, one curl per service). `build` runs it and keeps the
answer in state/shelf-status.json; the hook reads that file and never waits for the network. A stale
snapshot (older than STATUS_TTL) makes the hook start one `refresh` process in the background — not a
loop, at most once per REFRESH_GAP seconds.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STATE = os.path.join(ROOT, "state")
SHELF = os.path.join(STATE, "shelf.json")
SNAPSHOT = os.path.join(STATE, "shelf-status.json")     # the last `bin/status --json`
PRS = os.path.join(STATE, "shelf-prs.json")             # recent PRs per repo
OPS_SEEN = os.path.join(STATE, "shelf-ops.json")        # when the shelf first saw an operation
SEEN_DIR = os.path.join(STATE, "shelf-seen")            # one hash per session
REFRESH_STAMP = os.path.join(STATE, "shelf-refresh.stamp")

STATUS_TTL = 900            # bin/status is re-run in the background when its answer is older than this
REFRESH_GAP = 120           # never two background refreshes closer than this
PRS_TTL = 600
LOOK_DAYS = 7               # a merged PR, an approved decision: how far back the shelf looks
MAX_LINES = 15
WAIT_HOURS = 2              # an operation nobody touched this long waits on the coordinator
HOOK_SECONDS = 4
HEADER = {
    "uk": "[coordinator shelf] Живий стан проєктів, зібраний скриптом (status о {status}). Дійте за ним. "
          "Призупинені проєкти не показані.",
    "en": "[coordinator shelf] Live state of the projects, built by a script (status at {status}). Act on it. "
          "Paused projects are left out.",
}
ICON = [("prod", "🔴"), ("deploy", "⏭"), ("approved", "✅"), ("branches", "📤"), ("waiting", "⏳")]

# A step that must run after a release, said in the words a PR body or a CHANGELOG uses.
AFTER_DEPLOY = re.compile(
    r"after (the )?(deploy|deployment|release)|post-?deploy|on the host|"
    r"після депло[юя]|після (викату|релізу|розгортан)|на (хості|сервері)|після оновлення на сервері", re.I)
NEGATED = re.compile(r"^\s*(no|none|nothing|немає|нічого)\b", re.I)
# A decision the user made and approved: the words that carry «it is settled».
APPROVED = re.compile(
    r"затверд|погоджен|схвален|обрано|вибрано|вирішено|прийнято|"
    r"approved|chosen|selected|decided|agreed|settled|signed off", re.I)
# Words that every line of a shelf has: they never prove that two things are about the same thing.
NO_KEY = {"after", "deploy", "deployment", "release", "host", "server", "then", "that", "this", "with",
          "from", "there", "which", "should", "must", "після", "деплою", "деплой", "релізу", "хості",
          "сервері", "треба", "потрібно", "тоді", "потім", "який", "яка", "які", "щоб", "буде", "цього",
          "затверджено", "затверджений", "затверджена", "обрано", "вирішено", "погоджено", "рішення",
          "approved", "chosen", "decided", "agreed"}
QUOTED = re.compile(r"[«“\"'‘`]([^»”\"'’`\n]{3,60})[»”\"'’`]")
WORD = re.compile(r"[^\W\d_][\w.:-]{3,}")


def sh(args, timeout=20, env=None):
    try:
        r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write(path, value):
    """An atomic write: a failed build never leaves half a file for the hook to read."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def config():
    return read(os.path.join(ROOT, "genius.config.json"), {})


def clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


# ── the sources ────────────────────────────────────────────────────────────────────────────────────

def load_status(refresh=False):
    """([project state], when) — what `bin/status --json` last answered.

    `refresh` runs it now (~5 s) and keeps the answer; without it the snapshot is used as it is, so the
    hook never waits for git, gh or a URL."""
    snap = read(SNAPSHOT, {})
    if refresh:
        out = sh([sys.executable, "-I", os.path.join(ROOT, "bin", "status"), "--json"], timeout=30)
        try:
            data = json.loads(out) if out else None
        except ValueError:
            data = None
        if isinstance(data, list):
            snap = {"at": time.time(), "data": data}
            write(SNAPSHOT, snap)
    return snap.get("data") or [], snap.get("at") or 0.0


def gh_env(repo):
    """The token of the gh account the config names for this repo (a private repo answers 404 to another).
    The active login is never switched: the token goes to this one call."""
    for s in config().get("services") or []:
        if s.get("type") == "github" and s.get("account") and repo in (s.get("repos") or []):
            token = sh(["gh", "auth", "token", "--user", s["account"]], timeout=10)
            if token:
                return {**os.environ, "GH_TOKEN": token}
    return None


def load_prs(repo, refresh=False):
    """The recent PRs of a repo (open, merged, closed), cached for PRS_TTL. [] when gh did not answer."""
    if not repo:
        return []
    cache = read(PRS, {})
    hit = cache.get(repo) or {}
    if refresh and time.time() - hit.get("at", 0) > PRS_TTL:
        out = sh(["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "40", "--json",
                  "number,title,body,state,mergedAt,baseRefName,headRefName,url"], timeout=25, env=gh_env(repo))
        try:
            prs = json.loads(out) if out else None
        except ValueError:
            prs = None
        if prs is not None:
            hit = {"at": time.time(), "prs": prs}
            cache[repo] = hit
            write(PRS, cache)
    return hit.get("prs") or []


def repo_of(meta, st, path):
    """owner/repo: project.json → its `repo`, else the origin of bin/status's snapshot, else git."""
    repo = meta.get("repo") or ""
    if not repo and (st or {}).get("links", {}).get("repo"):
        repo = (st["links"]["repo"].split("github.com/")[-1] or "").strip("/")
    if not repo:
        url = sh(["git", "-C", path, "remote", "get-url", "origin"], timeout=10).removesuffix(".git")
        repo = url.split("github.com")[-1].lstrip(":/") if "github.com" in url else ""
    return repo


def shipping_branches(meta):
    """The branches whose merge sends code to prod: the project's own release and integration branch,
    and main or master (a PR into main is a release even when project.json says nothing)."""
    branches = meta.get("branches") or {}
    out = {branches.get(k) for k in ("release", "integration")}
    return {b for b in out | {"main", "master"} if b}


def clean_line(line):
    return re.sub(r"\s+", " ", re.sub(r"[*_`]+", "", line)).strip(" -:•—")


def steps_from_text(text):
    """The steps to run after a release, as a PR body or a CHANGELOG writes them.

    A line counts only when the words of a deploy step open it (the first 40 characters): the prose of a
    release note that mentions a deploy somewhere is not a step. A heading («## After the deploy») or a
    line that ends with a colon gives the lines under it; a plain line gives itself, from the marker on.
    «No steps after deploy» is not a step."""
    lines = (text or "").splitlines()
    out = []
    for i, raw in enumerate(lines):
        line = raw.strip().lstrip("-*#•> ").strip()
        m = AFTER_DEPLOY.search(line)
        if len(line) < 8 or not m or m.start() > 40 or NEGATED.match(line):
            continue
        body = clean_line(line[m.start():])
        if len(line) <= 60 and (raw.strip().startswith("#") or line.rstrip().endswith(":")):
            block = []
            for nxt in lines[i + 1:]:
                s = re.sub(r"^[-*•>]\s*|^\d+[.)]\s*", "", nxt.strip())
                if not s or s.startswith("#"):
                    if block:
                        break
                    continue
                block.append(clean_line(s))
                if len(block) == 3 or sum(len(b) for b in block) > 160:
                    break
            body = clean_line(" ".join(block)) or body
        if len(body) >= 8 and body[:60].lower() not in {x[:60].lower() for x in out}:
            out.append(body[:200])
    return out


def changelog_steps(path):
    """The after-deploy lines of the latest release: the top of the repo's CHANGELOG."""
    try:
        with open(os.path.join(path, "CHANGELOG.md"), encoding="utf-8") as f:
            head = "\n".join(f.read(8000).splitlines()[:120])
    except OSError:
        return []
    return steps_from_text(head)[:2]


def after_deploy_items(prs, branches, days=LOOK_DAYS, now=None):
    """[{key, text, url}] — the after-deploy steps of the PRs merged into a shipping branch lately."""
    now = now or time.time()
    out = []
    for pr in prs:
        if pr.get("state") != "MERGED" or pr.get("baseRefName") not in branches:
            continue
        try:
            merged = datetime.fromisoformat(str(pr.get("mergedAt")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if now - merged > days * 86400:
            continue
        for step in steps_from_text(pr.get("body") or ""):
            out.append({"key": f"#{pr['number']}", "text": f"#{pr['number']} ({pr['baseRefName']}): {step}",
                        "step": step, "url": pr.get("url", "")})
    return out


def identifiers(text):
    """The technical names of a line: a ticket key, a version, a CLI command (ABC-12, 2.8.2, staff:migrate-auth)."""
    out = []
    for raw in re.findall(r"\S+", text or ""):
        core = raw.strip(".,:;!?()[]{}<>«»\"'`")
        if len(core) < 4 or core.lower() in NO_KEY:
            continue
        if (any(ch.isdigit() for ch in core) or ":" in core or "_" in core
                or re.search(r"[^\W\d_][-_][^\W\d_]", core)):
            out.append(core)
    return out


def proper_nouns(text):
    """The names a line uses mid-sentence (Inter, Ритуал): not the first word, not a word after a stop."""
    out = []
    for m in re.finditer(r"\S+", text or ""):
        core = m.group(0).strip(".,:;!?()[]{}<>«»\"'`")
        if len(core) < 3 or not core[:1].isupper() or core.lower() in NO_KEY:
            continue
        before = (text or "")[:m.start()].rstrip()
        if not before or before[-1] in ".!?:\n-—•*»\"'(":
            continue                       # the first word of a sentence or of a list item: not a name
        out.append(core)
    return out


def keys(text, n=3):
    """(strong, weak) — what a line is about. Strong: a quoted name, an identifier, a proper noun. Weak:
    the longest plain words, minus the words every shelf line carries (deploy, host, approved, треба …)."""
    strong, weak, seen = [], [], set()
    for group in (QUOTED.findall(text or ""), identifiers(text), proper_nouns(text)):
        for k in group:
            if k and k.lower() not in NO_KEY and k[:5].lower() not in seen:
                seen.add(k[:5].lower())
                strong.append(k)
    for w in WORD.findall(text or ""):
        if w.lower() in NO_KEY or w[:5].lower() in seen:
            continue
        seen.add(w[:5].lower())
        weak.append(w)
    return strong[:n], sorted(weak, key=len, reverse=True)[:n]


def stems(text):
    """The first five letters of every word of five letters or more: «деплою» and «деплой» meet here."""
    return {w[:5] for w in re.findall(r"[^\W\d_][\w.:-]{4,}", (text or "").lower())}


def names(text, haystack, n=3):
    """(found, words) — does `haystack` name what `text` is about?

    Conservative by design: one strong name of the text is enough (a quoted word, a CLI command, a
    proper noun); plain words must be two of three. When in doubt the answer is False, so the item stays
    on the shelf for the coordinator to judge — a missed approval is the miss this shelf exists for."""
    strong, weak = keys(text, n)
    hay, low = stems(haystack), (haystack or "").lower()

    def hit(k):
        k = k.lower().strip(".,;!?()«»\"'`")
        if any(c.isdigit() for c in k) or ":" in k or "_" in k or re.search(r"[^\W\d_][-_][^\W\d_]", k):
            return len(k) >= 5 and k in low       # a command or a version: whole, never cut to five letters
        return k[:5] in hay                       # a name: five letters, so «Ритуал» meets «Ритуал» and «Ритуалом»

    sh = [k for k in strong if hit(k)]
    wh = [k for k in weak if hit(k)]
    if sh:
        return True, sh
    return len(wh) >= (1 if len(weak) <= 1 else 2), wh


def decision_records(days=LOOK_DAYS, now=None):
    """The knowledge base's own decisions of the last `days` days: [{at, project, text, source}]."""
    path = os.path.join(STATE, "knowledge.db")
    if not os.path.exists(path):
        return []
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = c.execute("SELECT at, project, text, source FROM entries WHERE kind = 'decision' "
                         "ORDER BY at DESC LIMIT 400").fetchall()
        c.close()
    except Exception:
        return []
    floor = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [{"at": r[0], "project": r[1] or "", "text": r[2] or "", "source": r[3] or ""}
            for r in rows if str(r[0])[:10] >= floor]


def approved_items(records, haystack_of, days=LOOK_DAYS):
    """The approved decisions that no operation and no PR names: [{project, text, source}]."""
    out = []
    for r in records:
        if not APPROVED.search(r["text"]):
            continue
        found, _ = names(r["text"], haystack_of(r["project"]))
        if not found:
            out.append(r)
    return out


def prod_items(st, service_names):
    """What is broken on prod: the lines of the project's card command, and its failed URL checks."""
    out = [n for n in (st.get("notes") or []) if not n.startswith("🕸")]
    for p in st.get("problems") or []:
        if any(p.startswith(f"{name}:") for name in service_names):
            out.append(p)
    return out


def branch_items(st):
    """The branches and worktrees of bin/status that no PR holds (problem_data)."""
    out = []
    for d in st.get("problem_data") or []:
        if not isinstance(d, dict):
            continue
        if d.get("kind") == "unpushed":
            out += [f"{b['branch']} (+{b.get('commits', '?')})" for b in d.get("branches") or []]
        elif d.get("kind") == "worktree_dirty":
            out += [f"worktree {os.path.basename(w['path'].rstrip('/'))}: {w.get('count', '?')} files"
                    for w in d.get("worktrees") or []]
    return out


def agent_items(projects):
    """{project: [text]} — the task agents that run now in Herdr (label genius.role, or a named sub-agent).

    `hc ls` is a local socket call of a few milliseconds; without Herdr nothing is reported."""
    out = {}
    try:
        data = json.loads(sh(["hc", "ls", "--json"], timeout=3) or "{}")
    except ValueError:
        return out
    for t in data.get("terminals") or []:
        if t.get("state") != "live" or t.get("agent") != "claude-code" or not t.get("name"):
            continue
        labels = t.get("labels") or {}
        if not (labels.get("genius.role") or t.get("parent")):
            continue                            # the coordinator's own session, not a task agent
        out.setdefault(project_of(t.get("cwd") or "", projects), []).append(
            f"{t['name']} ({t.get('status') or 'working'})")
    return out


def project_of(cwd, projects):
    """The project a folder belongs to (its own name when it is none)."""
    best, name = "", ""
    for p in projects:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if cwd == path or cwd.startswith(path + os.sep):
            if len(path) > len(best):
                best, name = path, p["name"]
    return name or "genius-ai"


def stamp_epoch(stamp, end_of_day=False):
    """An ISO date or datetime → epoch. A date alone counts from the END of its day: an operation
    updated today is never called stale, because its hour is not written anywhere."""
    s = str(stamp or "").strip()
    if not s:
        return None
    try:
        if len(s) <= 10:
            d = datetime.fromisoformat(s).replace(hour=23, minute=59, second=59) if end_of_day else datetime.fromisoformat(s)
            return d.timestamp()
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ops_waiting(ops, now=None, path=None):
    """The operations without «needs: user» that nobody touched for WAIT_HOURS: they wait on the coordinator.

    An operation's `updated` is a date, so the shelf keeps its own clock (state/shelf-ops.json: when it
    first saw the operation unchanged). An operation added this morning surfaces two hours later."""
    now = now or time.time()
    path = path or OPS_SEEN
    seen = read(path, {})
    out, keep = [], {}
    for o in ops:
        if o.get("needs") == "user" or o.get("paused") or o.get("queue") == "next" or not o.get("id"):
            continue
        stamp = str(o.get("updated") or o.get("started") or "")
        previous = seen.get(o["id"])
        changed = not isinstance(previous, dict) or previous.get("stamp") != stamp
        first = {"stamp": stamp, "at": now} if changed else previous
        keep[o["id"]] = first
        # a stamp seen before and unchanged since: the shelf's own clock has measured the wait.
        # a new stamp: the written date, read from the end of its day, so today never counts.
        since = (stamp_epoch(stamp, end_of_day=True) or now) if changed else first["at"]
        if now - since >= WAIT_HOURS * 3600:
            out.append(o)
    write(path, keep)
    return out


def pick_decided(text, found, vectors=True, top=3):
    """The records that answer a question — the gate of the stop hook (bin/lib/know.py stop):
    a vector close enough, or, without vectors, enough shared words. `found` is know.search's answer."""
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import know
    out = []
    for _score, row, cos, exact in found:
        if exact or (cos >= know.STEP_MIN if vectors else know.close_enough(text, row)):
            out.append(row)
        if len(out) >= top:
            break
    return out


# ── the build ──────────────────────────────────────────────────────────────────────────────────────

def covered(item_text, key, haystack):
    """Is this step already taken care of? An operation that names the PR (#276) or says what the step
    says covers it, open or closed: the shelf lists a step until its operation closes. Operations only —
    a pull request that touches the same code proves nothing about the step being run on the host."""
    if key and re.search(r"(?<![\w/])" + re.escape(key) + r"(?![\w/])", haystack):
        return True
    found, _ = names(item_text, haystack)
    return found


def collect(refresh=False, now=None):
    """The six shelves as data: {shelf: {project: [text]}} plus the project order and tags."""
    now = now or time.time()
    cfg = config()
    projects = cfg.get("projects") or []
    services = cfg.get("services") or []
    states, status_at = load_status(refresh=refresh)
    by_name = {s["name"]: s for s in states}

    ops = read(os.path.join(STATE, "operations.json"), [])
    closed = read(os.path.join(STATE, "operations.closed.json"), [])
    # An operation covers what it names — open or closed. A closed one is the proof that the work was
    # finished, so its item leaves the shelf for good.
    op_haystack = {}
    for o in ops + closed:
        text = " ".join(str(o.get(k, "")) for k in ("id", "what", "covers", "check", "next", "waiting_for", "ask"))
        op_haystack[o.get("project", "")] = op_haystack.get(o.get("project", ""), "") + "\n" + text
    records = decision_records(now=datetime.fromtimestamp(now))

    shelves = {key: {} for key, _ in ICON}
    tags, order = {}, []
    for p in projects:
        name, path = p["name"], os.path.expanduser(p.get("path", ""))
        st = by_name.get(name) or {}
        meta = read(os.path.join(ROOT, "projects", name, "project.json"), {})
        tags[name] = meta.get("tag") or name
        if st.get("paused"):
            continue                            # the user paused it: not checked, not reported
        order.append(name)
        mine = op_haystack.get(name, "")

        service_names = [s["name"] for s in services if s.get("project") == name and s.get("type") == "url"]
        broken = prod_items(st, service_names)
        if broken:
            shelves["prod"][name] = broken

        prs = load_prs(repo_of(meta, st, path), refresh=refresh)
        steps = after_deploy_items(prs, shipping_branches(meta), now=now) + [
            {"key": "", "text": f"CHANGELOG: {s}"} for s in changelog_steps(path)]
        steps = [x for x in steps if not covered(x.get("step") or x["text"], x["key"], mine)]
        if steps:
            shelves["deploy"][name] = [x["text"] for x in steps]

        named = mine + "\n" + "\n".join(f"{x.get('title', '')} {x.get('body', '')} {x.get('headRefName', '')}"
                                       for x in prs)
        approved = approved_items([r for r in records if r["project"] in ("", name)], lambda _proj, hay=named: hay)
        if approved:
            shelves["approved"][name] = [f"{clip(r['text'], 110)} ({r['source'] or r['at'][:10]})"
                                         for r in approved[:4]]

        branches = branch_items(st)
        if branches:
            shelves["branches"][name] = branches

    for name, items in agent_items(projects).items():
        if name in tags and name not in order:
            continue                            # a paused project stays silent
        shelves["waiting"].setdefault(name, []).extend(items)
        if name not in tags:
            tags[name] = name
            order.append(name)
    for o in ops_waiting(ops, now=now):
        proj = o.get("project") or "genius-ai"
        if proj in tags and proj not in order:
            continue
        shelves["waiting"].setdefault(proj, []).append(f"{o['id']}: {clip(o.get('what', ''), 70)}")
        if proj not in tags:
            tags[proj] = proj
            order.append(proj)

    order += [n for n in tags if n not in order]
    return {"at": now, "status_at": status_at, "shelves": shelves, "tags": tags, "order": order}


def render(built, language="uk"):
    """The compact text: one line per project and shelf, at most MAX_LINES lines."""
    lines = []
    for name in built["order"]:
        tag = built["tags"].get(name, name)
        for key, icon in ICON:
            items = built["shelves"][key].get(name) or []
            if items:
                lines.append(clip(f"{tag} · {icon} " + "; ".join(clip(x, 100) for x in items), 230))
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [f"… +{len(lines) - MAX_LINES + 1}"]
    if not lines:
        return ""
    at = built["status_at"]
    clock = datetime.fromtimestamp(at).strftime("%H:%M") if at else "—"
    return HEADER.get(language, HEADER["en"]).format(status=clock) + "\n" + "\n".join(lines)


def build(seen="", refresh=True, quiet=False):
    """The full shelf: state/shelf.json, the text on stdout, and (with `seen`) the session's mark."""
    built = collect(refresh=refresh)
    text = render(built, config().get("language", "uk"))
    digest = hashlib.sha1(text.encode()).hexdigest()
    write(SHELF, {"at": datetime.fromtimestamp(built["at"]).isoformat(timespec="seconds"),
                  "status_at": built["status_at"], "hash": digest, "text": text,
                  "shelves": built["shelves"]})
    if seen:
        write(seen_path(seen), {"hash": digest, "at": datetime.now().isoformat(timespec="seconds")})
    if not quiet and text:
        print(text)
    return text


def seen_path(session):
    return os.path.join(SEEN_DIR, re.sub(r"[^\w-]", "", session or "none")[:64] + ".json")


def inject(text, session):
    """Print the shelf to this session when it changed since the last time. True when it printed."""
    digest = hashlib.sha1(text.encode()).hexdigest()
    seen = read(seen_path(session), {})
    if seen.get("hash") == digest:
        return False
    write(seen_path(session), {"hash": digest, "at": datetime.now().isoformat(timespec="seconds")})
    if not text:
        return False
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": text}}, ensure_ascii=False))
    return True


def spawn_refresh(status_at):
    """One background `refresh` when the status snapshot is old. Not a loop: at most once per REFRESH_GAP."""
    if time.time() - status_at < STATUS_TTL:
        return
    try:
        if time.time() - os.path.getmtime(REFRESH_STAMP) < REFRESH_GAP:
            return
    except OSError:
        pass
    try:
        os.makedirs(STATE, exist_ok=True)
        open(REFRESH_STAMP, "w").close()
        subprocess.Popen([sys.executable, "-I", os.path.realpath(__file__), "refresh"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def hook():
    """UserPromptSubmit: the shelf, only when it changed for this session, and never longer than 4 s."""
    if os.environ.get("GENIUS_JUDGE"):
        return                                  # the gate's judge: no shelf, no work
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    prompt = h.get("prompt") or ""
    if isinstance(prompt, list):
        prompt = "\n".join(b.get("text", "") for b in prompt if isinstance(b, dict))
    if re.match(r"\s*(\[genius-ai live control\]|\[agents-watch\]|\[coordinator shelf\]|"
                r"<hc[ >]|<task-notification)", prompt or ""):
        return                                  # a turn of a script, not the user's own words

    def stop(signum, frame):
        raise TimeoutError("shelf")

    old = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, HOOK_SECONDS)
    try:
        built = collect(refresh=False)          # cheap sources only: no bin/status, no gh
        text = render(built, config().get("language", "uk"))
        inject(text, h.get("session_id", ""))
        spawn_refresh(built["status_at"])
    except Exception:                           # a shelf never breaks the user's own turn
        return
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def refresh():
    """The slow part alone: bin/status → its snapshot, gh → its cache, and the shelf again from both."""
    build(refresh=True, quiet=True)


def decided(text, project="", session=""):
    """Shelf 6: what the knowledge base answers to a question, by the gate of the stop hook."""
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import know
    found, vectors = know.search(text, project, know.STEP_TOP * 3, skip_session=session)
    return pick_decided(text, found, vectors)


# ── the self-check: today's six misses ─────────────────────────────────────────────────────────────

def selftest():
    """Today's six misses, each on its own shelf. The fixtures are the real words of 2026-11-20."""
    import tempfile

    def ok(cond, what):
        if not cond:
            raise AssertionError(what)

    now = datetime(2026, 11, 20, 14, 0).timestamp()

    # 2 ⏭ the step that #276 left on the host
    body = ("Better Auth, step 3.\n\n**After the deploy, on the host (owner):** `pnpm --filter @repo/api "
            "staff:migrate-auth` (read the counts), then `--apply`.\n\nNo open questions after deploy.\n")
    assert steps_from_text(body) and "staff:migrate-auth" in steps_from_text(body)[0], steps_from_text(body)
    assert steps_from_text("No steps after deploy are needed.") == [], steps_from_text("No steps after deploy are needed.")
    assert steps_from_text("Чіка: злито, нічого не треба") == []
    prs = [{"number": 276, "state": "MERGED", "baseRefName": "develop", "url": "u276",
            "mergedAt": datetime.fromtimestamp(now - 2 * 86400).isoformat(), "body": body},
           {"number": 270, "state": "MERGED", "baseRefName": "develop", "url": "u270",
            "mergedAt": datetime.fromtimestamp(now - 9 * 86400).isoformat(), "body": body},
           {"number": 280, "state": "OPEN", "baseRefName": "develop", "url": "u280",
            "mergedAt": None, "body": body}]
    steps = after_deploy_items(prs, {"develop", "main"}, now=now)
    ok([s["key"] for s in steps] == ["#276"], steps)          # #270 too old, #280 not merged
    ok(covered(steps[0]["step"], steps[0]["key"], "merge #276 and tell the user"), "an op that names #276 covers it")
    ok(not covered(steps[0]["step"], steps[0]["key"], "merge #2761 later"), "#2761 is another PR")
    ok(covered(steps[0]["step"], steps[0]["key"], "run staff:migrate-auth on the host by hand"),
       "an op that says what the step says covers it")
    ok(not covered(steps[0]["step"], steps[0]["key"], "release window opens at 19:00"), "another topic covers nothing")

    # 3 ✅ «Ритуал», approved on 19.11, still in no PR
    records = [{"at": "2026-11-19T18:00", "project": "chica", "source": "log 2026-11-19 18:00",
                "text": "Сторінку «Ритуал» затверджено: текст і фото беремо як у макеті"},
               {"at": "2026-11-19T12:00", "project": "chica", "source": "log 2026-11-19 12:00",
                "text": "Треба перевірити ціни постачальника"}]
    approved = approved_items(records, lambda proj: "")
    ok([r["text"][:7] for r in approved] == ["Сторінк"], approved)      # only the approved one
    ok(approved_items(records, lambda proj: "PR: сторінка Ритуал у роботі") == [],
       "a PR that names it takes it off the shelf")
    ok(approved_items(records, lambda proj: "feat/page")[0]["text"].startswith("Сторінку"), "another branch is not a name")
    ok(names("Сторінку «Ритуал» затверджено", "ритуал для сайту")[0], "one key word is enough for one")
    ok(not names("Сторінку «Ритуал» затверджено", "новий шрифт")[0], "an unrelated text does not name it")

    # 1 🔴 a card line and a dead URL
    st = {"notes": ["nova-poshta: на диску 2.8.2, активна 2.8.1", "🕸 PR #12: застарів"],
          "problems": ["Чіка: API health: HTTP 503", "⚔️ PR #5: конфлікт з develop"]}
    got = prod_items(st, ["Чіка: API health", "Чіка: сайт"])
    ok(got == ["nova-poshta: на диску 2.8.2, активна 2.8.1", "Чіка: API health: HTTP 503"], got)

    # 4 📤 three branches outside a PR
    st = {"problem_data": [{"kind": "unpushed", "branches": [{"branch": "feat/auth-ritual", "commits": 2},
                                                             {"branch": "fix/leech-a", "commits": 1},
                                                             {"branch": "fix/leech-b", "commits": 3}]},
                           {"kind": "worktree_dirty",
                            "worktrees": [{"path": "/r/.claude/worktrees/leech-c", "count": 2}]}]}
    ok(branch_items(st) == ["feat/auth-ritual (+2)", "fix/leech-a (+1)", "fix/leech-b (+3)",
                            "worktree leech-c: 2 files"], branch_items(st))

    # 5 ⏳ an agent that works, and an operation nobody touched for two hours
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "shelf-ops.json")
        ops = [{"id": "lw-ci", "project": "lw", "what": "CI", "started": "2026-11-20",
                 "updated": "2026-11-20"},
               {"id": "lw-ask", "project": "lw", "what": "x", "needs": "user", "updated": "2026-11-01"},
               {"id": "lw-next", "project": "lw", "what": "y", "queue": "next", "updated": "2026-11-01"},
               {"id": "lw-old", "project": "lw", "what": "z", "updated": "2026-11-19"}]
        ok([o["id"] for o in ops_waiting(ops, now=now, path=path)] == ["lw-old"],
           "an operation of yesterday waits now; one of today only starts the clock")
        later = ops_waiting(ops, now=now + 3 * 3600, path=path)
        ok([o["id"] for o in later] == ["lw-ci", "lw-old"], [o["id"] for o in later])
        ok(read(path, {}).get("lw-ask") is None, "an operation of the user is not the coordinator's")

    agents = {"terminals": [{"name": "ch-renew", "agent": "claude-code", "state": "live", "parent": "t2",
                             "status": "working", "cwd": "/repo/chica"},
                            {"name": "coach", "agent": "claude-code", "state": "live", "cwd": "/repo/chica"},
                            {"name": "genius-ops-sweep", "agent": "zsh", "state": "live", "cwd": "/repo"}]}
    saved = sh
    globals()["sh"] = lambda args, **k: json.dumps(agents) if args[:2] == ["hc", "ls"] else ""
    try:
        got = agent_items([{"name": "chica", "path": "/repo/chica"}])
    finally:
        globals()["sh"] = saved
    ok(got == {"chica": ["ch-renew (working)"]}, got)        # no parent, no name: not a task agent

    # 6 🔁 decided before: the base answers the question, so nobody asks the user again
    def rec(text, cos=0.72, exact=False):
        return (cos, {"id": "r", "text": text, "context": "", "kind": "rule", "project": "",
                      "source": "log", "at": "2026-11-20T09:00", "who": "user"}, cos, exact)
    font = pick_decided("Який шрифт брати для сайту?", [rec("Шрифт сайту: обрано Inter, рішення власника")])
    ok([r["text"][:6] for r in font] == ["Шрифт "], font)
    rule = pick_decided("Беру наступні дві?", [rec("Бери наступні задачі без питання, я завжди кажу «так»")])
    ok(len(rule) == 1, rule)
    ok(pick_decided("Який шрифт брати?", [rec("Шрифт сайту: Inter", cos=0.20)]) == [],
       "a weak match gives nothing: no hint is better than a wrong one")

    # the text: one line per project and shelf, per tag, at most MAX_LINES
    def built(key, items):
        return {"at": now, "status_at": now, "tags": {"chica": "💄 CH"}, "order": ["chica"],
                "shelves": {k: ({"chica": items} if k == key else {}) for k, _ in ICON}}

    text = render(built("prod", ["Чіка: API health: HTTP 503"]), "uk")
    ok(text.splitlines()[0].startswith("[coordinator shelf]"), text)
    ok(text.splitlines()[1] == "💄 CH · 🔴 Чіка: API health: HTTP 503", text)
    many = render({"at": now, "status_at": now, "order": [f"p{i}" for i in range(20)],
                   "tags": {f"p{i}": f"🏷 P{i}" for i in range(20)},
                   "shelves": {k: ({f"p{i}": [f"рядок {i}"] for i in range(20)} if k == "prod" else {})
                               for k, _ in ICON}}, "uk")
    ok(len(many.splitlines()) == MAX_LINES + 1 and many.splitlines()[-1].startswith("… +"),
       many.splitlines()[-2:])
    ok(render({"at": now, "status_at": 0, "tags": {}, "order": [],
               "shelves": {k: {} for k, _ in ICON}}) == "", "an empty shelf prints nothing")

    # the hook: once per session, and only when the shelf changed
    import contextlib
    import io
    global SEEN_DIR
    with tempfile.TemporaryDirectory() as d:
        saved, SEEN_DIR = SEEN_DIR, d
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ok(inject("[coordinator shelf] один", "s1") is True, "the first prompt of a session gets it")
                ok(inject("[coordinator shelf] один", "s1") is False, "the same shelf is not said twice")
                ok(inject("[coordinator shelf] два", "s1") is True, "a changed shelf comes again")
                ok(inject("[coordinator shelf] два", "s2") is True, "another session gets it too")
                ok(inject("", "s1") is False and read(seen_path("s1"), {}).get("hash"), "an empty shelf: silence")
        finally:
            SEEN_DIR = saved
    print("shelf selftest: ok")


def main():
    a = sys.argv[1:]
    if a[:1] == ["selftest"] or "--selftest" in a:
        return selftest()
    if a[:1] == ["hook"]:
        return hook()
    if a[:1] == ["refresh"]:
        return refresh()
    if a[:1] == ["build"]:
        seen = a[a.index("--seen") + 1] if "--seen" in a else ""
        return build(seen=seen, refresh=True)
    if a[:1] == ["decided"] and len(a) >= 2:
        project = a[a.index("--project") + 1] if "--project" in a else ""
        records = decided(a[1], project)
        if not records:
            print("nothing found")
            return
        for r in records:
            print(f"- [{r['source']}] {r['text']}")
        return
    sys.exit('usage: bin/lib/shelf.py build [--seen <session>] | hook | refresh | decided "<question>" '
             "[--project p] | selftest")


if __name__ == "__main__":
    main()
