"""Entry point used by single-file GUI builds."""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType


HELPER_MODULES = {
    "py_admin_launch",
    "py_nic_manager.global_forwarding",
    "py_nic_manager.macos_forwarding",
    "py_nic_manager.nat_persistence",
    "py_nic_manager.ttl_exceeded",
    "py_nic_manager.windows_loopback",
    "py_nic_manager.windows_virtual",
    "py_nic_manager.windows_wintun",
}


def main() -> int | None:
    stdout_path, stderr_path, argv = _extract_redirect_paths(sys.argv)
    with _optional_redirect(stdout_path, stderr_path):
        _ensure_standard_streams()
        helper_result = _dispatch_python_module_style_call(argv)
        if helper_result is not None:
            return helper_result

        from py_nic_manager.__main__ import main as gui_main

        gui_main()
    return None


def _dispatch_python_module_style_call(argv: list[str]) -> int | None:
    if len(argv) < 3 or argv[1] != "-m":
        return None
    module_name = argv[2]
    if module_name not in HELPER_MODULES:
        return None
    module = importlib.import_module(module_name)
    return _run_module_main(module, argv[3:])


def _run_module_main(module: ModuleType, argv: list[str]) -> int:
    entry = getattr(module, "main", None)
    if entry is None:
        raise SystemExit(f"Frozen helper module has no main() function: {module.__name__}")
    result = entry(argv)
    return 0 if result is None else int(result)


def _ensure_standard_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _open_standard_stream(1)
    if sys.stderr is None:
        sys.stderr = _open_standard_stream(2)


def _open_standard_stream(fd: int):
    try:
        return os.fdopen(fd, "w", encoding="utf-8", closefd=False)
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


def _extract_redirect_paths(argv: list[str]) -> tuple[str, str, list[str]]:
    cleaned = [argv[0]]
    stdout_path = ""
    stderr_path = ""
    index = 1
    while index < len(argv):
        item = argv[index]
        if item == "--py-nic-manager-frozen-stdout" and index + 1 < len(argv):
            stdout_path = argv[index + 1]
            index += 2
            continue
        if item == "--py-nic-manager-frozen-stderr" and index + 1 < len(argv):
            stderr_path = argv[index + 1]
            index += 2
            continue
        cleaned.append(item)
        index += 1
    return stdout_path, stderr_path, cleaned


class _optional_redirect:
    def __init__(self, stdout_path: str, stderr_path: str) -> None:
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.stdout_handle = None
        self.stderr_handle = None
        self.stdout_context = None
        self.stderr_context = None

    def __enter__(self):
        if self.stdout_path:
            Path(self.stdout_path).parent.mkdir(parents=True, exist_ok=True)
            self.stdout_handle = open(self.stdout_path, "w", encoding="utf-8")
            self.stdout_context = redirect_stdout(self.stdout_handle)
            self.stdout_context.__enter__()
        if self.stderr_path:
            Path(self.stderr_path).parent.mkdir(parents=True, exist_ok=True)
            self.stderr_handle = open(self.stderr_path, "w", encoding="utf-8")
            self.stderr_context = redirect_stderr(self.stderr_handle)
            self.stderr_context.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self.stderr_context is not None:
            self.stderr_context.__exit__(exc_type, exc, traceback)
        if self.stdout_context is not None:
            self.stdout_context.__exit__(exc_type, exc, traceback)
        if self.stderr_handle is not None:
            self.stderr_handle.close()
        if self.stdout_handle is not None:
            self.stdout_handle.close()
        return False


if __name__ == "__main__":
    raise SystemExit(main())
