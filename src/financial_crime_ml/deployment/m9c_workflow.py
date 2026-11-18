"""M9C run: export the portable specification, generate SQL, prove parity, build the
monitoring reference. Offline only; reads verified M9A/M9B evidence and never writes to it.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from financial_crime_ml.deployment import model_spec as ms
from financial_crime_ml.deployment import scoring_sql as sq
from financial_crime_ml.evaluation import explainability as xp
from financial_crime_ml.evaluation.behavioural import FEATURE_SPECS, INPUT_COLUMNS, MODEL_FEATURES
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT
from financial_crime_ml.monitoring import reference_profile as rp

DEFAULT_RUNS_ROOT = REPO_ROOT / "outputs/runs"
SCHEMA_PATH = REPO_ROOT / "sql/scoring/v1/transaction_risk_scores.schema.json"
FLOAT_TOLERANCE = 1e-12
LOGIT_TOLERANCE = 1e-9
INT_FEATURES = [f for f in MODEL_FEATURES if f in FEATURE_SPECS and
                FEATURE_SPECS[f]["type"] == "INT64"] + ["transaction_hour",
                                                       "transaction_day_of_week",
                                                       "is_cross_border_transaction"]
FLOAT_FEATURES = [f for f in MODEL_FEATURES if f in FEATURE_SPECS and
                  FEATURE_SPECS[f]["type"] == "FLOAT64"] + ["transaction_amount_log"]
CATEGORICAL = ["currency", "transaction_type", "merchant_category", "origin_country",
               "destination_country", "channel"]


# --------------------------------------------------------------------------- SQLite engine
def run_sqlite(spec: dict[str, Any], transactions: pd.DataFrame) -> pd.DataFrame:
    """Execute the SQLite rendering over typed rows (NULLs preserved)."""
    con = sqlite3.connect(":memory:")
    columns = ", ".join(f"{c} {'INTEGER' if c == 'amount_minor' else 'TEXT'}"
                        for c in INPUT_COLUMNS)
    con.execute(f"CREATE TABLE {sq.SOURCE_TABLE} ({columns})")

    def cell(column: str, value: Any) -> Any:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        return int(value) if column == "amount_minor" else str(value)

    rows = [[cell(c, v) for c, v in zip(INPUT_COLUMNS, row, strict=True)]
            for row in transactions.loc[:, list(INPUT_COLUMNS)].itertuples(index=False)]
    marks = ", ".join("?" * len(INPUT_COLUMNS))
    con.executemany(f"INSERT INTO {sq.SOURCE_TABLE} VALUES ({marks})", rows)
    return pd.read_sql(ms_sql(spec), con)


def ms_sql(spec: dict[str, Any]) -> str:
    return sq.render_select(spec, sq.SQLITE)


# --------------------------------------------------------------------------- parity
def _max_abs(a: Any, b: Any) -> float:
    diff = np.abs(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))
    return float(diff.max()) if diff.size else 0.0


def parity(python: pd.DataFrame, sql: pd.DataFrame) -> dict[str, Any]:
    """Row-by-row comparison on transaction_id; raises nothing, reports everything."""
    report: dict[str, Any] = {"python_rows": int(len(python)), "sql_rows": int(len(sql))}
    p = python.set_index("transaction_id").sort_index()
    s = sql.set_index("transaction_id").sort_index()
    report["same_ids"] = p.index.equals(s.index)
    report["duplicate_ids"] = int(s.index.duplicated().sum())
    if not report["same_ids"] or report["duplicate_ids"]:
        report["ok"] = False
        return report
    report["input_valid_mismatches"] = int((p["input_valid"] != s["input_valid"]).sum())
    v = p["input_valid"] == 1
    p, s = p[v], s[v]
    report["valid_rows"] = int(v.sum())
    report["int_feature_mismatches"] = {f: int((p[f].astype(np.int64) !=
                                                s[f].astype(np.int64)).sum())
                                        for f in INT_FEATURES}
    report["float_feature_max_abs"] = {f: _max_abs(p[f], s[f]) for f in FLOAT_FEATURES}
    report["categorical_mismatches"] = {f: int((p[f].astype(str) != s[f].astype(str)).sum())
                                        for f in CATEGORICAL}
    report["contribution_max_abs"] = max(_max_abs(p[f"c_{f}"], s[f"c_{f}"])
                                         for f in MODEL_FEATURES)
    report["logit_max_abs"] = _max_abs(p["logit"], s["logit"])
    report["probability_max_abs"] = _max_abs(p["probability"], s["probability"])
    report["score_ppm_mismatches"] = int((p["score_ppm"].astype(np.int64) !=
                                          s["score_ppm"].astype(np.int64)).sum())
    report["alert_mismatches"] = int((p["alert"].astype(int) != s["alert"].astype(int)).sum())
    report["alerts"] = int(p["alert"].sum())
    codes = [f"reason_code_{i}" for i in range(1, ms.REASON_SLOTS + 1)]
    report["reason_code_mismatches"] = int((p[codes].fillna("").astype(str) !=
                                            s[codes].fillna("").astype(str)).any(axis=1).sum())
    report["ok"] = bool(
        report["input_valid_mismatches"] == 0
        and not any(report["int_feature_mismatches"].values())
        and max(report["float_feature_max_abs"].values(), default=0) <= FLOAT_TOLERANCE
        and not any(report["categorical_mismatches"].values())
        and report["contribution_max_abs"] <= FLOAT_TOLERANCE
        and report["logit_max_abs"] <= LOGIT_TOLERANCE
        and report["probability_max_abs"] <= FLOAT_TOLERANCE
        and report["score_ppm_mismatches"] == 0 and report["alert_mismatches"] == 0
        and report["reason_code_mismatches"] == 0)
    return report


def decision_margins(python: pd.DataFrame, threshold: float,
                     threshold_ppm: int | None = None) -> dict[str, Any]:
    """How far each decision is from flipping under last-bit floating-point differences."""
    v = python[python["input_valid"] == 1]
    p = v["probability"].to_numpy(dtype=float)
    scaled = p * ms.PPM
    half = np.abs(scaled - (np.floor(scaled) + 0.5))
    gaps = []
    for _, row in v[v["alert"] == 1].iterrows():
        terms = sorted((row[f"c_{f}"] for f in MODEL_FEATURES if row[f"c_{f}"] > 0),
                       reverse=True)[:ms.REASON_SLOTS + 1]
        gaps += [a - b for a, b in zip(terms, terms[1:], strict=False)]
    margins: dict[str, Any] = {
            "min_abs_probability_minus_m9a_threshold": float(np.min(np.abs(p - threshold))),
            "min_distance_to_ppm_rounding_boundary_ppm": float(np.min(half)),
            "min_gap_between_ranked_reason_contributions": float(min(gaps)) if gaps else None,
            "interpretation": "a cross-engine difference smaller than these margins cannot "
                              "change an alert, a score_ppm or a reason-code order"}
    if threshold_ppm is not None:  # spec v2: the alert flips only across this point
        margins["min_distance_to_alert_boundary_ppm"] = float(
            np.min(np.abs(scaled - (threshold_ppm - 0.5))))
    return margins


def m9a_champion_probabilities(m9a: dict[str, Any], features: pd.DataFrame) -> pd.Series:
    """The original M9A champion's output for every row, computed by scikit-learn's own
    LogisticRegression with the frozen parameters (no fitting, no labels). Accepted only if
    it is bit-identical to every stored M9A champion prediction (test and fold 3)."""
    from sklearn.linear_model import LogisticRegression

    params = m9a["params"]
    columns = [(features[n].to_numpy(dtype=float) - m) / s for n, m, s in
               zip(params["numeric_features"], params["scaler_mean"], params["scaler_scale"],
                   strict=True)]
    for name, categories in zip(params["categorical_features"], params["categories"],
                                strict=True):
        values = features[name].to_numpy()
        columns += [(values == c).astype(float) for c in categories]
    model = LogisticRegression()
    model.coef_ = np.asarray([params["coefficients"]], dtype=float)
    model.intercept_ = np.asarray([params["intercept"]], dtype=float)
    model.classes_ = np.array([0, 1])
    probability = pd.Series(model.predict_proba(np.column_stack(columns))[:, 1],
                            index=features["transaction_id"].to_numpy())
    stored = ms.read_predictions(m9a["contents"]["test_predictions.csv"])
    oof = ms.read_predictions(m9a["contents"]["validation_predictions.csv"])
    fold3 = oof[(oof["model"] == "logistic_regression") & (oof["fold"] == oof["fold"].max())]
    for frame in (stored, fold3):
        if not np.array_equal(probability.loc[frame["transaction_id"]].to_numpy(),
                              frame["score"].to_numpy()):
            raise ms.SpecError("reconstructed M9A champion is not bit-identical to M9A")
    return probability


def edge_case_transactions() -> pd.DataFrame:
    """Deterministic fixture: zero history, ties, bursts, unseen categories, NULLs."""
    base: dict[str, Any] = {
            "customer_id": "C2-E0", "account_id": "A2-E0", "beneficiary_id": "B2-E0",
            "device_id": "D2-E0", "amount_minor": 12345, "amount": "123.45",
            "currency": "GBP", "transaction_type": "card_payment",
            "merchant_category": "grocery", "origin_country": "GB",
            "destination_country": "GB", "channel": "web"}
    rows: list[tuple[str, str | None, dict[str, Any]]] = [
        ("T-E01", "2025-03-01T10:00:00Z", {}),                                  # zero history
        ("T-E02", "2025-03-01T10:01:00Z", {"amount_minor": 50000, "amount": "500.00"}),
        ("T-E03", "2025-03-01T10:02:00Z", {"amount_minor": 50000, "amount": "500.00"}),
        ("T-E04", "2025-03-01T10:02:00Z", {"amount_minor": 50000, "amount": "500.00",
                                           "beneficiary_id": "B2-E1"}),        # same instant
        ("T-E05", "2025-03-01T10:03:00Z", {"device_id": "D2-E9", "channel": "telephone",
                                           "merchant_category": "casino",
                                           "origin_country": "BR"}),           # unseen
        ("T-E06", "2025-03-01T10:04:00Z", {"currency": "SGD"}),                 # unseen ccy
        ("T-E07", "2025-03-01T10:05:00Z", {"account_id": "A2-E1", "currency": "JPY",
                                           "amount_minor": 30000, "amount": "30000"}),
        ("T-E08", "2025-05-01T10:05:00Z", {}),                                  # gap > 30 d
        ("T-E09", "2025-05-01T10:06:00Z", {"device_id": None}),                 # NULL
        ("T-E10", "2025-05-01T10:07:00Z", {"beneficiary_id": ""}),              # empty
        ("T-E11", "2025-05-01T10:08:00Z", {"currency": "XYZ"}),                 # unsupported
        ("T-E12", None, {}),                                                    # NULL time
        ("T-E13", "2025-05-01T10:09:00Z", {"amount_minor": None}),              # NULL amount
        ("T-E14", "2025-05-01T10:10:00Z", {"customer_id": "C2-E1", "account_id": "A2-E2",
                                           "amount_minor": 999999999,
                                           "amount": "9999999.99"}),            # extreme
    ]
    return pd.DataFrame([{"transaction_id": tid, "transaction_timestamp": ts,
                          **{**base, **over}} for tid, ts, over in rows])


# --------------------------------------------------------------------------- run
def run_m9c(m9a_run: Path | str, m9b_run: Path | str, run_id: str | None = None, *,
            runs_root: Path = DEFAULT_RUNS_ROOT, version: str = ms.SPEC_VERSION,
            approval_path: Path = ms.APPROVAL_PATH,
            previous_run: Path | None = None) -> dict[str, Any]:
    run_id = run_id or f"m9c-{uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run_id must be 1-80 letters, digits, underscores or hyphens.")
    m9a = xp.load_m9a(Path(m9a_run))
    m9a_sha = sha256_bytes((m9a["run_dir"] / "manifest.json").read_bytes())
    m9b = ms.load_m9b(Path(m9b_run), m9a_sha)
    run_dir = Path(runs_root).resolve() / run_id
    for protected in (m9a["run_dir"], m9b["run_dir"]):
        if run_dir == protected or run_dir.is_relative_to(protected):
            raise ValueError("M9C never writes into M9A or M9B runs")
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"schema_version": "2.0", "milestone": "M9C", "run_id": run_id,
                                "status": "running", "started_at": utc_now(),
                                "m9a_manifest_sha256": m9a_sha,
                                "m9b_manifest_sha256": m9b["manifest_sha256"],
                                "labels_read": False}
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        manifest.update(execution_provenance())
        approval = (ms.load_approval(approval_path, m9a, m9b) if version == ms.SPEC_V2
                    else None)
        spec = ms.build_spec(m9a, m9b, version=version, approval=approval)
        spec_bytes, sha = ms.canonical(spec), ms.spec_sha256(spec)
        if ms.canonical(ms.build_spec(m9a, m9b, version=version, approval=approval)) != \
                spec_bytes:
            raise ms.SpecError("specification is not deterministic")
        (run_dir / "model_spec.json").write_bytes(spec_bytes)
        manifest.update(model_spec_sha256=sha, spec_version=version)
        # every model term is identical across spec versions; only the decision rule moves
        v1 = ms.build_spec(m9a, m9b, version=ms.SPEC_V1)
        changed = sorted(k for k in set(spec) | set(v1) if spec.get(k) != v1.get(k))
        if not set(changed) <= {"spec_version", "decision", "provenance"}:
            raise ms.SpecError(f"spec versions differ beyond the decision rule: {changed}")
        lineage: dict[str, Any] = {"v1_sha256": ms.spec_sha256(v1),
                                   "sections_changed_from_v1": changed}
        if previous_run is not None:
            previous = (Path(previous_run) / "model_spec.json").read_bytes()
            lineage["historical_v1_reproduced_byte_for_byte"] = previous == ms.canonical(v1)
            if not lineage["historical_v1_reproduced_byte_for_byte"]:
                raise ms.SpecError("historical v1 specification is not reproduced")
        if approval is not None:
            (run_dir / "approval_record.json").write_bytes(Path(approval_path).read_bytes())
            lineage["approval_record_sha256"] = approval["sha256"]
        manifest["spec_lineage"] = lineage

        # the spec must be the frozen champion, exactly
        params = ms.params_from_spec(spec)
        for key in ("scaler_mean", "scaler_scale", "coefficients", "categories",
                    "numeric_features", "categorical_features", "transformed_features"):
            if params[key] != m9a["params"][key]:
                raise ms.SpecError(f"spec differs from the frozen champion: {key}")
        if params["intercept"] != m9a["params"]["intercept"]:
            raise ms.SpecError("spec differs from the frozen champion: intercept")

        inputs = m9a["manifest"]["inputs"]
        content = (Path(inputs["dataset_dir"]) / "transactions.csv").read_bytes()
        if sha256_bytes(content) != inputs["sha256"]["transactions.csv"]:
            raise ms.SpecError("dataset differs from the M9A run")
        transactions = pd.read_csv(io.BytesIO(content), dtype=str)
        python = ms.score_python(spec, transactions)

        # Python reference vs the original M9A outputs and the M9B explanations
        stored = ms.read_predictions(m9a["contents"]["test_predictions.csv"])
        ref = python.set_index("transaction_id")
        p_test = ref.loc[stored["transaction_id"]]
        oof = ms.read_predictions(m9a["contents"]["validation_predictions.csv"])
        fold3 = oof[(oof["model"] == "logistic_regression") & (oof["fold"] == oof["fold"].max())]
        m9b_contrib = pd.read_csv(io.BytesIO(m9b["contents"]["contributions_test.csv"]),
                                  dtype={"transaction_id": str}, float_precision="round_trip")
        m9b_codes = pd.read_csv(io.BytesIO(m9b["contents"]["reason_codes.csv"]),
                                dtype={"transaction_id": str})
        wide = m9b_codes.pivot(index="transaction_id", columns="rank", values="code")
        alerts = p_test[p_test["alert"] == 1]
        reference: dict[str, Any] = {
            "test_probability_max_abs_vs_m9a": float(np.max(np.abs(
                p_test["probability"].to_numpy() - stored["score"].to_numpy()))),
            "test_alert_mismatches_vs_m9a": int((p_test["alert"].to_numpy() !=
                                                 stored["alert"].to_numpy()).sum()),
            "validation_fold3_probability_max_abs_vs_m9a": float(np.max(np.abs(
                ref.loc[fold3["transaction_id"], "probability"].to_numpy() -
                fold3["score"].to_numpy()))),
            "contribution_max_abs_vs_m9b": float(max(np.max(np.abs(
                ref.loc[m9b_contrib["transaction_id"], f"c_{f}"].to_numpy() -
                m9b_contrib[f].to_numpy())) for f in MODEL_FEATURES)),
            "reason_code_mismatches_vs_m9b": int(sum(
                list(alerts.loc[t, [f"reason_code_{i}" for i in (1, 2, 3)]]) !=
                [wide.loc[t, i] for i in (1, 2, 3)] for t in alerts.index)),
            "alerts": int(len(alerts))}
        threshold = m9a["selection"]["threshold"]
        valid = python[python["input_valid"] == 1]
        original = m9a_champion_probabilities(m9a, valid)
        m9a_alert = (original.to_numpy() >= threshold).astype(int)
        reference["all_rows_alert_parity_vs_m9a_champion"] = {
            "rows": int(len(valid)), "m9a_alerts": int(m9a_alert.sum()),
            "spec_alerts": int(valid["alert"].sum()),
            "mismatches": int((valid["alert"].to_numpy() != m9a_alert).sum()),
            "v1_float_rule_mismatches": int(
                ((valid["probability"].to_numpy() >= threshold).astype(int) != m9a_alert).sum()),
            "m9a_reference": "scikit-learn LogisticRegression with the frozen parameters; "
                             "bit-identical to all stored M9A champion predictions"}
        reference["ok"] = (reference["test_probability_max_abs_vs_m9a"] <= FLOAT_TOLERANCE and
                           reference["validation_fold3_probability_max_abs_vs_m9a"] <=
                           FLOAT_TOLERANCE and reference["contribution_max_abs_vs_m9b"] == 0.0
                           and reference["test_alert_mismatches_vs_m9a"] == 0
                           and reference["reason_code_mismatches_vs_m9b"] == 0
                           and (not ms.uses_ppm_rule(spec) or reference[
                               "all_rows_alert_parity_vs_m9a_champion"]["mismatches"] == 0))
        if not reference["ok"]:
            raise ms.SpecError(f"Python reference differs from M9A/M9B: {reference}")

        # SQL (SQLite rendering) vs Python reference: full dataset and edge cases
        full = parity(python, run_sqlite(spec, transactions))
        edges = edge_case_transactions()
        edge = parity(ms.score_python(spec, edges), run_sqlite(spec, edges))
        report = {"tolerances": {"float": FLOAT_TOLERANCE, "logit": LOGIT_TOLERANCE,
                                 "integers_and_codes": "exact"},
                  "python_vs_m9a_m9b": reference, "sql_vs_python_full_dataset": full,
                  "sql_vs_python_edge_cases": edge,
                  "decision_margins_full_dataset": decision_margins(
                      python, m9a["selection"]["threshold"],
                      spec["decision"]["threshold_ppm"] if ms.uses_ppm_rule(spec) else None),
                  "googlesql_executed": False,
                  "note": "SQL parity is executed on the SQLite rendering of the same builder; "
                          "the GoogleSQL rendering is executed only in gated M9D."}
        write_json(run_dir / "parity_report.json", report)
        if not (full["ok"] and edge["ok"]):
            raise ms.SpecError("SQL parity failed; see parity_report.json")

        # SQL artifacts (GoogleSQL is the deployable form; placeholders stay unresolved)
        (run_dir / "scoring_select.googlesql.sql").write_text(
            sq.render_select(spec, sq.GOOGLESQL))
        (run_dir / "scoring_refresh.googlesql.sql").write_text(sq.render_insert(spec))
        (run_dir / "preflight.googlesql.sql").write_text(sq.render_preflight(spec))
        (run_dir / "scoring_select.sqlite.sql").write_text(sq.render_select(spec, sq.SQLITE))
        shutil.copyfile(SCHEMA_PATH, run_dir / "transaction_risk_scores.schema.json")

        # monitoring reference: the champion's own validation window (fold 3), no labels
        partitions = xp._csv(m9a["contents"]["partitions.csv"])
        from financial_crime_ml.evaluation.evaluation_v2 import parse_config

        window = f"validation_{parse_config(m9a['contents']['config.yaml']).folds}"
        ids = partitions.loc[partitions["partition"] == window, "transaction_id"]
        scored = ref.loc[ids].reset_index()
        profile = rp.build_profile(scored, window=f"M9A {window}", spec_sha256=sha)
        write_json(run_dir / "monitoring_reference.json", profile)

        manifest["artifacts_sha256"] = {p.name: sha256_bytes(p.read_bytes())
                                        for p in sorted(run_dir.iterdir()) if p != manifest_path}
        manifest.update(status="completed", completed_at=utc_now())
        write_json(manifest_path, manifest)
        return {"run_dir": run_dir, "manifest": manifest, "spec": spec, "parity": report,
                "profile": profile}
    except Exception as error:
        manifest.update(status="failed", failed_at=utc_now(),
                        error={"type": type(error).__name__, "message": str(error)[:2000]})
        write_json(manifest_path, manifest)
        raise


def export_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml export-fraud-model")
    parser.add_argument("--m9a-run", type=Path, required=True)
    parser.add_argument("--m9b-run", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--spec-version", choices=(ms.SPEC_V1, ms.SPEC_V2),
                        default=ms.SPEC_VERSION)
    parser.add_argument("--approval", type=Path, default=ms.APPROVAL_PATH)
    parser.add_argument("--previous-run", type=Path,
                        help="an earlier v1 export whose model_spec.json must be reproduced")
    args = parser.parse_args(argv)
    result = run_m9c(args.m9a_run, args.m9b_run, args.run_id, version=args.spec_version,
                     approval_path=args.approval, previous_run=args.previous_run)
    parity_report = result["parity"]
    print(f"M9C export completed: {result['run_dir']}")
    print(json.dumps({"model_spec_sha256": result["manifest"]["model_spec_sha256"],
                      "python_vs_m9a_m9b": parity_report["python_vs_m9a_m9b"],
                      "sql_full_ok": parity_report["sql_vs_python_full_dataset"]["ok"],
                      "sql_edge_ok": parity_report["sql_vs_python_edge_cases"]["ok"]},
                     indent=1))
