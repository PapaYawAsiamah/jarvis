# Jarvis: voice desktop assistant for Windows

Say **"Jarvis"**, ask for something in plain English, and it gets done. Jarvis opens apps and
websites, manages files, arranges windows, controls volume and media, and talks back in a natural
voice. While it works, a holographic HUD shows what it's doing.

- **Wake word:** "Jarvis" or "Hey Jarvis", fully offline ([openWakeWord](https://github.com/dscripka/openWakeWord))
- **Speech to text:** offline, with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`base.en`)
- **Brain:** OpenAI (default) or Google Gemini, using tool calling
- **Voice:** a natural offline neural voice ([Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)), with the Windows voice as a fallback
- **HUD:** a transparent, click-through overlay built with PySide6
- **Safety first:** the AI can only call a fixed list of actions, never shell commands. Destructive actions need a spoken "yes".

> Status: the voice assistant and HUD work. A system tray icon is coming next.

## What you can say

| Say | Jarvis |
|---|---|
| "Jarvis" ... "what time is it?" | "Yes, sir?" then tells you the time |
| "Jarvis, I feel like listening to music" | Opens Spotify (or your `music_app`) |
| "Jarvis, open Chrome" / "open settings" / "open calculator" | Opens the app (fuzzy-matched against the Start menu) |
| "Search YouTube for lofi music" / "open github.com" | Opens the site or a Google search |
| "Snap Chrome to the left" / "maximize Spotify" / "minimize Notepad" | Arranges windows |
| "What windows are open?" / "switch to VS Code" | Lists or focuses windows |
| "Show the desktop" / "lock the computer" | Exactly that |
| "It's too loud" / "volume up a bit" / "mute" | Volume control |
| "Pause the music" / "next song" | Media keys |
| "Find my CV in documents" | Searches your files |
| "Create a folder called Projects on my desktop" | Creates it |
| "Move report.pdf from downloads to documents" | Moves it (folders need confirmation) |
| "Rename notes.txt to ideas" | Renames it and keeps the extension |
| "Delete old.zip from my downloads" | Reads it back, waits for "yes", then sends it to the Recycle Bin |
| "Clean up my desktop" / "undo organize" | Sorts loose files into folders by type (can be undone) |
| "Never mind" | Cancels |

**Conversation tips**
- Say "Jarvis" and pause, and it answers "Yes, sir?". Or say everything in one breath: "Jarvis, open Spotify".
- After a reply you have 5 seconds to keep talking without the wake word. If it asks a question, just answer.
- Say "Jarvis" while it's talking to interrupt.
- It remembers the last 10 exchanges ("rename *it*") and forgets after 5 minutes idle.

## Requirements

- **Windows 10 or 11.** It uses Windows APIs, so macOS and Linux aren't supported.
- **Python 3.11.**
- **A microphone and speakers.**
- **An OpenAI API key** ([platform.openai.com](https://platform.openai.com/api-keys)) with a little credit. Replies use a low-cost model, so normal use costs cents. You can use a Google Gemini key instead.
- **Optional: an NVIDIA GPU** (GTX 10-series or newer). This makes the voice start in about 0.4 s and transcription about 10× faster. Without one, everything runs on the CPU automatically.
- **RAM:** about 520 MB (CPU) or 850 MB (GPU). **Disk:** about 1.5 GB, or about 4 GB with the GPU packages.

## Setup

1. **Install Python 3.11:**
   ```powershell
   winget install Python.Python.3.11
   ```
2. **Clone the repo and install the dependencies:**
   ```powershell
   git clone <this repo's URL> jarvis
   cd jarvis
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. **Optional, NVIDIA GPU only:**
   ```powershell
   pip uninstall -y onnxruntime
   pip install -r requirements-gpu.txt
   ```
   **No NVIDIA GPU?** Set `stt.device: cpu` and `tts.kokoro_device: cpu` in `config.yaml`. It would
   fall back to the CPU anyway, but this avoids a warning in the log.
4. **Set your API key** as a user environment variable. Never put it in code or config:
   ```powershell
   setx OPENAI_API_KEY "sk-..."
   # or, for Gemini:  setx GEMINI_API_KEY "..."   and set brain.provider: gemini in config.yaml
   ```
   Then **close and reopen your terminal**, or VS Code entirely if you use its terminal, so the key is picked up.
5. **Check that everything works:**
   ```powershell
   python check_setup.py
   ```
   It records 2 seconds from your mic and speaks a line. The first run downloads the wake-word
   and Whisper models.

## Running

```powershell
python main.py                            # voice assistant with the HUD
python main.py --demo-ui                  # watch the HUD cycle through every state (no mic, no AI)
python main.py --no-ui                    # voice assistant, console only
python main.py --type                     # type commands instead of speaking (handy for testing)
python main.py --type --provider gemini   # use Gemini for this run
python main.py --type --fake-brain        # call actions directly, no AI: e.g.  open_app notepad
```
The first voice run downloads the Kokoro voice model into `models/`: about 120 MB, or 310 MB for
the GPU version. Press Ctrl+C in the terminal to quit.

## Safety

- The AI can **only** call the functions in `actions.py`. It cannot run commands or scripts.
- **Allowed:** your user folder, plus any folders you list in `paths.extra_allowed_folders`.
- **Blocked:** AppData, Windows, Program Files, ProgramData, hidden dot-folders like `.ssh`, and Jarvis's own folder.
- **Protected:** Desktop, Documents and the other known folders can be opened, but never deleted, moved or renamed themselves.
- **Confirmation:** deleting, organizing the desktop, and moving or renaming folders read back exactly what will happen. Only "yes" or "confirm" proceeds; anything else cancels.
- **Deletes** go to the **Recycle Bin**, never permanently.
- **Logging:** every action is logged with a timestamp to `jarvis_log.txt`.

## Privacy

- **Stays on your PC:** your **audio**. The wake word, speech recognition and the voice all run locally.
- **Sent to OpenAI or Gemini:** the **text** of what you said, plus the results of actions. Those
  results can include file names, folder paths and open window titles.
- **The log:** `jarvis_log.txt` records your commands and Jarvis's replies. It's in `.gitignore`;
  don't share it.

## Configuration

Every setting lives in `config.yaml`, with a comment on each line. The ones you'll most likely change:

| Setting | What it does |
|---|---|
| `brain.provider`, `brain.openai_model`, `brain.gemini_model` | Which AI to use |
| `assistant.address` / `assistant.wake_reply` | "sir" / "Yes, sir?" (change to anything) |
| `assistant.music_app` | What "I feel like listening to music" opens |
| `wake_word.threshold` | Lower if it doesn't hear you (try 0.25); raise if it wakes by itself (0.5) |
| `audio.input_gain` | Boost for quiet mics (1.0 = none) |
| `audio.silence_duration_s` | Raise it if Jarvis cuts you off mid-sentence |
| `conversation.follow_up_window_s` | How long it keeps listening after a reply (0 = off) |
| `tts.kokoro_voice` | `am_michael`, `am_puck`, `am_fenrir` (US male), `bm_george` (UK male), `af_heart` (US female), and [others](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) |
| `tts.kokoro_speed` | 1.1 = a little faster |
| `stt.device`, `tts.kokoro_device` | `cuda` (NVIDIA GPU) or `cpu` |
| `ui.position`, `ui.size`, `ui.palette`, `ui.fps` | HUD placement, size, colours, frame rate |
| `paths.extra_allowed_folders` | Extra folders Jarvis may touch, e.g. `["D:/Projects"]` |

## Troubleshooting

| Problem | Fix |
|---|---|
| "API key isn't set" even after `setx` | Restart the terminal. If you use VS Code's terminal, restart VS Code completely |
| Doesn't react to "Jarvis" | Lower `wake_word.threshold`, raise `audio.input_gain`, check Windows mic privacy settings |
| Wakes up by itself | Raise `wake_word.threshold` |
| Cuts you off mid-sentence | Raise `audio.silence_duration_s` (try 1.3) |
| Robotic voice | Kokoro failed to load and Jarvis fell back to the Windows voice. Check `jarvis_log.txt` |
| Gaps between sentences | You're on the CPU. Use the GPU setup, or set `tts.kokoro_speed` to 1.1 |
| GPU errors in the log | Jarvis falls back to the CPU automatically. On GTX 10-series cards keep the versions pinned in `requirements-gpu.txt` (newer CUDA and cuDNN dropped those cards) |

## Project layout

```
main.py            entry point: HUD + voice worker thread, --type / --demo-ui modes
config.yaml        all settings
brain.py           OpenAI / Gemini tool-calling loop, short conversation memory
voice.py           mic, wake word, recording with silence detection, Whisper, Kokoro voice
actions.py         the ONLY things Jarvis can do on the PC
safety.py          path checks, confirmations, logging, undo
gpu.py             makes the NVIDIA DLLs from pip findable
ui/overlay.py      the holographic HUD
check_setup.py     verifies the install
```

## Credits and licences

- [openWakeWord](https://github.com/dscripka/openWakeWord): code under Apache 2.0. The pretrained "hey jarvis" model is **CC BY-NC-SA 4.0 (non-commercial)**.
- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache 2.0), run through [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (MIT).
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT), with OpenAI Whisper models (MIT).
- Fonts: [Orbitron](https://fonts.google.com/specimen/Orbitron) and [Rajdhani](https://fonts.google.com/specimen/Rajdhani) (SIL Open Font License, included in `ui/fonts`).
- The HUD design is original. "Jarvis" is just the wake word.
