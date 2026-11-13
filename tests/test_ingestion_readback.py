"""M7C2 G5: strict tabledata.list conversion and three-table reconciliation with the packet."""

from __future__ import annotations

import calendar
import copy
import csv
import io
import time

import pytest
from m7c2_support import build_fixture_packet

from financial_crime_ml.deployment.ingestion_readback import (
    ReadbackError,
    acquisition_stable,
    convert_pages,
    reconcile_table,
    timestamp_from_micros,
)

TABLES = ("customers", "accounts", "curated_transactions")


@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    return build_fixture_packet(tmp_path_factory.mktemp("m7c2-readback"))


def schema(packet, table):
    return packet["manifest"]["load_jobs"][table]["configuration"]["load"]["schema"]["fields"]


def content(packet, table) -> bytes:
    return (packet["packet_dir"] / "data" / f"{table}.csv").read_bytes()


def packet_rows(packet, table):
    return list(csv.reader(io.StringIO(content(packet, table).decode(), newline="")))[1:]


def to_cells(row, fields):
    cells = []
    for value, field in zip(row, fields, strict=True):
        if field["type"] == "TIMESTAMP":
            value = str(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
                        * 1_000_000)
        cells.append({"v": value})
    return {"f": cells}


def pages_for(packet, table, size=3, rows=None):
    fields = schema(packet, table)
    rows = list(reversed(packet_rows(packet, table))) if rows is None else rows
    pages, token = [], ""
    for start in range(0, len(rows), size):
        page = {"kind": "bigquery#tableDataList", "etag": "e", "totalRows": str(len(rows)),
                "rows": [to_cells(r, fields) for r in rows[start:start + size]]}
        if start + size < len(rows):
            page["pageToken"] = f"t{start + size}"
        pages.append((token, page))
        token = page.get("pageToken", "")
    return pages


def convert(packet, table, pages, rows=None):
    count = len(packet_rows(packet, table)) if rows is None else rows
    return convert_pages(pages, schema(packet, table), count)


# ------------------------------------------------------------------ happy path
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("size", [1, 3, 1000])
def test_readback_rebuilds_the_packet_byte_for_byte(packet, table, size):
    rows = convert(packet, table, pages_for(packet, table, size))
    result = reconcile_table(table, rows, content(packet, table), packet["manifest"])
    assert result["sha256_matches_packet"] is True
    assert result["rebuilt_sha256"] == packet["manifest"]["tables"][table]["sha256"]
    assert result["row_count"] == packet["manifest"]["tables"][table]["row_count"]


def test_money_is_reconciled_per_currency(packet):
    rows = convert(packet, "curated_transactions", pages_for(packet, "curated_transactions"))
    result = reconcile_table("curated_transactions", rows, content(packet,
                             "curated_transactions"), packet["manifest"])
    want = {c: {"amount_minor": v["amount_minor"], "row_count": v["row_count"]}
            for c, v in packet["manifest"]["reconciliation"]["amount_minor_by_currency"]
            .items()}
    assert result["amount_minor_by_currency"] == want


def test_money_mismatch_is_rejected_independently(packet):
    rows = convert(packet, "curated_transactions", pages_for(packet, "curated_transactions"))
    manifest = copy.deepcopy(packet["manifest"])
    currency = next(iter(manifest["reconciliation"]["amount_minor_by_currency"]))
    manifest["reconciliation"]["amount_minor_by_currency"][currency]["amount_minor"] += 1
    with pytest.raises(ReadbackError, match="per-currency"):
        reconcile_table("curated_transactions", rows, content(packet, "curated_transactions"),
                        manifest)


# ------------------------------------------------------------------ timestamps
def test_timestamp_conversion_is_exact():
    assert timestamp_from_micros("0") == "1970-01-01T00:00:00Z"
    assert timestamp_from_micros("1735689600000000") == "2025-01-01T00:00:00Z"
    assert timestamp_from_micros("-1000000") == "1969-12-31T23:59:59Z"
    for bad in ("1.7356896E9", "1735689600", "1735689600000001", "01735689600000000",
                "", " 0", "1e6", "9" * 30, "-0"):
        with pytest.raises(ReadbackError):
            timestamp_from_micros(bad)


# ------------------------------------------------------------------ page strictness
def mutate_page(packet, mutation, table="accounts"):
    pages = pages_for(packet, table, 2)
    mutation(pages)
    return pages


# Index of the first TIMESTAMP column of accounts (opened_at).
TS = 3

PAGE_MUTATIONS = {
    "float timestamp": lambda p: p[0][1]["rows"][0]["f"][TS].update(v="1.7356896E9"),
    "sub-second timestamp": lambda p: p[0][1]["rows"][0]["f"][TS].update(
        v=str(int(p[0][1]["rows"][0]["f"][TS]["v"]) + 1)),
    "null value": lambda p: p[0][1]["rows"][0]["f"][0].update(v=None),
    "number value": lambda p: p[0][1]["rows"][0]["f"][0].update(v=5),
    "extra cell key": lambda p: p[0][1]["rows"][0]["f"][0].update(x=1),
    "extra row key": lambda p: p[0][1]["rows"][0].update(x=1),
    "short row": lambda p: p[0][1]["rows"][0]["f"].pop(),
    "long row": lambda p: p[0][1]["rows"][0]["f"].append({"v": "x"}),
    "row not object": lambda p: p[0][1]["rows"].__setitem__(0, ["a"]),
    "int64 leading zero": lambda p: p[0][1]["rows"][0]["f"].__setitem__(
        next(i for i, c in enumerate(p[0][1]["rows"][0]["f"]) if c["v"].isdigit()
             and len(c["v"]) < 10), {"v": "007"}),
    "kind": lambda p: p[0][1].update(kind="bigquery#table"),
    "unknown page key": lambda p: p[1][1].update(schema={}),
    "total rows changed": lambda p: p[1][1].update(totalRows="99"),
    "total rows not text": lambda p: [page.update(totalRows=6) for _, page in p],
    "total rows wrong": lambda p: [page.update(totalRows="7") for _, page in p],
    "first token not empty": lambda p: p.__setitem__(0, ("t0", p[0][1])),
    "token chain broken": lambda p: p.__setitem__(1, ("other", p[1][1])),
    "last page has token": lambda p: p[-1][1].update(pageToken="more"),
    "middle page lacks token": lambda p: p[0][1].pop("pageToken"),
    "middle page empty": lambda p: p[0][1].update(rows=[]),
    "missing page": lambda p: p.pop(1),
    "duplicated page": lambda p: p.insert(1, p[0]),
    "rows not a list": lambda p: p[-1][1].update(rows={}),
}


@pytest.mark.parametrize("name", sorted(PAGE_MUTATIONS))
def test_page_mutations_are_rejected(packet, name):
    with pytest.raises(ReadbackError):
        convert(packet, "accounts", mutate_page(packet, PAGE_MUTATIONS[name]))


def test_no_pages_is_rejected(packet):
    with pytest.raises(ReadbackError):
        convert(packet, "accounts", [])


# ------------------------------------------------------------------ reconciliation
def rows_for(packet, table):
    return convert(packet, table, pages_for(packet, table))


def change(row_index, column, value):
    def apply(rows):
        rows[row_index][column] = value
    return apply


@pytest.mark.parametrize("table", TABLES)
def test_changed_values_are_rejected(packet, table):
    rows = rows_for(packet, table)
    rows[0][1] = rows[0][1] + "x"
    with pytest.raises(ReadbackError, match="differ"):
        reconcile_table(table, rows, content(packet, table), packet["manifest"])


@pytest.mark.parametrize("table", TABLES)
def test_missing_and_substituted_rows_are_rejected(packet, table):
    rows = rows_for(packet, table)
    rows[0] = list(rows[0])
    rows[0][0] = "SUBSTITUTE"
    with pytest.raises(ReadbackError, match="identities"):
        reconcile_table(table, rows, content(packet, table), packet["manifest"])
    rows = rows_for(packet, table)
    rows[0] = list(rows[1])
    with pytest.raises(ReadbackError, match="duplicate"):
        reconcile_table(table, rows, content(packet, table), packet["manifest"])
    with pytest.raises(ReadbackError, match="identities"):
        reconcile_table(table, rows_for(packet, table)[1:], content(packet, table),
                        packet["manifest"])


def test_compensating_amount_changes_are_rejected(packet):
    """Totals per currency unchanged, but two rows altered: row-level comparison catches it."""
    table = "curated_transactions"
    rows = rows_for(packet, table)
    header = list(csv.reader(io.StringIO(content(packet, table).decode())))[0]
    amount, currency = header.index("amount_minor"), header.index("currency")
    first = rows[0]
    second = next(r for r in rows[1:] if r[currency] == first[currency])
    first[amount] = str(int(first[amount]) + 1)
    second[amount] = str(int(second[amount]) - 1)
    with pytest.raises(ReadbackError, match="differ"):
        reconcile_table(table, rows, content(packet, table), packet["manifest"])


def test_only_the_three_input_tables_are_in_scope(packet):
    with pytest.raises(ReadbackError, match="not one of"):
        reconcile_table("customer_daily", [], b"", packet["manifest"])


def test_byte_rebuild_is_checked_against_the_manifest(packet):
    manifest = copy.deepcopy(packet["manifest"])
    manifest["tables"]["customers"]["sha256"] = "0" * 64
    with pytest.raises(ReadbackError, match="byte-identical"):
        reconcile_table("customers", rows_for(packet, "customers"),
                        content(packet, "customers"), manifest)


def test_acquisition_must_be_stable():
    before = {"etag": "a", "numRows": "4", "numBytes": "10", "lastModifiedTime": "1"}
    assert acquisition_stable(before, dict(before)) == []
    for key in before:
        assert acquisition_stable(before, {**before, key: "changed"})
