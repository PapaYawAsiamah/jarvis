"""Voice pipeline: mic stream, wake word, recording with silence detection, STT, TTS,
and the conversation loop that ties them to the brain.

The loop reports progress through a Hooks object so the UI (Phase 5) can follow along
without the voice code knowing anything about Qt.
"""
from __future__ import annotations

import collections
import math
import os
import queue
import re
import threading
import time

import numpy as np
import sounddevice as sd

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import safety  # noqa: E402
from brain import is_cancel  # noqa: E402
from gpu import enable_cuda_dlls  # noqa: E402
from safety import log  # noqa: E402

CHUNK = 1280  # 80 ms at 16 kHz — what openWakeWord expects
_WAKE_PREFIX = re.compile(r"^\W*((hey|hi|okay|ok|yo)\W+)?(jarvis|jervis|travis)\b\W*", re.I)
CONFIRM_SUFFIX = "Should I go ahead? Say yes to confirm."
_HALLUCINATIONS = {"you", "thank you", "thanks for watching", "bye", "thank you for watching", ""}


class Hooks:
    """Override these to follow the assistant (UI, console...). All are called from the voice thread."""

    def on_state(self, state: str, info: str = "") -> None: ...   # idle|listening|thinking|speaking|confirm|error
    def on_level(self, level: float) -> None: ...                 # 0..1 mic level (listening) or speech level (speaking)
    def on_transcript(self, text: str) -> None: ...
    def on_reply(self, text: str) -> None: ...


# ================================================================ microphone

class Mic:
    """One always-open 16 kHz input stream feeding a queue of 80 ms int16 chunks."""

    def __init__(self, cfg: dict):
        a = cfg["audio"]
        self.rate = int(a["sample_rate"])
        self.device = a["input_device"]
        self.gain = float(a.get("input_gain", 1.0))
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=600)
        self.recent: collections.deque[np.ndarray] = collections.deque(maxlen=25)  # ~2 s pre-roll
        self.floor_hist: collections.deque[float] = collections.deque(maxlen=120)
        self.stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        if self.gain != 1.0:
            chunk = np.clip(chunk.astype(np.float32) * self.gain, -32768, 32767).astype(np.int16)
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            pass

    def start(self) -> None:
        if self.stream is None:
            self.stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="int16",
                                         blocksize=CHUNK, device=self.device, callback=self._callback)
            self.stream.start()

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.flush()

    def flush(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            chunk = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        self.recent.append(chunk)
        return chunk

    @staticmethod
    def rms(chunk: np.ndarray) -> float:
        x = chunk.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(x * x)))

    def noise_floor(self) -> float:
        return float(np.percentile(self.floor_hist, 20)) if len(self.floor_hist) > 10 else 0.0


# ================================================================ TTS

class SapiEngine:
    """Windows SAPI voices (Zira/Hazel). Offline, instant, robotic: used as the fallback.
    (Driven directly: pyttsx3's save_to_file hangs on the second call.)"""

    rate = 22050
    _FORMAT_22K_16BIT_MONO = 22

    def __init__(self, t: dict):
        import win32com.client
        self._client = win32com.client
        self.voice = win32com.client.Dispatch("SAPI.SpVoice")
        want = (t.get("voice") or "").lower()
        if want:
            tokens = self.voice.GetVoices()
            for i in range(tokens.Count):
                if want in tokens.Item(i).GetDescription().lower():
                    self.voice.Voice = tokens.Item(i)
                    break
        # config rate is words per minute; SAPI uses -10..10 where +10 is ~3x and 0 is ~180 wpm
        self.voice.Rate = max(-10, min(10, round(10 * math.log(max(int(t["rate"]), 60) / 180, 3))))
        self.voice.Volume = max(0, min(100, int(float(t["volume"]) * 100)))

    def synth(self, text: str) -> np.ndarray:
        stream = self._client.Dispatch("SAPI.SpMemoryStream")
        fmt = self._client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = self._FORMAT_22K_16BIT_MONO
        stream.Format = fmt
        self.voice.AudioOutputStream = stream
        self.voice.Speak(text, 0)  # synchronous, into memory
        return np.frombuffer(bytes(stream.GetData()), dtype=np.int16).astype(np.float32) / 32768.0


class KokoroEngine:
    """Kokoro-82M neural voice via onnxruntime (offline, natural-sounding)."""

    rate = 24000
    _URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"

    def __init__(self, t: dict):
        use_gpu = t.get("kokoro_device", "cpu") == "cuda" and enable_cuda_dlls()
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        ort.set_default_logger_severity(3)  # errors only
        use_gpu = use_gpu and "CUDAExecutionProvider" in ort.get_available_providers()
        # int8 ops aren't GPU-accelerated and Pascal GPUs are slow at fp16, so the GPU gets full fp32
        voices = os.path.join(safety.ROOT, t["kokoro_voices"])
        sess = None
        if use_gpu:
            try:
                sess = self._session(ort, os.path.join(safety.ROOT, t["kokoro_model_gpu"]),
                                     ["CUDAExecutionProvider", "CPUExecutionProvider"])
            except Exception as e:  # noqa: BLE001
                log(f"Kokoro GPU unavailable, using CPU: {e!s:.200}")
        if sess is None:
            sess = self._session(ort, os.path.join(safety.ROOT, t["kokoro_model"]), ["CPUExecutionProvider"])
        self.device = "cuda" if sess.get_providers()[0] == "CUDAExecutionProvider" else "cpu"
        log(f"Kokoro voice on {self.device}")
        if not os.path.exists(voices):
            self._download(voices)
        self.kokoro = Kokoro.from_session(sess, voices)
        self.voice = t["kokoro_voice"]
        self.speed = float(t["kokoro_speed"])
        self.volume = float(t["volume"])

    def _session(self, ort, model: str, providers: list[str]):
        if not os.path.exists(model):
            self._download(model)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.log_severity_level = 3  # errors only
        return ort.InferenceSession(model, opts, providers=providers)

    def _download(self, path: str) -> None:
        import urllib.request
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"Downloading {os.path.basename(path)} (first run only)...", flush=True)
        urllib.request.urlretrieve(self._URL + os.path.basename(path), path + ".part")
        os.replace(path + ".part", path)

    def synth(self, text: str) -> np.ndarray:
        samples, _ = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        return np.asarray(samples, dtype=np.float32) * self.volume


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def split_for_speech(text: str, max_len: int = 55) -> list[str]:
    """Sentences (long ones split at commas) so the first bit can start playing quickly."""
    pieces: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        while len(sentence) > max_len and ", " in sentence[15:max_len]:
            cut = sentence.rindex(", ", 15, max_len) + 1
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    return pieces


class Speaker:
    """Speaks text sentence by sentence: the next sentence is rendered while the current
    one plays, so the neural voice still starts talking quickly. Short phrases are cached.
    Must be created on the voice thread (SAPI/COM is thread-bound)."""

    def __init__(self, cfg: dict):
        t = cfg["tts"]
        self.engine = None
        if t.get("engine", "kokoro") == "kokoro":
            try:
                self.engine = KokoroEngine(t)
            except Exception as e:  # noqa: BLE001
                log(f"Kokoro unavailable, using Windows voice: {e!r}")
        self.fallback = SapiEngine(t)
        self.engine = self.engine or self.fallback
        self.cache: dict[str, np.ndarray] = {}
        self.stop_flag = threading.Event()
        # On the GPU whole sentences render fast enough (better prosody); on the CPU split at commas.
        self.max_piece = 160 if getattr(self.engine, "device", "") == "cuda" else 55

    def _synth(self, text: str) -> np.ndarray:
        if text in self.cache:
            return self.cache[text]
        try:
            audio = self.engine.synth(text)
        except Exception as e:  # noqa: BLE001
            if self.engine is self.fallback:
                raise
            log(f"TTS error, using fallback voice: {e!r}")
            audio = self._resample(self.fallback.synth(text), self.fallback.rate)
        if len(text) <= 40 and len(self.cache) < 50:
            self.cache[text] = audio
        return audio

    def _resample(self, audio: np.ndarray, rate: int) -> np.ndarray:
        if rate == self.engine.rate:
            return audio
        from scipy.signal import resample_poly
        g = math.gcd(self.engine.rate, rate)
        return resample_poly(audio, self.engine.rate // g, rate // g).astype(np.float32)

    def warm(self, *phrases: str) -> None:
        for p in phrases:
            for piece in split_for_speech(p or "", self.max_piece):
                self._synth(piece)

    def speak(self, text: str, on_level=None, should_stop=None) -> bool:
        """Blocking. Returns False if interrupted (stop flag or should_stop())."""
        self.stop_flag.clear()
        pieces = split_for_speech(text, self.max_piece)
        ready: queue.Queue = queue.Queue()
        cancel = threading.Event()

        def produce():
            try:
                for piece in pieces:
                    if cancel.is_set():
                        break
                    ready.put(self._synth(piece))
            except Exception as e:  # noqa: BLE001
                log(f"TTS error: {e!r}")
            ready.put(None)

        if isinstance(self.engine, KokoroEngine):  # onnxruntime is fine on another thread
            threading.Thread(target=produce, daemon=True).start()
        else:  # SAPI must stay on this thread (and is fast anyway)
            produce()

        def interrupted() -> bool:
            return self.stop_flag.is_set() or bool(should_stop and should_stop())

        rate = self.engine.rate
        block = int(rate * 0.04)
        try:
            with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", blocksize=block) as out:
                while True:
                    try:
                        audio = ready.get(timeout=0.05)
                    except queue.Empty:  # next sentence still rendering
                        if interrupted():
                            return False
                        continue
                    if audio is None:
                        return True
                    for pos in range(0, len(audio), block):
                        if interrupted():
                            return False
                        piece = audio[pos:pos + block]
                        out.write(piece.reshape(-1, 1))
                        if on_level:
                            on_level(min(1.0, float(np.sqrt(np.mean(piece * piece))) * 6))
        finally:
            cancel.set()


def _chime(rate: int = 22050) -> None:
    """Short soft two-tone 'I'm listening' sound (non-blocking)."""
    t = np.linspace(0, 0.07, int(rate * 0.07), endpoint=False)
    tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)])
    env = np.minimum(1, np.minimum(np.arange(len(tone)), np.arange(len(tone))[::-1]) / 300)
    sd.play((tone * env * 0.15).astype(np.float32), rate)


# ================================================================ assistant

class VoiceAssistant:
    def __init__(self, cfg: dict, brain, hooks: Hooks | None = None):
        from faster_whisper import WhisperModel
        from openwakeword.model import Model
        import openwakeword

        self.cfg, self.brain, self.hooks = cfg, brain, hooks or Hooks()
        a, w, s, c = cfg["audio"], cfg["wake_word"], cfg["stt"], cfg["conversation"]
        self.min_threshold = float(a["silence_threshold"])
        self.silence_s = float(a["silence_duration_s"])
        self.max_record_s = float(a["max_record_s"])
        self.no_speech_s = float(a["no_speech_timeout_s"])
        self.wake_threshold = float(w["threshold"])
        self.wake_cooldown = float(w["cooldown_s"])
        self.follow_up_s = float(c["follow_up_window_s"])
        self.question_wait_s = float(c["question_wait_s"])
        self.barge_in = bool(c["barge_in"])
        self.chime = bool(c["wake_sound"])
        self.wake_reply = (cfg["assistant"].get("wake_reply") or "").strip()
        self.beam = int(s["beam_size"])

        openwakeword.utils.download_models(model_names=[w["model"]])
        self.wake = Model(wakeword_models=[w["model"]], inference_framework="onnx")
        self.stt = self._load_whisper(WhisperModel, s)
        self.mic = Mic(cfg)
        self.speaker: Speaker | None = None  # created on the voice thread (SAPI/COM is thread-bound)

        self.running = threading.Event()
        self.paused = threading.Event()
        self._last_wake = 0.0

    @staticmethod
    def _load_whisper(WhisperModel, s: dict):
        if s["device"] == "cuda" and enable_cuda_dlls():
            try:
                model = WhisperModel(s["model"], device="cuda", compute_type=s["compute_type"])
                model.transcribe(np.zeros(16000, dtype=np.float32), language="en")  # fail now, not mid-sentence
                log(f"Whisper on cuda ({s['compute_type']})")
                return model
            except Exception as e:  # noqa: BLE001
                log(f"Whisper GPU unavailable, using CPU: {e!r}")
        log("Whisper on cpu (int8)")
        return WhisperModel(s["model"], device="cpu", compute_type="int8", cpu_threads=4)

    # ---------------------------------------------------------------- control (any thread)

    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    def stop(self) -> None:
        self.running.clear()
        if self.speaker:
            self.speaker.stop_flag.set()

    # ---------------------------------------------------------------- building blocks

    def _threshold(self) -> float:
        return max(self.min_threshold, self.mic.noise_floor() * 3.0)

    def _wake_score(self, chunk: np.ndarray) -> float:
        return max(self.wake.predict(chunk).values())

    def wait_for_wake(self) -> bool:
        """Block until the wake word is heard. Returns False if stopped or paused."""
        self.wake.reset()
        while self.running.is_set() and not self.paused.is_set():
            chunk = self.mic.read()
            if chunk is None:
                continue
            self.mic.floor_hist.append(self.mic.rms(chunk))
            if self._wake_score(chunk) >= self.wake_threshold and time.monotonic() - self._last_wake > self.wake_cooldown:
                self._last_wake = time.monotonic()
                return True
        return False

    def record(self, no_speech_s: float, preroll: bool = False, silence_s: float | None = None) -> np.ndarray | None:
        """Record until ~silence_duration_s of silence after speech. None if nobody spoke."""
        thr = self._threshold()
        frames: list[np.ndarray] = list(self.mic.recent) if preroll else []
        speech = sum(self.mic.rms(f) > thr for f in frames[-4:]) >= 2
        silent_for, elapsed, recent_loud = 0.0, 0.0, collections.deque(maxlen=3)
        step = CHUNK / self.mic.rate
        while self.running.is_set():
            chunk = self.mic.read()
            if chunk is None:
                continue
            frames.append(chunk)
            elapsed += step
            level = self.mic.rms(chunk)
            self.hooks.on_level(min(1.0, level / (thr * 8)))
            recent_loud.append(level > thr)
            if not speech:
                if sum(recent_loud) >= 2:
                    speech = True
                elif elapsed > no_speech_s:
                    return None
            else:
                silent_for = 0.0 if level > thr else silent_for + step
                if silent_for >= (silence_s or self.silence_s):
                    break
            if elapsed >= self.max_record_s:
                break
        if not speech:
            return None
        return np.concatenate(frames).astype(np.float32) / 32768.0

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.stt.transcribe(
            audio, language="en", beam_size=self.beam, condition_on_previous_text=False,
            initial_prompt="Jarvis, open Spotify, Chrome, desktop, documents, downloads, folder.")
        text = " ".join(s.text for s in segments).strip()
        if re.sub(r"[^a-z ]", "", text.lower()).strip() in _HALLUCINATIONS:
            return ""
        return text

    @staticmethod
    def strip_wake(text: str) -> str:
        return _WAKE_PREFIX.sub("", text).strip()

    def say(self, text: str, state: str = "speaking") -> bool:
        """Speak text. Returns False if interrupted by the wake word (barge-in)."""
        self.hooks.on_state(state, text)
        self.hooks.on_reply(text)
        self.mic.flush()
        self.wake.reset()
        interrupted = {"hit": False}

        def should_stop():
            if not self.running.is_set():
                return True
            if self.barge_in:
                while not self.mic.q.empty():
                    chunk = self.mic.read(timeout=0)
                    if chunk is not None and self._wake_score(chunk) >= self.wake_threshold:
                        interrupted["hit"] = True
                        return True
            return False

        try:
            finished = self.speaker.speak(text, on_level=self.hooks.on_level, should_stop=should_stop)
        except Exception as e:  # noqa: BLE001
            log(f"TTS error: {e!r}")
            finished = True
        if not interrupted["hit"]:
            time.sleep(0.15)
            self.mic.flush()  # drop our own voice echo
        self.hooks.on_level(0.0)
        return finished or not interrupted["hit"]

    def listen_text(self, no_speech_s: float, preroll: bool = False, silence_s: float | None = None) -> str | None:
        """Record + transcribe. None = nobody spoke."""
        self.hooks.on_state("listening")
        audio = self.record(no_speech_s, preroll=preroll, silence_s=silence_s)
        if audio is None:
            return None
        self.hooks.on_state("thinking")
        text = self.strip_wake(self.transcribe(audio))
        self.hooks.on_transcript(text)
        return text

    # ---------------------------------------------------------------- confirmation (runs inside brain tool calls)

    def _confirm(self, description: str) -> str | None:
        self.say(f"{description} {CONFIRM_SUFFIX}", state="confirm")
        self.hooks.on_state("confirm", description)
        audio = self.record(self.question_wait_s)
        if audio is None:
            return None
        answer = self.strip_wake(self.transcribe(audio))
        self.hooks.on_transcript(answer)
        self.hooks.on_state("thinking")
        return answer

    # ---------------------------------------------------------------- main loop

    def run(self) -> None:
        """Blocking loop; call from a worker thread (or the main thread in console mode)."""
        import pythoncom
        pythoncom.CoInitialize()
        self.speaker = Speaker(self.cfg)
        self.speaker.warm(self.wake_reply, "Okay.", CONFIRM_SUFFIX)  # instant replies for common phrases
        safety.set_confirm_handler(self._confirm)
        self.running.set()
        try:
            while self.running.is_set():
                if self.paused.is_set():
                    self.mic.stop()  # wake word completely off
                    self.hooks.on_state("idle", "paused")
                    while self.paused.is_set() and self.running.is_set():
                        time.sleep(0.2)
                    continue
                self.mic.start()
                self.hooks.on_state("idle")
                if not self.wait_for_wake():
                    continue
                log("WAKE")
                self.conversation()
        finally:
            self.mic.stop()
            self.hooks.on_state("idle")

    def acknowledge(self) -> None:
        """You said just "Jarvis": answer "Yes, sir?" (or chime)."""
        if self.wake_reply:
            self.hooks.on_state("speaking", self.wake_reply)
            self.hooks.on_reply(self.wake_reply)
            self.speaker.speak(self.wake_reply, on_level=self.hooks.on_level)
            self.hooks.on_level(0.0)
            time.sleep(0.1)
            self.mic.flush()  # drop our own voice echo
            self.wake.reset()
        elif self.chime:
            _chime()

    def conversation(self) -> None:
        """One wake = one conversation; continues while the user keeps talking.

        The first listen includes ~2 s of audio from before the wake trigger, so
        "Jarvis, open Spotify" in one breath works. If that turns out to be only
        the wake word, Jarvis answers "Yes, sir?" and listens for the command.
        """
        wait, preroll, acked = self.no_speech_s, True, False
        while self.running.is_set() and not self.paused.is_set():
            text = self.listen_text(wait, preroll=preroll, silence_s=0.7 if preroll else None)
            preroll = False
            if not text:
                if acked:
                    break  # nothing said after "Yes, sir?" / follow-up window ended
                self.acknowledge()
                acked, wait = True, self.no_speech_s
                continue
            if is_cancel(text):
                self.say("Okay.")
                break
            reply = self.brain.handle(text)
            if getattr(self.brain, "last_error", False):
                self.say(reply, state="error")
                break
            if not self.say(reply):  # barge-in: user said the wake word over the reply
                wait, preroll, acked = self.no_speech_s, True, False
                continue
            wait = self.question_wait_s if reply.rstrip().endswith("?") else self.follow_up_s
            if wait <= 0:
                break
            acked = True  # silence in the follow-up window ends the conversation
        self.hooks.on_state("idle")


# ================================================================ console mode

class ConsoleHooks(Hooks):
    def __init__(self):
        self._state = None

    def on_state(self, state, info=""):
        if state != self._state:
            self._state = state
            label = {"idle": "... waiting for 'Hey Jarvis'", "listening": "LISTENING", "thinking": "PROCESSING",
                     "speaking": "RESPONDING", "confirm": "CONFIRM"}.get(state, state.upper())
            print(f"[{label}]" + (" (paused)" if info == "paused" else ""), flush=True)

    def on_transcript(self, text):
        print(f"  you:    {text}", flush=True)

    def on_reply(self, text):
        print(f"  jarvis: {text}", flush=True)
