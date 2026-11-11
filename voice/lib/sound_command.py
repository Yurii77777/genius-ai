"""sound-command — UserPromptSubmit hook: turn the voice on or off by a short message.

Typed or dictated (F5 pastes the text into the same prompt), the whole message must be one command:
  sound on  | звук on  | увімкни звук | включи голос | …   → `voice/bin/speak on`
  sound off | звук off | вимкни звук  | виключи голос | … → `voice/bin/speak off`
The message is then blocked: it never reaches Claude, and the answer shows at once.
A longer message that only mentions sound passes through unchanged.
"""
import json
import os
import re
import subprocess
import sys

VOICE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SPEAK = os.path.join(VOICE, "bin", "speak")

NOUN = r"(sound|voice|звук|голос|озвучк[уа]|озвучення|звуковий супровід|голосовий супровід)"
ON = r"(on|увімкн[иі]|включ[иі]|ввімкн[иі]|вмикай|включай|увімкнути|включити|ввімкнути)"
OFF = r"(off|вимкн[иі]|виключ[иі]|вимикай|виключай|вимкнути|виключити)"
PATTERNS = [
    (re.compile(rf"^{NOUN} {ON}$"), "on"), (re.compile(rf"^{ON} {NOUN}$"), "on"),
    (re.compile(rf"^{NOUN} {OFF}$"), "off"), (re.compile(rf"^{OFF} {NOUN}$"), "off"),
]
ANSWER = {"on": "🔊 Звук увімкнено / sound on", "off": "🔇 Звук вимкнено / sound off"}


def command(prompt):
    text = re.sub(r"[^\w\s]", " ", prompt.lower())           # Whisper adds «.», «!», capitals
    text = re.sub(r"\b(будь ласка|please|будь-ласка)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text.split()) > 4:
        return None
    for rx, state in PATTERNS:
        if rx.match(text):
            return state
    return None


def main():
    try:
        prompt = json.load(sys.stdin).get("prompt", "")
    except (ValueError, OSError):
        return 0
    state = command(prompt)
    if not state:
        return 0
    subprocess.run([SPEAK, state], capture_output=True, timeout=5)
    print(json.dumps({"decision": "block", "reason": ANSWER[state]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
