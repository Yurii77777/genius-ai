"""install — put everything the voice needs into the project: voice/.venv and voice/models/.

    voice/bin/install [--lang uk|en|all] [--dictation] [--dry-run]

--lang       languages to install (default: "language" from genius.config.json, else all)
--dictation  also install voice input (Whisper)
--dry-run    print the plan, change nothing
Models are public on Hugging Face: no account, no token. Revisions come from voice/manifest.json.
A file already in the Hugging Face cache (or the old voice environment) is hard-linked, not
downloaded again, so it takes no extra disk space. Run doctor afterwards to see the result.
Claude asks the user before running this; the script itself never asks.
"""
import json
import os
import shutil
import subprocess
import sys

LIB = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, LIB)
import doctor  # noqa: E402

VOICE, ROOT, MODELS = doctor.VOICE, doctor.ROOT, doctor.MODELS
VENV_PY = os.path.join(VOICE, ".venv", "bin", "python")
DRY = "--dry-run" in sys.argv


def step(msg):
    print(f"\n▶ {msg}", flush=True)


def run(*args, **kw):
    print("  $ " + " ".join(args), flush=True)
    if not DRY:
        subprocess.run(args, check=True, **kw)


def link_or_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return
    src = os.path.realpath(src)                # HF snapshots are symlinks to blobs
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def hf_download(repo, filename, revision, local_dir, repo_type="model"):
    code = ("import sys; from huggingface_hub import hf_hub_download; "
            "print(hf_hub_download(repo_id=sys.argv[1], filename=sys.argv[2], revision=sys.argv[3], "
            "local_dir=sys.argv[4], repo_type=sys.argv[5]))")
    run(VENV_PY, "-c", code, repo, filename, revision, local_dir, repo_type)


TOOLS = os.path.join(VOICE, "tools")
TOOLS_BIN = os.path.join(TOOLS, "bin")
# uv keeps its Python builds and its cache inside the project, not in ~/.local/share/uv or ~/.cache
UV_ENV = dict(os.environ, UV_PYTHON_INSTALL_DIR=os.path.join(TOOLS, "python"),
              UV_CACHE_DIR=os.path.join(TOOLS, "cache"), UV_NO_CONFIG="1",
              UV_PYTHON_PREFERENCE="only-managed")       # never a Python found outside the project


def uv():
    return os.path.join(TOOLS_BIN, "uv")


def fetch_verified(url, sha256, dst):
    """Download url to dst and check its sha256 (the value pinned in voice/manifest.json)."""
    import hashlib
    import urllib.request
    print(f"  ⬇️  {url}", flush=True)
    if DRY:
        return
    tmp = dst + ".part"
    urllib.request.urlretrieve(url, tmp)
    digest = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
    if digest != sha256:
        os.remove(tmp)
        raise SystemExit(f"❌ sha256 mismatch for {url}: {digest} ≠ {sha256} — nothing installed")
    os.replace(tmp, dst)


def install_tools():
    """uv into voice/tools/bin, pinned and checked (voice/manifest.json → tools). ffmpeg is a system requirement (doctor)."""
    import platform
    import tarfile
    import zipfile
    step("Tools: voice/tools/bin (uv) — nothing global")
    os.makedirs(TOOLS_BIN, exist_ok=True)
    arch = platform.machine()
    for name, spec in doctor.read_json(os.path.join(VOICE, "manifest.json")).get("tools", {}).items():
        if not isinstance(spec, dict):
            continue                               # the "comment" entry
        target = os.path.join(TOOLS_BIN, name)
        if os.path.exists(target):
            print(f"  ✅ {name}")
            continue
        build = spec.get("builds", {}).get(arch)
        if not build or not build.get("sha256"):
            print(f"  ⚠️  {name}: no pinned build for {arch} — see voice/manifest.json; using the system one if present")
            continue
        archive = os.path.join(TOOLS, os.path.basename(build["url"].split("?")[0]) or name)
        fetch_verified(build["url"], build["sha256"], archive)
        if DRY:
            continue
        member = build["member"]
        if archive.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive) as t:
                src = t.extractfile(member)
                open(target, "wb").write(src.read())
        else:
            with zipfile.ZipFile(archive) as z:
                open(target, "wb").write(z.read(member))
        os.chmod(target, 0o755)
        os.remove(archive)
        print(f"  ✅ {name} installed")


def install_env():
    step("Environment: voice/.venv (Python 3.12, torch) from voice/requirements.lock")
    if os.path.exists(VENV_PY):
        print("  already built")
    else:
        run(uv(), "venv", "--python", "3.12", os.path.join(VOICE, ".venv"), env=UV_ENV)
    run(uv(), "pip", "sync", "--python", VENV_PY, os.path.join(VOICE, "requirements.lock"), env=UV_ENV)


def install_hf(cid, c, extra_files=()):
    target = os.path.join(MODELS, cid)
    snap = doctor.hf_cached(c)
    for f in list(c["files"]) + list(extra_files):
        dst = os.path.join(target, f)
        if os.path.exists(dst):
            print(f"  ✅ {f}")
        elif snap and os.path.exists(os.path.join(snap, f)):
            print(f"  ♻️  {f} ← cache (hard link)")
            if not DRY:
                link_or_copy(os.path.join(snap, f), dst)
        else:
            print(f"  ⬇️  {f}")
            hf_download(c["repo"], f, c["revision"], target)


def catalogue_voices(lang):
    cat = doctor.read_json(os.path.join(VOICE, "voice.json"))
    lc = (cat.get("languages") or {}).get(lang) or {}
    return [v for v in (lc.get("female"), lc.get("male")) if v]


def install_styletts_voices(lang, space):
    """Speaker vectors (2 KB each) from the patriotyk/styletts2-ukrainian space."""
    target = os.path.join(MODELS, "styletts2-uk")
    for name in catalogue_voices(lang):
        f = f"voices/{name}.pt"
        if os.path.exists(os.path.join(target, f)):
            print(f"  ✅ {f}")
            continue
        cached = os.path.join(doctor.HF_HUB, "spaces--patriotyk--styletts2-ukrainian", "snapshots",
                              space["revision"], f)
        if os.path.exists(cached):
            print(f"  ♻️  {f} ← cache (hard link)")
            if not DRY:
                link_or_copy(cached, os.path.join(target, f))
        else:
            print(f"  ⬇️  {f}")
            hf_download(space["repo"], f, space["revision"], target, "space")


def install_stanza(c):
    target = os.path.join(MODELS, "stanza")
    if os.path.isdir(os.path.join(target, c["lang"], "pos")):
        print("  ✅ already in voice/models/stanza")
        return
    src = doctor.stanza_cached(c)
    if src:
        print(f"  ♻️  {src} → voice/models/stanza (hard links)")
        if not DRY:
            shutil.copytree(src, target, copy_function=lambda s, d: link_or_copy(s, d), dirs_exist_ok=True)
        return
    run(VENV_PY, "-c", "import sys, stanza; stanza.download(sys.argv[1], processors=sys.argv[2], model_dir=sys.argv[3])",
        c["lang"], c["processors"], target)


def main():
    args = sys.argv[1:]
    cfg = doctor.read_json(os.path.join(ROOT, "genius.config.json"))
    manifest = doctor.read_json(os.path.join(VOICE, "manifest.json"))
    lang = args[args.index("--lang") + 1] if "--lang" in args else cfg.get("language", "all")
    langs = list(manifest["languages"]) if lang == "all" else [lang]
    plan = doctor.report(langs, "--dictation" in args, False)
    if plan["verdict"] == "not-enough-resources":
        sys.exit(f"Not enough resources: disk ok={plan['fits']['disk']}, RAM ok={plan['fits']['ram']}. Nothing installed.")
    print(f"Installing voice for: {', '.join(langs)}{' + dictation' if '--dictation' in args else ''}"
          f"{'   (dry run: nothing changes)' if DRY else ''}")
    print(f"To download: {plan['download_bytes'] / doctor.GB:.1f} GB (the rest is reused from the cache)")
    comps = manifest["components"]
    install_tools()
    for item in plan["components"]:
        cid, c = item["id"], comps[item["id"]]
        if item["state"] == "unsupported":
            step(f"{cid}: skipped, not for this Mac ({c.get('requires')})")
        elif c["kind"] == "venv":
            install_env()
        elif cid == "styletts2-uk":
            step(f"{cid}: {c['repo']} @ {c['revision'][:12]}")
            install_hf(cid, c)
            install_styletts_voices("uk", comps["styletts2-voices"])
        elif cid == "kokoro-en":
            step(f"{cid}: {c['repo']} @ {c['revision'][:12]}")
            install_hf(cid, c, extra_files=[f"voices/{v}.pt" for v in catalogue_voices("en")])
        elif c["kind"] == "hf":
            step(f"{cid}: {c['repo']} @ {c['revision'][:12]}")
            install_hf(cid, c)
        elif c["kind"] == "stanza":
            step(f"{cid}: stanza {c['lang']} ({c['processors']})")
            install_stanza(c)
    if not DRY:
        step("Result")
        doctor.human(doctor.report(langs, "--dictation" in args, False))


if __name__ == "__main__":
    main()
