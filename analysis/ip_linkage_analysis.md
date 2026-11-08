# Shared-IP Linkage Analysis

**Engine section:** `sql/fraud_network.sql` § B (steps B1–B5).
**Outputs:** `fraud.ip_addresses.network_class`, `fraud.ip_linkage_pairs`, `fraud.ip_network_summary`, `fraud.ip_class_summary`, `fraud.ip_suspicious_overlap`.

## Objective

Find accounts that share IP addresses and measure their transaction frequency, concurrent activity and fraud-label distribution. The analysis must **distinguish suspicious shared use from legitimate shared networks**, such as mobile carrier gateways.

## Methodology

1. **Density class (B1).** Each IP is classed by the distinct sender accounts it served over the whole period:

   | Class | Accounts on the IP |
   |---|---|
   | SINGLE_ACCOUNT | 1 |
   | SMALL_SHARED | 2–10 |
   | MODERATE_SHARED | 11–50 |
   | HIGH_DENSITY_SHARED | > 50 |

2. **Concurrent use (B2).** A pair is two different sender accounts on the same IP within 10 minutes. The query is an index nested loop on `(ip_address, txn_ts)` (hash and merge joins are disabled for this one statement). That turns what would be a per-IP Cartesian product of about 190 M comparisons on the densest network into 5 M short range probes.
3. **Network view (B3).** Per /16 network: IP count, accounts per IP, overlap pairs per 1,000 transactions, and fraud rate overall and among overlapping transactions.
4. **Treatment.** HIGH_DENSITY_SHARED IPs are treated as **legitimate shared networks**. They receive only a weight-0 context flag, `SHARED_NETWORK_IP`, and are excluded from ring edges and from the `SHARED_IP_10M` flag.

## Findings

### Seven networks, two very different behaviours

| Network | IPs | Txns | Avg accounts / IP | Max accounts / IP | Dominant class | 10-min overlap pairs | Pairs / 1k txns | Fraud rate | Fraud rate, overlapping txns |
|---|---|---|---|---|---|---|---|---|---|
| 197.210.0.0/16 | 65,536 | 1,251,526 | 19.1 | 38 | MODERATE | 936 | 0.75 | 3.579% | 3.478% |
| 41.58.0.0/16 | 32,768 | 1,251,208 | 38.2 | 62 | MODERATE | 1,954 | 1.56 | 3.589% | 4.128% |
| **102.89.0.0/16** | **4,096** | **1,247,909** | **304.6** | **365** | **HIGH_DENSITY** | **15,240** | **12.21** | 3.606% | 3.565% |
| 105.112.0.0/16 | 64,985 | 313,263 | 4.8 | 17 | SMALL | 52 | 0.17 | 3.567% | 3.846% |
| 105.113.0.0/16 | 64,992 | 311,670 | 4.8 | 17 | SMALL | 61 | 0.20 | 3.575% | 2.459% |
| 105.114.0.0/16 | 64,997 | 311,546 | 4.8 | 16 | SMALL | 68 | 0.22 | 3.600% | 3.676% |
| 105.115.0.0/16 | 64,988 | 312,878 | 4.8 | 15 | SMALL | 61 | 0.19 | 3.620% | 6.557% |

`102.89.0.0/16` carries a quarter of all transactions through only 4,096 addresses, about 305 accounts per IP, and produces 83% of all concurrent-use pairs (15,240 of 18,372). That is the signature of **carrier-grade NAT**, where thousands of mobile subscribers share a few public IPs. Its fraud rate (3.606%) is exactly the population rate. Treating it as suspicious would flood investigators with legitimate customers.

### By density class

| Class | IPs | Txns (% of all) | Fraud rate | Txns in a 10-min overlap | Fraud rate in overlap | Treatment |
|---|---|---|---|---|---|---|
| SINGLE_ACCOUNT | 10,573 | 10,573 (0.2%) | 3.263% | 0 | – | eligible |
| SMALL_SHARED | 248,035 | 1,220,358 (24.4%) | 3.601% | 480 | 3.750% | eligible |
| MODERATE_SHARED | 98,745 | 2,472,569 (49.5%) | 3.580% | 5,545 | 3.913% | eligible |
| HIGH_DENSITY_SHARED | 5,009 | 1,296,500 (25.9%) | 3.605% | 30,127 | 3.575% | legitimate shared network |

### Suspicious (eligible) concurrent use

| Measure | Value |
|---|---|
| Overlap pairs on eligible IPs (`SHARED_IP_10M` source) | **3,018** pairs on 2,944 IPs (2,704 moderate, 240 small) |
| Max accounts overlapping on one eligible IP | 6 |
| Transactions flagged `SHARED_IP_10M` | 6,025 (0.12% of volume) |
| Fraud rate when flagged / not flagged | 3.900% / 3.591% |
| Lift / relative risk (95% CI) | 1.09 / 1.09 (0.96 – 1.23) |
| Linked accounts with ≥ 1 label, observed vs expected for activity level | 1,358 vs 1,306.5 (**O/E 1.04**) |
| Context flag `SHARED_NETWORK_IP` (high-density), lift | 1.004 (RR CI 0.995 – 1.016) |

Highest-overlap eligible IPs (investigation starting points):

| IP | Class | Overlap pairs | Overlapping accounts | All-time accounts | Min gap | Labelled accounts |
|---|---|---|---|---|---|---|
| 197.210.207.230 | MODERATE | 4 | 5 | 18 | 186 s | 0 |
| 41.58.227.121 | MODERATE | 3 | 6 | 48 | 100 s | 1 |
| 41.58.218.96 | MODERATE | 3 | 3 | 42 | 68 s | 2 |
| 41.58.224.174 | MODERATE | 3 | 3 | 35 | 110 s | 1 |

## Interpretation

* **Shared-IP usage is overwhelmingly explained by network topology.** Concurrency rises with density, as it would for shared gateways, while the fraud rate stays flat at about 3.6% in every class and every network.
* After high-density networks are removed, the remaining concurrent-use signal is small (lift 1.09) and its confidence interval includes 1. Part of even that small difference is mechanical: labelled fraud never falls on an account's first generated transaction, and accounts that overlap are, on average, further into their histories. The fraud rate among non-first transactions is 4.38%.
* `105.115.0.0/16` shows 6.6% fraud among overlapping transactions, but that rests on only 61 pairs (about 120 transactions), and the neighbouring 105.113 shows 2.5%. This is sampling noise, not a finding.

## Business implications

* **An IP must never be judged on its own.** A policy of "N accounts per IP" would put 26% of volume (the high-density class) in front of investigators for zero gain in this data.
* Production practice should whitelist or down-weight known carrier NAT and enterprise egress ranges by ASN. Shared-IP evidence should only be used together with device or money-flow evidence. This project's ring corroboration rule does exactly that.
* A recommended enrichment for real deployment: ASN, carrier, VPN/proxy/hosting flags, and IP-to-geolocation distance from the account's usual location.

## Limitations

* There is no ASN or geolocation data, so "carrier NAT" is an inference from density.
* The class boundaries (10 and 50 accounts) are judgement calls. They are parameterised (`ip_high_density_accounts`) and can be re-run.
* The ±10 min window is symmetric (investigation-time view).
