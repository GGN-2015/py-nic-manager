from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping

from .admin import is_admin
from .subprocess_utils import run_no_window


QT_PROBE_TIMEOUT_SECONDS = 8
WINDOWS_PLATFORM = "windows"
ELEVATION_GUARD_ENV = "PY_NIC_MANAGER_ELEVATED_RELAUNCH"
PACKAGE_ENTRYPOINT = "py-nic-manager"


def main() -> None:
    if _should_relaunch_as_admin():
        raise SystemExit(_relaunch_as_admin())

    preference = _gui_preference()
    if preference == "tk":
        _run_tkinter()
        return
    if preference != "qt" and not _qt_supported_on_current_platform():
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


def _should_relaunch_as_admin(
    env: Mapping[str, str] | None = None,
    admin_checker=is_admin,
) -> bool:
    current_env = env or os.environ
    if current_env.get(ELEVATION_GUARD_ENV) == "1":
        return False
    return not bool(admin_checker())


def _admin_relaunch_command(
    argv: list[str] | None = None,
    *,
    cwd: str | None = None,
    entrypoint: str | None = None,
) -> list[str]:
    if _is_frozen_app() and entrypoint is None:
        launch_command = sys.executable
        launch_args = ["-m", "py_admin_launch"]
        package_entrypoint = sys.executable
    else:
        launch_command = shutil.which("py-admin-launch") or "py-admin-launch"
        launch_args = []
        package_entrypoint = entrypoint or shutil.which(PACKAGE_ENTRYPOINT) or PACKAGE_ENTRYPOINT
    return [
        launch_command,
        *launch_args,
        "--cwd",
        cwd or os.getcwd(),
        "--",
        package_entrypoint,
        *(argv if argv is not None else sys.argv[1:]),
    ]


def _relaunch_as_admin() -> int:
    env = os.environ.copy()
    env[ELEVATION_GUARD_ENV] = "1"
    command = _admin_relaunch_command()
    try:
        completed = run_no_window(command, check=False, env=env)
    except OSError as exc:
        print(f"Failed to relaunch Py NIC Manager with administrator privileges: {exc}", file=sys.stderr)
        return 1
    return int(completed.returncode)


def _gui_preference(env: Mapping[str, str] | None = None) -> str:
    value = (env or os.environ).get("PY_NIC_MANAGER_GUI", "auto").strip().lower()
    if value in {"qt", "pyqt", "pyqt6"}:
        return "qt"
    if value in {"tk", "tkinter", "legacy"}:
        return "tk"
    return "auto"


def _qt_supported_on_current_platform() -> bool:
    return platform.system().lower() == WINDOWS_PLATFORM


def _qt_runtime_available() -> bool:
    if _is_frozen_app():
        return True
    code = (
        "from PyQt6.QtWidgets import QApplication; "
        "app = QApplication(['py-nic-manager-probe']); "
        "app.quit()"
    )
    env = os.environ.copy()
    env.setdefault("QT_LOGGING_RULES", "*.debug=false")
    try:
        completed = run_no_window(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
            env=env,
            timeout=QT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


if __name__ == "__main__":
    main()
