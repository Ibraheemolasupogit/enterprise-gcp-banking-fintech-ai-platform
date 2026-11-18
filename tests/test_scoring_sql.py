"""M9C: portable spec, generated SQL, Python-vs-SQL parity, ppm rounding, monitoring.

A small M9A + M9B evidence chain is created in temporary directories. The SQLite rendering is
executed; the GoogleSQL rendering is checked structurally (it runs only in gated M9D).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.workflow import run_generation
from financial_crime_ml.deployment import m9c_workflow as wf
from financial_crime_ml.deployment import model_spec as ms
from financial_crime_ml.deployment import scoring_sql as sq
from financial_crime_ml.deployment.marts_bundle import render as render_identifiers
from financial_crime_ml.evaluation import evaluation_v2 as ev
from financial_crime_ml.evaluation import explainability as xp
from financial_crime_ml.evaluation.behavioural import MODEL_FEATURES, NO_HISTORY_SECONDS
from financial_crime_ml.governance.m9b_workflow import run_explainability
from financial_crime_ml.monitoring import reference_profile as rp

FAST = ("seed: 42\ntest_fraction: 0.2\nfolds: 3\nembargo_seconds: 3600\n"
        "operating_capacity: 0.05\ncapacities: [0.01, 0.05, 0.10]\nbootstrap_resamples: 40\n"
        "confidence: 0.95\nece_bins: 10\nsegment_min_rows: 200\nsegment_min_class: 20\n"
        "challenger_ece_tolerance: 0.02\nhgb_max_iter: 60\n")


def _tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.name.encode() + item.read_bytes())
    return digest.hexdigest()


def write_approval(root: Path, **override) -> Path:
    """An Option B approval bound to this fixture chain (same shape as configs/approvals)."""
    real = json.loads(ms.APPROVAL_PATH.read_text())
    m9a = xp.load_m9a(root / "m9a")
    decision = (root / "m9b" / "decision_record.json").read_bytes()
    record = {**real, "decision_record_id": json.loads(decision)["id"],
              "decision_record_sha256": hashlib.sha256(decision).hexdigest(),
              "selection_sha256": m9a["selection"]["selection_sha256"],
              "deployment_policy_change": {**real["deployment_policy_change"],
                                           "threshold_ppm": ms.to_ppm(
                                               m9a["selection"]["threshold"])}}
    record.update(override)
    tag = hashlib.sha256(json.dumps(override, sort_keys=True).encode()).hexdigest()[:8]
    path = root / f"approval-{tag}.json"
    path.write_text(json.dumps(record, indent=1))
    return path


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    root = tmp_path_factory.mktemp("m9c")
    data = run_generation(replace(SyntheticConfig(), seed=5, transactions=6000, customers=120,
                                  accounts=180, beneficiaries=360, devices=240),
                          "data", runs_root=root)
    assert data["manifest"]["status"] == "completed"
    (root / "fast.yaml").write_text(FAST)
    ev.run_evaluation_v2(root / "data", root / "fast.yaml", "m9a", runs_root=root)
    run_explainability(root / "m9a", "m9b", runs_root=root, repeats=2)
    before = (_tree(root / "m9a"), _tree(root / "m9b"))
    approval = write_approval(root)
    result = wf.run_m9c(root / "m9a", root / "m9b", "m9c", runs_root=root / "out",
                        approval_path=approval)
    return root, result, before


@pytest.fixture(scope="module")
def spec(chain):
    return chain[1]["spec"]


# --------------------------------------------------------------------------- spec
def test_spec_is_deterministic_hashed_and_equal_to_the_frozen_champion(chain, spec):
    root, result, _ = chain
    content = (result["run_dir"] / "model_spec.json").read_bytes()
    assert content == ms.canonical(spec)
    assert result["manifest"]["model_spec_sha256"] == hashlib.sha256(content).hexdigest()
    m9a = xp.load_m9a(root / "m9a")
    params = ms.params_from_spec(json.loads(content))
    for key in ("coefficients", "scaler_mean", "scaler_scale", "categories", "intercept"):
        assert params[key] == m9a["params"][key], key
    assert spec["spec_version"] == ms.SPEC_V2
    assert spec["decision"]["m9a_threshold"] == m9a["selection"]["threshold"]
    assert spec["decision"]["threshold_ppm"] == ms.to_ppm(m9a["selection"]["threshold"])
    assert spec["provenance"]["approval"]["production_approval"] == "pending"
    assert [f["name"] for f in spec["reason_codes"]["features"]] == list(MODEL_FEATURES)
    m9b = ms.load_m9b(root / "m9b", result["manifest"]["m9a_manifest_sha256"])
    approval = ms.load_approval(write_approval(root), m9a, m9b)
    assert ms.canonical(ms.build_spec(m9a, m9b, approval=approval)) == content


def test_spec_refuses_tampered_or_mismatched_m9b(chain, tmp_path):
    root, result, _ = chain
    with pytest.raises(ms.SpecError, match="different M9A"):
        ms.load_m9b(root / "m9b", "0" * 64)
    import shutil

    shutil.copytree(root / "m9b", tmp_path / "m9b")
    mapping = tmp_path / "m9b" / "reason_code_mapping.json"
    mapping.write_text(mapping.read_text().replace("Round payment amount", "Round amount"))
    with pytest.raises(ms.SpecError, match="hash mismatch"):
        ms.load_m9b(tmp_path / "m9b", result["manifest"]["m9a_manifest_sha256"])


def test_evidence_is_unchanged_and_labels_are_never_read(chain):
    root, result, before = chain
    assert (_tree(root / "m9a"), _tree(root / "m9b")) == before
    assert result["manifest"]["labels_read"] is False
    stored = (root / "m9a" / "test_predictions.csv").read_bytes()
    assert "label" not in ms.read_predictions(stored).columns


# --------------------------------------------------------------------------- parity
def test_full_dataset_and_edge_case_parity(chain):
    report = chain[1]["parity"]
    reference = report["python_vs_m9a_m9b"]
    assert reference["ok"] and reference["contribution_max_abs_vs_m9b"] == 0.0
    for name in ("sql_vs_python_full_dataset", "sql_vs_python_edge_cases"):
        part = report[name]
        assert part["ok"], name
        assert part["score_ppm_mismatches"] == part["alert_mismatches"] == 0
        assert part["reason_code_mismatches"] == 0 and part["input_valid_mismatches"] == 0
        assert part["logit_max_abs"] <= 1e-9 and part["probability_max_abs"] <= 1e-12
    assert report["sql_vs_python_edge_cases"]["valid_rows"] == 9
    margins = report["decision_margins_full_dataset"]
    assert margins["min_abs_probability_minus_m9a_threshold"] >= 0
    assert margins["min_distance_to_alert_boundary_ppm"] > 1e-3  # far from the v2 boundary
    assert 0 <= margins["min_distance_to_ppm_rounding_boundary_ppm"] <= 0.5
    assert report["googlesql_executed"] is False


def test_edge_case_semantics(spec):
    edges = wf.edge_case_transactions()
    python = ms.score_python(spec, edges).set_index("transaction_id")
    sql = wf.run_sqlite(spec, edges).set_index("transaction_id")
    first = python.loc["T-E01"]
    assert (first["acct_has_prev"], first["acct_txn_count_24h"], first["acct_new_beneficiary"],
            first["acct_amount_log_ratio_30d"]) == (0, 0, 1, 0.0)
    assert first["acct_seconds_since_prev_log"] == math.log1p(NO_HISTORY_SECONDS)
    # same instant: T-E03 and T-E04 share 10:02:00 and never see each other
    for name, want in (("acct_txn_count_10m", 2), ("acct_same_amount_count_24h", 1),
                       ("acct_history_count_30d", 2)):
        assert python.loc["T-E03", name] == python.loc["T-E04", name] == want, name
        assert sql.loc["T-E03", name] == sql.loc["T-E04", name] == want, name
    # unseen categories (and an unseen but supported currency) contribute exactly 0
    for name in ("channel", "merchant_category", "origin_country"):
        assert python.loc["T-E05", f"c_{name}"] == sql.loc["T-E05", f"c_{name}"] == 0.0
    assert python.loc["T-E06", "c_currency"] == 0.0 and python.loc["T-E06", "input_valid"] == 1
    assert python.loc["T-E07", "is_round_amount"] == 1  # JPY exponent 0: 30000 % 100 == 0
    assert python.loc["T-E08", "acct_seconds_since_prev_log"] == \
        math.log1p(NO_HISTORY_SECONDS)  # gap > 30 days is capped
    for tid in ("T-E09", "T-E10", "T-E11", "T-E12", "T-E13"):  # NULL, empty, unsupported
        assert python.loc[tid, "input_valid"] == sql.loc[tid, "input_valid"] == 0
        assert pd.isna(sql.loc[tid, "score_ppm"]) and pd.isna(sql.loc[tid, "reason_code_1"])


def test_invalid_rows_never_change_valid_rows(spec):
    edges = wf.edge_case_transactions()
    valid_only = edges[~edges["transaction_id"].isin(
        ["T-E09", "T-E10", "T-E11", "T-E12", "T-E13"])]
    a = wf.run_sqlite(spec, edges).query("input_valid == 1").set_index("transaction_id")
    b = wf.run_sqlite(spec, valid_only).set_index("transaction_id")
    pd.testing.assert_frame_equal(a.sort_index(), b.sort_index(), check_dtype=False)


def test_reason_code_ties_are_ordered_identically_in_python_and_sql(spec):
    tied = copy.deepcopy(spec)
    for n in tied["model"]["numeric"]:
        if n["name"] in ("acct_new_beneficiary", "is_round_amount"):
            n.update(mean=0.0, scale=1.0, coefficient=30.0)  # identical, dominant terms
    tied["model"]["intercept"] = 2.0  # make the rows alert
    edges = wf.edge_case_transactions()
    report = wf.parity(ms.score_python(tied, edges), wf.run_sqlite(tied, edges))
    assert report["ok"], report
    # T-E04: first payment to beneficiary B2-E1 and a round 500.00 GBP amount
    row = ms.score_python(tied, edges).set_index("transaction_id").loc["T-E04"]
    order = [r["code"] for r in tied["reason_codes"]["features"]
             if r["name"] in ("acct_new_beneficiary", "is_round_amount")]
    assert [row["reason_code_1"], row["reason_code_2"]] == order  # feature order breaks tie


def test_parity_detects_differences(spec):
    edges = wf.edge_case_transactions()
    python = ms.score_python(spec, edges)
    sql = wf.run_sqlite(spec, edges)
    broken = sql.copy()
    broken.loc[broken["input_valid"] == 1, "score_ppm"] += 1
    assert not wf.parity(python, broken)["ok"]
    broken = sql.copy()
    broken.loc[broken["transaction_id"] == "T-E02", "acct_txn_count_10m"] += 1
    assert wf.parity(python, broken)["int_feature_mismatches"]["acct_txn_count_10m"] == 1
    assert not wf.parity(python, sql.iloc[1:])["ok"]


# --------------------------------------------------------------------------- ppm
@pytest.mark.parametrize("p,expected", [(0.0, 0), (1.0, 1_000_000), (0.5, 500_000),
                                        (0.25, 250_000), (2**-21, 0), (2**-20, 1)])
def test_ppm_rounding_known_values(p, expected):
    assert ms.to_ppm(p) == expected


@pytest.mark.parametrize("bad", [-1e-12, 1.0 + 1e-12, float("nan"), float("inf")])
def test_ppm_rejects_out_of_range(bad):
    with pytest.raises(ms.SpecError):
        ms.to_ppm(bad)


def test_ppm_rule_is_identical_in_python_and_sql_at_half_boundaries():
    con = sqlite3.connect(":memory:")
    values = []
    for k in (0, 1, 2, 499_999, 500_000, 999_998, 999_999):
        for offset in (-1, 0, 1):
            centre = (k + 0.5) / 1e6
            values.append(float(np.nextafter(centre, 2.0 if offset > 0 else -1.0))
                          if offset else centre)
    values = [v for v in values if 0.0 <= v <= 1.0]
    sql = [con.execute(f"SELECT CAST(FLOOR({sq.literal(v)} * 1000000.0 + 0.5) AS INTEGER)")
           .fetchone()[0] for v in values]
    assert sql == [ms.to_ppm(v) for v in values]
    assert all(0 <= x <= 1_000_000 for x in sql)


def test_literals_round_trip_exactly():
    for value in (0.1, -0.1, 1.0, 1e-300, -2.5e17, 3, 0.30000000000000004):
        text = sq.literal(value)
        assert float(text.strip("()")) == float(value)
        assert text.startswith("(") == (value < 0) and ("." in text or "e" in text)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            sq.literal(bad)


# --------------------------------------------------------------------------- GoogleSQL
def test_googlesql_is_deterministic_and_matches_the_feature_contract(chain, spec):
    run_dir = chain[1]["run_dir"]
    select = (run_dir / "scoring_select.googlesql.sql").read_text()
    assert select == sq.render_select(spec, sq.GOOGLESQL)
    assert ms.spec_sha256(spec) in select.splitlines()[0]
    frames = re.findall(r"RANGE BETWEEN (\w+) PRECEDING AND (\w+) PRECEDING", select)
    assert frames and all(upper == "1" for _, upper in frames)
    windows = {int(w) for w, _ in frames if w != "UNBOUNDED"}
    assert windows == {600_000_000, 3_600_000_000, 86_400_000_000, 2_592_000_000_000}
    assert "UNIX_MICROS(transaction_timestamp)" in select
    assert set(re.findall(r"`PROJECT\.DATASET\.(\w+)`", select)) == {"curated_transactions"}
    for forbidden in ("DELETE", "INSERT", "DROP", "CREATE", "MERGE", "UPDATE", "RAND(",
                      "CURRENT_"):
        assert forbidden not in select
    rendered = render_identifiers(select, "ebank-pilot-dev-261031-k7m4", "bankpilot_dev")
    assert "PROJECT" not in rendered and "`ebank-pilot-dev-261031-k7m4.bankpilot_dev." \
        "curated_transactions`" in rendered


def test_refresh_and_preflight_scripts(chain, spec):
    run_dir = chain[1]["run_dir"]
    refresh = (run_dir / "scoring_refresh.googlesql.sql").read_text()
    lines = [x for x in refresh.splitlines() if not x.startswith("--")]
    assert lines[0] == "BEGIN TRANSACTION;" and lines[-1] == "COMMIT TRANSACTION;"
    assert refresh.count("DELETE FROM `PROJECT.DATASET.transaction_risk_scores` WHERE TRUE") == 1
    assert "INSERT INTO `PROJECT.DATASET.transaction_risk_scores` (" + \
        ", ".join(sq.OUTPUT_COLUMNS) + ")" in refresh
    schema = json.loads((run_dir / "transaction_risk_scores.schema.json").read_text())
    assert [f["name"] for f in schema["fields"]] == list(sq.OUTPUT_COLUMNS)
    preflight = (run_dir / "preflight.googlesql.sql").read_text()
    for check in ("duplicate_ids", "invalid_rows", "sub_second_timestamps"):
        assert check in preflight


def test_sqlite_and_googlesql_differ_only_in_dialect_hooks(spec):
    google = sq.render_select(spec, sq.GOOGLESQL)
    lite = sq.render_select(spec, sq.SQLITE)
    for text in (google, lite):
        assert text.count("PARTITION BY") == google.count("PARTITION BY")
        assert text.count(" THEN ") == google.count(" THEN ")
    assert google.count("OVER (") == lite.count("OVER (")


# --------------------------------------------------------------------------- monitoring
def test_monitoring_profile_is_label_free_and_detects_shift(chain, spec):
    profile = chain[1]["profile"]
    assert profile["labels_used"] is False and profile["rows"] > 0
    assert set(profile["numeric"]) | set(profile["categorical"]) == set(MODEL_FEATURES)
    assert profile["model_spec_sha256"] == ms.spec_sha256(spec)
    assert abs(sum(profile["score_ppm"]["proportions"]) - 1) < 1e-12
    root = chain[0]
    transactions = pd.read_csv(root / "data" / "transactions.csv", dtype=str)
    scored = ms.score_python(spec, transactions).query("input_valid == 1")
    partitions = pd.read_csv(root / "m9a" / "partitions.csv", dtype=str)
    window_ids = partitions.loc[partitions["partition"] == "validation_3", "transaction_id"]
    reference_rows = scored.set_index("transaction_id").loc[window_ids].reset_index()
    identity = rp.compare(profile, reference_rows)
    assert all(v["psi"] == 0.0 for k, v in identity.items() if k != "alert_rate")
    assert identity["alert_rate"]["current"] == profile["alert_rate"]
    shifted = reference_rows.assign(score_ppm=reference_rows["score_ppm"] * 0 + 900_000,
                                    channel="telephone")
    result = rp.compare(profile, shifted)
    assert result["score_ppm"]["band"] == "significant"
    assert result["channel"]["band"] == "significant"


def test_psi_identity_and_bins():
    assert rp.psi([0.2, 0.3, 0.5], [0.2, 0.3, 0.5]) == 0.0
    assert rp.psi([0.5, 0.5, 0.0], [0.5, 0.5, 0.0]) == 0.0
    assert rp.numeric_proportions(np.array([1.0, 2.0, 3.0]), [1.0, 2.0]) == \
        pytest.approx([1 / 3, 1 / 3, 1 / 3])
    assert rp.categorical_proportions(pd.Series(["a", "b", "z"]), ["a", "b"])["__other__"] == \
        pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        rp.psi([1.0], [0.5, 0.5])


# --------------------------------------------------------------------------- spec v2 (Option B)
def test_v2_changes_only_the_decision_rule_and_coefficients_are_unchanged(chain, spec):
    root = chain[0]
    m9a = xp.load_m9a(root / "m9a")
    m9b = ms.load_m9b(root / "m9b", chain[1]["manifest"]["m9a_manifest_sha256"])
    v1 = ms.build_spec(m9a, m9b, version=ms.SPEC_V1)
    for section in ("feature_contract", "input_validity", "model", "reason_codes"):
        assert spec[section] == v1[section], section
    assert v1["decision"]["threshold"] == m9a["selection"]["threshold"]
    assert ms.spec_sha256(v1) != ms.spec_sha256(spec)
    assert chain[1]["manifest"]["spec_lineage"]["sections_changed_from_v1"] == [
        "decision", "provenance", "spec_version"]


def test_v2_requires_a_matching_limited_approval(chain):
    root = chain[0]
    m9a = xp.load_m9a(root / "m9a")
    m9b = ms.load_m9b(root / "m9b", chain[1]["manifest"]["m9a_manifest_sha256"])
    with pytest.raises(ms.SpecError, match="requires the approved"):
        ms.build_spec(m9a, m9b, version=ms.SPEC_V2)
    real = json.loads(ms.APPROVAL_PATH.read_text())
    for override in ({"production_approval": "approved"}, {"status": "approved"},
                     {"decision_record_sha256": "0" * 64}, {"decision": "hist_gradient_boosting"},
                     {"deployment_policy_change": {**real["deployment_policy_change"],
                                                   "threshold_ppm": 463_906}}):
        with pytest.raises(ms.SpecError, match="approval record"):
            ms.load_approval(write_approval(root, **override), m9a, m9b)


def test_all_rows_alert_parity_against_the_m9a_champion(chain):
    parity = chain[1]["parity"]["python_vs_m9a_m9b"]["all_rows_alert_parity_vs_m9a_champion"]
    assert parity["rows"] == 6000 and parity["mismatches"] == 0
    assert parity["spec_alerts"] == parity["m9a_alerts"]


def test_m9a_reference_must_be_bit_identical(chain, tmp_path):
    import shutil

    root = chain[0]
    m9a = xp.load_m9a(root / "m9a")
    features = ms.score_python(chain[1]["spec"], pd.read_csv(
        root / "data" / "transactions.csv", dtype=str)).query("input_valid == 1")
    wf.m9a_champion_probabilities(m9a, features)  # accepted as is
    tampered = dict(m9a, params={**m9a["params"],
                                 "intercept": m9a["params"]["intercept"] + 1e-15})
    with pytest.raises(ms.SpecError, match="bit-identical"):
        wf.m9a_champion_probabilities(tampered, features)
    shutil.rmtree(tmp_path)


def _boundary_probabilities(threshold_ppm: int) -> list[float]:
    centre = (threshold_ppm - 0.5) / 1e6
    values = [centre, (threshold_ppm - 1) / 1e6, threshold_ppm / 1e6, (threshold_ppm + 1) / 1e6]
    for step in (1, 2, 3):
        up = down = centre
        for _ in range(step):
            up, down = float(np.nextafter(up, 1.0)), float(np.nextafter(down, 0.0))
        values += [up, down]
    return values


def test_integer_alert_rule_boundaries_in_python_and_sql(spec):
    threshold_ppm = spec["decision"]["threshold_ppm"]
    con = sqlite3.connect(":memory:")
    for p in _boundary_probabilities(threshold_ppm):
        ppm = ms.to_ppm(p)
        python_alert = int(ms.alert_flags(spec, np.array([p]), np.array([ppm]))[0])
        sql_alert = con.execute(
            f"SELECT CASE WHEN CAST(FLOOR({sq.literal(p)} * 1000000.0 + 0.5) AS INTEGER) >= "
            f"{threshold_ppm} THEN 1 ELSE 0 END").fetchone()[0]
        assert python_alert == sql_alert == int(ppm >= threshold_ppm), p
    assert ms.alert_flags(spec, np.array([0.0, 1.0]), np.array([0, 1_000_000])).tolist() == [0, 1]


def test_threshold_row_alerts_under_v2_even_one_ulp_below(spec):
    threshold = spec["decision"]["m9a_threshold"]
    below = float(np.nextafter(threshold, 0.0))
    probabilities = np.array([threshold, below])
    ppm = np.array([ms.to_ppm(threshold), ms.to_ppm(below)])
    assert ms.alert_flags(spec, probabilities, ppm).tolist() == [1, 1]
    v1_like = {"spec_version": ms.SPEC_V1, "decision": {"threshold": threshold}}
    assert ms.alert_flags(v1_like, probabilities, ppm).tolist() == [1, 0]  # the v1 flip


def test_quantisation_window_is_below_one_ppm(spec):
    threshold, threshold_ppm = spec["decision"]["m9a_threshold"], spec["decision"]["threshold_ppm"]
    lowest_alert = (threshold_ppm - 0.5) / 1e6
    assert 0 < threshold - lowest_alert < 1e-6
    inside = np.array([float(np.nextafter(lowest_alert, 1.0)), (lowest_alert + threshold) / 2])
    flags = ms.alert_flags(spec, inside, np.array([ms.to_ppm(v) for v in inside]))
    assert flags.tolist() == [1, 1] and (inside < threshold).all()  # alert under v2, not v1


def test_artifacts_are_deterministic(chain, tmp_path):
    root, result, _ = chain
    again = wf.run_m9c(root / "m9a", root / "m9b", "again", runs_root=tmp_path,
                       approval_path=write_approval(root))
    for path in sorted(result["run_dir"].iterdir()):
        if path.name != "manifest.json":
            assert (again["run_dir"] / path.name).read_bytes() == path.read_bytes(), path.name
    assert again["manifest"]["model_spec_sha256"] == result["manifest"]["model_spec_sha256"]


def test_historical_v1_export_is_reproduced(chain, tmp_path):
    root = chain[0]
    v1 = wf.run_m9c(root / "m9a", root / "m9b", "v1", runs_root=tmp_path, version=ms.SPEC_V1)
    v2 = wf.run_m9c(root / "m9a", root / "m9b", "v2", runs_root=tmp_path,
                    approval_path=write_approval(root), previous_run=v1["run_dir"])
    assert v2["manifest"]["spec_lineage"]["historical_v1_reproduced_byte_for_byte"] is True
    assert v1["spec"]["decision"]["alert_rule"].startswith("probability >=")


def test_profile_comparison_survives_a_json_round_trip(chain):
    """Regression (found in M9D): sorted JSON keys moved __other__ ahead of lowercase
    categories, and compare() paired proportions by position."""
    root, result, _ = chain
    saved = json.loads((result["run_dir"] / "monitoring_reference.json").read_text())
    assert list(saved["categorical"]["channel"]["proportions"])[0] == "__other__"
    transactions = pd.read_csv(root / "data" / "transactions.csv", dtype=str)
    scored = ms.score_python(result["spec"], transactions).query("input_valid == 1")
    partitions = pd.read_csv(root / "m9a" / "partitions.csv", dtype=str)
    ids = partitions.loc[partitions["partition"] == "validation_3", "transaction_id"]
    window = scored.set_index("transaction_id").loc[ids].reset_index()
    identity = rp.compare(saved, window)
    assert all(v["psi"] == 0.0 for k, v in identity.items() if k != "alert_rate")
