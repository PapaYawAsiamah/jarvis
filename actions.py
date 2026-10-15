"""The ONLY things Jarvis can do on this PC.

Each public action returns a short plain-English result string for the brain to relay.
TOOLS describes them (JSON schema) for the AI; run_tool() is the single entry point.
"""
from __future__ import annotations

import ctypes
import difflib
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.parse
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

import safety
from safety import SafetyError, confirm, log, pretty, safe_path

CANCELLED = "Cancelled. Nothing was changed."

# ================================================================ apps

APP_ALIASES = {
    "settings": "ms-settings:", "windows settings": "ms-settings:",
    "calculator": "calculator:", "calc": "calculator:",
    "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "task manager": "taskmgr.exe",
    "camera": "microsoft.windows.camera:",
    "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
}
_START_MENU_DIRS = [
    Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
    Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
]
_SKIP_SHORTCUT = re.compile(r"uninstall|readme|release notes|documentation|help|website|manual", re.I)
_shortcuts: dict[str, str] | None = None  # lowercase name -> .lnk path or shell:AppsFolder target


def _scan_shortcuts() -> dict[str, str]:
    found: dict[str, str] = {}
    for d in _START_MENU_DIRS:
        if d.is_dir():
            for lnk in d.rglob("*.lnk"):
                if not _SKIP_SHORTCUT.search(lnk.stem):
                    found.setdefault(lnk.stem.lower(), str(lnk))
    # Store apps (e.g. Notepad, Photos) have no .lnk; ask the Start menu for its app list.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        for line in out.splitlines():
            name, _, app_id = line.partition("\t")
            if name and app_id and not _SKIP_SHORTCUT.search(name):
                found.setdefault(name.strip().lower(), f"shell:AppsFolder\\{app_id.strip()}")
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def _score(query: str, candidate: str) -> float:
    if query == candidate:
        return 1.0
    score = difflib.SequenceMatcher(None, query, candidate).ratio()
    if candidate.startswith(query):
        score = max(score, 0.92)
    elif query in candidate:
        score = max(score, 0.85)
    elif all(w in candidate for w in query.split()):
        score = max(score, 0.8)
    return score


def _best_match(query: str, candidates: list[str]) -> tuple[str | None, float, list[str]]:
    q = query.lower().strip()
    ranked = sorted(candidates, key=lambda c: (-_score(q, c.lower()), len(c)))
    if not ranked:
        return None, 0.0, []
    return ranked[0], _score(q, ranked[0].lower()), ranked[:3]


def open_app(name: str) -> str:
    global _shortcuts
    key = name.lower().strip().removeprefix("the ").removesuffix(" app").strip()
    if key in APP_ALIASES:
        os.startfile(APP_ALIASES[key])
        return f"Opened {name}."
    if _shortcuts is None:
        _shortcuts = _scan_shortcuts()
    best, score, top = _best_match(key, list(_shortcuts))
    if score < 0.6:  # maybe something was installed since the last scan
        _shortcuts = _scan_shortcuts()
        best, score, top = _best_match(key, list(_shortcuts))
    if best is None or score < 0.6:
        hint = f" Closest: {', '.join(top)}." if top else ""
        return f"I couldn't find an app called {name}.{hint}"
    os.startfile(_shortcuts[best])
    return f"Opened {best.title()}."


# ================================================================ web

_URL_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(:\d+)?([/?#].*)?$", re.I)


def open_website(url_or_search: str) -> str:
    q = url_or_search.strip()
    if re.match(r"^[a-z][a-z0-9+.-]*:", q, re.I) and not q.lower().startswith(("http://", "https://")):
        raise SafetyError("Only web addresses (http/https) can be opened.")
    if _URL_RE.match(q.replace(" ", "")) and " " not in q:
        url = q if q.lower().startswith(("http://", "https://")) else "https://" + q
        webbrowser.open(url)
        return f"Opened {url}."
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(q))
    return f"Searched Google for {q}."


# ================================================================ files & folders

_SKIP_DIRS = {"appdata", "node_modules", "$recycle.bin", "__pycache__", ".git", ".venv", "venv",
              "site-packages"}


def open_folder(path: str) -> str:
    p = safe_path(path)
    if p.is_dir():
        os.startfile(p)
        return f"Opened {pretty(p)}."
    if p.is_file():
        subprocess.Popen(["explorer", f"/select,{p}"])
        return f"Showed {p.name} in its folder."
    return f"{pretty(p)} doesn't exist."


def find_files(name: str, folder: str = "home") -> str:
    root = safe_path(folder or "home")
    if not root.is_dir():
        return f"{pretty(root)} isn't a folder."
    pattern = name.lower().strip()
    wildcard = any(c in pattern for c in "*?[")
    results: list[Path] = []
    deadline = time.monotonic() + 6
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")
                       and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg"))]  # skip virtualenvs
        for n in dirnames + filenames:
            ln = n.lower()
            if (fnmatch.fnmatch(ln, pattern) if wildcard else pattern in ln):
                results.append(Path(dirpath) / n)
        if len(results) >= 15 or time.monotonic() > deadline:
            break
    if not results:
        return f"No matches for {name} in {pretty(root)}."
    listing = "; ".join(pretty(r) for r in results[:15])
    more = " (search stopped early, there may be more)" if len(results) >= 15 or time.monotonic() > deadline else ""
    return f"Found {len(results)}{more}: {listing}"


def create_folder(path: str) -> str:
    p = safe_path(path, modify=True)
    if p.exists():
        return f"{pretty(p)} already exists."
    safety.check_path(p.parent)
    p.mkdir(parents=True)
    return f"Created {pretty(p)}."


def _describe(p: Path) -> str:
    if p.is_dir():
        count = sum(len(f) + len(d) for _, d, f in os.walk(p))
        return f"the folder {pretty(p)} and everything in it ({count} items)"
    size = p.stat().st_size
    human = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{max(1, size // 1024)} KB"
    return f"the file {pretty(p)} ({human})"


def move_item(source: str, destination: str) -> str:
    src = safe_path(source, modify=True)
    if not src.exists():
        return f"{pretty(src)} doesn't exist."
    dst = safe_path(destination)
    final = dst / src.name if dst.is_dir() else dst
    safety.check_path(final, modify=True)
    if final.exists():
        return f"{pretty(final)} already exists, so I didn't move anything."
    if not final.parent.is_dir():
        return f"The destination folder {pretty(final.parent)} doesn't exist."
    if src.is_dir() and not confirm(f"Move {_describe(src)} to {pretty(final)}."):
        return CANCELLED
    shutil.move(str(src), str(final))
    return f"Moved {src.name} to {pretty(final.parent)}."


_BAD_NAME = re.compile(r'[\\/:*?"<>|]')


def rename_item(path: str, new_name: str) -> str:
    p = safe_path(path, modify=True)
    if not p.exists():
        return f"{pretty(p)} doesn't exist."
    new_name = new_name.strip()
    if not new_name or _BAD_NAME.search(new_name) or new_name in {".", ".."}:
        return f"'{new_name}' isn't a valid file name."
    if p.is_file() and not Path(new_name).suffix and p.suffix:
        new_name += p.suffix  # keep the extension if none was spoken
    target = p.with_name(new_name)
    safety.check_path(target, modify=True)
    if target.exists():
        return f"{pretty(target)} already exists."
    if p.is_dir() and not confirm(f"Rename {_describe(p)} to {new_name}."):
        return CANCELLED
    p.rename(target)
    return f"Renamed {p.name} to {new_name}."


def delete_item(path: str) -> str:
    p = safe_path(path, modify=True)
    if not p.exists():
        return f"{pretty(p)} doesn't exist."
    if not confirm(f"Move {_describe(p)} to the Recycle Bin."):
        return CANCELLED
    send2trash(str(p))
    return f"Moved {p.name} to the Recycle Bin."


# ---------------------------------------------------------------- desktop organizer

CATEGORIES = {
    "Images": "jpg jpeg png gif bmp webp tif tiff heic heif svg ico raw cr2 nef psd",
    "Documents": "pdf doc docx txt rtf odt xls xlsx csv ppt pptx odp ods md epub one",
    "Videos": "mp4 mkv avi mov wmv webm flv m4v mpg mpeg 3gp",
    "Music": "mp3 wav flac aac ogg m4a wma opus mid midi",
    "Archives": "zip rar 7z tar gz bz2 xz tgz iso cab",
    "Installers": "exe msi msix msixbundle appx appxbundle",
    "Code": "py js ts jsx tsx html htm css json java c cpp h hpp cs go rs rb php sh ps1 bat cmd ipynb yaml yml xml sql lua kt swift",
}
_EXT_TO_CAT = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts.split()}
_LEAVE_ALONE = {".lnk", ".url", ".ini"}


def _is_hidden(p: Path) -> bool:
    attrs = getattr(p.stat(), "st_file_attributes", 0)
    return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM)) or p.name.startswith(("~$", "."))


def _unique(dest_dir: Path, name: str) -> Path:
    target, stem, suffix, n = dest_dir / name, Path(name).stem, Path(name).suffix, 2
    while target.exists():
        target, n = dest_dir / f"{stem} ({n}){suffix}", n + 1
    return target


def plan_organize(folder: Path) -> dict[str, list[Path]]:
    plan: dict[str, list[Path]] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() in _LEAVE_ALONE or _is_hidden(p):
            continue
        plan.setdefault(_EXT_TO_CAT.get(p.suffix.lower().lstrip("."), "Other"), []).append(p)
    return plan


def organize_folder(folder: Path, ask: bool = True) -> str:
    plan = plan_organize(folder)
    total = sum(len(v) for v in plan.values())
    if not total:
        return f"{pretty(folder)} has no loose files to organize."
    summary = ", ".join(f"{len(v)} into {k}" for k, v in plan.items())
    if ask and not confirm(f"Move {total} loose files on the {pretty(folder)}: {summary}. Shortcuts stay where they are."):
        return CANCELLED
    record = {"time": datetime.now().isoformat(timespec="seconds"), "folder": str(folder),
              "moves": [], "created_dirs": []}
    try:
        for cat, files in plan.items():
            dest = folder / cat
            if not dest.exists():
                dest.mkdir()
                record["created_dirs"].append(str(dest))
            for f in files:
                target = _unique(dest, f.name)
                shutil.move(str(f), str(target))
                record["moves"].append({"from": str(f), "to": str(target)})
    finally:
        safety.save_undo(record)  # even a partial run can be undone
    return f"Organized {len(record['moves'])} files: {summary}. Say undo organize to put them back."


def organize_desktop() -> str:
    desktop = safety.KNOWN_FOLDERS["desktop"]
    safety.check_path(desktop)
    return organize_folder(desktop)


def undo_organize() -> str:
    record = safety.load_undo()
    if not record:
        return "There's no organize to undo."
    restored, skipped = 0, 0
    for move in reversed(record["moves"]):
        src, dst = Path(move["to"]), Path(move["from"])
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            restored += 1
        else:
            skipped += 1
    for d in record["created_dirs"]:
        try:
            Path(d).rmdir()  # only removes it if empty
        except OSError:
            pass
    safety.clear_undo()
    extra = f" {skipped} could not be restored because they were moved or replaced." if skipped else ""
    return f"Put {restored} files back.{extra}"


# ================================================================ windows

user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD()
    _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return bool(cloaked.value)


def _windows():
    import pygetwindow as gw
    out = []
    for w in gw.getAllWindows():
        title = w.title.strip()
        if (title and title != "Program Manager" and user32.IsWindowVisible(w._hWnd)
                and not _is_cloaked(w._hWnd)):
            out.append(w)
    return out


def _find_window(title: str):
    wins = _windows()
    best, score, top = _best_match(title, [w.title for w in wins])
    if best is None or score < 0.5:
        hint = f" Open windows include: {', '.join(top)}." if top else ""
        raise LookupError(f"I couldn't find a window matching {title}.{hint}")
    return next(w for w in wins if w.title == best)


def _activate(w) -> None:
    import pyautogui
    if w.isMinimized:
        w.restore()
    pyautogui.press("alt")  # lets us take the foreground (Windows focus-stealing rule)
    try:
        w.activate()
    except Exception:  # pygetwindow often raises even when it worked
        user32.SetForegroundWindow(w._hWnd)


def list_windows() -> str:
    titles = [w.title for w in _windows()]
    return "Open windows: " + "; ".join(titles) if titles else "No windows are open."


def focus_window(title: str) -> str:
    w = _find_window(title)
    _activate(w)
    return f"Switched to {w.title}."


def snap_window(title: str, position: str) -> str:
    import pyautogui
    position = position.lower().strip()
    if position not in {"left", "right", "maximize"}:
        return "Position must be left, right, or maximize."
    w = _find_window(title)
    _activate(w)
    time.sleep(0.15)
    if position == "maximize":
        w.maximize()
    else:
        if w.isMaximized:
            w.restore()
            time.sleep(0.1)
        pyautogui.hotkey("win", position)
    return f"Snapped {w.title} {'to the ' + position if position != 'maximize' else 'to full screen'}."


def minimize_window(title: str) -> str:
    w = _find_window(title)
    w.minimize()
    return f"Minimized {w.title}."


def close_window(title: str) -> str:
    w = _find_window(title)
    w.close()  # polite WM_CLOSE: apps still get to ask about unsaved work
    return f"Closed {w.title}."


def show_desktop() -> str:
    import pyautogui
    pyautogui.hotkey("win", "d")
    return "Showing the desktop."


# ================================================================ system

def volume(direction: str, steps: int = 5) -> str:
    import pyautogui
    direction = direction.lower().strip()
    steps = max(1, min(int(steps or 5), 50))
    if direction == "mute":
        pyautogui.press("volumemute")
        return "Toggled mute."
    if direction not in {"up", "down"}:
        return "Direction must be up, down, or mute."
    pyautogui.press("volumeup" if direction == "up" else "volumedown", presses=steps)
    return f"Volume {direction} by {steps * 2} percent."


def media(action: str) -> str:
    import pyautogui
    action = action.lower().strip()
    keys = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack"}
    if action not in keys:
        return "Media action must be play_pause, next, or previous."
    pyautogui.press(keys[action])
    return {"play_pause": "Toggled play/pause.", "next": "Next track.", "previous": "Previous track."}[action]


def lock_computer() -> str:
    user32.LockWorkStation()
    return "Locking the computer."


def current_time() -> str:
    now = datetime.now()
    return now.strftime("It's %I:%M %p on %A, %B %d, %Y.").replace(" 0", " ")


# ================================================================ tool registry

def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


_PATH = "File or folder path. May start with desktop, documents, downloads, pictures, music, videos or home, e.g. 'desktop/JarvisTest/notes.txt'."

TOOLS: list[dict] = [
    {"name": "open_app", "func": open_app,
     "description": "Open an installed application by name (fuzzy matched against Start Menu), e.g. 'chrome', 'spotify', 'settings', 'calculator', 'file explorer', 'task manager', 'camera', 'store'.",
     "parameters": {"type": "object", "properties": {"name": _s("App name.")}, "required": ["name"]}},
    {"name": "open_website", "func": open_website,
     "description": "Open a web address, or search Google if the text is not a URL.",
     "parameters": {"type": "object", "properties": {"url_or_search": _s("A URL like 'youtube.com' or search words.")}, "required": ["url_or_search"]}},
    {"name": "open_folder", "func": open_folder,
     "description": "Open a folder in File Explorer (or show a file in its folder).",
     "parameters": {"type": "object", "properties": {"path": _s(_PATH)}, "required": ["path"]}},
    {"name": "find_files", "func": find_files,
     "description": "Search for files or folders whose name contains the text (wildcards like '*.pdf' allowed).",
     "parameters": {"type": "object", "properties": {"name": _s("Part of the name, or a wildcard pattern."),
                                                     "folder": _s("Folder to search in. Default: home (whole user folder).")},
                    "required": ["name"]}},
    {"name": "create_folder", "func": create_folder,
     "description": "Create a new folder.",
     "parameters": {"type": "object", "properties": {"path": _s(_PATH)}, "required": ["path"]}},
    {"name": "move_item", "func": move_item,
     "description": "Move a file or folder. If destination is an existing folder, the item goes inside it. Folders require spoken confirmation (handled automatically).",
     "parameters": {"type": "object", "properties": {"source": _s(_PATH), "destination": _s(_PATH)}, "required": ["source", "destination"]}},
    {"name": "rename_item", "func": rename_item,
     "description": "Rename a file or folder (same location). The file extension is kept if new_name has none. Folders require confirmation (handled automatically).",
     "parameters": {"type": "object", "properties": {"path": _s(_PATH), "new_name": _s("New name only, not a path.")}, "required": ["path", "new_name"]}},
    {"name": "delete_item", "func": delete_item,
     "description": "Move a file or folder to the Recycle Bin (never permanent). Always asks the user to confirm (handled automatically).",
     "parameters": {"type": "object", "properties": {"path": _s(_PATH)}, "required": ["path"]}},
    {"name": "organize_desktop", "func": organize_desktop,
     "description": "Sort loose desktop files into Images, Documents, Videos, Music, Archives, Installers, Code, Other folders. Shortcuts stay. Asks for confirmation; can be undone.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "undo_organize", "func": undo_organize,
     "description": "Undo the last organize_desktop, putting files back where they were.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "list_windows", "func": list_windows,
     "description": "List the titles of open windows.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "focus_window", "func": focus_window,
     "description": "Bring a window to the front. Title is fuzzy matched (e.g. 'chrome', 'notepad').",
     "parameters": {"type": "object", "properties": {"title": _s("Part of the window title or app name.")}, "required": ["title"]}},
    {"name": "snap_window", "func": snap_window,
     "description": "Snap a window to the left or right half of the screen, or maximize it.",
     "parameters": {"type": "object", "properties": {"title": _s("Part of the window title or app name."),
                                                     "position": {"type": "string", "enum": ["left", "right", "maximize"]}},
                    "required": ["title", "position"]}},
    {"name": "minimize_window", "func": minimize_window,
     "description": "Minimize a window.",
     "parameters": {"type": "object", "properties": {"title": _s("Part of the window title or app name.")}, "required": ["title"]}},
    {"name": "close_window", "func": close_window,
     "description": "Close a window (the app may still ask to save).",
     "parameters": {"type": "object", "properties": {"title": _s("Part of the window title or app name.")}, "required": ["title"]}},
    {"name": "show_desktop", "func": show_desktop,
     "description": "Minimize everything and show the desktop.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "volume", "func": volume,
     "description": "Change system volume. Each step is 2 percent.",
     "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "mute"]},
                                                     "steps": {"type": "integer", "description": "Number of 2% steps (default 5)."}},
                    "required": ["direction"]}},
    {"name": "media", "func": media,
     "description": "Control media playback.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["play_pause", "next", "previous"]}}, "required": ["action"]}},
    {"name": "lock_computer", "func": lock_computer,
     "description": "Lock the PC.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "current_time", "func": current_time,
     "description": "Get the current local date and time.",
     "parameters": {"type": "object", "properties": {}}},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def run_tool(name: str, args: dict | None = None) -> str:
    """Single entry point used by the brain. Never raises; always logs."""
    args = dict(args or {})
    tool = _BY_NAME.get(name)
    if tool is None:
        result = f"Unknown action {name}."
    else:
        allowed = tool["parameters"]["properties"].keys()
        args = {k: v for k, v in args.items() if k in allowed}
        try:
            result = tool["func"](**args)
        except SafetyError as e:
            result = f"Blocked: {e}"
        except (LookupError, FileNotFoundError, PermissionError) as e:
            result = str(e) if isinstance(e, LookupError) else f"Couldn't do that: {e.strerror or e}."
        except TypeError as e:
            result = f"Bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001
            result = f"Something went wrong: {e}"
    log(f"ACTION {name}({args}) -> {result}")
    return result
