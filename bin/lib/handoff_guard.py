"""handoff-guard — a question in state/handoff.md must be an operation, never only text.

The owner's goal of 2026-11-20: «чому відкриті питання не були записані і чому я маю пинати і
памʼятати ці речі». The handoff is written at the end of the day, when the session is gone; a
question that only sits in it waits for nobody. This script refuses to let the day close while
such a question has no operation with `"needs": "user"` (state/operations.json, AGENTS.md
«Pending operations»).

    python3 -I bin/lib/handoff_guard.py            check state/handoff.md; exit 1 and name the lines
    python3 -I bin/lib/handoff_guard.py --json     the same as machine-readable data
    python3 -I bin/lib/handoff_guard.py --selftest

It runs itself from `bin/genius know digested <date>` (bin/lib/daylog.py → archive): the digest of a
day is refused until the fixes are registered. Every refused line comes with a ready
`bin/genius ops add …` command to fill in (bin/lib/ops.py → add).

What counts as a question: a line with «Питання», a `?`, or «чекає … слова/відповіді» («waiting
for … word/answer»). A line matches an operation when that operation waits for the user and
1. the line quotes the operation's `id` (`` `pr12-merge` ``, «pr12-merge», "pr12-merge"), or
2. the operation's `id` stands in the line as a whole word, or
3. the operation's `ask` (20+ characters) is part of the line.
Lines with ✅ (finished work), the 🔊 spoken line, headers, and URLs are not questions.
"""
import json
import os
import re
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
HANDOFF = os.path.join(ROOT, "state", "handoff.md")
OPS = os.path.join(ROOT, "state", "operations.json")
PROJECTS = os.path.join(ROOT, "projects")

URL = re.compile(r"https?://\S+|www\.\S+")
ASK_WORD = re.compile(r"питання|(?<![\w-])question(?![\w-])|(?<![\w-])q\s*:", re.I)
ASK_WAIT = re.compile(r"чека[єю].{0,60}(слова|слів|відповід|рішенн|підтвердженн)|"
                      r"waiting for .{0,60}(word|answer|decision|confirmation)", re.I)
QUOTED = re.compile(r"`([^`\n]{2,80})`|«([^»\n]{2,80})»|\"([^\"\n]{2,80})\"")
SKIP_START = ("#", ">", "```", "🔊")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def norm(text):
    """A text for comparison: lowercase, no punctuation, single spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", (text or "").lower(), flags=re.UNICODE)).strip()


def is_question(line):
    """A line that states an open question. Its URLs and quoted spans never make it one."""
    stripped = line.strip()
    if not stripped or stripped.startswith(SKIP_START) or "✅" in stripped:
        return False
    bare = QUOTED.sub(" ", URL.sub(" ", stripped))        # a quoted title or a link is not a question
    return bool("?" in bare or ASK_WORD.search(bare) or ASK_WAIT.search(bare))


def quoted_keys(line):
    return [g for groups in QUOTED.findall(line) for g in groups if g]


def matches(line, op, project_tags=None):
    """The line carries this operation: its id quoted or as a word, or its ask as part of the line."""
    if op.get("needs") != "user":
        return False
    op_id = (op.get("id") or "").strip() if isinstance(op.get("id"), str) else ""
    if op_id and (op_id in quoted_keys(line)
                  or re.search(rf"(?<![\w-]){re.escape(op_id)}(?![\w-])", line)):
        return True
    ask = norm(op.get("ask") or "")
    bare = norm(question_of(line, project_tags))
    return bool(ask) and (ask == bare or (len(ask) >= 20 and ask in norm(line)))


def project_of(line, tags):
    """The project whose tag stands in the line (`projects/<name>/project.json` → {"tag": "💄 CH"})."""
    for tag, name in tags.items():
        if tag and tag in line:
            return name
    return ""


def question_of(line, project_tags=None):
    """The line without its bullet, project tag and «Питання:» label: the question itself."""
    s = BULLET.sub("", line.strip()).strip()
    for tag in project_tags or {}:
        if tag and s.startswith(tag):
            s = s[len(tag):].lstrip(" ·-—:").strip()
            break
    return re.sub(r"^(питання|question|q)\s*[:—–-]\s*", "", s, flags=re.I).strip()


def hint(line, project, project_tags=None):
    """The ready `bin/genius ops add …` command for one line, with what is not known left to fill in."""
    q = question_of(line, project_tags)
    what = q if len(q) <= 60 else q[:57] + "…"
    parts = ["bin/genius", "ops", "add", shlex.quote(what),
             "--project", shlex.quote(project or "<project>"),
             "--ask", shlex.quote(q),
             "--answers", shlex.quote("<a>|<b>"),
             "--check", shlex.quote("echo manual"),
             "--next", shlex.quote("<next step>")]
    return " ".join(parts)


def tags(projects_dir=PROJECTS):
    out = {}
    try:
        names = sorted(os.listdir(projects_dir))
    except OSError:
        return out
    for name in names:
        meta = read(os.path.join(projects_dir, name, "project.json"), {})
        if isinstance(meta, dict) and meta.get("tag"):
            out[meta["tag"]] = name
    return out


def unanswered(text, ops, project_tags=None):
    """[(line, project, hint)] — the questions of the handoff that no user-waiting operation tracks."""
    project_tags = tags() if project_tags is None else project_tags
    user_ops = [o for o in ops or [] if isinstance(o, dict) and o.get("needs") == "user"]
    out = []
    for line in (text or "").splitlines():
        if not is_question(line) or any(matches(line, o, project_tags) for o in user_ops):
            continue
        project = project_of(line, project_tags)
        out.append((line.strip(), project, hint(line, project, project_tags)))
    return out


def check(handoff=HANDOFF, ops_file=OPS, project_tags=None):
    """The unanswered questions of the handoff file: [] when all is written down (or no handoff yet)."""
    if not os.path.exists(handoff):
        return []
    try:
        with open(handoff, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    return unanswered(text, read(ops_file, []), project_tags)


def message(problems):
    lines = [f"handoff: {len(problems)} open question(s) in state/handoff.md that no operation tracks. "
             "A question in text waits for nobody: register each one, then run this again."]
    for line, project, cmd in problems:
        lines.append(line if line.lstrip().startswith(("-", "*", "+")) else f"- {line}")
        lines.append(f"    {cmd}")
    lines.append("Quote the id that `ops add` prints in the handoff line (for example ` (`pr12-merge`)`), "
                 "so the line and the operation point at each other.")
    return "\n".join(lines)


def enforce(handoff=HANDOFF, ops_file=OPS):
    """Refuse (exit 1) while the handoff holds a question without an operation; print what to add."""
    problems = check(handoff, ops_file)
    if problems:
        print(message(problems), file=sys.stderr)
        raise SystemExit(1)
    return 0


def selftest():
    ops = [{"id": "pr12-merge", "needs": "user", "project": "lw", "ask": "Мерджити PR #12 у develop?"},
           {"id": "plain", "needs": "user", "project": "lw", "what": "check CI"},
           {"id": "answered", "project": "lw", "what": "site publish"}]
    project_tags = {"🌐 LW": "lw"}
    handoff = "\n".join([
        "# Handoff 2026-11-20",
        "- 🌐 LW · ✅ злито PR #11",
        "- 🌐 LW · Питання: мерджити PR #12 у develop? (`pr12-merge`)",       # 1: quoted id
        "- 🌐 LW · Питання: публікувати сайт сьогодні?",                      # 2: no operation
        "- 🌐 LW · чекає на слова власника про ціну",                         # 3: «чекає … слова»
        "- 🌐 LW · site publish — чекає на відповідь",                        # 4: no operation
        "- plain: питання, чи все гаразд?",                                  # 5: id as a word
        "Див. https://example.com/?tab=1 — нічого не питаю",                  # URL: not a question
        "🔊 Голосом: питань немає?",                                          # the spoken line: skipped
        "Мерджити PR #12 у develop? — без ключа, але ask збігається",          # 6: ask matches
    ])
    found = unanswered(handoff, ops, project_tags)
    got = [l for l, _p, _c in found]
    assert got == ["- 🌐 LW · Питання: публікувати сайт сьогодні?",
                   "- 🌐 LW · чекає на слова власника про ціну",
                   "- 🌐 LW · site publish — чекає на відповідь"], got
    assert [p for _l, p, _c in found] == ["lw", "lw", "lw"], found
    assert found[0][2].startswith("bin/genius ops add 'публікувати сайт сьогодні?' --project lw")
    assert "--ask" in found[0][2] and "--answers" in found[0][2] and "--next" in found[0][2]
    assert is_question("Питання — чи це питання?") and not is_question("✅ готово? ні")
    assert not is_question("Див. https://example.com/?tab=1") and not is_question("# Питання в заголовку?")
    assert not matches("нічого", {"id": "x"})                     # an operation without needs is not a tracker
    assert matches("Питання про це (`x`)", {"id": "x", "needs": "user"})
    assert unanswered("- Питання?", [{"id": "a", "needs": "user", "paused": {"since": "2026-11-19"}}]), \
        "a paused operation still holds its question"
    assert unanswered("", ops) == [] and unanswered("- все ✅", ops) == []

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        handoff_path, ops_path = os.path.join(d, "handoff.md"), os.path.join(d, "ops.json")
        with open(handoff_path, "w", encoding="utf-8") as f:
            f.write("- Питання: чекати далі?\n")
        with open(ops_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        assert len(check(handoff_path, ops_path, {})) == 1, check(handoff_path, ops_path, {})
        try:
            enforce(handoff_path, ops_path)
        except SystemExit as e:
            assert e.code == 1, e.code
        else:
            raise AssertionError("enforce did not refuse")
        with open(ops_path, "w", encoding="utf-8") as f:
            json.dump([{"id": "wait", "needs": "user", "ask": "Чекати далі?"}], f)
        assert check(handoff_path, ops_path, {}) == []
        assert enforce(handoff_path, ops_path) == 0
        assert check(os.path.join(d, "none.md"), ops_path) == []      # no handoff: nothing to refuse
    print("handoff_guard selftest: ok")


def main():
    a = sys.argv[1:]
    if "--selftest" in a:
        return selftest()
    if "--help" in a or "-h" in a:
        print(__doc__)
        return 0
    problems = check()
    if "--json" in a:
        json.dump({"ok": not problems, "unanswered": [{"line": l, "project": p, "add": c} for l, p, c in problems]},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0 if not problems else 1
    if problems:
        print(message(problems))
        return 1
    print("handoff: ok — every open question has an operation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
