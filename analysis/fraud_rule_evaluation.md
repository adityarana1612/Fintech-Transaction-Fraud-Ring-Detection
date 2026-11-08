# Fraud Rule Evaluation

**Engine section:** `sql/fraud_network.sql` § I.
**Outputs:** `fraud.rule_evaluation`, `fraud.rule_threshold_sweep`. Excel: `Fraud_Summary` (precision, recall and FPR recomputed by formula).

## Objective

Evaluate the proposed rule:

> *"If a new device initiates a transfer greater than $500 within one hour of account creation, hold the transaction for manual review."*

Do not assume it works. Measure it, compare it with simpler alternatives and thresholds, and recommend a practical rule that balances fraud capture against customer friction.

## Step 1: can the rule be evaluated? (field check)

| Required field | In dataset? | Notes |
|---|---|---|
| Account-creation timestamp | **No** | No account or KYC table. `accounts.first_seen_ts` is the *first observed* activity, and is left-censored at 2023-01-01. |
| Device identifier | Yes | `device_id` (hash), plus the publisher's `new_device_transaction` flag |
| Transaction timestamp | Yes | `txn_ts` |
| Transaction amount | Yes, in NGN | USD needs an FX assumption: ₦750 = $1, so $500 = ₦375,000 (swept below) |
| Transfer transaction type | Yes | `TRANSFER` (25.0% of rows) |
| Fraud label | Yes | `is_fraud` |

**Conclusion: the rule cannot be evaluated as written.** No result for it is fabricated; it is recorded as rule `R0`, status `NOT_EVALUABLE`. Proxies are evaluated and labelled `PROXY`.

**What "new device" means here.** The publisher's flag is False exactly on each account's first transaction in id order, so it means "not the account's first transaction". The flag derived from real `device_id` history (first use of a device by an account with prior history) is evaluated as well (`R1D`).

## Step 2: confusion matrices (all 5,000,000 transactions)

Base rate 3.591% (179,553 labelled frauds). Period 393 days.

| Rule | Definition | Status | Held | TP | FP | FN | TN | Precision | Recall | FPR | % volume held | Lift | FP per TP | Holds / day |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R0 | New device + transfer > $500, ≤ 1 h after **account creation** | NOT_EVALUABLE | – | – | – | – | – | – | – | – | – | – | – | – |
| **R1** | Source new-device flag + TRANSFER + > $500 + ≤ 1 h after **first observed** activity | PROXY | 917 | 46 | 871 | 179,507 | 4,819,576 | **5.02%** | **0.026%** | 0.018% | 0.018% | 1.40 | 18.9 | 2.3 |
| R1D | As R1, derived first-use-of-device | PROXY | 1 | 0 | 1 | 179,553 | 4,820,446 | 0.00% | 0.000% | 0.000% | 0.000% | 0 | – | 0.0 |
| R2 | As R1, ≤ 24 h | PROXY | 957 | 47 | 910 | 179,506 | 4,819,537 | 4.91% | 0.026% | 0.019% | 0.019% | 1.37 | 19.4 | 2.4 |
| R3 | As R1, ≤ 7 days | PROXY | 1,142 | 52 | 1,090 | 179,501 | 4,819,357 | 4.55% | 0.029% | 0.023% | 0.023% | 1.27 | 21.0 | 2.9 |
| R4 | New device + TRANSFER + > $500 (no age condition) | EVALUATED | 10,511 | 464 | 10,047 | 179,089 | 4,810,400 | 4.41% | 0.258% | 0.208% | 0.210% | 1.23 | 21.7 | 26.7 |
| R5 | New device + any type > $500 | EVALUATED | 1,460,961 | 63,776 | 1,397,185 | 115,777 | 3,423,262 | 4.37% | 35.52% | 28.99% | 29.22% | 1.22 | 21.9 | 3,717.5 |
| R6 | New device only | EVALUATED | 4,103,536 | 179,553 | 3,923,983 | 0 | 896,464 | 4.38% | 100.0% | 81.40% | 82.07% | 1.22 | 21.9 | 10,441.6 |
| R7 | TRANSFER > $500 only | EVALUATED | 12,859 | 464 | 12,395 | 179,089 | 4,808,052 | 3.61% | 0.258% | 0.257% | 0.257% | 1.00 | 26.7 | 32.7 |
| R8 | New device + top-5% amount | EVALUATED | 205,030 | 9,054 | 195,976 | 170,499 | 4,624,471 | 4.42% | 5.04% | 4.07% | 4.10% | 1.23 | 21.6 | 521.7 |
| R9 | New device + network evidence | EVALUATED | 90,931 | 4,075 | 86,856 | 175,478 | 4,733,591 | 4.48% | 2.27% | 1.80% | 1.82% | 1.25 | 21.3 | 231.4 |
| R10 | Engine tier HIGH + CRITICAL | EVALUATED | 37,996 | 1,616 | 36,380 | 177,937 | 4,784,067 | 4.25% | 0.90% | 0.76% | 0.76% | 1.18 | 22.5 | 96.7 |
| R11 | Engine tier CRITICAL | EVALUATED | 2,821 | 117 | 2,704 | 179,436 | 4,817,743 | 4.15% | 0.065% | 0.056% | 0.056% | 1.16 | 23.1 | 7.2 |
| R12 | Random 1% (benchmark) | BENCHMARK | 49,750 | 1,792 | 47,958 | 177,761 | 4,772,489 | 3.60% | 1.00% | 0.99% | 0.99% | 1.00 | 26.8 | 126.6 |

Precision = TP/(TP+FP); recall (= fraud capture rate) = TP/(TP+FN); FPR = FP/(FP+TN); % volume held = (TP+FP)/N. `scripts/06_validate.py` checks that every row sums to 5,000,000 and that every metric recomputes from its matrix.

## Step 3: threshold sweeps

**A. New device + TRANSFER, varying the USD threshold (₦750 per $1)**

| Threshold | Held | TP | Precision | Recall | % volume |
|---|---|---|---|---|---|
| $0 | 1,025,969 | 45,328 | 4.42% | 25.25% | 20.52% |
| $100 | 215,052 | 9,378 | 4.36% | 5.22% | 4.30% |
| $250 | 56,298 | 2,459 | 4.37% | 1.37% | 1.13% |
| **$500** | **10,511** | **464** | **4.41%** | **0.26%** | **0.21%** |
| $1,000 | 930 | 48 | 5.16% | 0.027% | 0.019% |
| $2,500 | 9 | 0 | 0.00% | 0% | 0.0002% |

**B. Amount only, any transaction:** precision stays between 3.58% and 3.64% at every threshold from $50 to $10,000, which is the same as random review. **Amount carries no signal.**

**C. Proxy account-age window (new device + TRANSFER + > $500):** 1 h → 917 held at 5.0% precision; 7 days → 1,142 at 4.6%; 30 days → 1,783 at 5.2%; 1 year → 10,507 at 4.4%. With 46–155 true positives per window, precision values between 4.4% and 5.2% cannot be told apart (95% CI for R1 ≈ 3.7% – 6.6%).

**D. Engine score cut-offs:** precision is 4.1% – 4.4% at every cut-off from 10 to 65.

## Step 4: findings

1. **The rule as written is untestable on this data**, because account creation is not recorded.
2. **Its best proxy (R1) would barely fire**: 917 holds in 13 months (about 2 per day), catching 46 of 179,553 frauds (**0.026% recall**) with **19 legitimate customers held for every fraud caught**.
3. Its components add nothing measurable:
   * Amount alone (R7) is exactly as good as random (lift 1.00).
   * "New device" carries a 1.22 lift only because of the first-transaction artefact.
   * The "within one hour of account creation" condition cannot be measured; its first-observed proxy changes precision by an amount within noise.
4. **Every rule's precision sits between 3.6% (random) and about 4.4%** (the fraud rate among non-first transactions). Nothing in the data lets any rule exceed that ceiling.

## Step 5: false-positive and operational impact

The figures below assume **10 minutes per manual review**. This is a planning assumption; replace it with your team's measured handling time.

| Rule | Holds / day | Legitimate customers held / day | Analyst hours / day | Fraud caught / day |
|---|---|---|---|---|
| R1 (proposed, proxy) | 2.3 | 2.2 | 0.4 | 0.12 |
| R4 (new device + transfer > $500) | 26.7 | 25.6 | 4.5 | 1.2 |
| R10 (engine HIGH + CRITICAL) | 96.7 | 92.6 | 16.1 (≈ 2 FTE) | 4.1 |
| R6 (new device only) | 10,442 | 9,985 | 1,740 | 457 |

Every rule here holds **about 21–27 legitimate customers per fraud caught**. A manual hold on a payment delays it for minutes to hours and is a leading driver of complaints and churn in digital banking. At the precision this data supports, every option trades a little fraud capture for disproportionate customer friction.

## Recommendation

1. **Do not deploy the proposed rule as a hard hold** on the strength of this analysis. It cannot be validated, and its best proxy catches 0.026% of fraud.
2. **If a control is required now, use the lowest-friction version: step-up authentication instead of a manual hold.** Ask for an OTP or biometric re-authentication on *new device + TRANSFER + > $500* (R4). That is about 27 events per day (0.21% of volume) with no analyst workload, and a genuine customer completes it in seconds.
3. **Fix the data before tuning thresholds.** Capture `account_created_at`, persistent device fingerprints (install id or device binding), and label confirmation dates. Re-run `sql/fraud_network.sql` § I unchanged once those exist.
4. **Measure, don't assume.** Launch any rule as champion/challenger with a 5–10% random hold-out. Promote it only if precision clearly beats the random benchmark (R12), with a confidence interval that excludes it.

## Limitations

* The FX assumption (₦750 per $1) matters little to the conclusion, because precision is flat across all thresholds (sweep A).
* "First observed" ≠ account creation. Accounts first seen in early January 2023 may be years old.
* Labels are the publisher's and carry no confirmation date, so detection latency and loss prevented cannot be measured.
