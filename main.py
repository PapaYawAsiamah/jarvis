"""Jarvis entry point.

  python main.py                       voice mode with the holographic HUD
  python main.py --demo-ui             cycle through all HUD states (no mic, no AI)
  python main.py --no-ui               voice mode, console only
  python main.py --type                type commands to the AI brain
  python main.py --type --fake-brain   type tool calls directly (no AI, no mic)
  python main.py --provider gemini     override brain.provider for this run
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import traceback

import safety
from brain import BrainError, is_cancel, make_brain
from config import load_config


def run_type_mode(brain) -> None:
    def ask(prompt: str) -> str:
        print(f"JARVIS: {prompt} Say YES to confirm.")
        return input("confirm> ")

    safety.set_confirm_handler(ask)
    print("Jarvis type mode. Type 'help' for tools, 'quit' to exit.")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            break
        if is_cancel(text):
            print("JARVIS: Okay, cancelled.")
            continue
        print(f"JARVIS: {brain.handle(text)}")


def run_console_voice(cfg: dict, brain) -> None:
    from voice import ConsoleHooks, VoiceAssistant
    print("Loading speech models...")
    assistant = VoiceAssistant(cfg, brain, ConsoleHooks())
    print("Ready. Say \"Hey Jarvis\" (or just \"Jarvis\"). Ctrl+C to quit.")
    worker = threading.Thread(target=assistant.run, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.3)
    except KeyboardInterrupt:
        print("\nStopping...")
        assistant.stop()
        worker.join(3)


def run_gui(cfg: dict, brain, demo: bool) -> None:
    from PySide6.QtCore import QThread, QTimer, Signal
    from PySide6.QtWidgets import QApplication

    from ui.overlay import DemoDriver, Overlay

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    overlay = Overlay(cfg)

    # Ctrl+C in the console quits (Qt would otherwise swallow it)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(250)

    if demo:
        print("HUD demo: cycling through all states. Ctrl+C to quit.")
        driver = DemoDriver(overlay)
        driver.start()
        app.exec()
        return

    from voice import ConsoleHooks, VoiceAssistant

    class VoiceWorker(QThread):
        """Runs the voice/brain loop off the UI thread; reports only through signals."""
        state_changed = Signal(str, str)
        level = Signal(float)
        transcript = Signal(str)
        reply = Signal(str)

        def __init__(self):
            super().__init__()
            self.assistant = None
            worker = self

            class QtHooks(ConsoleHooks):
                def on_state(self, state, info=""):
                    super().on_state(state, info)
                    worker.state_changed.emit(state, info)

                def on_level(self, value):
                    worker.level.emit(float(value))

                def on_transcript(self, text):
                    super().on_transcript(text)
                    worker.transcript.emit(text)

                def on_reply(self, text):
                    super().on_reply(text)
                    worker.reply.emit(text)

            self.hooks = QtHooks()

        def run(self):
            try:
                print("Loading speech models...", flush=True)
                self.assistant = VoiceAssistant(cfg, brain, self.hooks)
                print("Ready. Say \"Hey Jarvis\" (or just \"Jarvis\"). Ctrl+C to quit.", flush=True)
                self.assistant.run()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                safety.log(f"VOICE WORKER CRASH: {e!r}")
                self.state_changed.emit("error", f"Voice system failed: {e}")

        def stop(self):
            if self.assistant:
                self.assistant.stop()

    worker = VoiceWorker()
    worker.state_changed.connect(overlay.set_state)
    worker.level.connect(overlay.set_level)
    worker.transcript.connect(overlay.set_transcript)
    worker.reply.connect(overlay.set_reply)
    app.aboutToQuit.connect(worker.stop)
    worker.start()
    app.exec()
    worker.stop()
    worker.wait(4000)


def main() -> None:
    ap = argparse.ArgumentParser(description="Jarvis voice assistant")
    ap.add_argument("--type", action="store_true", help="type commands instead of using the mic")
    ap.add_argument("--fake-brain", action="store_true", help="no AI: type tool calls directly")
    ap.add_argument("--provider", choices=["openai", "gemini"], help="override brain.provider from config")
    ap.add_argument("--demo-ui", action="store_true", help="cycle through all HUD states (no mic, no AI)")
    ap.add_argument("--no-ui", action="store_true", help="voice mode without the HUD (console only)")
    a = ap.parse_args()

    cfg = load_config()
    if a.demo_ui:
        run_gui(cfg, None, demo=True)
        return
    safety.log("START " + " ".join(sys.argv[1:]))
    try:
        brain = make_brain(cfg, fake=a.fake_brain, provider=a.provider)
    except BrainError as e:
        sys.exit(f"JARVIS: {e}")
    if a.type:
        run_type_mode(brain)
    elif a.no_ui:
        run_console_voice(cfg, brain)
    else:
        run_gui(cfg, brain, demo=False)
    safety.log("STOP")


if __name__ == "__main__":
    main()
