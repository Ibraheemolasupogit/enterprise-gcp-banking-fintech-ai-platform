-- M5 PROPOSED ONLY: typed tables must already exist. No cloud execution verified.
-- Replace approved PROJECT.DATASET identifiers, retain explicit UTC/currency semantics.
BEGIN TRANSACTION;
DELETE FROM `PROJECT.DATASET.transaction_daily` WHERE TRUE;
DELETE FROM `PROJECT.DATASET.customer_daily` WHERE TRUE;
DELETE FROM `PROJECT.DATASET.account_daily` WHERE TRUE;
DELETE FROM `PROJECT.DATASET.operations_daily` WHERE TRUE;
-- PROPOSED GoogleSQL translation, NOT executed against BigQuery.
-- Replace PROJECT.DATASET with approved identifiers after loading typed inputs.
INSERT INTO `PROJECT.DATASET.transaction_daily` (event_date, currency, transaction_count, total_amount_minor, active_customer_count, active_account_count, first_event_time, last_event_time)
SELECT DATE(transaction_timestamp, 'UTC') AS event_date, currency,
       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor,
       COUNT(DISTINCT customer_id) AS active_customer_count,
       COUNT(DISTINCT account_id) AS active_account_count,
       MIN(transaction_timestamp) AS first_event_time,
       MAX(transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions` GROUP BY event_date, currency;

INSERT INTO `PROJECT.DATASET.customer_daily` (event_date, customer_id, currency, customer_country, transaction_count, total_amount_minor, active_account_count, first_event_time, last_event_time)
SELECT DATE(t.transaction_timestamp, 'UTC') AS event_date, t.customer_id, t.currency,
       c.country AS customer_country, COUNT(*) AS transaction_count,
       SUM(t.amount_minor) AS total_amount_minor,
       COUNT(DISTINCT t.account_id) AS active_account_count,
       MIN(t.transaction_timestamp) AS first_event_time,
       MAX(t.transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions` t JOIN `PROJECT.DATASET.customers` c ON t.customer_id = c.customer_id
GROUP BY event_date, t.customer_id, t.currency, c.country;

INSERT INTO `PROJECT.DATASET.account_daily` (event_date, account_id, currency, customer_id, transaction_count, total_amount_minor, first_event_time, last_event_time)
SELECT DATE(t.transaction_timestamp, 'UTC') AS event_date, t.account_id, t.currency,
       a.customer_id, COUNT(*) AS transaction_count, SUM(t.amount_minor) AS total_amount_minor,
       MIN(t.transaction_timestamp) AS first_event_time,
       MAX(t.transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions` t JOIN `PROJECT.DATASET.accounts` a ON t.account_id = a.account_id
GROUP BY event_date, t.account_id, t.currency, a.customer_id;

INSERT INTO `PROJECT.DATASET.operations_daily` (event_date, currency, transaction_type, channel, origin_country, destination_country, transaction_count, total_amount_minor, first_event_time, last_event_time)
SELECT DATE(transaction_timestamp, 'UTC') AS event_date, currency, transaction_type,
       channel, origin_country, destination_country,
       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor,
       MIN(transaction_timestamp) AS first_event_time,
       MAX(transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions`
GROUP BY event_date, currency, transaction_type, channel, origin_country, destination_country;

COMMIT TRANSACTION;
