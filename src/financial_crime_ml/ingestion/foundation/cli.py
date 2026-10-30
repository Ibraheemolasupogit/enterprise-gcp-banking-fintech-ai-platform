"""Explicit M2 snapshot adapter for the local foundation."""

import argparse
from pathlib import Path

from financial_crime_ml.ingestion.foundation.pipeline import ingest


def ingestion_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml ingest-financial-data")
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-time", required=True)
    parser.add_argument("--ingestion-time", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        result = ingest(**vars(args))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Ingestion rejected: {error}\n")
    print(f"Financial ingestion {result['manifest']['status']}: {result['run_dir']}")
    print(f"Reused: {result['reused']}")
    if result["manifest"]["status"] == "failed":
        parser.exit(1, f"{result['manifest']['error']}\n")
