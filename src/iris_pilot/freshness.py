"""Fresh / stale / missing assessment for candidate views and exports.

A view is stale when it was refreshed from an older dataset version than the
current one. An export is stale when it was produced from an older dataset
version, or when any exported file is missing or was modified afterwards.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from iris_pilot.exporting import EXPORT_META, dossier_dir
from iris_pilot.settings import Settings
from iris_pilot.verticals import Vertical

FRESH = "fresh"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assess_view(dataset_version: int, state: tuple[int, datetime, int] | None) -> dict[str, Any]:
    if state is None:
        return {"status": "never_refreshed", "reason": "view has not been refreshed by the pipeline yet"}
    source_version, refreshed_at, row_count = state
    info = {"source_version": source_version, "refreshed_at": _iso(refreshed_at), "rows": row_count}
    if source_version < dataset_version:
        reason = f"view reflects data v{source_version}, current data is v{dataset_version}"
        return {"status": "stale", "reason": reason, **info}
    return {"status": FRESH, **info}


def assess_export(export_dir: Path, dataset_version: int) -> dict[str, Any]:
    meta_path = export_dir / EXPORT_META
    if not meta_path.exists():
        return {"status": "missing", "reason": f"no export found in {export_dir}"}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        files = meta["files"]
        source_version = int(meta["source_version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "stale", "reason": f"export metadata unreadable: {exc}"}

    info = {
        "source_version": source_version,
        "exported_at": meta.get("exported_at"),
        "run_id": meta.get("run_id"),
        "dossiers": len(files),
    }
    damaged = []
    for entry in files:
        path = export_dir / entry["path"]
        if not path.is_file():
            damaged.append(f"{entry['path']} missing")
        elif sha256_file(path) != entry["sha256"]:
            damaged.append(f"{entry['path']} modified")
    if damaged:
        reason = f"{len(damaged)} file(s) changed since export: " + ", ".join(damaged[:3])
        return {"status": "stale", "reason": reason, **info}
    if source_version < dataset_version:
        reason = f"export reflects data v{source_version}, current data is v{dataset_version}"
        return {"status": "stale", "reason": reason, **info}
    return {"status": FRESH, **info}


def collect_freshness(conn: psycopg.Connection, settings: Settings, verticals: tuple[Vertical, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for vertical in verticals:
        row = conn.execute(
            "SELECT version, changed_at FROM ops.dataset_version WHERE dataset = %s", [vertical.dataset]
        ).fetchone()
        if row is None:
            raise RuntimeError(f"no ops.dataset_version row for {vertical.dataset!r}")
        version, changed_at = row
        state = conn.execute(
            "SELECT source_version, refreshed_at, row_count FROM ops.view_state WHERE view_name = %s",
            [vertical.view_name],
        ).fetchone()
        result[vertical.key] = {
            "dataset": vertical.dataset,
            "dataset_version": version,
            "data_changed_at": _iso(changed_at),
            "view": {"name": vertical.view_name, **assess_view(version, state)},
            "export": {"path": str(dossier_dir(settings, vertical)), **assess_export(dossier_dir(settings, vertical), version)},
        }
    all_fresh = all(v["view"]["status"] == FRESH and v["export"]["status"] == FRESH for v in result.values())
    return {"all_fresh": all_fresh, "verticals": result}
