# MEMORANDUM

| | |
|---|---|
| **To** | Chief Risk Officer |
| **From** | Fraud Risk Analytics |
| **Date** | November 2026 |
| **Subject** | Fraud-ring detection capability and the proposed new-device transfer-hold rule |
| **Classification** | Internal |

---

## 1. Executive summary

* We built an explainable, SQL-only fraud-ring detection engine and ran it on **5,000,000 transactions across 899,937 accounts** (Jan 2023 – Jan 2024). The source is a public, synthetic Nigerian fintech dataset with publisher-assigned fraud labels: **179,553 labelled frauds, 3.59%; 95% account takeover**.
* The engine links accounts through shared devices, shared IPs and 24-hour money-flow chains. It scores behaviour with 12 explainable flags, groups linked accounts into rings and produces a ranked case queue. Labels and rule outputs are kept strictly separate.
* **Headline finding: in this dataset, no behavioural or network signal predicts fraud.** Shared devices, shared IPs, velocity, money-flow chains, amount, merchant and time of day all show lift between 0.92 and 1.11, with confidence intervals around 1. The only separator, the publisher's "new device" flag, turns out to mean "not the account's first transaction". That is a data-generation artefact, not customer behaviour.
* **The proposed rule** ("new device + transfer > $500 within 1 h of account creation → manual hold") **cannot be evaluated**, because account-creation dates are not recorded. Its closest proxy would hold about 2 payments a day, catch **0.026%** of fraud, and hold **19 genuine customers for every fraud caught**.
* **Recommendation:** do not deploy the hold as written. Use step-up authentication instead of a manual hold, fix three data gaps, and pilot every rule against a random control before relying on it.

## 2. Fraud risk context

Account takeover dominates the label set (170,635 of 179,553 frauds). In ATO the *sending* account belongs to the victim, so "fraud-labelled accounts" (161,972, 18% of senders) are mostly compromised customers, not criminals. The Risk Team's concern is that fraudsters operate as coordinated networks that are invisible one transaction at a time. That was the hypothesis tested.

## 3. Dataset and methodology

* **Dataset choice.** BankShield-2M, the preferred source, was rejected after its schema was inspected. Only 5,000 rows are free, its IP addresses are unique per transaction, and it has no receiver account. The selected dataset is the only candidate with device ids, shared IPs and sender→receiver links. It is synthetic, as its publisher states, and every figure here describes it. No relationships or labels were fabricated.
* **Data quality.** 25 automated checks found zero duplicate ids, malformed accounts, invalid IPs or devices, or bad amounts or timestamps. Three source fields were found unreliable and excluded from scoring.
* **Engine.** PostgreSQL 16, in 37 steps:
  * window functions for velocity;
  * a recursive CTE for money chains (24 h window, depth 6, cycle and duplicate-path prevention);
  * recursive connected components for rings;
  * rule scoring and confusion-matrix rule evaluation.
* **Assurance.** Results were checked by a unit-test fixture with known answers, an independent recomputation in a second engine (DuckDB), invariant checks, and reconciliation of the Excel workbook and dashboard against SQL.

## 4. Fraud-ring identification approach

Accounts are linked when they share a device within 1 hour, share a low-density IP within 10 minutes, or sit on the same 24-hour money chain. Linked groups of ≥ 3 accounts are scored on two separate axes:

* **network evidence**: size, number of independent link types, fast pass-through and suspicious transactions;
* **existing fraud labels.**

A group becomes a **potential fraud ring** only if it has at least two independent types of evidence. Network evidence is validated *without* using labels.

## 5. Shared-device findings

* Only **594** device pairs occur within an hour (0.02% of transactions), and no device links more than 2 accounts.
* Device ids follow the pattern of a random draw: observed and expected counts agree to within 0.5% for one to five transactions per device. Gaps between the two accounts are spread evenly across the hour.
* Linked accounts carry fraud labels at the expected rate (observed/expected **1.01**); flag lift 1.03.
* *Implication:* device sharing here is coincidence. On real data, start with a device-fingerprint quality check.

## 6. Shared-IP findings

* One mobile network block (102.89.0.0/16) serves **about 305 accounts per IP** and a quarter of all volume, which is classic carrier NAT. It produces 83% of concurrent-IP events, at exactly the baseline fraud rate.
* After excluding such networks, 3,018 concurrent-use pairs remain. They show lift **1.09 (CI 0.96–1.23)** and label O/E 1.04.
* *Implication:* IP-count rules alone would put about 26% of volume in front of investigators for no gain. IP evidence should only corroborate other evidence.

## 7. Transaction-velocity findings

* The densest burst in the data is **3 transactions in 10 minutes**. The specified "> 5 in 10 minutes" rule would fire **zero times**.
* The calibrated velocity flag (1,078 transactions) shows lift 1.11 (CI 0.83–1.49). The median gap between an account's transactions is 37 days.
* *Implication:* this dataset cannot validate velocity controls. Real ATO "drain" patterns are not represented.

## 8. Money-flow analysis

* There are **105,058** 24-hour hops and **103,639** maximal chains (up to 4 transfers).
* Chains contain labelled fraud at exactly the rate chance predicts (lift **1.01**). After a labelled incoming payment, the onward payment is labelled *less* often (3.2% vs 3.7%).
* No receiver collects more labelled payments than random assignment would produce. There are no mule accounts in this data.

## 9. Behavioural fraud indicators

Twelve explainable flags were tested on all 5 M transactions, with lift ranging from 0.92 to 1.11 apart from the artefact flag. Merchant group, hour, transaction type, channel, city, persona and amount band each move the fraud rate by at most about 0.2 percentage points. The label behaves as if it were assigned at random among non-first transactions (fraud rate **4.38%**).

## 10. Fraud-rule evaluation

| Rule | Held (13 months) | Frauds caught | Precision | Recall | Genuine customers held per fraud |
|---|---|---|---|---|---|
| Proposed rule as written | **not measurable** (no account-creation date) | | | | |
| Proposed, proxy (first-observed activity) | 917 | 46 | 5.0% | 0.026% | 19 |
| New device + transfer > $500 | 10,511 | 464 | 4.4% | 0.26% | 22 |
| Transfer > $500 alone | 12,859 | 464 | 3.6% | 0.26% | 27 |
| Engine HIGH + CRITICAL queue | 37,996 | 1,616 | 4.3% | 0.90% | 23 |
| Random 1% review (benchmark) | 49,750 | 1,792 | 3.6% | 1.00% | 27 |

Precision is flat across every amount threshold from $50 to $10,000 and every account-age window.

## 11. False-positive assessment

Every evaluated rule holds about **21–27 genuine customers per fraud caught**. At an assumed 10 minutes per review, the engine's HIGH+CRITICAL queue would need about 16 analyst-hours a day (about 2 FTE) to catch 4 frauds a day. In digital channels, manual holds on legitimate transfers drive complaints and attrition. A control whose precision is barely above random review destroys more value than it protects.

## 12. Operational recommendations

1. **Do not implement the manual-hold rule as proposed.** Its effect cannot be measured, and the evidence available shows negligible capture.
2. **If a control is needed immediately,** apply **step-up authentication (OTP or biometric) rather than a hold** for new device + transfer > $500. That is about 27 events a day, with no analyst workload and seconds of friction for genuine customers.
3. **Close three data gaps:** account-creation timestamp, persistent device binding (install id), and label confirmation date and source. Without them, no ATO rule can be validated.
4. **Adopt the engine as the investigation workbench, not as an automatic decision system.** Ring cases (1,263 HIGH/CRITICAL rings) should be worked as leads, starting with a 50-ring yield sample.
5. **Run every rule as champion/challenger** with a 5–10% random hold-out, and promote only on a precision gain whose confidence interval excludes the random benchmark.

## 13. Implementation priorities

| Priority | Action | Owner | Horizon |
|---|---|---|---|
| 1 | Stop / redesign the hold rule; switch to step-up authentication | Fraud Strategy | 2 weeks |
| 2 | Add `account_created_at`, device binding, label confirmation dates to the data platform | Data Engineering | 1 quarter |
| 3 | Re-run this engine on production data (no code change needed) | Fraud Analytics | After 2 |
| 4 | 50-ring investigator yield study; calibrate weights from measured relative risk | Fraud Ops + Analytics | After 3 |
| 5 | Champion/challenger framework with random hold-out | Fraud Strategy | 1 quarter |

## 14. Risks and limitations

* **Synthetic data.** The absence of rings here says nothing about real fraud. The methods transfer to real data; these numbers do not.
* **Labels** are the publisher's, sit on the (often victim) sending account, and have no confirmation date.
* **The proposed rule could not be tested.** Proxies use first observed activity, and the FX rate of ₦750 per $1 is an assumption (the results are insensitive to it).
* The ring scoring weights are a priori. Connected groups can join accounts that have no direct relationship.

## 15. Conclusion

We now have a tested, explainable, PostgreSQL-native capability to link accounts, trace money, score behaviour and queue ring investigations. Applied honestly to the available data, it shows that **none of the behaviours we hypothesised predicts fraud in this dataset**, and that the proposed hold rule would mostly inconvenience genuine customers. The right next move is to fix the data gaps and re-run the same engine on production data. Customer-facing holds should wait for measured evidence.

*Supporting material: `analysis/*.md`, `excel/fraud_analysis.xlsx`, `dashboard/fraud_ops_console.html`, `reports/validation_report.md`.*
