"""Models: what each model provider has left, for the Herdr panel («Models») and for the coordinator.

    bin/genius models            one line per provider
    bin/genius models --json     the same for the panel

- Claude: the account's 5-hour and 7-day limits that Claude Code reported last (state/claude-status.json,
  written by bin/lib/statusline.py).
- Codex: the limits in the newest Codex session log (~/.codex/sessions; Codex writes them with every turn).
- Ollama: is the local server up, which models it holds in memory now, and — with an API key — the cloud
  usage of the last 24 h and the balance (https://ollama.com/api/usage, /api/balance). The key is
  OLLAMA_API_KEY from the environment, else from the file named by "env_file" in genius.config.json
  (KEY=value lines). It is never written to the config or to state/. The cloud answer is cached 5 min.

Read-only. Nothing here starts a model.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STATE = os.path.join(ROOT, "state")
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
CACHE = os.path.join(STATE, "models-cache.json")


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def get(url, key=None, timeout=4):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def claude():
    s = read(os.path.join(STATE, "claude-status.json"), {})
    rl = s.get("rate_limits") or {}
    out = {}
    for name, key in (("5h", "five_hour"), ("7d", "seven_day")):
        lim = rl.get(key) or {}
        if lim.get("used_percentage") is not None:
            out[name] = {"used": round(lim["used_percentage"]), "resets": lim.get("resets_at")}
    return {"name": "Claude", "model": (s.get("model") or {}).get("display_name", ""), "limits": out,
            "ok": bool(out)}


def codex():
    base = os.path.expanduser("~/.codex/sessions")
    files = []
    for root, _, names in os.walk(base):
        files += [os.path.join(root, n) for n in names if n.endswith(".jsonl")]
    if not files:
        return None                                 # Codex is not used on this computer: no row
    newest = max(files, key=os.path.getmtime)
    last = None
    try:
        with open(newest, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300000))
            for line in f.read().decode("utf-8", "ignore").split("\n"):
                if '"rate_limits"' in line:
                    last = line
    except OSError:
        pass
    i = (last or "").find('"rate_limits":')
    out = {}
    if i >= 0:
        try:
            rl, _ = json.JSONDecoder().raw_decode(last, i + len('"rate_limits":'))
        except ValueError:
            rl = {}
        rl = rl if isinstance(rl, dict) else {}
        for part in ("primary", "secondary"):
            lim = rl.get(part) or {}
            if lim.get("used_percent") is not None:
                mins = lim.get("window_minutes") or 0
                name = "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m"
                resets = lim.get("resets_at")
                used = round(lim["used_percent"]) if not (resets and resets < time.time()) else 0   # the window reset since
                out[name] = {"used": used, "resets": resets}
    return {"name": "Codex", "limits": out, "ok": bool(out), "seen": int(os.path.getmtime(newest))}


def ollama_key():
    if os.environ.get("OLLAMA_API_KEY"):
        return os.environ["OLLAMA_API_KEY"]
    env_file = read(os.path.join(ROOT, "genius.config.json"), {}).get("env_file")
    if not env_file:
        return None
    try:
        with open(os.path.expanduser(env_file), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?OLLAMA_API_KEY\s*=\s*(.+)", line)
                if m:
                    return m.group(1).strip().strip("\"'")
    except OSError:
        pass
    return None


def ollama():
    import shutil                                   # the panel runs with a short PATH: look in Homebrew's places too
    if not (shutil.which("ollama") or any(os.path.exists(p) for p in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"))):
        return None                                 # not installed: no row
    out = {"name": "Ollama", "up": False, "loaded": [], "ok": False}
    try:
        get(OLLAMA + "/api/version", timeout=2)
        out["up"] = True
        out["loaded"] = [m.get("name", "") for m in get(OLLAMA + "/api/ps", timeout=2).get("models", [])]
    except (OSError, ValueError, urllib.error.URLError):
        pass
    key = ollama_key()
    out["key"] = bool(key)
    if key:
        cache = read(CACHE, {})
        if time.time() - cache.get("at", 0) < 300:
            out["cloud"] = cache.get("cloud")
        else:
            cloud = {}
            for name, url in (("usage", "https://ollama.com/api/usage?range=24h"), ("balance", "https://ollama.com/api/balance")):
                try:
                    got = get(url, key, timeout=6)
                    if name == "usage":                 # the totals only: the hourly buckets are not shown
                        t = got.get("totals") or {}
                        got = {"requests": t.get("request_count"), "usd": t.get("usage_usd"),
                               "input": t.get("input_tokens"), "cached": t.get("cached_input_tokens"), "output": t.get("output_tokens")}
                    else:
                        inc = got.get("included") or {}
                        got = {"left_usd": inc.get("balance_usd"), "allowance_usd": inc.get("allowance_usd"),
                               "until": (inc.get("period") or {}).get("until", "")[:10],
                               "purchased_usd": (got.get("purchased") or {}).get("balance_usd")}
                    cloud[name] = got
                except (OSError, ValueError, urllib.error.URLError) as e:
                    cloud[name] = {"error": str(getattr(e, "code", "") or e)[:80]}
            out["cloud"] = cloud
            try:
                os.makedirs(STATE, exist_ok=True)
                with open(CACHE, "w", encoding="utf-8") as f:
                    json.dump({"at": time.time(), "cloud": cloud}, f)
            except OSError:
                pass
    out["ok"] = out["up"]
    return out


def line(p):
    if p["name"] == "Ollama":
        bits = ["up" if p["up"] else "down"]
        if p["loaded"]:
            bits.append("in memory: " + ", ".join(p["loaded"]))
        if not p.get("key"):
            bits.append("no OLLAMA_API_KEY: cloud usage unknown")
        elif p.get("cloud"):
            u, b = p["cloud"].get("usage") or {}, p["cloud"].get("balance") or {}
            if u.get("requests") is not None:
                bits.append(f"24h: {u['requests']} requests, ${u.get('usd') or 0:.2f}")
            if b.get("left_usd") is not None:
                bits.append(f"left ${b['left_usd']:.2f} of ${b.get('allowance_usd') or 0:.0f} until {b.get('until', '')}")
        return "Ollama · " + " · ".join(bits)
    lim = " · ".join(f"{k} {v['used']}%" for k, v in p["limits"].items()) or "no data yet"
    return f"{p['name']} · {lim}"


if __name__ == "__main__":
    data = [p for p in (claude(), codex(), ollama()) if p]
    if "--json" in sys.argv[1:]:
        json.dump({"at": int(time.time()), "providers": data}, sys.stdout, ensure_ascii=False)
        print()
    else:
        for p in data:
            print(line(p))
