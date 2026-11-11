"""kokoro-worker — Kokoro-82M voices for tts-server (English female/male and other Kokoro languages).

Started by tts-server on the first Kokoro text; stays up as long as the server.
Protocol on stdin/stdout, one line each:
  in:  <voice>\t<speed>\t<text>   (newlines in text escaped as \\n)
  out: wav path, or an empty line when nothing could be synthesised
Model and voices: voice/models/kokoro-en (offline). The voice prefix selects the Kokoro language: a=US English, b=UK English, e=es, f=fr, h=hi,
i=it, j=ja, p=pt-br, z=zh. One pipeline per language, made on first use.
"""
import os
import sys
import tempfile

# The protocol gets its own copy of stdout; everything libraries print goes to stderr,
# so a download message can never be read as a wav path.
proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)

import numpy as np
import soundfile as sf
from kokoro import KModel, KPipeline

SHARE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "run")
KOKORO = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "models", "kokoro-en")
REPO = "hexgrad/Kokoro-82M"
kmodel = KModel(repo_id=REPO, config=os.path.join(KOKORO, "config.json"),
                model=os.path.join(KOKORO, "kokoro-v1_0.pth")).eval()
pipes = {}


def pipe_for(voice):
    code = voice[:1] or "a"
    if code not in pipes:
        pipes[code] = KPipeline(lang_code=code, repo_id=REPO, model=kmodel)
    return pipes[code]


def voice_file(voice):
    return os.path.join(KOKORO, "voices", f"{voice}.pt")


pipe_for("a")                      # warm up US English so the first reply is quick
print("ready", file=proto, flush=True)

for line in sys.stdin:
    parts = line.rstrip("\n").split("\t", 2)
    if len(parts) == 3:
        voice, speed, text = parts[0] or "af_heart", float(parts[1] or 1.0), parts[2]
    else:                          # old protocol: text only
        voice, speed, text = os.environ.get("SPEAK_VOICE_EN", "af_heart"), 1.0, parts[-1]
    text = text.replace("\\n", "\n").strip()
    try:
        chunks = [a.numpy() for _, _, a in pipe_for(voice)(text, voice=voice_file(voice), speed=speed)] if text else []
    except Exception as e:
        print(f"kokoro: {e}", file=sys.stderr, flush=True)
        chunks = []
    if not chunks:
        print("", file=proto, flush=True)
        continue
    fd, path = tempfile.mkstemp(suffix=".wav", dir=SHARE)
    os.close(fd)
    sf.write(path, np.concatenate(chunks), 24000)
    print(path, file=proto, flush=True)
