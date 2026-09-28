"""Run manifest: one JSON file per run plus runs/latest.json."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from iris_pilot.runner import RunReport

MANIFEST_VERSION = 1


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def render_manifest(report: RunReport) -> str:
    data = {"manifest_version": MANIFEST_VERSION, **report.to_dict()}
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


def write_manifest(report: RunReport, runs_dir: Path) -> Path:
    text = render_manifest(report)
    path = runs_dir / f"{report.run_id}.json"
    atomic_write_text(path, text)
    atomic_write_text(runs_dir / "latest.json", text)
    return path
