"""M9C scoring SQL generated from the model specification.

One builder renders two dialects that differ only in small, named hooks:
* ``GOOGLESQL``: the deployable artifact (BigQuery). Event time is ``UNIX_MICROS``; window
  frames are ``RANGE BETWEEN w×10⁶ PRECEDING AND 1 PRECEDING``, exactly the feature contract.
* ``SQLITE``: the offline parity engine (event time in whole seconds; same frame shape).

Table names are placeholders (``PROJECT.DATASET.<table>``), substituted only by
``marts_bundle.render`` with validated identifiers. All model constants are literals taken
from the hashed specification; nothing is read from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from financial_crime_ml.deployment.model_spec import (
    PPM,
    REASON_SLOTS,
    spec_sha256,
    uses_ppm_rule,
)
from financial_crime_ml.evaluation.behavioural import (
    FEATURE_SPECS,
    INPUT_COLUMNS,
    MODEL_FEATURES,
    NO_HISTORY_SECONDS,
)

SOURCE_TABLE = "curated_transactions"
TARGET_TABLE = "transaction_risk_scores"
OUTPUT_COLUMNS = ("transaction_id", "model_spec_sha256", "input_valid", "score_ppm", "alert",
                  *(f"reason_code_{i}" for i in range(1, REASON_SLOTS + 1)))


@dataclass(frozen=True)
class Dialect:
    name: str
    unit: int                 # event-time integer units per second
    table: str                # format string for a table reference
    event_time: str           # integer event time from transaction_timestamp
    hour: str
    day_of_week: str          # Monday = 0
    to_float: str             # cast template, {x}
    integer_cast: str         # cast template, {x}
    mod: str                  # template, {a} {b}
    least: str                # template, {a} {b}
    seconds: str              # integer seconds from an event-time difference, {x}


GOOGLESQL = Dialect(
    name="googlesql", unit=1_000_000, table="`PROJECT.DATASET.{name}`",
    event_time="UNIX_MICROS(transaction_timestamp)",
    hour="EXTRACT(HOUR FROM transaction_timestamp AT TIME ZONE 'UTC')",
    day_of_week="MOD(EXTRACT(DAYOFWEEK FROM transaction_timestamp AT TIME ZONE 'UTC') + 5, 7)",
    to_float="CAST({x} AS FLOAT64)", integer_cast="CAST({x} AS INT64)",
    mod="MOD({a}, {b})", least="LEAST({a}, {b})", seconds="DIV({x}, 1000000)")
SQLITE = Dialect(
    name="sqlite", unit=1, table="{name}",
    event_time="unixepoch(transaction_timestamp)",
    hour="CAST(strftime('%H', transaction_timestamp) AS INTEGER)",
    day_of_week="((CAST(strftime('%w', transaction_timestamp) AS INTEGER) + 6) % 7)",
    to_float="CAST({x} AS REAL)", integer_cast="CAST({x} AS INTEGER)",
    mod="(({a}) % ({b}))", least="MIN({a}, {b})", seconds="({x})")


def literal(value: float) -> str:
    """Exact round-trip FLOAT64 literal (always with '.' or exponent; negatives bracketed)."""
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("non-finite constant in the specification")
    text = repr(value)
    if "." not in text and "e" not in text:
        text += ".0"
    return f"({text})" if value < 0 else text


def _string(value: str) -> str:
    if "'" in value or "\\" in value:
        raise ValueError("unexpected quote in a specification string")
    return f"'{value}'"


def _window(d: Dialect, partition: list[str], seconds: int | None, expr: str) -> str:
    lower = "UNBOUNDED PRECEDING" if seconds is None else f"{seconds * d.unit} PRECEDING"
    return (f"{expr} OVER (PARTITION BY {', '.join(partition)} ORDER BY t "
            f"RANGE BETWEEN {lower} AND 1 PRECEDING)")


def _features(d: Dialect, spec: dict[str, Any]) -> list[str]:
    exps = spec["input_validity"]["currency_exponents"]
    unit_case = "CASE currency " + " ".join(
        f"WHEN {_string(c)} THEN {100 * 10 ** e}" for c, e in exps.items()) + " END"
    amount = d.to_float.format(x="amount")
    cols = [f"LN(1.0 + {amount}) AS transaction_amount_log",
            f"{d.hour} AS transaction_hour", f"{d.day_of_week} AS transaction_day_of_week",
            "CASE WHEN origin_country != destination_country THEN 1 ELSE 0 END "
            "AS is_cross_border_transaction"]
    for name, rule in FEATURE_SPECS.items():
        if name in ("acct_amount_log_ratio_30d", "acct_seconds_since_prev_log",
                    "acct_has_prev", "is_round_amount"):
            continue
        count = _window(d, rule["partition"], rule["window_seconds"], "COUNT(*)")
        cols.append(f"CASE WHEN {count} = 0 THEN 1 ELSE 0 END AS {name}"
                    if name in ("acct_new_beneficiary", "cust_new_device")
                    else f"{count} AS {name}")
    n30 = _window(d, ["account_id"], 2592000, "COUNT(*)")
    s30 = _window(d, ["account_id"], 2592000, "SUM(amount_minor)")
    mean = f"({d.to_float.format(x=s30)} / {n30})"
    cols.append(f"CASE WHEN {n30} = 0 THEN 0.0 ELSE LN({d.to_float.format(x='amount_minor')} "
                f"+ 1.0) - LN({mean} + 1.0) END AS acct_amount_log_ratio_30d")
    prev = _window(d, ["account_id"], None, "MAX(t)")
    gap = d.least.format(a=d.seconds.format(x=f"t - {prev}"), b=str(NO_HISTORY_SECONDS))
    cols.append(f"LN(1.0 + {d.to_float.format(x=f'COALESCE({gap}, {NO_HISTORY_SECONDS})')}) "
                "AS acct_seconds_since_prev_log")
    cols.append(f"CASE WHEN {prev} IS NULL THEN 0 ELSE 1 END AS acct_has_prev")
    cols.append(f"CASE WHEN {d.mod.format(a='amount_minor', b=unit_case)} = 0 THEN 1 ELSE 0 "
                "END AS is_round_amount")
    return cols


def _contributions(d: Dialect, spec: dict[str, Any]) -> list[str]:
    cols = []
    for n in spec["model"]["numeric"]:
        x = d.to_float.format(x=n["name"])
        cols.append(f"{literal(n['coefficient'])} * (({x} - {literal(n['mean'])}) / "
                    f"{literal(n['scale'])}) AS c_{n['name']}")
    for c in spec["model"]["categorical"]:
        arms = " ".join(f"WHEN {_string(v['value'])} THEN {literal(v['coefficient'])}"
                        for v in c["categories"])
        cols.append(f"CASE {c['name']} {arms} ELSE 0.0 END AS c_{c['name']}")
    return cols


def _valid(spec: dict[str, Any]) -> str:
    rules = spec["input_validity"]
    parts = [f"{c} IS NOT NULL" for c in rules["required_non_null"]]
    parts += [f"{c} != ''" for c in rules["non_empty_strings"]]
    parts.append("currency IN (" + ", ".join(_string(c) for c in
                                             rules["currency_exponents"]) + ")")
    return " AND ".join(parts)


def render_select(spec: dict[str, Any], d: Dialect) -> str:
    """One row per source row: identifiers, features, contributions, scores, reason codes."""
    sha = spec_sha256(spec)
    valid = _valid(spec)
    ppm_expr = d.integer_cast.format(x=f"FLOOR((1.0 / (1.0 + EXP(-logit))) * {PPM}.0 + 0.5)")
    if uses_ppm_rule(spec):  # spec v2 (approved Option B): INT64 comparison
        alert_expr = f"{ppm_expr} >= {int(spec['decision']['threshold_ppm'])}"
    else:                    # spec v1 (historical): FLOAT64 comparison
        alert_expr = (f"1.0 / (1.0 + EXP(-logit)) >= "
                      f"{literal(spec['decision']['threshold'])}")
    reasons = spec["reason_codes"]
    terms = " + ".join(["intercept_term"] + [f"c_{f}" for f in MODEL_FEATURES])
    long = "\n    UNION ALL ".join(
        f"SELECT transaction_id, {r['order']} AS feature_order, {_string(r['code'])} AS code, "
        f"c_{r['name']} AS contribution FROM final WHERE alert = 1 AND c_{r['name']} > 0"
        for r in reasons["features"])
    slots = range(1, REASON_SLOTS + 1)
    pad = _string(reasons["padding"][0])
    feature_cols = ", ".join(MODEL_FEATURES)
    contrib_cols = ", ".join(f"c_{f}" for f in MODEL_FEATURES)
    nulls_features = ", ".join(f"NULL AS {f}" for f in MODEL_FEATURES)
    nulls_contrib = ", ".join(f"NULL AS c_{f}" for f in MODEL_FEATURES)
    return f"""-- M9C fraud scoring ({d.name}); model spec {sha}. Generated; do not edit.
WITH src AS (
  SELECT {', '.join(INPUT_COLUMNS)} FROM {d.table.format(name=SOURCE_TABLE)}
),
valid AS (
  SELECT *, {d.event_time} AS t FROM src WHERE {valid}
),
features AS (
  SELECT transaction_id, t, currency, transaction_type, merchant_category, origin_country,
    destination_country, channel,
    {(',' + chr(10) + '    ').join(_features(d, spec))}
  FROM valid
),
terms AS (
  SELECT *, {literal(spec['model']['intercept'])} AS intercept_term,
    {(',' + chr(10) + '    ').join(_contributions(d, spec))}
  FROM features
),
scored AS (
  SELECT *, {terms} AS logit FROM terms
),
final AS (
  SELECT *, 1.0 / (1.0 + EXP(-logit)) AS probability,
    {ppm_expr} AS score_ppm,
    CASE WHEN {alert_expr} THEN 1 ELSE 0 END AS alert
  FROM scored
),
reasons_long AS (
    {long}
),
ranked AS (
  SELECT transaction_id, code, ROW_NUMBER() OVER (
    PARTITION BY transaction_id ORDER BY contribution DESC, feature_order ASC) AS slot
  FROM reasons_long
)
SELECT f.transaction_id, {_string(sha)} AS model_spec_sha256, 1 AS input_valid,
  {feature_cols}, {contrib_cols}, f.logit, f.probability, f.score_ppm, f.alert,
  {', '.join(f"CASE WHEN f.alert = 1 THEN COALESCE(r{i}.code, {pad}) END AS reason_code_{i}"
             for i in slots)}
FROM final AS f
{chr(10).join(f"LEFT JOIN ranked AS r{i} ON r{i}.transaction_id = f.transaction_id "
              f"AND r{i}.slot = {i}" for i in slots)}
UNION ALL
SELECT transaction_id, {_string(sha)} AS model_spec_sha256, 0 AS input_valid,
  {nulls_features}, {nulls_contrib}, NULL AS logit, NULL AS probability, NULL AS score_ppm,
  NULL AS alert, {', '.join(f"NULL AS reason_code_{i}" for i in slots)}
FROM src WHERE NOT ({valid}) OR {' OR '.join(f'{c} IS NULL' for c in INPUT_COLUMNS)}
"""


def render_insert(spec: dict[str, Any]) -> str:
    """GoogleSQL transactional replace of the score table (idempotent for a spec)."""
    select = render_select(spec, GOOGLESQL).split("\n", 1)[1]
    cols = ", ".join(OUTPUT_COLUMNS)
    target = GOOGLESQL.table.format(name=TARGET_TABLE)
    return (f"-- M9C score table refresh; model spec {spec_sha256(spec)}. Generated; do not "
            f"edit.\nBEGIN TRANSACTION;\nDELETE FROM {target} WHERE TRUE;\n"
            f"INSERT INTO {target} ({cols})\nSELECT {cols} FROM (\n{select});\n"
            "COMMIT TRANSACTION;\n")


def render_preflight(spec: dict[str, Any]) -> str:
    """GoogleSQL read-only checks the deployment must pass before scoring."""
    src = GOOGLESQL.table.format(name=SOURCE_TABLE)
    return (f"-- M9C preflight; model spec {spec_sha256(spec)}. Generated; do not edit.\n"
            f"SELECT COUNT(*) AS row_count,\n"
            f"  COUNT(*) - COUNT(DISTINCT transaction_id) AS duplicate_ids,\n"
            f"  COUNTIF(NOT ({_valid(spec)})"
            f" OR {' OR '.join(f'{c} IS NULL' for c in INPUT_COLUMNS)}) AS invalid_rows,\n"
            f"  COUNTIF(MOD(UNIX_MICROS(transaction_timestamp), 1000000) != 0)"
            f" AS sub_second_timestamps\nFROM {src}\n")
