# Money-Flow Analysis (Recursive CTE)

**Engine section:** `sql/fraud_network.sql` § D (steps D1–D5).
**Outputs:** `fraud.flow_hops`, `fraud.money_flow_chains_all`, `fraud.money_flow_paths`, `fraud.money_flow_summary`, `fraud.money_flow_hop_stats`, `fraud.mule_concentration`.

## Objective

Trace movements of money such as A → B → C → D, where each account forwards funds within a 24-hour window. Then test whether chains, fast pass-through, and receivers that collect many labelled transactions (mule behaviour) are associated with fraud.

## Data basis

Every source row has `sender_account` → `receiver_account`, and **893,115 accounts appear on both sides**. Real chains therefore exist in the data, and **no synthetic transfer extension was needed**. Chains follow the dataset's own sender→receiver semantics for every transaction type. The transactions in chains split almost evenly by type: DEPOSIT 50,943, PAYMENT 51,230, TRANSFER 51,613, WITHDRAWAL 50,773. Self-transfers (6 rows) are excluded.

## Methodology: the recursive CTE explained

```sql
WITH RECURSIVE chain AS (
    -- ANCHOR MEMBER: every hop (A->B at t1, then B->C at t1 < t2 <= t1 + 24h), excluding round trips C = A
    SELECT h.txn_in AS root_txn, 2 AS hop_count,
           ARRAY[h.sender_in, h.via_account, h.receiver_out] AS account_path,
           ARRAY[h.txn_in::TEXT, h.txn_out::TEXT]            AS txn_path,
           h.ts_in AS start_ts, h.ts_out AS last_ts, h.txn_out AS last_txn, ...
    FROM flow_hops h WHERE NOT h.is_round_trip
  UNION ALL
    -- RECURSIVE MEMBER: the chain's last transaction becomes the next hop's incoming leg
    SELECT c.root_txn, c.hop_count + 1, c.account_path || h.receiver_out, c.txn_path || h.txn_out::TEXT, ...
    FROM chain c JOIN flow_hops h ON h.txn_in = c.last_txn
    WHERE c.hop_count < 6                                          -- depth guard (max 6 transfers)
      AND h.ts_out <= c.start_ts + INTERVAL '24 hours'              -- whole chain inside 24 h
      AND NOT (h.receiver_out = ANY (c.account_path))               -- cycle prevention
)
SELECT DISTINCT ON (txn_path) * FROM chain ORDER BY txn_path;      -- identical paths collapse
```

Then, using a GIN index on `txn_path`, **only maximal chains are kept**: a chain is removed if another chain's transaction array contains all of its transactions (`@>`).

| Requirement | How it is met |
|---|---|
| Anchor member | Each qualifying hop seeds a 2-transaction chain; its 24 h clock starts at the first transfer |
| Recursive member | Extends the chain through `flow_hops` from the last transaction |
| Time window | Each hop is later than the previous one, and the whole chain finishes ≤ 24 h after it starts |
| Maximum depth | 6 transfers (7 accounts); the longest chain observed has 4 |
| Cycle prevention | The next receiver may not already be in `account_path`; round trips (A→B→A) are counted separately |
| Duplicate-path prevention | `DISTINCT ON (txn_path)`, then removal of every chain contained in a longer one |
| Explainable output | `path_text` such as `8126249442 -> 2677094437 -> …`, plus timings, amounts and label counts per path |

The fixture test (`sql/tests/test_fixture.sql`) proves the logic. The chain A→B→C→D→A is cut before revisiting A; a hop 26 h later is excluded; and the sub-path C→D→A is not reported separately.

## Findings

| Measure | Value |
|---|---|
| Hops (A→B then B→C within 24 h) | **105,058** |
| Round trips A→B→A | 1 |
| Hops forwarded within 1 h | 6,416 (6.1%) |
| Median time from receipt to forwarding | 682 min (11.4 h) |
| Distinct pass-through accounts | 96,562 |
| Maximal chains | **103,639** |
| Chains completing within 1 h | 6,093 (5.9%) |
| Chains where the last amount is 80–100% of the first (value carried through) | 3,083 (3.0%) |
| Longest chain | 4 transfers (22 chains) |

### Are chains associated with fraud?

| Transfers in chain | Chains | Median elapsed | Chains containing a labelled txn | Expected if independent, 1 − (1 − p)ⁿ | Lift |
|---|---|---|---|---|---|
| 2 | 102,200 | 694 min | 7.14% | 7.05% | **1.01** |
| 3 | 1,417 | 1,069 min | 10.16% | 10.39% | **0.98** |
| 4 | 22 | 1,122 min | 13.64% | 13.61% | **1.00** |

| Propagation test | Value |
|---|---|
| Outgoing hop labelled, given incoming hop labelled | 3.18% |
| Outgoing hop labelled, given incoming hop not labelled | 3.71% |
| `RAPID_PASS_THROUGH` flag: lift / RR (95% CI) | 1.02 / 1.02 (0.99 – 1.06) |

### Mule concentration (receivers collecting labelled transactions)

| Labelled inbound txns per receiver | Observed receivers | Expected if labels were random (Binomial) | O/E |
|---|---|---|---|
| 0 | 733,663 | 733,811.8 | 1.000 |
| 1 | 147,319 | 147,080.5 | 1.002 |
| 2 | 14,635 | 14,670.3 | 0.998 |
| 3 | 925 | 975.7 | 0.948 |
| 4 | 46 | 48.7 | 0.945 |
| 5 | 1 | 1.9 | 0.52 |

### Example chains (longest)

| Path | Elapsed | First → last amount (NGN) | Labelled txns |
|---|---|---|---|
| 8126249442 → 2677094437 → 4152139177 → 5404906246 → 6393935940 | 468 min | 4,493,344 → 21,219 | 0 |
| 7113514570 → 8920246325 → 2504186241 → 9810676846 → 4502056818 | 538 min | 290,844 → 6,673 | 0 |
| 9007680173 → 2781981844 → 5564047812 → 8667334349 → 6104586440 | 669 min | 94,566 → 695,864 | 0 |

## Interpretation

* The recursive engine finds over 100,000 genuine (in-data) money chains, but **they carry exactly the fraud rate that chance predicts**. Fraud does not propagate along hops: a labelled incoming payment is followed by a labelled outgoing payment *less* often (3.2%) than an unlabelled one (3.7%).
* No receiver concentrates labelled inflows beyond what random labelling produces. The data has no mule accounts in the usual sense.
* The amounts confirm it. In real layering, value is forwarded quickly with a small skim. Here the forwarded amount is unrelated to the amount received, and only 3% of chains carry 80–100% of the value through.

## Business implications

* The recursive CTE is production-ready: bounded, cycle-safe, de-duplicated and explainable. On real payment data, the same query plus the amount-retention ratio and the ≤ 1 h fast-hop count is a standard mule-detection feature.
* In this dataset, money-flow evidence should not raise a transaction's risk score on its own. The engine therefore uses it mainly as a *linkage* source for ring detection, where it must be corroborated by device or IP evidence.
* Recommended production refinements:
  * restrict chains to TRANSFER / P2P types;
  * require a retained-value ratio (for example, the outgoing amount is 70–100% of the incoming);
  * weight hops forwarded within 60 minutes.

## Limitations

* Every source row has a receiver, including deposits and withdrawals whose counterparty semantics are unclear. Chains follow the data as published.
* Labels describe the sending transaction. A chain's "labelled" count therefore says nothing about whether downstream accounts were knowingly involved.
* The 24 h window and depth of 6 are parameters (`flow_window_hours`, `flow_max_depth`). Longer windows would find more, weaker chains.
