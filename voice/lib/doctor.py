"""doctor — what this Mac has for genius-ai's voice, and what is missing. Changes nothing.

    voice/bin/doctor [--lang uk|en|all] [--dictation] [--online] [--json]

--lang       languages to check (default: "language" from genius.config.json, else all)
--dictation  also check voice input: Whisper, and the macOS permissions of the F5 listener
             (Full Disk Access, Microphone, Accessibility, Automation; voice/lib/dictate_perms.py)
--online     ask Hugging Face whether each model is public and ungated (needs network)
--json       machine-readable output (used by the SessionStart hook)
Standard library only: it must run on a fresh Mac before anything is installed.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

LIB = os.path.dirname(os.path.realpath(__file__))
VOICE = os.path.dirname(LIB)
ROOT = os.path.dirname(VOICE)
MODELS = os.path.join(VOICE, "models")
HF_HUB = os.path.join(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")), "hub")
GB = 1024 ** 3
# the voice environment used before genius-ai had its own (only a source to copy from)
# (optional, set by the owner of such a folder; a fresh install has none)
LEGACY_VOICEOVER = os.environ.get("SPEAK_VOICEOVER_DIR", "")


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def machine():
    ram = int(sh("sysctl", "-n", "hw.memsize") or 0)
    vol = sh("osascript", "-e", "get volume settings")      # "output volume:50, …, output muted:false"
    settings = dict(p.strip().split(":", 1) for p in vol.split(",") if ":" in p)
    says = sh("say", "-v", "?")
    return {
        "chip": sh("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor(),
        "arch": platform.machine(),
        "macos": platform.mac_ver()[0],
        "ram": ram,
        "disk_free": shutil.disk_usage(ROOT).free,
        "audio": {
            "muted": settings.get("output muted") == "true",
            "volume": int(settings["output volume"]) if settings.get("output volume", "").isdigit() else None,
        },
        "system_voice": {"uk": "uk_UA" in says, "en": "en_US" in says or "en_GB" in says},
        "tools": {t: bool(shutil.which(t)) for t in ("uv", "ffmpeg", "swiftc")},
        "system": system_checks(),
    }


TOOLS_BIN = os.path.join(VOICE, "tools", "bin")


def system_checks():
    """Everything genius-ai needs from this computer, each with its exact fix (AGENTS.md «Lead the user»)."""
    def tool(name):
        local = os.path.join(TOOLS_BIN, name)
        if os.access(local, os.X_OK):
            return "project"
        return "system" if shutil.which(name) else None
    darwin = platform.system() == "Darwin"
    clt = bool(sh("xcode-select", "-p")) if darwin else False
    checks = [
        ("macOS", darwin, "голос і диктування працюють лише на macOS; на іншій системі — режим «лише текст»", True),
        ("Xcode Command Line Tools", clt, "xcode-select --install   (вікно Apple → «Встановити», ≈ 10 хв)", True),
        ("python3", bool(shutil.which("python3")), "приходить разом із Xcode Command Line Tools", True),
        ("git", bool(shutil.which("git")), "приходить разом із Xcode Command Line Tools", True),
        ("swiftc (слухач F5)", bool(shutil.which("swiftc")), "приходить разом із Xcode Command Line Tools", False),
        ("uv", tool("uv"), "voice/bin/install — ставить uv у voice/tools/bin", False),
        ("ffmpeg (диктування F5)", tool("ffmpeg"),
         "встановіть ffmpeg: brew install ffmpeg   (Homebrew: https://brew.sh); без нього працює все, крім диктування", False),
        ("gh (GitHub)", bool(shutil.which("gh")), "потрібен лише для сервісу GitHub", False),
    ]
    return [{"name": n, "ok": bool(v), "where": v if isinstance(v, str) else None, "fix": f, "blocker": b}
            for n, v, f, b in checks]


def hf_cached(c):
    """Snapshot of the pinned revision in the shared Hugging Face cache (reusable without download)."""
    for repo in [c["repo"]] + c.get("aliases", []):          # a renamed repo keeps its old cache folder
        snap = os.path.join(HF_HUB, "models--" + repo.replace("/", "--"), "snapshots", c["revision"])
        if all(os.path.exists(os.path.join(snap, f)) for f in c["files"]):
            return snap
    return None


def stanza_cached(c):
    for base in (os.path.join(MODELS, "stanza"), os.environ.get("STANZA_RESOURCES_DIR", ""),
                 os.path.expanduser("~/stanza_resources"),
                 os.path.join(LEGACY_VOICEOVER, ".cache", "stanza") if LEGACY_VOICEOVER else ""):
        if base and os.path.isdir(os.path.join(base, c["lang"], "pos")):
            return base
    return None


def component_state(cid, c):
    if c["kind"] == "venv":
        py = os.path.join(ROOT, c["path"], "bin", "python")
        return ("installed", py) if os.path.exists(py) else ("missing", None)
    if c["kind"] == "hf":
        local = os.path.join(MODELS, cid)
        if all(os.path.exists(os.path.join(local, f)) for f in c["files"]):
            return "installed", local
        snap = hf_cached(c)
        return ("in-cache", snap) if snap else ("missing", None)
    if c["kind"] == "stanza":
        base = stanza_cached(c)
        if base and base.startswith(MODELS):
            return "installed", base
        return ("in-cache", base) if base else ("missing", None)
    return "unknown", None


def online(c):
    if c["kind"] != "hf":
        return None
    try:
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{c['repo']}", timeout=10) as r:
            d = json.load(r)
        return {"public": not d.get("private"), "gated": bool(d.get("gated"))}
    except Exception as e:
        return {"error": str(e)}


def report(langs, dictation, check_online):
    manifest = read_json(os.path.join(VOICE, "manifest.json"))
    m = machine()
    wanted = []
    for lang in langs:
        wanted += manifest["languages"][lang]["components"]
    if dictation:
        wanted += manifest["optional"]["dictation"]["components"]
    wanted = list(dict.fromkeys(wanted))                       # keep order, drop repeats
    comps, download, ram_need = [], 0, 0
    for cid in wanted:
        c = manifest["components"][cid]
        state, where = component_state(cid, c)
        item = {"id": cid, "state": state, "size": c.get("size", 0), "where": where,
                "license": c.get("license")}
        if c.get("requires") and c["requires"] != m["arch"]:
            item["state"] = "unsupported"
        if check_online:
            item["online"] = online(c)
        if item["state"] == "missing":
            download += item["size"]
        ram_need += c.get("ram", 0)
        comps.append(item)
    disk_ok = m["disk_free"] > download * 1.2 + 2 * GB
    ram_ok = m["ram"] >= ram_need + 4 * GB
    perms = None
    if dictation and platform.system() == "Darwin":
        sys.path.insert(0, LIB)
        import dictate_perms                                  # read-only: launchctl print + the listener's report
        perms = dictate_perms.checks()
    can_speak_now = all(x["state"] in ("installed", "in-cache") for x in comps if x["id"] != "whisper")
    return {
        "languages": langs, "machine": m, "components": comps,
        "dictation_permissions": perms,
        "download_bytes": download, "ram_needed": ram_need,
        "fits": {"disk": disk_ok, "ram": ram_ok},
        "can_speak_now": can_speak_now and not m["audio"]["muted"],
        "blockers": [c["name"] for c in m["system"] if c["blocker"] and not c["ok"]],
        "verdict": ("system-incomplete" if any(c["blocker"] and not c["ok"] for c in m["system"])
                    else "ready" if can_speak_now else "install-possible" if disk_ok and ram_ok else "not-enough-resources"),
    }


def human(r):
    m = r["machine"]
    g = lambda b: f"{b / GB:.1f} GB"
    print(f"Mac:    {m['chip']} ({m['arch']}), macOS {m['macos']}, RAM {g(m['ram'])}, free disk {g(m['disk_free'])}")
    a = m["audio"]
    print(f"Sound:  {'MUTED' if a['muted'] else 'on'}, volume {a['volume']}")
    print("System voices: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in m["system_voice"].items()))
    print("\nSystem:")
    for c in m["system"]:
        mark = "✅" if c["ok"] else ("⛔" if c["blocker"] else "⚠️ ")
        where = f" ({c['where']})" if c["where"] else ""
        print(f"  {mark} {c['name']}{where}" + ("" if c["ok"] else f"\n       → {c['fix']}"))
    print(f"\nComponents for {', '.join(r['languages'])}:")
    marks = {"installed": "✅ installed", "in-cache": "♻️  in cache (no download)", "missing": "⬇️  missing",
             "unsupported": "⛔ not for this Mac", "unknown": "?"}
    for c in r["components"]:
        extra = f"  online={c['online']}" if "online" in c else ""
        print(f"  {c['id']:14} {marks[c['state']]:30} {g(c['size']):>8}  {c['license'] or ''}{extra}")
    if r.get("dictation_permissions") is not None:
        print("\nF5 dictation (permissions of voice/build/dictate-hotkey; self-test: voice/bin/dictate-test):")
        import dictate_perms
        dictate_perms.human(r["dictation_permissions"])
    print(f"\nTo download: {g(r['download_bytes'])}   RAM while speaking: ≈{g(r['ram_needed'])}")
    print(f"Fits: disk={'yes' if r['fits']['disk'] else 'NO'}, RAM={'yes' if r['fits']['ram'] else 'NO'}")
    print(f"Verdict: {r['verdict']}   can speak now: {'yes' if r['can_speak_now'] else 'no'}")


def main():
    args = sys.argv[1:]
    cfg = read_json(os.path.join(ROOT, "genius.config.json"))
    manifest = read_json(os.path.join(VOICE, "manifest.json"))
    lang = args[args.index("--lang") + 1] if "--lang" in args else cfg.get("language", "all")
    langs = list(manifest["languages"]) if lang == "all" else [lang]
    r = report(langs, "--dictation" in args, "--online" in args)
    if "--json" in args:
        json.dump(r, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        human(r)


if __name__ == "__main__":
    main()
