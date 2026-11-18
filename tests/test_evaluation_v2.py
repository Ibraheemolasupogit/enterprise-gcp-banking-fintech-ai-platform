"""M9A: point-in-time features, rolling-origin evaluation and the sealed test window.

Synthetic v2 data generated into temporary directories only; no network or cloud access.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from financial_crime_ml.data_generation.v2.contracts import CURRENCY_EXPONENTS, SyntheticConfig
from financial_crime_ml.data_generation.v2.workflow import run_generation
from financial_crime_ml.evaluation import behavioural as bh
from financial_crime_ml.evaluation import evaluation_v2 as ev
from financial_crime_ml.evaluation import metrics_v2 as mv

ROOT = Path(__file__).resolve().parents[1]
FAST = ("seed: 42\ntest_fraction: 0.2\nfolds: 3\nembargo_seconds: 3600\n"
        "operating_capacity: 0.05\ncapacities: [0.01, 0.05, 0.10]\nbootstrap_resamples: 40\n"
        "confidence: 0.95\nece_bins: 10\nsegment_min_rows: 200\nsegment_min_class: 20\n"
        "challenger_ece_tolerance: 0.02\nhgb_max_iter: 60\n")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("m9a")
    config = replace(SyntheticConfig(), seed=11, transactions=6000, customers=120, accounts=180,
                     beneficiaries=360, devices=240)
    result = run_generation(config, "data", runs_root=root)
    assert result["manifest"]["status"] == "completed"
    return root / "data"


@pytest.fixture(scope="module")
def observations(dataset):
    return pd.read_csv(dataset / "transactions.csv", dtype=str)


@pytest.fixture(scope="module")
def run(dataset, tmp_path_factory):
    root = tmp_path_factory.mktemp("runs")
    config = root / "fast.yaml"
    config.write_text(FAST)
    calls = []
    original = ev.SealedTestWindow.evaluate

    def spy(self, frozen, digest, *args, **kwargs):
        calls.append((root / "eval" / "selection.json").exists())
        return original(self, frozen, digest, *args, **kwargs)

    ev.SealedTestWindow.evaluate = spy
    try:
        result = ev.run_evaluation_v2(dataset, config, "eval", runs_root=root)
    finally:
        ev.SealedTestWindow.evaluate = original
    return result, calls


# --------------------------------------------------------------------------- features
def _oracle(obs: pd.DataFrame) -> pd.DataFrame:
    """Row-by-row definition straight from FEATURE_SPECS (quadratic; small inputs only)."""
    data = bh.order_observations(obs)
    t = (data["transaction_timestamp"].astype("int64") // 10**9).to_numpy()
    rows = []
    for i in range(len(data)):
        r = data.iloc[i]
        earlier = t < t[i]
        acct = earlier & (data["account_id"] == r["account_id"]).to_numpy()
        cust = earlier & (data["customer_id"] == r["customer_id"]).to_numpy()

        def within(mask, w, i=i):
            return int((mask & (t >= t[i] - w)).sum())

        same_amount = acct & (data["amount_minor"] == r["amount_minor"]).to_numpy()
        hist = acct & (t >= t[i] - 2592000)
        mean = data["amount_minor"].to_numpy()[hist].sum() / hist.sum() if hist.any() else None
        gap = min(t[i] - t[acct].max(), bh.NO_HISTORY_SECONDS) if acct.any() else \
            bh.NO_HISTORY_SECONDS
        rows.append({
            "acct_txn_count_10m": within(acct, 600), "acct_txn_count_1h": within(acct, 3600),
            "acct_txn_count_24h": within(acct, 86400),
            "cust_txn_count_24h": within(cust, 86400),
            "acct_same_amount_count_24h": within(same_amount, 86400),
            "acct_history_count_30d": int(hist.sum()),
            "acct_amount_log_ratio_30d": 0.0 if mean is None else
            math.log(r["amount_minor"] + 1) - math.log(mean + 1),
            "acct_seconds_since_prev_log": math.log1p(gap),
            "acct_has_prev": int(acct.any()),
            "acct_new_beneficiary": int(not (acct & (data["beneficiary_id"] ==
                                                     r["beneficiary_id"]).to_numpy()).any()),
            "cust_new_device": int(not (cust & (data["device_id"] ==
                                                r["device_id"]).to_numpy()).any()),
            "is_round_amount": int(r["amount_minor"] %
                                   (100 * 10 ** CURRENCY_EXPONENTS[r["currency"]]) == 0)})
    return pd.DataFrame(rows)


def test_vectorised_features_equal_the_row_by_row_definition(observations):
    sample = observations.iloc[:700]
    got = bh.build_behavioural_features(sample)[list(bh.BEHAVIOURAL_FEATURES)]
    want = _oracle(sample)
    for name in bh.BEHAVIOURAL_FEATURES:
        if bh.FEATURE_SPECS[name]["type"] == "INT64":
            assert got[name].tolist() == want[name].tolist(), name
        else:
            assert np.allclose(got[name], want[name], rtol=0, atol=1e-12), name
    assert want["acct_txn_count_10m"].max() > 0  # bursts are actually exercised


def test_future_rows_never_change_earlier_features(observations):
    full = bh.build_behavioural_features(observations)
    cut = full["transaction_timestamp"].iloc[len(full) // 2]
    ts = pd.to_datetime(observations["transaction_timestamp"], utc=True)
    past = observations[ts < cut]
    future = observations[ts >= cut].copy()
    future["amount_minor"] = "999999"
    future["account_id"] = observations["account_id"].iloc[0]
    for variant in (past, pd.concat([past, future])):
        got = bh.build_behavioural_features(variant)
        got = got[got["transaction_timestamp"] < cut].reset_index(drop=True)
        want = full[full["transaction_timestamp"] < cut].reset_index(drop=True)
        pd.testing.assert_frame_equal(got, want)


def test_same_instant_rows_never_see_each_other(observations):
    base = bh.build_behavioural_features(observations)
    twin = observations.iloc[[100]].copy()
    twin["transaction_id"] = "T2-99999999"
    with_twin = bh.build_behavioural_features(pd.concat([observations, twin]))
    original = with_twin[with_twin["transaction_id"] == observations["transaction_id"].iloc[100]]
    copy_row = with_twin[with_twin["transaction_id"] == "T2-99999999"]
    cols = list(bh.BEHAVIOURAL_FEATURES)
    assert original[cols].values.tolist() == base[base["transaction_id"] ==
                                                  observations["transaction_id"].iloc[100]][
        cols].values.tolist()
    assert copy_row[cols].values.tolist() == original[cols].values.tolist()


def test_labels_are_never_inputs_and_order_is_deterministic(observations, dataset):
    base = bh.build_behavioural_features(observations)
    outcomes = pd.read_csv(dataset / "outcomes.csv", dtype=str)
    labelled = observations.merge(outcomes, on="transaction_id")
    labelled["is_suspicious"] = "1"  # flipping every label must change nothing
    shuffled = labelled.sample(frac=1.0, random_state=3)
    got = bh.build_behavioural_features(shuffled)
    pd.testing.assert_frame_equal(got, base)
    assert not set(bh.LABEL_LIKE) & set(got.columns)
    assert not any(c in bh.INPUT_COLUMNS for c in bh.LABEL_LIKE)


def test_feature_inputs_exist_in_the_bigquery_curated_table_and_contract_is_stable():
    schema = json.loads((ROOT / "infrastructure/schemas/v1/source_tables.json").read_text())
    curated = {f["name"] for f in schema["curated_transactions"]}
    assert set(bh.INPUT_COLUMNS) <= curated
    first, second = bh.contract(), bh.contract()
    assert first == second and len(first["contract_sha256"]) == 64
    assert set(first["behavioural"]) == set(bh.BEHAVIOURAL_FEATURES)
    assert first["cloud_reproduction"]["not_implemented"]


def test_sqlite_window_sql_reproduces_the_cloud_feature_spec(observations):
    """Offline evidence that the GoogleSQL-style definitions are exact (M9C requirement)."""
    data = bh.order_observations(observations.iloc[:2500])
    con = sqlite3.connect(":memory:")
    frame = data.assign(t=data["transaction_timestamp"].astype("int64") // 10**9)
    frame.drop(columns="transaction_timestamp").to_sql("tx", con, index=False)

    def window(partition, seconds, expr="COUNT(*)"):
        frame_clause = (f"RANGE BETWEEN {seconds} PRECEDING AND 1 PRECEDING" if seconds
                        else "RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING")
        return (f"{expr} OVER (PARTITION BY {partition} ORDER BY t {frame_clause})")

    sql = f"""SELECT transaction_id,
      {window('account_id', 600)} a10m, {window('account_id', 3600)} a1h,
      {window('account_id', 86400)} a24h, {window('customer_id', 86400)} c24h,
      {window('account_id, amount_minor', 86400)} same24h,
      {window('account_id', 2592000)} h30, {window('account_id', 2592000,
              'SUM(amount_minor)')} s30,
      {window('account_id', None, 'MAX(t)')} prev,
      {window('account_id, beneficiary_id', None)} pair_b,
      {window('customer_id, device_id', None)} pair_d, t, amount_minor
      FROM tx ORDER BY t, transaction_id"""
    out = pd.read_sql(sql, con)
    got = bh.build_behavioural_features(observations.iloc[:2500])
    assert out["transaction_id"].tolist() == got["transaction_id"].tolist()
    for sql_col, name in (("a10m", "acct_txn_count_10m"), ("a1h", "acct_txn_count_1h"),
                          ("a24h", "acct_txn_count_24h"), ("c24h", "cust_txn_count_24h"),
                          ("same24h", "acct_same_amount_count_24h"),
                          ("h30", "acct_history_count_30d")):
        assert out[sql_col].tolist() == got[name].tolist(), name
    assert ((out["pair_b"] == 0).astype(int).tolist() == got["acct_new_beneficiary"].tolist())
    assert ((out["pair_d"] == 0).astype(int).tolist() == got["cust_new_device"].tolist())
    mean = out["s30"] / out["h30"]
    ratio = np.where(out["h30"] > 0, np.log(out["amount_minor"] + 1.0) - np.log(mean + 1.0), 0.0)
    assert np.allclose(ratio, got["acct_amount_log_ratio_30d"], rtol=0, atol=1e-12)
    gap = np.where(out["prev"].notna(), np.minimum(out["t"] - out["prev"].fillna(0),
                                                   bh.NO_HISTORY_SECONDS), bh.NO_HISTORY_SECONDS)
    assert np.allclose(np.log1p(gap.astype(float)), got["acct_seconds_since_prev_log"],
                       rtol=0, atol=1e-12)


def test_round_amounts_respect_the_currency_exponent():
    rows = pd.DataFrame([
        {"transaction_id": f"T2-{i:08d}", "customer_id": "C2-1", "account_id": "A2-1",
         "beneficiary_id": "B2-1", "device_id": "D2-1",
         "transaction_timestamp": f"2025-01-01T00:0{i}:00Z", "amount_minor": str(m),
         "amount": a, "currency": c, "transaction_type": "card_payment",
         "merchant_category": "grocery", "origin_country": "GB", "destination_country": "GB",
         "channel": "web"}
        for i, (m, a, c) in enumerate([(10000, "100.00", "GBP"), (10050, "100.50", "GBP"),
                                       (100, "100", "JPY"), (150, "150", "JPY")])])
    assert bh.build_behavioural_features(rows)["is_round_amount"].tolist() == [1, 0, 1, 0]


# --------------------------------------------------------------------------- partitions
def test_rolling_origin_folds_are_expanding_chronological_and_embargoed(observations):
    features = bh.build_behavioural_features(observations)
    config = ev.EvaluationV2Config()
    parts = ev.make_partitions(features, config)
    times = features["transaction_timestamp"]
    assert len(parts["folds"]) == 3
    previous_train = 0
    for fold in parts["folds"]:
        train, val = fold["train"], fold["validation"]
        assert len(train) > previous_train
        previous_train = len(train)
        assert times.iloc[train].max() < times.iloc[val].min() - pd.Timedelta(hours=1)
        assert times.iloc[val].max() < times.iloc[parts["test"]].min()
    for cut in parts["bounds"][1:-1]:  # no instant spans two blocks
        assert times.iloc[cut] != times.iloc[cut - 1]
    with pytest.raises(ValueError, match="folds"):
        ev.EvaluationV2Config(folds=2)


# --------------------------------------------------------------------------- metrics
def test_metric_definitions_are_exact():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.4, 0.35, 0.8])
    assert mv.brier(y, p) == pytest.approx((0.01 + 0.16 + 0.4225 + 0.04) / 4)
    # bins: 0.1->1, 0.35->3, 0.4->4, 0.8->8; each |p - y| weighted 1/4
    assert mv.ece(y, p, 10) == pytest.approx((0.1 + 0.65 + 0.4 + 0.2) / 4)
    ids = np.array(["b", "a", "c", "d"])
    tied = np.array([0.5, 0.5, 0.1, 0.1])
    assert mv.capacity_alerts(tied, ids, 0.25).tolist() == [False, True, False, False]
    assert mv.capacity_alerts(tied, ids, 0.5).tolist() == [True, True, False, False]


def test_bootstrap_is_deterministic_and_skips_single_class_resamples():
    rng = np.random.default_rng(0)
    y = (rng.random(400) < 0.1).astype(int)
    p = np.clip(y * 0.4 + rng.random(400) * 0.6, 0, 1)
    ids = np.array([f"T{i:04d}" for i in range(400)])
    days = np.array([f"d{i // 20}" for i in range(400)])
    args = dict(capacities=(0.05,), threshold=0.5, bins=10, resamples=60, confidence=0.95)
    a = mv.bootstrap(y, p, ids, days, seed=1, **args)
    assert a == mv.bootstrap(y, p, ids, days, seed=1, **args)
    lo, hi = a["intervals"]["pr_auc"]
    assert lo <= hi and a["used"] + a["skipped_single_class"] == 60


def test_segments_report_only_with_minimum_support():
    frame = pd.DataFrame({"label": [1] * 30 + [0] * 300 + [1] * 2 + [0] * 50,
                          "score": np.linspace(0, 1, 382),
                          "seg": ["big"] * 330 + ["small"] * 52})
    out = mv.segment_metrics(frame, column="seg", threshold=0.5, bins=10, min_rows=200,
                             min_class=20)
    assert out["big"]["status"] == "reported" and "pr_auc" in out["big"]
    assert out["small"] == {"rows": 52, "positives": 2, "negatives": 50,
                            "status": "insufficient_support"}


# --------------------------------------------------------------------------- selection/test
def _pooled(pr_auc, ci_lower, ece):
    return {"metrics": {"pr_auc": pr_auc, "ece": ece},
            "bootstrap": {"intervals": {"pr_auc": [ci_lower, 1.0]}}}


@pytest.mark.parametrize("challenger,expected", [
    (_pooled(0.60, 0.51, 0.03), "hist_gradient_boosting"),
    (_pooled(0.60, 0.49, 0.03), "logistic_regression"),   # CI lower bound not above
    (_pooled(0.60, 0.55, 0.05), "logistic_regression"),   # calibration worse than tolerance
])
def test_promotion_rule_is_pre_registered(challenger, expected):
    champion = _pooled(0.50, 0.40, 0.02)
    assert ev.promote_challenger(champion, challenger,
                                 ev.EvaluationV2Config())["selected"] == expected


def test_sealed_window_releases_labels_once_and_only_to_a_frozen_record():
    sealed = ev.SealedTestWindow(pd.Series([0, 1], index=["T1", "T2"]))
    frozen = {"threshold": 0.5}
    config = ev.EvaluationV2Config(bootstrap_resamples=20, capacities=(0.05, 0.5),
                                   operating_capacity=0.5)
    args = (np.array(["T1", "T2"]), np.array([0.2, 0.9]), config, np.array(["d1", "d2"]))
    with pytest.raises(RuntimeError, match="frozen"):
        sealed.evaluate(frozen, "0" * 64, *args)
    sealed.evaluate(frozen, ev._digest(frozen), *args)
    with pytest.raises(RuntimeError, match="already"):
        sealed.evaluate(frozen, ev._digest(frozen), *args)


def test_run_freezes_selection_before_touching_the_test_window(run):
    result, calls = run
    run_dir, manifest = result["run_dir"], result["manifest"]
    assert calls == [True]  # selection.json existed when test labels were first released
    assert manifest["status"] == "completed" and manifest["test_evaluations"] == 1
    selection = json.loads((run_dir / "selection.json").read_text())
    assert selection["test_labels_read"] is False
    assert selection["selected_model"] in ev.MODELS
    oof = pd.read_csv(run_dir / "validation_predictions.csv", float_precision="round_trip")
    test_ids = set(pd.read_csv(run_dir / "test_predictions.csv")["transaction_id"])
    assert not test_ids & set(oof["transaction_id"])
    last = oof[(oof["model"] == selection["selected_model"]) & (oof["fold"] == 3)]
    assert selection["threshold"] == ev.capacity_threshold(
        last["score"].to_numpy(), last["transaction_id"].to_numpy(), 0.05)
    assert set(oof["model"]) == {*ev.MODELS, ev.REFERENCE}


def test_run_artifacts_are_hashed_and_complete(run):
    from financial_crime_ml.evaluation.manifest import sha256_bytes

    result, _ = run
    run_dir, manifest = result["run_dir"], result["manifest"]
    files = {p.name for p in run_dir.iterdir()} - {"manifest.json"}
    assert files == {"config.yaml", "feature_contract.json", "partitions.csv",
                     "fold_metrics.json", "validation_predictions.csv", "selection.json",
                     "test_metrics.json", "test_predictions.csv", "model_parameters.json"}
    for name in files:
        assert manifest["artifacts_sha256"][name] == sha256_bytes((run_dir / name).read_bytes())
    metrics = json.loads((run_dir / "test_metrics.json").read_text())
    for key in ("pr_auc", "brier", "ece", "at_capacity", "at_threshold", "bootstrap",
                "segments", "segment_rule"):
        assert key in metrics
    assert set(metrics["segments"]) == {"currency", "channel", "corridor", "corridor_type"}
    lo, hi = metrics["bootstrap"]["intervals"]["pr_auc"]
    assert lo <= hi
    params = json.loads((run_dir / "model_parameters.json").read_text())
    if params["model_type"] == "LogisticRegression":
        assert len(params["coefficients"]) == len(params["transformed_features"])
        assert params["feature_version"] == bh.FEATURE_VERSION


def test_run_ids_are_never_reused(dataset, run, tmp_path):
    result, _ = run
    with pytest.raises(FileExistsError):
        ev.run_evaluation_v2(dataset, result["run_dir"] / "config.yaml", "eval",
                             runs_root=result["run_dir"].parent)
