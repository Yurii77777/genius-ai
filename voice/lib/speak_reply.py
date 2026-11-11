"""Claude Code Stop hook: speak the reply that just finished.

Reads the hook JSON on stdin, takes the last assistant text of the turn (from
`last_assistant_message` when the hook gets it, else from the transcript), strips what
cannot be listened to — code blocks, tables, paths, URLs, markdown marks — keeps the
first paragraphs up to SPEAK_MAX characters (default 600) and hands it to `speak`.
Never blocks the session: any failure is silent, the speech runs detached.
"""
import json
import os
import re
import subprocess
import sys

MAX = int(os.environ.get("SPEAK_MAX", "600"))
SHARE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "run")            # runtime state (lang-<session>)
LIB = os.path.dirname(os.path.realpath(__file__))
SPEAK = os.path.join(os.path.dirname(LIB), "bin", "speak")
sys.path.insert(0, LIB)
import voice_config  # noqa: E402


def last_reply(hook):
    if isinstance(hook.get("last_assistant_message"), str):
        return hook["last_assistant_message"]
    path = hook.get("transcript_path")
    if not path or not os.path.exists(path):
        return ""
    last = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            for block in (e.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                    last = block["text"]
    return last


def listenable(md):
    md = re.sub(r"```.*?```", " ", md, flags=re.S)                 # code blocks
    lines = []
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("|") or re.fullmatch(r"[-|: ]+", s or "x"):  # tables
            continue
        lines.append(ln)
    t = "\n".join(lines)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)                  # links → their text
    t = re.sub(r"https?://\S+", " ", t)                              # bare URLs
    t = re.sub(r"`([^`]*)`", lambda m: "" if re.search(r"[/~\\.$=]|\w_\w", m.group(1)) else m.group(1), t)
    t = re.sub(r"(?m)^\s*#+\s*", "", t)                              # headings
    t = re.sub(r"(?m)^\s*([-*•]|\d+\.)\s+", "", t)                   # list marks
    t = re.sub(r"[*_~>#]", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    out = ""
    for p in paras:
        if out and len(out) + len(p) > MAX:
            break
        joined = ""
        for ln in p.split("\n"):
            ln = re.sub(r"^[\s—–-]+", "", ln).strip()           # dashes left by removed code
            if not ln:
                continue
            sep = "" if not joined else (" " if re.search(r"[.:;!?,]$", joined) else ". ")
            joined += sep + ln
        out += (" " if out else "") + joined
    if len(out) > MAX:                                               # one long paragraph
        cut = out[:MAX]
        out = cut[: max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), 1) + 1]
    return out.strip()


def main():
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return
    session = str(hook.get("session_id") or "unknown")
    off = os.path.exists(os.path.join(SHARE, "speak.off")) or not voice_config.speaking() or subprocess.run(
        ["osascript", "-e", "output muted of (get volume settings)"],
        capture_output=True, text=True).stdout.strip() == "true"       # the system mute key
    if len(sys.argv) > 1 and sys.argv[1] == "stop":     # UserPromptSubmit: hush only this tab
        # remember the language of the request: statuses of this turn are spoken in it
        prompt = hook.get("prompt") or ""
        letters = [c for c in prompt if c.isalpha()]
        if letters:
            uk = sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3
            with open(os.path.join(SHARE, f"lang-{session}"), "w") as f:
                f.write("uk" if uk else "en")
        subprocess.run([SPEAK, "stop", session],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if off or hook.get("stop_hook_active"):
        return
    reply = last_reply(hook)
    # The reply's own spoken gist: a line starting with 🔊 (the user wants the dry essence in
    # plain words, never the whole reply). Without one, only the first sentence is spoken.
    gist = re.findall(r"(?m)^\s*🔊\s*(.+?)\s*$", reply)
    if gist:
        text = listenable(gist[-1])
    else:
        first = listenable(reply)
        text = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0][:200]
    if text:
        subprocess.Popen([SPEAK, "--from", session, text],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
