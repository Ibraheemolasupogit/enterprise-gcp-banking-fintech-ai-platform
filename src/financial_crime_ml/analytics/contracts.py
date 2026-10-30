"""Analytics v1 grains and proposed BigQuery target fields (not cloud-tested)."""

VERSION = "1.0.0"
GRAINS = {
    "transaction_daily": ("event_date", "currency"),
    "customer_daily": ("event_date", "customer_id", "currency"),
    "account_daily": ("event_date", "account_id", "currency"),
    "operations_daily": ("event_date", "currency", "transaction_type", "channel",
                         "origin_country", "destination_country"),
}
COLUMNS = {
    "transaction_daily": (*GRAINS["transaction_daily"], "transaction_count", "total_amount_minor",
                          "active_customer_count", "active_account_count",
                          "first_event_time", "last_event_time"),
    "customer_daily": (*GRAINS["customer_daily"], "customer_country", "transaction_count",
                       "total_amount_minor", "active_account_count",
                       "first_event_time", "last_event_time"),
    "account_daily": (*GRAINS["account_daily"], "customer_id", "transaction_count",
                      "total_amount_minor", "first_event_time", "last_event_time"),
    "operations_daily": (*GRAINS["operations_daily"], "transaction_count", "total_amount_minor",
                         "first_event_time", "last_event_time"),
}


def bigquery_schemas() -> dict:
    def field_type(field):
        if field == "event_date":
            return "DATE"
        if field.endswith("_time"):
            return "TIMESTAMP"
        if field.endswith("_count") or field == "total_amount_minor":
            return "INT64"
        return "STRING"

    return {"contract_version": VERSION, "status": "proposed_not_cloud_tested", "tables": {
        table: {"grain": grain, "fields": [
            {"name": field, "type": field_type(field), "mode": "REQUIRED"}
            for field in COLUMNS[table]]}
        for table, grain in GRAINS.items()
    }}
