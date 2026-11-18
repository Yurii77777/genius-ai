"""Put one secret into the user's env file, typed without echo — it never appears on screen or in the chat.

    bin/genius secret <NAME>        e.g. bin/genius secret OLLAMA_API_KEY

The file is "env_file" in genius.config.json (default ~/.claude/.env): one KEY=value line per secret, mode
600. An existing line of the same name is replaced; the other lines stay as they are. The value is never
printed: the script shows only its length and the last 4 characters, so the user can check it.
"""
import getpass
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", sys.argv[1]):
        sys.exit("usage: bin/genius secret <NAME>   (NAME in capitals, e.g. OLLAMA_API_KEY)")
    name = sys.argv[1]
    try:
        with open(os.path.join(ROOT, "genius.config.json"), encoding="utf-8") as f:
            env_file = json.load(f).get("env_file") or "~/.claude/.env"
    except (OSError, ValueError):
        env_file = "~/.claude/.env"
    path = os.path.expanduser(env_file)
    value = getpass.getpass(f"{name} (typing is hidden, Enter to finish): ").strip()
    if not value:
        sys.exit("nothing typed: the file is not changed")
    if "\n" in value or " " in value:
        sys.exit("the value has spaces or line breaks: not saved")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    kept = [l for l in lines if not re.match(rf"\s*(export\s+)?{name}\s*=", l)]
    replaced = len(kept) != len(lines)
    kept.append(f"{name}={value}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    print(f"{'replaced' if replaced else 'added'} {name} in {env_file} ({len(value)} characters, ends with …{value[-4:]}); mode 600")


if __name__ == "__main__":
    main()
