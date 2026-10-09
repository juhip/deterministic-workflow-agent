"""Shared test fixtures. Every test works on a COPY of a scenario DB in a temp folder."""
import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from procurement.db import copy_scenario  # noqa: E402

# The provided data sits next to the project; override with PROCUREMENT_DATA_DIR if moved.
sys.path.insert(0, str(PROJECT / "evals"))
from datadir import as_provided, data_dir   # noqa: E402

DATA_DIR = data_dir()
SCENARIO_DIR = DATA_DIR / "scenarios"
ALL_SCENARIOS = sorted(p.name for p in SCENARIO_DIR.glob("*.sqlite")) if SCENARIO_DIR.exists() else []

# Fingerprints of the provided data when the test run starts: no test may change these files.
import hashlib   # noqa: E402
DATA_FINGERPRINTS = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(SCENARIO_DIR.glob("*.sqlite"))} if SCENARIO_DIR.exists() else {}


@pytest.fixture
def scenario_copy(tmp_path):
    """Call with a scenario file name; returns the path of a fresh disposable copy."""
    if not SCENARIO_DIR.exists():
        pytest.skip(f"scenario data not found at {SCENARIO_DIR}")

    def _copy(name: str) -> Path:
        return as_provided(copy_scenario(SCENARIO_DIR / name, tmp_path))
    return _copy


@pytest.fixture(autouse=True)
def _isolated_label_cache(tmp_path, monkeypatch):
    """CLI runs in tests must never write the project's real part-label cache."""
    monkeypatch.setenv("PROCUREMENT_CACHE_DIR", str(tmp_path / "label-cache"))
