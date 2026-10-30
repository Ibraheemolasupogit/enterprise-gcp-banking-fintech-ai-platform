"""Financial foundation v1: explicit snapshot and observation contracts."""

from financial_crime_ml.data_generation.v2.contracts import SCHEMAS, parse_utc

VERSION = "1.0.0"
SOURCE_VERSION = "2.0.0"
TABLES = ("customers", "accounts", "beneficiaries", "devices", "transactions")
ENTITY_SCHEMAS = {name: dict(SCHEMAS[name]) for name in TABLES}
EVENT_FIELDS = {
    "customers": "onboarded_at", "accounts": "opened_at",
    "beneficiaries": "first_seen_at", "devices": "first_seen_at",
    "transactions": "transaction_timestamp",
}
LINEAGE_FIELDS = ("source_id", "source_time", "ingestion_time", "source_row", "source_sha256")
CURATED_FIELDS = (*ENTITY_SCHEMAS["transactions"], "customer_country", *LINEAGE_FIELDS)


def validate_snapshot(source_id: str, source_time: str, ingestion_time: str) -> None:
    if not isinstance(source_id, str) or not source_id or len(source_id) > 128:
        raise ValueError("source_id must be a nonempty string of at most 128 characters")
    if parse_utc(ingestion_time) < parse_utc(source_time):
        raise ValueError("ingestion_time must be at or after source_time")


def description() -> dict:
    return {
        "version": VERSION, "source_contract": SOURCE_VERSION, "schemas": ENTITY_SCHEMAS,
        "event_fields": EVENT_FIELDS, "lineage_fields": LINEAGE_FIELDS,
        "curated_fields": CURATED_FIELDS,
        "time_semantics": "entity/event time <= source snapshot time <= ingestion receipt time",
        "money": "positive integer minor units; account currency; aggregate by currency only",
        "duplicates": "quarantine every occurrence of a duplicated primary key within a snapshot",
        "snapshot": "complete independent batch; no upsert or merge across sources",
    }
