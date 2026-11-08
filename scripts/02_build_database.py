"""
02_build_database.py
-----------------------------------------------------------------------------
Builds the PostgreSQL fraud-investigation database and runs the detection engine.

  1. sql/schema.sql         - drop/create schema `fraud` (core + derived tables)
  2. COPY data/cleaned/transactions_clean.csv -> fraud.stg_transactions
  3. sql/load_core.sql      - populate core tables (single transaction) + indexes
  4. sql/fraud_network.sql  - detection engine (steps separated by `-- >>> STEP:`)
  5. export analytical outputs to data/processed/*.csv

Connection: --dsn argument, else the DATABASE_URL environment variable, else an
embedded PostgreSQL started with the optional `pgserver` package.

Usage:
  python scripts/02_build_database.py                 # full build
  python scripts/02_build_database.py --engine-only   # re-run steps 4-5 only
  python scripts/02_build_database.py --from-step G1  # resume engine at a step
-----------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "sql"
CLEAN = ROOT / "data" / "cleaned" / "transactions_clean.csv"
OUT = ROOT / "data" / "processed"

# analytical outputs exported for the reports, Excel workbook and dashboard
EXPORTS = {
    "engine_parameters": "SELECT * FROM fraud.engine_parameters ORDER BY param_name",
    "flag_definitions": "SELECT * FROM fraud.flag_definitions ORDER BY flag_code",
    "flag_performance": "SELECT * FROM fraud.flag_performance ORDER BY relative_risk DESC NULLS LAST",
    "tier_performance": "SELECT * FROM fraud.tier_performance ORDER BY min_score DESC",
    "device_connectivity_distribution": "SELECT * FROM fraud.device_connectivity_distribution ORDER BY txns_per_device",
    "device_linkage_summary": "SELECT * FROM fraud.device_linkage_summary ORDER BY linked_accounts DESC, min_gap_seconds",
    "device_linkage_pairs": "SELECT * FROM fraud.device_linkage_pairs ORDER BY ts_a",
    "ip_class_summary": "SELECT * FROM fraud.ip_class_summary ORDER BY min_accounts",
    "ip_network_summary": "SELECT * FROM fraud.ip_network_summary ORDER BY txn_count DESC",
    "ip_suspicious_overlap": "SELECT * FROM fraud.ip_suspicious_overlap ORDER BY overlap_pairs DESC, ip_address",
    "velocity_distribution": "SELECT * FROM fraud.velocity_distribution ORDER BY window_label, txns_in_window",
    "velocity_episodes": "SELECT * FROM fraud.velocity_episodes ORDER BY max_txns_any_10m DESC, txn_count DESC, start_ts",
    "velocity_spec_check": "SELECT * FROM fraud.velocity_spec_check",
    "money_flow_summary": "SELECT * FROM fraud.money_flow_summary ORDER BY hop_count",
    "money_flow_paths": "SELECT path_id, root_transaction_id, hop_count, path_text, array_to_string(transaction_path,'|') AS transaction_path, start_ts, end_ts, elapsed_minutes, first_amount_ngn, last_amount_ngn, total_value_ngn, fraud_txn_in_path, labelled_accounts_in_path FROM fraud.money_flow_paths ORDER BY hop_count DESC, elapsed_minutes",
    "money_flow_hop_stats": "SELECT * FROM fraud.money_flow_hop_stats",
    "mule_concentration": "SELECT * FROM fraud.mule_concentration ORDER BY fraud_inbound_txns",
    "fraud_rings": "SELECT * FROM fraud.fraud_rings ORDER BY ring_id",
    "fraud_ring_membership": "SELECT * FROM fraud.fraud_ring_membership ORDER BY ring_id, degree DESC",
    "ring_validation": "SELECT * FROM fraud.ring_validation ORDER BY band_order",
    "ring_size_distribution": "SELECT * FROM fraud.ring_size_distribution ORDER BY size_band_order",
    "account_status_summary": "SELECT * FROM fraud.account_status_summary ORDER BY accounts DESC",
    "investigation_cases": "SELECT * FROM fraud.investigation_cases ORDER BY priority_rank, case_id",
    "rule_evaluation": "SELECT * FROM fraud.rule_evaluation ORDER BY rule_order",
    "rule_threshold_sweep": "SELECT * FROM fraud.rule_threshold_sweep ORDER BY sweep, threshold_value",
    "kpi_summary": "SELECT * FROM fraud.kpi_summary",
    "vw_ring_leaderboard": "SELECT * FROM fraud.vw_ring_leaderboard ORDER BY risk_rank, ring_id LIMIT 500",
    "vw_ring_key_accounts": "SELECT * FROM fraud.vw_ring_key_accounts WHERE ring_id <= 100 ORDER BY ring_id, member_rank",
    "vw_flagged_account_timeline": "SELECT * FROM fraud.vw_flagged_account_timeline ORDER BY sender_account, txn_ts LIMIT 5000",
    "label_concentration": "SELECT * FROM fraud.label_concentration ORDER BY sent_txn_count",
}


def connect(dsn: str | None):
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        import pgserver  # optional embedded PostgreSQL (pip install pgserver)
        pgdata = ROOT / "data" / "pgdata"
        srv = pgserver.get_server(str(pgdata), cleanup_mode=None)
        dsn = srv.get_uri()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def run_file(cur, path: Path, from_step: str | None = None) -> None:
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^-- >>> STEP: *(\S+).*$", text, flags=re.M)
    preamble, steps = parts[0], list(zip(parts[1::2], parts[2::2]))
    if not steps:
        t = time.time()
        cur.execute(text)
        print(f"  {path.name:<22} {time.time() - t:7.1f}s")
        return
    cur.execute(preamble)
    started = from_step is None
    for name, body in steps:
        if not started:
            started = name == from_step
            if not started:
                continue
        t = time.time()
        cur.execute(body)
        print(f"  {path.name} [{name:<28}] {time.time() - t:7.1f}s")


def export(cur) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, sql in EXPORTS.items():
        with open(OUT / f"{name}.csv", "w", encoding="utf-8", newline="") as f:
            cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", f)
    print(f"  exported {len(EXPORTS)} tables to {OUT}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    ap.add_argument("--engine-only", action="store_true")
    ap.add_argument("--from-step")
    ap.add_argument("--no-export", action="store_true")
    a = ap.parse_args()

    conn = connect(a.dsn)
    cur = conn.cursor()
    cur.execute("SET maintenance_work_mem = '1GB'; SET work_mem = '256MB';")
    t0 = time.time()
    if not a.engine_only and not a.from_step:
        run_file(cur, SQL / "schema.sql")
        t = time.time()
        with open(CLEAN, "r", encoding="utf-8") as f:
            cur.copy_expert("COPY fraud.stg_transactions FROM STDIN WITH (FORMAT csv, HEADER true)", f)
        print(f"  COPY staging            {time.time() - t:7.1f}s")
        run_file(cur, SQL / "load_core.sql")
    run_file(cur, SQL / "fraud_network.sql", a.from_step)
    if not a.no_export:
        export(cur)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
