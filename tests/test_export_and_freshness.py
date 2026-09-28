"""Dossier rendering, atomic publication and stale detection (no database needed)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from iris_pilot.exporting import EXPORT_META, NOT_PROVIDED, UNCERTAINTY_NOTICE, publish_directory, remove_leftovers, render_bess
from iris_pilot.freshness import assess_export, assess_view, sha256_file

BESS_ROW = {
    "country_code": "DE",
    "site_id": "BESS-003",
    "site_name": "Kalden",
    "capacity_mw": Decimal("15.00"),
    "grid_distance_m": None,
    "area_ha": Decimal("1.99"),
    "lat": Decimal("47.890600"),
    "lon": Decimal("10.206000"),
    "screening_status": "insufficient_data",
    "screening_notes": "grid connection distance not provided by source",
    "source_name": "IRIS synthetic fixture v1",
    "source_date": date(2026, 6, 30),
}


def test_dossier_marks_missing_values_instead_of_inventing_them():
    text = render_bess(BESS_ROW, version=1)

    assert f"| Grid connection distance | {NOT_PROVIDED} |" in text
    assert "insufficient_data" in text
    assert UNCERTAINTY_NOTICE in text
    assert "`bess_site` v1" in text


def test_dossier_rendering_is_deterministic():
    assert render_bess(BESS_ROW, 1) == render_bess(dict(BESS_ROW), 1)


def test_publish_replaces_previous_export_completely(tmp_path):
    target = tmp_path / "bess"
    (target / "DE").mkdir(parents=True)
    (target / "DE" / "OLD.md").write_text("old")

    staged = tmp_path / ".bess.tmp-run2"
    (staged / "DE").mkdir(parents=True)
    (staged / "DE" / "NEW.md").write_text("new")
    publish_directory(staged, target)

    assert (target / "DE" / "NEW.md").read_text() == "new"
    assert not (target / "DE" / "OLD.md").exists()
    assert not staged.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["bess"]  # no backup left behind


def test_leftovers_from_an_interrupted_export_are_removed(tmp_path):
    target = tmp_path / "bess"
    (tmp_path / ".bess.tmp-crashed").mkdir()
    (tmp_path / ".bess.old-1234").mkdir()
    remove_leftovers(target)
    assert list(tmp_path.iterdir()) == []


def test_view_freshness():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    assert assess_view(3, None)["status"] == "never_refreshed"
    assert assess_view(3, (3, now, 10))["status"] == "fresh"
    stale = assess_view(3, (2, now, 10))
    assert stale["status"] == "stale"
    assert stale["reason"] == "view reflects data v2, current data is v3"


def _write_export(directory, version):
    (directory / "DE").mkdir(parents=True)
    dossier = directory / "DE" / "BESS-001.md"
    dossier.write_text("dossier", encoding="utf-8")
    meta = {"source_version": version, "files": [{"path": "DE/BESS-001.md", "sha256": sha256_file(dossier)}]}
    (directory / EXPORT_META).write_text(json.dumps(meta), encoding="utf-8")
    return dossier


def test_export_freshness(tmp_path):
    assert assess_export(tmp_path / "none", 1)["status"] == "missing"

    dossier = _write_export(tmp_path / "bess", version=2)
    assert assess_export(tmp_path / "bess", 2)["status"] == "fresh"
    assert assess_export(tmp_path / "bess", 3)["reason"] == "export reflects data v2, current data is v3"

    dossier.write_text("edited by hand", encoding="utf-8")
    tampered = assess_export(tmp_path / "bess", 2)
    assert tampered["status"] == "stale"
    assert "DE/BESS-001.md modified" in tampered["reason"]

    dossier.unlink()
    assert "DE/BESS-001.md missing" in assess_export(tmp_path / "bess", 2)["reason"]
