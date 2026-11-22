"""Locate Zig for C translation and Windows C++ compilation."""

import importlib.util
import shutil
import sys


def command() -> list[str]:
    if executable := shutil.which("zig"):
        return [executable]
    if importlib.util.find_spec("ziglang") is not None:
        return [sys.executable, "-m", "ziglang"]
    raise ValueError("Install Zig on PATH or install the ziglang package (python -m pip install ziglang)")
