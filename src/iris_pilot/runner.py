"""Generic, database-agnostic stage runner.

The runner executes an ordered list of stages, skips any stage whose
dependencies did not succeed, records timing, counts, warnings and errors per
stage, and derives one overall run status. It knows nothing about PostgreSQL,
which keeps its correctness rules small and unit-testable.
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class StageStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"  # every stage succeeded
    DEGRADED = "degraded"  # only non-critical stages did not succeed
    FAILED = "failed"  # at least one critical stage did not succeed
    ABORTED = "aborted"  # interrupted (e.g. Ctrl+C)


EXIT_CODES: dict[RunStatus, int] = {
    RunStatus.SUCCEEDED: 0,
    RunStatus.FAILED: 1,
    RunStatus.DEGRADED: 3,
    RunStatus.ABORTED: 130,
}


@dataclass
class StageOutcome:
    """What a stage function returns on success."""

    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class StageFailure(Exception):
    """Raised by a stage to fail with a clear message and structured details."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class InjectedFailure(StageFailure):
    """Raised in place of running a stage targeted by fault injection."""


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[Any], StageOutcome | None]
    depends_on: tuple[str, ...] = ()
    critical: bool = True
    description: str = ""


@dataclass
class StageRecord:
    name: str
    description: str
    critical: bool
    depends_on: list[str]
    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    skip_reason: str | None = None

    @classmethod
    def for_stage(cls, stage: Stage) -> StageRecord:
        return cls(
            name=stage.name,
            description=stage.description,
            critical=stage.critical,
            depends_on=list(stage.depends_on),
        )


@dataclass
class RunReport:
    run_id: str
    started_at: str
    stages: list[StageRecord]
    status: RunStatus | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    freshness: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status or RunStatus.FAILED]

    def stage(self, name: str) -> StageRecord:
        for record in self.stages:
            if record.name == name:
                return record
        raise KeyError(name)

    def summary(self) -> dict[str, int]:
        counts = {status.value: 0 for status in StageStatus if status is not StageStatus.PENDING}
        for record in self.stages:
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
        counts["warnings"] = sum(len(record.warnings) for record in self.stages)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "summary": self.summary(),
            "stages": [asdict(record) for record in self.stages],
            "freshness": self.freshness,
            "metadata": self.metadata,
        }


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def derive_run_status(stages: Sequence[StageRecord], *, aborted: bool = False) -> RunStatus:
    """Success has to be proven: every critical stage must have SUCCEEDED.

    A critical stage that failed, was skipped, or never ran makes the run
    FAILED, so a broken run can never be reported as a success by omission.
    """
    if aborted:
        return RunStatus.ABORTED
    if not stages or any(r.critical and r.status != StageStatus.SUCCEEDED for r in stages):
        return RunStatus.FAILED
    if any(r.status != StageStatus.SUCCEEDED for r in stages):
        return RunStatus.DEGRADED
    return RunStatus.SUCCEEDED


def validate_pipeline(stages: Sequence[Stage]) -> None:
    """Names must be unique and dependencies must point to earlier stages."""
    if not stages:
        raise ValueError("pipeline has no stages")
    seen: set[str] = set()
    for stage in stages:
        if stage.name in seen:
            raise ValueError(f"duplicate stage name {stage.name!r}")
        missing = [dep for dep in stage.depends_on if dep not in seen]
        if missing:
            raise ValueError(f"stage {stage.name!r} depends on {missing}, which must be defined earlier")
        seen.add(stage.name)


def describe_error(exc: BaseException) -> dict[str, Any]:
    error: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc).strip() or repr(exc)}
    if sqlstate := getattr(exc, "sqlstate", None):
        error["sqlstate"] = sqlstate
    if not isinstance(exc, StageFailure):
        # Point at the deepest frame of the stage code, not inside a third-party driver.
        frames = traceback.extract_tb(exc.__traceback__)
        ours = [f for f in frames if "site-packages" not in f.filename] or frames
        if ours:
            frame = ours[-1]
            error["location"] = f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
    return error


def run_pipeline(
    stages: Sequence[Stage],
    context: Any,
    *,
    run_id: str | None = None,
    fail_stages: Collection[str] = (),
    on_stage_end: Callable[[StageRecord], None] | None = None,
    collect_freshness: Callable[[], dict[str, Any]] | None = None,
    on_finish: Callable[[RunReport], None] | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunReport:
    """Run stages in order and always finish with a complete report.

    ``collect_freshness`` and ``on_finish`` (used to write the manifest) run in
    a ``finally`` block, so a report exists even if the run is interrupted.
    """
    validate_pipeline(stages)
    unknown = set(fail_stages) - {stage.name for stage in stages}
    if unknown:
        raise ValueError(f"unknown stage(s) for fault injection: {sorted(unknown)}")

    report = RunReport(
        run_id=run_id or new_run_id(),
        started_at=utc_now(),
        stages=[StageRecord.for_stage(stage) for stage in stages],
        metadata=dict(metadata or {}),
    )
    records = {record.name: record for record in report.stages}
    started = time.perf_counter()
    aborted = False
    try:
        for stage in stages:
            record = records[stage.name]
            blocked = [dep for dep in stage.depends_on if records[dep].status != StageStatus.SUCCEEDED]
            if blocked:
                record.status = StageStatus.SKIPPED
                record.skip_reason = "upstream stage did not succeed: " + ", ".join(
                    f"{dep} ({records[dep].status})" for dep in blocked
                )
            else:
                _execute(stage, record, context, inject_failure=stage.name in fail_stages)
            if on_stage_end is not None:
                on_stage_end(record)
    except BaseException:
        aborted = True
        raise
    finally:
        for record in report.stages:
            if record.status == StageStatus.PENDING:
                record.status = StageStatus.SKIPPED
                record.skip_reason = "run aborted before this stage started"
        if collect_freshness is not None:
            try:
                report.freshness = collect_freshness()
            except Exception as exc:  # freshness is diagnostic; never lose the manifest over it
                report.freshness = {"error": f"could not assess freshness: {type(exc).__name__}: {exc}"}
        report.status = derive_run_status(report.stages, aborted=aborted)
        report.finished_at = utc_now()
        report.duration_ms = round((time.perf_counter() - started) * 1000)
        if on_finish is not None:
            on_finish(report)
    return report


def _execute(stage: Stage, record: StageRecord, context: Any, *, inject_failure: bool) -> None:
    record.started_at = utc_now()
    started = time.perf_counter()
    try:
        if inject_failure:
            raise InjectedFailure(f"fault injected into stage {stage.name!r} (--fail-stage)")
        outcome = stage.run(context) or StageOutcome()
    except BaseException as exc:
        record.status = StageStatus.FAILED
        record.error = describe_error(exc)
        if isinstance(exc, StageFailure):
            record.details.update(exc.details)
        if not isinstance(exc, Exception):  # KeyboardInterrupt/SystemExit stop the whole run
            raise
    else:
        record.status = StageStatus.SUCCEEDED
        record.counts = dict(outcome.counts)
        record.details = dict(outcome.details)
        record.warnings = list(outcome.warnings)
    finally:
        record.finished_at = utc_now()
        record.duration_ms = round((time.perf_counter() - started) * 1000)
