"""The gate: decides every action of an unattended agent, so nobody has to answer its permission prompts.

    (hook)  PreToolUse   python3 bin/lib/gate.py      — reads the hook JSON on stdin, answers allow or deny
    bin/genius gate log [-n 20]                       the latest decisions
    bin/genius gate test <tool> '<input json>' [cwd]  what the gate would answer, without the judge's cache
    bin/genius gate chain                             the judge's links, in the order it asks them
    python3 -I bin/lib/gate.py selftest [ollama]      the rules, the cache key and the fixtures of 2026-11-20,
                                                      without docker or a model; «ollama» also asks the live link

The user's decision of 2026-11-17: agents on other models (DeepSeek through Ollama) must not wait for the
user, and «auto» mode cannot work for them (its safety check runs on Anthropic's servers). bin/genius agent
gives such an agent this hook (`claude --settings`); the agent itself runs in the «default» mode, so every
action reaches the gate first, and the gate always answers — never «ask», so no prompt ever waits.

How it decides:
1. Rules, at once. Deny: pushing, merging, releasing, prod and remote hosts (ssh, scp, deploy commands),
   secrets (~/.ssh, .env files, keychains), deleting outside the work folder, privilege, wiping volumes,
   disabling TLS. Allow: reading, searching, editing inside the work folder or the scratch space, local
   git that changes no remote, tests, builds, linters, the heavy queue, the knowledge base, web search.
2. Anything else goes to the judge, which is the role «judge» of bin/lib/agent.py (bin/lib/judge.py):
   kimi-k3.1-pro:cloud → glm-5.3:cloud → deepseek-v4.1-flash:cloud → codex → claude (sonnet), the first that
   answers. The local Ollama answers in about a second and costs the user nothing. Claude is the LAST link only:
   a judge call on Claude eats the user's own 5-hour window (the owner's decision of 2026-11-20: 252 calls in
   35 minutes, 13 % → 81 %). A link of the agent's own family is skipped, and Claude is no link at all while the
   config says "claude_fallback": false. The judge gets the action, the folder and the rules above, and answers
   ALLOW or DENY and one reason. The same action in the same folder is judged once (state/gate-cache.json). No
   judge, or no clear answer → deny: a stopped agent is cheaper than a wrong action.
Every decision goes to state/gate.jsonl; the agent gets the reason with a deny, and can report it.

The gate never decides what only the user decides (a merge, a write to prod, money, lifting a hold): those
are denied by rule, and the agent reports them as questions.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
LOG = os.path.join(ROOT, "state", "gate.jsonl")
CACHE = os.path.join(ROOT, "state", "gate-cache.json")
HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import judge as judge_role                                   # the role «judge»: the chain and the callers


def judge_chain():
    """The judge's links now, [(provider, model)]: the role «judge» without the family of the agent judged and
    without the Claude link while the config says "claude_fallback": false."""
    return [(l["provider"], l.get("model", "")) for l in judge_role.links(judge_role.self_link())]

DENY_BASH = [
    (r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\$HOME|\.\.)", "deleting outside the work folder"),
    (r"\bgit\s+push\b", "push: the coordinator and the user decide"),
    (r"\bgh\s+pr\s+(merge|ready|review\s+--approve)\b", "merge or approval of a PR: the user's word"),
    (r"\bgh\s+(release|repo\s+(delete|edit|rename)|secret|variable|workflow\s+run|run\s+(rerun|cancel))\b", "a change on GitHub"),
    (r"\bgh\s+api\b.*\s-X\s*(POST|PUT|PATCH|DELETE)\b|\bgh\s+api\b.*--method\s+(POST|PUT|PATCH|DELETE)\b", "a write through the GitHub API"),
    (r"(^|[\s;&|(])(ssh|scp|sftp|rsync)\s", "a remote host"),
    (r"\b(vercel|netlify|fly|flyctl|heroku|doctl|aws|gcloud|az|kubectl|terraform|tofu|pulumi)\b", "a cloud or deploy tool"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bnpm\s+(adduser|login|token)\b", "publishing a package"),
    (r"\bsudo\b|\bsu\s|\bchown\b|\bchmod\s+[0-7]*7[0-7]*\s+/", "privilege"),
    (r"\bdocker\s+(compose\s+)?.*(down\s+(-v|--volumes)|volume\s+(rm|prune)|system\s+prune)", "wiping containers' data"),
    (r"\bcurl\b[^|;]*\s(-k|--insecure|-sk|-ks)\b", "TLS switched off"),
    (r"(\.ssh/|id_rsa|id_ed25519|\.pem\b|\.p12\b|\bsecurity\s+(find|dump)|keychain|\.netrc|\.aws/|\.kube/|\.docker/config)", "secrets"),
    (r"(^|[\s/])\.env(\.[\w.-]+)?(\s|$|['\"])", "a secrets file (.env)"),
    (r"\b(launchctl|crontab|defaults\s+write|osascript)\b", "the user's system"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|restore\s+--source)|\bgit\s+branch\s+-D\b", "discarding work"),
    (r"\bgit\s+(remote\s+(add|set-url|remove)|config\s+--global)\b", "git settings"),
    (r"\bgit\s+config\s+(?!--get|--list|-l\b)[\w.-]+\s+[^\s|&;)]", "changing git settings"),
    (r"\b(shutdown|reboot|killall|pkill)\b|\bkill\s+-9\s+-1\b", "the user's processes"),
    (r"\bfind\b[^|;&]*\s(-delete|-exec(dir)?\s+rm)\b|\bxargs\b[^|;&]*\brm\b", "deleting files in bulk"),
    (r"\brm\s+(-[a-zA-Z]+\s+)*['\"]?(\.{1,2}/?|\*)['\"]?(\s|$)", "deleting the work folder itself"),
]
SAFE_BASH = re.compile(
    r"^\s*(?:[\w./~-]*/)?("
    r"ls|pwd|cat|head|tail|wc|grep|egrep|fgrep|rg|find|fd|tree|stat|file|du|df|which|type|echo|printf|date|sort|uniq|cut|tr|"
    r"tac|rev|fold|nl|column|join|paste|comm|seq|expr|bc|strings|iconv|base64|xxd|od|hexdump|shasum|sha256sum|md5sum|md5|"
    r"jq|yq|diff|cmp|basename|dirname|realpath|readlink|test|\[|\[\[|true|false|sleep|env|printenv|whoami|id|uname|"
    r"sw_vers|nproc|sysctl|hostname|uptime|vm_stat|ps|pgrep|lsof|"
    r"git\s+(status|diff|log|show|branch|rev-parse|rev-list|ls-files|ls-tree|show-ref|symbolic-ref|for-each-ref|cat-file|"
    r"reflog|count-objects|shortlog|grep|blame|check-ignore|cherry|stash|add|commit|checkout|switch|restore|worktree|"
    r"fetch|merge|merge-base|cherry-pick|rebase|tag\s+-l|describe|mv|rm|remote\s+(-v|show|get-url)|"
    r"config\s+(--get\w*|--list|-l|[\w.-]+))|"
    r"gh\s+(pr|issue|run|repo)\s+(view|list|diff|checks|status)|gh\s+auth\s+status|gh\s+api\s+(?!.*(-X|--method))|"
    r"node|npx\s+(vitest|jest|playwright|tsc|eslint|prettier|biome|turbo)|tsc|eslint|prettier|vitest|jest|"
    r"(npm|pnpm|yarn|bun)\s+(vitest|jest|playwright|tsc|eslint|prettier|biome|turbo|run|test|exec|install|i|ci|add|remove|why|ls|list|outdated|audit|lint|typecheck|build|dlx)|"
    r"python3?\b|pytest|ruff|mypy|uv\s+(run|pip|sync)|pip3?\s+(install|show|list)|go\s+(test|build|vet|run)|cargo\s+(test|build|check|clippy)|"
    r"make|mkdir|touch|cp|mv|ln|rm|chmod|sed|awk|tee|xargs|curl|wget|tar|unzip|zip|gzip|sips|ffmpeg|ffprobe|magick|convert|"
    r"docker\s+(ps|logs|images|inspect|stats|top|version|info|port)|"
    r"docker\s+compose\s+(-{1,2}[\w-]+(\s+|=)\S*\s+)*(ps|logs|config|stop|start|restart|up|top)|hc\s+(ls|get|read|wait)|"
    r"bin/heavy|bin/genius\s+(know|models|log|focus|load|gate\s+(log|test))|bin/status)(?=\s|$)")
# Every part that may write to a path it names: its absolute and «~» targets must stay inside a work folder.
WRITE_PATHS = re.compile(r"^\s*(?:\S*/)?(rm|mv|cp|tee|touch|mkdir|rmdir|ln|chmod|truncate)\b")
# A part that only asks a program for its version or its help reads nothing of the project's.
VERSION_HELP = re.compile(r"^\s*\S+\s+(--version|-V|--help|-h|version)(\s+2>&1)?\s*$")
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "TodoRead", "Task", "Agent", "AskUserQuestion",
              "WebSearch", "WebFetch", "ToolSearch", "Skill", "ExitPlanMode", "BashOutput", "KillShell", "Monitor"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SECRET_PATH = re.compile(r"(/\.ssh/|/\.aws/|/\.kube/|/\.docker/config|/\.netrc|/\.gnupg/|(^|/)\.env(\.[\w.-]+)?$|\.pem$|\.p12$|"
                         r"/Library/Keychains/|/\.claude/\.env$|/\.config/gh/)")

# A read-only look into a database that runs in a container on this Mac (the lesson of 2026-11-18).
# `docker exec -i chica-db-1 psql …` with a «begin read only … rollback» and SELECTs was denied by the judge
# («reads the DB through a container I can't confirm is local»), and the deny was cached, so the user had to press
# the button every 30 s. The rules now answer it first, and the judge is told which containers run here.
DB_PROGRAMS = {"psql": "postgres", "pg_dump": "postgres", "mysql": "mysql", "mariadb": "mysql", "redis-cli": "redis",
               "sqlite3": "sqlite"}
SQL_WRITE = re.compile(r"\b(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy|call|do|reindex|refresh|"
                       r"cluster|vacuum|nextval|setval|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|"
                       r"lo_import|lo_export)\b")
SQLITE_READ = {"tables", "schema", "indexes", "indices", "dump", "databases", "dbinfo", "show", "mode", "headers",
               "separator", "width", "nullvalue", "print", "eqp", "explain", "help", "quit", "exit", "timeout",
               "bail", "echo", "stats", "timer", "version"}
SQLITE_VALUE = {"-cmd", "-init", "-separator", "-newline", "-nullvalue", "-mmap", "-pagecache", "-vfs"}
SQL_READ = re.compile(r"^(select|with|show|explain|values|table|begin|start|commit|rollback|set|reset)\b")
SET_SAFE = re.compile(r"\b(read\s+only|search_path|timezone|statement_timeout|lock_timeout|application_name|"
                      r"datestyle|intervalstyle|extra_float_digits|client_min_messages)\b")
PSQL_READ = {"d", "dt", "di", "dv", "dm", "ds", "df", "dn", "dp", "du", "dg", "l", "x", "timing", "pset", "echo",
             "conninfo", "encoding", "q", "?"}
REDIS_READ = {"get", "mget", "exists", "keys", "scan", "ttl", "pttl", "type", "lrange", "llen", "lindex", "hget",
              "hgetall", "hkeys", "hlen", "hvals", "hmget", "smembers", "sismember", "scard", "srandmember", "zrange",
              "zrangebyscore", "zscore", "zcard", "zcount", "info", "dbsize", "ping", "echo", "time", "lastsave",
              "randomkey", "xrange", "xlen", "xinfo", "getrange", "bitcount", "strlen", "geopos", "geodist", "command",
              "slowlog", "role"}
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "[::1]", "localhost.localdomain", "/var/run/postgresql", "/tmp"}
PSQL_VALUE = {"-c", "--command", "-d", "--dbname", "-U", "--username", "-h", "--host", "-p", "--port", "-v", "--set",
              "-P", "--pset", "-o", "--output", "-f", "--file", "-F", "--field-separator", "-R", "--record-separator",
              "-T", "--table-attr", "-L", "--log-file"}
PGDUMP_VALUE = {"-d", "--dbname", "-U", "--username", "-h", "--host", "-p", "--port", "-f", "--file", "-F", "--format",
                "-t", "--table", "-n", "--schema", "-T", "--exclude-table", "-N", "--exclude-schema", "-Z", "--compress"}
MYSQL_VALUE = {"-e", "--execute", "-h", "--host", "-u", "--user", "-P", "--port", "-D", "--database", "-S", "--socket",
               "--defaults-file", "--defaults-extra-file", "--init-command"}
REDIS_VALUE = {"-h", "--host", "-p", "--port", "-a", "--pass", "-u", "--uri", "-n", "--db", "-r", "--repeat",
               "-i", "--interval", "-s", "--socket", "--user", "--sni"}
DOCKER_OPT_VALUE = {"-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys"}
# A script written inside the command (`python3 - <<'PY'`, `python3 -c '…'`, `node -e '…'`) is an edit of the work
# folder when it only writes files inside it (the lesson of 2026-11-18: a worktree README write was denied).
INLINE_NO = re.compile(
    r"\b(subprocess|child_process|execSync|execFile|spawn\w*|popen|fork|shutil|socket|urllib|requests|httpx|ftplib|"
    r"smtplib|paramiko|telnetlib|ctypes|pty|webbrowser|fetch|XMLHttpRequest|dgram|eval|exec|compile|__import__|"
    r"importlib|input|sys\.argv|sys\.stdin)\b"
    r"|os\.(system|popen|exec\w*|spawn\w*|fork|kill|remove|unlink|rmdir|rename|replace|chmod|chown|symlink|link|"
    r"truncate|putenv|environ|setuid|setgid)\b"
    r"|\.(unlink|rmdir|rename|symlink_to|hardlink_to|chmod|chown|unlinkSync|rmSync|rmdirSync|renameSync|chmodSync)\b"
    r"|process\.env")
PATH_LITERAL = re.compile(r"(['\"])((?:[^'\"\\\n]|\\.)*)\1")
DB_ENV = re.compile(r"^(PGHOST|PGHOSTADDR|POSTGRES_HOST|POSTGRESQL_HOST|MYSQL_HOST|MARIADB_HOST|REDIS_HOST|"
                    r"MONGO_HOST|DB_HOST|DATABASE_HOST|DATABASE_URL|PG_URL|POSTGRES_URL|POSTGRESQL_URL|MYSQL_URL|"
                    r"MARIADB_URL|REDIS_URL|MONGO_URL)$")


def inside(path, base):
    path = os.path.realpath(os.path.expanduser(path))
    base = os.path.realpath(base)
    return path == base or path.startswith(base + os.sep)


def workspaces(cwd, h):
    # the agent's own report and drafts in genius-ai are part of its work (every brief asks for a report there),
    # and so is the rest of genius-ai's state: the operations, the logs and the cache that a brief may ask it to fix
    spaces = [cwd, "/tmp", "/private/tmp", os.environ.get("TMPDIR", "/tmp"), os.path.join(ROOT, "state")]
    if h.get("scratchpad_dir"):
        spaces.append(h["scratchpad_dir"])
    return [s for s in spaces if s]


def split_parts(cmd):
    """Split a shell command at && || ; | and newlines, but never inside quotes."""
    parts, cur, q, i = [], "", "", 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            cur += c
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                cur += cmd[i + 1]; i += 1
            elif c == q:
                q = ""
        elif c in "'\"":
            q = c; cur += c
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            parts.append(cur); cur = ""; i += 1
        elif c in ";|\n":
            parts.append(cur); cur = ""
        elif c == "\\" and i + 1 < len(cmd) and cmd[i + 1] == "\n":
            i += 1                                       # a line continuation
        else:
            cur += c
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n")


def split_heredocs(cmd):
    """(the command with every heredoc body cut out, {terminator: [bodies]}). A psql call carries its SQL in the
    body, and the body's lines are not commands of their own."""
    keep, bodies, i = [], {}, 0
    while True:
        m = HEREDOC.search(cmd, i)
        if not m:
            keep.append(cmd[i:])
            break
        keep.append(cmd[i:m.end()])
        term, body, j = m.group(2), [], m.end()
        while j <= len(cmd):
            e = cmd.find("\n", j)
            line, stop = (cmd[j:], len(cmd)) if e < 0 else (cmd[j:e], e)
            j = stop + 1
            if line.strip() == term:
                break
            body.append(line)
            if e < 0:
                break
        bodies.setdefault(term, []).append("\n".join(body))
        i = min(j, len(cmd))
    return "".join(keep), bodies


HEREDOC_NAME = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")


def heredoc_body(part, bodies):
    """The body of the heredoc that `part` opens, or None."""
    m = HEREDOC_NAME.search(part)
    if not m:
        return None
    found = bodies.get(m.group(2)) or []
    return found[-1] if found else None


def inline_code(part, bodies):
    """(language, code) when this part runs a script written inside the command itself, else (None, «»)."""
    try:
        tok = shlex.split(part, comments=False)
    except ValueError:
        return None, ""
    if not tok:
        return None, ""
    name = os.path.basename(tok[0])
    lang = "py" if name.startswith("python") else "js" if name in ("node", "nodejs") else None
    if not lang:
        return None, ""
    flag = ("-c", "--command") if lang == "py" else ("-e", "--eval", "-p", "--print")
    for i, t in enumerate(tok[1:], 1):
        if t in flag and i + 1 < len(tok):
            return lang, tok[i + 1]
    body = heredoc_body(part, bodies)
    if body is None:
        return None, ""
    rest = [t for t in tok[1:] if t != "-" and not t.startswith("-") and not t.startswith("<<")]
    return (lang, body) if not rest else (None, "")        # «python3 script.py < input» runs a file we cannot read


def inline_ok(code, cwd, h):
    """True when this inline script only writes files inside the work folder: no process, no network, no delete,
    no shell expansion, and no path that is absolute or climbs out of the folder."""
    if not code.strip():
        return False
    if INLINE_NO.search(code) or re.search(r"\)\s*\.replace\s*\(", code):
        return False
    if "$(" in code or "`" in code:
        return False                                       # a shell would run that part first
    for m in PATH_LITERAL.finditer(code):
        s = m.group(2)
        if not s or "://" in s or not s.strip():
            continue
        if (s.startswith("/") or s.startswith("~") or s.startswith("..")) and \
                not any(inside(s, w) for w in workspaces(cwd, h)):
            return False
    return True


WRITE_HINT = re.compile(
    r"\.write(_text|_bytes)?\s*\(|writeFile|appendFile|writelines|createWriteStream|openSync\s*\([^)]*['\"][wa]|"
    r"open\s*\([^)]*['\"][^'\"]*['\"]\s*,\s*['\"][wax+]|\bmakedirs\b|\bmkdir\b|\brmtree\b|\bunlink\b|\.remove\s*\(|"
    r"\brename\s*\(|\btruncate\b|\bsymlink\b|\bto_csv\b|\bto_excel\b|\bto_parquet\b|\bsave\w*\s*\(")


def inline_readonly(code):
    """True when a script written inside the command only reads: no process, no network, no shell expansion, no
    write call, and no path that names a secret. Reading a file of this computer is allowed work (the owner's rule:
    a read-only command never needs the judge), writing is not."""
    if not code.strip():
        return False
    if INLINE_NO.search(code) or "$(" in code or "`" in code or WRITE_HINT.search(code):
        return False
    return not any(m.group(2) and SECRET_PATH.search(m.group(2)) for m in PATH_LITERAL.finditer(code))


def parse_opts(args, takes_value):
    """([(option, value)], [bare words]) for one program's arguments: «-c x», «-cx», «--command=x», «-it»."""
    opts, bare, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            k, eq, v = a.partition("=")
            if eq:
                opts.append((k, v))
                i += 1
            elif k in takes_value and i + 1 < len(args):
                opts.append((k, args[i + 1]))
                i += 2
            else:
                opts.append((k, ""))
                i += 1
        elif a.startswith("-") and len(a) > 1:
            if a in takes_value:                          # an option of more than one letter: «-cmd», «-separator»
                if i + 1 < len(args):
                    opts.append((a, args[i + 1]))
                    i += 2
                else:
                    opts.append((a, ""))
                    i += 1
                continue
            k, rest = "-" + a[1], a[2:]
            while k not in takes_value and rest:
                k, rest = "-" + rest[0], rest[1:]
            if k in takes_value:
                if rest:
                    opts.append((k, rest))
                elif i + 1 < len(args):
                    opts.append((k, args[i + 1]))
                    i += 1
                else:
                    opts.append((k, ""))
            else:                                         # a cluster of flags: «-it», «-Xt»
                opts.extend(("-" + c, "") for c in a[1:])
            i += 1
        else:
            bare.append(a)
            i += 1
    return opts, bare


def clean_sql(sql):
    """SQL without comments and without string literals: a quote cannot hide a word that changes data."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def sql_readonly(sql):
    """True when this SQL only reads: SELECT, SHOW, EXPLAIN of a SELECT, WITH … SELECT, optionally inside
    «begin read only» … «rollback»/«commit». Anything else — any word that changes data — is not read-only."""
    s = re.sub(r"(?m)^\s*\\[a-zA-Z?!]+.*$", "", clean_sql(sql)).strip()   # psql's own \dt, \x: checked apart
    if not s:                                           # only metacommands: psql_metacommands vouched for each one
        return True
    for st in (x.strip().lower() for x in s.split(";")):
        if not st:
            continue
        if SQL_WRITE.search(st):
            return False
        head = SQL_READ.match(st)
        if not head:
            return False
        h = head.group(1)
        if h in ("begin", "start"):
            if not re.search(r"\bread\s+only\b", st):
                return False
        elif h in ("with", "explain"):
            if not re.search(r"\bselect\b", st):
                return False
        elif h in ("set", "reset"):
            if not SET_SAFE.search(st):
                return False
    return True


def psql_metacommands(sql):
    """True when every psql metacommand in the SQL only reads («\\dt», «\\x»); «\\copy», «\\i», «\\o», «\\!» do not."""
    return all(m.group(1).lower() in PSQL_READ for m in re.finditer(r"(?m)^\s*\\([a-zA-Z?!]+)", sql))


def sqlite_metacommands(sql):
    """True when every sqlite dot-command in the text only reads («.tables», «.schema»); «.read», «.import» do not."""
    return all(m.group(1).lower() in SQLITE_READ
               for m in re.finditer(r"(?m)(?:^|[;\n])\s*\.([a-zA-Z]+)", sql))


def sqlite_readonly(sql):
    """True when this sqlite text only reads: SELECT and friends, or a read-only dot-command. A write pragma
    («PRAGMA journal_mode=wal») or «.read»/».import» is not a read."""
    rest = re.sub(r"(?m)^\s*\.[^\n]*", "", sql)
    if not sqlite_metacommands(sql):
        return False
    for st in (x.strip().lower() for x in clean_sql(rest).split(";")):
        if not st:
            continue
        if SQL_WRITE.search(st) or not (SQL_READ.match(st) or re.match(r"pragma\s+\w+\s*\(", st)):
            return False
    return True


def local_host(name):
    return name.strip().lower() in LOCAL_HOSTS


def remote_url(text):
    """The host of a connection string inside `text` («postgres://user@host/db»), or «» when there is none."""
    m = re.search(r"\w+://(?:[^@/\s]*@)?([^/\s:]+)", text)
    return m.group(1) if m else ""


def local_containers():
    """[(name, id)] of the containers that run on this Mac now; None when docker points at another machine or
    does not answer. Only a local daemon: DOCKER_HOST and the docker context must both be a local socket."""
    host = os.environ.get("DOCKER_HOST", "").strip()
    if host and not re.match(r"^(unix|npipe)://", host):
        return None
    try:
        ep = subprocess.run(["docker", "context", "inspect", "-f", "{{.Endpoints.docker.Host}}"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if ep and not re.match(r"^(unix|npipe)://", ep):
            return None
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.ID}}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return [tuple(l.split("\t")) for l in out.splitlines() if l.strip()]


def env_host_local(host, own, names):
    """True when a database host named in a container's environment stays on this Mac: localhost, 127.0.0.1, ::1,
    the container itself, another container of this Mac, or a bare name (a compose service on the same network)."""
    h = host.strip().lower().strip("'\"")
    if h in LOCAL_HOSTS:
        return True
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]*:\d+", h):        # host:port
        h = h.rsplit(":", 1)[0]
    if h in LOCAL_HOSTS or h == own.strip().lower() or h in names:
        return True
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", h))    # a bare name: docker's own network, not a domain


def env_db_host(items, own, names):
    """The first database host of a container's environment that is not on this Mac; «» when all of them are."""
    for item in items:
        k, _, v = str(item).partition("=")
        if not DB_ENV.match(k.strip().upper()) or not v.strip():
            continue
        if "://" in v and not remote_url(v):
            continue                                        # a unix-socket DSN inside the container: local
        host = (remote_url(v) or v).split(",")[0].strip()    # a list of hosts: the first one decides
        if not env_host_local(host, own, names):
            return host
    return ""


def remote_db_host(container):
    """The database host outside this Mac that the container's own environment names: «» when every host stays here,
    None when docker cannot be asked — then the rule does not apply at all."""
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Env}}", container],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None                                    # no such container, or docker is not answering
        items = json.loads(r.stdout) if r.stdout.strip() else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(items, list):
        return None
    return env_db_host(items, container, [n for n, i in (local_containers() or [])])


def db_container(cmd):
    """The database container this command runs a client in, or «» (the judge's fact)."""
    body, bodies = split_heredocs(cmd)
    for p in split_parts(body):
        if re.match(r"^\s*(\S*/)?docker\s+exec\b", p):
            spec = db_exec_spec(p, bodies)
            if spec:
                return spec["container"]
    return ""


def db_exec_spec(part, bodies):
    """{container, program, sql} for `docker exec … psql|pg_dump|mysql|redis-cli …`, or None when it is not that."""
    try:
        tok = shlex.split(part, comments=False)
    except ValueError:
        return None
    if len(tok) < 4 or os.path.basename(tok[0]) != "docker" or tok[1] not in ("exec",):
        return None
    i = 2
    while i < len(tok) and tok[i].startswith("-"):        # docker's own options: -i, -t, -it, -e K=V, -u, -w
        i += 2 if tok[i] in DOCKER_OPT_VALUE else 1
    if i + 1 >= len(tok):
        return None
    container, program, args = tok[i], os.path.basename(tok[i + 1]), tok[i + 2:]
    return client_spec(container, program, args, part, bodies)


def db_client_spec(part, bodies):
    """{container, program, sql} for a database client of this command: inside a container
    (`docker exec … psql …`), or on this Mac itself (`psql -h 127.0.0.1 …`, `sqlite3 file.db 'select …'`).
    None when the part is not a database client at all."""
    try:
        tok = shlex.split(part, comments=False)
    except ValueError:
        return None
    if not tok:
        return None
    if os.path.basename(tok[0]) == "docker":
        return db_exec_spec(part, bodies)
    program = os.path.basename(tok[0])
    if program not in DB_PROGRAMS:
        return None
    return client_spec("", program, tok[1:], part, bodies)


def client_spec(container, program, args, part, bodies):
    """{container, program, sql} when this client's call reads only; None for a write, another host, or no SQL."""
    lang = DB_PROGRAMS.get(program)
    if not lang:
        return None
    if program in ("mysql", "mariadb"):                    # «-pSECRET» is a password, never SQL
        args = [a for a in args if not re.match(r"^-p\w", a)]
    takes = {"postgres": PSQL_VALUE if program == "psql" else PGDUMP_VALUE, "mysql": MYSQL_VALUE,
             "redis": REDIS_VALUE, "sqlite": SQLITE_VALUE}[lang]
    opts, bare = parse_opts(args, takes)
    if any(not local_host(v) for k, v in opts if k in ("-h", "--host", "--uri")):
        return None                                        # another host, not this Mac
    if any(remote_url(a) and not local_host(remote_url(a)) for a in bare + [v for k, v in opts]):
        return None                                        # a connection string to another machine
    if program in ("psql", "pg_dump"):
        if program == "psql" and any(k in ("-f", "--file") for k, v in opts):
            return None                                    # a SQL file whose text we cannot read
        if program == "pg_dump" and not any(k in ("-s", "--schema-only") for k, v in opts):
            return None                                    # a dump of the data is not a look at the schema
        sqls = [v for k, v in opts if k in ("-c", "--command")]
    elif program in ("mysql", "mariadb"):
        sqls = [v for k, v in opts if k in ("-e", "--execute")]
    elif program == "sqlite3":
        # «sqlite3 [options] <file> [sql]»: the file is the first bare word, the SQL comes after it. No SQL at all is
        # an interactive shell that reads its statements from stdin — those are not this part's business.
        sqls = bare[1:] + [v for k, v in opts if k == "-cmd"]
        if not sqls:
            return None
    else:                                                  # redis-cli: the command is a bare word
        words = [bare[0].lower(), bare[1].lower() if len(bare) > 1 else ""]
        if words[0] not in REDIS_READ and not (words[0] == "config" and words[1] == "get"):
            return None
        return {"container": container, "program": program, "sql": ""}
    if program == "pg_dump":                               # a schema dump runs no SQL of its own
        return {"container": container, "program": program, "sql": ""}
    if not sqls:
        body = heredoc_body(part, bodies)
        if body is None:
            return None
        sqls = [body]
    return {"container": container, "program": program, "sql": "\n;\n".join(sqls)}


def redirects(part):
    """True when this part sends its output or input somewhere («>», «>>», «<»): that path is not the SQL's business,
    and only the judge can weigh it."""
    q = ""
    for c in HEREDOC_NAME.sub(" ", part):
        if q:
            if c == q:
                q = ""
        elif c in "'\"":
            q = c
        elif c in "<>":
            return True
    return False


DB_CLIENT = re.compile(r"^\s*(\S*/)?(docker\s+exec|sqlite3|psql|pg_dump|mysql|mariadb|redis-cli)\b")


def db_look_allow(part, bodies):
    """The reason when this one part is a read-only look into a database that stays on this Mac; «» otherwise.
    It is one part, so a chain around it («… | head -30», «cd x && …») no longer sends the whole command to the
    judge (the lesson of 2026-11-20: 396 docker looks a day, almost all of them a SELECT and a pipe)."""
    spec = db_client_spec(part, bodies)
    if not spec or redirects(part):
        return ""
    name = spec["container"]
    if name:
        rows = local_containers()
        if not rows:
            return ""
        if not any(name == n or (re.fullmatch(r"[0-9a-f]{6,64}", name) and i.startswith(name)) for n, i in rows):
            return ""                                      # not a container of this Mac
        far = remote_db_host(name)                         # its own environment may name a database elsewhere
        if far is None or far:
            return ""                                      # unknown, or a host that is not on this Mac
    where = f"the local database container {name}" if name else "a local database"
    if spec["program"] in ("redis-cli", "pg_dump"):
        return f"a read-only look into {where}"
    if spec["program"] == "sqlite3":
        return f"a read-only look into {where}" if sqlite_readonly(spec["sql"]) else ""
    if not psql_metacommands(spec["sql"]) or not sql_readonly(spec["sql"]):
        return ""
    return f"a read-only look into {where}"


def db_exec_allow(cmd):
    """The reason when `cmd` is exactly one read-only look into a database container that runs on this Mac;
    «» when it is not that rule's business (then the judge decides, as before)."""
    body, bodies = split_heredocs(cmd)
    parts = [p for p in split_parts(body) if p]
    db = [p for p in parts if re.match(r"^\s*(\S*/)?docker\s+exec\b", p)]
    rest = [p for p in parts if not re.match(r"^\s*(\S*/)?docker\s+exec\b", p) and not re.match(r"^cd\s+\S+$", p)]
    if len(db) != 1 or rest:
        return ""                                          # one look, with nothing else around it in a chain
    return db_look_allow(db[0], bodies)


def rules(tool, ti, cwd, h):
    """('allow'|'deny', reason) by rule, or (None, '') for the judge."""
    if tool in WRITE_TOOLS or tool in ("Read", "NotebookRead"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if path and SECRET_PATH.search(os.path.realpath(os.path.expanduser(path))):
            return "deny", "a secrets file"
        if tool in WRITE_TOOLS:
            if not path:
                return None, ""
            if any(inside(path, s) for s in workspaces(cwd, h)):
                return "allow", "an edit inside the work folder"
            return "deny", f"an edit outside the work folder ({cwd})"
        return "allow", "reading"
    if tool in READ_TOOLS:
        return "allow", "reading or searching"
    if tool != "Bash":
        return None, ""
    cmd = re.sub(r"\bgit\s+-C\s+\S+", "git", ti.get("command", ""))   # «git -C <dir> push» is a push too
    for pattern, why in DENY_BASH:
        if re.search(pattern, cmd, re.I):
            return "deny", why
    got = db_exec_allow(cmd)                             # a read-only look into this Mac's dev database
    if got:
        return "allow", got
    # every part of a pipeline or a chain must be safe on its own; quoted text (a grep pattern with «\|»,
    # a message) is not split, and loop/compound keywords are not commands. A heredoc body is data for its part,
    # never a command of its own; a script written inside the command is judged by what it may touch.
    body, heredocs = split_heredocs(cmd)
    scripts = reads = dbs = 0
    spaces = workspaces(cwd, h)
    for p in split_parts(body):
        if re.match(r"^\s*(\S*/)?(python[\d.]*|node|nodejs|zsh|bash|sh|eval)\b", p) and ("$(" in p or "`" in p):
            return None, ""                              # «python3 -c "$(…)"» runs text we cannot read as code
        if "$(" in p or "`" in p:                        # «start=$(date +%s)», «for p in $(seq 1 19)»: every
            subs = re.findall(r"\$\(([^()]*)\)", p)      # substitution must be one safe command of its own
            if "`" in p or not all(SAFE_BASH.match(s.strip()) and not re.search(r"[|&;<>`$]", s) for s in subs):
                return None, ""
            p = re.sub(r"\$\([^()]*\)", "x", p)
        if HEREDOC_NAME.search(p) and heredoc_body(p, heredocs) is None:
            return None, ""                              # the body of this heredoc is not in the command at all
        p = re.sub(r"^\s*(\w+=\S+\s+)+", "", p)          # leading VAR=value
        p = re.sub(r"^\s*(time|command)\s+", "", p)      # a plain wrapper: «time python3 …»
        p = re.sub(r"^\s*command\s+-[vV]\s+", "which ", p)   # «command -v x» looks a program up, like which
        p = re.sub(r"^\s*[({]\s*", "", p)                 # a subshell or a group: «(cd x && ls)», «{ echo …»
        p = re.sub(r"\s*[)}]\s*$", "", p)
        p = re.sub(r"\s+2>&1\b|\s+2>\s*/dev/null\b", "", p)
        p = re.sub(r"^\s*(cd\s+\S+)\s*$", "true", p)
        p = re.sub(r"^\s*(do|then|else|fi|done)\b\s*", "", p)          # loop keywords, never «do» of «docker»
        p = re.sub(r"^\s*(for\s+\w+\s+in\b.*|while\b.*|until\b.*|if\b.*|elif\b.*)\s*", "", p)
        p = re.sub(r"^\s*git\s+-C\s+\S+", "git", p)      # git -C <dir> <subcommand>
        p = p.strip() or "true"
        lang, code = inline_code(p, heredocs)
        if lang:
            if inline_readonly(code):
                reads += 1
                continue
            if not inline_ok(code, cwd, h):
                return None, ""                          # a script we cannot vouch for: the judge looks
            scripts += 1
            continue
        if VERSION_HELP.match(p):
            continue
        if DB_CLIENT.match(p):                           # a SELECT and its pipes: no judge
            if not db_look_allow(p, heredocs):
                return None, ""                          # a database call we cannot vouch for
            dbs += 1
            continue
        if re.match(r"^\s*(\S*/)?docker\s+(start|stop|restart)\b", p):
            if local_containers() is None:
                return None, ""                          # docker there does not answer here
            dbs += 1
            continue
        if not SAFE_BASH.match(p):
            return None, ""
        if WRITE_PATHS.match(p) or re.search(r"(^|\s)>>?\s*\S", p):
            head = p.strip().split(None, 1)                  # the program itself is not a target
            args = head[1] if len(head) > 1 else ""
            targets = re.findall(r"(?:>>?\s*|\s)(/[^\s;|&]+|~[^\s;|&]*|\.\.[^\s;|&]*)", " " + args)
            targets = [t if t.startswith(("/", "~")) else os.path.join(cwd, t) for t in targets]
            if any(not any(inside(t, s) for s in spaces) for t in targets):
                return None, ""                          # writing outside: let the judge look
            if re.match(r"^\s*(\S*/)?rm\b", p) and any(t.rstrip("/") == s.rstrip("/") for t in targets for s in spaces):
                return "deny", "deleting the work folder itself"
    if re.search(r"\brm\s+-[a-z]*r[a-z]*f?\b.*(\s/|\s~|\$HOME|\.\.)", cmd):
        return "deny", "deleting outside the work folder"
    return "allow", ("an inline script that writes inside the work folder" if scripts
                     else "a read-only inline script" if reads
                     else "a read-only look into a local database" if dbs
                     else "a safe command inside the work folder")


JUDGE_RULES = (
    "You are the gate for an unattended coding agent (another model) working in the folder "
    "{cwd} (allowed folders: {spaces}; reading any project file on this computer that "
    "is not a secret is allowed too). Decide if this one action may run without asking the user. The FIRST WORD of "
    "your answer is ALLOW or DENY, then one short reason. Do not think aloud.\n"
    "ALLOW: work inside that folder and the temp space — reading, editing, running tests, builds, linters, local "
    "dev servers, installing the project's dependencies, local git that changes no remote, web research, and a "
    "read-only look into a database container that runs on this Mac.\n"
    "DENY: anything that pushes, merges, releases, deploys, writes to prod or another host, reads or prints "
    "secrets (keys, tokens, .env), changes the user's system or settings outside the folder, deletes data outside "
    "the folder, spends money, or that you cannot understand. When in doubt: DENY.\n\n")


def judge_prompt(tool, ti, cwd, precedent=""):
    """The judge's words, with the facts the gate itself can check: a container that runs on this Mac is local."""
    facts = ""
    if re.search(r"\bdocker\b", json.dumps(ti, ensure_ascii=False)):
        rows = local_containers()
        if rows is None:
            facts = ("Fact: docker did not answer here, or it points at another machine, so this Mac's containers are "
                     "unknown — judge the command itself.\n")
        else:
            facts = ("Fact, checked just now: `docker ps` on this Mac lists the containers: "
                     + (", ".join(n for n, i in rows) or "none")
                     + ". A container in that list runs on this Mac — it is a local dev machine, not prod. Reading from "
                     "it (docker exec … psql with SELECT, SHOW, or a «begin read only» … «rollback») is local work: allow "
                     "it unless the command writes data or reaches another host.\n")
        container = db_container(ti.get("command") or "")
        far = remote_db_host(container) if container else ""
        if far:
            facts += (f"Fact, checked just now: the environment of the container {container} points its database at "
                      f"{far}, which is not on this Mac — that database is remote, and a write to it is a write to "
                      "another host.\n")
    return (JUDGE_RULES.replace("{cwd}", cwd).replace("{spaces}", ", ".join(workspaces(cwd, {})))
            + ("Note: this input was copied from a terminal screen. Long lines are wrapped there, so a word, a path or a "
               "token may be split by a line break; judge what the command does, not the split.\n"
               if ti.get("from_screen") else "")
            + facts
            + f"{precedent}Tool: {tool}\nInput: "
            + json.dumps({k: v for k, v in ti.items() if k != "from_screen"}, ensure_ascii=False)[:3000])


def parse_verdict(out):
    """(decision, reason) from a judge's words, or None when it named no verdict."""
    m = re.match(r"\W*(ALLOW|DENY)\b[\s:.-]*(.*)", out, re.S)
    if m:
        return ("allow" if m.group(1) == "ALLOW" else "deny"), " ".join(m.group(2).split())[:200]
    verdicts = re.findall(r"\b(ALLOW|DENY)\b", out)      # it thought aloud: its last verdict counts
    if not verdicts:
        return None
    return ("allow" if verdicts[-1] == "ALLOW" else "deny"), " ".join(out.split())[-200:]


def judge(tool, ti, cwd):
    """(decision, why, provider) from the first link of the judge role that answers, or None when none does.
    Claude is the last link, never the first: its calls spend the user's own 5-hour window."""
    precedent = ""
    try:                                                # the user's earlier decisions about the same kind of action
        import know
        found, vectors = know.search(f"{tool} {json.dumps(ti, ensure_ascii=False)[:400]}", "", 4)
        rows = [r for s, r, cos, exact in found if r["who"] == "user" and (cos >= 0.55 if vectors else exact)]
        if rows:
            precedent = ("\nThe user's earlier words that may bear on it (follow them when they clearly cover this action):\n"
                         + "\n".join(f"- [{r['source']}] {r['text'][:300]}" for r in rows[:3]) + "\n")
    except Exception:
        precedent = ""
    text, link, _notes = judge_role.ask(judge_prompt(tool, ti, cwd, precedent), judged=judge_role.self_link())
    got = parse_verdict(text) if text else None
    if not got:
        return None
    name = (link.get("model") or link["provider"]).split(":")[0]
    return got[0], f"judge {name}: {got[1]}", link["provider"]


def read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


FINGERPRINT = ""


def rules_fingerprint():
    """A short hash of the rules and of the judge's words. The cache is keyed by it, so a fixed rule or a better
    judge prompt applies at once: a deny cached under the old rules is asked again, never repeated for the user
    (the lesson of 2026-11-18: a wrong deny was cached and shown every 30 s)."""
    global FINGERPRINT
    if not FINGERPRINT:
        import inspect
        try:
            src = "|".join([repr(DENY_BASH), SAFE_BASH.pattern, JUDGE_RULES, inspect.getsource(rules),
                            inspect.getsource(db_look_allow), inspect.getsource(db_client_spec),
                            inspect.getsource(client_spec), inspect.getsource(sql_readonly),
                            inspect.getsource(sqlite_readonly), repr(sorted(PSQL_READ)), repr(sorted(REDIS_READ)),
                            repr(sorted(SQLITE_READ)), repr(judge_chain()), WRITE_HINT.pattern,
                            INLINE_NO.pattern, inspect.getsource(inline_readonly), inspect.getsource(inline_ok),
                            inspect.getsource(inline_code)])
        except (OSError, TypeError):
            src = repr(DENY_BASH) + SAFE_BASH.pattern + JUDGE_RULES
        FINGERPRINT = hashlib.sha1(src.encode()).hexdigest()[:12]
    return FINGERPRINT


def cache_key(cwd, tool, ti):
    """The same action in the same folder under the same rules is judged once."""
    return hashlib.sha1(f"{rules_fingerprint()}|{cwd}|{tool}|{json.dumps(ti, sort_keys=True)}".encode()).hexdigest()


def decide(tool, ti, cwd, h, cached=True):
    """(decision, why, via, provider): by rule, from the cache, or from the first judge that answers."""
    decision, why = rules(tool, ti, cwd, h)
    via, provider = "rule", ""
    if decision is None:
        key = cache_key(cwd, tool, ti)
        cache = read(CACHE, {})
        if cached and key in cache:
            decision, why, via = cache[key]["d"], cache[key]["why"], "cache"
            provider = cache[key].get("p", "")
        else:
            got = judge(tool, ti, cwd)
            if got:
                decision, why, via, provider = got[0], got[1], "judge", got[2]
                cache[key] = {"d": decision, "why": why, "at": int(time.time()), "p": provider}
                if len(cache) > 2000:
                    cache = dict(sorted(cache.items(), key=lambda kv: kv[1]["at"])[-1500:])
                os.makedirs(os.path.dirname(CACHE), exist_ok=True)
                with open(CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
            else:
                decision, why, via = "deny", "no judge answered: denied to be safe", "judge"
    return decision, why, via, provider


def hook():
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    tool, ti, cwd = h.get("tool_name", ""), h.get("tool_input") or {}, h.get("cwd") or os.getcwd()
    decision, why, via, provider = decide(tool, ti, cwd, h)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "session": h.get("session_id", ""),
                            "cwd": cwd, "tool": tool, "input": json.dumps(ti, ensure_ascii=False)[:300],
                            "decision": decision, "via": via, "why": why,
                            **({"provider": provider} if provider else {})}, ensure_ascii=False) + "\n")
    reason = f"genius-ai gate ({via}): {why}"
    if decision == "deny":
        reason += ". Do not try to get around it; if the task needs this, stop and put it in your report as a question."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def selftest():
    """The read-only DB rule, the cache key, the judge's fact, the commands that reached the judge on 2026-11-20 and
    the order of the judge chain. No docker, no model, no user — «selftest ollama» also asks the live Ollama."""
    fails = []

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    global local_containers, remote_db_host, FINGERPRINT
    real, real_far = local_containers, remote_db_host
    local_containers = lambda: [("chica-db-1", "3f2a9c1b7d44"), ("chica-redis", "aa11bb22cc33")]
    remote_db_host = lambda c: ""                          # every fake container keeps its database here
    live = ("docker exec -i chica-db-1 psql -X -d chica_clean -U chica -v ON_ERROR_STOP=1 <<'SQL'\n"
            "begin read only;\nselect id, name from orders limit 5;\nrollback;\nSQL")
    try:
        d, why = rules("Bash", {"command": live}, "/tmp", {})
        check("the live command is allowed by a rule", d == "allow" and "local database" in why, f"{d}: {why}")
        bad = [
            ("the same with an UPDATE", live.replace("select id, name from orders limit 5", "update orders set x = 1")),
            ("an INSERT inside a read-only block", "docker exec -i chica-db-1 psql -c 'begin read only; insert into t values (1); rollback'"),
            ("a DELETE", "docker exec -i chica-db-1 psql -c 'delete from orders'"),
            ("a write inside a CTE", "docker exec -i chica-db-1 psql -c 'with d as (delete from t returning *) select * from d'"),
            ("a sequence that changes", "docker exec -i chica-db-1 psql -c \"select nextval('orders_id_seq')\""),
            ("«set transaction read write»", "docker exec -i chica-db-1 psql -c 'set transaction read write; select 1'"),
            ("«begin» without «read only»", "docker exec -i chica-db-1 psql -c 'begin; select 1; rollback'"),
            ("\\copy from a file", "docker exec -i chica-db-1 psql <<'SQL'\n\\copy orders from '/tmp/o.csv' csv\nSQL"),
            ("a dump of the data, not the schema", "docker exec -i chica-db-1 pg_dump -d chica_clean"),
            ("a psql to another host", "docker exec -i chica-db-1 psql -h db.prod.example.com -c 'select 1'"),
            ("a connection string to another machine",
             "docker exec -i chica-db-1 psql postgres://u:p@db.prod.example.com/x -c 'select 1'"),
            ("a container this Mac does not run", "docker exec -i other-db psql -c 'select 1'"),
            ("a pipeline into a script we cannot read",
             "docker exec -i chica-db-1 psql -c 'select 1' | python3 -c 'import os; os.system(\"ls\")'"),
            ("a redirect that writes a file", "docker exec -i chica-db-1 psql -c 'select 1' > /tmp/out.csv"),
            ("a redis write", "docker exec -i chica-redis redis-cli set k v"),
            ("a mysql write", "docker exec -i chica-db-1 mysql -e 'update t set x = 1'"),
        ]
        for name, cmd in bad:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"not by rule: {name}", "local database" not in (why or ""), f"{d}: {why}")
        good = [
            ("psql -c select", "docker exec -i chica-db-1 psql -c 'select 1'"),
            ("a read-only block in one -c",
             "docker exec -i chica-db-1 psql -c 'begin read only; select 1; rollback'"),
            ("a schema dump", "docker exec -i chica-db-1 pg_dump --schema-only -d chica_clean"),
            ("mysql select", "docker exec -i chica-db-1 mysql -e 'select count(*) from orders'"),
            ("redis keys", "docker exec -i chica-redis redis-cli keys '*'"),
            ("redis config get", "docker exec -i chica-redis redis-cli config get maxmemory"),
            ("psql «cd … &&» in front", "cd /tmp && docker exec -i chica-db-1 psql -c 'select 1'"),
            ("psql by container id", "docker exec -i 3f2a9c1b7d44 psql -c 'select 1'"),
            ("«\\dt» is a read", "docker exec -i chica-db-1 psql <<'SQL'\n\\dt\nselect 1;\nSQL"),
            ("a read with a pipe and a chain",
             "cd /tmp && docker exec -i chica-db-1 psql -U chica -d chica_clean -tAc \"select title from brand\" "
             "2>&1 | head -30"),
            ("a psql on this Mac's own address", "psql -h 127.0.0.1 -p 4432 -U chica_app -d chica_clean -tAc 'select 1'"),
            ("a sqlite read", "sqlite3 state/knowledge.db 'select count(*) from records'"),
            ("a sqlite read of the schema", "sqlite3 -cmd '.headers on' state/knowledge.db '.tables'"),
        ]
        for name, cmd in good:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"by rule: {name}", d == "allow", f"{d}: {why}")
        body, bodies = split_heredocs(live)
        check("the heredoc body is cut out of the command", "select id" not in body and bodies.get("SQL"), str(bodies)[:80])
        # a script written inside the command (the second false deny of 2026-11-18: a worktree README write)
        script = ("python3 - <<'PY'\nfrom pathlib import Path\n"
                  "Path('docs/working/x/README.md').write_text('# x\\n', encoding='utf-8')\nPY\n"
                  "git add docs/working/x/README.md && git diff --cached --check && git commit -m 'docs: x' && git status")
        d, why = rules("Bash", {"command": script}, "/tmp", {})
        check("an inline script that writes inside, plus local git, is allowed", d == "allow", f"{d}: {why}")
        for name, cmd in [
            ("a path outside the folder", script.replace("Path('docs/working/x/README.md')", "Path('/etc/motd')")),
            ("a climb out of the folder", script.replace("Path('docs/working/x/README.md')", "Path('../x.md')")),
            ("subprocess", script.replace("from pathlib import Path", "import subprocess, pathlib")),
            ("urllib", script.replace("from pathlib import Path", "import urllib.request")),
            ("requests", script.replace("from pathlib import Path", "import requests")),
            ("a delete", script.replace(".write_text(", ".unlink() or Path('x').write_text(")),
            ("a shell substitution", 'python3 -c "$(echo hi)"'),
            ("an eval", "python3 -c \"eval('1 + 1')\""),
            ("python -c writing outside", "python3 -c \"open('/etc/passwd', 'w')\""),
            ("node spawning a process", "node -e \"require('child_process').execSync('ls')\""),
        ]:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"to the judge: an inline script with {name}", d is None, f"{d}: {why}")
        for name, cmd in [
            ("python -c that only prints", "python3 -c 'print(1 + 1)'"),
            ("python -c that writes inside", "python3 -c \"open('notes.txt', 'w').write('x')\""),
            ("node that writes inside", "node -e \"require('fs').writeFileSync('docs/x.md', 'y')\""),
            ("a heredoc that is data for grep", "grep -c . <<'EOF'\nhello\nEOF"),
        ]:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"by rule: {name}", d == "allow", f"{d}: {why}")
        # the container's own environment: where its database really is
        check("a database named by a bare service name stays here",
              env_db_host(["PGHOST=db", "REDIS_URL=redis://redis:6379", "MYSQL_HOST=127.0.0.1:3306"], "chica-db-1",
                          ["chica-db-1"]) == "")
        check("a database named by the container's own name stays here",
              env_db_host(["DATABASE_URL=postgres://u:p@chica-db-1:5432/x"], "chica-db-1", ["chica-db-1"]) == "")
        check("a unix-socket DSN stays inside the container",
              env_db_host(["DATABASE_URL=postgresql:///chica?host=/var/run/postgresql"], "chica-db-1", []) == "")
        check("a domain in the environment is not local",
              env_db_host(["PGHOST=db", "DATABASE_URL=postgres://u:p@db.prod.example.com:5432/x"], "chica-db-1",
                          ["chica-db-1"]) == "db.prod.example.com")
        check("an address outside the loopback is not local",
              env_db_host(["MYSQL_HOST=10.0.0.5:3306"], "chica-db-1", []) == "10.0.0.5:3306")
        remote_db_host = lambda c: "db.prod.example.com"
        try:
            d, why = rules("Bash", {"command": "docker exec -i chica-db-1 psql -c 'select 1'"}, "/tmp", {})
            check("a container whose database is elsewhere is not allowed by rule",
                  "local database" not in (why or ""), f"{d}: {why}")
            p = judge_prompt("Bash", {"command": "docker exec -i chica-db-1 psql -c 'select 1'"}, "/tmp")
            check("the judge is told where that database is", "db.prod.example.com" in p and "remote" in p, p[-240:])
            remote_db_host = lambda c: None
            d, why = rules("Bash", {"command": "docker exec -i chica-db-1 psql -c 'select 1'"}, "/tmp", {})
            check("an unknown database host is not allowed by rule", "local database" not in (why or ""), f"{d}: {why}")
        finally:
            remote_db_host = lambda c: ""
        local_containers = real
        os.environ["DOCKER_HOST"] = "ssh://prod-host"
        try:
            check("a remote DOCKER_HOST has no local containers", local_containers() is None)
            d, why = rules("Bash", {"command": "docker exec -i chica-db-1 psql -c 'select 1'"}, "/tmp", {})
            check("…so that command goes to the judge", d is None, f"{d}: {why}")
        finally:
            os.environ.pop("DOCKER_HOST", None)
            local_containers = lambda: [("chica-db-1", "3f2a9c1b7d44")]
        p = judge_prompt("Bash", {"command": "docker exec -i chica-db-1 psql -c 'select 1'"}, "/tmp")
        check("the judge is told which containers run here", "chica-db-1" in p and "local dev machine" in p, p[-200:])
        check("the judge hears no docker fact about a plain edit",
              "docker ps" not in judge_prompt("Edit", {"file_path": "/tmp/a.ts"}, "/tmp"))
        check("the cache key is a sha1 of the rules' fingerprint and the action",
              len(cache_key("/tmp", "Bash", {"command": "x"})) == 40 and len(rules_fingerprint()) == 12)
        fp1 = rules_fingerprint()
        DENY_BASH.append((r"\bzzz-selftest\b", "a made-up deny"))
        try:
            FINGERPRINT = ""
            fp2 = rules_fingerprint()
        finally:
            DENY_BASH.pop()
            FINGERPRINT = ""
        check("a changed rule is another fingerprint, so the cache is asked again", fp1 != fp2 and len(fp2) == 12,
              f"{fp1} vs {fp2}")
        # The commands that reached the judge on 2026-11-20 (the day of 252 Claude calls): a rule or the Ollama judge
        # answers them now, never Claude. A command that judge DENIED and the rules allow carries the reason it is
        # safe, so a silent flip fails here.
        local_containers = lambda: [("app-db-1", "3f2a9c1b7d44"), ("app-redis", "aa11bb22cc33")]
        try:
            with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "gate-fixtures.json"),
                      encoding="utf-8") as f:
                fx = json.load(f)
        except (OSError, ValueError):
            fx = {}
        top = fx.get("top") or []
        app = "/Users/you/app"
        check("the commands that reached the judge on 2026-11-20 are the fixtures", len(top) >= 40, f"{len(top)}")
        for e in top:
            d, why = rules("Bash", {"command": e["cmd"]}, e.get("cwd") or app, {})
            head = " ".join(e["cmd"].split())[:60]
            if e.get("now") == "rule-allow":
                check("by rule: " + head, d == "allow", f"{d}: {why}")
            else:
                check("to the judge: " + head, d is None, f"{d}: {why}")
            if e.get("was") == "deny" and d == "allow":
                check("an old deny says why it passes now: " + head, bool(e.get("reviewed")), "no «reviewed» note")
        for cmd in fx.get("deny") or []:
            d, why = rules("Bash", {"command": cmd}, app, {})
            check("denied: " + cmd[:60], d == "deny", f"{d}: {why}")
        for cmd in fx.get("judge") or []:
            d, why = rules("Bash", {"command": cmd}, app, {})
            check("to the judge: " + cmd[:60], d is None, f"{d}: {why}")
        # the chain: the local Ollama decides first, Claude only when every other link is silent
        real_ollama, real_codex, real_claude = judge_role.ollama_text, judge_role.codex_text, judge_role.claude_text
        real_self = judge_role.self_link
        judge_role.self_link = lambda: {}                # no agent judged here: the whole chain is asked
        asked = []
        try:
            judge_role.ollama_text = lambda model, prompt, *a, **k: asked.append(model) or "ALLOW a script we cannot read"
            judge_role.codex_text = lambda prompt, *a, **k: asked.append("codex") or ""
            judge_role.claude_text = lambda prompt, *a, **k: asked.append("claude") or ""
            got = judge("Bash", {"command": "zsh /tmp/unknown.sh"}, "/tmp")
            check("the judge asks the local Ollama first, never Claude", bool(got) and got[2] == "ollama"
                  and "claude" not in asked, f"{got} asked {asked}")
            judge_role.ollama_text = lambda model, prompt, *a, **k: asked.append(model) or ""
            judge_role.codex_text = lambda prompt, *a, **k: "DENY the judge of last resort"
            got = judge("Bash", {"command": "zsh /tmp/unknown.sh"}, "/tmp")
            check("Codex answers when every Ollama link is silent", bool(got) and got[2] == "codex", str(got))
            judge_role.codex_text = lambda prompt, *a, **k: ""
            judge_role.claude_text = lambda prompt, *a, **k: "DENY the judge of last resort"
            got = judge("Bash", {"command": "zsh /tmp/unknown.sh"}, "/tmp")
            check("Claude answers only when every other link is silent", bool(got) and got[2] == "claude", str(got))
            if "ollama" in sys.argv:
                text = real_ollama(judge_chain()[0][1], judge_prompt("Bash", {"command": "zsh /tmp/unknown.sh"}, "/tmp"))
                check("the live Ollama link answers ALLOW or DENY", bool(parse_verdict(text or "")), (text or "")[:80])
        finally:
            judge_role.ollama_text, judge_role.codex_text, judge_role.claude_text = real_ollama, real_codex, real_claude
            judge_role.self_link = real_self
    finally:
        local_containers, remote_db_host = real, real_far
    print(f"\ngate selftest: {len(fails)} failed" if fails else "\ngate selftest: ok")
    return 1 if fails else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        hook()
    elif a[0] == "selftest":
        sys.exit(selftest())
    elif a[0] == "log":
        n = int(a[2]) if len(a) > 2 and a[1] == "-n" else 20
        try:
            lines = open(LOG, encoding="utf-8").read().splitlines()[-n:]
        except OSError:
            lines = []
        for l in lines:
            r = json.loads(l)
            print(f"{r['at'][11:19]} {r['decision']:5} {r['via']:5} {r['tool']:6} {r['input'][:70]} — {r['why'][:80]}")
    elif a[0] == "test" and len(a) >= 3:
        cwd = os.path.realpath(a[3]) if len(a) > 3 else os.getcwd()
        print(*decide(a[1], json.loads(a[2]), cwd, {}, cached=False))
    elif a[0] == "chain":
        for provider, model in judge_chain():
            print(f"{provider} {model}")
    else:
        sys.exit("usage: bin/genius gate log [-n N] | gate test <tool> '<input json>' [cwd] | gate chain   (hook: gate.py)")
