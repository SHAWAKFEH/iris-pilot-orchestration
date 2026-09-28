"""Load the committed fixture CSVs into staging.

Seeding replaces staging content with exactly the fixture rows in one
transaction, so it is deterministic and safe to repeat. Empty CSV cells become
NULL: a missing value stays missing and is never defaulted.
"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg
from psycopg import sql

from iris_pilot.verticals import VERTICALS, Vertical


def read_fixture(path: Path, vertical: Vertical) -> list[dict[str, str | None]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = tuple(reader.fieldnames or ())
        if set(header) != set(vertical.fixture_columns):
            raise ValueError(f"{path.name}: expected columns {list(vertical.fixture_columns)}, got {list(header)}")
        return [{key: (value.strip() or None) for key, value in row.items()} for row in reader]


def load_fixtures(
    conn: psycopg.Connection, fixtures_dir: Path, verticals: tuple[Vertical, ...] = VERTICALS
) -> dict[str, int]:
    loaded: dict[str, int] = {}
    with conn.transaction():
        for vertical in verticals:
            rows = read_fixture(fixtures_dir / f"{vertical.staging_table[1]}.csv", vertical)
            columns = [c for c in vertical.fixture_columns if c != "geom_wkt"]
            insert = sql.SQL("INSERT INTO {table} ({columns}, geom) VALUES ({values}, ST_Multi(ST_GeomFromText(%s, 4326)))").format(
                table=vertical.staging_ident,
                columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
                values=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            )
            conn.execute(sql.SQL("DELETE FROM {}").format(vertical.staging_ident))
            with conn.cursor() as cur:
                cur.executemany(insert, [[row[c] for c in columns] + [row["geom_wkt"]] for row in rows])
            loaded[vertical.key] = len(rows)
    return loaded
