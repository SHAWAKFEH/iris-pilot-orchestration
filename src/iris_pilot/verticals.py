"""The two pilot verticals, described as data so every stage is reusable.

Adding a vertical means adding migrations, a fixture file, a Vertical entry
and a dossier renderer; the stage code itself does not change.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql

# Rank used to pick sample dossiers: strongest screening result first.
_SCREENING_RANK = "CASE screening_status WHEN 'pass' THEN 0 WHEN 'review' THEN 1 ELSE 2 END"


@dataclass(frozen=True)
class Vertical:
    key: str
    title: str
    dataset: str
    staging_table: tuple[str, str]
    core_table: tuple[str, str]
    view: tuple[str, str]
    id_column: str
    attribute_columns: tuple[str, ...]
    sample_order: str  # trusted constant SQL, never user input

    @property
    def staging_ident(self) -> sql.Identifier:
        return sql.Identifier(*self.staging_table)

    @property
    def core_ident(self) -> sql.Identifier:
        return sql.Identifier(*self.core_table)

    @property
    def view_ident(self) -> sql.Identifier:
        return sql.Identifier(*self.view)

    @property
    def staging_name(self) -> str:
        return ".".join(self.staging_table)

    @property
    def view_name(self) -> str:
        return ".".join(self.view)

    @property
    def promoted_columns(self) -> tuple[str, ...]:
        """Business columns copied from staging to core (besides keys and geometry)."""
        return (*self.attribute_columns, "source_name", "source_date")

    @property
    def fixture_columns(self) -> tuple[str, ...]:
        return ("country_code", "source_id", *self.promoted_columns, "review_status", "geom_wkt")


BESS = Vertical(
    key="bess",
    title="Battery energy storage (BESS)",
    dataset="bess_site",
    staging_table=("staging", "bess_site_candidate"),
    core_table=("core", "bess_site"),
    view=("core", "mv_bess_candidates"),
    id_column="site_id",
    attribute_columns=("site_name", "capacity_mw", "grid_distance_m"),
    sample_order=f"{_SCREENING_RANK}, capacity_mw DESC NULLS LAST, site_id",
)

PEATLAND = Vertical(
    key="peatland",
    title="Peatland rewetting",
    dataset="peatland_parcel",
    staging_table=("staging", "peatland_parcel_candidate"),
    core_table=("core", "peatland_parcel"),
    view=("core", "mv_peatland_candidates"),
    id_column="parcel_id",
    attribute_columns=("parcel_name", "peat_depth_m", "land_use"),
    sample_order=f"{_SCREENING_RANK}, eco_points_indicative DESC, parcel_id",
)

VERTICALS: tuple[Vertical, ...] = (BESS, PEATLAND)
