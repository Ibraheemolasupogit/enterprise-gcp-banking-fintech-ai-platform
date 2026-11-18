"""M9B: exact contributions, reason codes, importance stability and governance artifacts.

A small M9A run is created in a temporary directory; nothing touches outputs/ or the cloud.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.workflow import run_generation
from financial_crime_ml.evaluation import evaluation_v2 as ev
from financial_crime_ml.evaluation import explainability as xp
from financial_crime_ml.evaluation.behavioural import MODEL_FEATURES
from financial_crime_ml.governance import m9b_workflow as wf
from financial_crime_ml.governance.model_card_v2 import READINESS
from financial_crime_ml.governance.model_risk_assessment import VALID_READINESS_STATUSES

FAST = ("seed: 42\ntest_fraction: 0.2\nfolds: 3\nembargo_seconds: 3600\n"
        "operating_capacity: 0.05\ncapacities: [0.01, 0.05, 0.10]\nbootstrap_resamples: 40\n"
        "confidence: 0.95\nece_bins: 10\nsegment_min_rows: 200\nsegment_min_class: 20\n"
        "challenger_ece_tolerance: 0.02\nhgb_max_iter: 60\n")


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.name.encode() + item.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def m9a_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("m9b")
    data = run_generation(replace(SyntheticConfig(), seed=5, transactions=6000, customers=120,
                                  accounts=180, beneficiaries=360, devices=240),
                          "data", runs_root=root)
    assert data["manifest"]["status"] == "completed"
    (root / "fast.yaml").write_text(FAST)
    result = ev.run_evaluation_v2(root / "data", root / "fast.yaml", "m9a", runs_root=root)
    assert result["selection"]["selected_model"] == ev.CHAMPION
    return root / "m9a"


@pytest.fixture(scope="module")
def m9b(m9a_run, tmp_path_factory):
    before = _tree_hash(m9a_run)
    result = wf.run_explainability(m9a_run, "m9b", runs_root=tmp_path_factory.mktemp("out"),
                                   repeats=2)
    return result, before


@pytest.fixture(scope="module")
def loaded(m9a_run):
    m9a = xp.load_m9a(m9a_run)
    features, partitions = xp.load_features(m9a)
    return m9a, features, partitions


# --------------------------------------------------------------------------- contributions
def test_contributions_reconstruct_the_original_model(m9b):
    result, _ = m9b
    for name, report in result["reconstruction"].items():
        assert report["max_abs_logit_vs_original_model"] <= 1e-9, name
        assert report["max_abs_logit_vs_design"] <= 1e-9, name
        assert report["max_abs_probability_vs_original_model"] <= 1e-12, name
    contrib = pd.read_csv(result["run_dir"] / "contributions_test.csv",
                          float_precision="round_trip")
    assert list(contrib.columns) == ["transaction_id", "intercept", *MODEL_FEATURES, "logit"]
    parts = contrib[["intercept", *MODEL_FEATURES]].to_numpy()
    assert np.allclose(parts.sum(axis=1), contrib["logit"], rtol=0, atol=1e-12)


def test_unseen_category_contributes_zero_and_stays_consistent(loaded):
    m9a, features, _ = loaded
    rows = features.iloc[:5].copy()
    rows["channel"] = "carrier_pigeon"
    contrib = xp.contributions(rows, m9a["params"])
    assert (contrib["channel"] == 0.0).all()
    assert np.allclose(contrib["logit"], xp.design_logit(rows, m9a["params"]), atol=1e-12)


def test_tampered_parameters_fail_reconstruction(loaded):
    m9a, features, _ = loaded
    params = copy.deepcopy(m9a["params"])
    params["coefficients"][3] += 1e-6
    stored = xp._csv(m9a["contents"]["test_predictions.csv"])
    with pytest.raises(xp.ExplainabilityError, match="reconstruct"):
        xp.reconstruction_report(xp.contributions(features, params), features, params,
                                 stored, "test")
    with pytest.raises(xp.ExplainabilityError):
        xp.stored_logit(np.array([0.0, 0.5]))


# --------------------------------------------------------------------------- reason codes
def _frame(values: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    contrib = pd.DataFrame([{"transaction_id": "T1", "intercept": -3.0,
                             **{f: values.get(f, 0.0) for f in MODEL_FEATURES}}])
    feats = pd.DataFrame([{"transaction_id": "T1", **{f: 1 for f in MODEL_FEATURES}}])
    return contrib, feats


def test_reason_codes_rank_deterministically_with_feature_order_tie_break():
    contrib, feats = _frame({"channel": 0.5, "acct_txn_count_10m": 0.5,
                             "transaction_hour": 0.5, "is_round_amount": 0.9})
    alerts = pd.DataFrame({"transaction_id": ["T1"], "score": [0.9]})
    codes = xp.reason_codes(contrib, feats, alerts)
    # 0.9 first; then the 0.5 ties in feature-contract order: transaction_hour precedes
    # acct_txn_count_10m, which precedes channel
    assert codes["feature"].tolist() == ["is_round_amount", "transaction_hour",
                                         "acct_txn_count_10m"]
    assert codes["rank"].tolist() == [1, 2, 3]
    assert codes.equals(xp.reason_codes(contrib, feats, alerts))


def test_reason_codes_pad_when_fewer_than_three_factors_increase_risk():
    contrib, feats = _frame({"acct_new_beneficiary": 0.7, "currency": -2.0})
    codes = xp.reason_codes(contrib, feats, pd.DataFrame({"transaction_id": ["T1"],
                                                          "score": [0.6]}))
    assert codes["code"].tolist() == ["R19", "R00", "R00"]
    assert codes["text"].iloc[1] == xp.NO_FURTHER_REASON[1]


def test_reason_mapping_is_complete_unique_and_human_readable():
    assert set(xp.REASONS) == set(MODEL_FEATURES)
    codes = [code for code, _ in xp.REASONS.values()]
    assert len(set(codes)) == len(codes) and xp.NO_FURTHER_REASON[0] not in codes
    assert all(text and not text.islower() and "_" not in text
               for _, text in xp.REASONS.values())


def test_every_alert_has_exactly_three_codes(m9b, loaded):
    result, _ = m9b
    m9a = loaded[0]
    codes = pd.read_csv(result["run_dir"] / "reason_codes.csv", dtype={"transaction_id": str})
    alerts = xp._csv(m9a["contents"]["test_predictions.csv"]).query("alert == 1")
    assert sorted(codes["transaction_id"].unique()) == sorted(alerts["transaction_id"])
    assert (codes.groupby("transaction_id")["rank"].apply(list) == pd.Series(
        [[1, 2, 3]] * len(alerts), index=sorted(alerts["transaction_id"]))).all()
    real = codes[codes["code"] != "R00"]
    assert (real["contribution"] > 0).all()


# --------------------------------------------------------------------------- importance
def test_spearman_handles_ties_and_constants():
    assert xp.spearman(np.array([1, 2, 3]), np.array([10, 20, 30])) == pytest.approx(1.0)
    assert xp.spearman(np.array([1, 2, 3]), np.array([3, 2, 1])) == pytest.approx(-1.0)
    assert xp.spearman(np.array([1, 1, 2]), np.array([1, 2, 3])) == pytest.approx(0.8660254)
    assert xp.spearman(np.array([1, 1, 1]), np.array([1, 2, 3])) is None


def test_importance_is_validation_only_deterministic_and_replicas_verified(loaded):
    m9a, features, partitions = loaded
    labels = wf._dev_labels(m9a, partitions)
    test_ids = set(partitions.loc[partitions["partition"] == "test", "transaction_id"])
    assert not test_ids & set(labels.index)  # test labels never loaded
    replicas = xp.fold_replicas(m9a, features, partitions, labels)
    for (_, fold), (_, val, _) in replicas.items():
        assert set(partitions.set_index("transaction_id").loc[val["transaction_id"],
                                                              "partition"]) == \
            {f"validation_{fold}"}
    subset = {k: v for k, v in replicas.items() if k[1] == 3}
    first = xp.importance(subset, repeats=2, seed=42)
    assert first == xp.importance(subset, repeats=2, seed=42)
    assert set(first) == set(ev.MODELS) and set(first[ev.CHAMPION]["3"]["mean"]) == \
        set(MODEL_FEATURES)


def test_replica_that_does_not_reproduce_m9a_is_rejected(loaded):
    m9a, features, partitions = loaded
    tampered = dict(m9a)
    oof = xp._csv(m9a["contents"]["validation_predictions.csv"])
    oof.loc[0, "score"] = oof.loc[0, "score"] + 1e-15
    tampered["contents"] = {**m9a["contents"],
                            "validation_predictions.csv": oof.to_csv(
                                index=False, float_format=ev.EXACT_FLOAT).encode()}
    with pytest.raises(xp.ExplainabilityError, match="reproduce"):
        xp.fold_replicas(tampered, features, partitions, wf._dev_labels(m9a, partitions))


def test_stability_reports_pairwise_spearman(m9b):
    result, _ = m9b
    for name in ev.MODELS:
        s = result["stability"][name]
        assert set(s["pairwise_spearman"]) == {"1-2", "1-3", "2-3"}
        assert s["mean_spearman"] is None or -1 <= s["mean_spearman"] <= 1
        assert len(s["mean_importance_ranking"]) == len(MODEL_FEATURES)
        supplementary = s["supplementary_material_features"]
        assert "not pre-registered" in supplementary["definition"]
        assert set(supplementary["pairwise_spearman"]) == {"1-2", "1-3", "2-3"}


def test_material_feature_filter_drops_negligible_features():
    folds = {str(k): {"mean": {f: (0.0001 * i if i > 2 else 0.1 * (3 - i) + 0.01 * k)
                               for i, f in enumerate(MODEL_FEATURES)}} for k in (1, 2, 3)}
    s = xp.stability({"m": folds})["m"]
    assert s["supplementary_material_features"]["features"] == list(MODEL_FEATURES[:3])
    assert s["supplementary_material_features"]["mean_spearman"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- governance
def test_model_card_and_decision_record_come_from_the_evidence(m9b, loaded):
    result, _ = m9b
    m9a = loaded[0]
    card, record = result["card"], result["record"]
    assert card["test_metrics"]["pr_auc"] == m9a["test_metrics"]["pr_auc"]
    assert card["selection"]["selection_sha256"] == m9a["selection"]["selection_sha256"]
    assert card["deployment_readiness"]["status"] == READINESS in VALID_READINESS_STATUSES
    text = (result["run_dir"] / "model_card.md").read_text()
    for heading in ("Data provenance", "Model selection", "Evaluation", "Calibration",
                    "Segments", "Leakage controls", "Synthetic-data limitations",
                    "Intended uses", "Prohibited uses", "Deployment readiness",
                    "Explainability"):
        assert f"## {heading}" in text, heading
    assert f"{m9a['test_metrics']['pr_auc']:.3f}" in text
    assert record["decision"] == m9a["selection"]["selected_model"] == ev.CHAMPION
    assert record["test_window_used_for_decision"] is False
    assert record["approval"]["status"] == "pending_human_review"
    assert "Decision:" in (result["run_dir"] / "decision_record.md").read_text()


def test_m9a_evidence_is_unchanged_and_hashes_recorded(m9b, m9a_run):
    result, before = m9b
    assert _tree_hash(m9a_run) == before
    manifest = result["manifest"]
    assert manifest["status"] == "completed" and manifest["test_labels_read"] is False
    from financial_crime_ml.evaluation.manifest import sha256_bytes

    for name, digest in manifest["artifacts_sha256"].items():
        assert sha256_bytes((result["run_dir"] / name).read_bytes()) == digest


def test_tampered_m9a_run_is_refused(m9a_run, tmp_path):
    copy_dir = tmp_path / "m9a"
    shutil.copytree(m9a_run, copy_dir)
    params = json.loads((copy_dir / "model_parameters.json").read_text())
    params["intercept"] += 0.1
    (copy_dir / "model_parameters.json").write_text(json.dumps(params))
    with pytest.raises(xp.ExplainabilityError, match="hash mismatch"):
        xp.load_m9a(copy_dir)
    with pytest.raises(ValueError, match="never writes into"):
        wf.run_explainability(m9a_run, "inside", runs_root=m9a_run, repeats=2)
