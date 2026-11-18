"""M9B model card and champion/challenger decision record, generated from run evidence.

Every number is read from the hashed M9A artifacts or the M9B explainability outputs; nothing
is typed in by hand. The structure follows the legacy ``model_card_generator`` sections; the
readiness vocabulary is ``model_risk_assessment.VALID_READINESS_STATUSES``.
"""

from __future__ import annotations

from typing import Any

from financial_crime_ml.evaluation.behavioural import BEHAVIOURAL_FEATURES, MODEL_FEATURES
from financial_crime_ml.governance.model_risk_assessment import VALID_READINESS_STATUSES

READINESS = "not_production_ready"
assert READINESS in VALID_READINESS_STATUSES
INTENDED_USES = [
    "Portfolio demonstration of governed fraud-model engineering on synthetic data.",
    "Reference model for the M9C cloud scoring parity work (BigQuery SQL, synthetic pilot).",
    "Teaching material for point-in-time features, sealed evaluation and explainability.",
]
PROHIBITED_USES = [
    "Any decision about a real customer, account, payment or investigation.",
    "Operational fraud or AML alerting, blocking, de-risking or regulatory reporting.",
    "Claims of detection performance on real financial crime.",
    "Use without human investigator review, independent validation and governance approval.",
    "Scoring data whose history differs from the feature contract's assumptions.",
]


def _f(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _ci(interval: list[float], digits: int = 3) -> str:
    return f"[{interval[0]:.{digits}f}, {interval[1]:.{digits}f}]"


def build_card(m9a: dict[str, Any], m9b: dict[str, Any]) -> dict[str, Any]:
    manifest, selection, test = m9a["manifest"], m9a["selection"], m9a["test_metrics"]
    pooled, decision = selection["pooled_validation"], selection["decision"]
    intervals = test["bootstrap"]["intervals"]
    segments = {kind: {name: seg for name, seg in values.items()}
                for kind, values in test["segments"].items()}
    insufficient = {kind: sorted(n for n, s in values.items()
                                 if s["status"] == "insufficient_support")
                    for kind, values in segments.items()}
    return {
        "model": {"name": "fraud-v2 logistic regression champion",
                  "feature_version": m9a["params"]["feature_version"],
                  "deployed": selection["deployed_model"],
                  "threshold": selection["threshold"],
                  "threshold_source": selection["threshold_source"],
                  "cloud_portable": selection["cloud_portable"],
                  "features": list(MODEL_FEATURES),
                  "behavioural_features": list(BEHAVIOURAL_FEATURES)},
        "data_provenance": {
            "m9a_run_id": manifest["run_id"], "m9a_manifest_sha256": m9b["m9a_manifest_sha256"],
            "dataset_dir": manifest["inputs"]["dataset_dir"],
            "dataset_sha256": manifest["inputs"]["sha256"],
            "configuration_sha256": manifest["inputs"]["configuration_sha256"],
            "feature_contract_sha256": m9a["feature_contract"]["contract_sha256"],
            "git_revision": manifest["git_revision"],
            "working_tree_dirty_at_run": bool(manifest["git_status_porcelain"]),
            "source_fingerprint_sha256": manifest["source_fingerprint_sha256"],
            "versions": manifest["versions"], "synthetic_only": True},
        "selection": {"rule": decision["rule"], "selected": decision["selected"],
                      "selection_sha256": selection["selection_sha256"],
                      "pooled_validation": {
                          name: {"pr_auc": v["metrics"]["pr_auc"],
                                 "pr_auc_ci": v["bootstrap"]["intervals"]["pr_auc"],
                                 "brier": v["metrics"]["brier"], "ece": v["metrics"]["ece"]}
                          for name, v in pooled.items()}},
        "test_metrics": {"rows": test["rows"], "positives": test["positives"],
                         "evaluated_once": manifest["test_evaluations"] == 1,
                         "pr_auc": test["pr_auc"], "pr_auc_ci": intervals["pr_auc"],
                         "roc_auc": test["roc_auc"], "roc_auc_ci": intervals["roc_auc"],
                         "at_threshold": test["at_threshold"],
                         "at_capacity": test["at_capacity"],
                         "precision_at_threshold_ci": intervals["precision@threshold"],
                         "recall_at_threshold_ci": intervals["recall@threshold"],
                         "bootstrap": {k: test["bootstrap"][k] for k in
                                       ("method", "resamples", "used", "confidence", "days")}},
        "calibration": {"brier": test["brier"], "brier_ci": intervals["brier"],
                        "ece": test["ece"], "ece_ci": intervals["ece"],
                        "method": "none (no class weights, no post-hoc calibration)"},
        "segments": {"rule": test["segment_rule"], "results": segments,
                     "insufficient_support": insufficient},
        "explainability": {"reconstruction": m9b["reconstruction"],
                           "reason_codes": m9b["reason_summary"],
                           "importance_stability": m9b["stability"]},
        "leakage_controls": [
            "Features use observations only; labels are read from a separate file and never "
            "enter feature construction (tested: flipping every label changes nothing).",
            "Behavioural features use strictly earlier event times; same-instant rows never "
            "see each other (oracle, future-invariance and same-instant tests).",
            "Chronological development/test split; rolling-origin folds with a one-hour "
            "embargo before each validation start.",
            "Model choice and threshold from validation only; the selection was frozen and "
            "hashed before the test labels were released once (test_evaluations = 1).",
            "M9B uses validation folds only for importance; test labels are not read.",
        ],
        "synthetic_data_limitations": list(manifest["limitations"]) + [
            "Benign controls share the fraud mechanisms and burst openers have no history, "
            "which caps precision; this is a property of the generator.",
        ],
        "intended_uses": INTENDED_USES, "prohibited_uses": PROHIBITED_USES,
        "deployment_readiness": {
            "status": READINESS,
            "ready_for": "M9C offline SQL-scoring parity and a gated synthetic BigQuery run",
            "blocking_for_production": [
                "Real labelled data, independent model validation and governance approval.",
                "Human approval of this decision record (not recorded here).",
                "Calibration and monitoring under prevalence shift; segment CIs.",
                "Cloud feature parity (M9C) including identical transaction history.",
            ]},
    }


def render_card(card: dict[str, Any]) -> str:
    m, prov, sel, t = card["model"], card["data_provenance"], card["selection"], \
        card["test_metrics"]
    cal, seg, exp = card["calibration"], card["segments"], card["explainability"]
    lines = [
        "# Model card: fraud-v2 logistic regression champion (M9B)", "",
        "Generated from M9A run evidence and M9B explainability outputs. **Synthetic data "
        f"only. Readiness: `{card['deployment_readiness']['status']}`.**", "",
        "## Model",
        f"- {m['deployed']}; feature contract `{m['feature_version']}` "
        f"({len(m['features'])} features, {len(m['behavioural_features'])} point-in-time).",
        f"- Frozen threshold {m['threshold']:.4f} ({m['threshold_source']}); "
        f"cloud-portable: {m['cloud_portable']}.", "",
        "## Data provenance",
        f"- M9A run `{prov['m9a_run_id']}` (manifest `{prov['m9a_manifest_sha256']}`); "
        f"git `{prov['git_revision'][:7]}`, working tree dirty at run: "
        f"{prov['working_tree_dirty_at_run']} (source fingerprint "
        f"`{prov['source_fingerprint_sha256'][:16]}…`).",
        *[f"- `{k}` `{v}`" for k, v in sorted(prov["dataset_sha256"].items())],
        f"- Feature contract `{prov['feature_contract_sha256']}`.", "",
        "## Model selection (validation only)", f"- Rule: {sel['rule']}.",
        *[f"- {name}: pooled PR-AUC {_f(v['pr_auc'])} {_ci(v['pr_auc_ci'])}, "
          f"Brier {_f(v['brier'], 4)}, ECE {_f(v['ece'], 4)}"
          for name, v in sel["pooled_validation"].items()],
        f"- Selected: **{sel['selected']}** (selection record `{sel['selection_sha256']}`).",
        "", "## Evaluation (sealed test window, evaluated once)",
        f"- {t['rows']} rows, {t['positives']} positives; {t['bootstrap']['method']}, "
        f"{t['bootstrap']['used']}/{t['bootstrap']['resamples']} resamples, "
        f"{t['bootstrap']['days']} days.",
        f"- PR-AUC {_f(t['pr_auc'])} {_ci(t['pr_auc_ci'])}; ROC AUC {_f(t['roc_auc'])} "
        f"{_ci(t['roc_auc_ci'])}.",
        f"- At the threshold: precision {_f(t['at_threshold']['precision'])} "
        f"{_ci(t['precision_at_threshold_ci'])}, recall {_f(t['at_threshold']['recall'])} "
        f"{_ci(t['recall_at_threshold_ci'])}, alert rate "
        f"{t['at_threshold']['alert_rate']:.2%}.",
        *[f"- At {float(c):.0%} capacity: precision {_f(r['precision'])}, recall "
          f"{_f(r['recall'])}" for c, r in t["at_capacity"].items()],
        "", "## Calibration",
        f"- Brier {_f(cal['brier'], 4)} {_ci(cal['brier_ci'], 4)}; ECE {_f(cal['ece'], 4)} "
        f"{_ci(cal['ece_ci'], 4)}. Method: {cal['method']}.", "",
        "## Segments and their limits",
        f"- Reported only with ≥{seg['rule']['min_rows']} rows and ≥"
        f"{seg['rule']['min_positives']} positives and negatives; point estimates without CIs.",
        *[f"- {kind} {name}: PR-AUC {_f(s['pr_auc'])}, alert rate "
          f"{s['alert_rate@threshold']:.2%} ({s['rows']} rows)"
          for kind in ("currency", "channel", "corridor_type")
          for name, s in seg["results"][kind].items() if s["status"] == "reported"],
        *[f"- Insufficient support ({kind}): {len(names)} — "
          f"{', '.join(names[:6])}{' …' if len(names) > 6 else ''}"
          for kind, names in seg["insufficient_support"].items() if names],
        "", "## Explainability",
        f"- Contributions reconstruct the original model: max |logit error| "
        f"{exp['reconstruction']['test']['max_abs_logit_vs_original_model']:.1e}, max "
        f"|probability error| "
        f"{exp['reconstruction']['test']['max_abs_probability_vs_original_model']:.1e} "
        f"(tolerances 1e-9 / 1e-12).",
        f"- Reason codes: {exp['reason_codes']['alerts']} alerts, "
        f"{exp['reason_codes']['codes_per_alert']} codes each; most frequent first codes: "
        + ", ".join(f"{k} ({v})" for k, v in exp["reason_codes"]["top_first_codes"].items())
        + ".",
        *[f"- Importance rank stability ({name}): mean Spearman over all "
          f"{len(MODEL_FEATURES)} features {_f(s['mean_spearman'])}; over the "
          f"{len(s['supplementary_material_features']['features'])} material features "
          f"{_f(s['supplementary_material_features']['mean_spearman'])} (supplementary, "
          f"not pre-registered); top-5 common to all folds: "
          f"{', '.join(s['top_5_common_to_all_folds']) or 'none'}"
          for name, s in exp["importance_stability"].items()],
        "", "## Leakage controls", *[f"- {c}" for c in card["leakage_controls"]],
        "", "## Synthetic-data limitations",
        *[f"- {c}" for c in card["synthetic_data_limitations"]],
        "", "## Intended uses", *[f"- {c}" for c in card["intended_uses"]],
        "", "## Prohibited uses", *[f"- {c}" for c in card["prohibited_uses"]],
        "", "## Deployment readiness",
        f"- Status: `{card['deployment_readiness']['status']}`; ready for: "
        f"{card['deployment_readiness']['ready_for']}.",
        *[f"- Blocking for production: {c}"
          for c in card["deployment_readiness"]["blocking_for_production"]], ""]
    return "\n".join(lines)


def build_decision_record(m9a: dict[str, Any], m9b: dict[str, Any]) -> dict[str, Any]:
    selection, decision = m9a["selection"], m9a["selection"]["decision"]
    folds: dict[str, dict[str, Any]] = {}
    for row in m9a["fold_metrics"]:
        folds.setdefault(row["model"], {})[str(row["fold"])] = {
            "pr_auc": row["metrics"]["pr_auc"], "ece": row["metrics"]["ece"]}
    return {
        "id": f"DR-M9B-{m9a['manifest']['run_id']}",
        "question": "Which model is the fraud-v2 champion for M9C?",
        "options": ["logistic_regression (champion)", "hist_gradient_boosting (challenger)"],
        "decision": decision["selected"],
        "rule": decision["rule"],
        "rule_outcome": {k: decision[k] for k in
                         ("challenger_pr_auc_ci_lower", "champion_pr_auc",
                          "pr_auc_condition", "challenger_ece", "champion_ece",
                          "ece_tolerance", "ece_condition")},
        "per_fold_validation": folds,
        "reference_row_local_pooled_pr_auc": decision.get("reference_pooled_pr_auc"),
        "supporting_evidence": [
            "Challenger PR-AUC CI lower bound does not exceed the champion's point "
            "estimate: the challenger is not shown to be better.",
            "Champion has the lower pooled Brier and ECE (better calibrated).",
            "Champion is cloud-portable as exact SQL (M9C); the challenger is not.",
            "Champion decisions decompose exactly into per-feature contributions "
            f"(max logit error "
            f"{m9b['reconstruction']['test']['max_abs_logit_vs_original_model']:.1e}).",
            "Point-in-time features carry the signal: the M1 row-local reference is near "
            "prevalence.",
            "Champion's top features are consistent across folds (see top-5 common to all "
            "folds); the challenger switches between substitute burst signals.",
        ],
        "importance_stability": {k: {"mean_spearman_all": v["mean_spearman"],
                                     "mean_spearman_material_supplementary":
                                     v["supplementary_material_features"]["mean_spearman"],
                                     "top_5_common_to_all_folds":
                                     v["top_5_common_to_all_folds"]}
                                 for k, v in m9b["stability"].items()},
        "selection_sha256": selection["selection_sha256"],
        "test_window_used_for_decision": False,
        "approval": {"status": "pending_human_review",
                     "note": "No approver is recorded by code; a named reviewer must sign "
                             "off before any use beyond the synthetic pilot."},
    }


def render_decision_record(record: dict[str, Any]) -> str:
    out = [f"# Decision record {record['id']}", "", f"**Question:** {record['question']}",
           "", f"**Decision:** `{record['decision']}`", "", f"**Rule:** {record['rule']}", "",
           "| Condition | Value |", "| --- | --- |"]
    out += [f"| {k} | {v:.4f} |" if isinstance(v, float) else f"| {k} | {v} |"
            for k, v in record["rule_outcome"].items()]
    out += ["", "| Model | " + " | ".join(f"Fold {k} PR-AUC / ECE"
                                         for k in sorted(next(iter(
                                             record["per_fold_validation"].values())))) + " |",
            "| --- |" + " --- |" * len(next(iter(record["per_fold_validation"].values())))]
    for model, folds in record["per_fold_validation"].items():
        out.append(f"| {model} | " + " | ".join(
            f"{folds[k]['pr_auc']:.3f} / {folds[k]['ece']:.3f}" for k in sorted(folds)) + " |")
    out += ["", "**Evidence:**", *[f"- {e}" for e in record["supporting_evidence"]],
            f"- Test window used for the decision: {record['test_window_used_for_decision']}",
            "", "| Model | Mean Spearman (all features) | Mean Spearman (material, "
            "supplementary) | Top-5 features common to all folds |", "| --- | --- | --- | --- |",
            *[f"| {model} | {_f(s['mean_spearman_all'])} | "
              f"{_f(s['mean_spearman_material_supplementary'])} | "
              f"{', '.join(s['top_5_common_to_all_folds']) or 'none'} |"
              for model, s in record["importance_stability"].items()],
            "", f"**Approval:** `{record['approval']['status']}` — {record['approval']['note']}",
            ""]
    return "\n".join(out)
