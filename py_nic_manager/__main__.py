from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping


QT_PROBE_TIMEOUT_SECONDS = 8


def main() -> None:
    preference = _gui_preference()
    if preference == "tk":
        _run_tkinter()
        return

    try:
        from .qt_app import main as qt_main
    except ImportError:
        _run_tkinter()
        return

    if preference != "qt" and not _qt_runtime_available():
        print(
            "PyQt6 could not initialize a GUI in this environment; "
            "falling back to the tkinter interface. Set PY_NIC_MANAGER_GUI=qt "
            "to force PyQt6.",
            file=sys.stderr,
        )
        _run_tkinter()
        return

    qt_main()


def _run_tkinter() -> None:
    from .app import main as tkinter_main

    tkinter_main()


def _gui_preference(env: Mapping[str, str] | None = None) -> str:
    value = (env or os.environ).get("PY_NIC_MANAGER_GUI", "auto").strip().lower()
    if value in {"qt", "pyqt", "pyqt6"}:
        return "qt"
    if value in {"tk", "tkinter", "legacy"}:
        return "tk"
    return "auto"


def _qt_runtime_available() -> bool:
    code = (
        "from PyQt6.QtWidgets import QApplication; "
        "app = QApplication(['py-nic-manager-probe']); "
        "app.quit()"
    )
    env = os.environ.copy()
    env.setdefault("QT_LOGGING_RULES", "*.debug=false")
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
            env=env,
            timeout=QT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


if __name__ == "__main__":
    main()
