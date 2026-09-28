"""Runtime configuration: defaults, overridable by environment and CLI flags."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = "postgresql://iris:iris@localhost:5433/iris"


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    output_dir: Path = PROJECT_ROOT / "out"
    runs_dir: Path = PROJECT_ROOT / "runs"
    migrations_dir: Path = PROJECT_ROOT / "migrations"
    fixtures_dir: Path = PROJECT_ROOT / "fixtures"
    sample_size: int = 3  # dossiers exported per vertical and country
    connect_timeout_s: int = 5

    def __post_init__(self) -> None:
        if self.sample_size < 1:
            raise ValueError("sample_size must be at least 1")

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        """Build settings from IRIS_* environment variables; non-None overrides win."""
        values: dict[str, object] = {}
        if url := os.environ.get("IRIS_DATABASE_URL"):
            values["database_url"] = url
        if out := os.environ.get("IRIS_OUTPUT_DIR"):
            values["output_dir"] = Path(out)
        if runs := os.environ.get("IRIS_RUNS_DIR"):
            values["runs_dir"] = Path(runs)
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)  # type: ignore[arg-type]
