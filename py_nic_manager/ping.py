from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

from .backends import decode_command_output
from .validation import validate_ip


def ping_test_command(backend_name: str, src_ip_addr: str, dest_ip_addr: str) -> list[str]:
    src = validate_ip(src_ip_addr)
    dest = validate_ip(dest_ip_addr)
    if backend_name == "Windows":
        return ["ping", "-S", src, dest]
    if backend_name == "Linux":
        return ["ping", "-I", src, "-c4", dest]
    if backend_name == "macOS":
        return ["ping", "-S", src, dest]
    return ["ping", "-I", src, dest]


def start_ping_test_process(
    backend_name: str,
    src_ip_addr: str,
    dest_ip_addr: str,
) -> subprocess.Popen[bytes]:
    command = ping_test_command(backend_name, src_ip_addr, dest_ip_addr)
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, **kwargs)


def iter_ping_process_output(process: subprocess.Popen[bytes]) -> Iterator[str]:
    if process.stdout is None:
        return
    for line in iter(process.stdout.readline, b""):
        if line:
            yield decode_command_output(line)
