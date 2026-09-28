# IRIS-CAND-22: One-Command Pilot Orchestration

One command promotes accepted staging data, refreshes the BESS and peatland
candidate views, exports sample dossiers and writes a run manifest. Every
stage is visible as succeeded, failed or skipped, and stale views and exports
are reported explicitly.

```text
iris-pilot run

[ OK ] check_prerequisites         0.07s  checks_passed=8
[ OK ] promote_bess                0.02s  accepted=6 inserted=0 updated=1 unchanged=3 rejected=2
         warning: DE/BESS-006 accepted in staging but not promoted: missing geometry
         warning: DE/BESS-007 accepted in staging but not promoted: invalid geometry: Self-intersection[10.2405 47.8705]
[ OK ] promote_peatland            0.00s  accepted=4 inserted=0 updated=0 unchanged=4 rejected=0
[FAIL] refresh_bess_view           0.00s
         error: InjectedFailure: fault injected into stage 'refresh_bess_view' (--fail-stage)
[ OK ] refresh_peatland_view       0.03s  rows=4 pass=1 review=2 insufficient_data=1
[SKIP] export_bess_dossiers           -
         upstream stage did not succeed: refresh_bess_view (failed)
[ OK ] export_peatland_dossiers    0.03s  dossiers=4 countries=2

Freshness:
  bess      data v2   view: STALE            export: STALE
         view: view reflects data v1, current data is v2
         export: export reflects data v1, current data is v2
  peatland  data v1   view: fresh            export: fresh

Run 20260928T113618Z-e49f36: FAILED (exit code 1) | succeeded=5 failed=1 skipped=1 warnings=2 | 0.17s
```

## Quick start

Requirements: Python 3.12+, and Docker (or any PostgreSQL 16+ with PostGIS 3.4+).

```bash
docker compose up -d --wait          # PostGIS 16-3.4 on localhost:5433 (user/password/db: iris)
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

iris-pilot init                      # apply migrations + load the committed fixtures into staging
iris-pilot run                       # THE one command
iris-pilot status                    # last run + fresh/stale state (exit 1 if anything is stale)
```

With `make`: `make demo` runs `db-up`, `init` and `pilot` in one go.

Another database: set `IRIS_DATABASE_URL=postgresql://user:pass@host:port/db` or pass `--database-url`.

### Try the failure path

```bash
iris-pilot run --fail-stage refresh_bess_view   # fault injection: exit code 1, BESS shown STALE
iris-pilot status                               # still STALE, exit code 1
iris-pilot run                                  # recovers, everything fresh, exit code 0
```

`--fail-stage` is a fault-injection switch for demos and tests. The
integration tests also cover real failures: a dropped view, missing input,
an unreachable database and a concurrent run.

### Tests

```bash
pytest                                                              # 24 unit tests, no database needed
IRIS_TEST_DATABASE_URL=postgresql://iris:iris@localhost:5433/iris_test pytest   # + 9 PostGIS tests
```

The integration tests **drop and recreate** the `staging`, `core` and `ops`
schemas in the target database, so use a disposable database
(`createdb -h localhost -p 5433 -U iris iris_test`).

## What happens in a run

```text
check_prerequisites ─┬─ promote_bess ───── refresh_bess_view ───── export_bess_dossiers
                     └─ promote_peatland ─ refresh_peatland_view ─ export_peatland_dossiers
                                    (always) freshness report + runs/<run_id>.json
```

| Stage | What it does |
|---|---|
| `check_prerequisites` | Connects, takes a single-run advisory lock, checks PostgreSQL >= 16, PostGIS >= 3.4, that every migration is applied with an unchanged checksum, that each vertical has accepted input, and that the output directory is writable. All checks are evaluated and reported together. |
| `promote_<vertical>` | One transaction: upserts accepted, geometrically valid staging rows into `core`, transforming EPSG:4326 to EPSG:3035. Rows with missing or invalid geometry are **refused and reported as warnings, never repaired**. Bumps the dataset version only if rows actually changed. |
| `refresh_<vertical>_view` | One transaction: `REFRESH MATERIALIZED VIEW` and records which dataset version the view now reflects (`ops.view_state`). |
| `export_<vertical>_dossiers` | Refuses to export from a stale view. Writes the top N candidates per country (default 3) as Markdown dossiers into a temp directory, then atomically swaps it into `out/dossiers/<vertical>/` together with `_export.json` (source version and a SHA-256 per file). |

Stages run in order. A stage whose dependency did not succeed is **skipped**
with the reason recorded. The two verticals are independent chains, so a BESS
failure still lets peatland produce its outputs, but the run is still `FAILED`.

## How each acceptance criterion is met

**1. One command produces both vertical outputs.** `iris-pilot run` (or `make pilot`)
produces `out/dossiers/bess/**` and `out/dossiers/peatland/**` plus `runs/<run_id>.json`.

**2. A failed critical stage cannot be reported as success.** Run status is derived in
one function (`runner.derive_run_status`) that requires *positive proof*: the run is
`succeeded` only if every critical stage has status `succeeded`. A critical stage
that failed, was skipped, or never ran makes the run `failed`. Failure is never
inferred from the absence of an error. This is visible in:

- the exit code: `0` succeeded, `1` failed, `3` degraded (only non-critical stages failed), `130` aborted;
- the manifest: `status`, and per stage `error.type`, `error.message`, `sqlstate`, `location` and `skip_reason`;
- the manifest is written in a `finally` block, so even Ctrl+C produces an `aborted` manifest.

**3. Stale view/export state is visible.** Promotion bumps `ops.dataset_version`.
A view is **stale** when `ops.view_state.source_version` is lower than the dataset
version. An export is **stale** when its `_export.json` reflects an older version, or
when any dossier is missing or was modified (SHA-256 mismatch). A never-built export
is **missing**. This report is printed after every run, stored in the manifest under
`freshness`, and available on demand via `iris-pilot status` (exit 1 if not all fresh).

**4. Rerun is safe.**

- Promotion upserts with `ON CONFLICT ... DO UPDATE ... WHERE row IS DISTINCT FROM ...`: an unchanged rerun reports `inserted=0 updated=0`, does not bump versions, and produces **byte-identical dossiers** (tested).
- Every database stage is a single transaction: it either fully happens or not at all.
- Exports are published with a directory swap. Readers see the old or the new export, never a mix. A failed swap restores the old export, and leftovers of a crashed export are removed on the next run.
- Manifests are written atomically (temp file + rename), one file per run; `latest.json` points to the newest.
- A PostgreSQL advisory lock prevents two concurrent runs; the second one fails its prerequisites with a clear message.
- Migrations and fixture seeding are idempotent as well.

## Data contracts and engineering constraints

| Constraint | Implementation |
|---|---|
| Canonical geometry column `geom` | Every table and view uses `geom`. Staging is `MultiPolygon, 4326`; core is `MultiPolygon, 3035` with `CHECK (ST_IsValid(geom))` and a GiST index. |
| Never invent missing geo attributes | Missing or invalid geometry is refused at promotion with the PostGIS reason (e.g. the self-intersection point). Missing attributes stay `NULL`, screen as `insufficient_data`, and show as "not provided by source" in dossiers. No `ST_MakeValid`, no defaults. |
| Non-null `country_code`, country-scoped ids and joins | `core.country_code` domain (`^[A-Z]{2}$`) + `NOT NULL` on every business table. Primary keys are `(country_code, id)`. The fixtures deliberately contain `DE/BESS-001` and `AT/BESS-001` as distinct sites (tested). |
| CRS | Explicit SRIDs in column types. Areas are computed in EPSG:3035 (equal-area, metres), so `ST_Area` returns true m². Dossier coordinates are `ST_PointOnSurface`, reprojected to WGS84. |
| Units | Encoded in column names and comments (`capacity_mw`, `grid_distance_m`, `peat_depth_m`, `area_ha`, `area_m2`). |
| Source date | `source_name` and `source_date` are `NOT NULL` from staging through to every dossier. |
| Uncertainty | Screening is rule-based and explained per record (`screening_notes`). Every dossier carries the project's reference uncertainty wording. Eco-points are labelled indicative, using the 8/m² commercial baseline. |
| Reproducible CLI and SQL migrations | Forward-only SQL migrations with checksums (edited migrations are detected), a CLI, and a Makefile. No notebooks. |
| No credentials or remote sources | Local synthetic fixtures only. Database URLs are logged with the password redacted. |

## Project layout

```text
migrations/            forward-only SQL (schemas, staging, core, ops state, candidate views)
fixtures/              deterministic synthetic fixture set (Allgäu region + one AT site per vertical)
src/iris_pilot/
  runner.py            generic stage runner: ordering, skipping, status derivation (no DB code)
  stages.py            prerequisites, promote, refresh, export + pipeline definition
  pipeline.py          wires stages, shared connection, freshness and manifest together
  freshness.py         fresh / stale / missing assessment
  exporting.py         dossier renderers + atomic directory publish
  manifest.py          atomic JSON manifest writing
  migrate.py, seed.py  schema migrations and fixture loading
  verticals.py         BESS and peatland described as data (tables, view, id column, ranking)
  cli.py, console.py   command line and concise output
tests/                 unit tests (runner, export, freshness) + PostGIS end-to-end tests
docs/sample-output/    manifests and dossiers produced by real runs, for reading without running
```

## Fixture set

Synthetic, fictitious sites around Bad Grönenbach (DE) plus one Austrian site
per vertical. It is designed to exercise every rule:

| Case | Rows |
|---|---|
| passes screening | DE/BESS-001, AT/BESS-001, DE/PEAT-001 |
| needs review (too small / too far / too shallow / not drained) | DE/BESS-002, DE/PEAT-002, AT/PEAT-001 |
| insufficient data (value missing, not imputed) | DE/BESS-003 (grid distance), DE/PEAT-003 (peat depth) |
| accepted but refused: missing geometry | DE/BESS-006 |
| accepted but refused: self-intersecting polygon | DE/BESS-007 |
| not promoted (pending / rejected review) | DE/BESS-004, DE/BESS-005, DE/PEAT-004 |

`iris-pilot seed` replaces staging content with exactly these rows in one transaction.

## Assumptions

- "Accepted data" means staging rows whose `review_status = 'accepted'`. The review workflow that sets this is upstream and out of scope.
- "Promote" is an upsert. Records that later become rejected in staging are **not** removed from core (see below).
- "Sample dossiers" means the top N candidates per country and vertical, ranked by screening result and then size (capacity or eco-points). Markdown stands in for the two-page layout.
- Screening thresholds (BESS: >= 0.5 ha and <= 2 km to the grid; peatland: >= 0.30 m peat on grassland/arable) are illustrative placeholders to be agreed with the business.
- All stages in this pilot are critical. The runner also supports non-critical stages (`degraded` status, exit code 3), which is covered by unit tests.

## Deliberate simplifications and how they would evolve

| Simplification | Production evolution |
|---|---|
| Custom ~250-line runner instead of an orchestrator | Keep stage functions as they are and wrap them as Dagster/Prefect/Airflow tasks when scheduling, retries and backfills are needed. |
| Full `REFRESH MATERIALIZED VIEW` (locks readers briefly) | `REFRESH ... CONCURRENTLY` (the unique indexes already exist) or incremental tables once data grows. |
| Additive promotion (no retraction) | Handle `accepted -> rejected` transitions with soft deletes (`retired_at`) and an audit trail of promotions. |
| Dataset version per dataset, not per country | Per-country versions so a DE-only change does not mark AT exports stale. |
| Markdown dossiers | Render the same data to a two-page PDF (HTML template + WeasyPrint) with a map snapshot. |
| Screening thresholds in SQL | Versioned rule table (`core.screening_rule`) so thresholds are data, auditable and per country. |
| Manifest files on local disk | Also persist to an `ops.pipeline_run` table and ship metrics and alerts to monitoring. |
| `--fail-stage` fault injection built into the CLI | Keep for chaos tests, but hide it behind a dev flag. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | succeeded: every stage succeeded |
| 1 | failed: a critical stage failed or was skipped (also: `status` found stale state, or a command error) |
| 3 | degraded: only non-critical stages failed |
| 130 | aborted: interrupted, manifest still written |
