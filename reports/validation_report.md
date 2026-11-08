# Validation Report

Generated 2026-11-08 14:35 by `scripts/06_validate.py` on PostgreSQL 16.2 + DuckDB 2.0 (Windows 11).

**69 of 69 checks passed.**

| # | Section | Check | Result | Detail |
|---|---|---|---|---|
| 1 | 1 Engine unit test (fixture) | Recursive CTE returns exactly the two expected maximal chains | PASS | [('{1000000001,1000000002,1000000003,1000000004}', '{T9000001,T9000002,T9000003}'), ('{1000000002,1000000003,1000000004,1000000001}', '{T9000002,T9000003,T90000 |
| 2 | 1 Engine unit test (fixture) | Cycle prevention: no chain revisits an account | PASS |  |
| 3 | 1 Engine unit test (fixture) | 24 h window: late hop D->E (T9000005) is in no chain | PASS |  |
| 4 | 1 Engine unit test (fixture) | Sub-paths removed (C->D->A not reported separately) | PASS |  |
| 5 | 1 Engine unit test (fixture) | Velocity: burst reported once as one 7-txn episode (no double counting) | PASS | [(7, 7, True)] |
| 6 | 1 Engine unit test (fixture) | Velocity: exactly one account breaches '>5 in 10 min' | PASS |  |
| 7 | 1 Engine unit test (fixture) | Shared device: X-Y pair (18 min) found, Z (3 h later) excluded | PASS | [(1000000020, 1000000021, Decimal('1080.0'))] |
| 8 | 1 Engine unit test (fixture) | Shared IP: X-W pair within 10 min found once | PASS | [(1000000020, 1000000023)] |
| 9 | 1 Engine unit test (fixture) | Ring detection: chain accounts A-D form one linked group of 4 | PASS | [(3,), (4,)] |
| 10 | 1 Engine unit test (fixture) | Labelled account classified CONFIRMED_FRAUD_LABEL (labels kept separate) | PASS | CONFIRMED_FRAUD_LABEL |
| 11 | 2 Independent recompute (DuckDB vs PostgreSQL) | Transaction count | PASS | 5,000,000 |
| 12 | 2 Independent recompute (DuckDB vs PostgreSQL) | Fraud transaction count | PASS | 179,553 |
| 13 | 2 Independent recompute (DuckDB vs PostgreSQL) | Shared-device pairs within 1 h | PASS | 594 |
| 14 | 2 Independent recompute (DuckDB vs PostgreSQL) | Shared-IP pairs within 10 min | PASS | 18,372 |
| 15 | 2 Independent recompute (DuckDB vs PostgreSQL) | Money-flow hops within 24 h | PASS | 105,058 |
| 16 | 2 Independent recompute (DuckDB vs PostgreSQL) | Max transactions in any 10-min window | PASS | 3 |
| 17 | 2 Independent recompute (DuckDB vs PostgreSQL) | Velocity episodes (count, txns) - gaps-and-islands | PASS | duckdb (1072, 2146) vs pg (1072, Decimal('2146')) |
| 18 | 2 Independent recompute (DuckDB vs PostgreSQL) | NEW_DEVICE_SRC lift | PASS | 1.2185 |
| 19 | 2 Independent recompute (DuckDB vs PostgreSQL) | Rule R4 confusion matrix (new device + transfer > $500) | PASS | TP 464 of 10,511 |
| 20 | 2 Independent recompute (DuckDB vs PostgreSQL) | HIGH_VALUE flag count (P95) | PASS | 250,000 |
| 21 | 3 Engine invariants | Every evaluated rule: TP+FP+FN+TN = total transactions | PASS |  |
| 22 | 3 Engine invariants | Proposed rule as written is NOT evaluated (no account-creation field) | PASS |  |
| 23 | 3 Engine invariants | Precision/recall/FPR recompute from confusion matrix | PASS |  |
| 24 | 3 Engine invariants | Lift = flagged fraud rate / overall rate (all flags) | PASS |  |
| 25 | 3 Engine invariants | Relative risk CI brackets the point estimate | PASS |  |
| 26 | 3 Engine invariants | Money-flow paths: no repeated account (cycle prevention) | PASS |  |
| 27 | 3 Engine invariants | Money-flow paths: within 24 h and depth <= 6 | PASS | max elapsed 1440.00 min |
| 28 | 3 Engine invariants | Money-flow paths: strictly increasing timestamps along each path | PASS |  |
| 29 | 3 Engine invariants | Money-flow paths: no duplicate transaction paths | PASS |  |
| 30 | 3 Engine invariants | Ring membership: each account in at most one ring, sizes consistent | PASS |  |
| 31 | 3 Engine invariants | Labels untouched: fraud_labels equals cleaned source label count | PASS |  |
| 32 | 3 Engine invariants | Every fraud-labelled account has status CONFIRMED_FRAUD_LABEL and vice versa | PASS |  |
| 33 | 3 Engine invariants | Shared-IP ring edges exclude high-density networks | PASS |  |
| 34 | 3 Engine invariants | PRIOR_FRAUD_LINK uses only strictly earlier labels (no look-ahead) | PASS |  |
| 35 | 3 Engine invariants | Risk score = capped sum of flag weights | PASS |  |
| 36 | 4 Dashboard reconciliation | Cube total = total transactions | PASS | 5000000 |
| 37 | 4 Dashboard reconciliation | Cube fraud = fraud transactions | PASS |  |
| 38 | 4 Dashboard reconciliation | Cube HIGH/CRITICAL count = flagged transactions | PASS |  |
| 39 | 4 Dashboard reconciliation | Cube suspicious value = SQL suspicious value | PASS | 159,297,694,696.92 |
| 40 | 4 Dashboard reconciliation | Cube per-flag counts = flag_performance | PASS |  |
| 41 | 4 Dashboard reconciliation | Investigation set holds every HIGH/CRITICAL transaction | PASS | 37996 |
| 42 | 4 Dashboard reconciliation | Flagged accounts (browser logic) = SQL | PASS | 36668 |
| 43 | 4 Dashboard reconciliation | Flagged accounts in rings (browser logic) = SQL | PASS | 2472 |
| 44 | 4 Dashboard reconciliation | High-risk accounts (browser logic) = SQL | PASS | 20992 |
| 45 | 4 Dashboard reconciliation | Potential rings with activity (browser logic) = SQL | PASS | 1858 |
| 46 | 4 Dashboard reconciliation | Dashboard opens from file:// and runs in headless Edge | PASS | self-test output captured |
| 47 | 4 Dashboard reconciliation | Filter scenario 'no_filter': UI KPIs = SQL | PASS | txns 5,000,000, fraud 179,553, flagged accts 36,668, rings 1,858 |
| 48 | 4 Dashboard reconciliation | Filter scenario 'fraud_only': UI KPIs = SQL | PASS | txns 179,553, fraud 179,553, flagged accts 1,614, rings 1,503 |
| 49 | 4 Dashboard reconciliation | Filter scenario 'tier_high_critical': UI KPIs = SQL | PASS | txns 37,996, fraud 1,616, flagged accts 36,668, rings 1,255 |
| 50 | 4 Dashboard reconciliation | Filter scenario 'betting_transfer': UI KPIs = SQL | PASS | txns 104,109, fraud 3,778, flagged accts 209, rings 1,178 |
| 51 | 4 Dashboard reconciliation | Filter scenario 'apr_to_jun_2023': UI KPIs = SQL | PASS | txns 1,245,395, fraud 44,653, flagged accts 7,311, rings 1,854 |
| 52 | 4 Dashboard reconciliation | Filter scenario 'flag_shared_device': UI KPIs = SQL | PASS | txns 1,188, fraud 44, flagged accts 320, rings 321 |
| 53 | 4 Dashboard reconciliation | Filter scenario 'combo_legit_critical_newdev_h1': UI KPIs = SQL | PASS | txns 554, fraud 0, flagged accts 554, rings 66 |
| 54 | 4 Dashboard reconciliation | Investigate drill-through renders all sections | PASS | ok |
| 55 | 4 Dashboard reconciliation | Network graph draws every member of the selected ring | PASS | ok |
| 56 | 5 Excel workbook | Formulas present | PASS | 13,477 formulas |
| 57 | 5 Excel workbook | Formulas calculated by Excel (cached values present) | PASS | 0 without cached value |
| 58 | 5 Excel workbook | No formula errors (#VALUE!, #REF!, #DIV/0!, #NAME?) | PASS | 0 errors |
| 59 | 5 Excel workbook | Executive fraud-rate formula = SQL | PASS | 0.0359106 |
| 60 | 5 Excel workbook | Merchant_Analysis population total = 5,000,000 | PASS | 5000000 |
| 61 | 5 Excel workbook | Merchant_Analysis sample total = 10,000 (COUNTIFS) | PASS | 10000 |
| 62 | 5 Excel workbook | Rule precision/recall/FPR: Excel formulas = SQL (to SQL's 3-dp rounding) | PASS | 0 mismatches |
| 63 | 5 Excel workbook | Hourly_Analysis sample counts sum to 10,000 | PASS |  |
| 64 | 6 Repository hygiene | All required deliverables exist and are non-empty | PASS | 28 files |
| 65 | 6 Repository hygiene | No empty files in repository | PASS | [] |
| 66 | 6 Repository hygiene | No TODO / FIXME / TBD markers | PASS | [] |
| 67 | 6 Repository hygiene | No credentials or keys in files | PASS | [] |
| 68 | 6 Repository hygiene | No tracked file above GitHub's 100 MB limit | PASS | [] |
| 69 | 6 Repository hygiene | Large raw/cleaned files are git-ignored | PASS |  |
