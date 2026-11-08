# Fraud Ring Analysis

**Engine section:** `sql/fraud_network.sql` § G (steps G1–G8) and § H.
**Outputs:** `fraud.account_links`, `fraud.account_components`, `fraud.fraud_rings`, `fraud.fraud_ring_membership`, `fraud.account_risk_profile`, `fraud.ring_validation`, `fraud.ring_size_distribution`, `fraud.investigation_cases`, and views `vw_ring_leaderboard`, `vw_ring_key_accounts`, `vw_flagged_account_timeline`.

## Objective

Combine shared-device, shared-IP and money-flow relationships into explainable account groups, score them, and prioritise them for investigation. The four populations must stay separate: **confirmed labels**, **suspicious accounts**, **potential ring members** and **accounts requiring investigation**.

## Methodology

1. **Evidence edges (G1)**, undirected, `account_a < account_b`:

   | Link type | Rule | Edges |
   |---|---|---|
   | SHARED_DEVICE | same `device_id`, different accounts, ≤ 1 h | 594 |
   | SHARED_IP | same low/moderate-density IP, ≤ 10 min | 3,018 |
   | MONEY_FLOW | transfer that belongs to a maximal ≥ 2-transfer chain within 24 h | 204,559 |

2. **Connected components (G2).** A recursive CTE computes reachability, `reach(root, node)`, using `UNION`, which drops pairs already reached so the recursion terminates. A component's id is its smallest account id.
3. **Scoring (G3).** Each group of ≥ 3 accounts gets two scores:
   * a **label-free network score** (0–75): size, link-type diversity, any device link, any IP link, any hop forwarded within 1 h, and suspicious member transactions;
   * a separate **label score** (0–25): share of members with a source fraud label.

   Tiers: CRITICAL ≥ 60, HIGH 40–59, MEDIUM 25–39, LOW < 25.
4. **Corroboration rule.** A group linked by only **one** evidence type is never above LOW. Without this rule, 13,590 single-type money-flow groups reached MEDIUM+ on size alone, which is not a usable investigation queue.
5. **Potential fraud ring** = tier MEDIUM or above. Account statuses (G5), in priority order:

   | Status | Definition |
   |---|---|
   | CONFIRMED_FRAUD_LABEL | ≥ 1 sent transaction labelled by the source |
   | REQUIRES_INVESTIGATION | unlabelled, and in a HIGH/CRITICAL ring, *or* flagged and in a MEDIUM ring, *or* has a CRITICAL transaction |
   | POTENTIAL_RING_MEMBER | unlabelled member of a MEDIUM ring with no HIGH/CRITICAL transaction |
   | SUSPICIOUS | unlabelled, ≥ 1 HIGH/CRITICAL transaction, not in a potential ring |
   | NOT_FLAGGED | everything else |

6. **Validation without circularity (G6).** Because ring tiers use labels, they cannot be used to prove that rings contain fraud. Groups are instead banded by the **label-free** network score. Observed labelled members are then compared with the number expected for accounts of the same activity level, Σ P(label | sent_txn_count), which removes the confounding effect of transaction volume.

## Findings

### Groups and rings

| Measure | Value |
|---|---|
| Accounts with at least one evidence edge | 263,379, in 55,211 connected groups (1,620 are pairs) |
| Linked groups of ≥ 3 accounts | **53,591** (260,139 accounts) |
| Of which multi-type (corroborated) | 1,858 |
| **Potential fraud rings (MEDIUM+)** | **1,858**: 287 CRITICAL, 976 HIGH, 595 MEDIUM |
| Accounts in potential rings | 21,306 |
| Largest ring | 139 accounts |

| Group size | Groups | Accounts | CRITICAL | HIGH | MEDIUM | LOW | Multi-type |
|---|---|---|---|---|---|---|---|
| 3–4 | 35,649 | 108,721 | 25 | 128 | 425 | 35,071 | 578 |
| 5–9 | 14,079 | 85,053 | 53 | 362 | 166 | 13,498 | 581 |
| 10–19 | 2,960 | 38,670 | 80 | 357 | 4 | 2,519 | 441 |
| 20–49 | 832 | 22,971 | 92 | 124 | 0 | 616 | 216 |
| 50+ | 71 | 4,724 | 37 | 5 | 0 | 29 | 42 |

### Top rings (`vw_ring_leaderboard`)

| Ring | Tier | Score (network + label) | Accounts | Links: device / IP / money flow | Fast hops (≤1 h) | Labelled members | Suspicious txns | Member value (NGN) |
|---|---|---|---|---|---|---|---|---|
| R-1 | CRITICAL | 80 (75 + 5) | 79 | 1 / 1 / 76 | 2 | 18 | 8 | 370.2 M |
| R-2 | CRITICAL | 80 (75 + 5) | 77 | 1 / 1 / 74 | 3 | 22 | 6 | 336.9 M |
| R-3 | CRITICAL | 80 (75 + 5) | 63 | 1 / 1 / 60 | 3 | 11 | 7 | 291.5 M |
| R-4 | CRITICAL | 80 (75 + 5) | 59 | 1 / 1 / 56 | 3 | 18 | 8 | 236.8 M |
| R-5 | CRITICAL | 80 (75 + 5) | 57 | 1 / 1 / 54 | 2 | 11 | 3 | 246.4 M |

The top rings share one structure: a **large money-flow component** joined to a single shared-device pair and a single shared-IP pair. The analysis in [`device_linkage_analysis.md`](device_linkage_analysis.md) shows that device pairs occur at chance rates, so the "corroboration" in these rings is plausibly coincidental. They are the right *first* cases for an analyst to open, and the right cases to show that a scoring rule can over-rank coincidence.

### Size-adjusted validation (label-free network score)

| Band | Groups | Sender accounts | Labelled: observed | Labelled: expected | O/E (95% CI) |
|---|---|---|---|---|---|
| Not in a linked group of 3+ | – | 636,675 | 109,966 | 109,978.9 | 1.000 (0.994 – 1.006) |
| Network score 0–14 | 39,978 | 140,595 | 26,610 | 27,613.3 | 0.964 (0.952 – 0.975) |
| Network score 15–24 | 9,433 | 63,795 | 13,568 | 12,959.5 | 1.047 (1.029 – 1.065) |
| Network score 25–34 | 2,663 | 29,091 | 6,120 | 5,977.2 | 1.024 (0.998 – 1.050) |
| Network score 35+ | 1,517 | 26,307 | 5,708 | 5,442.5 | **1.049 (1.022 – 1.076)** |

The most connected groups hold about **5% more** fraud-labelled accounts than accounts of the same activity level elsewhere: about 266 extra accounts among 26,307. The effect is statistically detectable but practically small, and it does not rise steadily with the network score. Organised rings in real data typically show labelled shares several times the baseline.

### Account classification (`fraud.account_status_summary`)

| Status | Accounts | % | In potential ring | Flagged |
|---|---|---|---|---|
| NOT_FLAGGED | 694,438 | 77.17% | 0 | 0 |
| CONFIRMED_FRAUD_LABEL | 161,972 | 18.00% | 4,456 | 8,095 |
| SUSPICIOUS | 24,710 | 2.75% | 0 | 24,710 |
| REQUIRES_INVESTIGATION | 16,505 | 1.83% | 14,538 | 3,863 |
| POTENTIAL_RING_MEMBER | 2,312 | 0.26% | 2,312 | 0 |

Related KPIs: 36,668 flagged accounts (≥ 1 HIGH/CRITICAL transaction), of which 2,472 belong to potential rings; 20,992 high-risk sending accounts (a CRITICAL transaction or a HIGH/CRITICAL ring).

### Investigation queue (`fraud.investigation_cases`)

**3,244 open cases**: 1,263 RING cases (HIGH/CRITICAL rings) and 1,981 ACCOUNT cases (unlabelled accounts with a CRITICAL transaction outside those rings). Priority is `DENSE_RANK()` by risk score. The top account cases (score 85) combine `PRIOR_FRAUD_LINK`, `RAPID_PASS_THROUGH`, `HIGH_VALUE`, `NEW_DEVICE_SRC` and `AMOUNT_SPIKE`.

## Interpretation

* The engine meets the brief: explainable rules, a connection-reason string for every ring, separate source-label and network scores, four distinct populations, and a ranked case queue.
* On this dataset the rings are **weak**: they are dominated by money-flow chains, which carry chance-level fraud (see `money_flow_analysis.md`), and their corroboration comes mostly from coincidental device and IP pairs. The 5% excess of labelled members in the most connected groups is the only network signal found, and it is too small to justify network-based blocking.
* **None of the 1,858 potential rings should be described as a confirmed fraud ring.** They are leads ranked by explainable evidence.

## Business implications

* Investigator effort: 1,263 ring cases at about 30 minutes each is about 630 analyst-hours. Given the validation result, a sample of 50 top-ranked rings should be reviewed first (champion/challenger) to measure true yield before the queue is worked in full.
* For real deployment, strengthen corroboration: require at least one *direct* device or IP link between two **money-flow-connected** accounts, not merely within the same component. Also cap component growth through hub accounts.
* Keep the label score separate from the network score, as here. That makes the network score testable and prevents ring tiers from simply echoing existing labels.

## Limitations

* Connected components are transitive: two accounts can share a ring without any direct relationship.
* Ring scoring weights are a priori. The corroboration rule and tier cut-offs were chosen for queue size, not tuned to labels.
* Labels sit on sending (often victim) accounts. A ring with labelled members may be a group of victims rather than a group of fraudsters.
