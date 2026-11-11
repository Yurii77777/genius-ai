"""PreToolUse hook: say in a few plain words what Claude is doing right now.

Spoken through tts-server as STATUS — only when nothing else is speaking or waiting, so it never
interrupts or delays a reply. At most one line per SPEAK_STATUS_GAP seconds (default 5) per tab,
never the same line twice in a row. Silent when spoken replies are off, status is off
(voice/run/status.off), or the Mac is muted. A Bash or Agent call speaks its own
description when it is in the language of the user's request; otherwise, and for other
tools, a fixed phrase in that language (uk/en, from the UserPromptSubmit hook's lang-<session>).
Never blocks: the work is done by a detached child.
"""
import json
import os
import re
import subprocess
import sys
import time

SHARE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "run")
LIB = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, LIB)
import voice_config  # noqa: E402
GAP = float(os.environ.get("SPEAK_STATUS_GAP", "5"))

PHRASES = {
    "uk": {"Read": "Читаю файл.", "Grep": "Шукаю в коді.", "Glob": "Шукаю файли.",
           "Edit": "Вношу зміни у файл.", "Write": "Записую файл.", "MultiEdit": "Вношу зміни у файл.",
           "WebSearch": "Шукаю в інтернеті.", "WebFetch": "Читаю сторінку.",
           "TodoWrite": "Оновлюю план.", "Skill": "Відкриваю інструкцію.",
           "_bash": "Виконую команду.", "_agent": "Запускаю агента.", "_mcp": "Звертаюся до зовнішнього сервісу."},
    "en": {"Read": "Reading a file.", "Grep": "Searching the code.", "Glob": "Looking for files.",
           "Edit": "Editing a file.", "Write": "Writing a file.", "MultiEdit": "Editing a file.",
           "WebSearch": "Searching the web.", "WebFetch": "Reading a page.",
           "TodoWrite": "Updating the plan.", "Skill": "Opening the instructions.",
           "_bash": "Running a command.", "_agent": "Starting an agent.", "_mcp": "Calling an external service."},
}


def is_uk(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3


def spoken_names():
    """The «spoken» names of the projects (genius-ai/projects/<name>/project.json)."""
    root = os.path.join(os.path.dirname(os.path.dirname(LIB)), "projects")
    names = []
    for name in os.listdir(root) if os.path.isdir(root) else []:
        try:
            spoken = json.load(open(os.path.join(root, name, "project.json"))).get("spoken")
        except (OSError, ValueError):
            continue
        if spoken and not spoken.startswith("<"):
            names.append(spoken)
    return names


def important(text):
    """An important background event starts with a project's spoken name and a colon."""
    return any(text.startswith(f"{n}:") for n in spoken_names())


def phrase(hook, lang):
    """The status in the language of the user's request (set by the UserPromptSubmit hook)."""
    words = PHRASES[lang]
    tool = hook.get("tool_name", "")
    args = hook.get("tool_input") or {}
    if tool in ("Bash", "Agent", "Task"):
        desc = (args.get("description") or "").strip()
        if desc and is_uk(desc) == (lang == "uk"):          # spoken only in the request's language
            return desc.rstrip(".") + "."
        return words["_bash" if tool == "Bash" else "_agent"]
    if tool.startswith("mcp__"):
        return words["_mcp"]
    return words.get(tool)


def main():
    hook = json.load(sys.stdin)
    cfg = voice_config.load()
    if os.path.exists(os.path.join(SHARE, "speak.off")) or os.path.exists(os.path.join(SHARE, "status.off")) \
            or not voice_config.speaking(cfg) or not cfg["status"]:
        return
    session = str(hook.get("session_id") or "unknown")
    try:
        lang = open(os.path.join(SHARE, f"lang-{session}")).read().strip() or "uk"
    except OSError:
        lang = "uk"
    text = phrase(hook, lang if lang in PHRASES else "uk")
    if not text:
        return
    # more than one project: routine steps are spam. Speak only an important background event:
    # a Bash/Agent description that starts with a project's spoken name, «Сайт: CI зелений» (AGENTS.md)
    if len(voice_config._read(voice_config.USER_CONFIG).get("projects") or []) > 1 \
            and not important(text):
        return
    state_file = os.path.join(SHARE, f"status-{session}.json")
    try:
        state = json.load(open(state_file))
    except (OSError, ValueError):
        state = {}
    now = time.time()
    if now - state.get("t", 0) < GAP or state.get("last") == text:
        return
    json.dump({"t": now, "last": text}, open(state_file, "w"))
    if subprocess.run(["osascript", "-e", "output muted of (get volume settings)"],
                      capture_output=True, text=True).stdout.strip() == "true":
        return
    sock = os.path.join(SHARE, "tts.sock")
    if not os.path.exists(sock):
        return                                    # a status never starts the voice by itself
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(1)
        c.connect(sock)
        c.sendall(f"STATUS {session}\t{text}\n".encode())


if __name__ == "__main__":
    data = sys.stdin.read()
    if os.fork() == 0:                            # detach: the tool call never waits for us
        os.setsid()
        try:
            sys.stdin = __import__("io").StringIO(data)
            main()
        except Exception:
            pass
        os._exit(0)
