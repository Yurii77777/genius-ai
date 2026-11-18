"""Ticket critique: a review agent reads a ticket BEFORE an autonomous engineer gets it.

    bin/genius ticket critique <ref>            start the critique agent (role review: Codex → Claude)
    bin/genius ticket critique --dry <ref>      print the brief, start nothing (tests)
    bin/genius ticket critique --sweep [--minutes N]   critique every ticket that became ready in the last
                                                N minutes (default 120) and was not critiqued yet; at most 2 at a time
    bin/genius ticket selftest                  checks of the parsing parts

    <ref>   a Jira key (ABC-123): the project is found by the key in its "tracker"; or a GitHub issue «owner/repo#N».

The user's decision of 2026-11-17: 8 of 9 PR reviews were «request changes» because tickets gave the agents only the
letter of the task. This is a mechanism, not an instruction: the critique runs by script.

`projects/<name>/project.json` → "tracker" (all keys but "type" optional):
    {"type": "jira", "keys": ["ABC"], "ready_jql": "project = ABC AND status = \\"AE Ready\\"", "env_file": "~/path/.env"}
    {"type": "github", "repo": "owner/repo", "ae_label": "ae-ready", "account": "<gh-login>"}
"keys": the Jira project keys of the project; without it they are read from `project = X` in the JQLs.
"account": the GitHub login that reads the repo (optional); without it the active `gh` login is used.
Jira credentials: JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN from the environment, else from `env_file`; never printed.

Output of the agent: state/drafts/ticket-critique-<ref>.md (Part A for the user, Part B for the ticket). The script adds an
operation `critique-<ref>` that waits for the user. It writes NOTHING to Jira or GitHub: applying the fixes is the
user's word. Read-only everywhere.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
CONFIG = os.path.join(ROOT, "genius.config.json")
DRAFTS = os.path.join(ROOT, "state", "drafts")
STATE = os.path.join(ROOT, "state", "ticket-critique.json")
MAX_PARALLEL = 2
DEFAULT_MINUTES = 120
JIRA_KEY = re.compile(r"^([A-Z][A-Z0-9_]*)-(\d+)$")
GH_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")

CHECKLIST = """\
1. Acceptance criteria: every requirement is an AC with a test that can FAIL. An AC proven only by a unit test of a helper is
   flagged when the real path (the code that runs in production) is not covered.
2. Contradictions between the ticket, the spec / PRD / glossary it cites, and the code. Name file:line.
3. Non-functional requirements as their own ACs: security (inputs, secrets in logs, SSRF, auth), the quality gate
   (Sonar / lint), the docs the ticket says to update.
4. Edge cases named: failure of each external service the change calls, retries and idempotency, empty and huge inputs,
   concurrency.
5. Scope: every file the change needs is in the scope list (the engineer refuses files outside it). Dependencies match the
   tracker links (Jira: blockers are the inward side of «Blocks»).
6. The estimate block validates (a yaml code block) where the project uses one."""


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


# ---- parsing -------------------------------------------------------------------------------------------------

def parse_ref(ref):
    """('jira', 'ABC-123', 'ABC') or ('github', 'owner/repo#4', 'owner/repo'); None when it is neither."""
    ref = ref.strip()
    m = JIRA_KEY.match(ref)
    if m:
        return "jira", ref, m.group(1)
    m = GH_REF.match(ref)
    if m:
        return "github", ref, m.group(1)
    return None


def slug(ref):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-")


def agent_name(ref):
    return ("crit-" + re.sub(r"[^a-z0-9_-]+", "-", ref.lower()).strip("-"))[:32].rstrip("-")


def draft_path(ref):
    return os.path.join(DRAFTS, f"ticket-critique-{slug(ref)}.md")


def jira_keys(tracker):
    """The Jira project keys of a tracker block: "keys", else every `project = X` in its JQLs."""
    if tracker.get("keys"):
        return {k.upper() for k in tracker["keys"]}
    found = set()
    for field in ("blocked_jql", "ready_jql"):
        found.update(re.findall(r"\bproject\s*=\s*\"?([A-Za-z][A-Za-z0-9_]*)", tracker.get(field) or ""))
    return {k.upper() for k in found}


def build_jql(base, minutes):
    """The ready JQL narrowed to what changed in the last `minutes`; its own ORDER BY is replaced."""
    base = re.sub(r"\s+ORDER\s+BY\s+.*$", "", base.strip(), flags=re.I | re.S)
    return f"({base}) AND updated >= -{int(minutes)}m ORDER BY updated DESC"


def adf_text(node):
    """Plain text of an Atlassian Document Format node (Jira v3 descriptions and comments)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    kind, out = node.get("type", ""), ""
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji", "inlineCard"):
        return node.get("attrs", {}).get("text") or node.get("attrs", {}).get("url", "")
    inner = adf_text(node.get("content"))
    if kind == "listItem":
        return "- " + inner.strip() + "\n"
    if kind == "codeBlock":
        return "```\n" + inner + "\n```\n"
    if kind in ("paragraph", "heading", "blockquote", "bulletList", "orderedList", "table", "tableRow", "rule"):
        return inner + ("\n" if kind not in ("bulletList", "orderedList", "table") else "")
    if kind in ("tableCell", "tableHeader"):
        return inner.strip() + " | "
    return inner + out


# ---- projects ------------------------------------------------------------------------------------------------

def projects():
    """[(name, folder, project.json)] for every project of the config."""
    out = []
    for p in read(CONFIG, {}).get("projects") or []:
        meta = read(os.path.join(ROOT, "projects", p["name"], "project.json"), {})
        out.append((p["name"], os.path.realpath(os.path.expanduser(p.get("path", ""))), meta))
    return out


def find_project(kind, scope):
    """The project that owns a ticket: by Jira key prefix, or by GitHub repo (tracker.repo)."""
    for name, folder, meta in projects():
        t = meta.get("tracker") or {}
        if kind == "jira" and t.get("type") == "jira" and scope.upper() in jira_keys(t):
            return name, folder, meta
        if kind == "github" and t.get("type") == "github" and (t.get("repo") or "").lower() == scope.lower():
            return name, folder, meta
    return None


# ---- fetching (read-only) ------------------------------------------------------------------------------------

def jira_env(tracker):
    env = {k: os.environ.get(k, "") for k in ("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN")}
    if not all(env.values()) and tracker.get("env_file"):
        try:
            with open(os.path.expanduser(tracker["env_file"]), encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"\s*(?:export\s+)?(JIRA_URL|JIRA_USERNAME|JIRA_API_TOKEN)\s*=\s*(.*)", line)
                    if m and not env[m.group(1)]:
                        env[m.group(1)] = m.group(2).strip().strip("\"'")
        except OSError:
            pass
    if not all(env.values()):
        sys.exit("no Jira credentials: set JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN or \"env_file\" in the tracker block")
    return env


def jira_get(env, path, params=None):
    url = env["JIRA_URL"].rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    auth = base64.b64encode(f"{env['JIRA_USERNAME']}:{env['JIRA_API_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        sys.exit(f"Jira did not answer for {path}: {type(e).__name__}")   # never the URL's headers or the token


def jira_ticket(key, tracker):
    env = jira_env(tracker)
    issue = jira_get(env, f"/rest/api/3/issue/{key}", {"fields": "summary,description,status,labels,issuelinks"})
    f = issue.get("fields", {})
    comments = jira_get(env, f"/rest/api/3/issue/{key}/comment", {"maxResults": 100}).get("comments", [])
    blocked_by, blocks = [], []
    for l in f.get("issuelinks", []):
        if l.get("type", {}).get("name") != "Blocks":
            continue
        if "inwardIssue" in l:      # this ticket «is blocked by» the other one
            blocked_by.append(l["inwardIssue"]["key"])
        if "outwardIssue" in l:     # this ticket «blocks» the other one
            blocks.append(l["outwardIssue"]["key"])
    lines = [f"# {key}: {f.get('summary', '')}", f"Status: {(f.get('status') or {}).get('name', '?')}",
             f"Labels: {', '.join(f.get('labels') or []) or '-'}",
             f"Tracker links, «Blocks»: blocked by (inward side) = {', '.join(blocked_by) or '-'}; blocks (outward side) = {', '.join(blocks) or '-'}",
             "", "## Description", adf_text(f.get("description")).strip() or "(empty)", "", "## Comments"]
    for c in comments:
        lines += [f"### {(c.get('author') or {}).get('displayName', '?')} · {c.get('created', '')[:16]}", adf_text(c.get("body")).strip(), ""]
    if not comments:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


def gh_env(tracker):
    """The environment for `gh`: the token of the login the tracker names (`account`), else the active one."""
    account = (tracker or {}).get("account")
    if account:
        r = subprocess.run(["gh", "auth", "token", "--user", account], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return {**os.environ, "GH_TOKEN": r.stdout.strip()}
    return dict(os.environ)


def github_ticket(repo, num, tracker):
    r = subprocess.run(["gh", "issue", "view", str(num), "-R", repo, "--json", "title,body,labels,comments,state"],
                       capture_output=True, text=True, env=gh_env(tracker), timeout=30)
    if r.returncode != 0:
        sys.exit(f"gh issue view failed for {repo}#{num}: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    lines = [f"# {repo}#{num}: {d.get('title', '')}", f"State: {d.get('state', '?')}",
             f"Labels: {', '.join(l['name'] for l in d.get('labels', [])) or '-'}", "", "## Body", (d.get("body") or "").strip() or "(empty)", "", "## Comments"]
    for c in d.get("comments", []):
        lines += [f"### {(c.get('author') or {}).get('login', '?')} · {c.get('createdAt', '')[:16]}", (c.get("body") or "").strip(), ""]
    if not d.get("comments"):
        lines.append("(none)")
    return "\n".join(lines) + "\n"


# ---- brief ---------------------------------------------------------------------------------------------------

def make_brief(ref, ticket_file, draft, folder, language):
    lang = {"uk": "Ukrainian", "en": "English"}.get(language, "Ukrainian")
    return f"""Ticket critique for {ref}. You are a READ-ONLY reviewer: change no file in {folder}, write nothing to Jira or GitHub, push nothing.
Goal: find what a ticket gives an autonomous engineer only as the letter of the task, before the engineer starts. Eight of nine PR reviews of 2026-11-17 were «request changes» for exactly this.

The ticket text is saved in {ticket_file}. Read it whole. Read the project rules ({folder}/AGENTS.md or CLAUDE.md) and every spec, PRD or glossary the ticket cites. Read the code the change would touch.

Checklist (check each point, name file:line for every claim about code):
{CHECKLIST}

Write ONE file: {draft}
- Part A («## A. Критика» in {lang}, short): the verdict READY or FIX FIRST, then the list of fixes, each one line, by checklist number.
- Part B («## B. Текст для тікета»): the exact text to add to the ticket (new or changed ACs, edge cases, scope lines, NFRs), written in the language of the ticket, ready to paste.
Do not invent product behaviour: when the ticket is unclear and the code and the spec do not answer, put it into Part A as an open question for the user."""


# ---- start ---------------------------------------------------------------------------------------------------

def add_operation(ref, project, draft):
    sys.path.insert(0, os.path.join(ROOT, "bin", "lib"))
    import ops
    items = ops.read(ops.OPS, [])
    op_id = f"critique-{slug(ref).lower()}"
    if any(o["id"] == op_id for o in items):
        return
    items.append({"id": op_id, "project": project, "what": f"Критика тікета {ref} (агент review)",
                  "waiting_for": f"критика тікета {ref}: чернетка {draft}", "check": f"ls {draft}",
                  "next": f"прочитати чернетку; якщо «apply» — власник вносить правки в тікет (скрипт у Jira/GitHub нічого не пише)",
                  "started": time.strftime("%Y-%m-%dT%H:%M%z"), "needs": "user",
                  "ask": f"{ref}: внести правки критики в тікет?", "answers": ["apply", "skip"],
                  "draft": draft})                   # stays until the user answers apply / skip (no done_when)
    ops.write(ops.OPS, items)


def critique(ref, dry=False):
    parsed = parse_ref(ref)
    if not parsed:
        sys.exit(f"«{ref}» is neither a Jira key (ABC-123) nor a GitHub issue (owner/repo#N)")
    kind, ref, scope = parsed
    hit = find_project(kind, scope)
    if not hit:
        sys.exit(f"no project owns {ref}: add a \"tracker\" block (type, and \"keys\" or \"repo\") to its projects/<name>/project.json")
    name, folder, meta = hit
    tracker = meta["tracker"]
    if not os.path.isdir(folder):
        sys.exit(f"project folder is missing: {folder}")
    text = jira_ticket(ref, tracker) if kind == "jira" else github_ticket(scope, ref.split("#")[1], tracker)
    os.makedirs(DRAFTS, exist_ok=True)
    ticket_file = os.path.join(DRAFTS, f"ticket-{slug(ref)}.txt")
    with open(ticket_file, "w", encoding="utf-8") as f:
        f.write(text)
    draft = draft_path(ref)
    brief = make_brief(ref, ticket_file, draft, folder, read(CONFIG, {}).get("language", "uk"))
    if dry:
        print(brief)
        return True
    r = subprocess.run([os.path.join(ROOT, "bin", "genius"), "agent", "review", agent_name(ref), folder, brief],
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        return False
    add_operation(ref, name, draft)
    state = read(STATE, {})
    state[ref] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": agent_name(ref)}
    write(STATE, state)
    return True


# ---- sweep ---------------------------------------------------------------------------------------------------

def running():
    """Critiques in work: operations `critique-*` whose draft does not exist yet."""
    sys.path.insert(0, os.path.join(ROOT, "bin", "lib"))
    import ops
    return sum(1 for o in ops.read(ops.OPS, []) if o["id"].startswith("critique-") and not os.path.exists(o.get("draft", "")))


def ready_refs(meta, minutes):
    t = meta.get("tracker") or {}
    if t.get("type") == "jira" and t.get("ready_jql"):
        env = jira_env(t)
        res = jira_get(env, "/rest/api/3/search/jql", {"jql": build_jql(t["ready_jql"], minutes), "fields": "summary", "maxResults": 20})
        return [i["key"] for i in res.get("issues", [])]
    if t.get("type") == "github" and t.get("repo") and t.get("ae_label"):
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))
        r = subprocess.run(["gh", "issue", "list", "-R", t["repo"], "--label", t["ae_label"], "--state", "open",
                            "--search", f"updated:>={since}", "--json", "number"],
                           capture_output=True, text=True, env=gh_env(t), timeout=30)
        return [f"{t['repo']}#{i['number']}" for i in json.loads(r.stdout or "[]")] if r.returncode == 0 else []
    return []


def reap():
    """Stop the critique agents whose draft is written: they leave Herdr's «Requests» (lesson of 2026-11-17)."""
    sys.path.insert(0, os.path.join(ROOT, "bin", "lib"))
    import ops
    for o in ops.read(ops.OPS, []):
        if o["id"].startswith("critique-") and os.path.exists(o.get("draft", "")):
            name = "crit-" + o["id"][len("critique-"):]
            subprocess.run(["hc", "kill", name], capture_output=True, text=True, timeout=30)


def sweep(minutes=DEFAULT_MINUTES):
    """Start a critique for each newly ready ticket, at most MAX_PARALLEL at a time. Returns the refs started."""
    try:
        reap()
    except (OSError, subprocess.SubprocessError):
        pass
    done, started = read(STATE, {}), []
    for name, folder, meta in projects():
        # a paused project is not checked, and a hold means «no new agent» (AGENTS.md «Live control»):
        # the sweep starts nothing for either (lesson of 2026-11-17: the user paused every project for the night)
        if meta.get("paused") or meta.get("hold"):
            continue
        try:
            refs = ready_refs(meta, minutes)
        except SystemExit:       # no credentials or no answer: the next pass tries again
            continue
        for ref in refs:
            if ref in done or ref in started:
                continue
            if running() + len(started) >= MAX_PARALLEL:
                return started
            if critique(ref):
                started.append(ref)
    return started


# ---- self-check ----------------------------------------------------------------------------------------------

def selftest():
    global STATE
    assert parse_ref("ABC-123") == ("jira", "ABC-123", "ABC")
    assert parse_ref("ABC-4") == ("jira", "ABC-4", "ABC")
    assert parse_ref("owner/repo#12") == ("github", "owner/repo#12", "owner/repo")
    assert parse_ref("abc-1") is None and parse_ref("ABC-") is None and parse_ref("a/b") is None
    assert slug("o/r#7") == "o-r-7" and agent_name("ABC-123") == "crit-abc-123" and len(agent_name("o" * 20 + "/" + "r" * 20 + "#12")) <= 32
    assert jira_keys({"keys": ["abc"]}) == {"ABC"}
    assert jira_keys({"blocked_jql": '(project = ABC OR parent = X-1) AND status = "B"', "ready_jql": "project = XYZ"}) == {"ABC", "XYZ"}
    q = build_jql('project = ABC AND status = "AE Ready" ORDER BY created ASC', 30)
    assert q == '(project = ABC AND status = "AE Ready") AND updated >= -30m ORDER BY updated DESC', q
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi"}]},
           {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}]}]}]}
    assert adf_text(adf).strip() == "Hi\n- a", repr(adf_text(adf))
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        STATE = os.path.join(d, "s.json")
        write(STATE, {"ABC-1": {"at": "x"}})
        assert "ABC-1" in read(STATE, {}) and read(os.path.join(d, "none.json"), {}) == {}
    b = make_brief("ABC-1", "/t.txt", "/d.md", "/f", "uk")
    assert "/d.md" in b and "READ-ONLY" in b and "Ukrainian" in b
    assert gh_env({}) == dict(os.environ) and gh_env({"account": ""}) == dict(os.environ)
    print("ticket selftest: ok")


def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return selftest()
    if a[:1] != ["critique"] or len(a) < 2:
        sys.exit(__doc__.strip().split("\n\n")[1])
    a = a[1:]
    if a[0] == "--sweep":
        minutes = int(a[a.index("--minutes") + 1]) if "--minutes" in a else DEFAULT_MINUTES
        started = sweep(minutes)
        print(f"ticket sweep: started {started or 'none'}")
    elif a[0] == "--dry" and len(a) == 2:
        critique(a[1], dry=True)
    elif len(a) == 1 and not a[0].startswith("-"):
        if not critique(a[0]):
            sys.exit(1)
    else:
        sys.exit("usage: bin/genius ticket critique <ref> | --dry <ref> | --sweep [--minutes N]")


if __name__ == "__main__":
    main()
