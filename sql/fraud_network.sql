/* =============================================================================
   fraud_network.sql  -  SQL fraud-ring detection engine
   -----------------------------------------------------------------------------
   Target   : PostgreSQL 14+ (developed and executed on PostgreSQL 16.2)
   Input    : core layer from sql/schema.sql + sql/load_core.sql
              (5,000,000 transactions, 899,814 accounts - see README)
   Output   : derived layer tables + analytical result tables + investigation views

   Sections
     0  Setup: parameters, flag catalogue, account label summary
     A  Shared-device linkage (same device, different accounts, <= 1 hour)
     B  Shared-IP linkage + legitimate shared-network classification
     C  Transaction velocity (window functions, non-overlapping episodes)
     D  Recursive money-flow chains (A -> B -> C -> D within 24 hours)
     E  Explainable behavioural flags + rule-based transaction score
     F  Flag / label association: fraud rate, lift, relative risk (95% CI)
     G  Fraud-ring detection: linkage graph, connected components, ring scoring
     H  Ranking & investigation views, investigation case queue
     I  Fraud-rule evaluation (confusion matrices, threshold sweeps)
     K  KPI summary

   Conventions
     * fraud_labels.is_fraud is the SOURCE ground-truth label. Everything produced
       here (flags, scores, tiers, rings, cases) is RULE OUTPUT and never
       overwrites or is presented as a label.
     * "Fraud-labelled account" = an account that SENT >= 1 transaction labelled
       is_fraud. Under account takeover this is usually the VICTIM's account,
       not the fraudster - it is evidence of compromise, not of guilt.
     * Lines starting with "-- >>> STEP:" are split points for
       scripts/02_build_database.py (timing / resume). The file also runs
       unchanged in psql:  psql -f sql/fraud_network.sql
   ============================================================================= */
SET search_path TO fraud, public;
SET work_mem = '256MB';

-- >>> STEP: 0_setup ------------------------------------------------------------
-- Small helper so every threshold is read from one table (explainability / tuning).
CREATE OR REPLACE FUNCTION fraud.p(name TEXT) RETURNS NUMERIC
LANGUAGE sql STABLE AS $$ SELECT param_value FROM fraud.engine_parameters WHERE param_name = name $$;

TRUNCATE investigation_cases, account_risk_profile, fraud_ring_membership, fraud_rings,
         money_flow_paths, account_links, transaction_risk_scores, suspicious_activity_flags,
         flag_definitions, engine_parameters;

INSERT INTO engine_parameters (param_name, param_value, unit, rationale) VALUES
 ('device_window_seconds',     3600, 'seconds',      'Brief: accounts using the same device within one hour'),
 ('ip_window_seconds',          600, 'seconds',      'Two different accounts transacting from one IP within 10 minutes = concurrent use'),
 ('ip_high_density_accounts',    50, 'accounts',     'IPs serving > 50 distinct accounts behave like carrier-grade NAT / shared gateways: treated as legitimate shared networks and excluded from ring edges'),
 ('velocity_window_seconds',    600, 'seconds',      'Brief: 10-minute velocity window'),
 ('velocity_spec_threshold',      5, 'transactions', 'Brief: MORE THAN 5 transactions in 10 minutes'),
 ('velocity_cal_10m',             2, 'transactions', 'Calibrated flag: >= 2 transactions in a trailing 10 minutes (brief threshold never occurs - see C5)'),
 ('velocity_cal_1h',              3, 'transactions', 'Calibrated flag: >= 3 transactions in a trailing hour'),
 ('flow_window_hours',           24, 'hours',        'Brief: whole money chain must complete within 24h of its first hop'),
 ('flow_max_depth',               6, 'transactions', 'Recursion guard: at most 6 transfers (7 accounts) per chain'),
 ('fast_flow_minutes',           60, 'minutes',      'Funds forwarded within 60 minutes of receipt = fast pass-through'),
 ('high_value_percentile',     0.95, 'quantile',     'High value = top 5% of transaction amounts'),
 ('amount_spike_multiple',        3, 'x',            'Amount >= 3x the sender''s trailing average (requires >= 2 prior transactions)'),
 ('multi_beneficiary_24h',        3, 'receivers',    '>= 3 distinct receivers within the trailing 24 hours'),
 ('night_start_hour',             0, 'hour',         'Unusual-hour window start (inclusive)'),
 ('night_end_hour',               4, 'hour',         'Unusual-hour window end (inclusive)'),
 ('usd_ngn_rate',               750, 'NGN per USD',  'ASSUMPTION used only to translate the $500 rule threshold (approx. official rate after the June-2023 FX unification); sensitivity covered by threshold sweep'),
 ('tier_critical',               65, 'score',        'Transaction score >= 65 -> CRITICAL'),
 ('tier_high',                   50, 'score',        'Transaction score 50-64 -> HIGH. Sized to review capacity: HIGH+CRITICAL ~ 38k txns = ~100 manual reviews/day over the 393-day period'),
 ('tier_medium',                 25, 'score',        'Transaction score 25-49 -> MEDIUM (below -> LOW)'),
 ('ring_tier_critical',          60, 'score',        'Ring score >= 60 -> CRITICAL'),
 ('ring_tier_high',              40, 'score',        'Ring score 40-59 -> HIGH'),
 ('ring_tier_medium',            25, 'score',        'Ring score 25-39 -> MEDIUM = potential fraud ring (below -> LOW linked group)'),
 ('ring_min_link_types',          2, 'link types',   'Corroboration rule: a group linked by only ONE evidence type is never above LOW, whatever its score');

-- A-priori weights (analyst judgement BEFORE looking at label performance).
-- Section F measures how well each one actually separates labelled fraud.
INSERT INTO flag_definitions (flag_code, flag_family, weight, description, data_basis) VALUES
 ('SHARED_DEVICE_1H',      'NETWORK',    25, 'Same device_id used by a different sender account within +/- 1 hour', 'DERIVED'),
 ('SHARED_IP_10M',         'NETWORK',    15, 'Same IP used by a different sender within +/- 10 minutes (IP is NOT a high-density shared network)', 'DERIVED'),
 ('PRIOR_FRAUD_LINK',      'NETWORK',    20, 'Receiver, or a device/IP-linked account, had a source fraud label strictly BEFORE this transaction', 'DERIVED'),
 ('HIGH_VELOCITY',         'VELOCITY',   15, '>= 2 transactions in trailing 10 min or >= 3 in trailing 1 h by the same sender', 'DERIVED'),
 ('MULTI_BENEFICIARY_24H', 'VELOCITY',   10, '>= 3 distinct receivers paid by the sender within trailing 24 hours', 'DERIVED'),
 ('NEW_DEVICE_SRC',        'DEVICE',     15, 'Source field new_device_transaction = True (publisher-provided, not reproducible from device_id history)', 'SOURCE_FIELD'),
 ('FIRST_DEVICE_USE',      'DEVICE',      0, 'Derived: first time this sender uses this device_id, sender has earlier history (informational - device ids are near-unique per txn)', 'DERIVED'),
 ('HIGH_VALUE',            'AMOUNT',     15, 'Amount in the top 5% of all transactions', 'DERIVED'),
 ('AMOUNT_SPIKE',          'AMOUNT',     10, 'Amount >= 3x the sender''s trailing average (>= 2 prior transactions)', 'DERIVED'),
 ('UNUSUAL_HOUR',          'TIMING',      5, 'Transaction between 00:00 and 04:59', 'DERIVED'),
 ('RAPID_PASS_THROUGH',    'MONEY_FLOW', 20, 'Sender forwards funds within 24 h of receiving funds (money-flow hop)', 'DERIVED'),
 ('SHARED_NETWORK_IP',     'CONTEXT',     0, 'Transaction from a high-density shared IP (> 50 accounts) - context only, treated as legitimate shared network', 'DERIVED');

-- Account-level view of the SOURCE label (used for "known fraud" counts and,
-- with first_fraud_ts, for strictly-prior connections without look-ahead leakage).
DROP TABLE IF EXISTS account_label_summary;
CREATE TABLE account_label_summary AS
SELECT t.sender_account                         AS account_id,
       COUNT(*)                                 AS sent_txn_count,
       COUNT(*) FILTER (WHERE l.is_fraud)       AS fraud_txn_count,
       BOOL_OR(l.is_fraud)                      AS has_fraud_label,
       MIN(t.txn_ts) FILTER (WHERE l.is_fraud)  AS first_fraud_ts
FROM transactions t
JOIN fraud_labels l USING (transaction_id)
GROUP BY t.sender_account;
ALTER TABLE account_label_summary ADD PRIMARY KEY (account_id);


/* =============================================================================
   A. SHARED DEVICE LINKAGE
   ---------------------------------------------------------------------------
   A shared device is evidence of a relationship, not proof of fraud: families,
   shared kiosks and agent-banking POS terminals all legitimately share devices.
   A3 tests whether observed sharing exceeds what pure chance would produce.
   ============================================================================= */
-- >>> STEP: A1_device_pairs
-- Every unordered pair of transactions on the same device by DIFFERENT sender
-- accounts no more than one hour apart. The ordering predicate
-- (b.txn_ts, b.transaction_id) > (a.txn_ts, a.transaction_id) emits each pair once.
DROP TABLE IF EXISTS device_linkage_pairs;
CREATE TABLE device_linkage_pairs AS
SELECT a.device_id,
       a.transaction_id AS txn_a,   b.transaction_id AS txn_b,
       a.sender_account AS account_a, b.sender_account AS account_b,
       a.txn_ts         AS ts_a,    b.txn_ts         AS ts_b,
       ROUND(EXTRACT(EPOCH FROM b.txn_ts - a.txn_ts), 1) AS gap_seconds
FROM transactions a
JOIN transactions b
  ON  b.device_id      = a.device_id
  AND b.sender_account <> a.sender_account
  AND b.txn_ts BETWEEN a.txn_ts AND a.txn_ts + make_interval(secs => (SELECT p('device_window_seconds'))::float8)
  AND (b.txn_ts, b.transaction_id) > (a.txn_ts, a.transaction_id);
CREATE INDEX ON device_linkage_pairs (txn_a);
CREATE INDEX ON device_linkage_pairs (txn_b);

-- >>> STEP: A2_device_summary
-- One row per device with in-window linkage: accounts, timing, label exposure.
DROP TABLE IF EXISTS device_linkage_summary;
CREATE TABLE device_linkage_summary AS
WITH sides AS (
    SELECT device_id, account_a AS account_id, ts_a AS ts, txn_a AS txn FROM device_linkage_pairs
    UNION
    SELECT device_id, account_b, ts_b, txn_b FROM device_linkage_pairs
), gaps AS (
    SELECT device_id, COUNT(*) AS linked_pairs, MIN(gap_seconds) AS min_gap_seconds,
           MAX(gap_seconds) AS max_gap_seconds, ROUND(AVG(gap_seconds), 1) AS avg_gap_seconds
    FROM device_linkage_pairs GROUP BY device_id
)
SELECT s.device_id,
       COUNT(DISTINCT s.account_id)                                        AS linked_accounts,
       string_agg(DISTINCT s.account_id::TEXT, ', ')                       AS account_ids,
       MIN(s.ts)                                                           AS first_observed_ts,
       MAX(s.ts)                                                           AS last_observed_ts,
       g.linked_pairs, g.min_gap_seconds, g.max_gap_seconds, g.avg_gap_seconds,
       d.account_count                                                     AS all_time_accounts,
       d.txn_count                                                         AS all_time_txns,
       COUNT(DISTINCT s.account_id) FILTER (WHERE als.has_fraud_label)     AS fraud_labelled_accounts,
       COUNT(DISTINCT s.txn)        FILTER (WHERE l.is_fraud)              AS fraud_txns_in_window
FROM sides s
JOIN gaps g USING (device_id)
JOIN devices d USING (device_id)
JOIN fraud_labels l ON l.transaction_id = s.txn
LEFT JOIN account_label_summary als ON als.account_id = s.account_id
GROUP BY s.device_id, g.linked_pairs, g.min_gap_seconds, g.max_gap_seconds, g.avg_gap_seconds,
         d.account_count, d.txn_count;

-- >>> STEP: A3_device_distribution
-- "Unusually high connectivity" needs a baseline. device_id values are 'D' + 7
-- digits; if 5M transactions drew device ids at random from the observed id
-- space, the number of transactions per device would be Poisson(lambda).
-- Observed counts close to expected = sharing explained by chance (hash collision).
DROP TABLE IF EXISTS device_connectivity_distribution;
CREATE TABLE device_connectivity_distribution AS
WITH space AS (
    SELECT (MAX(substr(device_id, 2)::INT) - MIN(substr(device_id, 2)::INT) + 1)::NUMERIC AS id_space,
           (SELECT COUNT(*) FROM transactions)::NUMERIC                             AS n_txn
    FROM devices
), obs AS (
    SELECT d.txn_count AS k,
           COUNT(*)                                  AS observed_devices,
           SUM(d.account_count)                      AS accounts_on_devices,
           ROUND(AVG(d.account_count), 3)            AS avg_accounts_per_device
    FROM devices d GROUP BY d.txn_count
), fr AS (
    SELECT d.txn_count AS k, ROUND(100.0 * AVG(l.is_fraud::INT), 3) AS fraud_rate_pct
    FROM transactions t JOIN devices d USING (device_id) JOIN fraud_labels l USING (transaction_id)
    GROUP BY d.txn_count
)
SELECT o.k AS txns_per_device,
       o.observed_devices,
       ROUND(s.id_space * EXP(-(s.n_txn / s.id_space)) * POWER(s.n_txn / s.id_space, o.k) / factorial(o.k)) AS expected_devices_if_random,
       ROUND(o.observed_devices / NULLIF(s.id_space * EXP(-(s.n_txn / s.id_space)) * POWER(s.n_txn / s.id_space, o.k) / factorial(o.k), 0), 3) AS observed_to_expected,
       o.avg_accounts_per_device,
       fr.fraud_rate_pct,
       s.id_space, ROUND(s.n_txn / s.id_space, 4) AS lambda
FROM obs o JOIN fr USING (k) CROSS JOIN space s;


/* =============================================================================
   B. SHARED IP LINKAGE
   ---------------------------------------------------------------------------
   Mobile carriers place thousands of subscribers behind one public IP (CGNAT),
   so IP sharing is normal on mobile networks. Each IP is classified by how
   many distinct accounts it serves; only low/moderate-density IPs contribute
   ring edges.
   ============================================================================= */
-- >>> STEP: B1_ip_class
UPDATE ip_addresses
SET network_class = CASE
        WHEN account_count = 1                              THEN 'SINGLE_ACCOUNT'
        WHEN account_count <= 10                            THEN 'SMALL_SHARED'
        WHEN account_count <= (SELECT p('ip_high_density_accounts')) THEN 'MODERATE_SHARED'
        ELSE 'HIGH_DENSITY_SHARED' END;

-- >>> STEP: B2_ip_pairs
-- Concurrent use: two different sender accounts on one IP within 10 minutes.
-- Nested-loop + (ip_address, txn_ts) index keeps this a 5M-probe range scan
-- instead of a per-IP cartesian product on high-density IPs.
SET enable_hashjoin = off; SET enable_mergejoin = off;
DROP TABLE IF EXISTS ip_linkage_pairs;
CREATE TABLE ip_linkage_pairs AS
SELECT a.ip_address,
       a.transaction_id AS txn_a,   b.transaction_id AS txn_b,
       a.sender_account AS account_a, b.sender_account AS account_b,
       a.txn_ts         AS ts_a,    b.txn_ts         AS ts_b,
       ROUND(EXTRACT(EPOCH FROM b.txn_ts - a.txn_ts), 1) AS gap_seconds
FROM transactions a
JOIN transactions b
  ON  b.ip_address     = a.ip_address
  AND b.sender_account <> a.sender_account
  AND b.txn_ts BETWEEN a.txn_ts AND a.txn_ts + make_interval(secs => (SELECT p('ip_window_seconds'))::float8)
  AND (b.txn_ts, b.transaction_id) > (a.txn_ts, a.transaction_id);
RESET enable_hashjoin; RESET enable_mergejoin;
ALTER TABLE ip_linkage_pairs ADD COLUMN network_class VARCHAR(30);
UPDATE ip_linkage_pairs p SET network_class = i.network_class FROM ip_addresses i WHERE i.ip_address = p.ip_address;
CREATE INDEX ON ip_linkage_pairs (txn_a);
CREATE INDEX ON ip_linkage_pairs (txn_b);

-- >>> STEP: B3_ip_network_summary
-- Per /16 network: density, concurrency and label distribution.
DROP TABLE IF EXISTS ip_network_summary;
CREATE TABLE ip_network_summary AS
WITH t AS (
    SELECT i.network_prefix16, t.ip_address, t.transaction_id, l.is_fraud
    FROM transactions t JOIN ip_addresses i USING (ip_address) JOIN fraud_labels l USING (transaction_id)
), paired AS (
    SELECT txn_a AS transaction_id FROM ip_linkage_pairs UNION SELECT txn_b FROM ip_linkage_pairs
), pc AS (
    SELECT set_masklen(ip_address, 16)::CIDR AS network_prefix16, COUNT(*) AS overlap_pairs
    FROM ip_linkage_pairs GROUP BY 1
)
SELECT t.network_prefix16,
       COUNT(DISTINCT t.ip_address)                                   AS ip_count,
       COUNT(*)                                                       AS txn_count,
       (SELECT ROUND(AVG(account_count), 1) FROM ip_addresses i WHERE i.network_prefix16 = t.network_prefix16) AS avg_accounts_per_ip,
       (SELECT MAX(account_count)           FROM ip_addresses i WHERE i.network_prefix16 = t.network_prefix16) AS max_accounts_per_ip,
       (SELECT mode() WITHIN GROUP (ORDER BY network_class) FROM ip_addresses i WHERE i.network_prefix16 = t.network_prefix16) AS dominant_class,
       COALESCE(MAX(pc.overlap_pairs), 0)                             AS overlap_pairs_10m,
       ROUND(1000.0 * COALESCE(MAX(pc.overlap_pairs), 0) / COUNT(*), 2) AS overlap_pairs_per_1k_txn,
       ROUND(100.0 * AVG(t.is_fraud::INT), 3)                         AS fraud_rate_pct,
       ROUND(100.0 * AVG(t.is_fraud::INT) FILTER (WHERE p.transaction_id IS NOT NULL), 3) AS fraud_rate_overlap_txn_pct
FROM t
LEFT JOIN paired p USING (transaction_id)
LEFT JOIN pc USING (network_prefix16)
GROUP BY t.network_prefix16;

-- >>> STEP: B4_ip_class_summary
DROP TABLE IF EXISTS ip_class_summary;
CREATE TABLE ip_class_summary AS
WITH paired AS (
    SELECT txn_a AS transaction_id FROM ip_linkage_pairs UNION SELECT txn_b FROM ip_linkage_pairs
)
SELECT i.network_class,
       MIN(i.account_count)                                  AS min_accounts,
       MAX(i.account_count)                                  AS max_accounts,
       COUNT(DISTINCT i.ip_address)                          AS ip_count,
       COUNT(*)                                              AS txn_count,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)    AS pct_of_txns,
       SUM(l.is_fraud::INT)                                  AS fraud_txns,
       ROUND(100.0 * AVG(l.is_fraud::INT), 3)                AS fraud_rate_pct,
       COUNT(p.transaction_id)                               AS txns_in_10m_overlap,
       ROUND(100.0 * AVG(l.is_fraud::INT) FILTER (WHERE p.transaction_id IS NOT NULL), 3) AS fraud_rate_overlap_pct,
       CASE WHEN i.network_class = 'HIGH_DENSITY_SHARED'
            THEN 'Treated as legitimate shared network (CGNAT-like); excluded from ring edges'
            ELSE 'Eligible for SHARED_IP_10M flag and ring edges' END AS treatment
FROM transactions t
JOIN ip_addresses i USING (ip_address)
JOIN fraud_labels l USING (transaction_id)
LEFT JOIN paired p USING (transaction_id)
GROUP BY i.network_class;

-- >>> STEP: B5_ip_suspicious_overlap
-- Low/moderate-density IPs where different accounts overlapped within 10 minutes.
DROP TABLE IF EXISTS ip_suspicious_overlap;
CREATE TABLE ip_suspicious_overlap AS
WITH sides AS (
    SELECT ip_address, account_a AS account_id, txn_a AS txn FROM ip_linkage_pairs WHERE network_class <> 'HIGH_DENSITY_SHARED'
    UNION
    SELECT ip_address, account_b, txn_b FROM ip_linkage_pairs WHERE network_class <> 'HIGH_DENSITY_SHARED'
), pr AS (
    SELECT ip_address, COUNT(*) AS overlap_pairs, MIN(gap_seconds) AS min_gap_seconds,
           MIN(ts_a) AS first_overlap_ts, MAX(ts_b) AS last_overlap_ts
    FROM ip_linkage_pairs WHERE network_class <> 'HIGH_DENSITY_SHARED' GROUP BY ip_address
)
SELECT s.ip_address, i.network_class, i.network_prefix16,
       pr.overlap_pairs, COUNT(DISTINCT s.account_id) AS overlapping_accounts,
       i.account_count AS all_time_accounts, i.txn_count AS all_time_txns,
       pr.first_overlap_ts, pr.last_overlap_ts, pr.min_gap_seconds,
       COUNT(DISTINCT s.txn) FILTER (WHERE l.is_fraud)                      AS fraud_txns_in_overlap,
       COUNT(DISTINCT s.account_id) FILTER (WHERE als.has_fraud_label)      AS fraud_labelled_accounts
FROM sides s
JOIN pr USING (ip_address)
JOIN ip_addresses i USING (ip_address)
JOIN fraud_labels l ON l.transaction_id = s.txn
LEFT JOIN account_label_summary als ON als.account_id = s.account_id
GROUP BY s.ip_address, i.network_class, i.network_prefix16, pr.overlap_pairs, i.account_count,
         i.txn_count, pr.first_overlap_ts, pr.last_overlap_ts, pr.min_gap_seconds;


/* =============================================================================
   C. TRANSACTION VELOCITY
   ---------------------------------------------------------------------------
   C1 computes per-transaction window features with RANGE frames (time-based,
   not row-based), LAG/LEAD and running averages. C3 groups each account's
   transactions into NON-OVERLAPPING episodes (gaps-and-islands) so a burst is
   reported once, however many sliding windows it touches.
   ============================================================================= */
-- >>> STEP: C1_txn_features
DROP TABLE IF EXISTS txn_features;
CREATE TABLE txn_features AS
SELECT t.transaction_id, t.sender_account, t.receiver_account, t.txn_ts, t.amount_ngn,
       t.transaction_type, t.device_id, t.ip_address, t.txn_hour, t.src_new_device_flag,
       l.is_fraud,
       ROW_NUMBER() OVER w_seq                                        AS sender_seq,
       LAG(t.txn_ts)  OVER w_seq                                      AS prev_txn_ts,
       LEAD(t.txn_ts) OVER w_seq                                      AS next_txn_ts,
       -- trailing (look-back) windows: what a real-time engine can know at txn time
       COUNT(*) OVER (w_time RANGE BETWEEN INTERVAL '10 minutes' PRECEDING AND CURRENT ROW) AS cnt_10m_back,
       COUNT(*) OVER (w_time RANGE BETWEEN INTERVAL '1 hour'     PRECEDING AND CURRENT ROW) AS cnt_1h_back,
       COUNT(*) OVER (w_time RANGE BETWEEN INTERVAL '24 hours'   PRECEDING AND CURRENT ROW) AS cnt_24h_back,
       -- forward window: used only to measure the densest 10-minute burst per episode
       COUNT(*) OVER (w_time RANGE BETWEEN CURRENT ROW AND INTERVAL '10 minutes' FOLLOWING) AS cnt_10m_fwd,
       AVG(t.amount_ngn) OVER (w_seq ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)      AS trailing_avg_amount,
       ROW_NUMBER() OVER (PARTITION BY t.sender_account, t.device_id ORDER BY t.txn_ts, t.transaction_id) AS device_seq_for_sender
FROM transactions t
JOIN fraud_labels l USING (transaction_id)
WINDOW w_seq  AS (PARTITION BY t.sender_account ORDER BY t.txn_ts, t.transaction_id),
       w_time AS (PARTITION BY t.sender_account ORDER BY t.txn_ts);
ALTER TABLE txn_features ADD PRIMARY KEY (transaction_id);

-- >>> STEP: C2_beneficiaries
-- Distinct receivers in the trailing 24 h. COUNT(DISTINCT) is not allowed as a
-- window function, so a bounded self-join is used - only where >= 2 txns exist.
SET enable_hashjoin = off; SET enable_mergejoin = off;
DROP TABLE IF EXISTS txn_beneficiaries_24h;
CREATE TABLE txn_beneficiaries_24h AS
SELECT a.transaction_id, COUNT(DISTINCT b.receiver_account) AS distinct_receivers_24h
FROM txn_features a
JOIN transactions b
  ON  b.sender_account = a.sender_account
  AND b.txn_ts BETWEEN a.txn_ts - INTERVAL '24 hours' AND a.txn_ts
WHERE a.cnt_24h_back >= 2
GROUP BY a.transaction_id;
RESET enable_hashjoin; RESET enable_mergejoin;
ALTER TABLE txn_beneficiaries_24h ADD PRIMARY KEY (transaction_id);

-- >>> STEP: C3_velocity_episodes
-- Gaps-and-islands: a new episode starts when the gap to the previous txn of the
-- same sender exceeds 10 minutes. Every transaction belongs to exactly ONE
-- episode, so no transaction is double-counted across overlapping windows.
DROP TABLE IF EXISTS velocity_episodes;
CREATE TABLE velocity_episodes AS
WITH marked AS (
    SELECT f.*,
           CASE WHEN f.prev_txn_ts IS NULL
                  OR f.txn_ts - f.prev_txn_ts > make_interval(secs => (SELECT p('velocity_window_seconds'))::float8)
                THEN 1 ELSE 0 END AS is_new_episode
    FROM txn_features f
), numbered AS (
    SELECT m.*, SUM(is_new_episode) OVER (PARTITION BY sender_account ORDER BY txn_ts, transaction_id
                                          ROWS UNBOUNDED PRECEDING) AS episode_no
    FROM marked m
)
SELECT sender_account, episode_no,
       COUNT(*)                                             AS txn_count,
       MIN(txn_ts)                                          AS start_ts,
       MAX(txn_ts)                                          AS end_ts,
       ROUND(EXTRACT(EPOCH FROM MAX(txn_ts) - MIN(txn_ts)), 1) AS duration_seconds,
       MAX(cnt_10m_fwd)                                     AS max_txns_any_10m,
       SUM(amount_ngn)                                      AS total_value_ngn,
       ROUND(AVG(amount_ngn), 2)                            AS avg_value_ngn,
       string_agg(DISTINCT transaction_type, ', ')          AS transaction_types,
       COUNT(DISTINCT receiver_account)                     AS unique_beneficiaries,
       SUM(is_fraud::INT)                                   AS fraud_txns,
       (MAX(cnt_10m_fwd) > (SELECT p('velocity_spec_threshold')))    AS breaches_spec_rule
FROM numbered
GROUP BY sender_account, episode_no
HAVING COUNT(*) >= 2;

-- >>> STEP: C4_velocity_distribution
DROP TABLE IF EXISTS velocity_distribution;
CREATE TABLE velocity_distribution AS
SELECT w.window_label, w.n AS txns_in_window,
       COUNT(*) AS txn_count,
       SUM(f.is_fraud::INT) AS fraud_txns,
       ROUND(100.0 * AVG(f.is_fraud::INT), 3) AS fraud_rate_pct
FROM txn_features f
CROSS JOIN LATERAL (VALUES ('trailing_10m', f.cnt_10m_back), ('trailing_1h', f.cnt_1h_back),
                           ('trailing_24h', f.cnt_24h_back)) AS w(window_label, n)
GROUP BY w.window_label, w.n;

-- >>> STEP: C5_velocity_spec_check
-- The brief's rule (> 5 txns in 10 minutes) evaluated literally.
DROP TABLE IF EXISTS velocity_spec_check;
CREATE TABLE velocity_spec_check AS
SELECT (SELECT MAX(cnt_10m_fwd) FROM txn_features)                                   AS max_txns_any_10m_window,
       (SELECT MAX(cnt_1h_back) FROM txn_features)                                   AS max_txns_any_1h_window,
       (SELECT MAX(cnt_24h_back) FROM txn_features)                                  AS max_txns_any_24h_window,
       (SELECT COUNT(DISTINCT sender_account) FROM txn_features
         WHERE cnt_10m_fwd > (SELECT p('velocity_spec_threshold')))                           AS accounts_breaching_spec_rule,
       (SELECT COUNT(*) FROM velocity_episodes)                                      AS multi_txn_episodes_10m_gap,
       (SELECT COUNT(DISTINCT sender_account) FROM txn_features WHERE cnt_10m_back >= 2) AS accounts_2plus_in_10m,
       (SELECT COUNT(DISTINCT sender_account) FROM txn_features WHERE cnt_1h_back  >= 3) AS accounts_3plus_in_1h,
       (SELECT COUNT(DISTINCT sender_account) FROM txn_features WHERE cnt_24h_back >= 3) AS accounts_3plus_in_24h,
       (SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM txn_ts - prev_txn_ts) / 3600.0)::NUMERIC, 1)
          FROM txn_features WHERE prev_txn_ts IS NOT NULL)                            AS median_hours_between_txns;


/* =============================================================================
   D. RECURSIVE MONEY-FLOW ANALYSIS
   ---------------------------------------------------------------------------
   Every source row has sender_account -> receiver_account, so each row is a
   directed value movement. A "hop" is a pair of transactions where the
   receiver of the first sends money onward within 24 hours.
   ============================================================================= */
-- >>> STEP: D1_flow_hops
-- Hop = (A -> B) followed by (B -> C) with 0 < gap <= 24 h. Self-transfers excluded.
SET enable_hashjoin = off; SET enable_mergejoin = off;
DROP TABLE IF EXISTS flow_hops;
CREATE TABLE flow_hops AS
SELECT i.transaction_id   AS txn_in,  o.transaction_id   AS txn_out,
       i.sender_account   AS sender_in, i.receiver_account AS via_account, o.receiver_account AS receiver_out,
       i.txn_ts           AS ts_in,   o.txn_ts           AS ts_out,
       i.amount_ngn       AS amount_in, o.amount_ngn     AS amount_out,
       ROUND(EXTRACT(EPOCH FROM o.txn_ts - i.txn_ts) / 60.0, 2) AS forward_minutes,
       (o.receiver_account = i.sender_account)              AS is_round_trip
FROM transactions i
JOIN transactions o
  ON  o.sender_account = i.receiver_account
  AND o.txn_ts >  i.txn_ts
  AND o.txn_ts <= i.txn_ts + make_interval(hours => (SELECT p('flow_window_hours'))::INT)
WHERE NOT i.is_self_transfer AND NOT o.is_self_transfer;
RESET enable_hashjoin; RESET enable_mergejoin;
CREATE INDEX ON flow_hops (txn_in);
CREATE INDEX ON flow_hops (txn_out);

-- >>> STEP: D2_money_flow_paths
TRUNCATE money_flow_paths CASCADE;
/* Recursive CTE `chain`
   ANCHOR MEMBER   : every non-round-trip hop A -> B -> C. It seeds a chain of two
                     transactions whose 24-hour clock starts at the first transfer.
   RECURSIVE MEMBER: extends the chain from its last transaction (txn_out becomes
                     the next txn_in) while ALL of the following hold:
                       1. depth guard      hop_count < flow_max_depth (6)
                       2. time window      next transfer <= chain start + 24 h
                                           (each hop is also later than the previous one)
                       3. cycle prevention next receiver NOT already in account_path
   Termination     : guaranteed by the depth guard and by strictly increasing
                     timestamps; the cycle check also stops A->B->C->A loops.
   Duplicate paths : every chain is also seeded from each of its later hops, so
                     sub-chains appear as separate rows. The final INSERT keeps only
                     MAXIMAL chains - a chain is dropped when another chain's
                     transaction_path contains all of its transactions.
   (PostgreSQL 14+ also offers the CYCLE clause; the explicit array check is used
   because it is portable and shows the logic.) */
DROP TABLE IF EXISTS money_flow_chains_all;
CREATE TABLE money_flow_chains_all AS
WITH RECURSIVE chain AS (
    -- anchor member
    SELECT h.txn_in                                    AS root_txn,
           2                                           AS hop_count,
           ARRAY[h.sender_in, h.via_account, h.receiver_out] AS account_path,
           ARRAY[h.txn_in::TEXT, h.txn_out::TEXT]     AS txn_path,
           h.ts_in                                     AS start_ts,
           h.ts_out                                    AS last_ts,
           h.txn_out                                   AS last_txn,
           h.amount_in                                 AS first_amount,
           h.amount_out                                AS last_amount,
           (h.amount_in + h.amount_out)::NUMERIC(16,2) AS total_value
    FROM flow_hops h
    WHERE NOT h.is_round_trip
  UNION ALL
    -- recursive member
    SELECT c.root_txn,
           c.hop_count + 1,
           c.account_path || h.receiver_out,
           c.txn_path || h.txn_out::TEXT,
           c.start_ts,
           h.ts_out,
           h.txn_out,
           c.first_amount,
           h.amount_out,
           (c.total_value + h.amount_out)::NUMERIC(16,2)
    FROM chain c
    JOIN flow_hops h ON h.txn_in = c.last_txn
    WHERE c.hop_count < (SELECT p('flow_max_depth'))                                         -- depth guard
      AND h.ts_out <= c.start_ts + make_interval(hours => (SELECT p('flow_window_hours'))::INT) -- 24 h window
      AND NOT (h.receiver_out = ANY (c.account_path))                                -- cycle prevention
)
SELECT DISTINCT ON (txn_path) *            -- identical paths reached twice collapse to one
FROM chain
ORDER BY txn_path;
CREATE INDEX ON money_flow_chains_all USING gin (txn_path);

-- keep maximal chains only (duplicate / sub-path prevention)
INSERT INTO money_flow_paths (path_id, root_transaction_id, hop_count, account_path, transaction_path, path_text,
                              start_ts, end_ts, elapsed_minutes, first_amount_ngn, last_amount_ngn,
                              total_value_ngn, fraud_txn_in_path, labelled_accounts_in_path)
SELECT ROW_NUMBER() OVER (ORDER BY c.start_ts, c.root_txn, c.txn_path) AS path_id,
       c.root_txn, c.hop_count, c.account_path, c.txn_path::VARCHAR(12)[],
       array_to_string(c.account_path, ' -> '),
       c.start_ts, c.last_ts,
       ROUND(EXTRACT(EPOCH FROM c.last_ts - c.start_ts) / 60.0, 2),
       c.first_amount, c.last_amount, c.total_value,
       (SELECT COUNT(*) FROM unnest(c.txn_path) x(tid) JOIN fraud_labels l ON l.transaction_id = x.tid WHERE l.is_fraud),
       (SELECT COUNT(*) FROM unnest(c.account_path) a(aid) JOIN account_label_summary s ON s.account_id = a.aid WHERE s.has_fraud_label)
FROM money_flow_chains_all c
WHERE NOT EXISTS (
        SELECT 1 FROM money_flow_chains_all o
        WHERE o.txn_path @> c.txn_path
          AND cardinality(o.txn_path) > cardinality(c.txn_path));
CREATE INDEX ON money_flow_paths USING gin (transaction_path);
CREATE INDEX ON money_flow_paths USING gin (account_path);

-- >>> STEP: D3_money_flow_summary
-- Compare observed share of chains containing a labelled txn with the share
-- expected if labels were independent of chain membership: 1 - (1 - p)^hops.
DROP TABLE IF EXISTS money_flow_summary;
CREATE TABLE money_flow_summary AS
WITH base AS (SELECT AVG(is_fraud::INT)::NUMERIC AS p FROM fraud_labels)
SELECT m.hop_count,
       COUNT(*)                                                          AS paths,
       ROUND(AVG(m.elapsed_minutes), 1)                                  AS avg_elapsed_minutes,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.elapsed_minutes)::NUMERIC, 1) AS median_elapsed_minutes,
       ROUND(AVG(m.total_value_ngn), 0)                                  AS avg_total_value_ngn,
       ROUND(AVG(m.last_amount_ngn / NULLIF(m.first_amount_ngn, 0)), 3)  AS avg_last_to_first_amount_ratio,
       ROUND(100.0 * AVG((m.fraud_txn_in_path > 0)::INT), 2)             AS pct_paths_with_fraud_txn,
       ROUND(100.0 * (1 - POWER(1 - b.p, m.hop_count)), 2)               AS pct_expected_if_independent,
       ROUND(AVG((m.fraud_txn_in_path > 0)::INT) / NULLIF(1 - POWER(1 - b.p, m.hop_count), 0), 3) AS lift_vs_independent
FROM money_flow_paths m CROSS JOIN base b
GROUP BY m.hop_count, b.p;

-- >>> STEP: D4_hop_stats
-- Does fraud propagate along a hop? P(outgoing labelled | incoming labelled).
DROP TABLE IF EXISTS money_flow_hop_stats;
CREATE TABLE money_flow_hop_stats AS
WITH h AS (
    SELECT fh.*, li.is_fraud AS in_fraud, lo.is_fraud AS out_fraud
    FROM flow_hops fh
    JOIN fraud_labels li ON li.transaction_id = fh.txn_in
    JOIN fraud_labels lo ON lo.transaction_id = fh.txn_out
)
SELECT COUNT(*)                                                     AS total_hops,
       COUNT(*) FILTER (WHERE is_round_trip)                        AS round_trip_hops,
       COUNT(*) FILTER (WHERE forward_minutes <= (SELECT p('fast_flow_minutes'))) AS hops_forwarded_within_1h,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY forward_minutes)::NUMERIC, 1) AS median_forward_minutes,
       COUNT(DISTINCT via_account)                                  AS pass_through_accounts,
       (SELECT COUNT(*) FROM money_flow_paths)                      AS maximal_paths,
       (SELECT MAX(hop_count) FROM money_flow_paths)                AS longest_path_txns,
       ROUND(100.0 * AVG(out_fraud::INT) FILTER (WHERE in_fraud), 3)     AS pct_out_fraud_given_in_fraud,
       ROUND(100.0 * AVG(out_fraud::INT) FILTER (WHERE NOT in_fraud), 3) AS pct_out_fraud_given_in_legit,
       (SELECT ROUND(100.0 * AVG(is_fraud::INT), 3) FROM fraud_labels)   AS overall_fraud_rate_pct
FROM h;

-- >>> STEP: D5_mule_concentration
-- Mule test: do some receivers collect many labelled transactions? Observed
-- receivers by number of inbound labelled txns vs. a Binomial(n_inbound, p)
-- expectation if labels were spread at random.
DROP TABLE IF EXISTS mule_concentration;
CREATE TABLE mule_concentration AS
WITH base AS (SELECT AVG(is_fraud::INT)::FLOAT8 AS p FROM fraud_labels),
r AS (
    SELECT t.receiver_account, COUNT(*) AS n_in, COUNT(*) FILTER (WHERE l.is_fraud) AS k_fraud
    FROM transactions t JOIN fraud_labels l USING (transaction_id)
    GROUP BY t.receiver_account
), obs AS (
    SELECT k_fraud, COUNT(*) AS observed_receivers FROM r GROUP BY k_fraud
), expw AS (
    -- expected receivers with k labelled inbound txns = sum over inbound-count groups of
    -- (#receivers with n inbound) * C(n,k) p^k (1-p)^(n-k)
    SELECT k.k AS k_fraud,
           SUM(r0.cnt * (factorial(r0.n_in) / (factorial(k.k) * factorial(r0.n_in - k.k)))::FLOAT8
                * POWER(b.p, k.k) * POWER(1 - b.p, r0.n_in - k.k)) AS expected_receivers
    FROM (SELECT n_in, COUNT(*) AS cnt FROM r GROUP BY n_in) r0
    JOIN LATERAL generate_series(0, r0.n_in) AS k(k) ON TRUE
    CROSS JOIN base b
    GROUP BY k.k
)
SELECT e.k_fraud AS fraud_inbound_txns,
       COALESCE(o.observed_receivers, 0) AS observed_receivers,
       ROUND(e.expected_receivers::NUMERIC, 1) AS expected_receivers_if_random,
       ROUND((COALESCE(o.observed_receivers, 0) / NULLIF(e.expected_receivers, 0))::NUMERIC, 3) AS observed_to_expected
FROM expw e LEFT JOIN obs o USING (k_fraud)
WHERE e.expected_receivers >= 0.05 OR o.observed_receivers IS NOT NULL;


/* =============================================================================
   E. BEHAVIOURAL FLAGS AND RULE-BASED SCORE
   ============================================================================= */
-- >>> STEP: E1_flags
-- Bulk-load pattern: drop FKs, insert, re-add FKs. Re-adding validates every row in one
-- set-based query instead of millions of per-row trigger checks (~180 us/row measured).
ALTER TABLE suspicious_activity_flags DROP CONSTRAINT IF EXISTS suspicious_activity_flags_transaction_id_fkey,
                                      DROP CONSTRAINT IF EXISTS suspicious_activity_flags_flag_code_fkey;
TRUNCATE suspicious_activity_flags;
INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT DISTINCT ON (txn) txn, 'SHARED_DEVICE_1H',
       'device ' || device_id || ' also used by account ' || other || ' (' || gap_seconds || ' s apart)'
FROM (SELECT txn_a AS txn, device_id, account_b AS other, gap_seconds FROM device_linkage_pairs
      UNION ALL
      SELECT txn_b, device_id, account_a, gap_seconds FROM device_linkage_pairs) x
ORDER BY txn, gap_seconds;

INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT DISTINCT ON (txn) txn, 'SHARED_IP_10M',
       'IP ' || host(ip_address) || ' (' || network_class || ') also used by account ' || other || ' (' || gap_seconds || ' s apart)'
FROM (SELECT txn_a AS txn, ip_address, network_class, account_b AS other, gap_seconds FROM ip_linkage_pairs
      UNION ALL
      SELECT txn_b, ip_address, network_class, account_a, gap_seconds FROM ip_linkage_pairs) x
WHERE network_class <> 'HIGH_DENSITY_SHARED'
ORDER BY txn, gap_seconds;

INSERT INTO suspicious_activity_flags (transaction_id, flag_code)
SELECT t.transaction_id, 'SHARED_NETWORK_IP'
FROM transactions t JOIN ip_addresses i USING (ip_address)
WHERE i.network_class = 'HIGH_DENSITY_SHARED';

INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT transaction_id, 'HIGH_VELOCITY',
       cnt_10m_back || ' txns in trailing 10 min, ' || cnt_1h_back || ' in trailing 1 h'
FROM txn_features
WHERE cnt_10m_back >= (SELECT p('velocity_cal_10m')) OR cnt_1h_back >= (SELECT p('velocity_cal_1h'));

INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT b.transaction_id, 'MULTI_BENEFICIARY_24H', b.distinct_receivers_24h || ' distinct receivers in trailing 24 h'
FROM txn_beneficiaries_24h b
WHERE b.distinct_receivers_24h >= (SELECT p('multi_beneficiary_24h'));

INSERT INTO suspicious_activity_flags (transaction_id, flag_code)
SELECT transaction_id, 'NEW_DEVICE_SRC' FROM txn_features WHERE src_new_device_flag;

INSERT INTO suspicious_activity_flags (transaction_id, flag_code)
SELECT transaction_id, 'FIRST_DEVICE_USE' FROM txn_features
WHERE device_seq_for_sender = 1 AND sender_seq > 1;

INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT f.transaction_id, 'HIGH_VALUE', 'amount >= P95 (' || ROUND(q.p95) || ' NGN)'
FROM txn_features f
CROSS JOIN (SELECT PERCENTILE_CONT(p('high_value_percentile')::FLOAT8) WITHIN GROUP (ORDER BY amount_ngn) AS p95 FROM transactions) q
WHERE f.amount_ngn >= q.p95;

INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT transaction_id, 'AMOUNT_SPIKE',
       ROUND(amount_ngn / trailing_avg_amount, 1) || 'x trailing average of ' || (sender_seq - 1) || ' prior txns'
FROM txn_features
WHERE sender_seq >= 3 AND amount_ngn >= (SELECT p('amount_spike_multiple')) * trailing_avg_amount;

INSERT INTO suspicious_activity_flags (transaction_id, flag_code)
SELECT transaction_id, 'UNUSUAL_HOUR' FROM txn_features
WHERE txn_hour BETWEEN (SELECT p('night_start_hour')) AND (SELECT p('night_end_hour'));

INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT DISTINCT ON (txn_out) txn_out, 'RAPID_PASS_THROUGH',
       'received ' || ROUND(amount_in) || ' NGN from ' || sender_in || ', forwarded after ' || forward_minutes || ' min'
FROM flow_hops
ORDER BY txn_out, forward_minutes;

-- strictly-prior label connection (no look-ahead): receiver, or device/IP partner
INSERT INTO suspicious_activity_flags (transaction_id, flag_code, flag_detail)
SELECT DISTINCT ON (txn) txn, 'PRIOR_FRAUD_LINK', reason
FROM (
    SELECT t.transaction_id AS txn, 'receiver ' || t.receiver_account || ' labelled since ' || s.first_fraud_ts::DATE AS reason
    FROM transactions t JOIN account_label_summary s ON s.account_id = t.receiver_account
    WHERE s.first_fraud_ts < t.txn_ts
    UNION ALL
    SELECT p.txn_a, 'device partner ' || p.account_b || ' labelled before txn'
    FROM device_linkage_pairs p JOIN account_label_summary s ON s.account_id = p.account_b WHERE s.first_fraud_ts < p.ts_a
    UNION ALL
    SELECT p.txn_b, 'device partner ' || p.account_a || ' labelled before txn'
    FROM device_linkage_pairs p JOIN account_label_summary s ON s.account_id = p.account_a WHERE s.first_fraud_ts < p.ts_b
    UNION ALL
    SELECT p.txn_a, 'IP partner ' || p.account_b || ' labelled before txn'
    FROM ip_linkage_pairs p JOIN account_label_summary s ON s.account_id = p.account_b
    WHERE p.network_class <> 'HIGH_DENSITY_SHARED' AND s.first_fraud_ts < p.ts_a
    UNION ALL
    SELECT p.txn_b, 'IP partner ' || p.account_a || ' labelled before txn'
    FROM ip_linkage_pairs p JOIN account_label_summary s ON s.account_id = p.account_a
    WHERE p.network_class <> 'HIGH_DENSITY_SHARED' AND s.first_fraud_ts < p.ts_b
) x
ORDER BY txn, reason;
ALTER TABLE suspicious_activity_flags
  ADD CONSTRAINT suspicious_activity_flags_transaction_id_fkey FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id),
  ADD CONSTRAINT suspicious_activity_flags_flag_code_fkey FOREIGN KEY (flag_code) REFERENCES flag_definitions(flag_code);

-- >>> STEP: E2_scores
-- Bulk-load pattern: drop FKs, insert, re-add FKs. Re-adding validates every row in one
-- set-based query instead of millions of per-row trigger checks (~180 us/row measured).
ALTER TABLE transaction_risk_scores DROP CONSTRAINT IF EXISTS transaction_risk_scores_transaction_id_fkey;
TRUNCATE transaction_risk_scores;
-- Explainable score = sum of flag weights (capped at 100); tiers from parameters.
INSERT INTO transaction_risk_scores (transaction_id, risk_score, risk_tier, flag_count, flag_list)
WITH s AS (
    SELECT f.transaction_id,
           LEAST(100, SUM(d.weight))::SMALLINT                         AS score,
           COUNT(*) FILTER (WHERE d.weight > 0)::SMALLINT               AS n_flags,
           string_agg(f.flag_code, '|' ORDER BY d.weight DESC, f.flag_code) FILTER (WHERE d.weight > 0) AS flags
    FROM suspicious_activity_flags f JOIN flag_definitions d USING (flag_code)
    GROUP BY f.transaction_id
)
SELECT t.transaction_id,
       COALESCE(s.score, 0),
       CASE WHEN COALESCE(s.score, 0) >= (SELECT p('tier_critical')) THEN 'CRITICAL'
            WHEN COALESCE(s.score, 0) >= (SELECT p('tier_high'))     THEN 'HIGH'
            WHEN COALESCE(s.score, 0) >= (SELECT p('tier_medium'))   THEN 'MEDIUM'
            ELSE 'LOW' END,
       COALESCE(s.n_flags, 0),
       s.flags
FROM transactions t LEFT JOIN s USING (transaction_id);
CREATE INDEX IF NOT EXISTS ix_trs_tier ON transaction_risk_scores (risk_tier);
ALTER TABLE transaction_risk_scores
  ADD CONSTRAINT transaction_risk_scores_transaction_id_fkey FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id);

/* =============================================================================
   F. FLAG / LABEL ASSOCIATION
   ---------------------------------------------------------------------------
   lift          = fraud rate among flagged  / overall fraud rate
   relative risk = fraud rate among flagged  / fraud rate among NOT flagged
   95% CI of RR  = exp( ln(RR) +/- 1.96 * sqrt(1/a - 1/n1 + 1/c - 1/n2) )
   These measure ASSOCIATION with the source label in this dataset. They do not
   show that a behaviour causes fraud (see methodology.md).
   ============================================================================= */
-- >>> STEP: F1_flag_performance
DROP TABLE IF EXISTS flag_performance;
CREATE TABLE flag_performance AS
WITH base AS (
    SELECT COUNT(*)::NUMERIC AS n, SUM(is_fraud::INT)::NUMERIC AS f FROM fraud_labels
), fl AS (
    SELECT s.flag_code, COUNT(*)::NUMERIC AS n1, SUM(l.is_fraud::INT)::NUMERIC AS a
    FROM suspicious_activity_flags s JOIN fraud_labels l USING (transaction_id)
    GROUP BY s.flag_code
)
SELECT d.flag_code, d.flag_family, d.weight, d.data_basis,
       COALESCE(fl.n1, 0)                                         AS flagged_txns,
       COALESCE(fl.a, 0)                                          AS fraud_flagged_txns,
       ROUND(100 * COALESCE(fl.n1, 0) / b.n, 3)                   AS pct_of_all_txns,
       ROUND(100 * fl.a / NULLIF(fl.n1, 0), 3)                    AS fraud_rate_flagged_pct,
       ROUND(100 * (b.f - COALESCE(fl.a, 0)) / NULLIF(b.n - COALESCE(fl.n1, 0), 0), 3) AS fraud_rate_unflagged_pct,
       ROUND(100 * b.f / b.n, 3)                                  AS overall_fraud_rate_pct,
       ROUND((fl.a / NULLIF(fl.n1, 0)) / (b.f / b.n), 3)          AS lift,
       ROUND((fl.a / NULLIF(fl.n1, 0)) / NULLIF((b.f - fl.a) / NULLIF(b.n - fl.n1, 0), 0), 3) AS relative_risk,
       CASE WHEN fl.a > 0 AND b.f - fl.a > 0 AND fl.n1 < b.n THEN
         ROUND(EXP(LN((fl.a / fl.n1) / ((b.f - fl.a) / (b.n - fl.n1)))
                   - 1.96 * SQRT(1 / fl.a - 1 / fl.n1 + 1 / (b.f - fl.a) - 1 / (b.n - fl.n1))), 3) END AS rr_ci95_low,
       CASE WHEN fl.a > 0 AND b.f - fl.a > 0 AND fl.n1 < b.n THEN
         ROUND(EXP(LN((fl.a / fl.n1) / ((b.f - fl.a) / (b.n - fl.n1)))
                   + 1.96 * SQRT(1 / fl.a - 1 / fl.n1 + 1 / (b.f - fl.a) - 1 / (b.n - fl.n1))), 3) END AS rr_ci95_high,
       ROUND(100 * COALESCE(fl.a, 0) / b.f, 3)                    AS recall_pct
FROM flag_definitions d
CROSS JOIN base b
LEFT JOIN fl USING (flag_code);

-- >>> STEP: F2_tier_performance
DROP TABLE IF EXISTS tier_performance;
CREATE TABLE tier_performance AS
WITH t AS (
    SELECT s.risk_tier, MIN(s.risk_score) AS min_score, MAX(s.risk_score) AS max_score,
           COUNT(*) AS txns, SUM(l.is_fraud::INT) AS fraud_txns, SUM(tx.amount_ngn) AS value_ngn
    FROM transaction_risk_scores s
    JOIN fraud_labels l USING (transaction_id)
    JOIN transactions tx USING (transaction_id)
    GROUP BY s.risk_tier
)
SELECT risk_tier, min_score, max_score, txns,
       ROUND(100.0 * txns / SUM(txns) OVER (), 3)                          AS pct_of_txns,
       fraud_txns,
       ROUND(100.0 * fraud_txns / txns, 3)                                 AS fraud_rate_pct,
       ROUND((1.0 * fraud_txns / txns) / (SUM(fraud_txns) OVER () * 1.0 / SUM(txns) OVER ()), 3) AS lift,
       ROUND(100.0 * SUM(fraud_txns) OVER (ORDER BY min_score DESC) / SUM(fraud_txns) OVER (), 2) AS cumulative_recall_pct,
       ROUND(100.0 * SUM(txns) OVER (ORDER BY min_score DESC) / SUM(txns) OVER (), 3)             AS cumulative_review_pct,
       value_ngn
FROM t;

-- >>> STEP: F3_label_concentration
-- Accounts with more activity are mechanically more likely to carry >= 1 label.
-- This table is the baseline used to size-adjust ring validation (G6).
DROP TABLE IF EXISTS label_concentration;
CREATE TABLE label_concentration AS
SELECT sent_txn_count,
       COUNT(*)                                          AS accounts,
       COUNT(*) FILTER (WHERE has_fraud_label)           AS labelled_accounts,
       ROUND(100.0 * AVG(has_fraud_label::INT), 3)       AS pct_accounts_labelled,
       ROUND(100.0 * SUM(fraud_txn_count) / SUM(sent_txn_count), 3) AS txn_fraud_rate_pct
FROM account_label_summary
GROUP BY sent_txn_count;


/* =============================================================================
   G. FRAUD-RING DETECTION
   ---------------------------------------------------------------------------
   1. account_links  : explainable edges
        SHARED_DEVICE  same device, different accounts, <= 1 h apart
        SHARED_IP      same low/moderate-density IP, <= 10 min apart
        MONEY_FLOW     transfer that is part of a maximal >= 2-transfer chain (D)
   2. connected components of the undirected link graph (recursive CTE)
   3. groups of >= 3 accounts are scored:
        network_score (label-free) : size, link diversity, device/IP evidence,
                                     fast pass-through, suspicious-txn volume
        label_score                : share of members with a source fraud label
      Tier MEDIUM or above = POTENTIAL FRAUD RING (an investigation lead, not a finding).
      Corroboration rule: groups linked by a single evidence type stay LOW.
   ============================================================================= */
-- >>> STEP: G1_account_links
ALTER TABLE account_links DROP CONSTRAINT IF EXISTS account_links_account_a_fkey,
    DROP CONSTRAINT IF EXISTS account_links_account_b_fkey;  -- bulk-load pattern (see E1)
TRUNCATE account_links;
INSERT INTO account_links (account_a, account_b, link_type, evidence_count, first_evidence_ts, last_evidence_ts, min_gap_seconds)
SELECT LEAST(account_a, account_b), GREATEST(account_a, account_b), 'SHARED_DEVICE',
       COUNT(*), MIN(ts_a), MAX(ts_b), MIN(gap_seconds)
FROM device_linkage_pairs
GROUP BY 1, 2;

INSERT INTO account_links (account_a, account_b, link_type, evidence_count, first_evidence_ts, last_evidence_ts, min_gap_seconds)
SELECT LEAST(account_a, account_b), GREATEST(account_a, account_b), 'SHARED_IP',
       COUNT(*), MIN(ts_a), MAX(ts_b), MIN(gap_seconds)
FROM ip_linkage_pairs
WHERE network_class <> 'HIGH_DENSITY_SHARED'
GROUP BY 1, 2;

INSERT INTO account_links (account_a, account_b, link_type, evidence_count, first_evidence_ts, last_evidence_ts, min_gap_seconds)
SELECT LEAST(t.sender_account, t.receiver_account), GREATEST(t.sender_account, t.receiver_account), 'MONEY_FLOW',
       COUNT(DISTINCT t.transaction_id), MIN(t.txn_ts), MAX(t.txn_ts), NULL
FROM (SELECT DISTINCT unnest(transaction_path) AS transaction_id FROM money_flow_paths) p
JOIN transactions t USING (transaction_id)
GROUP BY 1, 2;
ALTER TABLE account_links
  ADD CONSTRAINT account_links_account_a_fkey FOREIGN KEY (account_a) REFERENCES accounts(account_id),
  ADD CONSTRAINT account_links_account_b_fkey FOREIGN KEY (account_b) REFERENCES accounts(account_id);

-- >>> STEP: G2_components
-- Connected components via recursive reachability. UNION (not UNION ALL)
-- discards already-reached (root, node) pairs, so the recursion terminates.
-- component_id = smallest account id reachable from the node.
DROP TABLE IF EXISTS account_components;
CREATE TABLE account_components AS
WITH RECURSIVE edges AS (
    SELECT account_a AS src, account_b AS dst FROM account_links
    UNION
    SELECT account_b, account_a FROM account_links
), reach (root, node) AS (
    SELECT DISTINCT src, src FROM edges              -- anchor: every linked account reaches itself
    UNION
    SELECT r.root, e.dst                             -- recursive: step to any neighbour
    FROM reach r JOIN edges e ON e.src = r.node
)
SELECT root AS account_id, MIN(node) AS component_id, COUNT(*) AS component_size
FROM reach
GROUP BY root;
ALTER TABLE account_components ADD PRIMARY KEY (account_id);
CREATE INDEX ON account_components (component_id);

-- >>> STEP: G3_rings
TRUNCATE fraud_rings CASCADE;   -- also clears membership, account profile, cases
INSERT INTO fraud_rings (ring_id, component_id, account_count, link_count, device_links, ip_links, money_flow_links,
                         link_type_diversity, connection_reasons, labelled_fraud_accounts, suspicious_txn_count,
                         total_txn_count, total_value_ngn, fast_flow_hops, network_score, label_score,
                         ring_risk_score, risk_tier)
WITH comp AS (
    SELECT component_id, COUNT(*) AS n_acc FROM account_components GROUP BY component_id HAVING COUNT(*) >= 3
), lk AS (
    SELECT c.component_id,
           COUNT(*)                                            AS link_count,
           COUNT(*) FILTER (WHERE l.link_type = 'SHARED_DEVICE') AS device_links,
           COUNT(*) FILTER (WHERE l.link_type = 'SHARED_IP')     AS ip_links,
           COUNT(*) FILTER (WHERE l.link_type = 'MONEY_FLOW')    AS money_flow_links
    FROM account_links l JOIN account_components c ON c.account_id = l.account_a
    GROUP BY c.component_id
), mem AS (
    SELECT c.component_id,
           COUNT(*) FILTER (WHERE s.has_fraud_label) AS labelled
    FROM account_components c LEFT JOIN account_label_summary s USING (account_id)
    GROUP BY c.component_id
), tx AS (
    SELECT c.component_id, COUNT(*) AS n_txn, SUM(t.amount_ngn) AS value,
           COUNT(*) FILTER (WHERE r.risk_tier IN ('HIGH','CRITICAL')) AS suspicious
    FROM account_components c
    JOIN transactions t ON t.sender_account = c.account_id
    JOIN transaction_risk_scores r USING (transaction_id)
    GROUP BY c.component_id
), fast AS (
    SELECT c.component_id, COUNT(*) AS fast_hops
    FROM flow_hops h JOIN account_components c ON c.account_id = h.via_account
    JOIN account_components c2 ON c2.account_id = h.sender_in AND c2.component_id = c.component_id
    WHERE h.forward_minutes <= (SELECT p('fast_flow_minutes')) AND NOT h.is_round_trip
    GROUP BY c.component_id
), scored AS (
    SELECT comp.component_id, comp.n_acc, lk.link_count, lk.device_links, lk.ip_links, lk.money_flow_links,
           ((lk.device_links > 0)::INT + (lk.ip_links > 0)::INT + (lk.money_flow_links > 0)::INT) AS diversity,
           concat_ws('; ',
               CASE WHEN lk.device_links > 0 THEN lk.device_links || ' shared-device link(s) within 1 h' END,
               CASE WHEN lk.ip_links > 0 THEN lk.ip_links || ' shared-IP link(s) within 10 min' END,
               CASE WHEN lk.money_flow_links > 0 THEN lk.money_flow_links || ' money-flow link(s) in 24 h chains' END,
               CASE WHEN COALESCE(fast.fast_hops, 0) > 0 THEN fast.fast_hops || ' hop(s) forwarded within 1 h' END,
               CASE WHEN mem.labelled > 0 THEN mem.labelled || ' member(s) with source fraud label' END) AS reasons,
           mem.labelled, COALESCE(tx.suspicious, 0) AS suspicious, COALESCE(tx.n_txn, 0) AS n_txn,
           COALESCE(tx.value, 0) AS value, COALESCE(fast.fast_hops, 0) AS fast_hops
    FROM comp
    JOIN lk USING (component_id)
    JOIN mem USING (component_id)
    LEFT JOIN tx USING (component_id)
    LEFT JOIN fast USING (component_id)
), pts AS (
    SELECT s.*,
           ( CASE WHEN n_acc >= 10 THEN 15 WHEN n_acc >= 5 THEN 10 ELSE 5 END
           + CASE diversity WHEN 3 THEN 25 WHEN 2 THEN 15 ELSE 0 END
           + CASE WHEN device_links > 0 THEN 10 ELSE 0 END
           + CASE WHEN ip_links > 0 THEN 5 ELSE 0 END
           + CASE WHEN fast_hops > 0 THEN 10 ELSE 0 END
           + CASE WHEN suspicious >= 3 THEN 10 WHEN suspicious >= 1 THEN 5 ELSE 0 END ) AS network_score,
           CASE WHEN labelled::NUMERIC / n_acc >= 0.5  THEN 25
                WHEN labelled::NUMERIC / n_acc >= 0.34 THEN 15
                WHEN labelled > 0                      THEN 5 ELSE 0 END AS label_score
    FROM scored s
)
SELECT ROW_NUMBER() OVER (ORDER BY LEAST(100, network_score + label_score) DESC, n_acc DESC, value DESC, component_id) AS ring_id,
       component_id, n_acc, link_count, device_links, ip_links, money_flow_links, diversity, reasons,
       labelled, suspicious, n_txn, value, fast_hops, network_score, label_score,
       LEAST(100, network_score + label_score),
       CASE WHEN diversity < (SELECT p('ring_min_link_types'))                               THEN 'LOW'   -- corroboration rule
            WHEN LEAST(100, network_score + label_score) >= (SELECT p('ring_tier_critical')) THEN 'CRITICAL'
            WHEN LEAST(100, network_score + label_score) >= (SELECT p('ring_tier_high'))     THEN 'HIGH'
            WHEN LEAST(100, network_score + label_score) >= (SELECT p('ring_tier_medium'))   THEN 'MEDIUM'
            ELSE 'LOW' END
FROM pts;


-- >>> STEP: G4_membership
ALTER TABLE fraud_ring_membership DROP CONSTRAINT IF EXISTS fraud_ring_membership_ring_id_fkey,
    DROP CONSTRAINT IF EXISTS fraud_ring_membership_account_id_fkey;  -- bulk-load pattern (see E1)
INSERT INTO fraud_ring_membership (ring_id, account_id, degree, link_types, has_fraud_label)
WITH e AS (
    SELECT account_a AS a, account_b AS b, link_type FROM account_links
    UNION ALL
    SELECT account_b, account_a, link_type FROM account_links
)
SELECT r.ring_id, c.account_id,
       COUNT(DISTINCT e.b)                          AS degree,
       string_agg(DISTINCT e.link_type, ', ')       AS link_types,
       COALESCE(s.has_fraud_label, FALSE)
FROM account_components c
JOIN fraud_rings r USING (component_id)
JOIN e ON e.a = c.account_id
LEFT JOIN account_label_summary s USING (account_id)
GROUP BY r.ring_id, c.account_id, s.has_fraud_label;
ALTER TABLE fraud_ring_membership
  ADD CONSTRAINT fraud_ring_membership_ring_id_fkey FOREIGN KEY (ring_id) REFERENCES fraud_rings(ring_id),
  ADD CONSTRAINT fraud_ring_membership_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(account_id);

-- >>> STEP: G5_account_profile
ALTER TABLE account_risk_profile DROP CONSTRAINT IF EXISTS account_risk_profile_account_id_fkey,
    DROP CONSTRAINT IF EXISTS account_risk_profile_ring_id_fkey;  -- bulk-load pattern (see E1)
TRUNCATE account_risk_profile;
-- Keeps the four populations separate:
--   CONFIRMED_FRAUD_LABEL  source label on >= 1 sent txn (ground truth)
--   REQUIRES_INVESTIGATION unlabelled, and (HIGH/CRITICAL ring member, or flagged
--                          member of a MEDIUM ring, or a CRITICAL transaction)
--   POTENTIAL_RING_MEMBER  unlabelled member of a MEDIUM ring, no HIGH/CRITICAL txn
--   SUSPICIOUS             unlabelled, >= 1 HIGH/CRITICAL txn, not in a potential ring
INSERT INTO account_risk_profile
WITH tx AS (
    SELECT t.sender_account AS account_id,
           MAX(r.risk_score)                                             AS max_score,
           COUNT(*) FILTER (WHERE r.risk_tier IN ('HIGH','CRITICAL'))    AS hr_txns
    FROM transactions t JOIN transaction_risk_scores r USING (transaction_id)
    GROUP BY t.sender_account
), base AS (
    SELECT a.account_id,
           COALESCE(s.sent_txn_count, 0)                AS sent,
           COALESCE(s.fraud_txn_count, 0)               AS fraud_cnt,
           COALESCE(s.has_fraud_label, FALSE)           AS has_label,
           COALESCE(tx.max_score, 0)                    AS max_score,
           COALESCE(tx.hr_txns, 0)                      AS hr_txns,
           m.ring_id, r.risk_tier                       AS ring_tier
    FROM accounts a
    LEFT JOIN account_label_summary s USING (account_id)
    LEFT JOIN tx USING (account_id)
    LEFT JOIN fraud_ring_membership m USING (account_id)
    LEFT JOIN fraud_rings r USING (ring_id)
)
SELECT account_id, sent, fraud_cnt, has_label, max_score, hr_txns, ring_id, ring_tier,
       hr_txns > 0                                                      AS is_flagged,
       COALESCE(ring_tier IN ('CRITICAL','HIGH','MEDIUM'), FALSE)       AS is_ring_member,
       CASE WHEN has_label THEN 'CONFIRMED_FRAUD_LABEL'
            WHEN ring_tier IN ('CRITICAL','HIGH')
              OR (hr_txns > 0 AND ring_tier = 'MEDIUM')
              OR max_score >= (SELECT p('tier_critical'))                        THEN 'REQUIRES_INVESTIGATION'
            WHEN ring_tier = 'MEDIUM'                                   THEN 'POTENTIAL_RING_MEMBER'
            WHEN hr_txns > 0                                            THEN 'SUSPICIOUS'
            ELSE 'NOT_FLAGGED' END
FROM base;
ALTER TABLE account_risk_profile
  ADD CONSTRAINT account_risk_profile_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  ADD CONSTRAINT account_risk_profile_ring_id_fkey FOREIGN KEY (ring_id) REFERENCES fraud_rings(ring_id);

-- >>> STEP: G6_ring_validation
-- Label-free validation: is the share of fraud-labelled members in linked
-- groups higher than expected for accounts with the same activity level?
-- expected = sum over members of P(label | sent_txn_count) from F3.
DROP TABLE IF EXISTS ring_validation;
CREATE TABLE ring_validation AS
WITH base AS (SELECT sent_txn_count, pct_accounts_labelled / 100.0 AS rate FROM label_concentration),
acc AS (
    SELECT s.account_id, s.sent_txn_count AS n, s.has_fraud_label AS lab, r.network_score, r.ring_id
    FROM account_label_summary s                     -- senders only: receive-only accounts cannot carry a label
    LEFT JOIN fraud_ring_membership m USING (account_id)
    LEFT JOIN fraud_rings r USING (ring_id)
), banded AS (
    SELECT acc.*,
           CASE WHEN ring_id IS NULL      THEN 'Not in a linked group of 3+'
                WHEN network_score < 15   THEN 'Network score 0-14'
                WHEN network_score < 25   THEN 'Network score 15-24'
                WHEN network_score < 35   THEN 'Network score 25-34'
                ELSE                           'Network score 35+' END AS band,
           CASE WHEN ring_id IS NULL      THEN 0
                WHEN network_score < 15   THEN 1
                WHEN network_score < 25   THEN 2
                WHEN network_score < 35   THEN 3
                ELSE 4 END AS band_order
    FROM acc
)
SELECT band, band_order,
       COUNT(DISTINCT ring_id)                      AS groups,
       COUNT(*)                                     AS sender_accounts,
       ROUND(AVG(n), 2)                             AS avg_sent_txns,
       SUM(lab::INT)                                AS observed_labelled,
       ROUND(SUM(b.rate), 1)                        AS expected_labelled,
       ROUND(SUM(lab::INT) / NULLIF(SUM(b.rate), 0), 3)                                AS observed_to_expected,
       ROUND((SUM(lab::INT) - 1.96 * SQRT(SUM(lab::INT)::NUMERIC)) / NULLIF(SUM(b.rate), 0), 3) AS oe_ci95_low,
       ROUND((SUM(lab::INT) + 1.96 * SQRT(SUM(lab::INT)::NUMERIC)) / NULLIF(SUM(b.rate), 0), 3) AS oe_ci95_high,
       ROUND(100.0 * AVG(lab::INT), 2)              AS pct_labelled
FROM banded JOIN base b ON b.sent_txn_count = banded.n
GROUP BY band, band_order;

-- >>> STEP: G7_size_distribution
DROP TABLE IF EXISTS ring_size_distribution;
CREATE TABLE ring_size_distribution AS
SELECT CASE WHEN account_count <= 4 THEN '3-4' WHEN account_count <= 9 THEN '5-9'
            WHEN account_count <= 19 THEN '10-19' WHEN account_count <= 49 THEN '20-49' ELSE '50+' END AS size_band,
       CASE WHEN account_count <= 4 THEN 1 WHEN account_count <= 9 THEN 2
            WHEN account_count <= 19 THEN 3 WHEN account_count <= 49 THEN 4 ELSE 5 END AS size_band_order,
       COUNT(*)                                          AS groups,
       SUM(account_count)                                AS accounts,
       COUNT(*) FILTER (WHERE risk_tier = 'CRITICAL')    AS critical,
       COUNT(*) FILTER (WHERE risk_tier = 'HIGH')        AS high,
       COUNT(*) FILTER (WHERE risk_tier = 'MEDIUM')      AS medium,
       COUNT(*) FILTER (WHERE risk_tier = 'LOW')         AS low,
       COUNT(*) FILTER (WHERE link_type_diversity >= 2)  AS multi_link_type_groups,
       ROUND(AVG(labelled_fraud_accounts::NUMERIC / account_count) * 100, 2) AS avg_pct_members_labelled
FROM fraud_rings
GROUP BY 1, 2;

-- >>> STEP: G8_status_summary
DROP TABLE IF EXISTS account_status_summary;
CREATE TABLE account_status_summary AS
SELECT investigation_status,
       COUNT(*)                                            AS accounts,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 3)  AS pct_of_accounts,
       ROUND(AVG(sent_txn_count), 2)                       AS avg_sent_txns,
       COUNT(*) FILTER (WHERE is_ring_member)              AS in_potential_ring,
       COUNT(*) FILTER (WHERE is_flagged)                  AS flagged
FROM account_risk_profile
GROUP BY investigation_status;


/* =============================================================================
   H. RANKING AND INVESTIGATION
   ============================================================================= */
-- >>> STEP: H1_views
-- Ring leaderboard: RANK / DENSE_RANK / ROW_NUMBER, partitioned COUNT/AVG, running SUM.
CREATE OR REPLACE VIEW vw_ring_leaderboard AS
SELECT r.ring_id, r.risk_tier, r.ring_risk_score, r.network_score, r.label_score,
       r.account_count, r.link_type_diversity, r.connection_reasons,
       r.labelled_fraud_accounts, r.suspicious_txn_count, r.total_txn_count, r.total_value_ngn,
       RANK()       OVER (ORDER BY r.ring_risk_score DESC)                               AS risk_rank,
       DENSE_RANK() OVER (ORDER BY r.ring_risk_score DESC)                               AS dense_risk_rank,
       ROW_NUMBER() OVER (PARTITION BY r.risk_tier ORDER BY r.total_value_ngn DESC, r.ring_id) AS value_rank_in_tier,
       COUNT(*)     OVER (PARTITION BY r.risk_tier)                                      AS rings_in_tier,
       ROUND(AVG(r.account_count) OVER (PARTITION BY r.risk_tier), 2)                    AS avg_accounts_in_tier,
       ROUND(100.0 * r.total_value_ngn / SUM(r.total_value_ngn) OVER (), 4)              AS pct_of_ring_value,
       SUM(r.total_value_ngn) OVER (ORDER BY r.ring_risk_score DESC, r.ring_id ROWS UNBOUNDED PRECEDING) AS cumulative_value_ngn
FROM fraud_rings r
WHERE r.risk_tier <> 'LOW';

-- Key accounts inside each ring: ROW_NUMBER by connectivity, share of ring value.
CREATE OR REPLACE VIEW vw_ring_key_accounts AS
SELECT m.ring_id, m.account_id, m.degree, m.link_types, m.has_fraud_label,
       ap.investigation_status, ap.sent_txn_count, ap.max_txn_risk_score, sv.sent_value_ngn,
       ROW_NUMBER() OVER (PARTITION BY m.ring_id ORDER BY m.degree DESC, sv.sent_value_ngn DESC NULLS LAST, m.account_id) AS member_rank,
       ROUND(100.0 * sv.sent_value_ngn / NULLIF(SUM(sv.sent_value_ngn) OVER (PARTITION BY m.ring_id), 0), 2)       AS pct_of_ring_value,
       ROUND(AVG(m.degree) OVER (PARTITION BY m.ring_id), 2)                                                      AS avg_degree_in_ring,
       COUNT(*) OVER (PARTITION BY m.ring_id)                                                                     AS ring_size
FROM fraud_ring_membership m
JOIN account_risk_profile ap USING (account_id)
LEFT JOIN LATERAL (SELECT SUM(t.amount_ngn) AS sent_value_ngn FROM transactions t WHERE t.sender_account = m.account_id) sv ON TRUE;

-- Timeline of flagged accounts: LAG / LEAD for behaviour change, running totals.
CREATE OR REPLACE VIEW vw_flagged_account_timeline AS
SELECT t.sender_account, t.transaction_id, t.txn_ts, t.receiver_account, t.amount_ngn, t.transaction_type,
       t.device_id, host(t.ip_address) AS ip_address, r.risk_score, r.risk_tier, r.flag_list, l.is_fraud,
       LAG(t.txn_ts)  OVER w                                                   AS prev_txn_ts,
       LEAD(t.txn_ts) OVER w                                                   AS next_txn_ts,
       ROUND(EXTRACT(EPOCH FROM t.txn_ts - LAG(t.txn_ts) OVER w) / 3600.0, 2)  AS hours_since_prev,
       (t.device_id  IS DISTINCT FROM LAG(t.device_id)  OVER w)                AS device_changed,
       (t.ip_address IS DISTINCT FROM LAG(t.ip_address) OVER w)                AS ip_changed,
       ROUND(t.amount_ngn / NULLIF(LAG(t.amount_ngn) OVER w, 0), 2)            AS amount_vs_prev,
       SUM(t.amount_ngn) OVER (w ROWS UNBOUNDED PRECEDING)                     AS running_sent_ngn,
       COUNT(*) OVER (PARTITION BY t.sender_account)                           AS account_txn_count
FROM transactions t
JOIN transaction_risk_scores r USING (transaction_id)
JOIN fraud_labels l USING (transaction_id)
WHERE t.sender_account IN (SELECT account_id FROM account_risk_profile WHERE is_flagged)
WINDOW w AS (PARTITION BY t.sender_account ORDER BY t.txn_ts, t.transaction_id);

-- >>> STEP: H2_cases
TRUNCATE investigation_cases;
-- Case queue: one case per HIGH/CRITICAL ring, plus unlabelled accounts with a
-- CRITICAL transaction that are not already covered by such a ring.
INSERT INTO investigation_cases (case_id, case_type, ring_id, account_id, priority_rank, risk_tier, risk_score, case_reason)
WITH c AS (
    SELECT 'RING' AS case_type, ring_id, NULL::BIGINT AS account_id, risk_tier,
           ring_risk_score AS score, account_count AS tiebreak,
           account_count || ' linked accounts: ' || connection_reasons AS reason
    FROM fraud_rings WHERE risk_tier IN ('CRITICAL','HIGH')
  UNION ALL
    SELECT 'ACCOUNT', NULL, ap.account_id, 'CRITICAL', ap.max_txn_risk_score, ap.high_risk_txn_count,
           'Unlabelled account, ' || ap.high_risk_txn_count || ' HIGH/CRITICAL txn(s); top flags: ' || top.flag_list
    FROM account_risk_profile ap
    CROSS JOIN LATERAL (
        SELECT r.flag_list FROM transactions t JOIN transaction_risk_scores r USING (transaction_id)
        WHERE t.sender_account = ap.account_id ORDER BY r.risk_score DESC, t.txn_ts LIMIT 1) top
    WHERE ap.max_txn_risk_score >= (SELECT p('tier_critical'))
      AND NOT ap.has_fraud_label
      AND COALESCE(ap.ring_risk_tier, 'NONE') NOT IN ('CRITICAL','HIGH')
)
SELECT CASE case_type WHEN 'RING' THEN 'RC-' ELSE 'AC-' END
         || lpad(ROW_NUMBER() OVER (PARTITION BY case_type ORDER BY score DESC, tiebreak DESC, COALESCE(ring_id, account_id))::TEXT, 6, '0'),
       case_type, ring_id, account_id,
       DENSE_RANK() OVER (ORDER BY score DESC),
       risk_tier, score, reason
FROM c;


/* =============================================================================
   I. FRAUD-RULE EVALUATION
   ---------------------------------------------------------------------------
   Proposed rule: "If a NEW DEVICE initiates a TRANSFER > $500 within ONE HOUR
   OF ACCOUNT CREATION, hold for manual review."
   Field check: device id YES (device_id + source new-device flag), timestamp YES,
   amount YES (NGN -> USD needs an FX assumption), TRANSFER type YES, label YES,
   ACCOUNT-CREATION TIMESTAMP - NO. The rule cannot be evaluated as written.
   Proxy: "within one hour of the account's FIRST OBSERVED activity" - labelled
   PROXY everywhere; accounts first seen near 2023-01-01 are left-censored.
   ============================================================================= */
-- >>> STEP: I1_rule_eval
DROP TABLE IF EXISTS rule_eval_base;
CREATE UNLOGGED TABLE rule_eval_base AS
SELECT f.transaction_id,
       f.is_fraud::INT                                         AS y,
       f.amount_ngn                                            AS amt,
       f.amount_ngn / (SELECT p('usd_ngn_rate'))                        AS amt_usd,
       (f.transaction_type = 'TRANSFER')                       AS is_transfer,
       f.src_new_device_flag                                   AS nd_src,
       (f.device_seq_for_sender = 1 AND f.sender_seq > 1)      AS nd_derived,
       EXTRACT(EPOCH FROM f.txn_ts - a.first_seen_ts) / 3600.0 AS hrs_since_first_seen,
       r.risk_score, r.risk_tier,
       (nf.transaction_id IS NOT NULL)                         AS has_network_flag,
       (hv.transaction_id IS NOT NULL)                         AS is_high_value,
       (abs(hashtext(f.transaction_id)) % 100 = 0)             AS random_1pct
FROM txn_features f
JOIN accounts a ON a.account_id = f.sender_account
JOIN transaction_risk_scores r USING (transaction_id)
LEFT JOIN (SELECT DISTINCT transaction_id FROM suspicious_activity_flags
           WHERE flag_code IN ('SHARED_DEVICE_1H','SHARED_IP_10M','RAPID_PASS_THROUGH')) nf USING (transaction_id)
LEFT JOIN (SELECT transaction_id FROM suspicious_activity_flags WHERE flag_code = 'HIGH_VALUE') hv USING (transaction_id);

DROP TABLE IF EXISTS rule_evaluation;
CREATE TABLE rule_evaluation AS
WITH period AS (
    SELECT EXTRACT(EPOCH FROM MAX(txn_ts) - MIN(txn_ts)) / 86400.0 AS days FROM transactions
), tot AS (
    SELECT COUNT(*) AS n, SUM(y) AS f, SUM(amt) FILTER (WHERE y = 1) AS fraud_value FROM rule_eval_base
), hits AS (
    SELECT v.rule_id,
           SUM(CASE WHEN v.hit AND b.y = 1 THEN 1 ELSE 0 END)            AS tp,
           SUM(CASE WHEN v.hit AND b.y = 0 THEN 1 ELSE 0 END)            AS fp,
           SUM(CASE WHEN NOT v.hit AND b.y = 1 THEN 1 ELSE 0 END)        AS fn,
           SUM(CASE WHEN NOT v.hit AND b.y = 0 THEN 1 ELSE 0 END)        AS tn,
           SUM(CASE WHEN v.hit AND b.y = 1 THEN b.amt ELSE 0 END)        AS fraud_value_caught,
           SUM(CASE WHEN v.hit THEN b.amt ELSE 0 END)                    AS value_held
    FROM rule_eval_base b
    CROSS JOIN LATERAL (VALUES
        ('R1',  b.nd_src AND b.is_transfer AND b.amt_usd > 500 AND b.hrs_since_first_seen <= 1),
        ('R1D', b.nd_derived AND b.is_transfer AND b.amt_usd > 500 AND b.hrs_since_first_seen <= 1),
        ('R2',  b.nd_src AND b.is_transfer AND b.amt_usd > 500 AND b.hrs_since_first_seen <= 24),
        ('R3',  b.nd_src AND b.is_transfer AND b.amt_usd > 500 AND b.hrs_since_first_seen <= 168),
        ('R4',  b.nd_src AND b.is_transfer AND b.amt_usd > 500),
        ('R5',  b.nd_src AND b.amt_usd > 500),
        ('R6',  b.nd_src),
        ('R7',  b.is_transfer AND b.amt_usd > 500),
        ('R8',  b.nd_src AND b.is_high_value),
        ('R9',  b.nd_src AND b.has_network_flag),
        ('R10', b.risk_tier IN ('HIGH','CRITICAL')),
        ('R11', b.risk_tier = 'CRITICAL'),
        ('R12', b.random_1pct)
    ) AS v(rule_id, hit)
    GROUP BY v.rule_id
), meta(rule_order, rule_id, rule_name, rule_definition, evaluation_status) AS (VALUES
    (0,  'R0',  'Proposed rule AS WRITTEN',             'New device AND transfer > $500 AND <= 1 h after ACCOUNT CREATION', 'NOT_EVALUABLE - no account-creation timestamp in source'),
    (1,  'R1',  'Proposed rule - proxy (1 h)',           'Source new-device flag AND TRANSFER AND > $500 AND <= 1 h after FIRST OBSERVED activity', 'PROXY'),
    (2,  'R1D', 'Proposed rule - proxy, derived device', 'First use of device_id by sender AND TRANSFER AND > $500 AND <= 1 h after first observed activity', 'PROXY'),
    (3,  'R2',  'Proxy, 24 h account-age window',        'Source new-device flag AND TRANSFER AND > $500 AND <= 24 h after first observed activity', 'PROXY'),
    (4,  'R3',  'Proxy, 7 day account-age window',       'Source new-device flag AND TRANSFER AND > $500 AND <= 7 days after first observed activity', 'PROXY'),
    (5,  'R4',  'New device + transfer > $500',          'Source new-device flag AND TRANSFER AND > $500 (no account-age condition)', 'EVALUATED'),
    (6,  'R5',  'New device + any type > $500',          'Source new-device flag AND amount > $500', 'EVALUATED'),
    (7,  'R6',  'New device only',                       'Source new-device flag', 'EVALUATED'),
    (8,  'R7',  'Transfer > $500 only',                  'TRANSFER AND amount > $500', 'EVALUATED'),
    (9,  'R8',  'New device + top-5% amount',            'Source new-device flag AND amount >= P95', 'EVALUATED'),
    (10, 'R9',  'New device + network evidence',         'Source new-device flag AND (shared device 1 h OR shared IP 10 min OR rapid pass-through)', 'EVALUATED'),
    (11, 'R10', 'Engine score HIGH or CRITICAL',         'Transaction risk score >= tier_high', 'EVALUATED'),
    (12, 'R11', 'Engine score CRITICAL',                 'Transaction risk score >= tier_critical', 'EVALUATED'),
    (13, 'R12', 'Random 1% review (benchmark)',          'Deterministic pseudo-random 1% sample: abs(hashtext(transaction_id)) % 100 = 0', 'BENCHMARK')
)
SELECT m.rule_order, m.rule_id, m.rule_name, m.rule_definition, m.evaluation_status,
       h.tp + h.fp                                                        AS txns_captured,
       h.tp                                                               AS true_positives,
       h.fp                                                               AS false_positives,
       h.fn                                                               AS false_negatives,
       h.tn                                                               AS true_negatives,
       ROUND(100.0 * h.tp / NULLIF(h.tp + h.fp, 0), 3)                    AS precision_pct,
       ROUND(100.0 * h.tp / NULLIF(h.tp + h.fn, 0), 3)                    AS recall_pct,
       ROUND(100.0 * h.fp / NULLIF(h.fp + h.tn, 0), 3)                    AS false_positive_rate_pct,
       ROUND(100.0 * h.fraud_value_caught / NULLIF(t.fraud_value, 0), 3)  AS fraud_value_capture_pct,
       ROUND(100.0 * (h.tp + h.fp) / t.n, 3)                              AS pct_volume_for_review,
       ROUND((1.0 * h.tp / NULLIF(h.tp + h.fp, 0)) / (1.0 * t.f / t.n), 3) AS lift_vs_base_rate,
       ROUND(1.0 * h.fp / NULLIF(h.tp, 0), 2)                             AS false_positives_per_fraud_caught,
       ROUND((h.tp + h.fp) / pr.days, 1)                                  AS reviews_per_day,
       ROUND(2.0 * h.tp / NULLIF(2 * h.tp + h.fp + h.fn, 0), 4)           AS f1_score,
       h.value_held                                                       AS value_held_ngn
FROM meta m
LEFT JOIN hits h USING (rule_id)
CROSS JOIN tot t CROSS JOIN period pr;

-- >>> STEP: I2_threshold_sweep
DROP TABLE IF EXISTS rule_threshold_sweep;
CREATE TABLE rule_threshold_sweep AS
WITH tot AS (SELECT COUNT(*) AS n, SUM(y) AS f FROM rule_eval_base),
s AS (
    SELECT 'A_NEWDEV_TRANSFER_AMOUNT_USD' AS sweep, x.v AS threshold_value,
           SUM(b.y) FILTER (WHERE b.nd_src AND b.is_transfer AND b.amt_usd > x.v) AS tp,
           COUNT(*) FILTER (WHERE b.nd_src AND b.is_transfer AND b.amt_usd > x.v) AS hits
    FROM rule_eval_base b CROSS JOIN (VALUES (0),(50),(100),(250),(500),(1000),(2500),(5000),(10000)) x(v)
    GROUP BY x.v
  UNION ALL
    SELECT 'B_ANY_TXN_AMOUNT_USD', x.v,
           SUM(b.y) FILTER (WHERE b.amt_usd > x.v), COUNT(*) FILTER (WHERE b.amt_usd > x.v)
    FROM rule_eval_base b CROSS JOIN (VALUES (0),(50),(100),(250),(500),(1000),(2500),(5000),(10000)) x(v)
    GROUP BY x.v
  UNION ALL
    SELECT 'C_PROXY_ACCOUNT_AGE_HOURS', x.v,
           SUM(b.y) FILTER (WHERE b.nd_src AND b.is_transfer AND b.amt_usd > 500 AND b.hrs_since_first_seen <= x.v),
           COUNT(*) FILTER (WHERE b.nd_src AND b.is_transfer AND b.amt_usd > 500 AND b.hrs_since_first_seen <= x.v)
    FROM rule_eval_base b CROSS JOIN (VALUES (1),(6),(24),(72),(168),(720),(2160),(8760)) x(v)
    GROUP BY x.v
  UNION ALL
    SELECT 'D_ENGINE_MIN_SCORE', x.v,
           SUM(b.y) FILTER (WHERE b.risk_score >= x.v), COUNT(*) FILTER (WHERE b.risk_score >= x.v)
    FROM rule_eval_base b CROSS JOIN (VALUES (0),(10),(15),(20),(25),(30),(35),(40),(45),(50),(55),(60),(65),(70)) x(v)
    GROUP BY x.v
)
SELECT s.sweep, s.threshold_value,
       s.hits                                                       AS txns_captured,
       COALESCE(s.tp, 0)                                            AS true_positives,
       s.hits - COALESCE(s.tp, 0)                                   AS false_positives,
       t.f - COALESCE(s.tp, 0)                                      AS false_negatives,
       ROUND(100.0 * COALESCE(s.tp, 0) / NULLIF(s.hits, 0), 3)      AS precision_pct,
       ROUND(100.0 * COALESCE(s.tp, 0) / t.f, 3)                    AS recall_pct,
       ROUND(100.0 * (s.hits - COALESCE(s.tp, 0)) / (t.n - t.f), 3) AS false_positive_rate_pct,
       ROUND(100.0 * s.hits / t.n, 3)                               AS pct_volume_for_review
FROM s CROSS JOIN tot t;


/* =============================================================================
   K. KPI SUMMARY (feeds README, memo and dashboard reconciliation)
   ============================================================================= */
-- >>> STEP: K_kpi
DROP TABLE IF EXISTS kpi_summary;
CREATE TABLE kpi_summary AS
SELECT * FROM (VALUES
  (1,  'total_transactions',        (SELECT COUNT(*) FROM transactions)::NUMERIC, 'All source transactions analysed'),
  (2,  'total_accounts',            (SELECT COUNT(*) FROM accounts)::NUMERIC, 'Distinct accounts seen as sender or receiver'),
  (3,  'sender_accounts',           (SELECT COUNT(*) FROM account_label_summary)::NUMERIC, 'Accounts that sent >= 1 transaction'),
  (4,  'fraud_transactions',        (SELECT COUNT(*) FROM fraud_labels WHERE is_fraud)::NUMERIC, 'Source label is_fraud = True'),
  (5,  'fraud_rate_pct',            (SELECT ROUND(100.0 * AVG(is_fraud::INT), 3) FROM fraud_labels), 'Fraud transactions / all transactions'),
  (6,  'fraud_labelled_accounts',   (SELECT COUNT(*) FROM account_label_summary WHERE has_fraud_label)::NUMERIC, 'Accounts that sent >= 1 labelled transaction'),
  (7,  'flagged_transactions',      (SELECT COUNT(*) FROM transaction_risk_scores WHERE risk_tier IN ('HIGH','CRITICAL'))::NUMERIC, 'Rule tier HIGH or CRITICAL'),
  (8,  'suspicious_value_ngn',      (SELECT SUM(t.amount_ngn) FROM transactions t JOIN transaction_risk_scores r USING (transaction_id) WHERE r.risk_tier IN ('HIGH','CRITICAL')), 'Value of HIGH/CRITICAL transactions'),
  (9,  'flagged_accounts',          (SELECT COUNT(*) FROM account_risk_profile WHERE is_flagged)::NUMERIC, 'Accounts with >= 1 HIGH/CRITICAL sent transaction'),
  (10, 'flagged_accounts_in_ring',  (SELECT COUNT(*) FROM account_risk_profile WHERE is_flagged AND is_ring_member)::NUMERIC, 'Flagged accounts that belong to a potential fraud ring (ring tier >= MEDIUM)'),
  (11, 'high_risk_accounts',        (SELECT COUNT(*) FROM account_risk_profile WHERE sent_txn_count > 0 AND (max_txn_risk_score >= (SELECT p('tier_critical')) OR ring_risk_tier IN ('CRITICAL','HIGH')))::NUMERIC, 'Sending accounts with a CRITICAL transaction or in a HIGH/CRITICAL ring'),
  (12, 'linked_groups_3plus',       (SELECT COUNT(*) FROM fraud_rings)::NUMERIC, 'Connected groups of >= 3 linked accounts'),
  (13, 'potential_fraud_rings',     (SELECT COUNT(*) FROM fraud_rings WHERE risk_tier IN ('CRITICAL','HIGH','MEDIUM'))::NUMERIC, 'Linked groups scored MEDIUM or above'),
  (14, 'high_critical_rings',       (SELECT COUNT(*) FROM fraud_rings WHERE risk_tier IN ('CRITICAL','HIGH'))::NUMERIC, 'Linked groups scored HIGH or CRITICAL'),
  (15, 'accounts_requiring_investigation', (SELECT COUNT(*) FROM account_risk_profile WHERE investigation_status = 'REQUIRES_INVESTIGATION')::NUMERIC, 'Unlabelled accounts meeting investigation criteria'),
  (16, 'investigation_cases',       (SELECT COUNT(*) FROM investigation_cases)::NUMERIC, 'Open cases in the queue'),
  (17, 'device_linked_pairs_1h',    (SELECT COUNT(*) FROM device_linkage_pairs)::NUMERIC, 'Txn pairs: same device, different accounts, <= 1 h'),
  (18, 'ip_overlap_pairs_10m',      (SELECT COUNT(*) FROM ip_linkage_pairs)::NUMERIC, 'Txn pairs: same IP, different accounts, <= 10 min (all IP classes)'),
  (19, 'ip_overlap_pairs_eligible', (SELECT COUNT(*) FROM ip_linkage_pairs WHERE network_class <> 'HIGH_DENSITY_SHARED')::NUMERIC, 'As above, excluding high-density shared networks'),
  (20, 'money_flow_hops_24h',       (SELECT COUNT(*) FROM flow_hops)::NUMERIC, 'A->B followed by B->C within 24 h'),
  (21, 'money_flow_maximal_paths',  (SELECT COUNT(*) FROM money_flow_paths)::NUMERIC, 'Maximal chains from the recursive CTE'),
  (22, 'velocity_spec_breaches',    (SELECT accounts_breaching_spec_rule FROM velocity_spec_check)::NUMERIC, 'Accounts with > 5 txns in any 10-minute window')
) AS k(kpi_order, kpi, value, definition);
