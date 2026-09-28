"""End-to-end tests against a real PostgreSQL 16 + PostGIS 3.4 database.

Skipped unless IRIS_TEST_DATABASE_URL is set. Each test drops and rebuilds
the staging/core/ops schemas in that database, so point it at a disposable
database, never at shared data.
"""

from __future__ import annotations

import json
import os

import psycopg
import pytest

from iris_pilot import db
from iris_pilot.migrate import apply_migrations
from iris_pilot.pipeline import execute_run
from iris_pilot.runner import RunStatus, StageStatus
from iris_pilot.seed import load_fixtures
from iris_pilot.settings import Settings

TEST_DB_URL = os.environ.get("IRIS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="set IRIS_TEST_DATABASE_URL to run PostGIS integration tests"),
]


@pytest.fixture
def settings(tmp_path):
    settings = Settings(database_url=TEST_DB_URL, output_dir=tmp_path / "out", runs_dir=tmp_path / "runs")
    with db.connect(settings) as conn:
        conn.execute("DROP SCHEMA IF EXISTS staging, core, ops CASCADE")
        apply_migrations(conn, settings.migrations_dir)
        load_fixtures(conn, settings.fixtures_dir)
    return settings


@pytest.fixture
def conn(settings):
    with db.connect(settings) as connection:
        yield connection


def dossier_bytes(settings):
    root = settings.output_dir / "dossiers"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.md"))}


def test_one_command_produces_both_vertical_outputs(settings):
    result = execute_run(settings)
    report = result.report

    assert report.status == RunStatus.SUCCEEDED, json.dumps(report.to_dict(), default=str, indent=2)
    assert report.exit_code == 0

    # Accepted rows are promoted; missing/invalid geometry is refused and reported, never repaired.
    promote = report.stage("promote_bess")
    assert promote.counts == {"accepted": 6, "inserted": 4, "updated": 0, "unchanged": 0, "rejected": 2}
    reasons = {r["source_id"]: r["reason"] for r in promote.details["rejected_rows"]}
    assert reasons["BESS-006"] == "missing geometry"
    assert reasons["BESS-007"].startswith("invalid geometry: Self-intersection")
    assert len(promote.warnings) == 2
    assert report.stage("promote_peatland").counts["inserted"] == 4

    assert report.stage("refresh_bess_view").counts == {"rows": 4, "pass": 2, "review": 1, "insufficient_data": 1}

    files = dossier_bytes(settings)
    assert sorted(files) == [
        "bess/AT/BESS-001.md", "bess/DE/BESS-001.md", "bess/DE/BESS-002.md", "bess/DE/BESS-003.md",
        "peatland/AT/PEAT-001.md", "peatland/DE/PEAT-001.md", "peatland/DE/PEAT-002.md", "peatland/DE/PEAT-003.md",
    ]
    assert report.freshness["all_fresh"] is True

    manifest = json.loads((settings.runs_dir / "latest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == report.run_id
    assert manifest["status"] == "succeeded"


def test_identifiers_are_country_scoped(settings, conn):
    execute_run(settings)
    rows = conn.execute("SELECT country_code FROM core.bess_site WHERE site_id = 'BESS-001' ORDER BY 1").fetchall()
    assert rows == [("AT",), ("DE",)]
    nulls = conn.execute("SELECT count(*) FROM core.bess_site WHERE country_code IS NULL").fetchone()[0]
    assert nulls == 0


def test_geometry_is_stored_in_equal_area_crs(settings, conn):
    execute_run(settings)
    srid, area_ha = conn.execute(
        "SELECT ST_SRID(geom), round((ST_Area(geom) / 10000)::numeric, 2) FROM core.bess_site "
        "WHERE country_code = 'DE' AND site_id = 'BESS-001'"
    ).fetchone()
    assert srid == 3035
    assert float(area_ha) == pytest.approx(1.25, abs=0.02)  # ~112 m x ~111 m


def test_rerun_is_safe_and_idempotent(settings, conn):
    first = execute_run(settings).report
    files_before = dossier_bytes(settings)
    versions_before = conn.execute("SELECT dataset, version FROM ops.dataset_version ORDER BY 1").fetchall()

    second = execute_run(settings).report

    assert second.status == RunStatus.SUCCEEDED
    assert second.stage("promote_bess").counts == {"accepted": 6, "inserted": 0, "updated": 0, "unchanged": 4, "rejected": 2}
    assert second.stage("promote_bess").details["version_bumped"] is False
    assert conn.execute("SELECT dataset, version FROM ops.dataset_version ORDER BY 1").fetchall() == versions_before
    assert conn.execute("SELECT count(*) FROM core.bess_site").fetchone()[0] == 4
    assert dossier_bytes(settings) == files_before  # byte-identical outputs
    assert second.freshness["all_fresh"] is True
    assert first.run_id != second.run_id
    assert len(list(settings.runs_dir.glob("2*.json"))) == 2  # one manifest per run, none overwritten


def test_failed_critical_stage_is_reported_as_failure_and_stale_state_is_visible(settings, conn):
    assert execute_run(settings).report.status == RunStatus.SUCCEEDED

    # New accepted data arrives, then the BESS view refresh fails.
    conn.execute("UPDATE staging.bess_site_candidate SET capacity_mw = 25 WHERE country_code = 'DE' AND source_id = 'BESS-001'")
    result = execute_run(settings, fail_stages={"refresh_bess_view"})
    report = result.report

    assert report.status == RunStatus.FAILED
    assert report.exit_code == 1
    assert report.stage("promote_bess").counts["updated"] == 1
    assert report.stage("refresh_bess_view").status == StageStatus.FAILED
    assert report.stage("export_bess_dossiers").status == StageStatus.SKIPPED
    assert report.stage("export_peatland_dossiers").status == StageStatus.SUCCEEDED

    bess = report.freshness["verticals"]["bess"]
    assert report.freshness["all_fresh"] is False
    assert bess["view"]["status"] == "stale"
    assert bess["view"]["reason"] == "view reflects data v1, current data is v2"
    assert bess["export"]["status"] == "stale"
    assert report.freshness["verticals"]["peatland"]["export"]["status"] == "fresh"

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["summary"]["failed"] == 1

    # A clean rerun recovers and everything is fresh again.
    recovered = execute_run(settings).report
    assert recovered.status == RunStatus.SUCCEEDED
    assert recovered.freshness["all_fresh"] is True
    assert "| Planned capacity | 25.00 MW |" in (settings.output_dir / "dossiers/bess/DE/BESS-001.md").read_text(encoding="utf-8")


def test_real_database_error_is_captured_with_details(settings, conn):
    conn.execute("DROP MATERIALIZED VIEW core.mv_peatland_candidates")
    report = execute_run(settings).report

    failed = report.stage("refresh_peatland_view")
    assert report.status == RunStatus.FAILED
    assert failed.status == StageStatus.FAILED
    assert failed.error["type"] == "UndefinedTable"
    assert failed.error["sqlstate"] == "42P01"
    assert report.stage("export_bess_dossiers").status == StageStatus.SUCCEEDED


def test_missing_input_fails_prerequisites(settings, conn):
    conn.execute("UPDATE staging.peatland_parcel_candidate SET review_status = 'pending'")
    report = execute_run(settings).report

    prereq = report.stage("check_prerequisites")
    assert report.status == RunStatus.FAILED
    assert prereq.status == StageStatus.FAILED
    assert "input_peatland: 0 accepted row(s)" in prereq.error["message"]
    assert all(r.status == StageStatus.SKIPPED for r in report.stages[1:])


def test_concurrent_run_is_refused(settings):
    with db.connect(settings) as other:
        assert db.try_advisory_lock(other, db.RUN_LOCK_KEY)
        report = execute_run(settings).report

    assert report.status == RunStatus.FAILED
    assert "another iris-pilot run is in progress" in report.stage("check_prerequisites").error["message"]


def test_unreachable_database_is_a_visible_failure(tmp_path):
    settings = Settings(
        database_url="postgresql://iris:iris@127.0.0.1:1/iris", output_dir=tmp_path / "out",
        runs_dir=tmp_path / "runs", connect_timeout_s=2,
    )
    result = execute_run(settings)

    assert result.report.status == RunStatus.FAILED
    assert "cannot connect" in result.report.stage("check_prerequisites").error["message"]
    assert "could not assess freshness" in result.report.freshness["error"]
    assert result.manifest_path.exists()
    assert ":***@" in result.report.metadata["database"]  # password never written to the manifest
