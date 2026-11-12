#!/usr/bin/env python3
"""SessionStart hook: read genius.config.json and inject monitoring context.

Empty config  -> tell Claude to start onboarding (ask for projects and services).
Filled config -> give Claude a status snapshot of every connected project.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
CONFIG = ROOT / "genius.config.json"


def git(path: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def project_status(p: dict) -> str:
    name = p.get("name") or p.get("path", "?")
    path = Path(os.path.expanduser(p.get("path", "")))
    if not path.is_dir():
        return f"- {name}: PATH NOT FOUND ({path})"
    if not git(path, "rev-parse", "--is-inside-work-tree"):
        return f"- {name}: {path} (not a git repo)"
    branch = git(path, "branch", "--show-current") or "detached"
    dirty = git(path, "status", "--porcelain")
    changes = len(dirty.splitlines()) if dirty else 0
    last = git(path, "log", "-1", "--format=%cr: %s") or "no commits"
    ahead_behind = git(path, "rev-list", "--left-right", "--count", "@{u}...HEAD")
    sync = ""
    if ahead_behind:
        behind, ahead = ahead_behind.split()
        sync = f", ahead {ahead}, behind {behind}"
    return f"- {name}: {path} | branch {branch} | {changes} uncommitted{sync} | last commit {last}"


def dictation_ready(cfg: dict) -> bool:
    """Start the F5 (🎤) listener from the project (voice/bin/dictate-setup) and say whether it runs.

    Nothing global keeps it alive: a reboot or logout clears it, and this hook starts it again.
    """
    if (cfg.get("voice") or {}).get("enabled") is False:
        return False
    try:
        out = subprocess.run(
            [str(ROOT / "voice/bin/dictate-setup"), "start", "--quiet"],
            capture_output=True, text=True, timeout=25,
        )
        return out.returncode == 0
    except Exception:
        return False


def how_to_start(language: str | None, dictation: bool) -> str:
    """The first thing the user sees: what to do now."""
    uk = ("👉 Натисніть F5 🎤 і скажіть «погнали» — або напишіть це й натисніть Enter."
          if dictation else
          "👉 Напишіть «погнали» й натисніть Enter."
          "\n   Голосовий ввід (F5) ще не встановлено: voice/bin/dictate-setup")
    en = ("👉 Press F5 🎤 and say \"let's go\" — or type it and press Enter."
          if dictation else
          "👉 Type \"let's go\" and press Enter."
          "\n   Voice input (F5) is not installed yet: voice/bin/dictate-setup")
    if language == "uk":
        return uk
    if language == "en":
        return en
    return en + "\n" + uk


def speak_hint(cfg: dict, dictation: bool) -> None:
    """Say the hint aloud unless spoken replies are off (`speak off`)."""
    if (cfg.get("voice") or {}).get("enabled") is False:
        return
    if cfg.get("language") == "en":
        text = "Press F5 and speak, or type a message." if dictation else "Type a message to start."
    else:
        text = "Натисніть F5 і говоріть, або напишіть повідомлення." if dictation else "Напишіть повідомлення, щоб почати."
    try:
        subprocess.Popen([str(ROOT / "voice/bin/speak"), text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def load_config() -> tuple[dict, str]:
    if not CONFIG.exists():
        return {}, ""                                       # a fresh clone: onboarding from step 1
    try:
        return json.loads(CONFIG.read_text() or "{}"), ""
    except json.JSONDecodeError as e:
        return {"projects": [], "services": []}, f"config is invalid JSON: {e}"


STEPS = {"START": 1, "VOICE": 2, "TEXT": 3, "TONE": 3, "PROJECTS": 4, "SERVICES": 5, "FINISH": 6}
OPERATIONS = ROOT / "state" / "operations.json"


def pending_operations() -> str:
    """Long operations that an earlier session started and did not finish (AGENTS.md «Pending operations»)."""
    try:
        ops = json.loads(OPERATIONS.read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return ""
    if not ops:
        return ""
    lines = ["[genius-ai] PENDING OPERATIONS from earlier sessions. Report them FIRST, check each one's "
             "current state with its `check`, then continue or close it (AGENTS.md «Pending operations»):"]
    for op in ops:
        lines.append(f"- [{op.get('project', '?')}] {op.get('what', '?')} — waiting for: "
                     f"{op.get('waiting_for', '?')}; check: `{op.get('check', '')}`; next: {op.get('next', '?')}; "
                     f"started {op.get('started', '?')}")
    return "\n".join(lines)


LIVE_PROMPT = ("[genius-ai live control] Run `bin/status` in this folder and the `check` of every entry in "
               "state/operations.json; compare with the last state you reported; report changes first with the "
               "project tag and do the next approved step (AGENTS.md «Live control of all projects»).")


def herdr_panel() -> bool:
    """The genius-ai status shelves are installed in Herdr for this folder (bin/genius herdr install)."""
    herdr = Path(os.environ.get("HERDR_APP_HOME") or Path.home() / ".config" / "herdr-app")
    try:                                   # keep the installed copy current: herdr/ or Herdr's layout changed
        subprocess.run(["/usr/bin/python3", str(ROOT / "bin/lib/herdr.py"), "sync"], capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        return json.loads((herdr / "mods" / "genius-status" / "root.json").read_text()).get("root") == str(ROOT)
    except (OSError, ValueError):
        return False


def live_control(projects: list) -> str:
    """Every project in the config is live from the first session on (AGENTS.md «Live control»)."""
    if not projects:
        return ""
    try:
        out = subprocess.run([str(ROOT / "bin" / "status"), "--line"], capture_output=True, text=True,
                             timeout=25).stdout.strip()
    except Exception:
        out = ""
    return ("[genius-ai] LIVE CONTROL. Live state of every project in the config:\n"
            + (out or "- bin/status gave no output") +
            "\nIn your first reply: (1) schedule the live-control job with CronCreate, cron \"7,22,37,52 * * * *\", "
            f"recurring, prompt: {json.dumps(LIVE_PROMPT, ensure_ascii=False)} — every session, it dies with the "
            "session; " + ("(2) the status is on the Herdr panel: never end a reply with status lines."
                           if herdr_panel() else
                           "(2) end every reply with one status line per project (`bin/status --line`)."))


COPIED = (
    "[genius-ai] COPIED CONFIG. genius.config.json was made in another folder or on another Mac "
    "(made: {made}; here: {here}). Do NOT use it: no persona, no projects, no prod access, no pending "
    "operations from it — it may belong to someone else. In your first reply, before anything else, write "
    "neutrally in both languages (English first, Ukrainian below) and ask ONE question:\n"
    "«This folder holds someone's genius-ai settings from {made_path} ({made_host}). "
    "1 — it is mine, continue here · 2 — start fresh (the old settings go to backup/, nothing is deleted)» / "
    "«У цій папці налаштування genius-ai з {made_path} ({made_host}). 1 — це мої, продовжити тут · "
    "2 — почати з нуля (старе піде в backup/, нічого не видаляється)».\n"
    "1 → run `bin/genius adopt`, then tell the user that the next session starts normally (or continue now "
    "by reading the config). 2 → run `bin/genius new`, then start ONBOARDING from step 1 (AGENTS.md)."
)


def install_check(cfg: dict) -> str | None:
    """None = go on; else the context that replaces everything (AGENTS.md «Fresh start or continue»)."""
    sys.path.insert(0, str(ROOT / "bin" / "lib"))
    try:
        import install                                      # bin/lib/install.py
    except Exception:
        return None
    state = install.binding(cfg)
    if state == "unbound":                                  # an old config: the first session binds it here
        install.write_config(install.bind(cfg))
        return None
    if state != "copied":
        return None
    made, now = cfg.get("install") or {}, install.here()
    return COPIED.format(made=json.dumps(made, ensure_ascii=False), here=json.dumps(now, ensure_ascii=False),
                         made_path=made.get("path", "?"), made_host=made.get("host", "?"))


def main() -> None:
    cfg, error = load_config()
    copied = None if error else install_check(cfg)
    if copied:
        json.dump({
            "systemMessage": "genius-ai: налаштування з іншої папки чи іншого Mac — спершу питання «продовжити чи з нуля» / "
                             "settings from another folder or Mac — first the question «continue or start fresh»",
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": copied},
        }, sys.stdout, ensure_ascii=False)
        return
    projects = cfg.get("projects") or []
    services = cfg.get("services") or []

    language, gender = cfg.get("language"), cfg.get("gender")
    persona = ""
    if language and gender:
        tone = cfg.get("tone") or {}
        dials = ", ".join(f"{k}={tone[k]}" for k in ("humor", "directness", "detail") if k in tone)
        persona = (f"[genius-ai] PERSONA: language={language}, gender={gender}, tone={tone.get('preset', 'not chosen')}"
                   + (f" ({dials}; 0–2, see persona/tones.json)" if dials else "")
                   + (f" (user's words: {tone['notes']})" if tone.get("notes") else "") +
                   ". Write every reply in this language, grammatical gender and tone (see Persona in AGENTS.md).\n")

    voice = cfg.get("voice")
    if voice is None:
        missing = "START"                                   # step 1: voice or text
    elif voice.get("enabled") and not (voice.get("confirmed") and language and gender):
        missing = "VOICE"                                   # step 2: install, listen, choose, confirm
    elif not voice.get("enabled") and not (language and gender):
        missing = "TEXT"                                    # step 3: language and gender for text
    elif not cfg.get("tone"):
        missing = "TONE"                                    # step 3b: how to talk
    elif not projects:
        missing = "PROJECTS"                                # step 4: where to look, which, study each
    elif "services" not in cfg:
        missing = "SERVICES"                                # step 5: [] is a valid answer («none»)
    elif not cfg.get("onboarded"):
        missing = "FINISH"                                  # steps 6-8: prod access, backup, confirm
    else:
        missing = None

    if error or missing:
        context = persona + (
            f"[genius-ai] ONBOARDING MODE, next step: {missing or 'START'} "
            f"(step {STEPS.get(missing or 'START', 1)} of 6)"
            + (f" (config error: {error})" if error else "")
            + ". In your first reply, before anything else: greet briefly and continue onboarding "
            "from that step, as described in AGENTS.md."
        )
        banner = {"START": "genius-ai: перший запуск — голос чи текст? / first run — voice or text?",
                  "VOICE": "genius-ai: налаштування голосу",
                  "TEXT": "genius-ai: вибір мови",
                  "TONE": "genius-ai: тон спілкування",
                  "PROJECTS": "genius-ai: підключення проєктів",
                  "SERVICES": "genius-ai: підключення сервісів",
                  "FINISH": "genius-ai: доступи, резервна копія, підтвердження"}.get(missing, "genius-ai: налаштування")
    else:
        lines = ["[genius-ai] MONITORING MODE. Snapshot at session start:", "Projects:"]
        lines += [project_status(p) for p in projects] or ["- none"]
        lines.append("Services:")
        lines += [f"- {s.get('name', '?')} ({s.get('type', '?')})" for s in services] or ["- none"]
        lines.append(
            "In your first reply: give the user a short status report from this snapshot "
            "and run the service checks described in AGENTS.md. Flag problems first."
        )
        context = persona + "\n".join(lines)
        banner = f"genius-ai: {len(projects)} проєкт(ів), {len(services)} сервіс(ів) під моніторингом"

    live = live_control(projects)
    if live:
        context = context + "\n" + live

    handoff = ROOT / "state" / "handoff.md"
    if handoff.exists():                                # the summary the last session left (AGENTS.md)
        context = ("[genius-ai] HANDOFF from the last session (state/handoff.md) — start from it:\n"
                   + handoff.read_text()[:4000] + "\n" + context)

    ops = pending_operations()
    if ops:
        context = ops + "\n" + context
        banner = f"genius-ai: незавершених операцій — {ops.count(chr(10) + '- ')}\n" + banner

    dictation = dictation_ready(cfg)
    banner = banner + "\n" + how_to_start(language, dictation)
    speak_hint(cfg, dictation)

    json.dump({
        "systemMessage": banner,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
