"""Bundled font helpers for the tkinter interface."""

from __future__ import annotations

import os
import platform
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from importlib import resources
from pathlib import Path

from .subprocess_utils import run_no_window


BUNDLED_FONT_FAMILY = "JetBrains Mono"
BUNDLED_FONT_FILES = (
    "JetBrainsMono-Regular.ttf",
    "JetBrainsMono-Bold.ttf",
)


def configure_tk_fonts(root: tk.Misc) -> str:
    """Apply the bundled monospace font where the local Tk/font stack can see it."""
    family = BUNDLED_FONT_FAMILY if _activate_bundled_font(root) else _fallback_monospace_family(root)
    default_font = (family, 10)
    heading_font = (family, 11, "bold")
    text_font = (family, 10)

    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkCaptionFont", "TkSmallCaptionFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=10)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkHeadingFont").configure(family=family, size=11, weight="bold")
    except tk.TclError:
        pass

    root.option_add("*Font", default_font)
    root.option_add("*Text.Font", text_font)
    root.option_add("*Entry.Font", text_font)
    root.option_add("*Treeview.Font", text_font)
    root.option_add("*Treeview.Heading.Font", heading_font)
    return family


def bundled_font_dir() -> Path:
    return Path(str(resources.files("py_nic_manager.assets.fonts")))


def bundled_font_paths() -> list[Path]:
    directory = bundled_font_dir()
    return [directory / filename for filename in BUNDLED_FONT_FILES]


def _activate_bundled_font(root: tk.Misc) -> bool:
    if _font_family_available(root, BUNDLED_FONT_FAMILY):
        return True
    if platform.system().lower() == "linux":
        _configure_fontconfig()
    return _font_family_available(root, BUNDLED_FONT_FAMILY)


def _configure_fontconfig() -> None:
    directory = bundled_font_dir()
    existing = os.environ.get("FONTCONFIG_PATH", "")
    paths = [str(directory)]
    if existing:
        paths.append(existing)
    os.environ["FONTCONFIG_PATH"] = os.pathsep.join(paths)

    try:
        run_no_window(
            ["fc-cache", "-f", str(directory)],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _font_family_available(root: tk.Misc, family: str) -> bool:
    try:
        families = {item.lower() for item in tkfont.families(root)}
    except tk.TclError:
        return False
    return family.lower() in families


def _fallback_monospace_family(root: tk.Misc) -> str:
    preferred = [
        "JetBrains Mono",
        "DejaVu Sans Mono",
        "Liberation Mono",
        "Noto Sans Mono",
        "Consolas",
        "Menlo",
        "Courier New",
        "TkFixedFont",
    ]
    try:
        available = {item.lower(): item for item in tkfont.families(root)}
    except tk.TclError:
        return "TkFixedFont"
    for family in preferred:
        if family.lower() in available:
            return available[family.lower()]
    return "TkFixedFont"


__all__ = [
    "BUNDLED_FONT_FAMILY",
    "bundled_font_dir",
    "bundled_font_paths",
    "configure_tk_fonts",
]
