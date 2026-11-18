"""M9B run: explain the frozen M9A champion and generate governance artifacts.

Reads a completed M9A run (hash-verified), never writes into it, and writes a new run
directory with contributions, reason codes, permutation importance, rank stability, the
model card and the champion/challenger decision record.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from financial_crime_ml.evaluation import evaluation_v2 as ev
from financial_crime_ml.evaluation import explainability as xp
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.governance.model_card_v2 import (
    build_card,
    build_decision_record,
    render_card,
    render_decision_record,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT

DEFAULT_RUNS_ROOT = REPO_ROOT / "outputs/runs"
PERMUTATION_REPEATS = 10


def _dev_labels(m9a: dict, partitions: pd.DataFrame) -> pd.Series:
    """Development labels only; test-window labels are dropped before anything uses them."""
    inputs = m9a["manifest"]["inputs"]
    content = (Path(inputs["dataset_dir"]) / "outcomes.csv").read_bytes()
    if sha256_bytes(content) != inputs["sha256"]["outcomes.csv"]:
        raise xp.ExplainabilityError("dataset outcomes.csv differs from the M9A run")
    outcomes = pd.read_csv(io.BytesIO(content), dtype=str,
                           usecols=["transaction_id", "is_suspicious"])
    test_ids = set(partitions.loc[partitions["partition"] == "test", "transaction_id"])
    dev = outcomes[~outcomes["transaction_id"].isin(test_ids)]
    return dev.set_index("transaction_id")["is_suspicious"].astype(int)


def run_explainability(m9a_run: Path | str, run_id: str | None = None, *,
                       runs_root: Path = DEFAULT_RUNS_ROOT,
                       repeats: int = PERMUTATION_REPEATS) -> dict[str, Any]:
    run_id = run_id or f"explain-{uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run_id must be 1-80 letters, digits, underscores or hyphens.")
    m9a = xp.load_m9a(Path(m9a_run))
    run_dir = Path(runs_root).resolve() / run_id
    if run_dir == m9a["run_dir"] or run_dir.is_relative_to(m9a["run_dir"]):
        raise ValueError("M9B never writes into the M9A run")
    run_dir.mkdir(parents=True, exist_ok=False)
    m9a_manifest_sha = sha256_bytes((m9a["run_dir"] / "manifest.json").read_bytes())
    manifest: dict[str, Any] = {
        "schema_version": "2.0", "milestone": "M9B", "run_id": run_id, "status": "running",
        "started_at": utc_now(), "m9a_run_id": m9a["manifest"]["run_id"],
        "m9a_manifest_sha256": m9a_manifest_sha, "reason_version": xp.REASON_VERSION,
        "test_labels_read": False}
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        manifest.update(execution_provenance())
        features, partitions = xp.load_features(m9a)
        params, selection = m9a["params"], m9a["selection"]

        # 1-2. exact contributions, verified against the original model's stored outputs
        contrib = xp.contributions(features, params)
        test_pred = xp._csv(m9a["contents"]["test_predictions.csv"])
        oof = xp._csv(m9a["contents"]["validation_predictions.csv"])
        fold3 = oof[(oof["model"] == ev.CHAMPION) &
                    (oof["fold"] == oof["fold"].max())][["transaction_id", "score"]]
        reconstruction = {
            "test": xp.reconstruction_report(contrib, features, params, test_pred, "test"),
            "validation_fold3": xp.reconstruction_report(contrib, features, params, fold3,
                                                         "validation fold 3")}
        test_ids = set(test_pred["transaction_id"])
        contrib[contrib["transaction_id"].isin(test_ids)].to_csv(
            run_dir / "contributions_test.csv", index=False, float_format=ev.EXACT_FLOAT)

        # 3. reason codes for every alert (alert flags and scores read, never changed)
        alerts = test_pred[test_pred["alert"] == 1][["transaction_id", "score"]]
        if not (alerts["score"] >= selection["threshold"]).all():
            raise xp.ExplainabilityError("stored alerts disagree with the frozen threshold")
        codes = xp.reason_codes(contrib, features, alerts)
        codes.to_csv(run_dir / "reason_codes.csv", index=False, float_format=ev.EXACT_FLOAT)
        firsts = Counter(codes.loc[codes["rank"] == 1, "code"])
        reason_summary = {"alerts": int(len(alerts)), "codes_per_alert": 3,
                          "padded_codes": int((codes["code"] == xp.NO_FURTHER_REASON[0]).sum()),
                          "top_first_codes": dict(firsts.most_common(5)),
                          "mapping_version": xp.REASON_VERSION}

        # 4-5. permutation importance and rank stability on validation folds only
        replicas = xp.fold_replicas(m9a, features, partitions, _dev_labels(m9a, partitions))
        importance = xp.importance(replicas, repeats=repeats,
                                   seed=ev.parse_config(m9a["contents"]["config.yaml"]).seed)
        stability = xp.stability(importance)
        write_json(run_dir / "permutation_importance.json",
                   {"scoring": "average_precision (PR-AUC) drop", "repeats": repeats,
                    "data": "validation folds only", "replicas_verified": True,
                    "importance": importance})
        write_json(run_dir / "importance_stability.json", stability)
        write_json(run_dir / "reconstruction.json", reconstruction)
        write_json(run_dir / "reason_code_mapping.json",
                   {"version": xp.REASON_VERSION, "no_further_reason": xp.NO_FURTHER_REASON,
                    "codes": xp.REASONS})

        # 6-7. model card and decision record, generated from the evidence above
        m9b = {"m9a_manifest_sha256": m9a_manifest_sha, "reconstruction": reconstruction,
               "reason_summary": reason_summary, "stability": stability}
        card = build_card(m9a, m9b)
        write_json(run_dir / "model_card.json", card)
        (run_dir / "model_card.md").write_text(render_card(card))
        record = build_decision_record(m9a, m9b)
        write_json(run_dir / "decision_record.json", record)
        (run_dir / "decision_record.md").write_text(render_decision_record(record))

        # M9A evidence must be byte-identical afterwards
        xp.load_m9a(m9a["run_dir"])
        if sha256_bytes((m9a["run_dir"] / "manifest.json").read_bytes()) != m9a_manifest_sha:
            raise xp.ExplainabilityError("M9A manifest changed during M9B")
        manifest["artifacts_sha256"] = {p.name: sha256_bytes(p.read_bytes())
                                        for p in sorted(run_dir.iterdir()) if p != manifest_path}
        manifest.update(status="completed", completed_at=utc_now())
        write_json(manifest_path, manifest)
        return {"run_dir": run_dir, "manifest": manifest, "card": card, "record": record,
                "reconstruction": reconstruction, "stability": stability,
                "reason_summary": reason_summary}
    except Exception as error:
        manifest.update(status="failed", failed_at=utc_now(),
                        error={"type": type(error).__name__, "message": str(error)})
        write_json(manifest_path, manifest)
        raise


def explain_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml explain-fraud-v2")
    parser.add_argument("--m9a-run", type=Path, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    result = run_explainability(args.m9a_run, args.run_id)
    print(f"M9B explainability completed: {result['run_dir']}")
    print(json.dumps({"reconstruction": result["reconstruction"]["test"],
                      "reason_codes": result["reason_summary"],
                      "mean_spearman": {k: v["mean_spearman"]
                                        for k, v in result["stability"].items()},
                      "decision": result["record"]["decision"]}, indent=1))
