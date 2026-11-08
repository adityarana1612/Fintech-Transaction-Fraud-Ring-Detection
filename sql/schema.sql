/* =============================================================================
   schema.sql  -  Fintech Transaction Fraud Ring Detection
   PostgreSQL 14+  (tested on PostgreSQL 16.2)

   Layer 1  CORE (loaded from the cleaned source file - observed data)
            merchants, accounts, devices, ip_addresses, transactions, fraud_labels,
            account_device_mapping, account_ip_mapping
   Layer 2  DERIVED (written by sql/fraud_network.sql - analytical outputs)
            engine_parameters, flag_definitions, suspicious_activity_flags,
            account_links, money_flow_paths, fraud_rings, fraud_ring_membership,
            account_risk_profile, investigation_cases

   Every derived table is rule-based output, NOT observed fact. Columns that hold
   source labels are prefixed/named so they cannot be confused with rule flags
   (e.g. fraud_labels.is_fraud  vs  suspicious_activity_flags.flag_code).
   ============================================================================= */

DROP SCHEMA IF EXISTS fraud CASCADE;
CREATE SCHEMA fraud;
SET search_path TO fraud, public;

/* ---------------------------------------------------------------- staging */
-- Raw-typed landing table for the cleaned CSV (scripts/02_build_database.py COPYs into it)
CREATE UNLOGGED TABLE stg_transactions (
    transaction_id               TEXT,
    txn_ts                       TEXT,
    sender_account               TEXT,
    receiver_account             TEXT,
    transaction_type             TEXT,
    merchant_category            TEXT,
    merchant_group               TEXT,
    location                     TEXT,
    device_channel               TEXT,
    payment_channel              TEXT,
    device_id                    TEXT,
    ip_address                   TEXT,
    amount_ngn                   TEXT,
    is_fraud                     TEXT,
    fraud_type                   TEXT,
    src_new_device_flag          TEXT,
    bvn_linked                   TEXT,
    sender_persona               TEXT,
    src_time_since_last_txn      TEXT,
    src_spending_deviation_score TEXT,
    src_velocity_score           TEXT,
    src_geo_anomaly_score        TEXT,
    txn_date                     TEXT,
    txn_hour                     TEXT,
    is_self_transfer             TEXT
);

/* ================================================================ CORE LAYER */

-- Merchant / biller reference (21 source values -> 11 analytical groups)
CREATE TABLE merchants (
    merchant_category  VARCHAR(60)  PRIMARY KEY,
    merchant_group     VARCHAR(40)  NOT NULL
);

-- Every account seen as sender or receiver.
-- first_seen_ts is the FIRST OBSERVED activity in the dataset - it is NOT an
-- account-creation timestamp (the source has none). Accounts first seen in
-- early Jan-2023 are left-censored by the dataset start.
CREATE TABLE accounts (
    account_id         BIGINT       PRIMARY KEY,
    sender_persona     VARCHAR(20),                         -- NULL for receive-only accounts
    bvn_linked_any     BOOLEAN,                             -- NULL for receive-only accounts
    first_seen_ts      TIMESTAMP    NOT NULL,
    last_seen_ts       TIMESTAMP    NOT NULL,
    sent_txn_count     INTEGER      NOT NULL DEFAULT 0 CHECK (sent_txn_count >= 0),
    received_txn_count INTEGER      NOT NULL DEFAULT 0 CHECK (received_txn_count >= 0),
    CHECK (last_seen_ts >= first_seen_ts),
    CHECK (sender_persona IS NULL OR sender_persona IN ('Student','Trader','Salary Earner'))
);

-- Device hashes as provided by the source (normalised to upper case).
CREATE TABLE devices (
    device_id          VARCHAR(8)   PRIMARY KEY CHECK (device_id ~ '^D[0-9]{7}$'),
    first_seen_ts      TIMESTAMP    NOT NULL,
    last_seen_ts       TIMESTAMP    NOT NULL,
    txn_count          INTEGER      NOT NULL CHECK (txn_count > 0),
    account_count      INTEGER      NOT NULL CHECK (account_count > 0)
);

-- IPv4 addresses. network_class is a DERIVED context attribute (see fraud_network.sql B1)
CREATE TABLE ip_addresses (
    ip_address         INET         PRIMARY KEY,
    network_prefix16   CIDR         NOT NULL,
    first_seen_ts      TIMESTAMP    NOT NULL,
    last_seen_ts       TIMESTAMP    NOT NULL,
    txn_count          INTEGER      NOT NULL CHECK (txn_count > 0),
    account_count      INTEGER      NOT NULL CHECK (account_count > 0),
    network_class      VARCHAR(30)
);

-- Fact table: one row per source transaction.
CREATE TABLE transactions (
    transaction_id       VARCHAR(12)   PRIMARY KEY,
    txn_ts               TIMESTAMP     NOT NULL,
    sender_account       BIGINT        NOT NULL REFERENCES accounts(account_id),
    receiver_account     BIGINT        NOT NULL REFERENCES accounts(account_id),
    transaction_type     VARCHAR(12)   NOT NULL CHECK (transaction_type IN ('DEPOSIT','PAYMENT','TRANSFER','WITHDRAWAL')),
    merchant_category    VARCHAR(60)   NOT NULL REFERENCES merchants(merchant_category),
    location             VARCHAR(40)   NOT NULL,
    device_channel       VARCHAR(10)   NOT NULL,          -- source device_used (ATM/MOBILE/POS/WEB)
    payment_channel      VARCHAR(20)   NOT NULL,
    device_id            VARCHAR(8)    NOT NULL REFERENCES devices(device_id),
    ip_address           INET          NOT NULL REFERENCES ip_addresses(ip_address),
    amount_ngn           NUMERIC(14,2) NOT NULL CHECK (amount_ngn > 0),
    src_new_device_flag  BOOLEAN       NOT NULL,          -- source-provided flag, not derived
    bvn_linked           BOOLEAN       NOT NULL,
    src_spending_deviation_score NUMERIC(6,2),
    src_velocity_score   SMALLINT,
    src_geo_anomaly_score NUMERIC(4,2),
    src_time_since_last_txn DOUBLE PRECISION,             -- kept for lineage; NOT used (inconsistent with timestamps)
    txn_hour             SMALLINT      NOT NULL CHECK (txn_hour BETWEEN 0 AND 23),
    is_self_transfer     BOOLEAN       NOT NULL
);

-- Ground-truth labels exactly as published by the dataset author (1:1 with transactions).
CREATE TABLE fraud_labels (
    transaction_id   VARCHAR(12)  PRIMARY KEY REFERENCES transactions(transaction_id),
    is_fraud         BOOLEAN      NOT NULL,
    fraud_type       VARCHAR(30),
    label_source     VARCHAR(30)  NOT NULL DEFAULT 'SOURCE_DATASET',
    CHECK ((is_fraud AND fraud_type IS NOT NULL) OR (NOT is_fraud AND fraud_type IS NULL))
);

-- Bridge tables (M:N) built from observed transactions.
CREATE TABLE account_device_mapping (
    account_id     BIGINT      NOT NULL REFERENCES accounts(account_id),
    device_id      VARCHAR(8)  NOT NULL REFERENCES devices(device_id),
    first_seen_ts  TIMESTAMP   NOT NULL,
    last_seen_ts   TIMESTAMP   NOT NULL,
    txn_count      INTEGER     NOT NULL CHECK (txn_count > 0),
    PRIMARY KEY (account_id, device_id)
);

CREATE TABLE account_ip_mapping (
    account_id     BIGINT      NOT NULL REFERENCES accounts(account_id),
    ip_address     INET        NOT NULL REFERENCES ip_addresses(ip_address),
    first_seen_ts  TIMESTAMP   NOT NULL,
    last_seen_ts   TIMESTAMP   NOT NULL,
    txn_count      INTEGER     NOT NULL CHECK (txn_count > 0),
    PRIMARY KEY (account_id, ip_address)
);

/* ============================================================= DERIVED LAYER */

-- Tunable thresholds used by the detection engine (single source of truth).
CREATE TABLE engine_parameters (
    param_name   VARCHAR(40)  PRIMARY KEY,
    param_value  NUMERIC      NOT NULL,
    unit         VARCHAR(20)  NOT NULL,
    rationale    TEXT         NOT NULL
);

-- Catalogue of explainable behavioural flags and their a-priori score weights.
CREATE TABLE flag_definitions (
    flag_code    VARCHAR(30)  PRIMARY KEY,
    flag_family  VARCHAR(20)  NOT NULL CHECK (flag_family IN ('NETWORK','VELOCITY','DEVICE','AMOUNT','TIMING','MONEY_FLOW','CONTEXT')),
    weight       SMALLINT     NOT NULL CHECK (weight BETWEEN 0 AND 40),
    description  TEXT         NOT NULL,
    data_basis   VARCHAR(20)  NOT NULL CHECK (data_basis IN ('DERIVED','SOURCE_FIELD'))
);

-- One row per (transaction, rule flag) - rule output, NOT a fraud label.
CREATE TABLE suspicious_activity_flags (
    transaction_id  VARCHAR(12)  NOT NULL REFERENCES transactions(transaction_id),
    flag_code       VARCHAR(30)  NOT NULL REFERENCES flag_definitions(flag_code),
    flag_detail     TEXT,
    PRIMARY KEY (transaction_id, flag_code)
);

-- Explainable rule score per transaction (sum of flag weights) - NOT a fraud probability.
CREATE TABLE transaction_risk_scores (
    transaction_id  VARCHAR(12)  PRIMARY KEY REFERENCES transactions(transaction_id),
    risk_score      SMALLINT     NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    risk_tier       VARCHAR(10)  NOT NULL CHECK (risk_tier IN ('CRITICAL','HIGH','MEDIUM','LOW')),
    flag_count      SMALLINT     NOT NULL,
    flag_list       TEXT                       -- pipe-separated flag codes, NULL when no flags
);

-- Undirected account-to-account evidence edges (account_a < account_b).
CREATE TABLE account_links (
    account_a       BIGINT      NOT NULL REFERENCES accounts(account_id),
    account_b       BIGINT      NOT NULL REFERENCES accounts(account_id),
    link_type       VARCHAR(20) NOT NULL CHECK (link_type IN ('SHARED_DEVICE','SHARED_IP','MONEY_FLOW')),
    evidence_count  INTEGER     NOT NULL CHECK (evidence_count > 0),
    first_evidence_ts TIMESTAMP NOT NULL,
    last_evidence_ts  TIMESTAMP NOT NULL,
    min_gap_seconds NUMERIC,
    PRIMARY KEY (account_a, account_b, link_type),
    CHECK (account_a < account_b)
);

-- Maximal money-movement chains found by the recursive CTE (fraud_network.sql D).
CREATE TABLE money_flow_paths (
    path_id            BIGINT       PRIMARY KEY,
    root_transaction_id VARCHAR(12) NOT NULL REFERENCES transactions(transaction_id),
    hop_count          SMALLINT     NOT NULL CHECK (hop_count >= 2),
    account_path       BIGINT[]     NOT NULL,
    transaction_path   VARCHAR(12)[] NOT NULL,
    path_text          TEXT         NOT NULL,
    start_ts           TIMESTAMP    NOT NULL,
    end_ts             TIMESTAMP    NOT NULL,
    elapsed_minutes    NUMERIC(10,2) NOT NULL CHECK (elapsed_minutes >= 0),
    first_amount_ngn   NUMERIC(14,2) NOT NULL,
    last_amount_ngn    NUMERIC(14,2) NOT NULL,
    total_value_ngn    NUMERIC(16,2) NOT NULL,
    fraud_txn_in_path  SMALLINT     NOT NULL,
    labelled_accounts_in_path SMALLINT NOT NULL,
    CHECK (end_ts >= start_ts)
);

-- Connected groups of linked accounts (size >= 3) with rule-based risk scoring.
CREATE TABLE fraud_rings (
    ring_id               INTEGER      PRIMARY KEY,
    component_id          BIGINT       NOT NULL UNIQUE,  -- smallest account id in the connected component
    account_count        INTEGER      NOT NULL CHECK (account_count >= 3),
    link_count            INTEGER      NOT NULL,
    device_links          INTEGER      NOT NULL,
    ip_links              INTEGER      NOT NULL,
    money_flow_links      INTEGER      NOT NULL,
    link_type_diversity   SMALLINT     NOT NULL CHECK (link_type_diversity BETWEEN 1 AND 3),
    connection_reasons    TEXT         NOT NULL,
    labelled_fraud_accounts INTEGER    NOT NULL,     -- accounts with >=1 source fraud label
    suspicious_txn_count  INTEGER      NOT NULL,     -- member txns in HIGH/CRITICAL rule tier
    total_txn_count       INTEGER      NOT NULL,
    total_value_ngn       NUMERIC(18,2) NOT NULL,
    fast_flow_hops        INTEGER      NOT NULL,     -- money-flow hops forwarded within 1 hour
    network_score         SMALLINT     NOT NULL,     -- label-free evidence score
    label_score           SMALLINT     NOT NULL,     -- evidence from source fraud labels
    ring_risk_score       SMALLINT     NOT NULL CHECK (ring_risk_score BETWEEN 0 AND 100),
    risk_tier             VARCHAR(10)  NOT NULL CHECK (risk_tier IN ('CRITICAL','HIGH','MEDIUM','LOW'))
);

CREATE TABLE fraud_ring_membership (
    ring_id          INTEGER     NOT NULL REFERENCES fraud_rings(ring_id),
    account_id       BIGINT      NOT NULL REFERENCES accounts(account_id),
    degree           INTEGER     NOT NULL,        -- number of distinct linked accounts
    link_types       TEXT        NOT NULL,
    has_fraud_label  BOOLEAN     NOT NULL,
    PRIMARY KEY (ring_id, account_id),
    UNIQUE (account_id)                          -- an account belongs to at most one ring
);

-- Account-level classification that keeps labels and rule outputs separate.
CREATE TABLE account_risk_profile (
    account_id              BIGINT       PRIMARY KEY REFERENCES accounts(account_id),
    sent_txn_count          INTEGER      NOT NULL,
    labelled_fraud_txn_count INTEGER     NOT NULL,
    has_fraud_label         BOOLEAN      NOT NULL,     -- SOURCE label (ground truth)
    max_txn_risk_score      SMALLINT     NOT NULL,
    high_risk_txn_count     INTEGER      NOT NULL,     -- HIGH/CRITICAL rule tier
    ring_id                 INTEGER      REFERENCES fraud_rings(ring_id),
    ring_risk_tier          VARCHAR(10),
    is_flagged              BOOLEAN      NOT NULL,     -- >=1 HIGH/CRITICAL transaction
    is_ring_member          BOOLEAN      NOT NULL,
    investigation_status    VARCHAR(30)  NOT NULL CHECK (investigation_status IN
                             ('CONFIRMED_FRAUD_LABEL','REQUIRES_INVESTIGATION','POTENTIAL_RING_MEMBER','SUSPICIOUS','NOT_FLAGGED'))
);

-- Investigation queue generated from HIGH/CRITICAL rings and top suspicious accounts.
CREATE TABLE investigation_cases (
    case_id          VARCHAR(16)  PRIMARY KEY,
    case_type        VARCHAR(20)  NOT NULL CHECK (case_type IN ('RING','ACCOUNT')),
    ring_id          INTEGER      REFERENCES fraud_rings(ring_id),
    account_id       BIGINT       REFERENCES accounts(account_id),
    priority_rank    INTEGER      NOT NULL,
    risk_tier        VARCHAR(10)  NOT NULL,
    risk_score       SMALLINT     NOT NULL,
    case_reason      TEXT         NOT NULL,
    case_status      VARCHAR(20)  NOT NULL DEFAULT 'OPEN' CHECK (case_status IN ('OPEN','IN_REVIEW','CLOSED_FRAUD','CLOSED_LEGIT')),
    created_at       TIMESTAMP    NOT NULL DEFAULT now(),
    CHECK ((case_type = 'RING' AND ring_id IS NOT NULL) OR (case_type = 'ACCOUNT' AND account_id IS NOT NULL))
);
