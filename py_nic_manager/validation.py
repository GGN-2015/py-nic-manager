from __future__ import annotations

import ipaddress
import re


MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}$")


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def prefix_to_netmask(prefix_length: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask)


def netmask_to_prefix(netmask: str) -> int:
    return int(ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen)


def normalize_mac(mac: str, separator: str = "-") -> str:
    clean = mac.replace(":", "").replace("-", "").strip()
    if not clean:
        return ""
    if len(clean) != 12 or not MAC_RE.match(clean):
        raise ValueError("MAC address must contain 12 hexadecimal characters.")
    return separator.join(clean[index : index + 2].upper() for index in range(0, 12, 2))


def validate_ip(value: str, *, allow_empty: bool = False) -> str:
    text = value.strip()
    if not text and allow_empty:
        return ""
    ipaddress.ip_address(text)
    return text


def validate_network(value: str) -> str:
    text = value.strip()
    if text.lower() == "default":
        return text
    ipaddress.ip_network(text, strict=False)
    return text


def validate_prefix(value: str) -> int:
    try:
        prefix = int(value)
    except ValueError as exc:
        raise ValueError("Prefix length must be an integer.") from exc
    if prefix < 0 or prefix > 128:
        raise ValueError("Prefix length must be between 0 and 128.")
    return prefix

