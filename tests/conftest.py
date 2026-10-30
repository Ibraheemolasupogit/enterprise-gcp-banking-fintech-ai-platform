"""Keep inherited committed artifacts immutable throughout the test session."""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def preserve_inherited_artifacts():
    baseline = json.loads((ROOT / "docs/upgrade/baseline_artifacts.json").read_text())

    def verify():
        for artifact in baseline["artifacts"]:
            path = ROOT / artifact["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"], path

    verify()
    yield
    verify()
