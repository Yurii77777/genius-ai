"""Render one WAV sample per Ukrainian StyleTTS2 voice, for choosing a voice by ear.

    python sample_voices.py OUT_DIR "Name One" "Name Two" ...
    python sample_voices.py OUT_DIR --preset        # the single-speaker model (male, Filatov dataset)

Runs in the voiceover venv (StyleTTS2, ukrainian-word-stress, ipa-uk). Voice vectors come from the
patriotyk/styletts2-ukrainian space (downloaded once into the Hugging Face cache).
"""
import os
import sys
import unicodedata

import soundfile as sf
import torch
from huggingface_hub import hf_hub_download
from ipa_uk import ipa
from styletts2_inference.models import StyleTTS2
from ukrainian_word_stress import Stressifier, StressSymbol

TEXT = os.environ.get("SAMPLE_TEXT", (
    "Привіт! Я ваш голосовий помічник. Я стежу за проєктами і сервісами, "
    "а коли щось піде не так, скажу про це першим."))
SPEED = float(os.environ.get("SPEAK_SPEED", "1.12"))

out_dir, names = sys.argv[1], sys.argv[2:]
os.makedirs(out_dir, exist_ok=True)
stressify = Stressifier()
text = unicodedata.normalize("NFKC", TEXT.strip().replace("+", StressSymbol.CombiningAcuteAccent))

if names == ["--preset"]:
    model = StyleTTS2(hf_path="patriotyk/styletts2_ukrainian_single", device=torch.device("cpu"))
    tokens = torch.LongTensor(model.tokenizer.encode(ipa(stressify(text))))
    style = model.predict_style_single(tokens)
    with torch.no_grad():
        audio = model(tokens, speed=SPEED, s_prev=style).cpu().numpy()
    sf.write(os.path.join(out_dir, "00 — Пресет (single, Філатов).wav"), audio, 24000)
    print("ok: preset", flush=True)
    sys.exit(0)

model = StyleTTS2(hf_path="patriotyk/styletts2_ukrainian_multispeaker", device=torch.device("cpu"))
tokens = torch.LongTensor(model.tokenizer.encode(ipa(stressify(text))))
for i, name in enumerate(names, 1):
    try:
        speaker = torch.load(hf_hub_download("patriotyk/styletts2-ukrainian", f"voices/{name}.pt", repo_type="space"))
        with torch.no_grad():
            audio = model(tokens, speed=SPEED, s_prev=speaker).cpu().numpy()
        sf.write(os.path.join(out_dir, f"{i:02d} — {name}.wav"), audio, 24000)
        print(f"ok: {name}", flush=True)
    except Exception as e:
        print(f"fail: {name}: {e}", flush=True)
