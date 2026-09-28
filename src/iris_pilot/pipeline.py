"""Wires stages, the shared database connection, freshness and the manifest together."""

from __future__ import annotations

import platform
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from iris_pilot import __version__, db
from iris_pilot.freshness import collect_freshness
from iris_pilot.manifest import write_manifest
from iris_pilot.runner import RunReport, StageRecord, new_run_id, run_pipeline
from iris_pilot.settings import Settings
from iris_pilot.stages import build_pipeline
from iris_pilot.verticals import VERTICALS, Vertical


@dataclass
class PipelineContext:
    """State shared by stages during one run: settings and one DB connection.

    The connection holds the single-run advisory lock for the whole run; the
    lock is released when the connection closes.
    """

    settings: Settings
    run_id: str
    verticals: tuple[Vertical, ...] = VERTICALS
    _conn: psycopg.Connection | None = field(default=None, repr=False)

    def connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed or self._conn.broken:
            self.close()
            self._conn = db.connect(self.settings)
        return self._conn

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            raise RuntimeError("no database connection; check_prerequisites must run first")
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


@dataclass
class RunResult:
    report: RunReport
    manifest_path: Path | None


def execute_run(
    settings: Settings,
    *,
    fail_stages: Collection[str] = (),
    on_stage_end: Callable[[StageRecord], None] | None = None,
    verticals: tuple[Vertical, ...] = VERTICALS,
) -> RunResult:
    ctx = PipelineContext(settings=settings, run_id=new_run_id(), verticals=verticals)
    result = RunResult(report=None, manifest_path=None)  # type: ignore[arg-type]

    def finish(report: RunReport) -> None:
        result.report = report
        result.manifest_path = write_manifest(report, settings.runs_dir)

    try:
        run_pipeline(
            build_pipeline(verticals),
            ctx,
            run_id=ctx.run_id,
            fail_stages=fail_stages,
            on_stage_end=on_stage_end,
            collect_freshness=lambda: collect_freshness(ctx.connect(), settings, verticals),
            on_finish=finish,
            metadata={
                "command": "iris-pilot run",
                "iris_pilot_version": __version__,
                "python": platform.python_version(),
                "database": db.redact_url(settings.database_url),
                "output_dir": str(settings.output_dir),
                "sample_size_per_country": settings.sample_size,
                "fault_injection": sorted(fail_stages),
            },
        )
    finally:
        ctx.close()
    return result
