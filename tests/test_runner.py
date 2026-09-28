"""Stage runner semantics, tested with fake stages (no database needed)."""

from __future__ import annotations

import json

import pytest

from iris_pilot.manifest import write_manifest
from iris_pilot.runner import (
    RunStatus,
    Stage,
    StageFailure,
    StageOutcome,
    StageRecord,
    StageStatus,
    derive_run_status,
    run_pipeline,
    validate_pipeline,
)


def ok(counts=None, warnings=None):
    return lambda ctx: StageOutcome(counts=counts or {"rows": 1}, warnings=warnings or [])


def boom(ctx):
    raise RuntimeError("database exploded")


def two_vertical_pipeline(**overrides):
    """Same shape as the real pipeline: prereq -> (a chain, b chain)."""
    fns = {name: ok() for name in ("prereq", "promote_a", "promote_b", "refresh_a", "refresh_b", "export_a", "export_b")}
    fns.update(overrides)
    return [
        Stage("prereq", fns["prereq"]),
        Stage("promote_a", fns["promote_a"], depends_on=("prereq",)),
        Stage("promote_b", fns["promote_b"], depends_on=("prereq",)),
        Stage("refresh_a", fns["refresh_a"], depends_on=("promote_a",)),
        Stage("refresh_b", fns["refresh_b"], depends_on=("promote_b",)),
        Stage("export_a", fns["export_a"], depends_on=("refresh_a",)),
        Stage("export_b", fns["export_b"], depends_on=("refresh_b",)),
    ]


def statuses(report):
    return {r.name: r.status for r in report.stages}


def test_all_stages_succeed():
    report = run_pipeline(two_vertical_pipeline(), context=None)

    assert report.status == RunStatus.SUCCEEDED
    assert report.exit_code == 0
    assert set(statuses(report).values()) == {StageStatus.SUCCEEDED}
    assert all(r.started_at and r.finished_at and r.duration_ms is not None for r in report.stages)
    assert report.stage("promote_a").counts == {"rows": 1}


def test_failed_critical_stage_fails_run_and_skips_only_its_dependents():
    report = run_pipeline(two_vertical_pipeline(refresh_a=boom), context=None)

    assert report.status == RunStatus.FAILED
    assert report.exit_code == 1
    s = statuses(report)
    assert s["refresh_a"] == StageStatus.FAILED
    assert s["export_a"] == StageStatus.SKIPPED
    # the independent vertical still produces its output
    assert s["refresh_b"] == s["export_b"] == StageStatus.SUCCEEDED

    failed = report.stage("refresh_a")
    assert failed.error["type"] == "RuntimeError"
    assert failed.error["message"] == "database exploded"
    assert "test_runner.py" in failed.error["location"]
    assert "refresh_a (failed)" in report.stage("export_a").skip_reason


def test_failed_prerequisite_skips_everything_and_keeps_details():
    def prereq(ctx):
        raise StageFailure("1 prerequisite check(s) failed", details={"checks": [{"check": "db", "ok": False}]})

    report = run_pipeline(two_vertical_pipeline(prereq=prereq), context=None)

    assert report.status == RunStatus.FAILED
    assert [r.status for r in report.stages[1:]] == [StageStatus.SKIPPED] * 6
    assert report.stage("prereq").details["checks"][0]["check"] == "db"


def test_non_critical_failure_is_degraded_never_success():
    stages = [Stage("main", ok()), Stage("optional", boom, critical=False)]
    report = run_pipeline(stages, context=None)

    assert report.status == RunStatus.DEGRADED
    assert report.exit_code != 0


@pytest.mark.parametrize(
    ("critical_status", "expected"),
    [
        (StageStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        (StageStatus.FAILED, RunStatus.FAILED),
        (StageStatus.SKIPPED, RunStatus.FAILED),
        (StageStatus.PENDING, RunStatus.FAILED),  # never ran => not proven => not success
    ],
)
def test_success_requires_every_critical_stage_to_have_succeeded(critical_status, expected):
    record = StageRecord(name="x", description="", critical=True, depends_on=[], status=critical_status)
    assert derive_run_status([record]) == expected


def test_empty_stage_list_is_not_success():
    assert derive_run_status([]) == RunStatus.FAILED


def test_warnings_do_not_fail_the_run_but_are_reported():
    report = run_pipeline([Stage("promote", ok(warnings=["DE/X not promoted: missing geometry"]))], context=None)

    assert report.status == RunStatus.SUCCEEDED
    assert report.summary()["warnings"] == 1


def test_fault_injection_fails_the_named_stage():
    report = run_pipeline(two_vertical_pipeline(), context=None, fail_stages={"promote_b"})

    assert report.stage("promote_b").status == StageStatus.FAILED
    assert report.stage("promote_b").error["type"] == "InjectedFailure"
    assert report.status == RunStatus.FAILED


def test_unknown_fault_injection_target_is_rejected():
    with pytest.raises(ValueError, match="unknown stage"):
        run_pipeline(two_vertical_pipeline(), context=None, fail_stages={"nope"})


def test_interrupt_still_writes_an_aborted_manifest(tmp_path):
    def interrupted(ctx):
        raise KeyboardInterrupt

    written = []
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(
            two_vertical_pipeline(promote_a=interrupted),
            context=None,
            on_finish=lambda report: written.append(write_manifest(report, tmp_path)),
        )

    manifest = json.loads(written[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "aborted"
    assert manifest["exit_code"] == 130
    by_name = {s["name"]: s for s in manifest["stages"]}
    assert by_name["promote_a"]["status"] == "failed"
    assert by_name["export_b"]["status"] == "skipped"
    assert by_name["export_b"]["skip_reason"] == "run aborted before this stage started"


def test_freshness_error_is_recorded_not_raised():
    def broken_freshness():
        raise ConnectionError("db gone")

    report = run_pipeline([Stage("a", ok())], context=None, collect_freshness=broken_freshness)

    assert report.status == RunStatus.SUCCEEDED
    assert "db gone" in report.freshness["error"]


@pytest.mark.parametrize(
    "stages",
    [
        [Stage("a", ok()), Stage("a", ok())],
        [Stage("a", ok(), depends_on=("b",)), Stage("b", ok())],
        [],
    ],
    ids=["duplicate-name", "forward-dependency", "empty"],
)
def test_invalid_pipelines_are_rejected(stages):
    with pytest.raises(ValueError):
        validate_pipeline(stages)


def test_manifest_is_valid_json_and_latest_matches(tmp_path):
    report = run_pipeline(two_vertical_pipeline(refresh_b=boom), context=None, metadata={"command": "test"})
    path = write_manifest(report, tmp_path)

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest == json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["status"] == "failed"
    assert manifest["summary"] == {"succeeded": 5, "failed": 1, "skipped": 1, "warnings": 0}
    assert manifest["metadata"] == {"command": "test"}
    assert not list(tmp_path.glob("*.tmp"))  # atomic write leaves no temp files
