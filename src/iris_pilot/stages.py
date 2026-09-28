"""Concrete pipeline stages and the pipeline definition.

Order: check_prerequisites -> promote_* -> refresh_*_view -> export_*_dossiers.
Each vertical is its own dependency chain: a BESS failure skips only the
downstream BESS stages, while the run as a whole is still reported FAILED.
"""

from __future__ import annotations

import json
import re
import shutil
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from iris_pilot.db import RUN_LOCK_KEY, redact_url, try_advisory_lock
from iris_pilot.exporting import (
    EXPORT_META,
    RENDERERS,
    dossier_dir,
    publish_directory,
    remove_leftovers,
    safe_filename,
)
from iris_pilot.freshness import sha256_file
from iris_pilot.migrate import discover_migrations, migration_problems
from iris_pilot.runner import Stage, StageFailure, StageOutcome, utc_now
from iris_pilot.verticals import VERTICALS, Vertical

if TYPE_CHECKING:
    from iris_pilot.pipeline import PipelineContext

MIN_SERVER_VERSION = 160000  # PostgreSQL 16
MIN_POSTGIS_VERSION = (3, 4)
CORE_SRID = 3035  # ETRS89 / LAEA Europe: equal-area, metres


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:2])


def _dataset_version(conn: psycopg.Connection, dataset: str, *, lock: str = "") -> int:
    row = conn.execute(f"SELECT version FROM ops.dataset_version WHERE dataset = %s {lock}", [dataset]).fetchone()
    if row is None:
        raise StageFailure(f"no ops.dataset_version row for {dataset!r}; are migrations applied?")
    return row[0]


# --------------------------------------------------------------------------- prerequisites


def check_prerequisites(ctx: PipelineContext) -> StageOutcome:
    """Collect every check before failing, so one run shows all problems at once."""
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    target = redact_url(ctx.settings.database_url)
    try:
        conn = ctx.connect()
    except psycopg.Error as exc:
        record("database_connection", False, f"{target}: {str(exc).strip()}")
        raise StageFailure(f"cannot connect to database {target}", details={"checks": checks}) from exc
    record("database_connection", True, target)

    locked = try_advisory_lock(conn, RUN_LOCK_KEY)
    record("single_run_lock", locked, "acquired" if locked else "another iris-pilot run is in progress")

    server = conn.info.server_version
    record("postgresql_version", server >= MIN_SERVER_VERSION, f"{server // 10000}.{server % 10000} (need >= 16)")

    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'").fetchone()
    postgis_ok = row is not None and _version_tuple(row[0]) >= MIN_POSTGIS_VERSION
    record("postgis_version", postgis_ok, f"{row[0]} (need >= 3.4)" if row else "postgis extension not installed")

    problems = migration_problems(conn, discover_migrations(ctx.settings.migrations_dir))
    record("migrations_applied", not problems, "; ".join(problems) or "all applied")

    if not problems:  # input checks only make sense once the schema exists
        for vertical in ctx.verticals:
            accepted = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE review_status = 'accepted'").format(vertical.staging_ident)
            ).fetchone()[0]
            record(f"input_{vertical.key}", accepted > 0, f"{accepted} accepted row(s) in {vertical.staging_name}")

    out = ctx.settings.output_dir
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / f".write-probe-{ctx.run_id}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("output_dir_writable", True, str(out))
    except OSError as exc:
        record("output_dir_writable", False, f"{out}: {exc}")

    failed = [c for c in checks if not c["ok"]]
    if failed:
        message = "; ".join(f"{c['check']}: {c['detail']}" for c in failed)
        raise StageFailure(f"{len(failed)} prerequisite check(s) failed: {message}", details={"checks": checks})
    return StageOutcome(counts={"checks_passed": len(checks)}, details={"checks": checks})


# --------------------------------------------------------------------------- promotion


def _upsert_sql(vertical: Vertical) -> sql.Composed:
    """Promote accepted, geometrically valid staging rows into core.

    ON CONFLICT ... WHERE row IS DISTINCT FROM ... only touches rows whose
    content changed, so an unchanged rerun reports 0 inserted / 0 updated.
    ``xmax = 0`` in RETURNING is true for freshly inserted rows.
    """
    cols = vertical.promoted_columns
    ident = sql.Identifier
    return sql.SQL(
        """
        WITH upserted AS (
            INSERT INTO {core} AS t (country_code, {id_col}, {cols}, geom)
            SELECT s.country_code, s.source_id, {s_cols}, ST_Transform(s.geom, {srid})
            FROM {staging} AS s
            WHERE s.review_status = 'accepted' AND s.geom IS NOT NULL AND ST_IsValid(s.geom)
            ON CONFLICT (country_code, {id_col}) DO UPDATE
               SET {assignments}, geom = EXCLUDED.geom, updated_at = now()
             WHERE ROW({t_cols}) IS DISTINCT FROM ROW({ex_cols})
                OR ST_AsEWKB(t.geom) IS DISTINCT FROM ST_AsEWKB(EXCLUDED.geom)
            RETURNING (xmax = 0) AS inserted
        )
        SELECT count(*) FILTER (WHERE inserted), count(*) FILTER (WHERE NOT inserted) FROM upserted
        """
    ).format(
        core=vertical.core_ident,
        staging=vertical.staging_ident,
        id_col=ident(vertical.id_column),
        srid=sql.Literal(CORE_SRID),
        cols=sql.SQL(", ").join(map(ident, cols)),
        s_cols=sql.SQL(", ").join(sql.SQL("s.{}").format(ident(c)) for c in cols),
        assignments=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=ident(c)) for c in cols),
        t_cols=sql.SQL(", ").join(sql.SQL("t.{}").format(ident(c)) for c in cols),
        ex_cols=sql.SQL(", ").join(sql.SQL("EXCLUDED.{}").format(ident(c)) for c in cols),
    )


def promote(vertical: Vertical, ctx: PipelineContext) -> StageOutcome:
    conn = ctx.conn
    with conn.transaction():
        accepted = conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE review_status = 'accepted'").format(vertical.staging_ident)
        ).fetchone()[0]
        rejected = conn.execute(
            sql.SQL(
                """
                SELECT country_code, source_id,
                       CASE WHEN geom IS NULL THEN 'missing geometry'
                            ELSE 'invalid geometry: ' || ST_IsValidReason(geom) END
                FROM {} WHERE review_status = 'accepted' AND (geom IS NULL OR NOT ST_IsValid(geom))
                ORDER BY country_code, source_id
                """
            ).format(vertical.staging_ident)
        ).fetchall()
        inserted, updated = conn.execute(_upsert_sql(vertical)).fetchone()
        if inserted or updated:
            version = conn.execute(
                "UPDATE ops.dataset_version SET version = version + 1, changed_at = now() "
                "WHERE dataset = %s RETURNING version",
                [vertical.dataset],
            ).fetchone()
            if version is None:
                raise StageFailure(f"no ops.dataset_version row for {vertical.dataset!r}")
            version = version[0]
        else:
            version = _dataset_version(conn, vertical.dataset)

    unchanged = accepted - len(rejected) - inserted - updated
    return StageOutcome(
        counts={
            "accepted": accepted,
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "rejected": len(rejected),
        },
        details={
            "dataset": vertical.dataset,
            "dataset_version": version,
            "version_bumped": bool(inserted or updated),
            "rejected_rows": [{"country_code": c, "source_id": s, "reason": r} for c, s, r in rejected],
        },
        warnings=[f"{c}/{s} accepted in staging but not promoted: {r}" for c, s, r in rejected],
    )


# --------------------------------------------------------------------------- view refresh


def refresh_view(vertical: Vertical, ctx: PipelineContext) -> StageOutcome:
    """Refresh the view and record its source version in the same transaction."""
    conn = ctx.conn
    with conn.transaction():
        version = _dataset_version(conn, vertical.dataset, lock="FOR SHARE")
        conn.execute(sql.SQL("REFRESH MATERIALIZED VIEW {}").format(vertical.view_ident))
        rows = conn.execute(
            sql.SQL(
                """
                SELECT country_code, count(*),
                       count(*) FILTER (WHERE screening_status = 'pass'),
                       count(*) FILTER (WHERE screening_status = 'review'),
                       count(*) FILTER (WHERE screening_status = 'insufficient_data')
                FROM {} GROUP BY country_code ORDER BY country_code
                """
            ).format(vertical.view_ident)
        ).fetchall()
        total = sum(r[1] for r in rows)
        conn.execute(
            """
            INSERT INTO ops.view_state (view_name, dataset, source_version, row_count, refreshed_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (view_name) DO UPDATE
               SET source_version = EXCLUDED.source_version,
                   row_count      = EXCLUDED.row_count,
                   refreshed_at   = EXCLUDED.refreshed_at
            """,
            [vertical.view_name, vertical.dataset, version, total],
        )
    return StageOutcome(
        counts={
            "rows": total,
            "pass": sum(r[2] for r in rows),
            "review": sum(r[3] for r in rows),
            "insufficient_data": sum(r[4] for r in rows),
        },
        details={
            "view": vertical.view_name,
            "source_version": version,
            "by_country": {r[0]: {"rows": r[1], "pass": r[2], "review": r[3], "insufficient_data": r[4]} for r in rows},
        },
    )


# --------------------------------------------------------------------------- export


def export_dossiers(vertical: Vertical, ctx: PipelineContext) -> StageOutcome:
    conn = ctx.conn
    with conn.transaction():
        version = _dataset_version(conn, vertical.dataset, lock="FOR SHARE")
        state = conn.execute(
            "SELECT source_version FROM ops.view_state WHERE view_name = %s", [vertical.view_name]
        ).fetchone()
        if state is None or state[0] != version:
            reflected = "never refreshed" if state is None else f"v{state[0]}"
            raise StageFailure(
                f"refusing to export from stale view {vertical.view_name}: view is {reflected}, data is v{version}"
            )
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL(
                    """
                    SELECT * FROM (
                        SELECT v.*, row_number() OVER (PARTITION BY v.country_code ORDER BY {order}) AS sample_rank
                        FROM {view} AS v
                    ) ranked
                    WHERE sample_rank <= %s
                    ORDER BY country_code, sample_rank
                    """
                ).format(order=sql.SQL(vertical.sample_order), view=vertical.view_ident),
                [ctx.settings.sample_size],
            ).fetchall()
    if not rows:
        raise StageFailure(f"{vertical.view_name} has no candidate rows to export")

    target = dossier_dir(ctx.settings, vertical)
    remove_leftovers(target)
    staged = target.with_name(f".{target.name}.tmp-{ctx.run_id}")
    staged.mkdir(parents=True)
    try:
        files = _write_dossiers(vertical, rows, version, staged)
        meta = {
            "vertical": vertical.key,
            "view": vertical.view_name,
            "dataset": vertical.dataset,
            "source_version": version,
            "sample_size_per_country": ctx.settings.sample_size,
            "run_id": ctx.run_id,
            "exported_at": utc_now(),
            "files": files,
        }
        (staged / EXPORT_META).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        publish_directory(staged, target)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)  # the previous export stays untouched
        raise

    return StageOutcome(
        counts={"dossiers": len(files), "countries": len({f["country_code"] for f in files})},
        details={"output_dir": str(target), "source_version": version, "files": [f["path"] for f in files]},
    )


def _write_dossiers(vertical: Vertical, rows: list[dict[str, Any]], version: int, staged: Path) -> list[dict[str, Any]]:
    render = RENDERERS[vertical.key]
    files = []
    for row in rows:
        record_id = row[vertical.id_column]
        relative = f"{row['country_code']}/{safe_filename(record_id)}.md"
        path = staged / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(render(row, version), encoding="utf-8", newline="\n")
        files.append({
            "path": relative,
            "country_code": row["country_code"],
            "id": record_id,
            "screening_status": row["screening_status"],
            "sha256": sha256_file(path),
        })
    return files


# --------------------------------------------------------------------------- pipeline definition


def build_pipeline(verticals: tuple[Vertical, ...] = VERTICALS) -> list[Stage]:
    stages = [
        Stage(
            "check_prerequisites",
            check_prerequisites,
            description="database, PostGIS, migrations, input data, output dir, single-run lock",
        )
    ]
    for v in verticals:
        stages.append(Stage(
            f"promote_{v.key}", partial(promote, v), depends_on=("check_prerequisites",),
            description=f"promote accepted {v.staging_name} rows into {'.'.join(v.core_table)}",
        ))
    for v in verticals:
        stages.append(Stage(
            f"refresh_{v.key}_view", partial(refresh_view, v), depends_on=(f"promote_{v.key}",),
            description=f"refresh {v.view_name} and record its source version",
        ))
    for v in verticals:
        stages.append(Stage(
            f"export_{v.key}_dossiers", partial(export_dossiers, v), depends_on=(f"refresh_{v.key}_view",),
            description=f"export sample {v.key} dossiers atomically",
        ))
    return stages


STAGE_NAMES: tuple[str, ...] = tuple(stage.name for stage in build_pipeline())
