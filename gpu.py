"""Makes the NVIDIA CUDA/cuDNN DLLs from pip (site-packages/nvidia/*/bin) findable on Windows,
so onnxruntime-gpu (Kokoro voice) and CTranslate2 (Whisper) can use the GPU."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_done: bool | None = None


def enable_cuda_dlls() -> bool:
    """Idempotent. Returns True if NVIDIA pip DLL folders were found."""
    global _done
    if _done is not None:
        return _done
    dirs: list[str] = []
    for site in map(Path, sys.path):
        nvidia = site / "nvidia"
        if nvidia.is_dir():
            dirs += [str(b) for b in nvidia.glob("*/bin") if b.is_dir()]
    for d in dirs:
        os.add_dll_directory(d)
    if dirs:  # CTranslate2 loads cuBLAS/cuDNN by name, which also searches PATH
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    _done = bool(dirs)
    return _done
