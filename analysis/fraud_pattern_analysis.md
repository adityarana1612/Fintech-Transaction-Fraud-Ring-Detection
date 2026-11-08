# Fraud Pattern Analysis

**Scope:** all 5,000,000 transactions (2023-01-01 → 2024-01-28), 899,937 accounts.
**Sources:** PostgreSQL (`fraud.transactions`, `fraud.fraud_labels`, `fraud.flag_performance`, `fraud.tier_performance`) and `excel/fraud_analysis.xlsx`.

## Objective

Profile where labelled fraud occurs (by merchant, hour, type, channel, amount, customer attributes and behavioural flags) and identify which attributes actually separate fraud from legitimate activity in this dataset.

## Methodology

For every categorical attribute, the fraud rate (labelled fraud ÷ transactions) in each category is compared with the overall rate of 3.591%. For behavioural flags, the engine computes:

* lift = (fraud rate when flagged) ÷ (overall rate);
* relative risk = (fraud rate when flagged) ÷ (fraud rate when not flagged), with a log-method 95% confidence interval;
* recall, the share of all labelled fraud that the flag covers.

All figures are population values from SQL. The Excel workbook repeats the key cuts on a 10,000-row sample with live formulas.

## Findings

### 1. Label base

| Measure | Value |
|---|---|
| Labelled fraud transactions | **179,553 (3.591%)** |
| – Account Takeover | 170,635 (95.0%) |
| – Identity Fraud | 8,918 (5.0%) |
| Accounts with ≥ 1 labelled sent transaction | 161,972 (18.07% of 896,463 senders) |
| Average amount, fraud vs not | ₦750,124 vs ₦749,597 |
| Median amount, fraud vs not | ₦163,448 vs ₦164,317 |

Fraud is spread thinly across many accounts: the typical labelled account has one labelled transaction among about seven.

### 2. Attributes that do *not* separate fraud

| Attribute | Range of fraud rate across categories |
|---|---|
| Merchant group (11) | 3.552% (Bills & Utilities) – 3.651% (Donations) |
| Merchant category (21) | 3.498% (DSTV) – 3.665% (SPAR) |
| Transaction type (4) | 3.564% (PAYMENT) – 3.625% (TRANSFER) |
| Hour of day (24) | 3.505% (06:00) – 3.715% (00:00) |
| Device channel × payment channel (16) | 3.541% – 3.640% |
| City (10) | 3.552% – 3.639% |
| Persona (3) | 3.587% – 3.598% |
| BVN linked | 3.562% (no) vs 3.593% (yes) |
| Amount band (9 bands, ₦ < 5K → > 5M) | 3.567% – 3.618% |
| Month (Jan–Dec 2023) | 3.53% – 3.63% (partial Jan-2024: 3.29% on 6,417 txns) |
| Publisher scores (velocity, geo-anomaly, spending deviation) | flat across all values |

Transaction volume is about four times higher between 09:00 and 17:59, but the fraud **rate** is flat around the clock. Every spread above is within what sampling noise produces at these volumes. **No merchant, hour, channel, amount or customer segment is riskier than another in this data.**

### 3. Behavioural flags (population)

| Flag | Flagged txns | % volume | Fraud rate flagged | Lift | Relative risk (95% CI) | Recall |
|---|---|---|---|---|---|---|
| `NEW_DEVICE_SRC` (source field) | 4,103,536 | 82.07% | 4.376% | **1.22** | ∞ (no fraud when absent) | **100%** |
| `HIGH_VELOCITY` | 1,078 | 0.02% | 3.989% | 1.11 | 1.11 (0.83 – 1.49) | 0.02% |
| `SHARED_IP_10M` | 6,025 | 0.12% | 3.900% | 1.09 | 1.09 (0.96 – 1.23) | 0.13% |
| `SHARED_DEVICE_1H` | 1,188 | 0.02% | 3.704% | 1.03 | 1.03 (0.77 – 1.38) | 0.02% |
| `AMOUNT_SPIKE` | 595,997 | 11.92% | 3.678% | 1.02 | 1.03 (1.01 – 1.04) | 12.2% |
| `RAPID_PASS_THROUGH` | 103,593 | 2.07% | 3.670% | 1.02 | 1.02 (0.99 – 1.06) | 2.1% |
| `FIRST_DEVICE_USE` (derived, weight 0) | 4,103,536 | 82.07% | 3.644% | 1.01 | 1.09 (1.08 – 1.10) | 83.3% |
| `UNUSUAL_HOUR` | 499,722 | 9.99% | 3.646% | 1.02 | 1.02 (1.00 – 1.03) | 10.1% |
| `HIGH_VALUE` | 250,000 | 5.00% | 3.622% | 1.01 | 1.01 (0.99 – 1.03) | 5.0% |
| `SHARED_NETWORK_IP` (context, weight 0) | 1,296,500 | 25.93% | 3.605% | 1.00 | 1.00 (1.00 – 1.02) | 26.0% |
| `PRIOR_FRAUD_LINK` | 465,725 | 9.32% | 3.600% | 1.00 | 1.00 (0.99 – 1.02) | 9.3% |
| `MULTI_BENEFICIARY_24H` | 1,395 | 0.03% | 3.297% | 0.92 | 0.92 (0.69 – 1.22) | 0.03% |

### 4. The one real separator is a data-generation artefact

`new_device_transaction` is False on **exactly one transaction per sender**: the account's first transaction in transaction-id order (896,463 rows, plus one more). It is True on every other row. Every one of the 179,553 labelled frauds has it set to True, so:

* labelled fraud **never** occurs on an account's first generated transaction;
* the fraud rate among all other transactions is **4.376%**;
* the "new device" flag therefore means "not the account's first transaction", not device behaviour. The derived first-use-of-device flag, computed from actual `device_id` history, has a lift of only 1.01.

This also explains the small lifts of 1.02–1.11 on flags that need prior history (amount spike, velocity, shared IP and device within a window). Such flags never fire on a first transaction, so they inherit part of the 4.38% vs 3.59% gap.

### 5. Rule-score tiers

| Tier | Score | Txns | % volume | Fraud rate | Lift | Cumulative recall |
|---|---|---|---|---|---|---|
| CRITICAL | ≥ 65 | 2,821 | 0.06% | 4.15% | 1.15 | 0.07% |
| HIGH | 50–64 | 35,175 | 0.70% | 4.26% | 1.19 | 0.90% |
| MEDIUM | 25–49 | 1,000,609 | 20.01% | 4.23% | 1.18 | 24.46% |
| LOW | < 25 | 3,961,395 | 79.23% | 3.42% | 0.95 | 100% |

HIGH+CRITICAL volume rises from 597 transactions in Jan-2023 to 5,627 in Dec-2023. The labelled fraud rate stays flat over the same months, so this is **mechanical**, not a fraud trend: `PRIOR_FRAUD_LINK` can only fire once a counterparty has an earlier label, and `AMOUNT_SPIKE` needs prior history. Both accumulate through the year.

Tiers above LOW perform alike, at about 4.2% precision. That is no better than "any transaction that is not the account's first", which confirms that the a-priori weights add no information beyond the artefact.

## Interpretation

* In this dataset, fraud labels behave as if they were assigned at random among non-first transactions, with no dependence on merchant, time, amount, channel, customer, network or velocity.
* That is a property of the synthetic generator, and it is the central finding of this project: **an explainable engine applied honestly to this data shows that its behavioural and network signals do not predict the label**, and it says so rather than presenting rule hits as discoveries.
* Taken alone, the statistically significant RRs (amount spike 1.03, first-device use 1.09) are artefacts of very large samples combined with the first-transaction effect. They are not operationally meaningful.

## Business implications

1. **No rule from this data should go to production on the strength of its measured performance.** The best achievable precision without the artefact is the base rate.
2. The engine, flags and evaluation framework are the reusable asset. Running the same SQL on real labelled data (with confirmed fraud dates) is the next step.
3. Data requests to make before real deployment:
   * account-creation date;
   * device fingerprint persistence;
   * IP ASN and geolocation;
   * label confirmation date and source (chargeback, customer claim, analyst).

## Limitations

* All findings describe this synthetic dataset; real fraud patterns differ.
* Many comparisons were made, so some nominally significant CIs are expected by chance.
* Labels are transaction-level and attach to the sending (often victim) account.
