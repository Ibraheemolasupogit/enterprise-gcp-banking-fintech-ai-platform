"""Hashes and actual local execution provenance, including uncommitted source."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from financial_crime_ml.ingestion.load_data import REPO_ROOT


def utc_now() -> str:
    """Return actual wall-clock time rather than a synthetic audit timestamp."""
    return datetime.now(UTC).isoformat()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """Reject NaN/Infinity instead of emitting non-standard JSON evidence."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")


def execution_provenance() -> dict[str, Any]:
    """Read local Git state; fingerprint actual code, including newly added modules."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")

    source_paths = sorted(
        [
            *REPO_ROOT.glob("src/**/*.py"),
            *REPO_ROOT.glob("configs/*.yaml"),
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "requirements.txt",
        ]
    )
    hashes = {
        str(path.relative_to(REPO_ROOT)): sha256_bytes(path.read_bytes()) for path in source_paths
    }
    return {
        "git_revision": git("rev-parse", "HEAD"),
        "git_status_porcelain": git("status", "--porcelain=v1", "--untracked-files=all"),
        "source_sha256": hashes,
        "source_fingerprint_sha256": sha256_bytes(json.dumps(hashes, sort_keys=True).encode()),
        "versions": {
            "python": platform.python_version(),
            **{
                package: version(package)
                for package in ("numpy", "pandas", "scipy", "scikit-learn", "PyYAML")
            },
        },
    }
