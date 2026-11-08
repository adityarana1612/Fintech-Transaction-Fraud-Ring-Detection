# Fintech Transaction Fraud Ring Detection

**An explainable, SQL-driven fraud-ring investigation system, built on PostgreSQL and run against 5,000,000 transactions.**

`PostgreSQL 16` · `Advanced SQL (recursive CTEs, window functions)` · `Fraud & Risk Analytics` · `Data Engineering` · `Excel` · `Interactive BI dashboard (no install)` · `Statistical validation`

![Fraud Ops Console](reports/dashboard_screenshot.png)

---

## 1. Project overview

This project builds the analytics a fraud team would use to look for **coordinated account-takeover rings**:

* a relational investigation database;
* a 37-step SQL detection engine;
* a recursive money-flow tracer;
* explainable behavioural flags;
* ring scoring and an investigation case queue;
* an Excel analysis workbook;
* a browser-based Fraud Ops Console;
* a CRO risk memo.

**No machine-learning models are used.** Every result can be traced back to a SQL rule and a parameter.

The analysis is deliberately honest. On this (synthetic) dataset, the engine shows that network and behavioural signals **do not** predict the fraud label, and that the proposed transfer-hold rule cannot be validated. Both are reported plainly instead of being dressed up as discoveries.

## 2. Business problem

The Risk Team is seeing more **account takeover (ATO)**. Fraudsters operate through coordinated networks, so suspicious transactions are hard to spot one at a time. The business needs to find potential rings through:

* accounts sharing **device IDs**;
* accounts sharing **IP addresses**;
* activity within **unusually short time windows**;
* **money moving between connected accounts**;
* suspicious **transaction velocity**;
* **similarity to known fraud**.

It also needs a verdict on a proposed rule: *"If a new device initiates a transfer > $500 within one hour of account creation, hold it for manual review."*

## 3. Project objectives

1. Acquire and validate a real public dataset with device, IP and sender→receiver fields.
2. Clean and model it in PostgreSQL, keeping **source labels separate from rule outputs**.
3. Build explainable SQL detection: device/IP linkage, velocity, recursive money flow, flags, lift, rings and ranking.
4. Measure, rather than assume, how every flag and rule performs against the label.
5. Deliver Excel analysis, an interactive dashboard, analytical reports and a CRO memo.
6. Prove correctness with automated QA.

## 4. Dataset source

| | |
|---|---|
| Dataset | **Nigerian Financial Transactions and Fraud Detection Dataset**, V1 file (21 original columns) |
| Publisher | [`electricsheepafrica` on Hugging Face](https://huggingface.co/datasets/electricsheepafrica/Nigerian-Financial-Transactions-and-Fraud-Detection-Dataset) |
| Size | 5,000,000 transactions · 968.8 MB · SHA-256 `ed7753fb…cbe09` |
| Licence (card) | GPL |
| Nature | **Synthetic**, engineered for Nigerian fintech fraud research (stated by the publisher). Labels are the publisher's. |

**Why this dataset?** Each candidate's schema was inspected before selection:

* **BankShield-2M** (preferred) was rejected. Only a 5,000-row sample is free, every IP is unique per transaction (so nothing is shared), and there is no receiver account.
* **PaySim** variants have no device or IP fields.
* The **Kaggle** source needed authenticated credentials.

The selected dataset is the only one with device hashes, *shared* IPs **and** sender→receiver links. 893,115 accounts both send and receive, so real chains exist, and **no synthetic extension was needed**. The only hand-made rows are a labelled unit-test fixture, loaded into a separate database. Details: [methodology.md](methodology.md).

## 5. Dataset structure

21 source columns: transaction id, timestamp, sender and receiver account, type, merchant category, city, device channel, `is_fraud`, `fraud_type`, `device_hash`, `ip_address`, `amount_ngn`, `new_device_transaction`, `bvn_linked`, persona, payment channel, and three publisher scores. The full field-by-field dictionary is in [data_dictionary.md](data_dictionary.md).

| Fact | Value |
|---|---|
| Period | 2023-01-01 → 2024-01-28 |
| Accounts | 899,937 (896,463 senders) |
| Devices / IPs | 3,835,723 / 362,362 |
| Labelled fraud | 179,553 (3.591%): Account Takeover 170,635, Identity Fraud 8,918 |
| Amounts | NGN, ₦22.80 – ₦21.6 M (median ₦164 k) |

## 6. Database architecture

PostgreSQL schema `fraud` ([sql/schema.sql](sql/schema.sql)) in two layers:

* **Core (observed):** `accounts`, `transactions`, `devices`, `ip_addresses`, `merchants`, `fraud_labels`, `account_device_mapping`, `account_ip_mapping`.
* **Derived (rule output):** `engine_parameters`, `flag_definitions`, `suspicious_activity_flags`, `transaction_risk_scores`, `account_links`, `money_flow_paths`, `fraud_rings`, `fraud_ring_membership`, `account_risk_profile`, `investigation_cases`.

The design uses primary and foreign keys, CHECK constraints, partial and GIN indexes, single-transaction loads and idempotent engine steps. See [database_schema.md](database_schema.md) and the ER diagram ([PNG](reports/er_diagram.png), [Mermaid](reports/er_diagram.mmd)).

## 7. Data cleaning

[`scripts/01_clean_data.py`](scripts/01_clean_data.py), using DuckDB:

1. Verify the checksum.
2. Assert the schema.
3. Profile every column.
4. Run **25 validation rules**, each counting offending rows: duplicates, id formats, timestamps, amounts, device/IP format, label consistency, and so on. **All passed.**
5. Standardise types and codes, normalise device ids and IP octets, and derive merchant group, date, hour and self-transfer.

No rows were dropped. Unreliable source fields (for example, `time_since_last_transaction` is negative in 2.05 M rows) are kept for lineage but not used. Results: [data/processed/data_quality_checks.csv](data/processed/data_quality_checks.csv).

## 8. Fraud detection methodology

[`sql/fraud_network.sql`](sql/fraud_network.sql) has ten sections:

| § | What | Key SQL |
|---|---|---|
| A | Shared-device pairs ≤ 1 h, plus a random-collision (Poisson) baseline | Range self-join, tuple ordering, `factorial()` |
| B | Shared-IP concurrency ≤ 10 min, plus legitimate-network classification | Index nested loop, `INET`/`CIDR`, `mode()` |
| C | Velocity, without double counting | `COUNT() OVER (RANGE BETWEEN INTERVAL … )`, `LAG/LEAD`, gaps-and-islands |
| D | Money-flow chains | **Recursive CTE**, cycle and depth guards, `@>` sub-path removal |
| E | 12 explainable flags, score, tiers | Weighted sum, parameter table |
| F | Lift, relative risk, 95% CI, recall | Log-method CI |
| G | Linkage graph → connected components → ring scoring → account statuses | Recursive reachability CTE |
| H | Investigation views and case queue | `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `LAG`, `LEAD`, `COUNT/SUM/AVG OVER` |
| I | Rule evaluation and threshold sweeps | Confusion matrices via `CROSS JOIN LATERAL (VALUES …)` |
| K | Reconciled KPI table | – |

Correlation, association and causation are kept distinct; see [methodology.md §6](methodology.md).

## 9. Recursive CTE explanation

```sql
WITH RECURSIVE chain AS (
  -- ANCHOR: every hop A->B (t1) then B->C (t1 < t2 <= t1+24h), excluding round trips
  SELECT ..., 2 AS hop_count, ARRAY[a, b, c] AS account_path, ARRAY[t1, t2] AS txn_path FROM flow_hops WHERE NOT is_round_trip
  UNION ALL
  -- RECURSIVE: extend from the last transaction
  SELECT ..., c.hop_count + 1, c.account_path || h.receiver_out, c.txn_path || h.txn_out
  FROM chain c JOIN flow_hops h ON h.txn_in = c.last_txn
  WHERE c.hop_count < 6                                   -- depth guard
    AND h.ts_out <= c.start_ts + INTERVAL '24 hours'      -- whole chain inside 24 h
    AND NOT (h.receiver_out = ANY (c.account_path))       -- cycle prevention
)
SELECT DISTINCT ON (txn_path) * FROM chain ...;            -- then keep only maximal chains (GIN @>)
```

Termination is guaranteed by the depth guard, by strictly increasing timestamps and by the cycle check. A fixture proves that A→B→C→D→A is cut before it revisits A, that a hop 26 h later is excluded, and that sub-paths are not reported twice. Full walkthrough: [analysis/money_flow_analysis.md](analysis/money_flow_analysis.md).

## 10. Device and IP linkage

* **Devices:** 594 pairs within 1 h, never more than 2 accounts. Observed device sharing matches a random draw over the id space (observed/expected ≈ 1.00), and linked accounts carry labels at the expected rate (O/E 1.01). → [device_linkage_analysis.md](analysis/device_linkage_analysis.md)
* **IPs:** `102.89.0.0/16` averages about 305 accounts per IP (carrier-NAT-like) at baseline fraud, so it is treated as a legitimate shared network. The remaining 3,018 concurrent-use pairs show lift 1.09 (CI 0.96–1.23). → [ip_linkage_analysis.md](analysis/ip_linkage_analysis.md)

## 11. Transaction velocity analysis

The brief's rule (more than 5 transactions in 10 minutes) **never fires**: the densest burst is 3. 1,072 non-overlapping bursts were found with gaps-and-islands. The calibrated velocity flag shows lift 1.11 (CI 0.83–1.49). → [transaction_velocity_analysis.md](analysis/transaction_velocity_analysis.md)

## 12. Fraud ring identification

Evidence edges come from device, IP and money flow (208,171 edges in total), grouped into connected components. Rings get a **label-free network score** and a separate **label score**. A corroboration rule means groups linked by one evidence type stay LOW.

* **1,858 potential fraud rings**: 287 CRITICAL, 976 HIGH, 595 MEDIUM, covering 21,306 accounts.
* **3,244 open investigation cases.**
* Size-adjusted validation: the most connected groups hold **about 5% more** labelled accounts than expected (O/E 1.05, CI 1.02–1.08), a weak association.

→ [fraud_ring_analysis.md](analysis/fraud_ring_analysis.md)

## 13. Fraud rule evaluation

The proposed rule is **not evaluable as written** (no account-creation timestamp). Its proxy holds 917 transactions with 46 TP and 871 FP: **5.0% precision, 0.026% recall, 0.018% FPR**, and 19 genuine customers held per fraud caught. Thirteen variants and four threshold sweeps all land between 3.6% (random) and 5.0% precision. → [fraud_rule_evaluation.md](analysis/fraud_rule_evaluation.md)

## 14. Excel analysis

[`excel/fraud_analysis.xlsx`](excel/fraud_analysis.xlsx) has 10 sheets:

Raw_Transactions · Cleaned_Transactions · Data_Dictionary · Fraud_Sample · Merchant_Analysis · Hourly_Analysis · Behavioral_Flags · Account_Velocity · Fraud_Summary · Executive_Dashboard

* **13,477 live formulas** (`COUNTIFS`, `SUMIFS`, `AVERAGEIFS`, `IF`, `IFERROR`), recalculated in Microsoft Excel with **0 errors**.
* Conditional formatting and native charts.
* Population figures from SQL appear next to sample formulas, clearly labelled.

## 15. Dashboard features

[`dashboard/fraud_ops_console.html`](dashboard/fraud_ops_console.html) opens by double-click, with no server, install or internet needed.

* **8 KPI cards:** transactions, fraud, fraud rate, flagged accounts, flagged accounts in rings, suspicious value, high-risk accounts, potential rings.
* **Network graph:** force layout; device, IP and money-flow edges with distinct styles and arrows; node shapes by status; legend; click a node to investigate.
* **Ring investigation table** with reasons and risk tiers.
* **Suspicious transaction table** with an **Investigate** drill-through: reasons, related accounts, shared devices, shared IPs, money-flow paths and the full transaction history.
* **Filters:** fraud label, risk tier, merchant category, transaction type, period and behavioural flag. Every KPI, chart and table recomputes from the full 5 M-transaction cube.
* **Charts:** fraud rate by merchant and by hour, velocity distribution, flag lift, suspicious trend, and monthly fraud rate.

## 16. Key findings

1. Fraud labels are **flat across every segment**: merchant, hour, type, channel, amount, city, persona (3.5–3.7%).
2. **Device sharing is consistent with chance**, **IP sharing with carrier NAT**, and **money chains with random transfers**. No network signal is materially associated with the label.
3. The only separator, `new_device_transaction`, is **False exactly on each account's first transaction**: a generator artefact. Fraud never occurs on an account's first transaction (4.38% fraud rate otherwise).
4. The proposed hold rule is **untestable**, and its proxy catches **0.026%** of fraud at **19 false positives per catch**.
5. Engine tiers reach about 4.2% precision against 3.6% for random review. Weights set a priori add nothing beyond the artefact.

## 17. Business recommendations

1. Do **not** deploy the manual-hold rule. If a control is needed, use **step-up authentication** for new device + transfer > $500 (about 27 events a day, no analyst load).
2. Capture **account-creation date, persistent device binding and label confirmation dates**, then re-run this engine unchanged on production data.
3. Work the 1,263 HIGH/CRITICAL rings as **leads**, starting with a 50-ring yield study.
4. Launch every rule as **champion/challenger with a random hold-out**.
5. Never act on IP density alone. Whitelist carrier NAT ranges and use IP only to corroborate other evidence.

Full memo: [reports/Risk_Memo_CRO.md](reports/Risk_Memo_CRO.md) · One page: [reports/executive_summary.md](reports/executive_summary.md)

## 18. Limitations

Synthetic data; publisher labels sitting on (often victim) sending accounts; no account-creation, KYC, ASN or geolocation data; an FX assumption of ₦750 per $1; a priori weights; transitive ring components. Full list: [limitations.md](limitations.md).

## 19. Repository structure

```
Fintech-Transaction-Fraud-Ring-Detection/
├── README.md · data_dictionary.md · methodology.md · fraud_detection_rules.md
├── database_schema.md · limitations.md · requirements.txt · .gitignore
├── data/
│   ├── raw/          README.md (source file git-ignored, 969 MB; download script + checksum)
│   ├── cleaned/      transactions_clean_sample_10k.csv, README.md (full 1.06 GB file git-ignored)
│   └── processed/    data_quality_checks.csv, column_profile.csv + 30 engine output tables
├── sql/
│   ├── schema.sql            DDL: core + derived layers
│   ├── load_core.sql         single-transaction core load + indexes
│   ├── fraud_network.sql     the detection engine (37 steps)
│   └── tests/test_fixture.sql  hand-made unit-test fixture (separate DB)
├── scripts/  00_download · 01_clean · 02_build_database · 03_build_excel · 04_build_dashboard_data · 05_er_diagram · 06_validate
├── excel/fraud_analysis.xlsx
├── dashboard/
│   ├── fraud_ops_console.html
│   └── assets/ fraud_data.js + history/h_000..127.js (on-demand account histories)
├── analysis/ fraud_pattern · device_linkage · ip_linkage · transaction_velocity · money_flow · fraud_ring · fraud_rule_evaluation
└── reports/  Risk_Memo_CRO.md · executive_summary.md · er_diagram.png · er_diagram.mmd · validation_report.md · dashboard_screenshot.png
```

### Reproduce (optional; every output is already in the repository)

```bash
pip install -r requirements.txt
python scripts/00_download_data.py && python scripts/01_clean_data.py
python scripts/02_build_database.py --dsn postgresql://user:pass@localhost:5432/fraud   # or set DATABASE_URL
python scripts/03_build_excel.py && python scripts/04_build_dashboard_data.py && python scripts/05_er_diagram.py
python scripts/06_validate.py --dsn postgresql://...
```

Quality assurance: [reports/validation_report.md](reports/validation_report.md).
