"""Persistent NAT helpers used by platform backends."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


APP_DIR = Path("/etc/py-nic-manager")
NAT_STATE_FILE = APP_DIR / "nat-rules.json"
LINUX_SERVICE_FILE = Path("/etc/systemd/system/py-nic-manager-nat.service")
MACOS_ANCHOR_FILE = Path("/etc/pf.anchors/py-nic-manager-nat")
MACOS_PF_CONF = Path("/etc/pf.conf")
MACOS_FALLBACK_ANCHOR = "py-nic-manager-nat"


def load_rules() -> list[dict[str, Any]]:
    try:
        data = json.loads(NAT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def save_rules(rules: list[dict[str, Any]]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    NAT_STATE_FILE.write_text(json.dumps(rules, indent=2, sort_keys=True), encoding="utf-8")


def add_rule(name: str, source_cidr: str, outbound_interface: str, enabled: bool) -> None:
    rules = [
        rule
        for rule in load_rules()
        if str(rule.get("name", "")).strip().lower() != name.strip().lower()
    ]
    rules.append(
        {
            "name": name,
            "source_cidr": source_cidr,
            "outbound_interface": outbound_interface,
            "enabled": enabled,
            "persistent": True,
            "managed": True,
            "family": "ipv4",
        }
    )
    save_rules(rules)
    apply_rules()
    install_startup_hook()


def delete_rule(name: str) -> None:
    rules = [
        rule
        for rule in load_rules()
        if str(rule.get("name", "")).strip().lower() != name.strip().lower()
    ]
    save_rules(rules)
    apply_rules()
    install_startup_hook()


def apply_rules() -> None:
    system = platform.system().lower()
    if system == "linux":
        _apply_linux_rules(load_rules())
    elif system == "darwin":
        _apply_macos_rules(load_rules())
    else:
        raise RuntimeError("Persistent NAT helper supports Linux and macOS only.")


def install_startup_hook() -> None:
    system = platform.system().lower()
    if system == "linux":
        _install_linux_service()
    elif system == "darwin":
        _ensure_macos_pf_anchor()


def _apply_linux_rules(rules: list[dict[str, Any]]) -> None:
    iptables = shutil.which("iptables")
    if not iptables:
        raise RuntimeError("iptables is required for persistent Linux NAT rules.")
    marker = "py-nic-manager-nat"
    _run([iptables, "-t", "nat", "-S", "POSTROUTING"], allow_failure=True)
    for line in _iptables_postrouting_rules(iptables):
        if marker in line:
            parts = line.split()
            delete_args = ["-t", "nat", "-D", "POSTROUTING", *parts[2:]]
            _run([iptables, *delete_args], allow_failure=True)
    for rule in rules:
        if not _rule_enabled(rule):
            continue
        source = str(rule.get("source_cidr", "")).strip()
        outbound = str(rule.get("outbound_interface", "")).strip()
        name = str(rule.get("name", "")).strip()
        command = [iptables, "-t", "nat", "-A", "POSTROUTING", "-s", source]
        if outbound:
            command.extend(["-o", outbound])
        command.extend(["-m", "comment", "--comment", f"{marker}:{name}", "-j", "MASQUERADE"])
        _run(command)


def _iptables_postrouting_rules(iptables: str) -> list[str]:
    result = subprocess.run(
        [iptables, "-t", "nat", "-S", "POSTROUTING"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.startswith("-A POSTROUTING")]


def _install_linux_service() -> None:
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl is required to make Linux NAT rules persistent after reboot.")
    service = """[Unit]
Description=Py NIC Manager persistent NAT rules
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python -m py_nic_manager.nat_persistence apply
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
    LINUX_SERVICE_FILE.write_text(service, encoding="utf-8")
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", "--now", LINUX_SERVICE_FILE.name])


def _apply_macos_rules(rules: list[dict[str, Any]]) -> None:
    enabled_rules = [rule for rule in rules if _rule_enabled(rule)]
    lines = ["# Py NIC Manager persistent NAT rules\n"]
    for rule in enabled_rules:
        source = str(rule.get("source_cidr", "")).strip()
        outbound = str(rule.get("outbound_interface", "")).strip()
        name = str(rule.get("name", "")).replace('"', "")
        if outbound:
            lines.append(f'nat on {outbound} from {source} to any -> ({outbound}) # {name}\n')
        else:
            lines.append(f"nat from {source} to any -> (egress) # {name}\n")
    MACOS_ANCHOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    MACOS_ANCHOR_FILE.write_text("".join(lines), encoding="utf-8")
    _ensure_macos_pf_anchor()
    anchor = _macos_anchor_name()
    _run(["pfctl", "-a", anchor, "-f", str(MACOS_ANCHOR_FILE)])
    _run(["pfctl", "-E"], allow_failure=True)


def _ensure_macos_pf_anchor() -> None:
    try:
        pf_conf = MACOS_PF_CONF.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pf_conf = ""
    if 'anchor "com.apple/*"' in pf_conf:
        return
    if f'anchor "{MACOS_FALLBACK_ANCHOR}"' in pf_conf:
        return
    with MACOS_PF_CONF.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n# Py NIC Manager persistent NAT rules\n"
            f'anchor "{MACOS_FALLBACK_ANCHOR}"\n'
            f'load anchor "{MACOS_FALLBACK_ANCHOR}" from "{MACOS_ANCHOR_FILE}"\n'
        )
    _run(["pfctl", "-f", str(MACOS_PF_CONF)])


def _macos_anchor_name() -> str:
    try:
        pf_conf = MACOS_PF_CONF.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pf_conf = ""
    if 'anchor "com.apple/*"' in pf_conf:
        return "com.apple/py-nic-manager-nat"
    return MACOS_FALLBACK_ANCHOR


def _rule_enabled(rule: dict[str, Any]) -> bool:
    value = rule.get("enabled", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "disabled", "off"}


def _run(command: list[str], *, allow_failure: bool = False) -> None:
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    if result.returncode != 0 and not allow_failure:
        output = (result.stderr or result.stdout).strip()
        raise RuntimeError(output or "Command failed: " + " ".join(command))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Py NIC Manager persistent NAT rules.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add or replace a persistent NAT rule.")
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--source-cidr", required=True)
    add_parser.add_argument("--outbound-interface", default="")
    add_parser.add_argument("--disabled", action="store_true")

    delete_parser = subparsers.add_parser("delete", help="Delete a persistent NAT rule.")
    delete_parser.add_argument("--name", required=True)

    subparsers.add_parser("apply", help="Apply stored persistent NAT rules.")
    subparsers.add_parser("install", help="Install startup hooks for stored NAT rules.")

    args = parser.parse_args()
    if args.command == "add":
        add_rule(args.name, args.source_cidr, args.outbound_interface, not args.disabled)
    elif args.command == "delete":
        delete_rule(args.name)
    elif args.command == "apply":
        apply_rules()
    elif args.command == "install":
        install_startup_hook()


if __name__ == "__main__":
    main()
