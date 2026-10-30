-- PROPOSED GoogleSQL translation, NOT executed against BigQuery.
-- Replace PROJECT.DATASET with approved identifiers after loading typed inputs.
CREATE TABLE `PROJECT.DATASET.transaction_daily` AS
SELECT DATE(transaction_timestamp, 'UTC') AS event_date, currency,
       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor,
       COUNT(DISTINCT customer_id) AS active_customer_count,
       COUNT(DISTINCT account_id) AS active_account_count,
       MIN(transaction_timestamp) AS first_event_time,
       MAX(transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions` GROUP BY event_date, currency;

CREATE TABLE `PROJECT.DATASET.customer_daily` AS
SELECT DATE(t.transaction_timestamp, 'UTC') AS event_date, t.customer_id, t.currency,
       c.country AS customer_country, COUNT(*) AS transaction_count,
       SUM(t.amount_minor) AS total_amount_minor,
       COUNT(DISTINCT t.account_id) AS active_account_count,
       MIN(t.transaction_timestamp) AS first_event_time,
       MAX(t.transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions` t JOIN `PROJECT.DATASET.customers` c ON t.customer_id = c.customer_id
GROUP BY event_date, t.customer_id, t.currency, c.country;

CREATE TABLE `PROJECT.DATASET.account_daily` AS
SELECT DATE(t.transaction_timestamp, 'UTC') AS event_date, t.account_id, t.currency,
       a.customer_id, COUNT(*) AS transaction_count, SUM(t.amount_minor) AS total_amount_minor,
       MIN(t.transaction_timestamp) AS first_event_time,
       MAX(t.transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions` t JOIN `PROJECT.DATASET.accounts` a ON t.account_id = a.account_id
GROUP BY event_date, t.account_id, t.currency, a.customer_id;

CREATE TABLE `PROJECT.DATASET.operations_daily` AS
SELECT DATE(transaction_timestamp, 'UTC') AS event_date, currency, transaction_type,
       channel, origin_country, destination_country,
       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor,
       MIN(transaction_timestamp) AS first_event_time,
       MAX(transaction_timestamp) AS last_event_time
FROM `PROJECT.DATASET.curated_transactions`
GROUP BY event_date, currency, transaction_type, channel, origin_country, destination_country;
