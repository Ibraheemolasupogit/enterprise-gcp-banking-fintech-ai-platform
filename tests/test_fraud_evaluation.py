"""Training/threshold isolation and independent metric reconciliation."""

import numpy as np
import pandas as pd
import pytest

from financial_crime_ml.evaluation import fraud_baseline as baseline
from financial_crime_ml.evaluation.contracts import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
    EvaluationConfig,
    build_transaction_features,
    chronological_split,
    prepare_transactions,
)
from financial_crime_ml.evaluation.fraud_baseline import DEFAULT_INPUT_PATH


def test_preprocessor_fits_only_training_rows_and_ignores_future_categories():
    ordered = prepare_transactions(pd.read_csv(DEFAULT_INPUT_PATH))
    parts = chronological_split(ordered, EvaluationConfig())
    train = build_transaction_features(parts["train"])
    validation = build_transaction_features(parts["validation"])
    validation["channel"] = "future_only_channel"
    validation["transaction_amount_log"] = 1e12
    model = baseline.build_model(EvaluationConfig())
    model.fit(train, parts["train"][TARGET_COLUMN])
    preprocessor = model.named_steps["preprocessor"]
    scaler = preprocessor.named_transformers_["numeric"]
    encoder = preprocessor.named_transformers_["categorical"]
    expected_mean = train[list(NUMERIC_FEATURES)].mean().to_numpy()
    np.testing.assert_allclose(scaler.mean_, expected_mean)
    assert scaler.n_samples_seen_ == len(train)
    for name, categories in zip(CATEGORICAL_FEATURES, encoder.categories_, strict=True):
        assert set(categories) == set(train[name])
    before = scaler.mean_.copy()
    model.predict_proba(validation)
    np.testing.assert_array_equal(scaler.mean_, before)
    assert all("future_only_channel" not in values for values in encoder.categories_)


def test_validation_threshold_rule_and_ties_are_explicit():
    labels = pd.Series([0, 0, 1, 1])
    probabilities = np.array([0.1, 0.4, 0.6, 0.9])
    threshold, analysis = baseline.select_validation_threshold(
        labels, probabilities, (0.3, 0.5, 0.6, 0.8)
    )
    assert threshold == 0.6  # 0.5 and 0.6 both perfect; deterministic higher-threshold tie break.
    assert analysis.loc[analysis.threshold.eq(threshold), "f1_score"].item() == 1


def test_saved_metrics_reconcile_with_predictions_and_pairwise_auc():
    labels = pd.Series([0, 1, 0, 1, 1, 0])
    probabilities = np.array([0.1, 0.8, 0.7, 0.6, 0.3, 0.3])
    metrics = baseline.evaluate_test(labels, probabilities, 0.5)
    assert metrics["confusion_matrix"] == [[2, 1], [1, 2]]
    assert metrics["precision"] == pytest.approx(2 / 3)
    assert metrics["recall"] == pytest.approx(2 / 3)
    assert metrics["f1_score"] == pytest.approx(2 / 3)
    positives, negatives = probabilities[labels == 1], probabilities[labels == 0]
    pairwise_auc = np.mean(
        [float(pos > neg) + 0.5 * float(pos == neg) for pos in positives for neg in negatives]
    )
    assert metrics["roc_auc"] == pytest.approx(pairwise_auc)
    # AP at distinct score cutoffs, including the tied positive/negative probability.
    assert metrics["average_precision"] == pytest.approx((1 + (2 / 3) + (3 / 5)) / 3)


def test_test_labels_cannot_influence_training_or_threshold_and_test_evaluates_once(
    tmp_path, monkeypatch
):
    raw = pd.read_csv(DEFAULT_INPUT_PATH)
    ordered = prepare_transactions(raw)
    parts = chronological_split(ordered, EvaluationConfig())
    test_ids = set(parts["test"].transaction_id)
    reference = baseline.run_fraud_evaluation(run_id="reference", runs_root=tmp_path)
    raw.loc[raw.transaction_id.isin(test_ids), TARGET_COLUMN] = ~raw.loc[
        raw.transaction_id.isin(test_ids), TARGET_COLUMN
    ]
    changed_input = tmp_path / "changed.csv"
    raw.to_csv(changed_input, index=False)
    events = []
    original_build = baseline.build_model
    original_select = baseline.select_validation_threshold
    original_evaluate = baseline.evaluate_test

    def build(config):
        model = original_build(config)
        original_fit = model.fit
        original_predict = model.predict_proba

        def fit(features, labels):
            events.append(("fit", list(features.index)))
            return original_fit(features, labels)

        def predict(features):
            events.append(("predict", list(features.index)))
            return original_predict(features)

        model.fit = fit
        model.predict_proba = predict
        return model

    def select(labels, probabilities, candidates):
        events.append(("select", list(labels.index)))
        return original_select(labels, probabilities, candidates)

    def evaluate(labels, probabilities, threshold):
        events.append(("evaluate", list(labels.index)))
        return original_evaluate(labels, probabilities, threshold)

    monkeypatch.setattr(baseline, "build_model", build)
    monkeypatch.setattr(baseline, "select_validation_threshold", select)
    monkeypatch.setattr(baseline, "evaluate_test", evaluate)
    changed = baseline.run_fraud_evaluation(changed_input, run_id="changed", runs_root=tmp_path)
    assert events == [
        ("fit", list(parts["train"].index)),
        ("predict", list(parts["validation"].index)),
        ("select", list(parts["validation"].index)),
        ("predict", list(parts["test"].index)),
        ("evaluate", list(parts["test"].index)),
    ]
    assert changed["metrics"]["threshold"] == reference["metrics"]["threshold"]
    assert changed["metrics"]["confusion_matrix"] != reference["metrics"]["confusion_matrix"]
    assert (changed["run_dir"] / "model_parameters.json").read_bytes() == (
        reference["run_dir"] / "model_parameters.json"
    ).read_bytes()
