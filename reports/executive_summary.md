# Executive Summary: Transaction Fraud Ring Detection

**Question.** The Risk Team is seeing more account takeover and believes fraudsters work through networks of accounts. Can coordinated fraud rings be found with explainable SQL, and does the proposed "new device + transfer > $500 within 1 h of account creation → hold" rule work?

**Data.** 5,000,000 transactions and 899,937 accounts (Jan 2023 – Jan 2024) from a public, synthetic Nigerian fintech fraud dataset. It contains device ids, IPs and sender → receiver links; 179,553 transactions (3.59%) are labelled fraud, 95% of them account takeover.

## What was built

* A PostgreSQL schema with separate layers: observed data and labels on one side, rule outputs on the other.
* A 37-step SQL engine covering shared-device and shared-IP linkage, window-function velocity, a recursive money-flow CTE, 12 explainable flags, ring detection and a ranked case queue.
* An Excel workbook with 10 sheets and 13,477 formulas.
* A self-contained browser console with filters, a network graph, a ring table and account drill-through.
* An automated QA suite.

## What we found

| Area | Result |
|---|---|
| Shared devices | 594 pairs within 1 h; never more than 2 accounts; consistent with random id collisions; lift 1.03 |
| Shared IPs | One carrier-NAT-like network has about 305 accounts per IP and baseline fraud. Eligible concurrent use: lift 1.09 (CI 0.96–1.23) |
| Velocity | Max 3 transactions in 10 min; the "> 5 in 10 min" rule never fires |
| Money flow | 103,639 chains within 24 h; fraud at chance level (lift 1.01); no mule concentration |
| Rings | 1,858 potential rings (multi-evidence). The most connected groups hold about 5% more labelled accounts than expected (O/E 1.05), a weak effect |
| Behavioural / segment cuts | Merchant, hour, type, channel, amount, persona: fraud rate flat at 3.5–3.7% |
| Only separator | The publisher's "new device" flag, which actually marks "not the account's first transaction" (a generator artefact) |

## Proposed rule

**It cannot be evaluated as written**, because there is no account-creation timestamp. Its proxy holds 917 payments in 13 months, catches 46 frauds (0.026% recall) at 5.0% precision, and holds **19 genuine customers per fraud caught**. Every variant tested, including the engine's own queue, achieves 3.6–5.0% precision against 3.6% for random review.

## Recommendations

1. Do not deploy the manual-hold rule. If a control is required, use **step-up authentication** for new device + transfer > $500 (about 27 events a day, no analyst load).
2. Capture **account-creation date, persistent device binding and label confirmation dates**, then re-run the engine unchanged.
3. Treat the 1,263 HIGH/CRITICAL rings as **investigation leads**: start with a 50-ring yield study.
4. Pilot every rule against a **random hold-out** before relying on it.

## Assurance

All 69 automated checks in `reports/validation_report.md` pass:

* a fixture unit test of the recursive CTE, velocity and linkage logic;
* an independent DuckDB recomputation of the headline counts;
* confusion-matrix and lift invariants;
* dashboard KPIs matching SQL under seven filter combinations in headless Microsoft Edge;
* Excel formulas recalculated in Microsoft Excel with zero errors.
