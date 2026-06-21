"""Subprocess helpers that avoid transient console windows on Windows."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from typing import Any


def run_no_window(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(command, **_with_no_window(kwargs))


def popen_no_window(command: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    return subprocess.Popen(command, **_with_no_window(kwargs))


def _with_no_window(kwargs: dict[str, Any]) -> dict[str, Any]:
    if not sys.platform.startswith("win"):
        return kwargs
    flags = int(kwargs.pop("creationflags", 0) or 0)
    flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    startupinfo = kwargs.get("startupinfo")
    startupinfo = _hidden_startupinfo(startupinfo)
    kwargs["creationflags"] = flags
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _hidden_startupinfo(startupinfo: Any) -> Any:
    startupinfo_class = getattr(subprocess, "STARTUPINFO", None)
    startf_use_showwindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    sw_hide = getattr(subprocess, "SW_HIDE", 0)
    if startupinfo_class is None or not startf_use_showwindow:
        return startupinfo
    info = startupinfo or startupinfo_class()
    info.dwFlags |= startf_use_showwindow
    info.wShowWindow = sw_hide
    return info


__all__ = ["popen_no_window", "run_no_window"]
