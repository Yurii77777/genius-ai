"""The gate: decides every action of an unattended agent, so nobody has to answer its permission prompts.

    (hook)  PreToolUse   python3 bin/lib/gate.py      — reads the hook JSON on stdin, answers allow, deny or ask
    bin/genius gate log [-n 20]                       the latest decisions
    bin/genius gate test <tool> '<input json>' [cwd]  what the gate would answer
    python3 -I bin/lib/gate.py selftest               the read-only DB rule and the stack-stop rule, without docker

The user's decision of 2026-11-17: agents on other models (DeepSeek through Ollama) must not wait for the
user, and «auto» mode cannot work for them (its safety check runs on Anthropic's servers). bin/genius agent
gives such an agent this hook (`claude --settings`); the agent itself runs in the «default» mode, so every
action reaches the gate first.

How it decides: rules only, at once, with no model (the user's decision of 2026-11-21: «те, що не покривають
правила, йде до координатора — суддю вирізати»).
1. Deny: pushing, merging, releasing, prod and remote hosts (ssh, scp, deploy commands), secrets (~/.ssh,
   .env files, keychains), deleting outside the work folder, privilege, wiping volumes, disabling TLS.
2. Allow: reading, searching, editing inside the work folder or the scratch space, local git that changes no
   remote, tests, builds, linters, the heavy queue, the knowledge base, web search, a read-only look into a
   database container that runs on this Mac, and stopping such a stack (`docker stop <name>`,
   `docker compose stop`: never `down`, `--volumes` or `rm`).
3. No rule decides → «ask»: nothing runs, nothing is pressed. The coordinator and the user decide
   (bin/lib/agents_watch.py brings that action to the coordinator's session).
Every decision goes to state/gate.jsonl; the agent gets the reason with a deny, and can report it.

The gate never decides what only the user decides (a merge, a write to prod, money, lifting a hold): those
are denied by rule, and the agent reports them as questions.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
LOG = os.path.join(ROOT, "state", "gate.jsonl")
HOME = os.path.expanduser("~")

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
    (r"\b(shutdown|reboot|killall|pkill)\b|\bkill\s+-9\s+-1\b", "the user's processes"),
]
SAFE_BASH = re.compile(
    r"^\s*([\w.-]+\s+(-v|-V|--version)(\s+2>&1)?\s*$|\[\s|\[\[\s|continue\b|break\b|ls|pwd|cat|head|tail|wc|grep|rg|find|fd|tree|stat|file|du|df|which|type|echo|printf|date|sort|uniq|cut|tr|"
    r"jq|diff|cmp|pdftotext|pdfinfo|lsof|ps|pgrep|basename|dirname|realpath|readlink|test|true|false|sleep|env|whoami|uname|sw_vers|nproc|"
    r"git\s+(status|diff|log|show|branch|rev-parse|ls-files|grep|blame|stash|add|commit|checkout|switch|restore|"
    r"worktree|fetch|merge-base|cherry-pick|rebase|tag\s+-l|describe|config\s+--get|mv|rm|apply|rev-list|cherry|check-ignore|ls-tree|cat-file)|"
    r"gh\s+(pr|issue|run|repo)\s+(view|list|diff|checks|status)|gh\s+api\s+(?!.*(-X|--method))|"
    r"node|tsx|npx\s+(tsx|vitest|jest|playwright|tsc|eslint|prettier|biome|turbo)|tsc|eslint|prettier|vitest|jest|"
    r"(npm|pnpm|yarn|bun)\s+(vitest|jest|playwright|tsc|eslint|prettier|biome|turbo|run|test|exec|install|i|ci|add|remove|why|ls|list|outdated|audit|lint|typecheck|build|dlx)|"
    r"python3?|[\w./~-]*\.venv/bin/python3?|swift|pytest|ruff|mypy|uv\s+(run|pip|sync)|pip3?\s+(install|show|list)|go\s+(test|build|vet|run)|cargo\s+(test|build|check|clippy)|"
    r"make|mkdir|touch|cp|mv|rm|ln\s+-s[fn]*|set\s+--|sed|awk|tee|xargs|curl|wget|tar|unzip|zip|gzip|shasum|md5|sips|ffmpeg|ffprobe|magick|convert|"
    r"docker\s+(ps|logs|images|inspect|start|volume\s+(ls|inspect)|network\s+(ls|inspect)|compose\s+((-p|-f|--file|--project-name|--env-file)\s+\S+\s+)*(ps|logs|stop|start|up|config))|hc\s+(ls|get|read|wait)|"
    r"[\w./~-]*bin/heavy|[\w./~-]*bin/genius\s+(know|models|log|focus|load|--help|help|gate\s+(log|test))|[\w./~-]*bin/status)\b")
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "TodoRead", "Task", "Agent", "AskUserQuestion",
              "WebSearch", "WebFetch", "ToolSearch", "Skill", "ExitPlanMode", "BashOutput", "KillShell", "Monitor"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SECRET_PATH = re.compile(r"(/\.ssh/|/\.aws/|/\.kube/|/\.docker/config|/\.netrc|/\.gnupg/|(^|/)\.env(\.[\w.-]+)?$|\.pem$|\.p12$|"
                         r"/Library/Keychains/|/\.claude/\.env$|/\.config/gh/)")

# A read-only look into a database that runs in a container on this Mac (the lesson of 2026-11-18).
# `docker exec -i demo-db-1 psql …` with a «begin read only … rollback» and SELECTs went to the judge then, which
# denied it, so the user had to press the button every 30 s. The rules answer it first.
DB_PROGRAMS = {"psql": "postgres", "pg_dump": "postgres", "mysql": "mysql", "mariadb": "mysql", "redis-cli": "redis"}
SQL_WRITE = re.compile(r"\b(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy|call|do|reindex|refresh|"
                       r"cluster|vacuum|nextval|setval|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|"
                       r"lo_import|lo_export)\b")
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
    r"(?<!\.)\b(subprocess|child_process|execSync|execFile|spawn\w*|popen|fork|shutil|socket|urllib|requests|httpx|ftplib|"
    r"smtplib|paramiko|telnetlib|ctypes|pty|webbrowser|fetch|XMLHttpRequest|dgram|eval|exec|compile|__import__|"
    r"importlib|input)\b"   # sys.stdin is data from a pipe; exec/eval stay banned (21.11)
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
    # the agent's own report and drafts in genius-ai are part of its work (every brief asks for a report there)
    spaces = [cwd, "/tmp", "/private/tmp", os.environ.get("TMPDIR", "/tmp"),
              os.path.join(ROOT, "state", "agents"), os.path.join(ROOT, "state", "drafts")]
    if h.get("scratchpad_dir"):
        spaces.append(h["scratchpad_dir"])
    return [s for s in spaces if s]


def split_parts(cmd):
    """Split a shell command at && || ; | and newlines, but never inside quotes."""
    parts, cur, q, i, b = [], "", "", 0, 0
    while i < len(cmd):
        c = cmd[i]
        if not q and cmd.startswith("${", i):            # «${pair%%|*}» — a | inside ${…} is not a pipe (21.11)
            b += 1; cur += "${"; i += 2; continue
        if b and not q and c == "}":
            b -= 1; cur += c; i += 1; continue
        if b and not q:
            cur += c; i += 1; continue
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
    writes = re.search(r"""open\([^)]*['"][wax]b?\+?['"]|\.write(_text|_bytes)?\(|\bwriteFile|appendFile|mkdir|makedirs|\.touch\(|copyfile|\.save\(|>\s*\S""", code)
    if not writes:
        return True                                       # a script that only reads may name any path (21.11)
    for m in PATH_LITERAL.finditer(code):
        s = m.group(2)
        if not s or "://" in s or not s.strip():
            continue
        if (s.startswith("/") or s.startswith("~") or s.startswith("..")) and \
                not any(inside(s, w) for w in workspaces(cwd, h)):
            return False
    return True


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
    s = re.sub(r"(?m)^\s*\\[a-zA-Z?!]+\S*", "", clean_sql(sql)).strip()   # psql's own \dt, \x: checked apart
    if not s:
        return False
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
    lang = DB_PROGRAMS.get(program)
    if not lang:
        return None
    if program in ("mysql", "mariadb"):                    # «-pSECRET» is a password, never SQL
        args = [a for a in args if not re.match(r"^-p\w", a)]
    takes = {"postgres": PSQL_VALUE if program == "psql" else PGDUMP_VALUE, "mysql": MYSQL_VALUE,
             "redis": REDIS_VALUE}[lang]
    opts, bare = parse_opts(args, takes)
    if any(not local_host(v) for k, v in opts if k in ("-h", "--host", "--uri")):
        return None                                        # another host, not this Mac's container
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
    and only the coordinator can weigh it."""
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


def db_exec_allow(cmd):
    """The reason when `cmd` is exactly one read-only look into a database container that runs on this Mac;
    «» when it is not that rule's business (then no rule decides, and the coordinator does)."""
    body, bodies = split_heredocs(cmd)
    parts = [p for p in split_parts(body) if p]
    db = [re.sub(r"\s+2>&1\s*$", "", p) for p in parts if re.match(r"^\s*(\S*/)?docker\s+exec\b", p)]
    rest = [p for p in parts if not re.match(r"^\s*(\S*/)?docker\s+exec\b", p) and not re.match(r"^cd\s+\S+$", p)]
    # around the looks only filters of their output and labels (`| head`, `; echo "---"`): the lesson of 2026-11-21
    if not db or any(not READ_FILTER.match(p) or redirects(p) for p in rest) or any(redirects(d) for d in db):
        return ""
    for d in db:
        got = db_one_look(d, bodies)
        if not got:
            return ""
    return got


READ_FILTER = re.compile(r"^\s*(head|tail|grep|wc|cut|sort|uniq|cat|tr|echo|printf)\b[^`$]*$")


def db_one_look(part, bodies):
    """The reason when this one part is a read-only look into a local database container, else «»."""
    spec = db_exec_spec(part, bodies)
    if not spec:
        return ""
    name = spec["container"]
    rows = local_containers()
    if not rows:
        return ""
    if not any(name == n or (re.fullmatch(r"[0-9a-f]{6,64}", name) and i.startswith(name)) for n, i in rows):
        return ""                                          # not a container of this Mac
    far = remote_db_host(name)                             # its own environment may name a database elsewhere
    if far is None or far:
        return ""                                          # unknown, or a host that is not on this Mac
    if spec["program"] in ("redis-cli", "pg_dump"):
        return f"a read-only look into the local database container {name}"
    sql = re.sub(r"(?m)^\s*\\[a-zA-Z?!+]+[^\n]*$", "", spec["sql"])   # «\d brand» reads: checked by psql_metacommands
    if not psql_metacommands(spec["sql"]) or (sql.strip() and not sql_readonly(sql)):
        return ""
    return f"a read-only look into the local database container {name}"


COMPOSE_VALUE = {"-f", "--file", "-p", "--project-name", "--env-file", "--profile", "--ansi", "--progress",
                 "-H", "--host", "--context", "--project-directory"}


def stop_spec(part):
    """{'names': [...]} for `docker stop <name> …`, {'services': [...]} for `docker compose stop …`, else None."""
    try:
        tok = shlex.split(part, comments=False)
    except ValueError:
        return None
    if not tok or os.path.basename(tok[0]) != "docker":
        return None
    i = 1
    while i < len(tok) and tok[i] in ("-H", "--host", "-c", "--context", "--config"):   # docker's own options
        i += 2
    if i >= len(tok):
        return None
    if tok[i] == "stop":
        names = tok[i + 1:]
        if not names or any(n.startswith("-") or not re.fullmatch(r"[A-Za-z0-9][\w.-]*", n) for n in names):
            return None                                    # an option, a substitution, an empty call: not that rule
        return {"names": names}
    if tok[i] != "compose":
        return None
    i += 1
    while i < len(tok) and tok[i].startswith("-"):
        i += 2 if tok[i] in COMPOSE_VALUE else 1
    if i >= len(tok) or tok[i] != "stop":
        return None                                        # `down`, `rm`, `up`: not this rule's business
    services = tok[i + 1:]
    if any(s.startswith("-") and s not in ("--remove-orphans",) for s in services):
        return None
    services = [s for s in services if not s.startswith("-")]
    if any(not re.fullmatch(r"[A-Za-z0-9][\w.-]*", s) for s in services):
        return None
    return {"services": services}


def stop_allow(cmd):
    """The reason when `cmd` stops containers that run on this Mac: `docker stop <name>` and `docker compose stop`.
    Never `down`, never `--volumes`, never `rm` — those stay denied or unknown (the lesson of 2026-11-21: an agent
    started demo-db-1, the gate refused its `docker stop`, and the stack stayed up)."""
    body, _ = split_heredocs(cmd)
    parts = [p for p in split_parts(body) if p and not re.match(r"^cd\s+\S+$", p)]
    if len(parts) != 1 or redirects(parts[0]):
        return ""                                          # nothing else around it in a chain
    spec = stop_spec(parts[0])
    if not spec:
        return ""
    rows = local_containers()
    if not rows:
        return ""                                          # docker points elsewhere, or does not answer
    if "names" in spec:
        for name in spec["names"]:
            if not any(name == n or (re.fullmatch(r"[0-9a-f]{6,64}", name) and i.startswith(name)) for n, i in rows):
                return ""                                  # not a container of this Mac
        return "stopping a stack of this Mac: " + ", ".join(spec["names"])
    return "stopping a compose stack of this Mac" + (": " + ", ".join(spec["services"]) if spec["services"] else "")


def rules(tool, ti, cwd, h):
    """('allow'|'deny', reason) by rule, or (None, '') when no rule decides — then the coordinator does."""
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
    got = stop_allow(cmd)                                # stopping a stack this agent itself left running
    if got:
        return "allow", got
    # every part of a pipeline or a chain must be safe on its own; quoted text (a grep pattern with «\|»,
    # a message) is not split, and loop/compound keywords are not commands. A heredoc body is data for its part,
    # never a command of its own; a script written inside the command is judged by what it may touch.
    # «$(…)» inside a command: judge the inner command by these same rules; a safe one becomes a plain word (21.11)
    if "$(" in cmd and re.search(r"\b(python3?|node|ruby|perl|php|deno|bun|sh|bash|zsh)\b[^|;&]*\s-[ceE]\b|\beval\b|\bsource\b|(^|[\s;&|])\.\s", cmd):
        return None, ""                                  # «python3 -c "$(curl …)"» runs fetched code: never by rule
    for _ in range(8):
        m = re.search(r"\$\(([^()]*)\)", cmd)
        if not m:
            break
        inner = m.group(1)
        if not inner.strip():
            return None, ""
        d, _why = rules("Bash", {"command": inner}, cwd, h)
        if d != "allow":
            return None, ""
        cmd = cmd[:m.start()] + "X" + cmd[m.end():]
    body, heredocs = split_heredocs(cmd)
    scripts = 0
    for p in split_parts(body):
        p = re.sub(r"""^\s*(export\s+)?\w+=('[^']*'|"[^"$`]*"|[^\s$`'"]*)\s*$""", "true", p)   # UA='Mozilla/5.0 (…)' (21.11)
        p = re.sub(r"^\s*(\w+=\S+\s+)+", "", p)        # leading VAR=value
        p = re.sub(r"^\s*(time|nice)\s+", "", p)                    # «time <cmd>» is the command itself (21.11)
        p = re.sub(r"^\s*(cd\s+\S+)\s*$", "true", p)
        for _k in range(4):                              # «do for r in …» — a keyword can follow a keyword
            q = re.sub(r"^\s*(do|then|else|fi|done|for\s+\w+\s+in\b.*|while\b.*|until\b.*|if)(?=\s|$)\s*", "", p)
            if q == p:
                break
            p = q
        p = p or "true"                                  # \b-less «do» ate «docker» (21.11)
        p = re.sub(r"^\s*git\s+-C\s+\S+", "git", p)      # git -C <dir> <subcommand>
        lang, code = inline_code(p, heredocs)
        if lang:
            if not inline_ok(code, cwd, h):
                return None, ""                          # a script we cannot vouch for: the coordinator looks
            scripts += 1
            continue
        if not SAFE_BASH.match(p):
            return None, ""
        if re.match(r"^\s*(rm|mv|cp|tee|touch|mkdir|ln)\b", p) or re.search(r"(^|\s)>>?\s*\S", p):
            targets = [t for t in re.findall(r"(?:>>?\s*|\s)(/[^\s;|&]+|~[^\s;|&]*)", " " + p) if t != "/dev/null"]
            dest = p.split()[-1] if re.match(r"^\s*cp\b", p) else p   # cp reads its sources; only the target writes
            if re.search(r"(^|[\s/=])\.\.(/|\s|$)", dest):         # «../x» climbs out of the folder (21.11)
                return None, ""
            if any(not any(inside(t, s) for s in workspaces(cwd, h)) for t in targets):
                return None, ""                          # writing outside: the coordinator looks
    if re.search(r"\brm\s+-[a-z]*r[a-z]*f?\b.*(\s/|\s~|\$HOME|\.\.)", cmd):
        return "deny", "deleting outside the work folder"
    return "allow", ("an inline script that writes inside the work folder" if scripts
                     else "a safe command inside the work folder")


def decide(tool, ti, cwd, h):
    """('allow'|'deny'|'ask', reason, via). «ask» means no rule decides: nothing runs and nothing is pressed —
    the coordinator and the user decide (the user's decision of 2026-11-21)."""
    decision, why = rules(tool, ti, cwd, h)
    if decision is None:
        return "ask", "no rule: to the coordinator", "rule"
    return decision, why, "rule"


def hook():
    try:
        h = json.load(sys.stdin)
    except ValueError:
        return
    tool, ti, cwd = h.get("tool_name", ""), h.get("tool_input") or {}, h.get("cwd") or os.getcwd()
    decision, why, via = decide(tool, ti, cwd, h)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "session": h.get("session_id", ""),
                            "cwd": cwd, "tool": tool, "input": json.dumps(ti, ensure_ascii=False)[:300],
                            "decision": decision, "via": via, "why": why}, ensure_ascii=False) + "\n")
    reason = f"genius-ai gate ({via}): {why}"
    if decision == "deny":
        reason += ". Do not try to get around it; if the task needs this, stop and put it in your report as a question."
    elif decision == "ask":
        reason += ". No rule covers it: it waits for the coordinator, so put it in your report and work around it."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def selftest():
    """The read-only DB rule, the stack-stop rule and «no rule → the coordinator». No docker, no model, no user."""
    fails = []

    def check(name, ok, why=""):
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — {why}"))
        if not ok:
            fails.append(name)

    global local_containers, remote_db_host
    real, real_far = local_containers, remote_db_host
    local_containers = lambda: [("demo-db-1", "3f2a9c1b7d44"), ("demo-redis", "aa11bb22cc33")]
    remote_db_host = lambda c: ""                          # every fake container keeps its database here
    live = ("docker exec -i demo-db-1 psql -X -d demo_clean -U demo -v ON_ERROR_STOP=1 <<'SQL'\n"
            "begin read only;\nselect id, name from orders limit 5;\nrollback;\nSQL")
    try:
        d, why = rules("Bash", {"command": live}, "/tmp", {})
        check("the live command is allowed by a rule", d == "allow" and "local database" in why, f"{d}: {why}")
        bad = [
            ("the same with an UPDATE", live.replace("select id, name from orders limit 5", "update orders set x = 1")),
            ("an INSERT inside a read-only block", "docker exec -i demo-db-1 psql -c 'begin read only; insert into t values (1); rollback'"),
            ("a DELETE", "docker exec -i demo-db-1 psql -c 'delete from orders'"),
            ("a write inside a CTE", "docker exec -i demo-db-1 psql -c 'with d as (delete from t returning *) select * from d'"),
            ("a sequence that changes", "docker exec -i demo-db-1 psql -c \"select nextval('orders_id_seq')\""),
            ("«set transaction read write»", "docker exec -i demo-db-1 psql -c 'set transaction read write; select 1'"),
            ("«begin» without «read only»", "docker exec -i demo-db-1 psql -c 'begin; select 1; rollback'"),
            ("\\copy from a file", "docker exec -i demo-db-1 psql <<'SQL'\n\\copy orders from '/tmp/o.csv' csv\nSQL"),
            ("a dump of the data, not the schema", "docker exec -i demo-db-1 pg_dump -d demo_clean"),
            ("a psql to another host", "docker exec -i demo-db-1 psql -h db.prod.example.com -c 'select 1'"),
            ("a connection string to another machine",
             "docker exec -i demo-db-1 psql postgres://u:p@db.prod.example.com/x -c 'select 1'"),
            ("a container this Mac does not run", "docker exec -i other-db psql -c 'select 1'"),
            ("a pipeline into a shell", "docker exec -i demo-db-1 psql -c 'select 1' | sh"),
            ("a redirect that writes a file", "docker exec -i demo-db-1 psql -c 'select 1' > /tmp/out.csv"),
            ("a redis write", "docker exec -i demo-redis redis-cli set k v"),
            ("a mysql write", "docker exec -i demo-db-1 mysql -e 'update t set x = 1'"),
        ]
        for name, cmd in bad:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"not by rule: {name}", "local database" not in (why or ""), f"{d}: {why}")
        good = [
            ("psql -c select", "docker exec -i demo-db-1 psql -c 'select 1'"),
            ("a read-only block in one -c",
             "docker exec -i demo-db-1 psql -c 'begin read only; select 1; rollback'"),
            ("a schema dump", "docker exec -i demo-db-1 pg_dump --schema-only -d demo_clean"),
            ("mysql select", "docker exec -i demo-db-1 mysql -e 'select count(*) from orders'"),
            ("redis keys", "docker exec -i demo-redis redis-cli keys '*'"),
            ("redis config get", "docker exec -i demo-redis redis-cli config get maxmemory"),
            ("psql «cd … &&» in front", "cd /tmp && docker exec -i demo-db-1 psql -c 'select 1'"),
            ("psql by container id", "docker exec -i 3f2a9c1b7d44 psql -c 'select 1'"),
            ("«\\dt» is a read", "docker exec -i demo-db-1 psql <<'SQL'\n\\dt\nselect 1;\nSQL"),
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
            check(f"to the coordinator: an inline script with {name}", d is None, f"{d}: {why}")
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
              env_db_host(["PGHOST=db", "REDIS_URL=redis://redis:6379", "MYSQL_HOST=127.0.0.1:3306"], "demo-db-1",
                          ["demo-db-1"]) == "")
        check("a database named by the container's own name stays here",
              env_db_host(["DATABASE_URL=postgres://u:p@demo-db-1:5432/x"], "demo-db-1", ["demo-db-1"]) == "")
        check("a unix-socket DSN stays inside the container",
              env_db_host(["DATABASE_URL=postgresql:///demo?host=/var/run/postgresql"], "demo-db-1", []) == "")
        check("a domain in the environment is not local",
              env_db_host(["PGHOST=db", "DATABASE_URL=postgres://u:p@db.prod.example.com:5432/x"], "demo-db-1",
                          ["demo-db-1"]) == "db.prod.example.com")
        check("an address outside the loopback is not local",
              env_db_host(["MYSQL_HOST=10.0.0.5:3306"], "demo-db-1", []) == "10.0.0.5:3306")
        remote_db_host = lambda c: "db.prod.example.com"
        try:
            d, why = rules("Bash", {"command": "docker exec -i demo-db-1 psql -c 'select 1'"}, "/tmp", {})
            check("a container whose database is elsewhere is not allowed by rule",
                  "local database" not in (why or ""), f"{d}: {why}")
            remote_db_host = lambda c: None
            d, why = rules("Bash", {"command": "docker exec -i demo-db-1 psql -c 'select 1'"}, "/tmp", {})
            check("an unknown database host is not allowed by rule", "local database" not in (why or ""), f"{d}: {why}")
        finally:
            remote_db_host = lambda c: ""
        local_containers = real
        os.environ["DOCKER_HOST"] = "ssh://prod-host"
        try:
            check("a remote DOCKER_HOST has no local containers", local_containers() is None)
            d, why = rules("Bash", {"command": "docker exec -i demo-db-1 psql -c 'select 1'"}, "/tmp", {})
            check("…so that command goes to the coordinator", d is None, f"{d}: {why}")
            d, why = rules("Bash", {"command": "docker stop demo-db-1"}, "/tmp", {})
            check("…and so does a docker stop, when docker points elsewhere", d is None, f"{d}: {why}")
        finally:
            os.environ.pop("DOCKER_HOST", None)
            local_containers = lambda: [("demo-db-1", "3f2a9c1b7d44"), ("demo-redis", "aa11bb22cc33")]
        # stopping a stack the agent itself left running (the lesson of 2026-11-21: demo-db-1 stayed up)
        for name, cmd in [
            ("docker stop <name>", "docker stop demo-db-1"),
            ("docker stop by container id", "docker stop 3f2a9c1b7d44"),
            ("docker stop of two containers", "docker stop demo-db-1 demo-redis"),
            ("docker compose stop", "docker compose stop"),
            ("docker compose stop of one service", "docker compose stop db"),
            ("docker compose -f … stop", "docker compose -f docker-compose.yml stop db"),
            ("docker stop after «cd …»", "cd /tmp && docker stop demo-db-1"),
        ]:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"by rule: {name}", d == "allow" and "stack of this Mac" in (why or ""), f"{d}: {why}")
        for name, cmd in [
            ("docker stop of a container this Mac does not run", "docker stop other-db"),
            ("docker stop of a name from a substitution", "docker stop $(docker ps -q)"),
            ("docker stop with an option", "docker stop -t 0 demo-db-1"),
            ("docker compose down", "docker compose down"),
            ("docker compose down -v", "docker compose down -v"),
            ("docker compose rm", "docker compose rm -f db"),
            ("docker rm", "docker rm -f demo-db-1"),
            ("docker stop inside a chain", "docker stop demo-db-1 && docker compose up -d"),
            ("docker stop with a redirect", "docker stop demo-db-1 > /tmp/out.txt"),
        ]:
            d, why = rules("Bash", {"command": cmd}, "/tmp", {})
            check(f"not by rule: {name}", "stack of this Mac" not in (why or ""), f"{d}: {why}")
        # no rule decides → «ask»: nothing runs, nothing is pressed, the coordinator and the user decide
        for name, cmd in [
            ("a command no rule covers", "brew install jq"),
            ("a script we cannot vouch for", "python3 -c \"import urllib.request\""),
        ]:
            got = decide("Bash", {"command": cmd}, "/tmp", {})
            check(f"«ask»: {name}", got == ("ask", "no rule: to the coordinator", "rule"), str(got))
        check("the gate keeps no judge and no cache",
              not any(hasattr(sys.modules[__name__], n) for n in ("judge", "judge_prompt", "JUDGE_RULES", "JUDGE_MODEL",
                                                                   "cache_key", "rules_fingerprint", "CACHE")))
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
        print(*decide(a[1], json.loads(a[2]), cwd, {}))
    else:
        sys.exit("usage: bin/genius gate log [-n N] | gate test <tool> '<input json>' [cwd]   (hook: gate.py)")
