"""stt-server — Whisper large-v3-turbo kept loaded between dictations, on the Apple GPU (MLX).

Started on demand by stt_client.py (the first F5 press loads it while you speak).
Exits, and frees its memory, after DICTATE_IDLE seconds without a request (default 900 = 15 min).
Everything comes from the project: voice/.venv and voice/models/whisper, offline.

Protocol on voice/run/stt.sock, one JSON line each way:
  in:  {"wav": "/path/in.wav", "lang": "auto" | "uk" | "en" | ...}
  out: {"text": "...", "lang": "uk"}  or  {"error": "..."}
An empty line is a liveness probe (and a "warm" request): it only resets the idle timer.
DICTATE_LANGS (default uk,en): languages auto-detection may pick; any other detection is redone
in the nearest of them (Ukrainian heard as Russian is redone as Ukrainian).
DICTATE_PROMPT: vocabulary hint (default keeps git/dev terms in Latin script inside Ukrainian).
"""
import json
import os
import socket
import sys
import time
import wave

import numpy as np
import mlx_whisper

LIB = os.path.dirname(os.path.realpath(__file__))
VOICE = os.path.dirname(LIB)
RUN = os.path.join(VOICE, "run")                      # runtime state: socket, logs
SOCK = os.path.join(RUN, "stt.sock")
MODEL = os.path.join(VOICE, "models", "whisper")      # filled by voice/bin/install --dictation
IDLE = int(os.environ.get("DICTATE_IDLE", "900"))
ALLOWED = os.environ.get("DICTATE_LANGS", "uk,en").split(",")
PROMPT = os.environ.get(
    "DICTATE_PROMPT",
    "Зроби rebase на develop, запуш commit і відкрий pull request. Claude Code, CLI, git, merge, deploy.")
CYRILLIC = {"ru", "be", "bg", "sr", "mk", "kk"}


def nearest(code):
    if code in CYRILLIC and "uk" in ALLOWED:
        return "uk"
    return "en" if "en" in ALLOWED else ALLOWED[0]


def transcribe(path, lang):
    w = wave.open(path)
    audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    language = None if lang == "auto" else lang
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL, language=language, initial_prompt=PROMPT)
    if language is None and r["language"] not in ALLOWED:
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL, language=nearest(r["language"]),
                                   initial_prompt=PROMPT)
    return {"text": r["text"].strip(), "lang": r["language"]}


def main():
    os.makedirs(RUN, exist_ok=True)
    t = time.time()
    # load the weights now: mlx_whisper keeps the model it loaded last, so every next call is fast
    mlx_whisper.transcribe(np.zeros(16000, np.float32), path_or_hf_repo=MODEL)
    print(f"stt-server: model loaded in {time.time() - t:.1f} s, idle limit {IDLE} s", flush=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(4)
    srv.settimeout(5)
    last = time.time()
    while time.time() - last < IDLE:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        last = time.time()
        with conn:
            conn.settimeout(None)
            line = conn.makefile(encoding="utf-8").readline().strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                t = time.time()
                out = transcribe(req["wav"], req.get("lang", "auto"))
                print(f"stt-server: [{out['lang']}] {time.time() - t:.1f} s", flush=True)
            except Exception as e:                    # noqa: BLE001 - report every failure to the client
                out = {"error": f"{type(e).__name__}: {e}"}
            conn.sendall((json.dumps(out, ensure_ascii=False) + "\n").encode())
        last = time.time()
    srv.close()
    os.unlink(SOCK)
    print("stt-server: idle, unloaded", flush=True)


if __name__ == "__main__":
    sys.exit(main())
