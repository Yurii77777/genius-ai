"""Voice settings.

voice/voice.json      — the catalogue: engine, speed, female and male voice for every language.
genius.config.json    — the user's choice: "language", "gender" (top level: they drive both the voice
                        and the grammatical gender of Claude's text) and "voice": {"enabled", "status"}.
                        Empty after a clone; onboarding fills it on the first run.
Speaks by default, also before onboarding; silent only after `speak off` or "enabled": false.
Language of a text is detected by script: Cyrillic -> uk, otherwise en.
"""
import json
import os

LIB = os.path.dirname(os.path.realpath(__file__))
VOICE_DIR = os.path.dirname(LIB)
ROOT = os.path.dirname(VOICE_DIR)
CATALOGUE = os.path.join(VOICE_DIR, "voice.json")
USER_CONFIG = os.path.join(ROOT, "genius.config.json")
USER_DEFAULT = {"gender": "female", "enabled": True, "confirmed": False, "status": True}


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load():
    cfg = _read(CATALOGUE)
    cfg.setdefault("default_language", "uk")
    cfg.setdefault("languages", {})
    user = _read(USER_CONFIG)
    cfg.update(USER_DEFAULT, **(user.get("voice") or {}))
    cfg["gender"] = user.get("gender") or cfg["gender"]
    return cfg


def save_user(**changes):
    root = _read(USER_CONFIG)
    root["voice"] = dict(root.get("voice") or {}, **changes)
    with open(USER_CONFIG, "w", encoding="utf-8") as f:
        json.dump(root, f, ensure_ascii=False, indent=2)
        f.write("\n")


def detect(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return load()["default_language"]
    cyr = sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters)
    return "uk" if cyr > 0.3 else "en"


def pick(lang, cfg=None):
    """(engine, voice, speed, say_voice) for a language and the current gender."""
    cfg = cfg or load()
    gender = cfg.get("gender", "female")
    lc = cfg["languages"].get(lang) or cfg["languages"][cfg["default_language"]]
    return lc["engine"], lc.get(gender) or lc["female"], float(lc.get("speed", 1.0)), \
        (lc.get("say") or {}).get(gender)


if __name__ == "__main__":            # tiny CLI for the `speak` shell script
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "enabled":                              # exit 0 when spoken replies are on
        sys.exit(0 if load()["enabled"] else 1)
    if cmd == "gender" and len(sys.argv) > 2:
        if sys.argv[2] not in ("female", "male"):
            sys.exit("gender: female | male")
        root = _read(USER_CONFIG)
        root["gender"] = sys.argv[2]
        (root.get("voice") or {}).pop("gender", None)
        with open(USER_CONFIG, "w", encoding="utf-8") as f:
            json.dump(root, f, ensure_ascii=False, indent=2)
            f.write("\n")
    elif cmd == "flag" and len(sys.argv) > 3:         # flag enabled|status on|off
        save_user(**{sys.argv[2]: sys.argv[3] == "on"})
    elif cmd == "set" and len(sys.argv) > 4:          # set <lang> <female|male> <voice> (catalogue)
        cat = _read(CATALOGUE)
        cat.setdefault("languages", {}).setdefault(sys.argv[2], {})[sys.argv[3]] = sys.argv[4]
        with open(CATALOGUE, "w", encoding="utf-8") as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
            f.write("\n")
    cfg = load()
    g = cfg["gender"]
    print(f"gender: {g}   spoken replies: {'on' if cfg['enabled'] else 'off'}   status: {'on' if cfg['status'] else 'off'}")
    for lang, lc in cfg["languages"].items():
        print(f"  {lang} ({lc['engine']}): female={lc.get('female')}  male={lc.get('male')}  ← {lc.get(g)}")


def speaking(cfg=None):
    """Spoken replies are on unless the user turned them off (`speak off`, onboarding choice 2)."""
    cfg = cfg or load()
    return bool(cfg.get("enabled"))
