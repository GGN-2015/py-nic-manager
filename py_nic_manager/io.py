from __future__ import annotations

import json
from pathlib import Path

from .models import CONFIG_SCHEMA_VERSION, NetworkSnapshot


def export_snapshot(snapshot: NetworkSnapshot, path: str | Path) -> None:
    target = Path(path)
    target.write_text(
        json.dumps(snapshot.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def import_snapshot(path: str | Path) -> NetworkSnapshot:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration file must contain a JSON object.")
    version = int(data.get("schema_version", 0))
    if version != CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported configuration schema {version}; "
            f"expected {CONFIG_SCHEMA_VERSION}."
        )
    return NetworkSnapshot.from_dict(data)

