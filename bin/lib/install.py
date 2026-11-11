#!/usr/bin/env python3
"""Whose personal data is this? — the install identity of a genius-ai folder.

A copy of the folder (Finder copy, `cp -R`, a zip sent to someone) carries the
personal data with it: genius.config.json, projects/<name>/, state/. A git clone
does not (they are in .gitignore). To tell a copy from the owner's own folder,
onboarding writes `install: {id, path, host, created}` into the config, and the
SessionStart hook compares it with the folder and the Mac it runs on
(AGENTS.md «Product and personal data» → «Fresh start or continue»).

Used by .claude/hooks/session_start.py and by `bin/genius save|restore|new|adopt`.
"""
from __future__ import annotations

import json
import platform
import shutil
import sys
import tarfile
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "genius.config.json"
BACKUP = ROOT / "backup"

# Personal data of this user (AGENTS.md «Product and personal data»). Runtime and
# downloads (voice/.venv, voice/models, voice/run, voice/build, voice/tools) are
# machine-local and re-created by voice/bin/install, so they are never saved or moved.
PERSONAL = ("genius.config.json", "state")


def personal_paths(root: Path = ROOT) -> list[Path]:
    """The personal files and folders that exist now, project rules included."""
    found = [root / p for p in PERSONAL if (root / p).exists()]
    projects = root / "projects"
    if projects.is_dir():
        found += sorted(p for p in projects.iterdir() if p.name != "_template" and not p.name.startswith("."))
    return found


def here() -> dict:
    return {"path": str(ROOT.resolve()), "host": platform.node()}


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def write_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def binding(cfg: dict) -> str:
    """'empty' — nothing personal yet; 'unbound' — an old config without `install`;
    'ours' — made in this folder on this Mac; 'copied' — made elsewhere."""
    if not cfg:
        return "empty"
    inst = cfg.get("install")
    if not isinstance(inst, dict):
        return "unbound"
    now = here()
    return "ours" if inst.get("path") == now["path"] and inst.get("host") == now["host"] else "copied"


def bind(cfg: dict) -> dict:
    """Bind the config to this folder and Mac; keep the id if there is one (the same install moved)."""
    inst = cfg.get("install") if isinstance(cfg.get("install"), dict) else {}
    cfg["install"] = {
        "id": inst.get("id") or str(uuid.uuid4()),
        **here(),
        "created": inst.get("created") or datetime.now().astimezone().isoformat(timespec="seconds"),
        "bound": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return cfg


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def move_personal_to_backup() -> Path | None:
    """Move (never delete) the personal data into backup/<stamp>/; return that folder."""
    items = personal_paths()
    if not items:
        return None
    dest = BACKUP / stamp()
    for item in items:
        target = dest / item.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(target))
    return dest


# ── bin/genius subcommands ────────────────────────────────────────────────────

def cmd_adopt() -> int:
    cfg = read_config()
    if not cfg:
        print("Конфіга немає — нічого прив'язувати. / No config — nothing to bind.")
        return 1
    was = binding(cfg)
    write_config(bind(cfg))
    print(f"✅ Установку прив'язано до цієї папки й цього Mac (було: {was}). / Bound to this folder and Mac.")
    return 0


def cmd_new() -> int:
    dest = move_personal_to_backup()
    if dest is None:
        print("Особистих даних немає — це вже чистий старт. / Already a clean start.")
        return 0
    print(f"✅ Чистий старт. Попередні особисті дані перенесено в {dest.relative_to(ROOT)}/ (нічого не видалено).")
    print(f"   Clean start. Previous personal data moved to {dest.relative_to(ROOT)}/ (nothing deleted).")
    return 0


def cmd_save(target: str | None) -> int:
    if not target:
        print("Вкажіть папку для збереження: bin/genius save <папка> / Give a folder: bin/genius save <folder>")
        return 2
    out_dir = Path(target).expanduser().resolve()
    if out_dir == ROOT or ROOT in out_dir.parents:
        print("Збереження не можна класти всередину genius-ai. / Do not save inside genius-ai.")
        return 2
    items = personal_paths()
    if not items:
        print("Особистих даних немає — нічого зберігати. / Nothing to save.")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"genius-ai-save_{stamp()}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for item in items:
            tar.add(item, arcname=str(item.relative_to(ROOT)))
    print(f"✅ Збережено: {archive}")
    print("   Там конфіг, правила проєктів і стан. Тримайте архів у приватному місці.")
    print("   Contains the config, project rules and state. Keep it private.")
    return 0


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    allowed = {p for p in PERSONAL} | {"projects"}
    members = []
    for m in tar.getmembers():
        name = Path(m.name)
        if name.is_absolute() or ".." in name.parts or name.parts[0] not in allowed:
            raise ValueError(f"unexpected path in the archive: {m.name}")
        if m.issym() or m.islnk():
            raise ValueError(f"links are not allowed in a save: {m.name}")
        members.append(m)
    return members


def cmd_restore(source: str | None, force: bool) -> int:
    if not source:
        print("Вкажіть архів: bin/genius restore <файл.tar.gz> / Give an archive.")
        return 2
    archive = Path(source).expanduser()
    if not archive.is_file():
        print(f"Немає файлу {archive}")
        return 2
    if personal_paths() and not force:
        print("Тут уже є особисті дані. Спершу `bin/genius new` або додайте --force (старе піде в backup/).")
        print("Personal data exists here. Run `bin/genius new` first, or add --force (old data goes to backup/).")
        return 1
    with tarfile.open(archive, "r:gz") as tar:
        members = _safe_members(tar)
        backup = move_personal_to_backup() if force else None
        tar.extractall(ROOT, members=members)
    cfg = read_config()
    if cfg:
        write_config(bind(cfg))
    print(f"✅ Відновлено з {archive.name} і прив'язано до цієї папки." + (f" Старе — у {backup.relative_to(ROOT)}/." if backup else ""))
    print("   Restored and bound to this folder. Voice models: run voice/bin/install if needed.")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    args = [a for a in argv[1:] if not a.startswith("--")]
    if cmd == "adopt":
        return cmd_adopt()
    if cmd == "new":
        return cmd_new()
    if cmd == "save":
        return cmd_save(args[0] if args else None)
    if cmd == "restore":
        return cmd_restore(args[0] if args else None, "--force" in argv)
    if cmd == "where":
        cfg = read_config()
        print(json.dumps({"binding": binding(cfg), "install": cfg.get("install"), "here": here()}, ensure_ascii=False, indent=2))
        return 0
    print("usage: bin/genius [save <folder> | restore <archive> [--force] | new | adopt | where]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
