"""Phase 1 setup check: verifies every dependency works on this machine.

Usage:  python check_setup.py [--no-mic] [--no-speak]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

OK, FAIL, WARN = "[ OK ]", "[FAIL]", "[WARN]"
failures = 0


def step(name, fn):
    global failures
    t0 = time.perf_counter()
    try:
        msg = fn()
        print(f"{OK} {name}" + (f" — {msg}" if msg else "") + f"  ({time.perf_counter() - t0:.1f}s)")
    except Exception as e:  # noqa: BLE001
        failures += 1
        print(f"{FAIL} {name}: {e}")
        if os.environ.get("JARVIS_DEBUG"):
            traceback.print_exc()


def check_python():
    v = sys.version_info
    if (v.major, v.minor) != (3, 11):
        raise RuntimeError(f"Python {v.major}.{v.minor} found; Jarvis targets 3.11")
    return sys.version.split()[0]


def check_config():
    from config import load_config
    cfg = load_config()
    return f"provider={cfg['brain']['provider']}, stt={cfg['stt']['model']}/{cfg['stt']['device']}"


def check_imports():
    import importlib
    mods = ["yaml", "numpy", "sounddevice", "openwakeword", "onnxruntime", "faster_whisper",
            "pyttsx3", "openai", "google.genai", "send2trash", "pyautogui", "pygetwindow", "PySide6"]
    for m in mods:
        importlib.import_module(m)
    return f"{len(mods)} packages"


def check_api_keys():
    found = [k for k in ("OPENAI_API_KEY", "GEMINI_API_KEY") if os.environ.get(k)]
    missing = [k for k in ("OPENAI_API_KEY", "GEMINI_API_KEY") if k not in found]
    if not found:
        raise RuntimeError("neither OPENAI_API_KEY nor GEMINI_API_KEY is set (needed from Phase 3)")
    return "set: " + ", ".join(found) + (f"; not set: {', '.join(missing)}" if missing else "")


def check_mic(seconds=2.0):
    import numpy as np
    import sounddevice as sd
    from config import load_config
    cfg = load_config()["audio"]
    dev = sd.query_devices(cfg["input_device"], "input")
    print(f"       mic: {dev['name']}  — say something for {seconds:.0f}s...")
    rec = sd.rec(int(seconds * cfg["sample_rate"]), samplerate=cfg["sample_rate"], channels=1,
                 dtype="int16", device=cfg["input_device"])
    sd.wait()
    x = rec[:, 0].astype(np.float32) / 32768.0
    rms, peak = float(np.sqrt(np.mean(x ** 2))), float(np.max(np.abs(x)))
    if peak < 0.002:
        raise RuntimeError("mic recorded pure silence — check Windows mic privacy settings / device")
    return f"rms={rms:.4f} peak={peak:.3f} (silence_threshold in config: {cfg['silence_threshold']})"


def check_wakeword():
    import numpy as np
    import openwakeword
    from openwakeword.model import Model
    from config import load_config
    name = load_config()["wake_word"]["model"]
    openwakeword.utils.download_models(model_names=[name])
    model = Model(wakeword_models=[name], inference_framework="onnx")
    scores = model.predict(np.zeros(1280, dtype=np.int16))
    return f"loaded {list(scores)} (onnx)"


def check_whisper():
    import numpy as np
    from faster_whisper import WhisperModel
    from config import load_config
    s = load_config()["stt"]
    model = WhisperModel(s["model"], device=s["device"], compute_type=s["compute_type"], cpu_threads=4)
    segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, language="en")
    list(segs)
    return f"{s['model']} on {s['device']}/{s['compute_type']}"


def check_cuda():
    import ctranslate2
    n = ctranslate2.get_cuda_device_count()
    if n == 0:
        raise RuntimeError("no CUDA device visible to CTranslate2 (CPU mode still works)")
    return f"{n} CUDA device(s) — set stt.device: cuda to use it (needs cuBLAS/cuDNN 9 DLLs)"


def check_tts(speak=True):
    import pyttsx3
    engine = pyttsx3.init()
    voices = [v.name for v in engine.getProperty("voices")]
    print("       voices: " + "; ".join(voices))
    if speak:
        engine.say("Jarvis setup check complete.")
        engine.runAndWait()
    return f"{len(voices)} voice(s)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mic", action="store_true")
    ap.add_argument("--no-speak", action="store_true")
    a = ap.parse_args()

    step("Python version", check_python)
    step("config.yaml", check_config)
    step("Package imports", check_imports)
    step("Wake word model", check_wakeword)
    step("Whisper STT model", check_whisper)
    if not a.no_mic:
        step("Microphone", check_mic)
    step("Text-to-speech", lambda: check_tts(not a.no_speak))
    # Optional checks — warn instead of fail
    for name, fn in (("API keys", check_api_keys), ("CUDA (optional)", check_cuda)):
        try:
            print(f"{OK} {name} — {fn()}")
        except Exception as e:  # noqa: BLE001
            print(f"{WARN} {name}: {e}")

    print("\nAll checks passed." if failures == 0 else f"\n{failures} check(s) failed.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
