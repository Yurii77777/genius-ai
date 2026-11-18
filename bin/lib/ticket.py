"""Ticket critique: a review agent reads a ticket BEFORE an autonomous engineer gets it.

    bin/genius ticket critique <ref>            start the critique agent (role review: Codex → Claude)
    bin/genius ticket critique --dry <ref>      print the brief, start nothing (tests)
    bin/genius ticket critique --sweep [--minutes N]   critique every ticket that became ready in the last
                                                N minutes (default 120) and was not critiqued yet; at most 2 at a time;
                                                then apply every finished critique
    bin/genius ticket apply <ref> | --dry <ref>  the apply step of a finished critique, now instead of on the next sweep
    bin/genius ticket selftest                  checks of the parsing parts

    <ref>   a Jira key (ABC-123): the project is found by the key in its "tracker"; or a GitHub issue «owner/repo#N».

The user's decision of 2026-11-17: 8 of 9 PR reviews were «request changes» because tickets gave the agents only the
letter of the task. This is a mechanism, not an instruction: the critique runs by script.

The user's decision of 2026-11-18: the sweep does NOT ask «apply the critique?» for every ticket. A finished critique
goes through the apply step: the fixes are written into the ticket by the project's own narrow tracker script, then
validated, commented and the operation is closed. Only the PRODUCT questions the critique found become one operation
that waits for the user.

`projects/<name>/project.json` → "tracker" (all keys but "type" optional):
    {"type": "jira", "keys": ["ABC"], "ready_jql": "project = ABC AND status = \\"AE Ready\\"", "env_file": "~/path/.env",
     "apply_cmd": "<the project's write script> {ref} {text_file}",
     "comment_cmd": "<the project's write script> {ref} {text_file} --comment", "apply_note": "<line above the comment>"}
    {"type": "github", "repo": "owner/repo", "ae_label": "ae-ready", "account": "<gh-login>", "apply_cmd": "…"}
"keys": the Jira project keys of the project; without it they are read from `project = X` in the JQLs.
"account": the GitHub login that reads the repo (optional); without it the active `gh` login is used.
"apply_cmd": the project's own narrow write command, run by the apply step with the fixes of the critique. Placeholders
  {ref} (the ticket key), {text_file} (the fixes file), {draft} (the whole critique), {project}; the file paths are
  shell-quoted by the script, so do not quote them in the template. No "apply_cmd" → the operation asks the user as
  before. "comment_cmd" (optional) posts the critique as a comment the same way; without it no comment is posted.
Jira credentials: JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN from the environment, else from `env_file`; never printed.

The agent writes state/drafts/ticket-critique-<ref>.md: Part A (the verdict and the fixes), Part B (the text to add to the
ticket), Part C (the product questions, «немає» when none). The script applies Part B, validates that it really landed
in the ticket, comments, and closes the operation. Part C becomes one operation that waits for the user.
A repeat critique of the same ref always writes a fresh draft: the old one moves to state/drafts/archive/ first, and a
critique that wrote nothing says why (state, saved screen, one operation).
"""
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
CONFIG = os.path.join(ROOT, "genius.config.json")
DRAFTS = os.path.join(ROOT, "state", "drafts")
ARCHIVE = os.path.join(DRAFTS, "archive")
STATE = os.path.join(ROOT, "state", "ticket-critique.json")
MAX_PARALLEL = 2
DEFAULT_MINUTES = 120
STUCK_MIN = 45          # a critique whose agent wrote no draft after this long has failed
MAX_ATTEMPTS = 2        # starts per ref before the user is asked
SETTLE_SEC = 20         # a draft younger than this may still be written by its agent
APPLY_TIMEOUT = 180
JIRA_KEY = re.compile(r"^([A-Z][A-Z0-9_]*)-(\d+)$")
GH_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
HEADING = re.compile(r"^#{1,6}\s*([ABC])\b", re.M | re.I)
NO_QUESTIONS = {"", "-", "—", "немає", "нема", "none", "no", "n/a"}

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


def say(text):
    """The step's own line: the panel and the sweep log show it, a silent skip never happens."""
    print(text)


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


def fixes_path(ref):
    return os.path.join(DRAFTS, f"ticket-fixes-{slug(ref)}.md")


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


# ---- the draft of a critique ---------------------------------------------------------------------------------

def split_draft(text):
    """{'a': Part A, 'b': Part B, 'c': Part C} of a critique draft; 'a' holds the whole text without its parts."""
    parts, cur, buf = {}, None, []
    for line in (text or "").splitlines():
        m = HEADING.match(line)
        if m:
            if cur:
                parts[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).lower(), []
            continue
        buf.append(line)
    if cur:
        parts[cur] = "\n".join(buf).strip()
    if "a" not in parts:
        parts["a"] = (text or "").strip()
    parts.setdefault("b", "")
    parts.setdefault("c", "")
    return parts


def verdict(part_a):
    """'READY', 'FIX FIRST' or '' — the first verdict the critique gives."""
    found = re.search(r"\b(FIX\s+FIRST|READY)\b", part_a or "", re.I)
    return re.sub(r"\s+", " ", found.group(1).upper()) if found else ""


def questions_of(part_c):
    """The product questions of Part C, one per line; empty when the critique found none."""
    out = []
    for line in (part_c or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line).strip()
        if line.strip("*_ `'\"") and line.lower().strip("*_ `.'\"") not in NO_QUESTIONS:
            out.append(line)
    return out


def probe_line(part_b):
    """The marker the applied ticket must contain: the start of the longest plain line of Part B."""
    best, fence = "", False
    for line in (part_b or "").splitlines():
        plain = line.strip()
        if plain.startswith("```"):
            fence = not fence
            continue
        if fence or not plain or plain.startswith(("|", "#")):
            continue
        plain = re.sub(r"^[-*+]\s+", "", plain)
        if len(plain) > len(best):
            best = plain
    return best[:60] if len(best) >= 20 else ""


def normalize(text):
    """Comparison form: no markdown marks, no repeated spaces — Jira and GitHub store the text their own way."""
    text = re.sub(r"[*_`#>|]+", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def applied_text(text, probe):
    """True when the ticket text now holds the probe line of the fixes."""
    return bool(probe) and normalize(probe) in normalize(text)


def apply_command(template, ref, project, folder, text_file, draft):
    """The shell command of "apply_cmd" / "comment_cmd" with its placeholders filled in (paths shell-quoted)."""
    out = template
    for key, value in (("{ref}", ref), ("{project}", project), ("{text_file}", shlex.quote(text_file)),
                       ("{draft}", shlex.quote(draft))):
        out = out.replace(key, value)
    return out


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


def desc_of(text):
    """The Description section of a fetched ticket (what the apply step writes into)."""
    m = re.search(r"^## Description\n(.*?)(?=^## Comments\n|\Z)", text or "", re.M | re.S)
    return m.group(1) if m else (text or "")


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


# ---- state of one ref ----------------------------------------------------------------------------------------

def state_of(ref):
    return read(STATE, {}).get(ref) or {}


def save_state(ref, **fields):
    state = read(STATE, {})
    state[ref] = {**state.get(ref, {}), **fields, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    write(STATE, state)
    return state[ref]


def archive_draft(ref):
    """Move the previous draft aside, so a repeat critique always writes a fresh one (the bug of 2026-11-18)."""
    path = draft_path(ref)
    if not os.path.exists(path):
        return None
    os.makedirs(ARCHIVE, exist_ok=True)
    dst = os.path.join(ARCHIVE, f"{os.path.basename(path)[:-3]}-{time.strftime('%Y%m%d-%H%M%S')}.md")
    os.replace(path, dst)
    return dst


def draft_ready(path, started_epoch=0):
    """The draft exists, is settled, and is newer than the attempt that should have written it."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    return mtime >= started_epoch and (time.time() - mtime) >= SETTLE_SEC


# ---- brief ---------------------------------------------------------------------------------------------------

def make_brief(ref, ticket_file, draft, folder, language):
    lang = {"uk": "Ukrainian", "en": "English"}.get(language, "Ukrainian")
    return f"""Ticket critique for {ref}. You are a READ-ONLY reviewer of the repository: change no file in {folder}, write nothing to Jira or GitHub, push nothing.
Goal: find what a ticket gives an autonomous engineer only as the letter of the task, before the engineer starts. Eight of nine PR reviews of 2026-11-17 were «request changes» for exactly this.

The ticket text is saved in {ticket_file}. Read it whole. Read the project rules ({folder}/AGENTS.md or CLAUDE.md) and every spec, PRD or glossary the ticket cites. Read the code the change would touch.

Checklist (check each point, name file:line for every claim about code):
{CHECKLIST}

Write ONE file: {draft}. It replaces any older critique: write it fresh, from this ticket as it is now.
- Part A («## A. Критика», short, in {lang}): the verdict READY or FIX FIRST, then the list of fixes, each one line, by checklist number.
- Part B («## B. Текст для тікета»): the exact text to add to the ticket (new or changed ACs, edge cases, scope lines, NFRs), written in the language of the ticket, ready to paste. Write «немає» when the ticket needs no change.
- Part C («## C. Питання до власника»): only the PRODUCT questions that the ticket, the spec and the code do not answer — behaviour, wording, prices, rules. One question per line, in {lang}. Write «немає» when there are none.
A script writes Part B into the ticket at once and asks the user only about Part C. Do not put anything into Part C that you can decide from the ticket, the spec or the code."""


# ---- operations ----------------------------------------------------------------------------------------------

def ops_module():
    sys.path.insert(0, os.path.join(ROOT, "bin", "lib"))
    import ops
    return ops


def add_operation(ref, project, draft, apply=False):
    """The operation of one critique: waits for the draft, then (with "apply_cmd") for the apply step itself."""
    ops = ops_module()
    items = ops.read(ops.OPS, [])
    op_id = f"critique-{slug(ref).lower()}"
    op = {"id": op_id, "project": project, "what": f"Критика тікета {ref} (агент review)",
          "waiting_for": f"критика тікета {ref}: чернетка {draft}", "check": f"test -f {draft}",
          "started": time.strftime("%Y-%m-%dT%H:%M%z"), "started_epoch": int(time.time()), "draft": draft}
    if apply:
        op["next"] = f"застосувати правки критики в тікеті й закрити цю операцію (bin/genius ticket apply {ref})"
    else:
        op.update({"needs": "user", "ask": f"{ref}: внести правки критики в тікет?",
                   "answers": ["apply", "skip"],
                   "next": f"прочитати чернетку; якщо «apply» — власник вносить правки в тікет (у трекері немає \"apply_cmd\")"})
    old = next((o for o in items if o["id"] == op_id), None)
    if old:
        old.update(op)     # a repeat keeps one operation, never a second one for the same ref
        for key in ("answered", "stale", "paused"):
            old.pop(key, None)
        if apply:          # the earlier run asked the user; this one applies by itself
            for key in ("needs", "ask", "answers"):
                old.pop(key, None)
    else:
        items.append(op)
    ops.write(ops.OPS, items)


def close_operation(op_id, why):
    ops = ops_module()
    try:
        if any(o["id"] == op_id for o in ops.read(ops.OPS, [])):
            ops.close(op_id, why)
    except SystemExit:
        pass


def set_wait(op_id, waiting_for, ask=None, answers=None, next_step=None):
    """The operation now waits for the user, with one short question, or for a step that is named."""
    ops = ops_module()
    items = ops.read(ops.OPS, [])
    for o in items:
        if o["id"] != op_id:
            continue
        o["waiting_for"] = waiting_for
        o.pop("stale", None)
        if ask:
            o.update({"needs": "user", "ask": ask, "answers": answers or ["ok", "skip"]})
        else:
            o.pop("needs", None)
            o.pop("ask", None)
            o.pop("answers", None)
        if next_step:
            o["next"] = next_step
        o["updated"] = time.strftime("%Y-%m-%d")
        ops.write(ops.OPS, items)
        return True
    return False


def ask_user(ref, project, draft, waiting_for, ask, answers, next_step):
    """One short question for the user on the panel. The operation is there even if an earlier pass closed it:
    a wait that nobody sees is the bug of 2026-11-18 in another form."""
    op_id = f"critique-{slug(ref).lower()}"
    if not set_wait(op_id, waiting_for, ask=ask, answers=answers, next_step=next_step):
        add_operation(ref, project, draft, apply=True)
        set_wait(op_id, waiting_for, ask=ask, answers=answers, next_step=next_step)
    return op_id


# ---- start ---------------------------------------------------------------------------------------------------

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
    # a repeat writes a fresh draft: the old one moves to state/drafts/archive/ and never blocks the new agent
    moved = archive_draft(ref)
    if moved:
        say(f"{ref}: the earlier critique moved to {moved}; a new one is written")
    attempt = int(state_of(ref).get("attempt") or 0) + 1
    r = subprocess.run([os.path.join(ROOT, "bin", "genius"), "agent", "review", agent_name(ref), folder, brief],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        reason = " ".join(out.split())[:300] or f"bin/genius agent exited {r.returncode}"
        save_state(ref, status="failed", attempt=attempt, why=reason, agent=agent_name(ref),
                   started_epoch=int(time.time()), draft=draft, project=name)
        say(f"{ref}: the critique agent did not start — {reason}")
        return False
    add_operation(ref, name, draft, apply=bool(tracker.get("apply_cmd")))
    save_state(ref, status="started", attempt=attempt, agent=agent_name(ref), started_epoch=int(time.time()),
               draft=draft, project=name, why="", waiting="", applied="", verdict="")
    print(out)
    return True


# ---- apply step ---------------------------------------------------------------------------------------------

def note_for(tracker, language):
    """The line above the comment: the tracker's own words when it names them, else in the user's language."""
    if tracker.get("apply_note"):
        return tracker["apply_note"]
    return ("Правки з критики тікета внесені автоматично (genius-ai)." if language == "uk"
            else "The ticket critique's fixes were applied automatically (genius-ai).")


def decision(draft_text, tracker):
    """What to do with a finished critique:
    ('apply', part_b) · ('questions', [q]) · ('ask', why) · ('close', why)."""
    parts = split_draft(draft_text)
    verdict_a, part_b = verdict(parts["a"]), parts["b"].strip()
    questions = questions_of(parts["c"])
    if verdict_a == "READY" and not questions and part_b.strip("*_ `.").lower() in NO_QUESTIONS:
        return "close", "критика: READY, правок не треба" if tracker.get("apply_cmd") else "критика: READY"
    if questions:
        return "questions", questions
    if not tracker.get("apply_cmd"):
        return "ask", "у трекері проєкту немає \"apply_cmd\""
    if not part_b or part_b.strip("*_ `.").lower() in NO_QUESTIONS:
        return "close", "критика: правок у частині B немає"
    return "apply", part_b


def run_command(template, ref, project, folder, text_file, draft):
    """(code, output) of one tracker write command, run in the project's folder."""
    cmd = apply_command(template, ref, project, folder, text_file, draft)
    try:
        r = subprocess.run(["/bin/zsh", "-lc", cmd], cwd=folder, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
        return r.returncode, " ".join(((r.stdout or "") + (r.stderr or "")).split())[:300]
    except subprocess.TimeoutExpired:
        return 124, f"the command did not answer in {APPLY_TIMEOUT} s"


def confirm(kind, ref, scope, tracker, part_b):
    """The ticket now holds the fixes: re-read it and look for the probe line of Part B."""
    probe = probe_line(part_b)
    if not probe:
        return True, "no probe line (short fixes): not checked against the ticket"
    try:
        text = jira_ticket(ref, tracker) if kind == "jira" else github_ticket(scope, ref.split("#")[1], tracker)
    except SystemExit as e:
        return False, f"the ticket could not be read back: {e}"
    return applied_text(desc_of(text), probe), f"probe «{probe[:40]}…» " + ("found" if applied_text(desc_of(text), probe) else "NOT found")


def apply_one(ref, dry=False):
    """The apply step of one finished critique. Returns (state, line): applied · questions · ask · wait · close."""
    parsed = parse_ref(ref)
    if not parsed:
        return "wait", f"«{ref}» is not a ticket reference"
    kind, ref, scope = parsed
    hit = find_project(kind, scope)
    if not hit:
        return "wait", f"no project owns {ref}"
    name, folder, meta = hit
    tracker, draft = meta["tracker"], draft_path(ref)
    st = state_of(ref)
    if not os.path.exists(draft):
        return "wait", f"{ref}: no draft yet"
    if not draft_ready(draft, st.get("started_epoch") or 0):
        return "wait", f"{ref}: the draft is still being written"
    if st.get("applied"):
        return "close", f"{ref}: already applied at {st['applied']}"
    try:
        with open(draft, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return "wait", f"{ref}: the draft cannot be read ({type(e).__name__})"
    what, payload = decision(text, tracker)
    op_id = f"critique-{slug(ref).lower()}"
    if what == "close":
        close_operation(op_id, f"critique: {payload}")
        save_state(ref, status="done", verdict=verdict(split_draft(text)["a"]), questions=0,
                   applied=time.strftime("%Y-%m-%dT%H:%M:%S"), apply=payload,
                   draft_mtime=int(os.path.getmtime(draft)))
        return "close", f"{ref}: {payload}"
    if what == "questions":
        # the only ask the sweep makes: the product questions the ticket, the spec and the code do not answer.
        # The fixes wait for the answer, because the answer can change them.
        one = " ".join(payload)
        ask = one if len(one) <= 220 else one[:217].rstrip() + "…"
        ask_user(ref, name, draft, f"{ref}: критика знайшла питання до власника ({len(payload)}); правки чекають відповіді",
                 ask, ["answer", "later"], f"після відповіді — bin/genius ticket critique {ref}, потім bin/genius ticket apply {ref}")
        save_state(ref, status="done", verdict=verdict(split_draft(text)["a"]), questions=len(payload),
                   waiting="questions", draft_mtime=int(os.path.getmtime(draft)))
        return "questions", f"{ref}: {len(payload)} product question(s) wait for the user"
    if what == "ask":
        ask_user(ref, name, draft, f"критика тікета {ref}: чернетка {draft}",
                 f"{ref}: внести правки критики в тікет?", ["apply", "skip"],
                 "прочитати чернетку; якщо «apply» — власник вносить правки в тікет")
        save_state(ref, status="done", verdict=verdict(split_draft(text)["a"]),
                   waiting="ask", draft_mtime=int(os.path.getmtime(draft)))
        return "ask", f"{ref}: {payload}; the user is asked"
    part_b = payload
    fixes = fixes_path(ref)
    if dry:
        print(f"{ref}: {apply_command(tracker['apply_cmd'], ref, name, folder, fixes, draft)}")
        return "apply", f"{ref}: dry, nothing written"
    with open(fixes, "w", encoding="utf-8") as f:
        f.write(part_b.rstrip() + "\n")
    code, out = run_command(tracker["apply_cmd"], ref, name, folder, fixes, draft)
    if code != 0:
        save_state(ref, status="done", apply=f"failed: {out}", project=name, waiting="apply",
                   draft_mtime=int(os.path.getmtime(draft)))
        ask_user(ref, name, draft, f"{ref}: apply_cmd exited {code}: {out}",
                 f"{ref}: правки критики не внесені в тікет — повторити?", ["retry", "skip"],
                 f"bin/genius ticket apply {ref}")
        return "ask", f"{ref}: apply_cmd exited {code} — {out}"
    ok, how = confirm(kind, ref, scope, tracker, part_b)
    if not ok:
        save_state(ref, status="done", apply=f"not confirmed: {how}", project=name, waiting="apply",
                   draft_mtime=int(os.path.getmtime(draft)))
        ask_user(ref, name, draft, f"{ref}: apply_cmd finished, but the ticket does not hold the fixes ({how})",
                 f"{ref}: правки не підтверджені в тікеті — перевірити?", ["check", "skip"],
                 "перевірити опис тікета й apply_cmd проєкту")
        return "ask", f"{ref}: the fixes are not confirmed in the ticket ({how})"
    commented = "no comment_cmd"
    if tracker.get("comment_cmd"):
        comment = os.path.join(DRAFTS, f"ticket-comment-{slug(ref)}.md")
        with open(comment, "w", encoding="utf-8") as f:
            f.write(f"{note_for(tracker, read(CONFIG, {}).get('language', 'uk'))}\n\n{part_b.rstrip()}\n")
        ccode, cout = run_command(tracker["comment_cmd"], ref, name, folder, comment, draft)
        commented = "comment posted" if ccode == 0 else f"comment failed: {cout}"
    close_operation(op_id, f"auto (ticket apply): fixes written into {ref}, {how}; {commented}")
    save_state(ref, status="done", verdict=verdict(split_draft(text)["a"]), applied=time.strftime("%Y-%m-%dT%H:%M:%S"),
               apply=f"ok, {how}; {commented}", questions=0, project=name, waiting="",
               draft_mtime=int(os.path.getmtime(draft)))
    return "applied", f"{ref}: fixes written into the ticket, {how}; {commented}"


# ---- sweep ---------------------------------------------------------------------------------------------------

def running():
    """Critiques in work: operations `critique-*` whose draft does not exist yet."""
    ops = ops_module()
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
    """Stop the critique agents that wrote their draft. Only a draft NEWER than the attempt counts (the bug of
    2026-11-18: a stale draft of an earlier critique killed the fresh agent before it wrote anything)."""
    ops = ops_module()
    for o in ops.read(ops.OPS, []):
        if not o["id"].startswith("critique-"):
            continue
        draft = o.get("draft", "")
        try:
            fresh = os.path.getmtime(draft) >= (o.get("started_epoch") or 0)
        except OSError:
            fresh = False
        if fresh:
            name = "crit-" + o["id"][len("critique-"):]
            subprocess.run(["hc", "kill", name], capture_output=True, text=True, timeout=30)


def active(ref):
    """The project of a ref is neither paused nor under a hold: the sweep may work with it."""
    parsed = parse_ref(ref)
    hit = find_project(parsed[0], parsed[2]) if parsed else None
    return hit, not (hit and (hit[2].get("paused") or hit[2].get("hold")))


def stuck():
    """Critiques whose agent wrote no draft: the screen is saved, the attempt is counted, then it says why —
    one more start while attempts are left, else one operation for the user. Returns refs reported."""
    reported, retry, now = [], [], time.time()
    for ref, st in read(STATE, {}).items():
        if st.get("status") != "started" or os.path.exists(st.get("draft") or draft_path(ref)):
            continue
        if now - (st.get("started_epoch") or now) < STUCK_MIN * 60:
            continue
        hit, live = active(ref)
        if hit and not live:
            continue                             # a paused project or a hold: report nothing, start nothing
        agent = st.get("agent") or agent_name(ref)
        saved, screen = os.path.join(ROOT, "state", "agents", f"{agent}.md"), ""
        try:
            r = subprocess.run(["hc", "read", agent], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                os.makedirs(os.path.dirname(saved), exist_ok=True)
                with open(saved, "w", encoding="utf-8") as f:
                    f.write(r.stdout)
                screen = " ".join(r.stdout.split())[-200:]
        except (OSError, subprocess.SubprocessError):
            pass
        subprocess.run(["hc", "kill", agent], capture_output=True, text=True, timeout=30)
        why = (f"агент критики {agent} не написав чернетку за {STUCK_MIN} хв (спроба {st.get('attempt')}); "
               f"екран збережено: {saved}" + (f"; останнє: …{screen[-120:]}" if screen else ""))
        save_state(ref, status="failed", why=why, waiting="")
        if (st.get("attempt") or 0) < MAX_ATTEMPTS:
            retry.append(ref)
            reported.append(f"{ref}: no draft, attempt {st.get('attempt')} — starting it again")
            continue
        ask_user(ref, hit[0] if hit else "", st.get("draft") or draft_path(ref), why,
                 f"{ref}: критика не дала результату — повторити?", ["retry", "skip"],
                 f"bin/genius ticket critique {ref}")
        save_state(ref, status="failed", why=why, waiting="failed")
        reported.append(f"{ref}: asked the user ({why})")
    return reported, retry


def apply_pending():
    """The apply step of every finished critique that nobody has handled yet. Returns the refs applied/asked."""
    applied = []
    for ref, st in list(read(STATE, {}).items()):
        if st.get("status") not in ("started", "done") or st.get("applied") or st.get("waiting"):
            continue                     # in work, already applied, or already waiting for the user
        draft = st.get("draft") or draft_path(ref)
        if not os.path.exists(draft):
            continue
        hit, live = active(ref)
        if hit and not live:
            continue                     # a paused project or a hold: no write to its tracker
        try:
            state, line = apply_one(ref)
            say(f"ticket apply: {line}")
            if state in ("applied", "questions"):
                applied.append(ref)
        except (SystemExit, OSError, subprocess.SubprocessError) as e:
            say(f"ticket apply: {ref}: {type(e).__name__}: {e}")
    return applied


def sweep(minutes=DEFAULT_MINUTES):
    """Apply finished critiques, report the critiques that wrote nothing, start the new ones (at most
    MAX_PARALLEL at a time). Returns (started, applied) refs."""
    try:
        reap()
    except (OSError, subprocess.SubprocessError):
        pass
    applied = apply_pending()
    started, retry = [], []
    try:
        lines, retry = stuck()
        say("ticket critique: " + ("; ".join(lines) or "no stuck critique"))
    except (SystemExit, OSError, subprocess.SubprocessError) as e:
        say(f"ticket critique: stuck check failed: {type(e).__name__}")
    for ref in retry:                       # a critique that wrote nothing, attempts left: one more start
        if running() + len(started) >= MAX_PARALLEL:
            break
        if critique(ref):
            started.append(ref)
    for name, folder, meta in projects():
        # a paused project is not checked, and a hold means «no new agent» (AGENTS.md «Live control»):
        # the sweep starts nothing for either (lesson of 2026-11-17: the user paused every project for the night)
        if meta.get("paused") or meta.get("hold"):
            continue
        try:
            refs = ready_refs(meta, minutes)
        except (SystemExit, OSError, subprocess.SubprocessError):   # no credentials or no answer: the next pass tries
            continue
        for ref in refs:
            st = read(STATE, {}).get(ref) or {}
            if st.get("status") in ("started", "done"):
                continue             # in work or finished: a repeat is the user's word
            if st.get("status") == "failed" and (st.get("attempt") or 0) >= MAX_ATTEMPTS:
                continue             # the user was asked; never a silent loop of starts
            if running() + len(started) >= MAX_PARALLEL:
                return started, applied
            if critique(ref):
                started.append(ref)
    return started, applied


# ---- self-check ----------------------------------------------------------------------------------------------

def selftest():
    global STATE, DRAFTS, ARCHIVE
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
    # the draft of a critique: three parts, the verdict, the product questions, the probe line
    draft = """## A. Критика
FIX FIRST
1. AC-03 має тест, що може впасти — зараз лише юніт-тест хелпера.
2. Контракт звіту суперечить spec.md:41.

## B. Текст для тікета
AC-03: звіт віддає 403, коли тенант чужий, і тест падає без перевірки тенанта.

## C. Питання до власника
- Чи показувати звіт без ціни?
"""
    p = split_draft(draft)
    assert verdict(p["a"]) == "FIX FIRST" and p["b"].startswith("AC-03") and questions_of(p["c"]) == ["Чи показувати звіт без ціни?"], p
    assert questions_of("немає") == [] and questions_of("- none\n- n/a") == []
    assert verdict("READY\nусе гаразд") == "READY" and verdict("") == ""
    probe = probe_line(p["b"])
    assert probe.startswith("AC-03: звіт віддає 403"), probe
    assert applied_text("Опис тікета без правок: лише старі пункти.", probe) is False
    # the ticket stores the fixes its own way: a table row, marks and a broken line still match
    assert applied_text("| AC-03: звіт віддає 403, коли тенант чужий, і тест падає без перевірки тенанта. |", probe)
    assert applied_text("**AC-03:** звіт віддає 403, коли тенант чужий,\nі тест падає без перевірки тенанта.", probe)
    assert probe_line("ok") == ""                       # a fix too short to prove anything
    tracker = {"apply_cmd": "~/x/jira-write {ref} {text_file}"}
    plain = draft.split("## C.")[0] + "## C. Питання до власника\nнемає\n"
    assert decision(plain, tracker) == ("apply", split_draft(plain)["b"]), decision(plain, tracker)
    assert decision(plain, {})[0] == "ask"                # no apply_cmd: today's ask
    assert decision(draft, tracker)[0] == "questions"    # a product question wins over any write
    assert decision(draft, tracker)[1] == questions_of(p["c"])
    assert decision("## A. Критика\nREADY\n## B. Текст для тікета\nнемає\n## C. Питання до власника\nнемає\n", tracker)[0] == "close"
    assert decision("## A. Критика\nFIX FIRST\n## B. Текст для тікета\nAC-1: нове\n", {})[0] == "ask"
    assert apply_command(tracker["apply_cmd"], "ABC-1", "chica", "/f", "/t f/x.md", "/d.md") == "~/x/jira-write ABC-1 '/t f/x.md'"
    # every placeholder, and a template without placeholders stays as it is
    assert apply_command("{project} {ref} {text_file} {draft}", "A-1", "p", "/f", "/a b.md", "/c.md") == "p A-1 '/a b.md' /c.md"
    assert apply_command("~/bin/jira-write", "A-1", "p", "/f", "/x.md", "/y.md") == "~/bin/jira-write"
    # a draft with no headings is Part A only: nothing to apply, nothing invented
    alone = split_draft("FIX FIRST\n1. щось")
    assert alone["a"].startswith("FIX FIRST") and alone["b"] == "" and alone["c"] == ""
    assert decision("FIX FIRST\n1. щось", tracker)[0] == "close"      # no Part B: no empty write into the ticket
    # a product question wins even over a READY verdict
    assert decision("## A. Критика\nREADY\n## B. Текст для тікета\nнемає\n## C. Питання до власника\n- Чи є ціна?\n",
                    tracker) == ("questions", ["Чи є ціна?"])
    # a probe never comes from a table row or a code fence
    assert probe_line("| a | b |\n```\ncode line long enough to be a probe\n```") == ""
    assert note_for({}, "uk").startswith("Правки") and note_for({"apply_note": "x"}, "en") == "x"
    assert applied_text("AC-03: звіт віддає 403, коли тенант чужий, і тест падає без перевірки тенанта.",
                        probe_line(p["b"]))
    assert desc_of("# K: t\nStatus: x\n\n## Description\nBody here\n\n## Comments\nc\n") == "Body here\n\n"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        STATE = os.path.join(d, "s.json")
        write(STATE, {"ABC-1": {"at": "x"}})
        assert "ABC-1" in read(STATE, {}) and read(os.path.join(d, "none.json"), {}) == {}
        # a repeat moves the old draft aside and the new path is free; an untouched draft stays until then
        DRAFTS, ARCHIVE = d, os.path.join(d, "archive")
        with open(draft_path("ABC-1"), "w", encoding="utf-8") as f:
            f.write("old")
        assert os.path.exists(draft_path("ABC-1"))
        dst = archive_draft("ABC-1")
        assert dst and os.path.exists(dst) and not os.path.exists(draft_path("ABC-1")), dst
        assert archive_draft("ABC-1") is None
        # a draft younger than the attempt that should have written it is not ready, an older one is
        with open(draft_path("ABC-2"), "w", encoding="utf-8") as f:
            f.write("new")
        assert draft_ready(draft_path("ABC-2"), 0) is False     # settled for SETTLE_SEC only
        old = time.time() - SETTLE_SEC - 5
        os.utime(draft_path("ABC-2"), (old, old))
        assert draft_ready(draft_path("ABC-2"), 0) and not draft_ready(draft_path("ABC-2"), old + 1)
    b = make_brief("ABC-1", "/t.txt", "/d.md", "/f", "uk")
    assert "/d.md" in b and "READ-ONLY" in b and "Ukrainian" in b and "C. Питання" in b
    assert gh_env({}) == dict(os.environ) and gh_env({"account": ""}) == dict(os.environ)
    print("ticket selftest: ok")


def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return selftest()
    if not a or a[0] not in ("critique", "apply") or len(a) < 2:
        sys.exit(__doc__.strip().split("\n\n")[1])
    cmd, a = a[0], a[1:]
    if cmd == "critique" and a[0] == "--sweep":
        minutes = int(a[a.index("--minutes") + 1]) if "--minutes" in a else DEFAULT_MINUTES
        started, applied = sweep(minutes)
        print(f"ticket sweep: started {started or 'none'}; applied {applied or 'none'}")
    elif cmd == "critique" and a[0] == "--dry" and len(a) == 2:
        critique(a[1], dry=True)
    elif cmd == "critique" and len(a) == 1 and not a[0].startswith("-"):
        if not critique(a[0]):
            sys.exit(1)
    elif cmd == "apply" and a[0] == "--dry" and len(a) == 2:
        state, line = apply_one(a[1], dry=True)
        print(line)
    elif cmd == "apply" and len(a) == 1 and not a[0].startswith("-"):
        state, line = apply_one(a[0])
        print(line)
        if state == "wait":
            sys.exit(1)
    else:
        sys.exit("usage: bin/genius ticket critique <ref> | --dry <ref> | --sweep [--minutes N] | apply <ref> | --dry <ref>")


if __name__ == "__main__":
    main()
