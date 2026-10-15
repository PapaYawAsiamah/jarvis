"""Safety layer: path resolution + allow/deny checks, confirmations, logging, undo records.

Every action in actions.py goes through this module before touching the file system.
"""
from __future__ import annotations

import json
import os
import re
import threading
import winreg
from datetime import datetime
from pathlib import Path
from typing import Callable

from config import ROOT, load_config

_cfg = load_config()
LOG_PATH = ROOT / _cfg["logging"]["file"]
UNDO_PATH = ROOT / "undo_organize.json"
_log_lock = threading.Lock()


class SafetyError(Exception):
    """Raised when an action would touch something Jarvis is not allowed to touch."""


# ---------------------------------------------------------------- logging

def log(message: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)


# ---------------------------------------------------------------- known folders

_SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
_KNOWN_FOLDER_VALUES = {
    "desktop": "Desktop",
    "documents": "Personal",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "My Pictures",
    "music": "My Music",
    "videos": "My Video",
}
_FALLBACK_NAMES = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
                   "pictures": "Pictures", "music": "Music", "videos": "Videos"}


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _read_known_folders() -> dict[str, Path]:
    home = Path.home()
    folders: dict[str, Path] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS_KEY) as key:
            for alias, value_name in _KNOWN_FOLDER_VALUES.items():
                try:
                    raw, _ = winreg.QueryValueEx(key, value_name)
                    folders[alias] = Path(os.path.expandvars(raw))
                except OSError:
                    pass
    except OSError:
        pass
    for alias, name in _FALLBACK_NAMES.items():
        folders.setdefault(alias, home / name)
    return folders


HOME = Path.home()
KNOWN_FOLDERS = _read_known_folders()
ALIASES: dict[str, Path] = {
    **KNOWN_FOLDERS,
    "home": HOME, "user": HOME, "user folder": HOME, "my files": HOME,
    "document": KNOWN_FOLDERS["documents"], "download": KNOWN_FOLDERS["downloads"],
    "picture": KNOWN_FOLDERS["pictures"], "photos": KNOWN_FOLDERS["pictures"],
    "video": KNOWN_FOLDERS["videos"],
}
if os.environ.get("OneDrive"):
    ALIASES["onedrive"] = Path(os.environ["OneDrive"])

EXTRA_ROOTS = [Path(os.path.expandvars(os.path.expanduser(p)))
               for p in _cfg["paths"]["extra_allowed_folders"] or []]
ALLOWED_ROOTS = [HOME, *EXTRA_ROOTS]

_env = os.environ.get
BLOCKED_ROOTS = [p for p in (
    HOME / "AppData",
    _env("SystemRoot") or r"C:\Windows",
    _env("ProgramFiles") or r"C:\Program Files",
    _env("ProgramFiles(x86)") or r"C:\Program Files (x86)",
    _env("ProgramData") or r"C:\ProgramData",
) if p]

# Folders that may be read but never deleted, moved or renamed themselves.
PROTECTED_ROOTS = {_norm(p) for p in [HOME, *KNOWN_FOLDERS.values(), *EXTRA_ROOTS,
                                      *( [ALIASES["onedrive"]] if "onedrive" in ALIASES else [] )]}


def _is_within(path: str, root: Path | str) -> bool:
    root_n = _norm(root)
    return path == root_n or path.startswith(root_n.rstrip("\\") + "\\")


# ---------------------------------------------------------------- path resolution

def resolve_path(spoken: str) -> Path:
    """Turn 'desktop', 'downloads/report.pdf', 'C:/Users/.../x' etc. into an absolute path.

    Relative paths without a known-folder prefix are taken relative to the user folder.
    """
    s = (spoken or "").strip().strip("\"'").strip()
    if not s:
        raise SafetyError("No path given.")
    s = os.path.expanduser(s)
    if os.path.isabs(s):
        return Path(os.path.realpath(s))

    parts = re.split(r"[\\/]+", s)
    head = parts[0].lower().removeprefix("my ").removesuffix(" folder").strip()
    if head in ALIASES:
        return Path(os.path.realpath(ALIASES[head].joinpath(*parts[1:])))
    return Path(os.path.realpath(HOME.joinpath(*parts)))


def check_path(path: Path | str, *, modify: bool = False) -> Path:
    """Raise SafetyError unless `path` is inside an allowed root and outside blocked roots.

    modify=True additionally forbids the root of known folders, hidden dot-folders in the
    user folder, and Jarvis's own folder.
    """
    p = _norm(path)
    if not any(_is_within(p, r) for r in ALLOWED_ROOTS):
        raise SafetyError(f"{path} is outside the folders I'm allowed to touch.")
    for blocked in BLOCKED_ROOTS:
        if _is_within(p, blocked):
            raise SafetyError(f"{path} is in a protected system location.")
    if modify:
        if p in PROTECTED_ROOTS:
            raise SafetyError(f"I can't modify {Path(path).name or path} itself, only things inside it.")
        if _is_within(p, ROOT) or _is_within(_norm(ROOT), p):
            raise SafetyError("I won't modify my own program folder.")
        rel = os.path.relpath(p, _norm(HOME)) if _is_within(p, HOME) else ""
        if rel and rel.split("\\")[0].startswith("."):
            raise SafetyError(f"{path} is a hidden configuration folder.")
    return Path(os.path.realpath(str(path)))


def safe_path(spoken: str, *, modify: bool = False) -> Path:
    return check_path(resolve_path(spoken), modify=modify)


def pretty(path: Path) -> str:
    """Short spoken-friendly form: 'Desktop\\JarvisTest\\a.txt'."""
    p = _norm(path)
    for alias in ("desktop", "documents", "downloads", "pictures", "music", "videos"):
        root = KNOWN_FOLDERS[alias]
        if _is_within(p, root):
            rel = os.path.relpath(str(path), str(root))
            return alias.capitalize() if rel == "." else f"{alias.capitalize()}\\{rel}"
    return str(path)


# ---------------------------------------------------------------- confirmations

_YES = {"yes", "confirm", "yes confirm", "confirm yes", "yes please", "yes do it", "yes go ahead"}
_confirm_handler: Callable[[str], str | None] | None = None


def set_confirm_handler(fn: Callable[[str], str | None]) -> None:
    """fn(prompt) must present the prompt to the user and return their raw answer (or None)."""
    global _confirm_handler
    _confirm_handler = fn


def is_yes(answer: str | None) -> bool:
    text = re.sub(r"[^a-z ]", "", (answer or "").lower()).strip()
    return re.sub(r"\s+", " ", text) in _YES


def confirm(description: str) -> bool:
    """Read back exactly what will happen; only an explicit yes/confirm proceeds."""
    if _confirm_handler is None:
        log(f"CONFIRM (no handler, cancelled): {description}")
        return False
    answer = _confirm_handler(description)
    ok = is_yes(answer)
    log(f"CONFIRM {'YES' if ok else 'NO'} ({answer!r}): {description}")
    return ok


# ---------------------------------------------------------------- undo records

def save_undo(record: dict) -> None:
    UNDO_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")


def load_undo() -> dict | None:
    if not UNDO_PATH.exists():
        return None
    return json.loads(UNDO_PATH.read_text(encoding="utf-8"))


def clear_undo() -> None:
    UNDO_PATH.unlink(missing_ok=True)
