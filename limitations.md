# Limitations

This file lists what the project **cannot** show and why, so the findings are not read as more than they are.

## 1. Data provenance

| # | Limitation | Consequence | How it is handled |
|---|---|---|---|
| 1 | The dataset is **synthetic** (stated on the dataset card), not bank-observed. | Patterns reflect the publisher's generator, not real fraudster behaviour. In particular, the absence of real fraud rings is a property of this data, not evidence that rings are rare in practice. | Stated in every report, the dashboard header and the memo. The engine is presented as a method whose results on real data would differ. |
| 2 | `is_fraud` is the **publisher's label**, with no confirmation date, chargeback or investigation outcome. | Precision and recall measure agreement with the generator's label, not with confirmed losses. | Labels are kept in `fraud_labels` with `label_source = 'SOURCE_DATASET'`. |
| 3 | Labels are **transaction-level** and attach to the sending account. | Under account takeover the sending account is usually the *victim*. A "fraud-labelled account" is evidence of compromise, not of the holder's guilt. | Wording throughout: "fraud-labelled account", never "fraudster". |
| 4 | The licence field on the card says GPL / "other"; original rights remain with the publisher. | Redistribution of the full raw file is not attempted. | The raw file is git-ignored and downloaded with a checksum by `scripts/00_download_data.py`. |

## 2. Missing fields

| Missing field | Impact | Treatment |
|---|---|---|
| **Account-creation timestamp** | The proposed "$500 within one hour of account creation" rule **cannot be evaluated as written**. | Reported as `NOT_EVALUABLE` (rule R0). Proxies use *first observed activity* and are labelled `PROXY`. Accounts first seen near 2023-01-01 are left-censored, because they may be years old. |
| Customer / KYC entity | Accounts belonging to the same person cannot be distinguished from collusion. | No `customers` table is modelled. |
| Device-to-customer registry, device type per hash | A shared `device_hash` cannot be confirmed as one physical device. | Linkage is described as "same device id", and is tested against a random-collision baseline. |
| IP geolocation, ASN, VPN/proxy flags | Carrier NAT cannot be confirmed directly. | IPs are classified by observed account density. The 102.89.0.0/16 block (about 305 accounts per IP) is *treated as* a legitimate shared network, which is an inference. |
| Currency conversion | The $500 threshold needs an assumption. | ₦750 per $1 (`engine_parameters.usd_ngn_rate`), plus a sweep from $0 to $10,000. The NGN rate moved sharply during 2023. |
| Investigation outcomes / feedback loop | Rule tiers cannot be calibrated against analyst decisions. | Weights are a priori, and their measured lift is reported next to them. |

## 3. Source-field quality issues

* `time_since_last_transaction` is negative in 2,051,331 rows and null in 896,513, so it contradicts the timestamps. It is retained but not used; gaps are recomputed with `LAG()`.
* `new_device_transaction` is False on exactly each sender's first transaction (by id) and True on every other row. **Every** labelled fraud has it set to True, so it encodes "not the account's first generated transaction". This is a generator artefact, and a rule built on it would not transfer to real data.
* `device_hash` values are consistent with random draws from a ~9 M id space (observed vs Poisson-expected counts agree to within 0.5% for one to five transactions per device). Most device "sharing" is therefore indistinguishable from chance.
* `transaction_type` and `merchant_category` are sometimes contradictory; for example, 104,493 deposits are at "ATM/POS Withdrawal". Rows are kept unchanged.
* Every row has a `receiver_account`, including deposits and withdrawals. The money-flow graph follows the dataset's own sender→receiver semantics for all types.
* The publisher's `velocity_score`, `geo_anomaly_score` and `spending_deviation_score` show no association with the label and are not used.

## 4. Method limitations

* **Thresholds are judgement calls.** They are 1 h for devices, 10 min for IPs, more than 50 accounts for high-density IPs, 24 h and a depth of 6 for chains, and the weights and tiers. All of them sit in one table and can be re-run, but only some were swept.
* **The brief's velocity rule (more than 5 transactions in 10 minutes) never fires.** The densest burst in the data is 3 transactions. The calibrated flag (at least 2 in 10 min, or at least 3 in 1 h) is a substitute, not the rule requested.
* **Connected components chain weak evidence.** A large money-flow group can join accounts with no direct relationship. Ring scores reward diversity and fast hops to offset this, but a "ring" is still a lead for investigation.
* **Association is not causation.** Lift and relative risk describe this dataset. With 12 flags and several thresholds tested, some apparent effects will be chance; confidence intervals are reported for that reason.
* **Point-in-time correctness.** Velocity and prior-fraud flags use only past information (trailing windows, labels strictly earlier). Device/IP pairing and money-flow chains use a symmetric or forward window, so they describe *investigation-time* evidence that is not all available when the first transaction occurs.
* **Recursive-chain anchoring.** Chains are seeded at every hop and reduced to maximal paths. Two overlapping chains with different 24-hour windows are both kept, which is intended.

## 5. Delivery limitations

* The full raw (969 MB) and cleaned (1.06 GB) CSVs exceed GitHub's file limit. They are reproducible from scripts but not committed. A 10 k-row cleaned sample and all aggregated outputs are committed.
* The Excel workbook holds a 10,000-row random sample for its formulas. Population figures are written in as values from SQL and are labelled as such.
* The dashboard embeds a population cube for all 5 M transactions plus a transaction-level *investigation set*. Account drill-through shows the full history only for flagged accounts and potential-ring members; other counterparties show only their shared transactions.
* The PostgreSQL build was executed on an embedded PostgreSQL 16.2 on Windows. Other versions of 14+ should work but were not tested.
