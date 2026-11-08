"""
01_clean_data.py
-----------------------------------------------------------------------------
Data preparation pipeline for the Fintech Transaction Fraud Ring Detection
project.

Input  : data/raw/V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv
         (never modified - read-only)
Output : data/cleaned/transactions_clean.csv          (full, 5,000,000 rows; git-ignored)
         data/cleaned/transactions_clean_sample_10k.csv (committed sample)
         data/processed/data_quality_checks.csv       (every validation rule + result)
         data/processed/column_profile.csv            (per-column profile of the raw file)

Engine : DuckDB (in-process, no server). Every rule below is a plain SQL expression,
         so the same logic can be ported to PostgreSQL if needed.

Run    : python scripts/01_clean_data.py
-----------------------------------------------------------------------------
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv"
CLEAN_DIR = ROOT / "data" / "cleaned"
PROC_DIR = ROOT / "data" / "processed"
EXPECTED_SHA256 = "ed7753fbd7da6d775ce1cd0a339c15abdcc304dae6f668643d15f7e9427cbe09"

# Source merchant_category values are merchant/biller names. They are mapped to
# 11 analytical merchant groups. The original value is always kept.
MERCHANT_GROUPS = {
    "Bolt Ride": "Travel & Transport",
    "ABC Transport Ticket": "Travel & Transport",
    "Arik Air Flight": "Travel & Transport",
    "SPAR Purchase": "Retail & Groceries",
    "Shoprite Purchase": "Retail & Groceries",
    "Local Market Purchase": "Retail & Groceries",
    "Jumia Purchase": "E-commerce",
    "Konga Order": "E-commerce",
    "Paystack Payment": "Payment Gateway",
    "Flutterwave Payment": "Payment Gateway",
    "Bet9ja Stake": "Betting & Gaming",
    "SportyBet Deposit": "Betting & Gaming",
    "ATM Withdrawal": "Cash Access",
    "POS Withdrawal": "Cash Access",
    "Ikeja Electric Bill": "Bills & Utilities",
    "DSTV Payment": "Bills & Utilities",
    "Airtime Top-up (MTN)": "Telecom",
    "Data Subscription (Airtel)": "Telecom",
    "Filmhouse Cinemas Ticket": "Entertainment",
    "Church Offering": "Donations",
    "Other Transaction": "Other",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if not RAW.exists():
        sys.exit(f"Raw file not found: {RAW}\nRun scripts/00_download_data.py first.")
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    PROC_DIR.mkdir(parents=True, exist_ok=True)

    digest = sha256(RAW)
    print(f"raw sha256 = {digest}")
    if digest != EXPECTED_SHA256:
        print("WARNING: checksum differs from the version analysed in this project.")

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = true")

    # ------------------------------------------------------------------ 1. load
    # Every column is read as text so that type validation is explicit.
    con.execute(f"""
        CREATE TABLE raw AS
        SELECT * FROM read_csv('{RAW.as_posix()}', header = true, all_varchar = true)
    """)
    raw_cols = [r[0] for r in con.execute("DESCRIBE raw").fetchall()]
    expected_cols = [
        "transaction_id", "timestamp", "sender_account", "receiver_account",
        "transaction_type", "merchant_category", "location", "device_used", "is_fraud",
        "fraud_type", "time_since_last_transaction", "spending_deviation_score",
        "velocity_score", "geo_anomaly_score", "payment_channel", "ip_address",
        "device_hash", "amount_ngn", "bvn_linked", "new_device_transaction", "sender_persona",
    ]
    assert raw_cols == expected_cols, f"Unexpected schema: {raw_cols}"

    # ------------------------------------------------------- 2. column profile
    prof_sql = " UNION ALL ".join(
        f"""SELECT '{c}' AS column_name,
                   COUNT(*) AS rows,
                   COUNT(*) FILTER (WHERE {c} IS NULL OR TRIM({c}) = '') AS null_or_blank,
                   COUNT(DISTINCT {c}) AS distinct_values,
                   MIN({c}) AS min_text_value,
                   MAX({c}) AS max_text_value
            FROM raw"""
        for c in raw_cols
    )
    con.execute(f"COPY ({prof_sql}) TO '{(PROC_DIR / 'column_profile.csv').as_posix()}' (HEADER)")

    # ---------------------------------------------------- 3. validation rules
    # Each rule returns the number of offending rows. Nothing is silently dropped.
    checks = [
        ("ROW_COUNT", "Total rows in raw file", "SELECT COUNT(*) FROM raw", "info"),
        ("DUP_TRANSACTION_ID", "Duplicate transaction_id values",
         "SELECT COUNT(*) - COUNT(DISTINCT transaction_id) FROM raw", "must_be_zero"),
        ("DUP_BUSINESS_KEY", "Duplicate (sender, receiver, timestamp, amount) rows",
         "SELECT COUNT(*) - COUNT(DISTINCT (sender_account, receiver_account, timestamp, amount_ngn)) FROM raw",
         "must_be_zero"),
        ("BAD_TRANSACTION_ID", "transaction_id not matching ^T[0-9]+$",
         "SELECT COUNT(*) FROM raw WHERE NOT regexp_full_match(transaction_id, 'T[0-9]+')", "must_be_zero"),
        ("MISSING_SENDER", "Missing sender_account",
         "SELECT COUNT(*) FROM raw WHERE sender_account IS NULL OR TRIM(sender_account) = ''", "must_be_zero"),
        ("MISSING_RECEIVER", "Missing receiver_account",
         "SELECT COUNT(*) FROM raw WHERE receiver_account IS NULL OR TRIM(receiver_account) = ''", "must_be_zero"),
        ("BAD_ACCOUNT_FORMAT", "Account id not a 10-digit number (sender or receiver)",
         "SELECT COUNT(*) FROM raw WHERE NOT regexp_full_match(sender_account, '[0-9]{10}') "
         "OR NOT regexp_full_match(receiver_account, '[0-9]{10}')", "must_be_zero"),
        ("SELF_TRANSFER", "sender_account = receiver_account (kept, flagged, excluded from money-flow graph)",
         "SELECT COUNT(*) FROM raw WHERE sender_account = receiver_account", "info"),
        ("BAD_TIMESTAMP", "Timestamp not parseable",
         "SELECT COUNT(*) FROM raw WHERE TRY_CAST(timestamp AS TIMESTAMP) IS NULL", "must_be_zero"),
        ("TIMESTAMP_OUT_OF_RANGE", "Timestamp outside 2023-01-01 .. 2024-01-31",
         "SELECT COUNT(*) FROM raw WHERE TRY_CAST(timestamp AS TIMESTAMP) NOT BETWEEN "
         "TIMESTAMP '2023-01-01' AND TIMESTAMP '2024-01-31 23:59:59.999999'", "must_be_zero"),
        ("BAD_AMOUNT", "Amount not numeric",
         "SELECT COUNT(*) FROM raw WHERE TRY_CAST(amount_ngn AS DOUBLE) IS NULL", "must_be_zero"),
        ("NON_POSITIVE_AMOUNT", "Amount <= 0",
         "SELECT COUNT(*) FROM raw WHERE TRY_CAST(amount_ngn AS DOUBLE) <= 0", "must_be_zero"),
        ("BAD_TRANSACTION_TYPE", "transaction_type outside {deposit, payment, transfer, withdrawal}",
         "SELECT COUNT(*) FROM raw WHERE LOWER(TRIM(transaction_type)) NOT IN ('deposit','payment','transfer','withdrawal')",
         "must_be_zero"),
        ("BAD_DEVICE_ID", "device_hash not matching ^D[0-9]{7}$ after normalisation",
         "SELECT COUNT(*) FROM raw WHERE NOT regexp_full_match(UPPER(TRIM(device_hash)), 'D[0-9]{7}')", "must_be_zero"),
        ("BAD_IP", "ip_address not a valid dotted IPv4 (each octet 0-255)",
         "SELECT COUNT(*) FROM raw WHERE NOT regexp_full_match(TRIM(ip_address), '[0-9]{1,3}(\\.[0-9]{1,3}){3}') "
         "OR list_max(list_transform(string_split(TRIM(ip_address), '.'), lambda x: CAST(x AS INTEGER))) > 255",
         "must_be_zero"),
        ("BAD_FRAUD_LABEL", "is_fraud not in {True, False}",
         "SELECT COUNT(*) FROM raw WHERE is_fraud NOT IN ('True','False')", "must_be_zero"),
        ("FRAUD_TYPE_WITHOUT_LABEL", "fraud_type populated while is_fraud = False",
         "SELECT COUNT(*) FROM raw WHERE is_fraud = 'False' AND fraud_type IS NOT NULL", "must_be_zero"),
        ("LABEL_WITHOUT_FRAUD_TYPE", "is_fraud = True but fraud_type missing",
         "SELECT COUNT(*) FROM raw WHERE is_fraud = 'True' AND fraud_type IS NULL", "must_be_zero"),
        ("FRAUD_ROWS", "Rows labelled is_fraud = True", "SELECT COUNT(*) FROM raw WHERE is_fraud = 'True'", "info"),
        ("BAD_BOOLEAN_FLAGS", "bvn_linked / new_device_transaction not in {True, False}",
         "SELECT COUNT(*) FROM raw WHERE bvn_linked NOT IN ('True','False') OR new_device_transaction NOT IN ('True','False')",
         "must_be_zero"),
        ("UNMAPPED_MERCHANT", "merchant_category with no merchant_group mapping",
         "SELECT COUNT(*) FROM raw WHERE merchant_category NOT IN ("
         + ",".join("'" + k.replace("'", "''") + "'" for k in MERCHANT_GROUPS) + ")", "must_be_zero"),
        ("PERSONA_INCONSISTENT", "Sender accounts with more than one sender_persona",
         "SELECT COUNT(*) FROM (SELECT sender_account FROM raw GROUP BY 1 HAVING COUNT(DISTINCT sender_persona) > 1)",
         "must_be_zero"),
        ("SRC_TSL_NULL", "Source time_since_last_transaction is NULL (first event per account in source order)",
         "SELECT COUNT(*) FROM raw WHERE time_since_last_transaction IS NULL", "info"),
        ("SRC_TSL_NEGATIVE", "Source time_since_last_transaction < 0 (inconsistent with timestamps - field not used)",
         "SELECT COUNT(*) FROM raw WHERE TRY_CAST(time_since_last_transaction AS DOUBLE) < 0", "info"),
        ("TYPE_MERCHANT_CONFLICT", "transaction_type = 'deposit' paired with a cash-withdrawal merchant (synthetic-data artefact)",
         "SELECT COUNT(*) FROM raw WHERE LOWER(transaction_type) = 'deposit' AND merchant_category IN ('ATM Withdrawal','POS Withdrawal')",
         "info"),
    ]
    rows = []
    for code, desc, sql, kind in checks:
        n = con.execute(sql).fetchone()[0]
        status = "PASS" if (kind == "info" or n == 0) else "FAIL"
        rows.append((code, desc, int(n), kind, status))
        print(f"{code:<26} {n:>10,}  {status}")
    con.execute("CREATE TABLE dq (check_code VARCHAR, description VARCHAR, result BIGINT, rule_type VARCHAR, status VARCHAR)")
    con.executemany("INSERT INTO dq VALUES (?,?,?,?,?)", rows)
    con.execute(f"COPY dq TO '{(PROC_DIR / 'data_quality_checks.csv').as_posix()}' (HEADER)")
    if any(r[4] == "FAIL" for r in rows):
        sys.exit("Validation failed - see data/processed/data_quality_checks.csv")

    # ---------------------------------------------------------- 4. transform
    con.execute("CREATE TABLE merchant_map (merchant_category VARCHAR, merchant_group VARCHAR)")
    con.executemany("INSERT INTO merchant_map VALUES (?, ?)", list(MERCHANT_GROUPS.items()))

    con.execute("""
        CREATE TABLE clean AS
        SELECT
            r.transaction_id                                         AS transaction_id,
            CAST(r.timestamp AS TIMESTAMP)                           AS txn_ts,
            CAST(r.sender_account AS BIGINT)                         AS sender_account,
            CAST(r.receiver_account AS BIGINT)                       AS receiver_account,
            UPPER(TRIM(r.transaction_type))                          AS transaction_type,
            TRIM(r.merchant_category)                                AS merchant_category,
            m.merchant_group                                         AS merchant_group,
            TRIM(r.location)                                         AS location,
            UPPER(TRIM(r.device_used))                               AS device_channel,
            UPPER(REPLACE(TRIM(r.payment_channel), ' ', '_'))        AS payment_channel,
            UPPER(TRIM(r.device_hash))                               AS device_id,
            -- normalise IPv4: strip whitespace and leading zeros in each octet
            array_to_string(list_transform(string_split(TRIM(r.ip_address), '.'),
                                           lambda x: CAST(CAST(x AS INTEGER) AS VARCHAR)), '.')
                                                                     AS ip_address,
            ROUND(CAST(r.amount_ngn AS DECIMAL(18, 2)), 2)           AS amount_ngn,
            CASE r.is_fraud WHEN 'True' THEN 1 ELSE 0 END            AS is_fraud,
            r.fraud_type                                             AS fraud_type,
            CASE r.new_device_transaction WHEN 'True' THEN 1 ELSE 0 END AS src_new_device_flag,
            CASE r.bvn_linked WHEN 'True' THEN 1 ELSE 0 END          AS bvn_linked,
            TRIM(r.sender_persona)                                   AS sender_persona,
            CAST(r.time_since_last_transaction AS DOUBLE)            AS src_time_since_last_txn,
            CAST(r.spending_deviation_score AS DOUBLE)               AS src_spending_deviation_score,
            CAST(r.velocity_score AS INTEGER)                        AS src_velocity_score,
            CAST(r.geo_anomaly_score AS DOUBLE)                      AS src_geo_anomaly_score,
            CAST(CAST(r.timestamp AS TIMESTAMP) AS DATE)             AS txn_date,
            HOUR(CAST(r.timestamp AS TIMESTAMP))                     AS txn_hour,
            CASE WHEN r.sender_account = r.receiver_account THEN 1 ELSE 0 END AS is_self_transfer
        FROM raw r
        JOIN merchant_map m USING (merchant_category)
        ORDER BY txn_ts, transaction_id
    """)
    n_clean = con.execute("SELECT COUNT(*) FROM clean").fetchone()[0]
    n_raw = con.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
    assert n_clean == n_raw, "Row count changed during transformation"

    out = CLEAN_DIR / "transactions_clean.csv"
    con.execute(f"COPY clean TO '{out.as_posix()}' (HEADER, DELIMITER ',')")
    con.execute(f"""COPY (SELECT * FROM clean USING SAMPLE reservoir(10000 ROWS) REPEATABLE (42) ORDER BY txn_ts)
                    TO '{(CLEAN_DIR / 'transactions_clean_sample_10k.csv').as_posix()}' (HEADER)""")
    print(f"clean rows written: {n_clean:,} -> {out}")


if __name__ == "__main__":
    main()
