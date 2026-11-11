"""tts-server — one voice for all Claude Code tabs, spoken from ONE queue.

Voices come from genius-ai/voice/voice.json (read before every item, so a change of gender or
voice applies to the next reply without a restart):
  styletts2  Ukrainian: voice/models/styletts2-uk (model + speaker vectors), ipa(stressify(_prep(text))),
             one pass per line.
  kokoro     English (and other Kokoro languages): voice/models/kokoro-en via kokoro_worker.py.
Everything runs from the project: voice/.venv and voice/models/, offline (voice/bin/install fills them).
  macOS `say` with the language's `say` voice when an engine fails.

Several tabs never talk over each other:
  SAY <session>\t<text>   queue it; a newer reply of the SAME session replaces that session's
                          queued and playing speech, another session's reply waits its turn
  STOP <session>          drop that session's speech (the user wrote in that tab)
  STOP *                  drop everything (dictation started: the mic must not hear us)
  STATUS <session>\t<text> a short progress line, spoken only when nothing else is speaking or waiting
One request per connection, one line. Started on demand by `speak`; exits after SPEAK_IDLE s idle.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

import soundfile as sf
import torch
from ipa_uk import ipa
from styletts2_inference.models import StyleTTS2
from ukrainian_word_stress import Stressifier, StressSymbol

LIB = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, LIB)
import voice_config  # noqa: E402

SHARE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "run")      # runtime state: socket, temp wavs, logs
SOCK = os.path.join(SHARE, "tts.sock")
IDLE = int(os.environ.get("SPEAK_IDLE", "900"))
LINE = int(os.environ.get("SPEAK_LINE", "260"))
VOICE = os.path.dirname(LIB)
MODELS = os.path.join(VOICE, "models")              # filled by voice/bin/install; never the network
STYLETTS = os.path.join(MODELS, "styletts2-uk")
KOKORO_PY = sys.executable                          # one environment (voice/.venv) for every engine

model = StyleTTS2(config_path=os.path.join(STYLETTS, "config.yml"),
                  weights_path=os.path.join(STYLETTS, "pytorch_model.bin"), device=torch.device("cpu"))
stressify = Stressifier()
speakers = {}           # StyleTTS2 voice name -> style vector


def speaker(name):
    if name not in speakers:
        speakers[name] = torch.load(os.path.join(STYLETTS, "voices", f"{name}.pt"))
    return speakers[name]


cv = threading.Condition()
queue = []              # [(session, text)] waiting, oldest first
current = None          # session being spoken now
cancel = set()          # sessions whose current speech must stop ("*" = all)
player = None           # the running afplay/say process
last_request = time.time()


def _prep(text):
    text = text.strip().replace("+", StressSymbol.CombiningAcuteAccent)
    return unicodedata.normalize("NFKC", text)


def _sentences(text):
    parts = re.split(r"(?<=[.!?])(?<!\b[А-ЯІЇЄҐA-Z]\.)\s+|(?<=[.!?][»”])\s+", text.strip())
    return [p for p in (x.strip() for x in parts) if p]


def lines(text):
    """Sentences grouped into lines, but a sentence that opens a new project («Сайт: …») starts a new line."""
    out = []
    for s in _sentences(text):
        if out and not TOPIC.match(s) and len(out[-1]) + 1 + len(s) <= LINE:
            out[-1] += " " + s
        else:
            out.append(s)
    return out


TOPIC = re.compile(r"^[A-ZА-ЯІЇЄҐ][\w'’-]{1,24}:")       # «Сайт: …» — a sentence that opens a new project


def pause_before(nxt):
    """The silence before the next sentence (voice/voice.json → pace)."""
    pace = voice_config.load().get("pace") or {}
    if nxt is None:
        return 0.0
    return float(pace.get("topic_pause", 0.9) if TOPIC.match(nxt) else pace.get("sentence_pause", 0.45))


def rest(session, seconds, nxt=None):
    """Wait between lines; before a new project play the soft topic sound. A stop cuts it short."""
    sound = (voice_config.load().get("pace") or {}).get("topic_sound")
    if nxt is not None and TOPIC.match(nxt) and sound and os.path.exists(sound) and not cancelled(session):
        run_player(["afplay", "-v", "0.5", sound], session)
    end = time.time() + seconds
    while time.time() < end:
        if cancelled(session):
            return
        time.sleep(0.05)


def synth(text, style, speed):
    tokens = torch.LongTensor(model.tokenizer.encode(ipa(stressify(_prep(text)))))
    with torch.no_grad():
        return model(tokens, speed=speed, s_prev=style).cpu().numpy()


def muted():
    """The system mute key: a muted Mac speaks nothing, and nothing piles up for later."""
    r = subprocess.run(["osascript", "-e", "output muted of (get volume settings)"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "true"


def cancelled(session):
    with cv:
        if session in cancel or "*" in cancel:
            return True
    return muted()


def run_player(args, session):
    global player
    with cv:
        if session in cancel or "*" in cancel:
            return
        player = subprocess.Popen(args)
    player.wait()
    with cv:
        player = None


kokoro = None             # the Kokoro worker, started on the first Kokoro text
kokoro_lock = threading.Lock()


def kokoro_wav(text, voice, speed):
    """Synthesise with Kokoro in its own venv; None if it is unavailable."""
    global kokoro
    with kokoro_lock:
        if kokoro is None or kokoro.poll() is not None:
            kokoro = subprocess.Popen([KOKORO_PY, os.path.join(LIB, "kokoro_worker.py")],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            if kokoro.stdout.readline().strip() != "ready":
                return None
        kokoro.stdin.write(f"{voice}\t{speed}\t" + text.replace("\n", "\\n") + "\n")
        kokoro.stdin.flush()
        path = kokoro.stdout.readline().strip()
        return path or None


def say_fallback(text, say_voice, session):
    run_player(["say", "-v", say_voice, text] if say_voice else ["say", text], session)


def recording():
    """F5 dictation is recording now (voice/run/recording.pid holds a live pid): never speak into the mic."""
    try:
        os.kill(int(open(os.path.join(SHARE, "recording.pid")).read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def speak_item(session, text):
    waited = 0.0
    while recording() and waited < 600:          # hold the reply until the take ends (max 10 min)
        if cancelled(session):
            return
        time.sleep(0.2)
        waited += 0.2
    if muted():
        return
    cfg = voice_config.load()
    engine, voice, speed, say_voice = voice_config.pick(voice_config.detect(text), cfg)
    if engine == "kokoro":
        sentences = lines(text)
        for i, sentence in enumerate(sentences):
            if cancelled(session):
                return
            try:
                path = kokoro_wav(sentence, voice, speed)
            except Exception as e:
                print(f"kokoro unavailable: {e}", flush=True)
                path = None
            if path:
                run_player(["afplay", path], session)
                os.unlink(path)
            else:
                say_fallback(sentence, say_voice, session)
            rest(session, pause_before(sentences[i + 1] if i + 1 < len(sentences) else None), sentences[i + 1] if i + 1 < len(sentences) else None)
        return
    try:
        style = speaker(voice)
    except Exception as e:
        print(f"voice {voice!r} unavailable: {e}", flush=True)
        say_fallback(text, say_voice, session)
        return
    pending = lines(text)
    nxt = synth(pending[0], style, speed) if pending else None
    for i in range(len(pending)):
        audio = nxt
        if cancelled(session):
            return
        result = {}
        worker = None
        if i + 1 < len(pending):                   # synthesise the next line while this one plays
            worker = threading.Thread(target=lambda j=i + 1: result.update(a=synth(pending[j], style, speed)))
            worker.start()
        fd, path = tempfile.mkstemp(suffix=".wav", dir=SHARE)
        os.close(fd)
        sf.write(path, audio, 24000)
        run_player(["afplay", path], session)
        os.unlink(path)
        if worker:
            worker.join()
            nxt = result.get("a")
        rest(session, pause_before(pending[i + 1] if i + 1 < len(pending) else None), pending[i + 1] if i + 1 < len(pending) else None)


def worker_loop():
    global current
    while True:
        with cv:
            while not queue:
                cv.wait()
            session, text = queue.pop(0)
            current = session
            cancel.discard(session)
            cancel.discard("*")
        print(f"speak {session}: {text[:40]}", flush=True)
        try:
            speak_item(session, text)
        except Exception as e:                     # one bad reply must not kill the voice
            print(f"skip: {e}", flush=True)
        with cv:
            current = None


def stop(session):
    """Drop a session's speech (queued and playing); '*' drops everything."""
    with cv:
        queue[:] = [q for q in queue if session != "*" and q[0] != session]
        if current is not None and (session == "*" or current == session):
            cancel.add(session)
            if player and player.poll() is None:
                player.terminate()


def handle(line):
    global last_request
    last_request = time.time()
    cmd, _, rest = line.partition(" ")
    if cmd == "STOP":
        stop(rest or "*")
    elif cmd == "STATUS":
        session, _, text = rest.partition("\t")
        text = text.strip()
        with cv:
            idle = current is None and not queue
            if text and idle:
                queue.append((session, text))
                cv.notify()
    elif cmd == "SAY":
        session, _, text = rest.partition("\t")
        text = text.replace("\\n", "\n").strip()
        if text:
            stop(session)                          # this tab's older reply goes
            with cv:
                queue.append((session, text))      # other tabs keep their turn
                cv.notify()


def main():
    os.makedirs(SHARE, exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    srv.settimeout(5)
    threading.Thread(target=worker_loop, daemon=True).start()
    print(f"tts-server ready, catalogue {voice_config.CATALOGUE}, user config {voice_config.USER_CONFIG}", flush=True)
    while time.time() - last_request < IDLE or current is not None or queue:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        with conn:
            line = conn.makefile(encoding="utf-8").readline().rstrip("\n")
        if line:                                   # an empty line is a liveness probe
            handle(line)
    os.unlink(SOCK)


if __name__ == "__main__":
    main()
