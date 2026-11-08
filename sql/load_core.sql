/* =============================================================================
   load_core.sql  -  populate the CORE layer from fraud.stg_transactions
   Runs as a single transaction: either every core table loads or none does.
   Prerequisite: scripts/02_build_database.py has COPY-ed
                 data/cleaned/transactions_clean.csv into fraud.stg_transactions
   ============================================================================= */
SET search_path TO fraud, public;

BEGIN;

-- Bulk-load pattern: foreign keys on the large tables are dropped for the load and
-- re-added (and fully validated) at the end of this same transaction. One set-based
-- validation per key replaces ~25 million per-row trigger checks.
ALTER TABLE transactions DROP CONSTRAINT transactions_sender_account_fkey,
                         DROP CONSTRAINT transactions_receiver_account_fkey,
                         DROP CONSTRAINT transactions_merchant_category_fkey,
                         DROP CONSTRAINT transactions_device_id_fkey,
                         DROP CONSTRAINT transactions_ip_address_fkey;
ALTER TABLE fraud_labels DROP CONSTRAINT fraud_labels_transaction_id_fkey;
ALTER TABLE account_device_mapping DROP CONSTRAINT account_device_mapping_account_id_fkey,
                                   DROP CONSTRAINT account_device_mapping_device_id_fkey;
ALTER TABLE account_ip_mapping DROP CONSTRAINT account_ip_mapping_account_id_fkey,
                               DROP CONSTRAINT account_ip_mapping_ip_address_fkey;

-- 1. merchants ---------------------------------------------------------------
INSERT INTO merchants (merchant_category, merchant_group)
SELECT DISTINCT merchant_category, merchant_group
FROM stg_transactions;

-- 2. accounts: union of senders and receivers ---------------------------------
WITH events AS (
    SELECT sender_account::BIGINT AS account_id, txn_ts::TIMESTAMP AS ts, 1 AS sent, 0 AS recv,
           sender_persona, (bvn_linked = '1') AS bvn
    FROM stg_transactions
    UNION ALL
    SELECT receiver_account::BIGINT, txn_ts::TIMESTAMP, 0, 1, NULL, NULL
    FROM stg_transactions
)
INSERT INTO accounts (account_id, sender_persona, bvn_linked_any, first_seen_ts, last_seen_ts,
                      sent_txn_count, received_txn_count)
SELECT account_id,
       MAX(sender_persona),                 -- persona is constant per sender (validated in 01_clean_data.py)
       BOOL_OR(bvn),
       MIN(ts), MAX(ts), SUM(sent), SUM(recv)
FROM events
GROUP BY account_id;

-- 3. devices -------------------------------------------------------------------
INSERT INTO devices (device_id, first_seen_ts, last_seen_ts, txn_count, account_count)
SELECT device_id, MIN(txn_ts::TIMESTAMP), MAX(txn_ts::TIMESTAMP), COUNT(*), COUNT(DISTINCT sender_account)
FROM stg_transactions
GROUP BY device_id;

-- 4. ip_addresses (network_class filled by fraud_network.sql B1) ---------------
INSERT INTO ip_addresses (ip_address, network_prefix16, first_seen_ts, last_seen_ts, txn_count, account_count)
SELECT ip_address::INET,
       set_masklen(ip_address::INET, 16)::CIDR,
       MIN(txn_ts::TIMESTAMP), MAX(txn_ts::TIMESTAMP), COUNT(*), COUNT(DISTINCT sender_account)
FROM stg_transactions
GROUP BY ip_address;

-- 5. transactions --------------------------------------------------------------
INSERT INTO transactions
SELECT transaction_id,
       txn_ts::TIMESTAMP,
       sender_account::BIGINT,
       receiver_account::BIGINT,
       transaction_type,
       merchant_category,
       location,
       device_channel,
       payment_channel,
       device_id,
       ip_address::INET,
       amount_ngn::NUMERIC(14,2),
       (src_new_device_flag = '1'),
       (bvn_linked = '1'),
       NULLIF(src_spending_deviation_score, '')::NUMERIC(6,2),
       NULLIF(src_velocity_score, '')::SMALLINT,
       NULLIF(src_geo_anomaly_score, '')::NUMERIC(4,2),
       NULLIF(src_time_since_last_txn, '')::DOUBLE PRECISION,
       txn_hour::SMALLINT,
       (is_self_transfer = '1')
FROM stg_transactions;

-- 6. fraud_labels (source ground truth, 1:1) ------------------------------------
INSERT INTO fraud_labels (transaction_id, is_fraud, fraud_type)
SELECT transaction_id, (is_fraud = '1'), NULLIF(fraud_type, '')
FROM stg_transactions;

-- 7. bridge tables ---------------------------------------------------------------
INSERT INTO account_device_mapping
SELECT sender_account, device_id, MIN(txn_ts), MAX(txn_ts), COUNT(*)
FROM transactions GROUP BY sender_account, device_id;

INSERT INTO account_ip_mapping
SELECT sender_account, ip_address, MIN(txn_ts), MAX(txn_ts), COUNT(*)
FROM transactions GROUP BY sender_account, ip_address;

-- re-add and validate every foreign key (fails the whole load if any orphan exists)
ALTER TABLE transactions
  ADD CONSTRAINT transactions_sender_account_fkey    FOREIGN KEY (sender_account)    REFERENCES accounts(account_id),
  ADD CONSTRAINT transactions_receiver_account_fkey  FOREIGN KEY (receiver_account)  REFERENCES accounts(account_id),
  ADD CONSTRAINT transactions_merchant_category_fkey FOREIGN KEY (merchant_category) REFERENCES merchants(merchant_category),
  ADD CONSTRAINT transactions_device_id_fkey         FOREIGN KEY (device_id)         REFERENCES devices(device_id),
  ADD CONSTRAINT transactions_ip_address_fkey        FOREIGN KEY (ip_address)        REFERENCES ip_addresses(ip_address);
ALTER TABLE fraud_labels ADD CONSTRAINT fraud_labels_transaction_id_fkey FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id);
ALTER TABLE account_device_mapping
  ADD CONSTRAINT account_device_mapping_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  ADD CONSTRAINT account_device_mapping_device_id_fkey  FOREIGN KEY (device_id)  REFERENCES devices(device_id);
ALTER TABLE account_ip_mapping
  ADD CONSTRAINT account_ip_mapping_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  ADD CONSTRAINT account_ip_mapping_ip_address_fkey FOREIGN KEY (ip_address) REFERENCES ip_addresses(ip_address);

COMMIT;

-- 8. indexes that support the detection engine -------------------------------------
CREATE INDEX ix_txn_sender_ts    ON transactions (sender_account, txn_ts) INCLUDE (receiver_account, amount_ngn);
CREATE INDEX ix_txn_receiver_ts  ON transactions (receiver_account, txn_ts);
CREATE INDEX ix_txn_device_ts    ON transactions (device_id, txn_ts);
CREATE INDEX ix_txn_ip_ts        ON transactions (ip_address, txn_ts);
CREATE INDEX ix_txn_ts           ON transactions (txn_ts);
CREATE INDEX ix_label_fraud      ON fraud_labels (transaction_id) WHERE is_fraud;
CREATE INDEX ix_adm_device       ON account_device_mapping (device_id);
CREATE INDEX ix_aim_ip           ON account_ip_mapping (ip_address);

DROP TABLE stg_transactions;   -- staging no longer needed

ANALYZE;
