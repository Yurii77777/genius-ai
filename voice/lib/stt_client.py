"""stt-client — hand a recording to stt-server; start the server first when it is not loaded.

    stt_client.py --warm            start the server (if needed) and return at once
    stt_client.py WAV [LANG]        print the text on stdout, "LANG <code>" on stderr; exit 1 on failure
    stt_client.py --status          print "loaded" or "not loaded"
Runs on the system Python (stdlib only); the server runs in voice/.venv.
"""
import json
import os
import socket
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.realpath(__file__))
VOICE = os.path.dirname(LIB)
RUN = os.path.join(VOICE, "run")
SOCK = os.path.join(RUN, "stt.sock")
PY = os.path.join(VOICE, ".venv", "bin", "python")
LOAD_WAIT = 180                                     # s: a cold load of the model takes ~5-20 s


def connect(timeout=2):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    return s


def up():
    try:
        with connect(1) as s:
            s.sendall(b"\n")                        # liveness probe; also resets the idle timer
        return True
    except OSError:
        return False


def start():
    if up():
        return
    running = subprocess.run(["pgrep", "-f", os.path.join(LIB, "stt_server.py")],
                             capture_output=True).returncode == 0
    if running:
        return                                      # loading right now
    os.makedirs(RUN, exist_ok=True)
    env = dict(os.environ, HF_HUB_OFFLINE="1", HF_HOME=os.path.join(VOICE, "models", ".hf"))
    with open(os.path.join(RUN, "stt.log"), "a") as log:
        subprocess.Popen([PY, os.path.join(LIB, "stt_server.py")], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, env=env, start_new_session=True)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--status"]:
        print("loaded" if up() else "not loaded")
        return 0
    start()
    if args[:1] == ["--warm"]:
        return 0
    wav, lang = args[0], (args[1] if len(args) > 1 else "auto")
    deadline = time.time() + LOAD_WAIT
    while not up():
        if time.time() > deadline:
            print(f"stt-server did not start, see {os.path.join(RUN, 'stt.log')}", file=sys.stderr)
            return 1
        time.sleep(0.3)
    with connect(timeout=600) as s:
        s.sendall((json.dumps({"wav": wav, "lang": lang}) + "\n").encode())
        reply = json.loads(s.makefile(encoding="utf-8").readline())
    if "error" in reply:
        print(reply["error"], file=sys.stderr)
        return 1
    print(reply["text"])
    print(f"LANG {reply['lang']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
