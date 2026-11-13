"""M7C2 strict BigQuery read-back converter and three-table reconciliation (gap G5).

Input: ``tabledata.list`` pages the operator captured with
``formatOptions.useInt64Timestamp=true`` (TIMESTAMP values arrive as exact int64
microseconds, never as floating-point seconds). Output: the table rebuilt as CSV in the M3
dialect, ordered by lineage ``source_row``, which must be **byte-identical** to the packet
file (so its SHA-256 equals the packet SHA-256), plus row-identity and per-currency checks.

Only ``customers``, ``accounts`` and ``curated_transactions`` are in scope. The four marts
are not loaded in M7 and the seven-table comparator in ``reconcile.py`` is unchanged.
Offline and pure: no cloud call, no writes.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta

from financial_crime_ml.deployment.ingestion_evidence import DECIMAL_RE
from financial_crime_ml.deployment.ingestion_packet import EXPECTED_FIELDS, TABLES
from financial_crime_ml.evaluation.manifest import sha256_bytes

PAGE_KEYS = frozenset({"kind", "etag", "totalRows", "pageToken", "rows"})
INT64_RE = re.compile(r"^(0|-?[1-9][0-9]{0,18})$")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ReadbackError(ValueError):
    """The read-back cannot be converted or does not reconcile with the packet."""


def timestamp_from_micros(value: str) -> str:
    """Exact int64 microseconds -> ``YYYY-MM-DDTHH:MM:SSZ``; whole seconds only."""
    if not INT64_RE.fullmatch(value):
        raise ReadbackError("TIMESTAMP is not an int64 microsecond string")
    micros = int(value)
    if micros % 1_000_000:
        raise ReadbackError("TIMESTAMP has sub-second precision; the packet has none")
    try:
        moment = EPOCH + timedelta(seconds=micros // 1_000_000)
    except OverflowError:
        raise ReadbackError("TIMESTAMP out of range") from None
    if not 1 <= moment.year <= 9999:
        raise ReadbackError("TIMESTAMP out of range")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def convert_value(cell: object, field: dict) -> str:
    if not isinstance(cell, dict) or set(cell) != {"v"}:
        raise ReadbackError(f"{field['name']}: cell is not exactly {{'v': ...}}")
    value = cell["v"]
    if not isinstance(value, str):
        raise ReadbackError(f"{field['name']}: NULL or non-string value for a REQUIRED field")
    if field["type"] == "STRING":
        return value
    if field["type"] == "INT64":
        if not INT64_RE.fullmatch(value):
            raise ReadbackError(f"{field['name']}: INT64 is not an exact decimal integer")
        return value
    if field["type"] == "TIMESTAMP":
        return timestamp_from_micros(value)
    raise ReadbackError(f"{field['name']}: unsupported type {field['type']}")


def convert_pages(pages: list[tuple[str, object]], schema: list[dict],
                  expected_rows: int) -> list[list[str]]:
    """``pages`` is ``[(page token used for the request, response), ...]`` in order."""
    if not pages:
        raise ReadbackError("no read-back pages")
    rows: list[list[str]] = []
    total = None
    for index, (token_used, page) in enumerate(pages):
        label = f"page {index + 1}"
        if not isinstance(page, dict) or set(page) - PAGE_KEYS:
            raise ReadbackError(f"{label}: not a tabledata.list page or unreviewed attributes")
        if page.get("kind") != "bigquery#tableDataList":
            raise ReadbackError(f"{label}: kind is not bigquery#tableDataList")
        if not isinstance(page.get("totalRows"), str) or not DECIMAL_RE.fullmatch(
                page["totalRows"]):
            raise ReadbackError(f"{label}: totalRows is not a decimal string")
        if total is None:
            total = page["totalRows"]
        elif page["totalRows"] != total:
            raise ReadbackError(f"{label}: totalRows changed during acquisition")
        previous = "" if index == 0 else pages[index - 1][1].get("pageToken")
        if token_used != previous or (index > 0 and not previous):
            raise ReadbackError(f"{label}: page token chain is broken")
        last = index == len(pages) - 1
        if last and page.get("pageToken"):
            raise ReadbackError("read-back is incomplete: the last page has a pageToken")
        if not last and not page.get("pageToken"):
            raise ReadbackError(f"{label}: pagination ended early")
        page_rows = page.get("rows", [])
        if not isinstance(page_rows, list) or (not page_rows and not last):
            raise ReadbackError(f"{label}: rows missing")
        for row in page_rows:
            if not isinstance(row, dict) or set(row) != {"f"} or not isinstance(
                    row["f"], list) or len(row["f"]) != len(schema):
                raise ReadbackError(f"{label}: row shape differs from the schema")
            rows.append([convert_value(cell, field)
                         for cell, field in zip(row["f"], schema, strict=True)])
    if int(total) != expected_rows or len(rows) != expected_rows:
        raise ReadbackError(f"read-back has {len(rows)} rows (totalRows {total}); "
                            f"the packet has {expected_rows}")
    return rows


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    """M3 dialect: csv.DictWriter defaults with LF line endings."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _money(header: list[str], rows: list[list[str]]) -> dict:
    currency, amount = header.index("currency"), header.index("amount_minor")
    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(row[currency], {"amount_minor": 0, "row_count": 0})
        entry["amount_minor"] += int(row[amount])
        entry["row_count"] += 1
    return dict(sorted(totals.items()))


def reconcile_table(table: str, rows: list[list[str]], packet_content: bytes,
                    manifest: dict) -> dict:
    """Row identities, every value, byte-identical rebuild and (transactions) money."""
    if table not in TABLES:
        raise ReadbackError(f"{table} is not one of the three M7 input tables")
    header = list(EXPECTED_FIELDS[table])
    packet_rows = list(csv.reader(io.StringIO(packet_content.decode("utf-8"), newline=""),
                                  strict=True))
    if packet_rows[0] != header:
        raise ReadbackError(f"{table}: packet header differs from the contract")
    expected = packet_rows[1:]
    key = 0  # the first column is the primary key (customer_id, account_id, transaction_id)
    got_keys = [row[key] for row in rows]
    if len(set(got_keys)) != len(got_keys):
        raise ReadbackError(f"{table}: duplicate primary keys in the read-back")
    want_keys = {row[key] for row in expected}
    missing, extra = want_keys - set(got_keys), set(got_keys) - want_keys
    if missing or extra:
        raise ReadbackError(f"{table}: row identities differ ({len(missing)} missing, "
                            f"{len(extra)} unexpected)")
    by_key = {row[key]: row for row in expected}
    differing = sum(1 for row in rows if row != by_key[row[key]])
    if differing:
        raise ReadbackError(f"{table}: {differing} row(s) differ from the packet")
    order = header.index("source_row")
    rebuilt = _csv_bytes(header, sorted(rows, key=lambda row: int(row[order])))
    entry = manifest["tables"][table]
    if sha256_bytes(rebuilt) != entry["sha256"] or len(rebuilt) != entry["size_bytes"]:
        raise ReadbackError(f"{table}: rebuilt CSV is not byte-identical to the packet file")
    result = {"rebuilt_sha256": sha256_bytes(rebuilt), "row_count": len(rows),
              "row_identities_match": True, "values_match": True,
              "sha256_matches_packet": True}
    if table == "curated_transactions":
        money = _money(header, rows)
        want = {c: {"amount_minor": v["amount_minor"], "row_count": v["row_count"]}
                for c, v in manifest["reconciliation"]["amount_minor_by_currency"].items()}
        if money != want:
            raise ReadbackError("curated_transactions: per-currency totals differ from the "
                                "packet reconciliation")
        result["amount_minor_by_currency"] = money
    return result


def acquisition_stable(before: dict, after: dict) -> list[str]:
    """No writer may run during acquisition: table metadata identical before and after."""
    keys = ("etag", "numRows", "numBytes", "lastModifiedTime")
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return [f"table changed during acquisition: {changed}"] if changed else []
