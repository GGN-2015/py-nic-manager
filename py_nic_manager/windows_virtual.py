from __future__ import annotations

import argparse
import json
import sys

from . import windows_tap, windows_wintun


def create_virtual_adapter(name: str, address: str = "") -> None:
    errors: list[str] = []
    try:
        windows_tap.create_virtual_adapter(name, address)
        return
    except Exception as exc:
        errors.append(f"TAP-Windows6 creation failed: {exc}")
    try:
        windows_wintun.create_virtual_adapter(name, address)
        print("Created Wintun fallback adapter. Windows ICS compatibility is not guaranteed.", file=sys.stderr)
        return
    except Exception as exc:
        errors.append(f"Wintun fallback creation failed: {exc}")
    raise RuntimeError("Unable to create a Windows virtual NIC. " + " | ".join(errors))


def delete_virtual_adapter(name: str) -> None:
    errors: list[str] = []
    deleted = False
    for module in (windows_tap, windows_wintun):
        try:
            module.delete_virtual_adapter(name)
            deleted = True
        except Exception as exc:
            errors.append(str(exc))
    if not deleted:
        raise RuntimeError("Unable to delete the Windows virtual NIC. " + " | ".join(errors))


def list_virtual_adapters() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for module in (windows_tap, windows_wintun):
        try:
            for item in module.list_virtual_adapters():
                name = str(item.get("name", ""))
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                items.append(item)
        except Exception:
            continue
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Py NIC Manager Windows virtual adapters.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create a Windows virtual adapter.")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--address", default="")
    delete_parser = subparsers.add_parser("delete", help="Delete a Windows virtual adapter.")
    delete_parser.add_argument("--name", required=True)
    subparsers.add_parser("list", help="List Py NIC Manager Windows virtual adapters.")
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            create_virtual_adapter(args.name, args.address)
            return 0
        if args.command == "delete":
            delete_virtual_adapter(args.name)
            return 0
        if args.command == "list":
            print(json.dumps(list_virtual_adapters(), indent=2))
            return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
