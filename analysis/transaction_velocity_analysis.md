# Transaction Velocity Analysis

**Engine section:** `sql/fraud_network.sql` § C (steps C1–C5).
**Outputs:** `fraud.txn_features`, `fraud.txn_beneficiaries_24h`, `fraud.velocity_episodes`, `fraud.velocity_distribution`, `fraud.velocity_spec_check`.

## Objective

Identify accounts with **more than five transactions within ten minutes**, as the brief specifies. For each one, report the transaction count, time window, total and average value, transaction types and unique beneficiaries, without counting any transaction more than once across overlapping windows.

## Methodology

* **Per-transaction windows (C1).** These use time-based window frames, not row-based ones:

  ```sql
  COUNT(*) OVER (PARTITION BY sender_account ORDER BY txn_ts
                 RANGE BETWEEN INTERVAL '10 minutes' PRECEDING AND CURRENT ROW)   -- trailing (real-time view)
  COUNT(*) OVER (... RANGE BETWEEN CURRENT ROW AND INTERVAL '10 minutes' FOLLOWING) -- densest burst
  ```

  The same pattern gives trailing 1 h and 24 h counts. `LAG`/`LEAD` give the previous and next transaction, and `AVG(...) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)` gives the trailing average amount.
* **Unique beneficiaries (C2).** `COUNT(DISTINCT)` is not allowed as a window function, so a bounded self-join counts distinct receivers in the trailing 24 h. It runs only for transactions with at least two in the window.
* **No double counting (C3, gaps-and-islands).** A new *episode* starts whenever the gap to the sender's previous transaction exceeds 10 minutes:

  ```sql
  SUM(is_new_episode) OVER (PARTITION BY sender_account ORDER BY txn_ts ROWS UNBOUNDED PRECEDING)
  ```

  Each transaction belongs to exactly one episode, so a burst is reported once with its densest 10-minute count, however many sliding windows overlap it. The fixture test proves this: a 7-transaction burst in 8 minutes is reported as one episode with `txn_count = 7`.

## Findings

### The brief's rule never fires

| Check (`fraud.velocity_spec_check`) | Value |
|---|---|
| Max transactions by one account in **any 10-minute window** | **3** |
| Max in any 1 h / any 24 h window | 3 / 4 |
| Accounts with **> 5 transactions in 10 minutes** | **0** |
| Accounts with ≥ 2 in a trailing 10 min | 1,071 |
| Accounts with ≥ 3 in a trailing 1 h | 7 |
| Accounts with ≥ 3 in a trailing 24 h | 1,367 |
| Median time between an account's consecutive transactions | **878.6 hours (≈ 37 days)** |

Accounts in this dataset average 5.6 sent transactions across 13 months. High-frequency behaviour essentially does not exist, so a "> 5 in 10 minutes" velocity rule would generate **zero alerts and catch zero fraud**.

### Velocity distribution (all 5,000,000 transactions)

| Sender's transactions in window, including this one | trailing 10 min | fraud rate | trailing 1 h | fraud rate | trailing 24 h | fraud rate |
|---|---|---|---|---|---|---|
| 1 | 4,998,926 | 3.591% | 4,993,505 | 3.590% | 4,897,068 | 3.589% |
| 2 | 1,073 | 4.007% | 6,488 | 4.038% | 101,537 | 3.694% |
| 3 | 1 | 0.000% | 7 | 0.000% | 1,380 | 3.261% |
| 4 | – | – | – | – | 15 | 6.667% (1 of 15) |

### Bursts (non-overlapping episodes)

* **1,072 episodes** have two or more transactions less than 10 minutes apart; they contain 2,146 transactions in total.
* Only 2 episodes reach 3 transactions. Their median duration is 302 s.
* The labelled-fraud rate inside episodes is 4.61% (99 of 2,146). The relevant comparison is the 4.38% rate among *non-first* transactions, not the 3.59% population rate, because labelled fraud never falls on an account's first generated transaction, and transactions in a burst are almost always later in an account's history. Against 4.38%, the difference (99 observed vs about 94 expected) is within noise.

Densest episodes:

| Account | Txns | Start | Duration | Max in any 10 min | Value (NGN) | Types | Beneficiaries | Labelled |
|---|---|---|---|---|---|---|---|---|
| 2642119964 | 3 | 2023-12-25 09:46 | 567 s | 3 | 473,475 | DEPOSIT, PAYMENT, WITHDRAWAL | 3 | 0 |
| 3653994287 | 3 | 2023-12-26 17:31 | 979 s | 2 | 18,837,950 | DEPOSIT | 3 | 0 |
| 1009360957 | 2 | 2023-12-08 10:20 | 445 s | 2 | 2,910,545 | DEPOSIT, PAYMENT | 2 | 1 |

### Calibrated velocity flags

| Flag | Definition | Flagged txns | Fraud rate | Lift | RR 95% CI |
|---|---|---|---|---|---|
| `HIGH_VELOCITY` | ≥ 2 in trailing 10 min **or** ≥ 3 in trailing 1 h | 1,078 | 3.99% | 1.11 | 0.83 – 1.49 |
| `MULTI_BENEFICIARY_24H` | ≥ 3 distinct receivers in trailing 24 h | 1,395 | 3.30% | 0.92 | 0.69 – 1.22 |

## Interpretation

* Velocity carries **no measurable signal** about the label in this data. Both calibrated flags have confidence intervals spanning 1.
* The absence of bursts is itself informative. Account-takeover fraud in production typically shows rapid drain sequences (several transfers in minutes after a credential change). The generator behind this dataset did not model that, so the dataset cannot test velocity rules.

## Business implications

* Do not deploy the "> 5 in 10 minutes" rule on the strength of this data. It would be untested rather than validated.
* On real data, velocity should be measured relative to the account's own baseline (for example, transactions per hour above the account's 95th percentile) rather than a fixed count. This engine's window functions already provide that building block.
* The gaps-and-islands episode table is the correct way to present velocity alerts to investigators: one alert per burst, not one per sliding window.

## Limitations

* The source `velocity_score` (1–20) was not used: it shows no association with the label and its definition is unknown.
* Trailing windows use only past data and are real-time safe. The densest-burst column uses a forward window and is an investigation-time metric.
* The calibrated thresholds were chosen from the observed distribution because the brief's threshold is never reached. They are substitutes, not the rule requested.
