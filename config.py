"""Loads config.yaml into a plain dict, filling missing keys with defaults."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

DEFAULTS: dict = {
    "brain": {
        "provider": "openai",
        "openai_model": "gpt-6-luna",
        "gemini_model": "gemini-3.5-flash-lite",
        "temperature": 0.4,
        "max_tool_rounds": 5,
        "request_timeout_s": 20,
        "memory_turns": 10,
        "memory_reset_idle_s": 300,
    },
    "assistant": {"user_name": "", "music_app": "spotify", "address": "sir", "wake_reply": "Yes, sir?"},
    "conversation": {"follow_up_window_s": 5, "question_wait_s": 7, "barge_in": True, "wake_sound": True},
    "wake_word": {"model": "hey_jarvis", "threshold": 0.35, "cooldown_s": 2.0},
    "audio": {
        "input_device": None,
        "sample_rate": 16000,
        "input_gain": 2.0,
        "silence_threshold": 0.006,
        "silence_duration_s": 1.0,
        "max_record_s": 10,
        "no_speech_timeout_s": 4.0,
    },
    "stt": {"model": "base.en", "device": "cpu", "compute_type": "int8", "beam_size": 1},  # cuda via config.yaml
    "tts": {"engine": "kokoro", "kokoro_voice": "am_michael", "kokoro_speed": 1.0, "kokoro_device": "cpu",
            "kokoro_model_gpu": "models/kokoro-v1.0.onnx",
            "kokoro_model": "models/kokoro-v1.0.int8.onnx", "kokoro_voices": "models/voices-v1.0.bin",
            "voice": "", "rate": 185, "volume": 1.0},
    "paths": {"extra_allowed_folders": []},
    "ui": {
        "position": "center",
        "size": 420,
        "margin": 40,
        "fps": 60,
        "fade_in_ms": 250,
        "fade_out_delay_ms": 1500,
        "font": "Orbitron",
        "caption_font": "Rajdhani",
        "backdrop": 0.35,
        "palette": {
            "primary": "#00E5FF",
            "secondary": "#0077FF",
            "highlight": "#FFFFFF",
            "accent": "#FF5A36",
        },
    },
    "logging": {"file": "jarvis_log.txt"},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path = CONFIG_PATH) -> dict:
    data = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    return _merge(DEFAULTS, data)
