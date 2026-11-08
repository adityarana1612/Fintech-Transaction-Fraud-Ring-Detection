# Shared-Device Linkage Analysis

**Engine section:** `sql/fraud_network.sql` § A (steps A1–A3).
**Outputs:** `fraud.device_linkage_pairs`, `fraud.device_linkage_summary`, `fraud.device_connectivity_distribution`, exported to `data/processed/`.

## Objective

Find accounts that use the same `device_id` within one hour of each other. Measure how connected those devices are and how often linked accounts carry a fraud label. Then decide whether device sharing in this dataset is evidence of coordination or just coincidence.

## Methodology

1. **Pairing (A1).** A range self-join on the `(device_id, txn_ts)` index finds every pair of transactions on the same device by *different* sender accounts, with the second transaction no more than 3,600 s after the first. The predicate `(b.txn_ts, b.transaction_id) > (a.txn_ts, a.transaction_id)` emits each unordered pair exactly once.
2. **Per-device summary (A2).** For each device the summary records:
   - linked accounts and their ids;
   - first and last observed timestamps;
   - minimum, average and maximum gap between account activities;
   - all-time accounts on the device;
   - fraud-labelled accounts and labelled transactions inside the window.
3. **Baseline (A3).** Device ids are `D` + 7 digits; the observed span is 8,999,998 ids. If 5,000,000 transactions picked ids at random, transactions per device would follow Poisson(λ = 0.5556). Observed and expected device counts are compared for each level of sharing.
4. **Label exposure, adjusted for activity.** Accounts that transact more are both more likely to be linked and more likely to carry at least one label. Expected labelled accounts are therefore Σ P(label | sent_txn_count), taken from `fraud.label_concentration`.

## Findings

### Volume of linkage

| Measure | Value |
|---|---|
| Transaction pairs: same device, different accounts, ≤ 1 h apart | **594** |
| Devices involved | 594 (one pair each) |
| Accounts involved | 1,188 (1,187 distinct senders) |
| Largest number of accounts linked by one device in-window | **2** |
| Pairs ≤ 60 s apart / ≤ 10 min apart | 8 / 91 |
| Median gap between the two accounts | 1,786 s (≈ 30 min) |

No device connected more than two accounts within an hour. Gaps are spread almost evenly across the hour: the median of 1,786 s is close to the 1,800 s expected for random timing, and 91 of 594 pairs (15%) fall in the first 10 minutes against about 99 expected. A device shared by a coordinated group would show gaps clustered at seconds to minutes; this does not.

### Device connectivity against random collision

| Transactions on device | Observed devices | Expected if ids were random | Observed / expected | Fraud rate |
|---|---|---|---|---|
| 1 | 2,867,947 | 2,868,767 | 1.000 | 3.600% |
| 2 | 796,902 | 796,880 | 1.000 | 3.592% |
| 3 | 147,960 | 147,570 | 1.003 | 3.577% |
| 4 | 20,428 | 20,496 | 0.997 | 3.397% |
| 5 | 2,287 | 2,277 | 1.004 | 3.148% |
| 6 | 176 | 211 | 0.835 | 2.936% |
| 7 | 19 | 17 | 1.135 | 3.008% |
| 8 | 3 | 1 | 2.58 | 4.167% |
| 9 | 1 | 0.07 | 13.9 | 0.000% |

From one to seven transactions per device, the observed counts match a random draw to within sampling noise. Only four devices sit above the Poisson tail (8–9 transactions), and none of them carries an elevated fraud rate. Highly shared devices are **not** riskier: the fraud rate falls slightly as sharing rises.

### Fraud-label exposure

| Measure | Value |
|---|---|
| Labelled transactions among the 1,188 in-window transactions | 44 (3.70% vs 3.59% overall) |
| `SHARED_DEVICE_1H` flag: lift / relative risk (95% CI) | 1.03 / 1.03 (0.77 – 1.38) |
| Linked accounts with ≥ 1 fraud label, observed vs expected for their activity level | 260 vs 257.9 (**O/E 1.01**) |

### Example: tightest pairs (for an investigator)

| Device | Accounts | Gap | Labelled accounts |
|---|---|---|---|
| D1847216 | 1519601446, 9678711669 | 0.8 s | 0 |
| D1268723 | 2856308125, 2937779694 | 1.8 s | 1 |
| D5722876 | 5861366265, 8031120140 | 21.9 s | 0 |
| D5700109 | 3341168620, 3509802573 | 46.5 s | 1 |

## Interpretation

* **In this dataset, device sharing is consistent with chance.** The id distribution reproduces a Poisson draw, the in-window gaps are uniform, and linked accounts carry labels at the expected rate.
* The engine still surfaces real (in-data) pairs and ranks them by tightness. A 0.8-second gap between two accounts on one device is exactly the kind of lead an analyst should review in production data. Here it does not correlate with the label.
* A shared device was deliberately **not** treated as proof of fraud. It earns 25 score points (the largest single weight), but in the ring logic it only matters alongside a second, independent evidence type.

## Business implications

* On real data, the same query would be the first line of ring detection. The A3 baseline should be run first: if device ids are this close to random, the problem lies with device fingerprinting (hashing, resets, emulators), not with fraud.
* Do not auto-decline on shared device. Legitimate sharing (family phones, agent-banking POS terminals) is common in Nigeria's agent and USSD channels.
* Recommended production threshold: a device seen with **≥ 3 distinct accounts within 24 h**, or **≥ 2 accounts within 60 s**, triggers review. Neither occurs here (0 and 8 cases), so the rule's cost on this data would be negligible.

## Limitations

* `device_hash` is a publisher field whose generation process is unknown. Its near-random distribution means it cannot represent persistent physical devices.
* There is no device-to-customer registry, so shared family devices cannot be separated from mule devices.
* The ±1 h window is symmetric and uses future information. It is an investigation-time view; a real-time check would only look back.
