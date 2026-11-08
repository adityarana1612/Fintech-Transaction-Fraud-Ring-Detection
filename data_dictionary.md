# Data Dictionary

This project has three layers of data, and they are kept strictly apart:

| Layer | Where | What it is |
|---|---|---|
| **Source** | `data/raw/` (not altered) | 21 columns exactly as published |
| **Cleaned** | `data/cleaned/`, PostgreSQL core tables | Typed, validated and standardised source fields, plus a few clearly derived helpers |
| **Derived (rule output)** | PostgreSQL derived tables | Flags, scores, tiers, links, chains, rings and cases produced by `sql/fraud_network.sql`. **None of these is a fraud label.** |

The only ground-truth field is `is_fraud` (with `fraud_type`), as published by the dataset author.

---

## 1. Source file: `V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv`

5,000,000 rows. Every column was profiled in `data/processed/column_profile.csv`. The validation results are in `data/processed/data_quality_checks.csv`.

| # | Source column | Example | Nulls | Distinct | Used as | Notes |
|---|---|---|---|---|---|---|
| 1 | `transaction_id` | T100000 | 0 | 5,000,000 | `transaction_id` | Unique; format `T` + digits |
| 2 | `timestamp` | 2023-08-27 09:03:17.516168 | 0 | 5,000,000 | `txn_ts` | 2023-01-01 → 2024-01-28; no time zone; treated as local time |
| 3 | `sender_account` | 9899691027 | 0 | 896,463 | `sender_account` | 10-digit account |
| 4 | `receiver_account` | 5792850510 | 0 | 896,589 | `receiver_account` | 10-digit account; 893,115 accounts both send and receive |
| 5 | `transaction_type` | withdrawal | 0 | 4 | `transaction_type` (upper-case) | deposit / payment / transfer / withdrawal, about 25% each |
| 6 | `merchant_category` | Data Subscription (Airtel) | 0 | 21 | `merchant_category` + derived `merchant_group` | The values are merchant or biller names |
| 7 | `location` | Lagos | 0 | 10 | `location` | Nigerian cities |
| 8 | `device_used` | mobile | 0 | 4 | `device_channel` | atm / mobile / pos / web |
| 9 | `is_fraud` | False | 0 | 2 | `is_fraud` (0/1) | **Ground-truth label**: 179,553 True (3.591%) |
| 10 | `fraud_type` | Account Takeover | 4,820,447 | 2 | `fraud_type` | Account Takeover 170,635; Identity Fraud 8,918. Null exactly when `is_fraud` is False |
| 11 | `time_since_last_transaction` | -781.79 | 896,513 | — | kept as `src_time_since_last_txn`, **not used** | Negative in 2,051,331 rows, so it contradicts the timestamps; gaps are recomputed with `LAG()` instead |
| 12 | `spending_deviation_score` | -0.36 | 0 | — | kept, not used | Publisher score, no association with the label |
| 13 | `velocity_score` | 12 | 0 | 20 | kept, not used | Publisher score 1–20, no association with the label |
| 14 | `geo_anomaly_score` | 0.98 | 0 | — | kept, not used | Publisher score 0–1, no association with the label |
| 15 | `payment_channel` | Bank Transfer | 0 | 4 | `payment_channel` | BANK_TRANSFER / CARD / MOBILE_APP / USSD |
| 16 | `ip_address` | 41.58.183.79 | 0 | 362,362 | `ip_address` (INET) | Seven /16 networks in four Nigerian carrier ranges |
| 17 | `device_hash` | D8536477 | 0 | 3,835,723 | `device_id` | `D` + 7 digits; near-unique per transaction (see §4) |
| 18 | `amount_ngn` | 1889724.49 | 0 | — | `amount_ngn` | Nigerian naira; ₦22.80 – ₦21,628,477.86; median ₦164,278 |
| 19 | `bvn_linked` | True | 0 | 2 | `bvn_linked` | Bank Verification Number linked |
| 20 | `new_device_transaction` | False | 0 | 2 | `src_new_device_flag` | **Publisher flag.** False exactly on each sender's first transaction (by id); it does not track device history (§4) |
| 21 | `sender_persona` | Trader | 0 | 3 | `accounts.sender_persona` | Constant per sender (validated) |

**The source does not contain** an account-creation timestamp, a customer or KYC table, a device-to-customer registry, geolocation coordinates, merchant IDs, a chargeback or confirmation date, or currency conversion. Any analysis that would need these fields is reported as a limitation instead of being approximated without disclosure.

---

## 2. Cleaned transaction file: `data/cleaned/transactions_clean.csv`

There are 25 columns and one row per source row, with no rows removed. They are listed in output order.

| Column | Type | Rule |
|---|---|---|
| `transaction_id` | text | unchanged |
| `txn_ts` | timestamp(6) | `CAST(timestamp AS TIMESTAMP)`; all parse |
| `sender_account`, `receiver_account` | bigint | cast from a validated 10-digit string |
| `transaction_type` | text | `UPPER(TRIM())` |
| `merchant_category` | text | `TRIM()` |
| `merchant_group` | text | **derived** from the 21 → 11 mapping in `scripts/01_clean_data.py` |
| `location` | text | `TRIM()` |
| `device_channel` | text | `UPPER(TRIM(device_used))` |
| `payment_channel` | text | upper-case, spaces → `_` |
| `device_id` | text | `UPPER(TRIM(device_hash))`, validated `^D[0-9]{7}$` |
| `ip_address` | text/INET | each octet cast to integer (strips whitespace and leading zeros), validated 0–255 |
| `amount_ngn` | decimal(18,2) | rounded to 2 dp, validated > 0 |
| `is_fraud` | 0/1 | `'True'` → 1 |
| `fraud_type` | text | unchanged |
| `src_new_device_flag`, `bvn_linked` | 0/1 | `'True'` → 1 |
| `sender_persona` | text | `TRIM()` |
| `src_time_since_last_txn`, `src_spending_deviation_score`, `src_velocity_score`, `src_geo_anomaly_score` | numeric | cast, kept for lineage |
| `txn_date` | date | **derived** from `txn_ts` |
| `txn_hour` | 0–23 | **derived** from `txn_ts` |
| `is_self_transfer` | 0/1 | **derived** `sender_account = receiver_account` (6 rows); these are excluded from money-flow chains |

### Merchant group mapping (derived)

| merchant_group | source merchant_category values |
|---|---|
| Betting & Gaming | Bet9ja Stake, SportyBet Deposit |
| Bills & Utilities | Ikeja Electric Bill, DSTV Payment |
| Cash Access | ATM Withdrawal, POS Withdrawal |
| Donations | Church Offering |
| E-commerce | Jumia Purchase, Konga Order |
| Entertainment | Filmhouse Cinemas Ticket |
| Other | Other Transaction |
| Payment Gateway | Paystack Payment, Flutterwave Payment |
| Retail & Groceries | SPAR Purchase, Shoprite Purchase, Local Market Purchase |
| Telecom | Airtime Top-up (MTN), Data Subscription (Airtel) |
| Travel & Transport | Bolt Ride, ABC Transport Ticket, Arik Air Flight |

---

## 3. Database tables (schema `fraud`)

The full DDL is in [`sql/schema.sql`](sql/schema.sql), and the design rationale is in [`database_schema.md`](database_schema.md).

### Core layer: observed data

| Table | Grain | Key columns |
|---|---|---|
| `merchants` | merchant category | `merchant_category` PK, `merchant_group` |
| `accounts` | account (sender ∪ receiver) | `account_id` PK, `sender_persona`, `first_seen_ts` (*first observed, **not** creation*), `last_seen_ts`, `sent_txn_count`, `received_txn_count` |
| `devices` | device_id | `device_id` PK, first/last seen, `txn_count`, `account_count` (distinct senders) |
| `ip_addresses` | IPv4 | `ip_address` PK (INET), `network_prefix16` (CIDR), `account_count`, `network_class` (derived context, §5) |
| `transactions` | transaction | `transaction_id` PK; FKs to `accounts` (×2), `merchants`, `devices`, `ip_addresses` |
| `fraud_labels` | transaction (1:1) | `transaction_id` PK/FK, `is_fraud`, `fraud_type`, `label_source = 'SOURCE_DATASET'` |
| `account_device_mapping` | account × device | PK (`account_id`, `device_id`), first/last seen, `txn_count` |
| `account_ip_mapping` | account × IP | PK (`account_id`, `ip_address`), first/last seen, `txn_count` |

### Derived layer: rule-engine output

| Table | Grain | Meaning |
|---|---|---|
| `engine_parameters` | parameter | Every threshold used by the engine, with its rationale |
| `flag_definitions` | flag | Flag code, family, a-priori weight, and whether it is DERIVED or a SOURCE_FIELD |
| `suspicious_activity_flags` | transaction × flag | One row per rule hit, with a human-readable `flag_detail` |
| `transaction_risk_scores` | transaction | `risk_score` = capped sum of weights, `risk_tier`, `flag_list` |
| `account_links` | account pair × link type | Evidence edges: SHARED_DEVICE, SHARED_IP, MONEY_FLOW |
| `money_flow_paths` | maximal chain | Output of the recursive CTE: account path, transaction path, timing and value |
| `fraud_rings` | connected group (≥3 accounts) | Link counts, reasons, `network_score`, `label_score`, `ring_risk_score`, `risk_tier` |
| `fraud_ring_membership` | ring × account | `degree`, `link_types`, `has_fraud_label` |
| `account_risk_profile` | account | Keeps the source label (`has_fraud_label`) separate from rule outputs (`is_flagged`, `is_ring_member`, `investigation_status`) |
| `investigation_cases` | case | RING and ACCOUNT cases with priority, reason and status `OPEN` |

### Analytical work tables (created by `fraud_network.sql`)

`account_label_summary`, `device_linkage_pairs`, `device_linkage_summary`, `device_connectivity_distribution`, `ip_linkage_pairs`, `ip_network_summary`, `ip_class_summary`, `ip_suspicious_overlap`, `txn_features`, `txn_beneficiaries_24h`, `velocity_episodes`, `velocity_distribution`, `velocity_spec_check`, `flow_hops`, `money_flow_chains_all`, `money_flow_summary`, `money_flow_hop_stats`, `mule_concentration`, `flag_performance`, `tier_performance`, `label_concentration`, `account_components`, `ring_validation`, `ring_size_distribution`, `account_status_summary`, `rule_eval_base`, `rule_evaluation`, `rule_threshold_sweep`, `kpi_summary`; views `vw_ring_leaderboard`, `vw_ring_key_accounts`, `vw_flagged_account_timeline`.

Most of these are exported to `data/processed/*.csv` by `scripts/02_build_database.py`.

---

## 4. Field caveats that shape the analysis

* **`device_hash` behaves like a random draw.** 5,000,000 transactions draw from a 9,000,000-value id space (`D1000000`–`D9999999`). Random draws would give about 3.84 M distinct values (Poisson λ = 0.556), and 3,835,723 are observed. Most "shared devices" are therefore consistent with chance collisions. `fraud.device_connectivity_distribution` tests this explicitly.
* **`new_device_transaction` is not about devices.** It is False on exactly one transaction per sender, the account's first transaction in `transaction_id` order (896,463 rows, plus one more), and True on all 4,103,536 others. Every labelled fraud has it True. The flag therefore encodes "not the account's first generated transaction", which is a generator artefact. Real device history (first use of a `device_id`) has a lift of only 1.01.
* **`time_since_last_transaction` conflicts with the timestamps**: it is negative in 41% of rows. It is not used.
* **Merchant and type are sometimes contradictory.** For example, 104,493 `deposit` rows have a merchant of "ATM Withdrawal" or "POS Withdrawal", which is a synthetic-data artefact. These rows are kept and not "fixed".
* **Amounts are in NGN.** Rules stated in USD use an explicit FX assumption (`engine_parameters.usd_ngn_rate` = 750), and a threshold sweep is provided.

## 5. Derived categorical values

| Field | Values |
|---|---|
| `ip_addresses.network_class` | SINGLE_ACCOUNT (1 account), SMALL_SHARED (2–10), MODERATE_SHARED (11–50), HIGH_DENSITY_SHARED (>50; treated as a legitimate shared network such as carrier NAT) |
| `transaction_risk_scores.risk_tier` | LOW (<25), MEDIUM (25–44), HIGH (45–64), CRITICAL (≥65) |
| `fraud_rings.risk_tier` | LOW (<25, linked group only), MEDIUM (25–39), HIGH (40–59), CRITICAL (≥60) |
| `account_risk_profile.investigation_status` | CONFIRMED_FRAUD_LABEL, REQUIRES_INVESTIGATION, POTENTIAL_RING_MEMBER, SUSPICIOUS, NOT_FLAGGED |
| `account_links.link_type` | SHARED_DEVICE, SHARED_IP, MONEY_FLOW |
