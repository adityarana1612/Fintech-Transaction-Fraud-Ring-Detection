# Database Schema

* **Engine:** PostgreSQL 14+; developed and executed on **PostgreSQL 16.2**.
* **Schema:** `fraud`.
* **DDL:** [`sql/schema.sql`](sql/schema.sql) (create), [`sql/load_core.sql`](sql/load_core.sql) (populate), [`sql/fraud_network.sql`](sql/fraud_network.sql) (detection engine).
* **ER diagram:** [`reports/er_diagram.png`](reports/er_diagram.png), with Mermaid source in [`reports/er_diagram.mmd`](reports/er_diagram.mmd).

![ER diagram](reports/er_diagram.png)

## Design principles

1. **Observed facts and rule outputs live in different layers.** The core tables hold only what the source file contains. Derived tables hold engine output. A flag, score, tier or ring is never written into `fraud_labels`, and `fraud_labels` is never updated by the engine.
2. **Model only what the data supports.** There is no `customers` table and no `account_created_at` column, because the source has no customer or KYC data and no account-creation date. `accounts.first_seen_ts` is named and documented as the *first observed* activity.
3. **Explainability through the schema.** Every threshold sits in `engine_parameters`, every flag and its weight in `flag_definitions`, and every flag hit carries a readable `flag_detail`. As a result, any score can be reconstructed by joining three tables.
4. **Integrity is enforced by the database, not by convention.** Primary keys, foreign keys, CHECK constraints (formats, ranges, tier vocabularies, label/fraud_type consistency) and UNIQUE constraints (one ring per account) are all declared in the DDL.

## Core layer: observed data

| Table | PK | Foreign keys | Rows | Notable constraints |
|---|---|---|---|---|
| `merchants` | `merchant_category` | – | 21 | – |
| `accounts` | `account_id` | – | 899,937 | `last_seen_ts >= first_seen_ts`; persona vocabulary; counts ≥ 0 |
| `devices` | `device_id` | – | 3,835,723 | `device_id ~ '^D[0-9]{7}$'` |
| `ip_addresses` | `ip_address` (INET) | – | 362,362 | `network_prefix16` CIDR |
| `transactions` | `transaction_id` | sender → accounts, receiver → accounts, merchant → merchants, device → devices, ip → ip_addresses | 5,000,000 | `amount_ngn > 0`; type vocabulary; hour 0–23 |
| `fraud_labels` | `transaction_id` (FK, 1:1) | → transactions | 5,000,000 | `fraud_type` present **iff** `is_fraud` |
| `account_device_mapping` | (`account_id`, `device_id`) | → accounts, → devices | 4,999,999 | `txn_count > 0` |
| `account_ip_mapping` | (`account_id`, `ip_address`) | → accounts, → ip_addresses | 4,999,749 | `txn_count > 0` |

### Cardinalities

* `accounts` 1 — N `transactions` as sender, and 1 — N as receiver. The second relationship is the **money-flow edge** that the recursive CTE walks.
* `merchants` / `devices` / `ip_addresses` 1 — N `transactions`.
* `transactions` 1 — 1 `fraud_labels`.
* `accounts` M — N `devices` through `account_device_mapping`, and M — N `ip_addresses` through `account_ip_mapping`.

### Why `fraud_labels` is a separate table

Keeping the label in its own table makes its provenance explicit (`label_source = 'SOURCE_DATASET'`). It also prevents analysts from querying transactions and treating rule columns as truth, and it would let a production system add analyst-confirmed labels later with a different `label_source`.

## Derived layer: rule-engine output

| Table | PK | FKs | Purpose |
|---|---|---|---|
| `engine_parameters` | `param_name` | – | Single source of truth for every threshold |
| `flag_definitions` | `flag_code` | – | Flag catalogue: family, weight, DERIVED vs SOURCE_FIELD |
| `suspicious_activity_flags` | (`transaction_id`, `flag_code`) | → transactions, → flag_definitions | Rule hits with explanations |
| `transaction_risk_scores` | `transaction_id` | → transactions | Score 0–100 and tier; CHECK on tier vocabulary |
| `account_links` | (`account_a`, `account_b`, `link_type`) | → accounts ×2 | Undirected evidence edges; `CHECK (account_a < account_b)` stops duplicate edges in reverse order |
| `money_flow_paths` | `path_id` | root → transactions | Maximal chains; arrays of accounts and transactions; `hop_count >= 2`, `end_ts >= start_ts` |
| `fraud_rings` | `ring_id` | – | One row per connected group of ≥3 accounts; `component_id` UNIQUE |
| `fraud_ring_membership` | (`ring_id`, `account_id`) | → fraud_rings, → accounts | `UNIQUE (account_id)` means an account belongs to at most one ring |
| `account_risk_profile` | `account_id` | → accounts, → fraud_rings | Separates `has_fraud_label` (source) from `is_flagged`, `is_ring_member` and `investigation_status` (rules) |
| `investigation_cases` | `case_id` | → fraud_rings (0..1), → accounts (0..1) | Case queue; CHECK makes a RING case reference a ring and an ACCOUNT case reference an account |

## Indexes

| Index | Supports |
|---|---|
| `transactions (sender_account, txn_ts) INCLUDE (receiver_account, amount_ngn)` | Velocity windows, money-flow next hop, beneficiary counts |
| `transactions (receiver_account, txn_ts)` | Inbound activity, mule analysis |
| `transactions (device_id, txn_ts)` | Shared-device ±1 h range join |
| `transactions (ip_address, txn_ts)` | Shared-IP ±10 min range join (index nested loop instead of a per-IP Cartesian product) |
| `transactions (txn_ts)` | Period filters |
| partial index `fraud_labels (transaction_id) WHERE is_fraud` | Labelled-fraud lookups |
| `flow_hops (txn_in)`, `flow_hops (txn_out)` | Recursive member join |
| GIN on `money_flow_chains_all.txn_path`, `money_flow_paths.transaction_path` / `account_path` | Sub-path elimination (`@>`), account → path lookups |
| `account_components (component_id)` | Ring aggregation |

## Transactional integrity

* `load_core.sql` wraps every core insert in one `BEGIN … COMMIT`, so a failure leaves no partially loaded core tables. Indexes are built after the commit, which is faster than maintaining them during a 5 M-row load.
* Each engine step is idempotent: it uses `DROP TABLE IF EXISTS` / `TRUNCATE … CASCADE` before rebuilding. The whole engine can be re-run (`--engine-only`) or resumed from any step (`--from-step G1`).
* Staging (`stg_transactions`) is `UNLOGGED` and dropped after loading.

## Build

```text
python scripts/02_build_database.py --dsn postgresql://user:pass@host:5432/db
  schema.sql -> COPY cleaned CSV -> load_core.sql -> fraud_network.sql -> export data/processed/*.csv
```

Measured on a laptop with embedded PostgreSQL 16.2: COPY of 5 M rows took 46 s. The first core load took 57 min, because foreign keys were checked row by row (about 180 µs per row, measured). `load_core.sql` and the large engine inserts now use the **bulk-load pattern**: drop the FK, insert, then re-add it. Re-adding validates every row in one set-based query, so integrity is unchanged. This pattern was executed end to end on the fixture database by `06_validate.py`, and on the full data for the engine steps. A clean full engine run is logged per step by the build script.
