# data/processed

Analytical outputs. `data_quality_checks.csv` and `column_profile.csv` come from `scripts/01_clean_data.py`; every other file is exported from PostgreSQL by `scripts/02_build_database.py` after `sql/fraud_network.sql` runs over all 5,000,000 transactions. These are rule outputs and statistics, **not** fraud labels.

| File | Rows | Engine source |
|---|---|---|
| `account_status_summary.csv` | 5 | fraud.account_status_summary |
| `column_profile.csv` | 21 | 01_clean_data.py |
| `data_quality_checks.csv` | 25 | 01_clean_data.py |
| `device_connectivity_distribution.csv` | 9 | fraud.device_connectivity_distribution |
| `device_linkage_pairs.csv` | 594 | fraud.device_linkage_pairs |
| `device_linkage_summary.csv` | 594 | fraud.device_linkage_summary |
| `engine_parameters.csv` | 23 | fraud.engine_parameters |
| `flag_definitions.csv` | 12 | fraud.flag_definitions |
| `flag_performance.csv` | 12 | fraud.flag_performance |
| `fraud_ring_membership.csv` | 260,139 | fraud.fraud_ring_membership |
| `fraud_rings.csv` | 53,591 | fraud.fraud_rings |
| `investigation_cases.csv` | 3,244 | fraud.investigation_cases |
| `ip_class_summary.csv` | 4 | fraud.ip_class_summary |
| `ip_network_summary.csv` | 7 | fraud.ip_network_summary |
| `ip_suspicious_overlap.csv` | 2,944 | fraud.ip_suspicious_overlap |
| `kpi_summary.csv` | 22 | fraud.kpi_summary |
| `label_concentration.csv` | 20 | fraud.label_concentration |
| `money_flow_hop_stats.csv` | 1 | fraud.money_flow_hop_stats |
| `money_flow_paths.csv` | 103,639 | fraud.money_flow_paths |
| `money_flow_summary.csv` | 3 | fraud.money_flow_summary |
| `mule_concentration.csv` | 7 | fraud.mule_concentration |
| `ring_size_distribution.csv` | 5 | fraud.ring_size_distribution |
| `ring_validation.csv` | 5 | fraud.ring_validation |
| `rule_evaluation.csv` | 14 | fraud.rule_evaluation |
| `rule_threshold_sweep.csv` | 40 | fraud.rule_threshold_sweep |
| `tier_performance.csv` | 4 | fraud.tier_performance |
| `velocity_distribution.csv` | 10 | fraud.velocity_distribution |
| `velocity_episodes.csv` | 1,072 | fraud.velocity_episodes |
| `velocity_spec_check.csv` | 1 | fraud.velocity_spec_check |
| `vw_flagged_account_timeline.csv` | 5,000 | view fraud.vw_flagged_account_timeline (sample of rows) |
| `vw_ring_key_accounts.csv` | 2,255 | view fraud.vw_ring_key_accounts (sample of rows) |
| `vw_ring_leaderboard.csv` | 500 | view fraud.vw_ring_leaderboard (sample of rows) |

Section-by-section meaning: [`../../data_dictionary.md`](../../data_dictionary.md) §3 and the reports in [`../../analysis/`](../../analysis/).
