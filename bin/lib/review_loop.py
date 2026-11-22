"""The fix↔review loop of one pull request, run by script (the owner's goal of 2026-11-20: «не бачиш кінця
дороги — посилай іншого; ти лише фінальне рев'ю і апрув»).

    bin/genius loop <owner/repo> <N> [--project p] [--max-rounds 4]   start it on an open PR
    bin/genius loop status                                            the loops in work
    bin/genius loop selftest                                          fake hc, canned reports, no model

A round is: a `review` agent reads the PR head and writes a verdict; «request changes» → a `code` agent fixes
in the same worktree, commits and ends with «READY FOR REVIEW <sha>»; then the next review. The loop stops:

- on approve → ONE line to the coordinator session: «[review-loop] #N approved at <sha> after K rounds —
  final review». The coordinator does the final review, nothing else;
- on max rounds, a product question, or an agent that wrote nothing a script can read → one operation
  `loop-<repo>-<N>` with `needs: user` and the exact question, plus one line to the coordinator.

Who does what:
- the review agent and the fix agent start through `bin/genius agent`, so the brief critic (bin/lib/critic.py)
  and the project's provider limits apply to every spawn;
- the watcher (`bin/lib/agents_watch.py`) hands each finished turn to `on_turn_end()` and raises no per-round
  operation: a loop is one operation, not one per round;
- the loop kills each agent after its turn (AGENTS.md «Resources: stop what nobody uses»);
- the push: `"loop": {"push": true}` in projects/<name>/project.json — the SCRIPT pushes the commit to the
  PR's branch after every fix (the rule of that project). Without it the script only commits and the push
  stays the coordinator's step (a project: «do not push each commit»). No agent ever pushes.

Files of one PR (key = `owner-repo-N`):
    state/review-loop/<key>.json              the state: round, phase, agent, report, head, status
    state/review-loop/<key>.jsonl             one line per event: a round started, a verdict, a push, a stop
    state/review-loop/<key>-r<k>-review.md    what the review of round k wrote (the verdict lives here)
    state/review-loop/<key>-r<k>-fix.md       what the fix of round k wrote
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

DIR = os.path.join(ROOT, "state", "review-loop")
AGENTS = os.path.join(ROOT, "state", "agents")
OPS = os.path.join(ROOT, "state", "operations.json")
CLOSED = os.path.join(ROOT, "state", "operations.closed.json")
CONFIG = os.path.join(ROOT, "genius.config.json")
MAX_ROUNDS = 4

VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(APPROVE|REQUEST[\s_-]*CHANGES)", re.I | re.M)
QUESTION_RE = re.compile(r"^\s*PRODUCT QUESTION\s*:\s*(.*?)\s*$", re.I | re.M)
READY_RE = re.compile(r"READY FOR REVIEW\s*[:#]?\s*([0-9a-f]{7,40})", re.I)
NONE = ("", "none", "no", "немає", "нет", "-", "—", "n/a")

WORDS = {
    "uk": {"max": "PR #{n}: {k} раундів — межа, а рев'ю ще просить правки. Що робимо?",
           "noverdict": "PR #{n}: агент рев'ю не дав вердикту (ні «APPROVE», ні «REQUEST CHANGES»). Повторити рев'ю?",
           "noready": "PR #{n}: агент правок не дав «READY FOR REVIEW». Повторити правки?",
           "pushfail": "PR #{n}: push у гілку {head} не пройшов ({why}). Повторити?",
           "startfail": "PR #{n}: агент {name} не стартував ({why}). Повторити?",
           "crashed": "PR #{n}: кроку кола не вдалося ({why}). Повторити?",
           "question": "PR #{n}: рев'ю питає",
           "what": "🔁 {repo}#{n}: коло рев'ю й правок, раунд {k}",
           "review_wait": "агент {name}: рев'ю раунду {k}",
           "fix_wait": "агент {name}: правки за рев'ю раунду {k}",
           "answering": "відповісти й запустити знову: bin/genius loop {repo} {n}",
           "stop": "stop", "more": "one more round", "retry": "retry"},
    "en": {"max": "PR #{n}: {k} rounds is the limit and the review still asks for changes. What now?",
           "noverdict": "PR #{n}: the review agent gave no verdict (neither «APPROVE» nor «REQUEST CHANGES»). Review again?",
           "noready": "PR #{n}: the fix agent did not say «READY FOR REVIEW». Fix again?",
           "pushfail": "PR #{n}: the push to «{head}» failed ({why}). Retry?",
           "startfail": "PR #{n}: agent {name} did not start ({why}). Retry?",
           "crashed": "PR #{n}: a step of the loop failed ({why}). Retry?",
           "question": "PR #{n}: the review asks",
           "what": "🔁 {repo}#{n}: the fix↔review loop, round {k}",
           "review_wait": "agent {name}: review of round {k}",
           "fix_wait": "agent {name}: fixes after the review of round {k}",
           "answering": "answer it and start again: bin/genius loop {repo} {n}",
           "stop": "stop", "more": "one more round", "retry": "retry"},
}


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


def sh(args, env=None, timeout=300):
    """(code, stdout, stderr) of one command: hc, git, gh. Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def words():
    return WORDS.get(read(CONFIG, {}).get("language"), WORDS["uk"])


def lang_word():
    return {"uk": "Ukrainian", "en": "English"}.get(read(CONFIG, {}).get("language"), "Ukrainian")


def none_word():
    return "немає" if read(CONFIG, {}).get("language", "uk") == "uk" else "none"


def file_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ---- names and paths ------------------------------------------------------------------------------------------

def key_of(repo, number):
    """`owner/repo` + 291 → `owner-repo-291`: one flat name for the files of that PR."""
    return f"{re.sub(r'[^a-z0-9]+', '-', repo.lower()).strip('-')}-{number}"


def short(repo):
    return re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-") or "repo"


def agent_name(repo, number, rnd, phase):
    """Herdr's rule: 1-32 of a-z 0-9 - _ — `loop-repo-291-r1-rev`, `loop-repo-291-r1-fix`."""
    tail = f"-{number}-r{rnd}-{'rev' if phase == 'review' else 'fix'}"
    return f"loop-{short(repo)}"[: 32 - len(tail)].rstrip("-") + tail


def op_id(repo, number):
    return f"loop-{short(repo)}-{number}"


def report_path(key, rnd, phase):
    return os.path.join(DIR, f"{key}-r{rnd}-{'review' if phase == 'review' else 'fix'}.md")


def state_path(key):
    return os.path.join(DIR, f"{key}.json")


def log(key, record):
    """One line per event of the loop: it survives the state file and shows what happened in which round."""
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, f"{key}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), **record}, ensure_ascii=False) + "\n")


def loops():
    """Every loop the scripts know, newest first."""
    out = []
    try:
        names = sorted(os.listdir(DIR))
    except OSError:
        return out
    for name in names:
        if name.endswith(".json"):
            st = read(os.path.join(DIR, name), {})
            if st.get("repo") and st.get("pr"):
                out.append(st)
    return sorted(out, key=lambda s: s.get("started", ""), reverse=True)


def owning(name):
    """The running loop whose current agent is `name`, else None. The watcher asks this before it treats a
    finished turn as a wait for the user."""
    for st in loops():
        if st.get("status") == "running" and st.get("agent") == name:
            return st
    return None


# ---- what an agent wrote --------------------------------------------------------------------------------------

def verdict_of(text):
    """`approve` · `changes` · '' — the token first, then a line that starts with the word."""
    m = VERDICT_RE.search(text or "")
    if m:
        return "approve" if m.group(1).upper().startswith("APPROVE") else "changes"
    for line in (text or "").splitlines()[:25]:
        s = line.strip().strip("*#_ `>")
        if not s or s.upper().startswith("VERDICT"):
            continue
        if s.upper().startswith("APPROVE"):
            return "approve"
        if "REQUEST CHANGES" in s.upper():
            return "changes"
    return ""


def question_of(text):
    """The exact product question the review asks, or '' (a «none» is a question of nobody)."""
    m = QUESTION_RE.search(text or "")
    if not m:
        return ""
    q = m.group(1).strip().strip("«»\"'`")
    return "" if q.lower().rstrip(".") in NONE else q


def ready_of(text):
    """The commit a fix agent names on its «READY FOR REVIEW <sha>» line, or ''."""
    m = READY_RE.search(text or "")
    return m.group(1) if m else ""


def last_question(text):
    """A fix agent that stopped to ask: its last non-empty line is a question."""
    for line in reversed((text or "").splitlines()):
        s = line.strip().strip("*_")
        if s:
            return s if s.endswith("?") else ""
    return ""


# ---- spawn, push ---------------------------------------------------------------------------------------------

def spawn(role, name, folder, brief):
    """One agent through `bin/genius agent`: the role picks the model, the project limits the providers, the
    brief passes the critic. (ok, what the script printed)."""
    r = subprocess.run([os.path.join(ROOT, "bin", "genius"), "agent", role, name, folder, brief],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out


def account_env(repo, cfg=None):
    """(env, account) for the account the config names for this repo — the active gh login is never switched."""
    import prfix
    return prfix.gh_env(repo, cfg if cfg is not None else read(CONFIG, {}))


def push(folder, head, env=None):
    """Push the fix commit to the PR's own branch: fast-forward only, never a force (the rule of a project
    with `"loop": {"push": true}`). (ok, why)."""
    code, out, err = sh(["git", "-C", folder, "-c", "credential.helper=!gh auth git-credential",
                         "push", "origin", f"HEAD:{head}"], env=env)
    return code == 0, (err or out)[:300]


def git_sha(folder):
    code, out, _ = sh(["git", "-C", folder, "rev-parse", "--short=12", "HEAD"], timeout=30)
    return out if code == 0 else ""


def parent_of(name):
    """The Herdr terminal of this session: the agent's parent (the loop answers to it)."""
    try:
        return json.loads(sh(["hc", "get", name, "--json"], timeout=30)[1] or "{}").get("parent") or ""
    except ValueError:
        return ""


def tell(st, line):
    """One line to the coordinator session (the owner sees the round in the panel, not in the chat)."""
    log(st["key"], {"event": "tell", "line": line})
    print(line)
    if st.get("parent"):
        sh(["hc", "send", st["parent"], line], timeout=60)


def save_screen(name, text):
    """The agent's last message in state/agents/<name>.md, as the watcher does for every agent."""
    if not text:
        return
    os.makedirs(AGENTS, exist_ok=True)
    with open(os.path.join(AGENTS, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(f"# {name} — {datetime.now().isoformat(timespec='minutes')}\n\n{text}\n")


def kill(name):
    sh(["hc", "kill", name], timeout=30)


# ---- operations ----------------------------------------------------------------------------------------------

def open_operation(st, waiting_for):
    """One operation for the whole loop: the panel shows it, `ops sweep` closes it when the PR is merged."""
    items = read(OPS, [])
    cfg = read(CONFIG, {})
    account = next((s.get("account") for s in cfg.get("services") or []
                    if s.get("type") == "github" and st["repo"] in (s.get("repos") or [])), "")
    token = f'GH_TOKEN="$(gh auth token --user {account})" ' if account else ""
    op = {"id": op_id(st["repo"], st["pr"]), "project": st["project"],
          "what": words()["what"].format(repo=st["repo"], n=st["pr"], k=st["round"]),
          "waiting_for": waiting_for or st.get("agent", ""),
          "check": f"hc get {st.get('agent', '')} --json",
          "done_when": f'{token}gh pr view {st["pr"]} -R {st["repo"]} --json state -q .state | grep -qE "MERGED|CLOSED"',
          "next": f"bin/genius loop status · state/review-loop/{st['key']}.jsonl",
          "started": st["started"], "updated": datetime.now().date().isoformat(), "by": "review-loop"}
    items = [o for o in items if o.get("id") != op["id"]] + [op]
    write(OPS, items)
    return op["id"]


def update_operation(st, waiting_for="", ask="", answers=(), next_step=""):
    items = read(OPS, [])
    oid = op_id(st["repo"], st["pr"])
    hit = next((o for o in items if o.get("id") == oid), None)
    if hit is None:
        open_operation(st, waiting_for)
        return update_operation(st, waiting_for, ask, answers, next_step)
    hit.pop("stale", None)
    hit["updated"] = datetime.now().date().isoformat()
    if waiting_for:
        hit["waiting_for"] = waiting_for
    if ask:
        hit.update({"needs": "user", "ask": ask, "answers": list(answers) or ["ok", "later"]})
    if next_step:
        hit["next"] = next_step
    write(OPS, items)
    return oid


def close_operation(st, why):
    """Move the loop's operation to operations.closed.json with its reason (as `bin/genius ops close` does)."""
    oid = op_id(st["repo"], st["pr"])
    items = read(OPS, [])
    hit = next((o for o in items if o.get("id") == oid), None)
    if not hit:
        return
    archive = read(CLOSED, [])
    archive.append({**hit, "closed": datetime.now().date().isoformat(), "closed_why": why})
    write(CLOSED, archive)
    write(OPS, [o for o in items if o.get("id") != oid])


# ---- briefs ---------------------------------------------------------------------------------------------------

def review_brief(st, rnd, prev):
    lang, none = lang_word(), none_word()
    n, repo, base = st["pr"], st["repo"], st["base"]
    earlier = (f"Round {rnd}. The review of round {rnd - 1} is in {prev}: read it, and check each of its "
               f"findings against the code.\n" if prev else "")
    return f"""Review pull request #{n} in {repo}: head branch «{st['head']}», base «{base}», head commit {st['sha'] or 'HEAD'}.
You are a READ-ONLY reviewer: change no file, commit nothing, push nothing, write nothing to GitHub or a tracker.

The worktree is {st['folder']} (branch {st['branch']}). Read the project rules first (AGENTS.md, or CLAUDE.md
in an old project) and the documents they point to.
What to review: `git diff origin/{base}...HEAD` in the worktree, the PR itself (`gh pr view {n} --repo {repo}`),
and the code the change touches.
{earlier}
Check, and name file:line for every claim about the code:
1. Correctness: the change does what the PR says, in every case the PR names, and follows the project rules.
2. Tests: a test fails when the behaviour breaks; no test of the PR is dropped or weakened.
3. Edge cases: the failure of each external service, retries, empty and huge input, concurrency.
4. Security and data: no secret in code or logs, no unbounded query, no personal data in a log.
5. The project's own gate for a PR (the commands its rules name): run it single-threaded through the heavy
   queue; say what you could not run and why.

Write ONE file: {st['report']}. Write it fresh.
Line 1 is exactly one of: `VERDICT: APPROVE` or `VERDICT: REQUEST CHANGES`.
Line 2 is exactly one of: `PRODUCT QUESTION: none` or `PRODUCT QUESTION: <one exact question in {lang}>`.
A product question is only what the PR, the spec and the code cannot answer: behaviour, wording, prices, a
rule. Everything you can decide from the code is a finding, not a question.
Then «## Findings»: one line per finding, in {lang}, each with file:line and why it blocks the merge. Write
«{none}» when there are none.
The last line of your final message repeats the verdict line: that line is what the script reads."""


def fix_brief(st, rnd, review_report):
    n, repo = st["pr"], st["repo"]
    if st["push"]:
        pushing = ("Do NOT push: the loop pushes your commit to the PR branch «" + st["head"] + "» by itself "
                   "after you finish.")
    else:
        pushing = ("Do NOT push: nobody pushes. The commit stays on the local branch; the coordinator decides "
                   "the push to «" + st["head"] + "».")
    return f"""Fix what the review of pull request #{n} in {repo} asks: round {rnd}.
The review report is {review_report}. Read it whole: each finding names file:line and why it blocks the merge.
Your folder is the worktree of the PR: {st['folder']} (branch {st['branch']}), head «{st['head']}», base «{st['base']}».
Read the project rules first (AGENTS.md, or CLAUDE.md in an old project).

Do this, in order:
1. Fix every finding of the report, and only those. No refactor, no new feature, no fix the report did not name.
2. If a finding needs a product decision (behaviour, wording, prices, a rule) that the PR, the spec and the
   code do not answer: stop, and make the one exact question the last line of your message. Change nothing else.
3. Run the checks the project rules name for a PR and the tests of the files you changed, single-threaded
   through the heavy queue. Fix what your change broke; report what was already broken before it.
4. Commit in the worktree; the message names each finding you fixed. {pushing}

The last line of your final message is exactly `READY FOR REVIEW <sha>`, where <sha> is your commit
(`git rev-parse HEAD`). The script reads that line and starts the next review."""


# ---- the rounds -----------------------------------------------------------------------------------------------

def save(st, **fields):
    st.update({k: v for k, v in fields.items() if v is not None})
    write(state_path(st["key"]), st)
    return st


def round_start(st, rnd, env=None):
    """Start the review of round `rnd` in the PR's worktree."""
    name = agent_name(st["repo"], st["pr"], rnd, "review")
    rpt = report_path(st["key"], rnd, "review")
    prev = report_path(st["key"], rnd - 1, "review") if rnd > 1 else ""
    save(st, round=rnd, phase="review", agent=name, report=rpt, status="running")
    log(st["key"], {"round": rnd, "phase": "review", "event": "start", "agent": name, "head": st["sha"]})
    ok, out = spawn("review", name, st["folder"], review_brief(st, rnd, prev))
    print(out or f"review round {rnd}: {name}")
    if not ok:
        return stop(st, f"the review agent did not start: {out[:300]}",
                    ask=words()["startfail"].format(n=st["pr"], name=name, why=out[:200]),
                    answers=[words()["retry"], words()["stop"]])
    update_operation(st, waiting_for=words()["review_wait"].format(name=name, k=rnd),
                     next_step=words()["answering"].format(repo=st["repo"], n=st["pr"]))


def fix_start(st):
    """Start the fix of this round: the same worktree, the review report as the brief."""
    rnd = st["round"]
    name = agent_name(st["repo"], st["pr"], rnd, "fix")
    rpt = report_path(st["key"], rnd, "fix")
    review = report_path(st["key"], rnd, "review")
    save(st, phase="fix", agent=name, report=rpt)
    log(st["key"], {"round": rnd, "phase": "fix", "event": "start", "agent": name})
    ok, out = spawn("code", name, st["folder"], fix_brief(st, rnd, review))
    print(out or f"fix round {rnd}: {name}")
    if not ok:
        return stop(st, f"the fix agent did not start: {out[:300]}",
                    ask=words()["startfail"].format(n=st["pr"], name=name, why=out[:200]),
                    answers=[words()["retry"], words()["stop"]])
    update_operation(st, waiting_for=words()["fix_wait"].format(name=name, k=rnd),
                     next_step=words()["answering"].format(repo=st["repo"], n=st["pr"]))


def review_done(st, report, said):
    """The review of this round wrote its report (or at least its last message)."""
    v = verdict_of(report) or verdict_of(said)
    q = question_of(report) or question_of(said)
    log(st["key"], {"round": st["round"], "phase": "review", "event": "end", "verdict": v, "question": q})
    if q:
        # the review cannot decide it and neither can a fix agent: it goes to the user, as it is
        return stop(st, f"product question: {q}", ask=q, answers=["ok", "later"])
    if v == "approve":
        return finish(st)
    if v == "changes":
        if st["round"] >= st["max_rounds"]:
            return stop(st, f"max rounds ({st['max_rounds']}): the review still asks for changes",
                        ask=words()["max"].format(n=st["pr"], k=st["max_rounds"]),
                        answers=[words()["stop"], words()["more"]])
        return fix_start(st)
    return stop(st, "the review gave no verdict", ask=words()["noverdict"].format(n=st["pr"]),
                answers=[words()["retry"], words()["stop"]])


def fix_done(st, report, said, env=None):
    """The fix of this round ended: it must name its commit."""
    said = said or report
    sha = ready_of(report) or ready_of(said)
    log(st["key"], {"round": st["round"], "phase": "fix", "event": "end", "sha": sha})
    if not sha:
        q = last_question(said)
        if q:
            return stop(st, f"the fix agent asks: {q}", ask=q, answers=["ok", "later"])
        return stop(st, "the fix agent wrote no «READY FOR REVIEW»", ask=words()["noready"].format(n=st["pr"]),
                    answers=[words()["retry"], words()["stop"]])
    head = git_sha(st["folder"])
    if head:
        # the worktree is the truth: an agent may name another commit than the one HEAD holds
        if sha[:7] != head[:7]:
            log(st["key"], {"round": st["round"], "event": "sha-note", "said": sha, "head": head})
        sha = head
    if st["push"]:
        ok, why = push(st["folder"], st["head"], env or account_env(st["repo"])[0])
        log(st["key"], {"round": st["round"], "event": "push", "head": st["head"], "sha": sha,
                        "ok": ok, "why": why})
        if not ok:
            return stop(st, f"the push failed: {why}",
                        ask=words()["pushfail"].format(n=st["pr"], head=st["head"], why=why[:160]),
                        answers=[words()["retry"], words()["stop"]])
    nxt = st["round"] + 1
    if nxt > st["max_rounds"]:
        return stop(st, f"max rounds ({st['max_rounds']})", ask=words()["max"].format(n=st["pr"], k=st["max_rounds"]),
                    answers=[words()["stop"], words()["more"]])
    save(st, sha=sha)
    return round_start(st, nxt, env)


def finish(st):
    """Approve: the loop is done, the coordinator gets one line and the final review."""
    if not st.get("sha"):
        save(st, sha=git_sha(st["folder"]) or "HEAD")
    save(st, status="done", finished=now())
    log(st["key"], {"round": st["round"], "event": "finish", "sha": st["sha"], "pushed": st["push"]})
    close_operation(st, f"approved by the review of round {st['round']} at {st['sha']}")
    if not st["push"]:
        # the line stays exactly as the coordinator reads it; the push it owes is one step away
        print(f"loop {st['key']}: the commits are local — git -C {st['folder']} push origin HEAD:{st['head']}")
    tell(st, f"[review-loop] #{st['pr']} approved at {st['sha']} after {st['round']} rounds — final review")
    print(f"loop {st['key']}: approved at {st['sha']} after {st['round']} rounds")


def stop(st, why, ask="", answers=()):
    """The loop stops where a script cannot decide: one operation, one question for the user, one line here."""
    save(st, status="stopped", why=why, finished=now())
    log(st["key"], {"event": "stop", "round": st["round"], "phase": st["phase"], "why": why, "ask": ask})
    if ask:
        update_operation(st, waiting_for=ask, ask=ask, answers=answers,
                         next_step=words()["answering"].format(repo=st["repo"], n=st["pr"]))
    tell(st, f"[review-loop] #{st['pr']} stopped after {st['round']} rounds: {why}"
             + (f" — «{ask}»" if ask else ""))
    print(f"loop {st['key']}: stopped — {why}")
    return True


def on_turn_end(name, message=""):
    """The watcher calls this when an agent of a loop finished its turn. True: the loop took the turn."""
    st = owning(name)
    if not st:
        return False
    said = message or ""
    save_screen(name, said)
    parent = parent_of(name)
    kill(name)
    st = owning(name) or st                   # a fresh read: the state file is the truth between rounds
    if parent and not st.get("parent"):
        st["parent"] = parent
        write(state_path(st["key"]), st)
    report = file_text(st.get("report", ""))
    env = account_env(st["repo"])[0] if st["push"] else None
    try:
        if st.get("phase") == "review":
            review_done(st, report, said)
        elif st.get("phase") == "fix":
            fix_done(st, report, said, env)
    except Exception as e:          # a broken step must not leave the turn to the watcher as a wait for the user
        stop(st, f"{type(e).__name__}: {e}",
             ask=words()["crashed"].format(n=st["pr"], why=f"{type(e).__name__}: {e}"[:160]),
             answers=[words()["retry"], words()["stop"]])
    return True


# ---- start ----------------------------------------------------------------------------------------------------

def find_project(repo, cfg, wanted=""):
    """The project that owns the repo: the one the user named, else the one whose origin is <owner/repo>."""
    import prfix
    if wanted:
        hit = next((p for p in cfg.get("projects") or [] if p.get("name") == wanted), None)
        if not hit:
            sys.exit(f"no project «{wanted}» in genius.config.json")
        path = os.path.realpath(os.path.expanduser(hit["path"]))
        code, url, _ = sh(["git", "-C", path, "remote", "get-url", "origin"], timeout=30)
        if code != 0 or prfix.repo_of_url(url).lower() != repo.lower():
            sys.exit(f"{path} has origin «{prfix.repo_of_url(url) or 'none'}», not {repo}: the loop would work "
                     f"in a wrong repository")
        return {"name": wanted, "path": path,
                **read(os.path.join(ROOT, "projects", wanted, "project.json"), {})}
    return prfix.find_project(repo, cfg)


def gh_pr(repo, number, env):
    code, out, err = sh(["gh", "pr", "view", str(number), "--repo", repo, "--json",
                         "headRefName,baseRefName,headRefOid,state,isCrossRepository,title"], env=env, timeout=60)
    if code != 0:
        sys.exit(f"gh pr view failed: {err or out}")
    pr = json.loads(out)
    if pr.get("state") != "OPEN":
        sys.exit(f"PR #{number} is {pr.get('state')}, not OPEN")
    if pr.get("isCrossRepository"):
        sys.exit(f"PR #{number} comes from a fork: its head is not in {repo}. Run the loop by hand.")
    return pr


def worktree_of(path, head):
    """The worktree that holds the branch <head> and is clean, else None (its path)."""
    code, out, _ = sh(["git", "-C", path, "worktree", "list", "--porcelain"], timeout=60)
    if code != 0:
        return None
    block = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if block.get("branch") == f"refs/heads/{head}" and block.get("worktree"):
                code, st, _ = sh(["git", "-C", block["worktree"], "status", "--porcelain"], timeout=60)
                if code == 0 and not st.strip():
                    return block["worktree"]
            block = {}
            continue
        k, _, v = line.partition(" ")
        block[k] = v
    return None


def ensure_worktree(project_path, head, base, number, env):
    """(folder, branch, created): a worktree of the PR head — the clean one that already holds it, else a new one."""
    found = worktree_of(project_path, head)
    if found:
        return found, head, False
    folder = os.path.join(project_path, ".claude", "worktrees", f"review-{number}")
    branch = f"review-{number}"
    if os.path.isdir(folder):
        return folder, branch, False
    code, _, err = sh(["git", "-C", project_path, "fetch", "origin", head, base], env=env, timeout=600)
    if code != 0:
        sys.exit(f"git fetch failed: {err}")
    os.makedirs(os.path.dirname(folder), exist_ok=True)
    code, _, err = sh(["git", "-C", project_path, "worktree", "add", "-B", branch, folder, f"origin/{head}",
                       "--no-track"], timeout=600)
    if code != 0:
        sys.exit(f"git worktree add failed: {err}")
    return folder, branch, True


def begin(repo, number, project, pr, folder, branch, max_rounds=MAX_ROUNDS, env=None, created=False):
    """The state of one loop, then the first review. No gh, no git: the selftest calls this directly."""
    key = key_of(repo, number)
    st = {"key": key, "repo": repo, "pr": number, "project": project["name"], "folder": folder,
          "branch": branch, "head": pr["headRefName"], "base": pr["baseRefName"],
          "sha": pr.get("headRefOid", ""), "round": 0, "phase": "", "agent": "", "report": "",
          "max_rounds": int(max_rounds), "push": bool((project.get("loop") or {}).get("push")),
          "status": "running", "started": now(), "why": "", "parent": ""}
    first = agent_name(repo, number, 1, "review")
    st["round"], st["agent"] = 1, first
    write(state_path(key), st)
    log(key, {"event": "begin", "head": st["head"], "base": st["base"], "sha": st["sha"],
              "push": st["push"], "worktree": folder, "created": created})
    open_operation(st, words()["review_wait"].format(name=first, k=1))
    if created:
        print(f"worktree {folder} on branch {branch} at origin/{st['head']}")
    print(f"loop {key}: PR #{number} · head «{st['head']}» · base «{st['base']}» · "
          f"push by the script: {'yes' if st['push'] else 'no'} · max {st['max_rounds']} rounds")
    round_start(st, 1, env)
    return st


def start(repo, number, project_name="", max_rounds=MAX_ROUNDS):
    cfg = read(CONFIG, {})
    project = find_project(repo, cfg, project_name)
    if not project:
        sys.exit(f"no project in genius.config.json has origin {repo}: name it with --project")
    if project.get("hold"):
        sys.exit(f"project {project['name']} is on hold (no new agent): lift it with "
                 f"`bin/genius unhold {project['name']}`")
    if project.get("paused"):
        sys.exit(f"project {project['name']} is paused: `bin/genius resume {project['name']}` first")
    key = key_of(repo, number)
    old = read(state_path(key), {})
    if old.get("status") == "running":
        sys.exit(f"the loop of {repo}#{number} already runs (round {old.get('round')}, agent "
                 f"{old.get('agent')}): bin/genius loop status")
    if old:                       # a stopped or done loop: its files stay, the state starts fresh
        log(key, {"event": "restart", "after": old.get("status"), "round": old.get("round")})
    env, _ = account_env(repo, cfg)
    pr = gh_pr(repo, number, env)
    folder, branch, created = ensure_worktree(project["path"], pr["headRefName"], pr["baseRefName"], number, env)
    st = begin(repo, number, project, pr, folder, branch, max_rounds, env, created)
    if not created:
        print(f"worktree {folder} (branch {branch}) is used as it is")
    return st


def status():
    """One line per loop: what round, which agent, how it stands."""
    items = loops()
    if not items:
        print("no loop: bin/genius loop <owner/repo> <N>")
        return
    for st in items:
        print(f"{st.get('status', '?'):8} {st['repo']}#{st['pr']} · round {st.get('round', 0)}/"
              f"{st.get('max_rounds', '?')} · {st.get('phase') or '—'} · {st.get('agent') or '—'}"
              + (f" · {st['why']}" if st.get("why") else ""))


# ---- self-check -----------------------------------------------------------------------------------------------

def selftest():
    """No Herdr terminal, no model, no git: a temp root, a fake hc, and canned reports. Three endings: approve
    after 2 rounds; a product question; the round limit."""
    import contextlib
    import io
    import tempfile
    global ROOT, DIR, AGENTS, OPS, CONFIG, sh, spawn
    keep = (ROOT, DIR, AGENTS, OPS, CONFIG, sh, spawn)
    tmp = tempfile.mkdtemp(prefix="genius-loop-")
    calls, spawned, fails = [], [], []

    def fake_sh(args, env=None, timeout=300):
        calls.append(list(args))
        if args[:2] == ["git", "-C"]:
            return 0, "", ""
        if args[:2] == ["hc", "get"]:
            return 0, '{"parent": "coord"}', ""
        return 0, "", ""

    def fake_spawn(role, name, folder, brief):
        spawned.append({"role": role, "name": name, "folder": folder, "brief": brief})
        return True, f"started {name} (t9) · role {role}"

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    ROOT = tmp
    DIR = os.path.join(tmp, "state", "review-loop")
    AGENTS = os.path.join(tmp, "state", "agents")
    OPS = os.path.join(tmp, "state", "operations.json")
    CONFIG = os.path.join(tmp, "genius.config.json")
    folder = os.path.realpath(os.path.join(tmp, "app"))
    for d in (folder, DIR, AGENTS, os.path.join(tmp, "state")):
        os.makedirs(d, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump({"language": "en"}, f)
    sh, spawn = fake_sh, fake_spawn
    project = {"name": "app", "path": folder, "loop": {"push": False}}
    pr = {"headRefName": "feat/x", "baseRefName": "develop", "headRefOid": "1a2b3c4d5e6f"}
    try:
        def begin_loop(number, max_rounds=4):
            return begin("own/app", number, project, pr, folder, "review-" + str(number), max_rounds)

        # 1) approve after 2 rounds: request changes → fix → ready → review → approve
        st = begin_loop(7)
        check("the loop starts with the review of round 1", st["phase"] == "review" and st["round"] == 1, st)
        check("the name fits Herdr's rule and names the round",
              re.fullmatch(r"[a-z0-9_-]{1,32}", st["agent"]) and st["agent"].endswith("-r1-rev"), st["agent"])
        check("only the review role starts it", [s["role"] for s in spawned] == ["review"], spawned)
        check("one operation for the whole loop, not one per round",
              [o["id"] for o in read(OPS, [])] == ["loop-app-7"], read(OPS, []))
        first = st["agent"]
        with open(st["report"], "w", encoding="utf-8") as f:
            f.write("VERDICT: REQUEST CHANGES\nPRODUCT QUESTION: none\n\n## Findings\n- apps/web/a.ts:12 bad\n")
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            taken = on_turn_end(first, "VERDICT: REQUEST CHANGES")
        st = read(state_path("own-app-7"), {})
        check("a finished review turn is the loop's, not a wait for the user", taken is True, taken)
        check("request changes starts the fix agent in the same worktree",
              st["phase"] == "fix" and spawned[-1]["role"] == "code" and spawned[-1]["folder"] == folder, st)
        check("the fix brief points to the review report",
              report_path("own-app-7", 1, "review") in spawned[-1]["brief"], spawned[-1]["brief"][:200])
        check("the fix brief carries the project's push rule (commit only)",
              "Do NOT push" in spawned[-1]["brief"], spawned[-1]["brief"][-400:])
        check("the finished agent is stopped at once", ["hc", "kill", first] in calls, calls)
        fixname = st["agent"]
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(fixname, "Fixed both.\n\nREADY FOR REVIEW 1a2b3c4d5e6f")
        st = read(state_path("own-app-7"), {})
        check("«READY FOR REVIEW <sha>» starts the next review",
              st["phase"] == "review" and st["round"] == 2 and spawned[-1]["role"] == "review", st)
        check("the next review gets the previous report path",
              report_path("own-app-7", 1, "review") in spawned[-1]["brief"], spawned[-1]["brief"][:300])
        with open(st["report"], "w", encoding="utf-8") as f:
            f.write("VERDICT: APPROVE\nPRODUCT QUESTION: none\n\n## Findings\nнемає\n")
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(st["agent"], "VERDICT: APPROVE")
        st = read(state_path("own-app-7"), {})
        sent = [c[-1] for c in calls if c[:2] == ["hc", "send"]]
        check("approve ends the loop", st["status"] == "done", st)
        check("approve sends exactly one line to the coordinator",
              sent == ["[review-loop] #7 approved at 1a2b3c4d5e6f after 2 rounds — final review"], sent)
        check("approve closes the operation", read(OPS, []) == [], read(OPS, []))
        lines = file_text(os.path.join(DIR, "own-app-7.jsonl")).splitlines()
        check("every round is logged", sum('"event": "start"' in l for l in lines) == 3
              and sum('"event": "end"' in l for l in lines) == 3, lines)
        check("a done loop is no longer owned by the watcher", owning("loop-app-7-r2-rev") is None)

        # 2) a product question stops the loop and asks the user the exact question
        spawned.clear()
        st = begin_loop(8)
        with open(st["report"], "w", encoding="utf-8") as f:
            f.write("VERDICT: REQUEST CHANGES\nPRODUCT QUESTION: Чи показувати знижку гостям?\n")
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(st["agent"], "")
        st = read(state_path("own-app-8"), {})
        op = next((o for o in read(OPS, []) if o["id"] == "loop-app-8"), {})
        check("a product question stops the loop", st["status"] == "stopped" and st["phase"] == "review", st)
        check("the question goes to the user as it is", op.get("ask") == "Чи показувати знижку гостям?", op)
        check("that operation waits for the user", op.get("needs") == "user", op)
        check("no fix agent starts on a product question", len(spawned) == 1, spawned)

        # 3) the round limit stops the loop with a question, not a silent end
        spawned.clear()
        st = begin_loop(9, max_rounds=1)
        with open(st["report"], "w", encoding="utf-8") as f:
            f.write("VERDICT: REQUEST CHANGES\nPRODUCT QUESTION: none\n")
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(st["agent"], "")
        st = read(state_path("own-app-9"), {})
        op = next((o for o in read(OPS, []) if o["id"] == "loop-app-9"), {})
        check("the round limit stops the loop", st["status"] == "stopped", st)
        check("the round limit asks the user", op.get("needs") == "user" and "1" in (op.get("ask") or ""), op)
        check("the limit starts no next agent", len(spawned) == 1, spawned)

        # 4) an agent that writes nothing a script can read is a question, never a silent loop
        spawned.clear()
        st = begin_loop(10)
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(st["agent"], "I read the diff. It looks fine to me.")
        st = read(state_path("own-app-10"), {})
        call = [o for o in read(OPS, []) if o["id"] == "loop-app-10"]
        check("no verdict → the loop stops and asks", st["status"] == "stopped" and call
              and call[0].get("answers") == ["retry", "stop"], call)

        # 4b) a fix agent that stops on a product decision: its own question goes to the user
        spawned.clear()
        st = begin_loop(11)
        with open(st["report"], "w", encoding="utf-8") as f:
            f.write("VERDICT: REQUEST CHANGES\nPRODUCT QUESTION: none\n")
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(st["agent"], "")
        st = read(state_path("own-app-11"), {})
        with contextlib.redirect_stdout(io.StringIO()):
            on_turn_end(st["agent"], "I read the report.\n\nWhich price do the guests see?")
        st = read(state_path("own-app-11"), {})
        call = [o for o in read(OPS, []) if o["id"] == "loop-app-11"]
        check("a fix agent's question stops the loop", st["status"] == "stopped", st)
        check("the fix agent's question goes to the user as it is",
              call and call[0].get("ask") == "Which price do the guests see?", call)
        check("no review starts on a fix agent's question", [s["role"] for s in spawned] == ["review", "code"],
              spawned)

        # 5) the parsers, on the exact lines the briefs ask for
        check("the verdict token is read", verdict_of("VERDICT: APPROVE\n") == "approve"
              and verdict_of("verdict: request changes") == "changes", verdict_of("verdict: request changes"))
        check("«PRODUCT QUESTION: none» is not a question", question_of("PRODUCT QUESTION: none") == ""
              and question_of("PRODUCT QUESTION: Чи це так?") == "Чи це так?")
        check("the ready line needs a real sha", ready_of("READY FOR REVIEW 1a2b3c4") == "1a2b3c4"
              and ready_of("READY FOR REVIEW <sha>") == "")

        # 6) the start path passes the brief critic (bin/lib/selfcheck.py checks it file by file)
        with open(os.path.realpath(__file__), encoding="utf-8") as f:
            code = f.read().split("def selftest(")[0]
        check("every spawn goes through bin/genius agent, so it passes the brief critic",
              '"agent", role, name, folder, brief' in code and '"hc", "run"' not in code)
    finally:
        ROOT, DIR, AGENTS, OPS, CONFIG, sh, spawn = keep
    if fails:
        print(f"review-loop selftest: {len(fails)} failed")
        sys.exit(1)
    print("review-loop selftest: ok")


# ---- command line ---------------------------------------------------------------------------------------------

def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return selftest()
    if a in (["status"], []):
        return status()
    import ops
    o, rest = ops.opts(a, {"project", "max-rounds"})
    if len(rest) == 2 and re.fullmatch(r"[\w.-]+/[\w.-]+", rest[0]) and rest[1].isdigit():
        rounds = int(o.get("max-rounds") or MAX_ROUNDS)
        if rounds < 1:
            sys.exit("--max-rounds must be 1 or more")
        return start(rest[0], int(rest[1]), o.get("project", ""), rounds)
    sys.exit("usage: bin/genius loop <owner/repo> <N> [--project p] [--max-rounds 4] | status | selftest")


if __name__ == "__main__":
    main()
