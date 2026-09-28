"""Command line interface: iris-pilot {run,status,init,migrate,seed}."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import psycopg

from iris_pilot import console, db
from iris_pilot.freshness import collect_freshness
from iris_pilot.migrate import MigrationError, apply_migrations
from iris_pilot.pipeline import execute_run
from iris_pilot.seed import load_fixtures
from iris_pilot.settings import Settings
from iris_pilot.stages import STAGE_NAMES
from iris_pilot.verticals import VERTICALS


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    console.out(f"iris-pilot run | database {db.redact_url(settings.database_url)} | output {settings.output_dir}")
    if args.fail_stage:
        console.out(f"fault injection enabled for: {', '.join(args.fail_stage)}")
    console.out()
    try:
        result = execute_run(settings, fail_stages=args.fail_stage or (), on_stage_end=console.print_stage)
    except KeyboardInterrupt:
        console.out(f"\nInterrupted. Run recorded as ABORTED in {settings.runs_dir / 'latest.json'}")
        return 130
    console.print_summary(result.report, result.manifest_path)
    return result.report.exit_code


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    latest = settings.runs_dir / "latest.json"
    if latest.exists():
        last = json.loads(latest.read_text(encoding="utf-8"))
        console.out(f"Last run: {last['run_id']} {str(last['status']).upper()} (finished {last['finished_at']})")
        for stage in last["stages"]:
            if stage["status"] != "succeeded":
                console.out(f"  {stage['status'].upper():<9} {stage['name']}")
    else:
        console.out("Last run: none recorded")
    with db.connect(settings) as conn:
        freshness = collect_freshness(conn, settings, VERTICALS)
    console.print_freshness(freshness)
    return 0 if freshness["all_fresh"] else 1


def cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    with db.connect(settings) as conn:
        applied = apply_migrations(conn, settings.migrations_dir)
    console.out(f"Applied: {', '.join(applied)}" if applied else "Schema up to date.")
    return 0


def cmd_seed(settings: Settings, args: argparse.Namespace) -> int:
    with db.connect(settings) as conn:
        loaded = load_fixtures(conn, settings.fixtures_dir)
    console.out("Loaded fixtures into staging: " + ", ".join(f"{k}={v}" for k, v in loaded.items()))
    return 0


def cmd_init(settings: Settings, args: argparse.Namespace) -> int:
    return cmd_migrate(settings, args) or cmd_seed(settings, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iris-pilot", description="IRIS one-command pilot orchestration")
    parser.add_argument("--database-url", help="PostgreSQL URL (default: $IRIS_DATABASE_URL or local dev DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="promote -> refresh views -> export dossiers, then write the run manifest")
    run.add_argument("--output-dir", type=Path, help="dossier output directory (default: ./out)")
    run.add_argument("--runs-dir", type=Path, help="run manifest directory (default: ./runs)")
    run.add_argument("--sample-size", type=int, help="dossiers per vertical and country (default: 3)")
    run.add_argument(
        "--fail-stage", action="append", choices=STAGE_NAMES, metavar="STAGE",
        help="fault injection for demos/tests: fail this stage (repeatable). Choices: " + ", ".join(STAGE_NAMES),
    )
    run.set_defaults(handler=cmd_run)

    status = sub.add_parser("status", help="show last run and fresh/stale state (exit 1 if anything is stale)")
    status.add_argument("--output-dir", type=Path)
    status.add_argument("--runs-dir", type=Path)
    status.set_defaults(handler=cmd_status)

    sub.add_parser("migrate", help="apply pending SQL migrations").set_defaults(handler=cmd_migrate)
    sub.add_parser("seed", help="replace staging content with the committed fixtures").set_defaults(handler=cmd_seed)
    sub.add_parser("init", help="migrate + seed").set_defaults(handler=cmd_init)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env(
        database_url=args.database_url,
        output_dir=getattr(args, "output_dir", None),
        runs_dir=getattr(args, "runs_dir", None),
        sample_size=getattr(args, "sample_size", None),
    )
    try:
        return args.handler(settings, args)
    except (psycopg.Error, MigrationError, ValueError, OSError) as exc:
        console.out(f"error: {type(exc).__name__}: {str(exc).strip()}")
        return 1
