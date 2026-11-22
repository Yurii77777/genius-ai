#!/usr/bin/env python3
"""genius-ai console bridge: the page and a small JSON API over the data the Herdr panel reads.

Run:  python3 -I console/server.py --root <genius-ai folder> [--port 4419] [--dry-run]
Binds 127.0.0.1 only. Python stdlib only, one process, no daemons.

Reads (the same commands and files as herdr/genius-status):
  bin/status --json            projects: problems, release window, pause, hold, operations
  bin/genius focus --json      the focus of the day, the work in progress, done today
  bin/heavy --json             the heavy queue and local model work
  bin/genius models --json     the providers' limits
  hc ls --json, hc get <t>     agents and sub-agents (status, folder, model label, goal)
  state/agents-watch.json      when each sub-agent's screen last changed (minutes of quiet)
  state/log/<today>.jsonl      the coordinator's latest final reply (written by the Stop hook)
  genius.config.json           the user's language

Polling: only while a page is connected (an open /events stream, or /api/state in the last 60 s).
  every 15 s   focus, heavy queue, agents, quiet minutes, latest reply, operations.json change
  every 30 s   bin/status (at once when state/operations.json changed)
  every 60 s   models
/events (Server-Sent Events): the full state on connect, then {"changed": {...}} with only the
top-level parts that changed; a comment line every 20 s keeps the stream open.

Actions (POST /api/action, JSON), each one an existing command, nothing else:
  answer  {id, text}     bin/genius ops answer <id> <text>  +  hc send <coordinator> "<Answer to> «ask» (id): text"
  later   {id}           bin/genius ops later <id>
  ask     {id}           hc send <coordinator> "<Tell me about the operation> «what» (id)"  (the panel's 💬)
  open    {address}      herdr-app do pane.focus --terminal <t>  (an agent with a pane)
                         herdr-app do terminal.peek --terminal <t>  (a sub-agent)
  pause   {name}         bin/genius pause <name>
  resume  {name}         bin/genius resume <name>
  send    {text}         hc send <coordinator> <text>
--dry-run logs the command lines instead of running them (GET /api/dry-log lists them).
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/hunter.js": "hunter.js",
          "/classic.js": "classic.js", "/classic.css": "classic.css",
          # three.js 0.170.0, vendored so the hunter skin works offline (sha256 in console/vendor/SHA256SUMS)
          "/vendor/three.module.js": "vendor/three.module.js"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

FAST, STATUS_EVERY, MODELS_EVERY, IDLE_AFTER, KEEPALIVE = 15, 30, 60, 60, 20
ACTIVITY_EVERY = 4                                  # the coordinator's live activity line, while it works
SPINNER = re.compile(r"^\s*[✻✶✳✢✽·*]\s+(\S.*?…)")
QUIET_FROM, QUIET_ALARM = 3, 10                     # the panel's thresholds (minutes)

# the panel's own words for what it types into the coordinator's chat
WORDS = {"uk": {"answer_text": "Відповідь на", "ask_text": "Розкажи про операцію"},
         "en": {"answer_text": "Answer to", "ask_text": "Tell me about the operation"}}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def inside(d, path):
    return bool(d) and bool(path) and (d == path or d.startswith(path.rstrip("/") + "/"))


# What the hooks log as the user's «ask» is not always the owner's words: agents' hand-backs, task
# notifications, system reminders and pasted blocks also arrive as user turns.
NOT_OWNER = ("<agent-message", "<task-notification", "<system-reminder", "[SYSTEM NOTIFICATION", "[agents-watch]",
             "<local-command", "<bash-", "Another Claude session sent a message")
NEW_MESSAGE = "The user sent a new message while you were working:"
WRAPPERS = re.compile(r"<(system-reminder|task-notification|agent-message|local-command-[\w-]+|bash-[\w-]+)\b[^>]*>.*?</\1>\s*", re.S)
PASTED = re.compile(r"<pasted_content\b[^>]*>(?:.*?</pasted_content>|.*\Z)", re.S)


def owner_words(text):
    """The owner's own words in a logged «ask», or None when it is not the owner speaking."""
    t = (text or "").strip()
    if t.startswith(NEW_MESSAGE):
        t = t[len(NEW_MESSAGE):].strip()
    if not t or t.startswith(NOT_OWNER):
        return None
    # the coordinator's own scheduled prompts (session cron, loops)
    if t.startswith(("AF reminder (scheduled by me", "<<autonomous-loop", "/loop")) or "(scheduled by me at" in t.split("\n", 1)[0]:
        return None
    t = WRAPPERS.sub("", t)
    t = PASTED.sub("[вставлений текст] ", t).strip()
    return t if t.replace("[вставлений текст]", "").strip() else None


def clean_reply(text):
    """A reply without wrapper blocks (system reminders, notifications) that may have leaked into it."""
    t = WRAPPERS.sub("", text or "")
    return re.sub(r"</?(system-reminder|task-notification|agent-message)\b[^>]*>", "", t).strip()


def today():
    return dt.date.today().isoformat()


class Bridge:
    def __init__(self, root, dry):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.dry = dry
        self.dry_log = []
        self.hc = shutil.which("hc") or os.path.expanduser("~/.local/bin/hc")
        self.herdr = shutil.which("herdr-app") or os.path.expanduser("~/.local/bin/herdr-app")
        self.lock = threading.Condition()
        self.state = {}                             # part name → value
        self.hashes = {}
        self.version = 0
        self.changes = []                           # (version, {part: value}) recent diffs
        self.clients = 0
        self.last_api = 0.0
        self.wake = threading.Event()
        self.next_status = 0.0
        self.next_models = 0.0
        self.ops_mtime = None
        self.errors = {}

    # ---------- reading ----------
    def run(self, argv, timeout=60):
        try:
            r = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except Exception as e:                       # noqa: BLE001 — a missing tool is a state, not a crash
            return -1, "", str(e)

    def json_of(self, argv, timeout=60, name=None):
        code, out, err = self.run(argv, timeout)
        if code != 0:
            self.errors[name or argv[0]] = (err or f"exit {code}").strip().splitlines()[-1:] or [f"exit {code}"]
            return None
        try:
            self.errors.pop(name or argv[0], None)
            return json.loads(out)
        except ValueError as e:
            self.errors[name or argv[0]] = [f"bad JSON: {e}"]
            return None

    def file_json(self, rel, default):
        try:
            with open(os.path.join(self.root, rel), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def lang(self):
        return (self.file_json("genius.config.json", {}) or {}).get("language") or "en"

    def read_status(self):
        data = self.json_of([os.path.join(self.root, "bin/status"), "--json"], 120, "status")
        if data is None:
            return
        projects = []
        for s in data:
            path = os.path.expanduser(s.get("path") or "")
            ops = s.get("ops_detail") or []
            live_ops = [o for o in ops if not o.get("paused")]
            urls, pdata = s.get("problem_urls") or [], s.get("problem_data") or []
            problems = [{"text": t, "url": urls[i] if i < len(urls) else None, "data": pdata[i] if i < len(pdata) else None}
                        for i, t in enumerate(s.get("problems") or [])]
            projects.append({
                "name": s.get("name"), "tag": s.get("tag") or s.get("name"), "spoken": s.get("spoken") or "",
                "rank": s.get("rank") or 99, "path": path, "paused": bool(s.get("paused")), "hold": s.get("hold"),
                "release": s.get("release"), "problems": problems, "known": s.get("known") or [],
                "info": s.get("info") or [], "urls": s.get("urls") or [], "notes": s.get("notes") or [],
                "links": s.get("links") or {},
                "counts": {"you": sum(1 for o in live_ops if o.get("needs_user")),
                           "work": sum(1 for o in live_ops if not o.get("needs_user")),
                           "parked": sum(1 for o in ops if o.get("paused"))},
            })
        projects.sort(key=lambda p: p["rank"])
        ops = []
        for s, p in zip(sorted(data, key=lambda s: s.get("rank") or 99), projects):
            for o in s.get("ops_detail") or []:
                if o.get("needs_user") and o.get("kind") == "interrupted":
                    group = "cut"
                elif o.get("needs_user"):
                    group = "you"
                elif o.get("queue") == "next":
                    group = "next"
                else:
                    group = "work"
                ops.append({**{k: o.get(k) for k in ("id", "what", "waiting_for", "next", "ask", "answers", "needs_user",
                                                      "paused", "queue", "kind", "answered", "agent", "started")},
                            "project": p["name"], "tag": p["tag"], "project_paused": p["paused"], "group": group})
        # sectors: up to four projects by rank; their look follows the brief's rule
        for i, p in enumerate(projects):
            p["sector"] = i if i < 4 else None
            if p["paused"]:
                p["look"] = "dormant"
            elif p["problems"]:
                p["look"] = "wounded"
            elif (p["release"] or {}).get("open") or p["counts"]["work"] > 0:
                p["look"] = "hunt"
            else:
                p["look"] = "alive"
        self.put({"projects": projects, "ops": ops, "status_at": int(time.time())})

    def read_fast(self):
        parts = {}
        focus = self.json_of([os.path.join(self.root, "bin/genius"), "focus", "--json"], 15, "focus")
        if focus is None:
            focus = self.file_json("state/focus.json", {})
        parts["focus"] = focus
        heavy = self.json_of([os.path.join(self.root, "bin/heavy"), "--json"], 15, "heavy")
        parts["heavy"] = heavy if heavy is not None else self.file_json("state/heavy.json", {})
        parts["lang"] = self.lang()
        parts["agents"], parts["coordinator"] = self.read_agents()
        parts["reply"] = self.read_reply()
        parts["dry_run"] = self.dry
        try:
            m = os.path.getmtime(os.path.join(self.root, "state/operations.json"))
        except OSError:
            m = None
        if self.ops_mtime is not None and m != self.ops_mtime:
            self.next_status = 0                     # an operation changed: read the status now
        self.ops_mtime = m
        self.put(parts)

    def read_agents(self):
        ls = self.json_of([self.hc, "ls", "--json"], 15, "hc")
        if ls is None:
            return [], None
        watch = self.file_json("state/agents-watch.json", {})
        projects = self.state.get("projects") or []
        now = time.time()
        out, coord = [], None
        for t in ls.get("terminals") or []:
            if t.get("state") != "live" or not t.get("status"):
                continue                             # a shell or a watcher: not an agent (the panel's rule)
            addr = t.get("address")
            g = self.json_of([self.hc, "get", addr, "--json"], 10, "hc get") or {}
            labels = g.get("labels") or {}
            st = g.get("status") or {}
            name = t.get("name") or addr
            title = g.get("title") or ""
            proj = next((p for p in projects if inside(t.get("cwd"), p["path"])), None)
            changed = (watch.get(name) or {}).get("changed")
            quiet = int(max(0, (now - changed) / 60)) if changed else None
            a = {"address": addr, "name": name, "sub": bool(t.get("parent")), "cwd": t.get("cwd"),
                 "status": t.get("status"), "app": t.get("agent"), "model": labels.get("genius.model", ""),
                 "role": labels.get("genius.role", ""), "goal": title if title and title != name and title != "Claude Code" else "",
                 "msg": st.get("msg") or (g.get("report") or {}).get("msg") or "",
                 "project": proj["name"] if proj else None, "tag": proj["tag"] if proj else (t.get("where") or ""),
                 "quiet": quiet, "stuck": t.get("status") == "working" and quiet is not None and quiet >= QUIET_ALARM}
            if not a["sub"] and inside(t.get("cwd"), self.root) and coord is None:
                coord = {"address": addr, "status": a["status"]}
                continue
            out.append(a)
        if coord is None:                            # the panel's fallback: any agent in the genius-ai folder
            c = next((a for a in out if inside(a["cwd"], self.root)), None)
            if c:
                coord = {"address": c["address"], "status": c["status"]}
        return out, coord

    def read_reply(self):
        r = self.read_replies(1, days=1)
        return r[-1] if r and str(r[-1].get("at") or "").startswith(today()) else None

    def read_replies(self, limit=20, days=14):
        """The last `limit` final replies of the coordinator (cwd inside root), newest last, each with the
        user's message that came before it in the same session. Read-only: the live day files
        state/log/<date>.jsonl and the digested ones state/log/archive/<date>.jsonl.gz."""
        logdir = os.path.join(self.root, "state/log")
        ndays, days = days, {}
        try:
            for n in os.listdir(logdir):
                if n.endswith(".jsonl") and len(n) == 16:
                    days.setdefault(n[:10], []).append(os.path.join(logdir, n))
        except OSError:
            pass
        try:
            for n in os.listdir(os.path.join(logdir, "archive")):
                if n.endswith(".jsonl.gz") and len(n) == 19:
                    days.setdefault(n[:10], []).append(os.path.join(logdir, "archive", n))
        except OSError:
            pass
        out = []
        for day in sorted(days, reverse=True)[:ndays]:
            recs = []
            for f in days[day]:
                try:
                    op = gzip.open if f.endswith(".gz") else open
                    with op(f, "rt", encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                recs.append(json.loads(line))
                            except ValueError:
                                pass
                except OSError:
                    pass
            recs.sort(key=lambda r: str(r.get("at") or ""))
            asks, got = {}, []
            for r in recs:
                if not inside(r.get("cwd"), self.root):
                    continue
                if r.get("who") == "user" and r.get("kind") == "ask":
                    w = owner_words(r.get("text"))
                    if w:                                   # not the owner speaking: keep the earlier one
                        asks[r.get("session")] = {"at": r.get("at"), "text": w[:2000]}
                elif r.get("who") == "assistant" and r.get("kind") == "reply":
                    got.append({"at": r.get("at"), "id": r.get("id"), "text": clean_reply(r.get("text")),
                                "ask": asks.pop(r.get("session"), None)})
            out = got + out
            if len(out) >= limit:
                break
        return out[-limit:]

    def read_models(self):
        m = self.json_of([os.path.join(self.root, "bin/genius"), "models", "--json"], 30, "models")
        if m is not None:
            self.put({"models": m})

    # ---------- state and diffs ----------
    def put(self, parts):
        parts = dict(parts)
        parts["errors"] = {k: v for k, v in self.errors.items()}
        changed = {}
        for k, v in parts.items():
            h = hashlib.sha1(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
            if self.hashes.get(k) != h:
                self.hashes[k] = h
                changed[k] = v
        if not changed:
            return
        with self.lock:
            self.state.update(changed)
            self.state["at"] = int(time.time())
            self.version += 1
            self.changes.append((self.version, {**changed, "at": self.state["at"]}))
            self.changes = self.changes[-50:]
            self.lock.notify_all()

    def snapshot(self):
        with self.lock:
            return self.version, dict(self.state)

    def active(self):
        return self.clients > 0 or time.time() - self.last_api < IDLE_AFTER

    def loop(self):
        log(f"polling: fast {FAST}s, status {STATUS_EVERY}s, models {MODELS_EVERY}s — only while a page is open")
        while True:
            if self.active() or not self.state:
                t0 = time.time()
                try:
                    if t0 >= self.next_status or "projects" not in self.state:
                        self.read_status()
                        self.next_status = time.time() + STATUS_EVERY
                    self.read_fast()
                    if t0 >= self.next_models:
                        self.read_models()
                        self.next_models = time.time() + MODELS_EVERY
                except Exception as e:               # noqa: BLE001 — keep polling
                    log("poll error:", e)
            self.wake.wait(FAST)
            self.wake.clear()

    # The coordinator's current activity while it works (the page's «thinking» loader): its screen's
    # last tool line (the text above «⎿») and its spinner word. Read-only: hc get / hc read.
    def read_activity(self):
        addr = (self.state.get("coordinator") or {}).get("address")
        if not addr:
            return {"status": None}
        g = self.json_of([self.hc, "get", addr, "--json"], 5, "hc get") or {}
        status = (g.get("status") or {}).get("state")
        out = {"status": status, "busy": False, "doing": "", "spin": "", "at": int(time.time())}
        if status != "working":
            return out
        try:
            screen = subprocess.run([self.hc, "read", addr], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return out
        lines = screen.splitlines()[-60:]
        # a running turn: Claude Code's footer offers «esc to interrupt» (a session that only waits for a
        # background agent shows «working» in Herdr, but no turn runs)
        out["busy"] = any("esc to interrupt" in ln for ln in lines[-8:])
        if not out["busy"]:
            return out
        for i in range(len(lines) - 1, 0, -1):
            m = SPINNER.match(lines[i])
            if m and not out["spin"]:
                out["spin"] = m.group(1)
            if lines[i].lstrip().startswith("⎿") and not out["doing"]:
                j = i - 1
                while j > 0 and not lines[j].strip():
                    j -= 1
                out["doing"] = lines[j].strip().lstrip("⏺").strip()[:160]
            if out["spin"] and out["doing"]:
                break
        return out

    def activity_loop(self):
        while True:
            time.sleep(ACTIVITY_EVERY)
            if not self.active() or not self.state:
                continue
            try:
                self.put({"coordinator_activity": self.read_activity(), "reply": self.read_reply()})
            except Exception as e:                   # noqa: BLE001 — keep polling
                log("activity error:", e)

    def refresh_soon(self, status=True):
        if status:
            self.next_status = 0
        self.wake.set()

    # ---------- actions ----------
    def do(self, argv):
        line = " ".join(argv)
        if self.dry:
            self.dry_log.append({"at": time.strftime("%H:%M:%S"), "argv": argv})
            log("[dry-run]", line)
            return True, "dry-run"
        code, out, err = self.run(argv, 30)
        log("[run]", argv[:3], "→", code)
        return code == 0, (err or out).strip()[-300:]

    def op(self, oid):
        return next((o for o in self.state.get("ops") or [] if o["id"] == oid), None)

    def coordinator(self):
        return (self.state.get("coordinator") or {}).get("address")

    def action(self, body):
        kind = body.get("kind")
        w = WORDS.get(self.state.get("lang") or "en", WORDS["en"])
        g = os.path.join(self.root, "bin/genius")
        if kind in ("answer", "later", "ask"):
            o = self.op(str(body.get("id") or ""))
            if not o:
                return 404, {"ok": False, "error": "no such operation"}
            if kind == "later":
                ok, msg = self.do([g, "ops", "later", o["id"]])
                self.refresh_soon()
                return 200, {"ok": ok, "dry": self.dry, "msg": msg}
            chat = self.coordinator()
            if not chat:
                return 409, {"ok": False, "error": "the genius-ai chat is not in Herdr"}
            if kind == "ask":
                ok, msg = self.do([self.hc, "send", chat, f"{w['ask_text']} «{o['what']}» ({o['id']})"])
                if ok:
                    self.do([self.herdr, "do", "pane.focus", "--terminal", chat])
                return 200, {"ok": ok, "dry": self.dry, "msg": msg}
            text = str(body.get("text") or "")
            if text not in (o.get("answers") or []):
                return 400, {"ok": False, "error": "not one of the operation's answers"}
            ok, msg = self.do([g, "ops", "answer", o["id"], text])
            if ok:
                ok, msg = self.do([self.hc, "send", chat, f"{w['answer_text']} «{o.get('ask') or o.get('what')}» ({o['id']}): {text}"])
            self.refresh_soon()
            return 200, {"ok": ok, "dry": self.dry, "msg": msg}
        if kind in ("pause", "resume"):
            name = str(body.get("name") or "")
            if not any(p["name"] == name for p in self.state.get("projects") or []):
                return 404, {"ok": False, "error": "no such project"}
            ok, msg = self.do([g, kind, name])
            self.refresh_soon()
            return 200, {"ok": ok, "dry": self.dry, "msg": msg}
        if kind == "open":
            addr = str(body.get("address") or "")
            a = next((a for a in self.state.get("agents") or [] if a["address"] == addr), None)
            if not a and addr != self.coordinator():
                return 404, {"ok": False, "error": "no such agent"}
            cmd = "terminal.peek" if a and a["sub"] else "pane.focus"
            ok, msg = self.do([self.herdr, "do", cmd, "--terminal", addr])
            return 200, {"ok": ok, "dry": self.dry, "msg": msg}
        if kind == "send":
            text = str(body.get("text") or "").strip()
            if not text or len(text) > 4000:
                return 400, {"ok": False, "error": "empty or too long"}
            chat = self.coordinator()
            if not chat:
                return 409, {"ok": False, "error": "the genius-ai chat is not in Herdr"}
            ok, msg = self.do([self.hc, "send", chat, text])
            self.wake.set()
            return 200, {"ok": ok, "dry": self.dry, "msg": msg}
        return 400, {"ok": False, "error": "unknown action"}


def make_handler(bridge, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def guard(self):
            # DNS rebinding and other sites' pages: only this page may talk to the bridge
            if self.headers.get("Host") not in hosts:
                self.send_json(403, {"error": "host"})
                return False
            o = self.headers.get("Origin")
            if o and o not in origins:
                self.send_json(403, {"error": "origin"})
                return False
            return True

        def send_json(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_HEAD(self):
            # the page asks whether this bridge serves the vendored three.js (index.html)
            if not self.guard():
                return
            f = STATIC.get(self.path.split("?")[0])
            if not f or not os.path.isfile(os.path.join(HERE, f)):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", TYPES[os.path.splitext(f)[1]])
            self.send_header("Content-Length", str(os.path.getsize(os.path.join(HERE, f))))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def do_GET(self):
            if not self.guard():
                return
            path = self.path.split("?")[0]
            if path in STATIC:
                f = os.path.join(HERE, STATIC[path])
                try:
                    with open(f, "rb") as fh:
                        b = fh.read()
                except OSError:
                    return self.send_json(404, {"error": "missing"})
                self.send_response(200)
                self.send_header("Content-Type", TYPES[os.path.splitext(f)[1]])
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b)
                return
            if path == "/api/state":
                bridge.last_api = time.time()
                bridge.wake.set()
                return self.send_json(200, bridge.snapshot()[1])
            if path == "/api/replies":
                q = self.path.partition("?")[2]
                try:
                    n = int(dict(x.partition("=")[::2] for x in q.split("&") if x).get("limit", 20))
                except ValueError:
                    n = 20
                return self.send_json(200, {"replies": bridge.read_replies(max(1, min(n, 100)))})
            if path == "/api/dry-log":
                return self.send_json(200, {"dry_run": bridge.dry, "commands": bridge.dry_log})
            if path == "/events":
                return self.events()
            self.send_json(404, {"error": "not found"})

        def events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            bridge.clients += 1
            bridge.wake.set()
            try:
                ver, snap = bridge.snapshot()
                self.wfile.write(f"event: full\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while True:
                    with bridge.lock:
                        bridge.lock.wait_for(lambda: bridge.version > ver, timeout=KEEPALIVE)
                        new = [(v, c) for v, c in bridge.changes if v > ver]
                        cur = bridge.version
                        # fell behind the kept diffs: send the whole state
                        snap = dict(bridge.state) if cur > ver and (not new or new[0][0] > ver + 1) else None
                    if snap is not None:
                        self.wfile.write(f"event: full\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n".encode())
                    elif new:
                        merged = {}
                        for _, c in new:
                            merged.update(c)
                        self.wfile.write(f"event: diff\ndata: {json.dumps({'changed': merged}, ensure_ascii=False)}\n\n".encode())
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    ver = cur
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                bridge.clients -= 1

        def do_POST(self):
            if not self.guard():
                return
            if self.path != "/api/action":
                return self.send_json(404, {"error": "not found"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_json(415, {"error": "json only"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self.send_json(413, {"error": "too big"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "bad json"})
            bridge.last_api = time.time()
            code, res = bridge.action(body if isinstance(body, dict) else {})
            self.send_json(code, res)

    return H


def main():
    ap = argparse.ArgumentParser(description="genius-ai console bridge")
    ap.add_argument("--root", required=True, help="the genius-ai folder")
    ap.add_argument("--port", type=int, default=4419)
    ap.add_argument("--dry-run", action="store_true", help="log the action commands instead of running them")
    a = ap.parse_args()
    bridge = Bridge(a.root, a.dry_run)
    if not os.path.isfile(os.path.join(bridge.root, "bin/status")):
        sys.exit(f"not a genius-ai folder: {bridge.root}")
    threading.Thread(target=bridge.loop, daemon=True).start()
    threading.Thread(target=bridge.activity_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(bridge, a.port))
    srv.daemon_threads = True
    log(f"console on http://127.0.0.1:{a.port}/  root {bridge.root}{'  DRY-RUN' if a.dry_run else ''}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
