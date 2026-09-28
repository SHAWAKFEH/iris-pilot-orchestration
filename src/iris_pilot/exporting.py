"""Dossier rendering and atomic publication of export directories."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from iris_pilot.settings import Settings
from iris_pilot.verticals import Vertical

EXPORT_META = "_export.json"

UNCERTAINTY_NOTICE = (
    "Preliminary prospecting material. Figures, eco-point estimates and site suitability are indicative "
    "and based on available source data and commercial screening assumptions. The 8 eco-points/m2 factor "
    "is the current commercial baseline, not certified compensation. Ownership, planning, grid capacity, "
    "environmental eligibility and transferability remain subject to project-specific verification. "
    "No permit, reservation or construction readiness is represented."
)

NOT_PROVIDED = "_not provided by source_"

Row = Mapping[str, Any]


def dossier_dir(settings: Settings, vertical: Vertical) -> Path:
    return settings.output_dir / "dossiers" / vertical.key


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _num(value: Any, fmt: str, unit: str = "") -> str:
    if value is None:
        return NOT_PROVIDED
    return f"{value:{fmt}}{' ' + unit if unit else ''}"


def _header(title: str, row: Row, record_id: str, dataset: str, version: int) -> list[str]:
    return [
        f"# {title}: {row['name']}",
        "",
        "| | |",
        "|---|---|",
        f"| Record | `{row['country_code']}/{record_id}` |",
        f"| Country | {row['country_code']} |",
        f"| Screening result | **{row['screening_status']}** |",
        f"| Data version | `{dataset}` v{version} |",
        "",
    ]


def _footer(row: Row) -> list[str]:
    return [
        "## Sources",
        "",
        "| Source | As of |",
        "|---|---|",
        f"| {row['source_name']} | {row['source_date']} |",
        "",
        "## Important notice",
        "",
        f"> {UNCERTAINTY_NOTICE}",
        "",
    ]


_GEOMETRY_NOTE = (
    "Area is measured on the stored geometry in EPSG:3035 (ETRS89-LAEA, equal-area, metres). "
    "The coordinate is a point guaranteed to lie inside the geometry, reprojected to EPSG:4326."
)


def render_bess(row: Row, version: int) -> str:
    lines = _header("BESS prospecting dossier", {**row, "name": row["site_name"]}, row["site_id"], "bess_site", version)
    lines += [
        "## 1. Site summary",
        "",
        "| Attribute | Value |",
        "|---|---|",
        f"| Planned capacity | {_num(row['capacity_mw'], ',.2f', 'MW')} |",
        f"| Site area | {_num(row['area_ha'], ',.2f', 'ha')} |",
        f"| Grid connection distance | {_num(row['grid_distance_m'], ',.0f', 'm')} |",
        f"| Representative point (WGS84) | lat {row['lat']:.6f}, lon {row['lon']:.6f} |",
        "",
        _GEOMETRY_NOTE,
        "",
        "## 2. Screening",
        "",
        f"**Result:** {row['screening_status']}  ",
        f"**Why:** {row['screening_notes']}",
        "",
        "Rules applied:",
        "",
        "- site area of at least 0.5 ha",
        "- grid connection distance of at most 2 km",
        "- if capacity or grid distance is missing the result is `insufficient_data`; missing values are never estimated",
        "",
    ]
    return "\n".join(lines + _footer(row))


def render_peatland(row: Row, version: int) -> str:
    lines = _header(
        "Peatland prospecting dossier", {**row, "name": row["parcel_name"]}, row["parcel_id"], "peatland_parcel", version
    )
    lines += [
        "## 1. Parcel summary",
        "",
        "| Attribute | Value |",
        "|---|---|",
        f"| Parcel area | {_num(row['area_m2'], ',.0f', 'm²')} ({_num(row['area_ha'], ',.2f', 'ha')}) |",
        f"| Mean peat depth | {_num(row['peat_depth_m'], ',.2f', 'm')} |",
        f"| Current land use | {row['land_use'] or NOT_PROVIDED} |",
        f"| Representative point (WGS84) | lat {row['lat']:.6f}, lon {row['lon']:.6f} |",
        "",
        _GEOMETRY_NOTE,
        "",
        "## 2. Indicative eco-points",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Commercial baseline factor | {row['eco_points_per_m2']} eco-points/m² |",
        f"| Indicative eco-points | {_num(row['eco_points_indicative'], ',.0f')} |",
        "",
        "Indicative only: area × commercial baseline factor. This is not certified compensation.",
        "",
        "## 3. Screening",
        "",
        f"**Result:** {row['screening_status']}  ",
        f"**Why:** {row['screening_notes']}",
        "",
        "Rules applied:",
        "",
        "- mean peat depth of at least 0.30 m",
        "- current land use is drained agriculture (grassland or arable), where rewetting creates uplift",
        "- if depth or land use is missing the result is `insufficient_data`; missing values are never estimated",
        "",
    ]
    return "\n".join(lines + _footer(row))


RENDERERS: dict[str, Callable[[Row, int], str]] = {"bess": render_bess, "peatland": render_peatland}


def remove_leftovers(target: Path) -> None:
    """Delete temp/backup directories left behind by an interrupted export."""
    if target.parent.exists():
        for leftover in target.parent.glob(f".{target.name}.*"):
            shutil.rmtree(leftover, ignore_errors=True)


def publish_directory(staged: Path, target: Path) -> None:
    """Swap a fully written directory into place.

    Readers see either the complete previous export or the complete new one,
    never a mix. If the swap fails the previous export is restored.
    """
    backup = target.with_name(f".{target.name}.old-{uuid.uuid4().hex[:8]}")
    had_previous = target.exists()
    if had_previous:
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except BaseException:
        if had_previous and not target.exists():
            os.replace(backup, target)
        raise
    if had_previous:
        shutil.rmtree(backup, ignore_errors=True)
