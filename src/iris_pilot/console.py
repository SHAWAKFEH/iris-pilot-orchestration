"""Concise, ASCII-only console reporting (safe on any Windows code page)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iris_pilot.runner import RunReport, StageRecord, StageStatus

TAGS = {
    StageStatus.SUCCEEDED: " OK ",
    StageStatus.FAILED: "FAIL",
    StageStatus.SKIPPED: "SKIP",
    StageStatus.PENDING: " .. ",
}
INDENT = " " * 9


def out(line: str = "") -> None:
    print(line, flush=True)


def print_stage(record: StageRecord) -> None:
    duration = f"{record.duration_ms / 1000:6.2f}s" if record.duration_ms is not None else "     - "
    counts = " ".join(f"{key}={value}" for key, value in record.counts.items())
    out(f"[{TAGS[record.status]}] {record.name:<25} {duration}  {counts}".rstrip())
    for warning in record.warnings:
        out(f"{INDENT}warning: {warning}")
    if record.error:
        first_line = record.error["message"].splitlines()[0]
        out(f"{INDENT}error: {record.error['type']}: {first_line}")
        for check in record.details.get("checks", []):
            if not check["ok"]:
                out(f"{INDENT}  - {check['check']}: {check['detail']}")
        if "location" in record.error:
            out(f"{INDENT}at {record.error['location']}")
    if record.skip_reason:
        out(f"{INDENT}{record.skip_reason}")


def _state(info: dict[str, Any]) -> str:
    status = info["status"]
    return status if status == "fresh" else status.upper()


def print_freshness(freshness: dict[str, Any] | None) -> None:
    if not freshness:
        return
    if "error" in freshness:
        out(f"Freshness: {freshness['error']}")
        return
    out("Freshness:")
    for key, info in freshness["verticals"].items():
        out(f"  {key:<9} data v{info['dataset_version']:<3} view: {_state(info['view']):<16} export: {_state(info['export'])}")
        for part in ("view", "export"):
            if info[part]["status"] != "fresh":
                out(f"{INDENT}{part}: {info[part]['reason']}")


def print_summary(report: RunReport, manifest_path: Path | None) -> None:
    summary = report.summary()
    out()
    print_freshness(report.freshness)
    out()
    out(
        f"Run {report.run_id}: {str(report.status).upper()} (exit code {report.exit_code}) | "
        f"succeeded={summary['succeeded']} failed={summary['failed']} skipped={summary['skipped']} "
        f"warnings={summary['warnings']} | {(report.duration_ms or 0) / 1000:.2f}s"
    )
    if manifest_path:
        out(f"Manifest: {manifest_path}")
