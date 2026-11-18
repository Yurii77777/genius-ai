"""Resolve the merge conflicts of a pull request with a sub-agent (the user's decision of 2026-11-17:
«кнопочка, щоб в один клік запустити субагента резолвити конфлікти, враховуючи, що develop — голова»).

    bin/genius pr resolve <owner/repo> <N>   worktree of the PR's head branch + a `code` sub-agent + operation prfix-<N>
    bin/genius pr selftest                   self-check: repo → project, names, the brief

What it does (the Herdr panel's «resolve» button runs the same command):
  1. finds the project whose `origin` is <owner/repo> (genius.config.json → projects[].path);
  2. reads the PR with `gh` (the account the config names for the repo, as bin/status does);
  3. fetches the head and the base, makes the worktree <project>/.claude/worktrees/prfix-<N> on a local
     branch prfix-<N> that starts at origin/<head> (the head branch itself may be checked out elsewhere);
  4. starts the sub-agent through `bin/genius agent code …`, so the project's provider limits apply;
  5. adds the operation `prfix-<N>` (check: hc get <agent>; done_when: the PR is MERGEABLE).
The agent commits in the worktree and never pushes: the push is the coordinator's step.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sh(*cmd, env=None, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=300)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def repo_of_url(url):
    """`git@github.com:owner/repo.git` or `https://github.com/owner/repo` → `owner/repo`; else ''."""
    url = (url or "").strip().removesuffix(".git")
    return url.split("github.com")[-1].lstrip(":/") if "github.com" in url else ""


def find_project(repo, cfg=None):
    """The project (config entry + project.json) whose origin remote is `repo`; None when no one."""
    cfg = cfg if cfg is not None else read(os.path.join(ROOT, "genius.config.json"), {})
    for p in cfg.get("projects") or []:
        path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if not os.path.isdir(path):
            continue
        code, url, _ = sh("git", "-C", path, "remote", "get-url", "origin")
        if code == 0 and repo_of_url(url).lower() == repo.lower():
            return {"name": p["name"], "path": path,
                    **read(os.path.join(ROOT, "projects", p["name"], "project.json"), {})}
    return None


def gh_env(repo, cfg=None):
    """GH_TOKEN of the account the config names for this repo (the active gh login is never switched)."""
    cfg = cfg if cfg is not None else read(os.path.join(ROOT, "genius.config.json"), {})
    for s in cfg.get("services") or []:
        if s.get("type") == "github" and s.get("account") and repo in (s.get("repos") or []):
            code, token, _ = sh("gh", "auth", "token", "--user", s["account"])
            if code == 0 and token:
                return {**os.environ, "GH_TOKEN": token}, s["account"]
    return None, ""


def agent_name(repo, number):
    """`prfix-<repo>-<N>`: a-z 0-9 - _, at most 32 (the rule of bin/lib/agent.py)."""
    base = re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-") or "repo"
    tail = f"-{number}"
    return f"prfix-{base}"[: 32 - len(tail)].rstrip("-") + tail


def brief(repo, number, head, base, folder, report):
    return f"""Resolve the merge conflicts of pull request #{number} in {repo}: head branch «{head}», base branch «{base}».
Your folder is a git worktree of the PR: {folder} (local branch prfix-{number}, it started at origin/{head}).

Rule of the user: the BASE «{base}» is the head, the authority. Where the base and the PR disagree, the base's code wins.

Do this, in order:
1. Read the project rules first: the project's own AGENTS.md or CLAUDE.md in this folder, and what they point to.
2. Read what the PR does (`gh pr view {number} --repo {repo}`, `git log origin/{base}..HEAD`, `git diff origin/{base}...HEAD`): write down its own intent.
3. Merge the base into the branch with a MERGE COMMIT: `git fetch origin {base}` then `git merge origin/{base}`. No rebase, no force, no history rewrite.
4. For every conflict: keep the base's code. Then re-apply only the PR's own intent on top of it, in the base's style and structure. Never keep the PR's version of a line only because it is the PR's.
5. Never drop or weaken a test of the PR. If a base change makes a PR test wrong, adapt the test to the base's behaviour and say so in the report.
6. Run the tests of the PR, lint and the type check of the files it touches, single-threaded, each through the heavy queue (see the rules below). Fix what your merge broke. Do not fix what the base already had broken: report it.
7. Commit the merge (the merge commit message says what was resolved). Do NOT push. The coordinator pushes `git push origin HEAD:{head}` after the review.
8. The final message is your report; the watcher saves it to {report}. It lists: each conflicted file and how you resolved it (what from the base, what from the PR), the checks you ran and their result, what is not verified, your questions.
Do not do anything else: no refactor, no new feature, no fix outside the conflicts."""


def start(repo, number):
    cfg = read(os.path.join(ROOT, "genius.config.json"), {})
    project = find_project(repo, cfg)
    if not project:
        sys.exit(f"no project in genius.config.json has origin {repo}")
    if project.get("hold"):
        sys.exit(f"project {project['name']} is on hold (no new agent): lift it with `bin/genius unhold {project['name']}`")
    name = agent_name(repo, number)
    op_id = f"prfix-{number}"
    ops_path = os.path.join(ROOT, "state", "operations.json")
    ops = read(ops_path, [])
    if any(o.get("id") == op_id for o in ops):
        sys.exit(f"operation {op_id} already exists: the agent {name} works or waits for a look (state/agents/{name}.md)")
    env, account = gh_env(repo, cfg)
    code, out, err = sh("gh", "pr", "view", str(number), "--repo", repo, "--json",
                        "headRefName,baseRefName,state,isCrossRepository", env=env)
    if code != 0:
        sys.exit(f"gh pr view failed: {err or out}")
    pr = json.loads(out)
    if pr.get("state") != "OPEN":
        sys.exit(f"PR #{number} is {pr.get('state')}, not OPEN")
    if pr.get("isCrossRepository"):
        sys.exit(f"PR #{number} comes from a fork: its head is not in {repo}. Resolve it by hand.")
    head, base = pr["headRefName"], pr["baseRefName"]
    path = project["path"]
    folder = os.path.join(path, ".claude", "worktrees", op_id)
    if os.path.exists(folder):
        sys.exit(f"{folder} exists: remove it (git worktree remove) or continue there by hand")
    code, _, err = sh("git", "-C", path, "fetch", "origin", head, base, env=env)
    if code != 0:
        sys.exit(f"git fetch failed: {err}")
    os.makedirs(os.path.dirname(folder), exist_ok=True)
    code, _, err = sh("git", "-C", path, "worktree", "add", "-B", op_id, folder, f"origin/{head}", "--no-track")
    if code != 0:
        sys.exit(f"git worktree add failed: {err}")
    report = os.path.join(ROOT, "state", "agents", f"{name}.md")
    r = subprocess.run([os.path.join(ROOT, "bin", "genius"), "agent", "code", name, folder,
                        brief(repo, number, head, base, folder, report)], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sh("git", "-C", path, "worktree", "remove", "--force", folder)
        sh("git", "-C", path, "branch", "-D", op_id)
        sys.exit(f"the agent did not start (code {r.returncode}); the worktree is removed")
    token = f"GH_TOKEN=$(gh auth token --user {account}) " if account else ""
    ops.append({
        "id": op_id, "project": project["name"],
        "what": f"⚔️ PR #{number} ({repo}): агент зливає {base} у {head}, {base} — головна",
        "waiting_for": f"агент {name}: merge origin/{base} у гілку, тести; без push",
        "check": f"hc get {name} --json",
        "done_when": f"{token}gh pr view {number} --repo {repo} --json mergeable -q .mergeable | grep -q MERGEABLE",
        "next": f"звіт state/agents/{name}.md → рев'ю → git -C {folder} push origin HEAD:{head} (push — крок координатора)",
        "started": datetime.now().astimezone().isoformat(timespec="minutes"),
    })
    os.makedirs(os.path.dirname(ops_path), exist_ok=True)
    tmp = ops_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ops, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, ops_path)
    print(f"operation {op_id} added · worktree {folder} · head {head} · base {base}")


def selftest():
    import tempfile
    assert repo_of_url("git@github.com:acme/widget.git") == "acme/widget"
    assert repo_of_url("https://github.com/o/r") == "o/r" and repo_of_url("git@gitlab.com:o/r.git") == ""
    assert agent_name("o/Store", 246) == "prfix-store-246"
    n = agent_name("acme/a-very-long-repository-name-here", 12345)
    assert len(n) <= 32 and n.endswith("-12345") and re.fullmatch(r"[a-z0-9_-]{1,32}", n), n
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "remote", "add", "origin", "git@github.com:own/app.git"], check=True)
        cfg = {"projects": [{"name": "gone", "path": d + "/nope"}, {"name": "app", "path": d}]}
        assert find_project("own/app", cfg)["name"] == "app" and find_project("OWN/App", cfg)["name"] == "app"
        assert find_project("own/other", cfg) is None
    b = brief("own/app", 7, "feat/x", "develop", "/w/prfix-7", "/g/state/agents/prfix-app-7.md")
    for need in ("MERGE COMMIT", "No rebase, no force", "BASE «develop» is the head", "Never drop or weaken a test",
                 "Do NOT push", "git push origin HEAD:feat/x", "/g/state/agents/prfix-app-7.md", "single-threaded"):
        assert need in b, need
    print("prfix selftest: ok")


def main():
    a = sys.argv[1:]
    if a == ["selftest"]:
        return selftest()
    if len(a) == 3 and a[0] == "resolve" and re.fullmatch(r"[\w.-]+/[\w.-]+", a[1]) and a[2].isdigit():
        return start(a[1], int(a[2]))
    sys.exit(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
