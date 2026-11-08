# Fraud Detection Rules

This is the catalogue of every rule in [`sql/fraud_network.sql`](sql/fraud_network.sql): its exact logic, the parameters it uses, its a-priori weight, and its **measured** performance on all 5,000,000 transactions. Every threshold lives in `fraud.engine_parameters`, so changing one row and re-running reproduces everything.

> **Rule outputs are not labels.** A flag, score, tier or ring is an explainable *lead*. Ground truth is only `fraud_labels.is_fraud`, as published with the dataset.

## 1. Engine parameters

| Parameter | Value | Use |
|---|---|---|
| `device_window_seconds` | 3,600 | Shared-device pairing window |
| `ip_window_seconds` | 600 | Shared-IP concurrency window |
| `ip_high_density_accounts` | 50 | IPs with more accounts than this = legitimate shared network (carrier-NAT-like) |
| `velocity_window_seconds` / `velocity_spec_threshold` | 600 / 5 | The brief's rule: more than 5 transactions in 10 min |
| `velocity_cal_10m` / `velocity_cal_1h` | 2 / 3 | Calibrated velocity flag (the brief's threshold never occurs) |
| `flow_window_hours` / `flow_max_depth` | 24 / 6 | Money-flow chain window and recursion depth |
| `fast_flow_minutes` | 60 | Fast pass-through |
| `high_value_percentile` | 0.95 | High value = top 5% (₦3,597,327 and above) |
| `amount_spike_multiple` | 3 | Amount ≥ 3 × sender's trailing average |
| `multi_beneficiary_24h` | 3 | Distinct receivers in 24 h |
| `night_start_hour` / `night_end_hour` | 0 / 4 | Unusual hour 00:00–04:59 |
| `usd_ngn_rate` | 750 | **Assumption** used to express USD thresholds in NGN |
| `tier_medium` / `tier_high` / `tier_critical` | 25 / 50 / 65 | Transaction tiers. HIGH+CRITICAL is sized to about 100 reviews/day. |
| `ring_tier_medium` / `ring_tier_high` / `ring_tier_critical` | 25 / 40 / 60 | Ring tiers |
| `ring_min_link_types` | 2 | Corroboration rule: a single-evidence-type group is never above LOW |

## 2. Transaction flags

| Code | Family | Weight | Logic (SQL step) | Data basis | Flagged txns | Lift | RR (95% CI) |
|---|---|---|---|---|---|---|---|
| `SHARED_DEVICE_1H` | NETWORK | 25 | Same `device_id`, different sender, ±1 h (A1 → E1) | derived | 1,188 | 1.03 | 1.03 (0.77–1.38) |
| `SHARED_IP_10M` | NETWORK | 15 | Same IP, different sender, ±10 min; IP **not** HIGH_DENSITY (B2 → E1) | derived | 6,025 | 1.09 | 1.09 (0.96–1.23) |
| `PRIOR_FRAUD_LINK` | NETWORK | 20 | Receiver, or a device/IP partner, labelled **strictly before** this transaction (no look-ahead) | derived | 465,725 | 1.00 | 1.00 (0.99–1.02) |
| `RAPID_PASS_THROUGH` | MONEY_FLOW | 20 | Transaction forwards funds received ≤ 24 h earlier (D1) | derived | 103,593 | 1.02 | 1.02 (0.99–1.06) |
| `HIGH_VELOCITY` | VELOCITY | 15 | ≥ 2 txns in trailing 10 min **or** ≥ 3 in trailing 1 h (C1 `RANGE` windows) | derived | 1,078 | 1.11 | 1.11 (0.83–1.49) |
| `MULTI_BENEFICIARY_24H` | VELOCITY | 10 | ≥ 3 distinct receivers in trailing 24 h (C2) | derived | 1,395 | 0.92 | 0.92 (0.69–1.22) |
| `NEW_DEVICE_SRC` | DEVICE | 15 | Source `new_device_transaction` = True. **Equals "not the account's first transaction" (generator artefact).** | source field | 4,103,536 | 1.22 | ∞ (no fraud when absent) |
| `FIRST_DEVICE_USE` | DEVICE | 0 | First use of this `device_id` by a sender with earlier history. Informational: device ids are near-unique. | derived | 4,103,536 | 1.01 | 1.09 (1.08–1.10) |
| `HIGH_VALUE` | AMOUNT | 15 | Amount ≥ P95 | derived | 250,000 | 1.01 | 1.01 (0.99–1.03) |
| `AMOUNT_SPIKE` | AMOUNT | 10 | ≥ 3 × trailing average of ≥ 2 prior transactions (`AVG() OVER (ROWS … 1 PRECEDING)`) | derived | 595,997 | 1.02 | 1.03 (1.01–1.04) |
| `UNUSUAL_HOUR` | TIMING | 5 | 00:00–04:59 | derived | 499,722 | 1.02 | 1.02 (1.00–1.03) |
| `SHARED_NETWORK_IP` | CONTEXT | 0 | IP serves > 50 accounts: legitimate shared network, context only | derived | 1,296,500 | 1.00 | 1.00 (1.00–1.02) |

Every hit is stored in `fraud.suspicious_activity_flags` with a readable `flag_detail`, for example `device D1268723 also used by account 2937779694 (1.8 s apart)` or `received 500000 NGN from 1000000001, forwarded after 60.00 min`.

## 3. Transaction score and tiers

`risk_score = LEAST(100, SUM(weight of flags present))`. Every score can be rebuilt from `suspicious_activity_flags ⋈ flag_definitions`, and `06_validate.py` checks this for all 5 M rows.

| Tier | Score | Txns | Precision | Recall | Lift |
|---|---|---|---|---|---|
| CRITICAL | ≥ 65 | 2,821 | 4.15% | 0.07% | 1.15 |
| HIGH | 50–64 | 35,175 | 4.26% | 0.83% | 1.19 |
| MEDIUM | 25–49 | 1,000,609 | 4.23% | 23.6% | 1.18 |
| LOW | < 25 | 3,961,395 | 3.42% | 75.5% | 0.95 |

The weights were set **before** any label performance was measured. Measured afterwards, they add no lift beyond the `NEW_DEVICE_SRC` artefact. On real data, weights should be re-estimated from measured relative risk, or replaced with logistic-regression coefficients so they stay explainable.

## 4. Linkage rules (ring detection)

| Edge | Rule | Edges |
|---|---|---|
| `SHARED_DEVICE` | Device pair within 1 h | 594 |
| `SHARED_IP` | Eligible-IP pair within 10 min | 3,018 |
| `MONEY_FLOW` | Transfer belonging to a maximal ≥ 2-transfer chain completed within 24 h | 204,559 |

**Ring score** = network score (0–75, label-free) + label score (0–25):

| Network component | Points |
|---|---|
| Size 3–4 / 5–9 / ≥ 10 | 5 / 10 / 15 |
| Link-type diversity 2 / 3 | 15 / 25 |
| Has a device link | 10 |
| Has an eligible IP link | 5 |
| Has a hop forwarded within 1 h | 10 |
| Suspicious (HIGH/CRITICAL) member transactions ≥ 1 / ≥ 3 | 5 / 10 |

| Label component (share of members labelled) | Points |
|---|---|
| ≥ 50% / ≥ 34% / > 0 | 25 / 15 / 5 |

Corroboration rule: diversity < 2 means LOW. **Potential fraud ring = MEDIUM+** (1,858 rings).

## 5. Account statuses

| Status | Rule | Accounts |
|---|---|---|
| CONFIRMED_FRAUD_LABEL | Source label on ≥ 1 sent transaction | 161,972 |
| REQUIRES_INVESTIGATION | Unlabelled and (HIGH/CRITICAL ring member, or flagged MEDIUM-ring member, or a CRITICAL transaction) | 16,505 |
| POTENTIAL_RING_MEMBER | Unlabelled MEDIUM-ring member, no HIGH/CRITICAL transaction | 2,312 |
| SUSPICIOUS | Unlabelled, ≥ 1 HIGH/CRITICAL transaction, not in a potential ring | 24,710 |
| NOT_FLAGGED | Everything else | 694,438 |

## 6. Proposed business rule

"New device + transfer > $500 within 1 h of account creation → hold for manual review." This is **not evaluable as written** (no account-creation field). Proxy R1: 917 holds, 46 frauds caught, 5.0% precision, 0.026% recall, 19 false positives per fraud. The full analysis and the recommendation (step-up authentication instead of a hold, plus data fixes) are in [`analysis/fraud_rule_evaluation.md`](analysis/fraud_rule_evaluation.md).

## 7. Changing a rule

```sql
UPDATE fraud.engine_parameters SET param_value = 60 WHERE param_name = 'tier_high';
-- then:  python scripts/02_build_database.py --from-step E2_scores
```

Every step is idempotent (`TRUNCATE` / `DROP … IF EXISTS` first), so the engine can be resumed from any step. To make a change permanent, edit the `INSERT INTO engine_parameters` block in step `0_setup`, which a full run reloads.
