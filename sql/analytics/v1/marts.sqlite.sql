-- Contract 1.0.0. Event dates are UTC; every grain retains currency.
CREATE TABLE transaction_daily AS
SELECT substr(transaction_timestamp, 1, 10) AS event_date, currency,
       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor,
       COUNT(DISTINCT customer_id) AS active_customer_count,
       COUNT(DISTINCT account_id) AS active_account_count,
       MIN(transaction_timestamp) AS first_event_time,
       MAX(transaction_timestamp) AS last_event_time
FROM curated_transactions GROUP BY event_date, currency;

CREATE TABLE customer_daily AS
SELECT substr(t.transaction_timestamp, 1, 10) AS event_date, t.customer_id, t.currency,
       c.country AS customer_country, COUNT(*) AS transaction_count,
       SUM(t.amount_minor) AS total_amount_minor,
       COUNT(DISTINCT t.account_id) AS active_account_count,
       MIN(t.transaction_timestamp) AS first_event_time,
       MAX(t.transaction_timestamp) AS last_event_time
FROM curated_transactions t JOIN customers c ON t.customer_id = c.customer_id
GROUP BY event_date, t.customer_id, t.currency, c.country;

CREATE TABLE account_daily AS
SELECT substr(t.transaction_timestamp, 1, 10) AS event_date, t.account_id, t.currency,
       a.customer_id, COUNT(*) AS transaction_count, SUM(t.amount_minor) AS total_amount_minor,
       MIN(t.transaction_timestamp) AS first_event_time,
       MAX(t.transaction_timestamp) AS last_event_time
FROM curated_transactions t JOIN accounts a ON t.account_id = a.account_id
GROUP BY event_date, t.account_id, t.currency, a.customer_id;

CREATE TABLE operations_daily AS
SELECT substr(transaction_timestamp, 1, 10) AS event_date, currency, transaction_type,
       channel, origin_country, destination_country,
       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor,
       MIN(transaction_timestamp) AS first_event_time,
       MAX(transaction_timestamp) AS last_event_time
FROM curated_transactions
GROUP BY event_date, currency, transaction_type, channel, origin_country, destination_country;
