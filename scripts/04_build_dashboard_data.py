"""
04_build_dashboard_data.py
-----------------------------------------------------------------------------
Exports the PostgreSQL engine outputs into dashboard/assets/fraud_data.js
(window.FRAUD_DATA = {...}), which dashboard/fraud_ops_console.html loads with a
plain <script src> tag - so the console works from file:// with no server.

Two data blocks:
  cube : population cube over ALL transactions, one row per
         (month, hour, merchant group, type, fraud label, rule tier, 24h-velocity
         bucket, flag slot) with count and value. Flag slot 0 = every transaction
         once; slot k = transactions carrying behavioural flag k. KPIs and charts
         are summed from it, so they are exact for every filter combination.
  tx   : transaction-level set = every HIGH/CRITICAL transaction plus every
         transaction sent by a member of a potential fraud ring (tier MEDIUM+).
  history/h_NNN.js : full sent+received history of every flagged account and
         potential-ring member, sharded by account_id % 128 and loaded on demand
         when an analyst opens the account (plain <script> tags work on file://).
Numeric columns are shipped as base64 typed arrays to keep the file compact.

Run: python scripts/04_build_dashboard_data.py [--dsn ...]
-----------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dashboard" / "assets" / "fraud_data.js"
TS0 = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp())
N_SHARDS = 128

FLAG_META = {  # filterable flags (weight > 0), display order
    "SHARED_DEVICE_1H": ("Shared device (1 h)", "Shared device"),
    "SHARED_IP_10M": ("Shared IP (10 min)", "Shared IP"),
    "PRIOR_FRAUD_LINK": ("Prior fraud-labelled connection", "Prior fraud link"),
    "RAPID_PASS_THROUGH": ("Rapid pass-through (money flow)", "Pass-through"),
    "HIGH_VELOCITY": ("High transaction velocity", "Velocity"),
    "MULTI_BENEFICIARY_24H": ("Multiple beneficiaries (24 h)", "Multi-benef."),
    "NEW_DEVICE_SRC": ("New device (source flag)", "New device"),
    "HIGH_VALUE": ("High value (top 5%)", "High value"),
    "AMOUNT_SPIKE": ("Amount spike (3x average)", "Amount spike"),
    "UNUSUAL_HOUR": ("Unusual hour (00-04)", "Night"),
}
TIERS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
STATUSES = ["CONFIRMED_FRAUD_LABEL", "REQUIRES_INVESTIGATION", "POTENTIAL_RING_MEMBER", "SUSPICIOUS", "NOT_FLAGGED"]
LINKS = ["SHARED_DEVICE", "SHARED_IP", "MONEY_FLOW"]


def enc(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def connect(dsn):
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        import pgserver
        dsn = pgserver.get_server(str(ROOT / "data" / "pgdata"), cleanup_mode=None).get_uri()
    return psycopg2.connect(dsn)


def q(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    a = ap.parse_args()
    conn = connect(a.dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO fraud, public; SET work_mem = '512MB';")

    flags = [r[0] for r in q(cur, "SELECT flag_code FROM flag_definitions WHERE weight > 0")]
    flags = [f for f in FLAG_META if f in flags]
    fdef = {r[0]: r for r in q(cur, "SELECT flag_code, weight, description, flag_family FROM flag_definitions")}
    mgroups = [r[0] for r in q(cur, "SELECT DISTINCT merchant_group FROM merchants ORDER BY 1")]
    types = ["DEPOSIT", "PAYMENT", "TRANSFER", "WITHDRAWAL"]
    months = [f"{2023 + i // 12}-{i % 12 + 1:02d}" for i in range(13)]

    # temp mapping tables so the cube is grouped by small integer codes
    cur.execute("DROP TABLE IF EXISTS pg_temp.map_mg, pg_temp.map_flag")
    cur.execute("CREATE TEMP TABLE map_mg (merchant_group TEXT, idx INT)")
    cur.executemany("INSERT INTO map_mg VALUES (%s,%s)", [(g, i) for i, g in enumerate(mgroups)])
    cur.execute("CREATE TEMP TABLE map_flag (flag_code TEXT, idx INT)")
    cur.executemany("INSERT INTO map_flag VALUES (%s,%s)", [(f, i + 1) for i, f in enumerate(flags)])

    # ------------------------------------------------------------------ cube
    cur.execute("""
        CREATE TEMP TABLE dash_tx AS
        SELECT t.transaction_id,
               ((EXTRACT(YEAR FROM t.txn_ts) - 2023) * 12 + EXTRACT(MONTH FROM t.txn_ts) - 1)::INT AS m,
               t.txn_hour::INT AS h, mg.idx AS g,
               CASE t.transaction_type WHEN 'DEPOSIT' THEN 0 WHEN 'PAYMENT' THEN 1 WHEN 'TRANSFER' THEN 2 ELSE 3 END AS ty,
               l.is_fraud::INT AS f,
               CASE r.risk_tier WHEN 'LOW' THEN 0 WHEN 'MEDIUM' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END AS tier,
               LEAST(x.cnt_24h_back, 3)::INT - 1 AS vel,
               t.amount_ngn::FLOAT8 AS amt
        FROM transactions t
        JOIN merchants m USING (merchant_category)
        JOIN map_mg mg USING (merchant_group)
        JOIN fraud_labels l USING (transaction_id)
        JOIN transaction_risk_scores r USING (transaction_id)
        JOIN txn_features x USING (transaction_id)""")
    rows = q(cur, """
        SELECT m, h, g, ty, f, tier, vel, 0 AS slot, COUNT(*), SUM(amt) FROM dash_tx GROUP BY 1,2,3,4,5,6,7
        UNION ALL
        SELECT d.m, d.h, d.g, d.ty, d.f, d.tier, d.vel, mf.idx, COUNT(*), SUM(d.amt)
        FROM dash_tx d JOIN suspicious_activity_flags s USING (transaction_id) JOIN map_flag mf USING (flag_code)
        GROUP BY 1,2,3,4,5,6,7,8""")
    cube = np.array(rows, dtype=np.float64)
    key = (cube[:, 0].astype(np.uint32) | (cube[:, 1].astype(np.uint32) << 4) | (cube[:, 2].astype(np.uint32) << 9)
           | (cube[:, 3].astype(np.uint32) << 13) | (cube[:, 4].astype(np.uint32) << 15) | (cube[:, 5].astype(np.uint32) << 16)
           | (cube[:, 6].astype(np.uint32) << 18) | (cube[:, 7].astype(np.uint32) << 20))
    cube_out = {"rows": int(len(rows)), "key": enc(key.astype(np.uint32)), "cnt": enc(cube[:, 8].astype(np.uint32)),
                "amt": enc(cube[:, 9].astype(np.float64))}

    # ------------------------------------------------- investigation set
    cur.execute("""
        CREATE TEMP TABLE inv_acct AS
        SELECT account_id FROM account_risk_profile WHERE is_flagged OR is_ring_member""")
    cur.execute("CREATE INDEX ON inv_acct (account_id)")
    # core set: every HIGH/CRITICAL txn + every txn SENT by a potential-ring member
    # (exactly what the filter-aware account/ring KPIs need)
    cur.execute("""
        CREATE TEMP TABLE inv_tx AS
        SELECT t.transaction_id FROM transactions t JOIN transaction_risk_scores r USING (transaction_id)
        WHERE r.risk_tier IN ('HIGH','CRITICAL')
        UNION SELECT t.transaction_id FROM transactions t
              JOIN account_risk_profile p ON p.account_id = t.sender_account AND p.is_ring_member""")
    tx = q(cur, """
        SELECT substr(t.transaction_id, 2)::BIGINT, t.sender_account, t.receiver_account,
               EXTRACT(EPOCH FROM t.txn_ts AT TIME ZONE 'UTC')::BIGINT, t.amount_ngn::FLOAT8,
               substr(t.device_id, 2)::BIGINT, (t.ip_address - '0.0.0.0'::INET)::BIGINT,
               mg.idx, CASE t.transaction_type WHEN 'DEPOSIT' THEN 0 WHEN 'PAYMENT' THEN 1 WHEN 'TRANSFER' THEN 2 ELSE 3 END,
               CASE r.risk_tier WHEN 'LOW' THEN 0 WHEN 'MEDIUM' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END,
               r.risk_score, l.is_fraud::INT, LEAST(x.cnt_24h_back, 3) - 1, COALESCE(r.flag_list, '')
        FROM inv_tx i JOIN transactions t USING (transaction_id)
        JOIN merchants m USING (merchant_category) JOIN map_mg mg USING (merchant_group)
        JOIN transaction_risk_scores r USING (transaction_id) JOIN fraud_labels l USING (transaction_id)
        JOIN txn_features x USING (transaction_id)
        ORDER BY t.txn_ts, t.transaction_id""")

    links = q(cur, """
        WITH mf_dir AS (
            SELECT LEAST(t.sender_account, t.receiver_account) AS a, GREATEST(t.sender_account, t.receiver_account) AS b,
                   SIGN(SUM(CASE WHEN t.sender_account < t.receiver_account THEN 1 ELSE -1 END)) *
                   (CASE WHEN COUNT(DISTINCT t.sender_account) = 1 THEN 1 ELSE 0 END) AS dir
            FROM (SELECT DISTINCT unnest(transaction_path) AS transaction_id FROM money_flow_paths) p
            JOIN transactions t USING (transaction_id)
            GROUP BY 1, 2)
        SELECT l.account_a, l.account_b, l.link_type, l.evidence_count, COALESCE(ROUND(l.min_gap_seconds)::INT, -1),
               COALESCE(d.dir, 0)::INT
        FROM account_links l
        LEFT JOIN mf_dir d ON l.link_type = 'MONEY_FLOW' AND d.a = l.account_a AND d.b = l.account_b
        WHERE l.account_a IN (SELECT account_id FROM inv_acct) OR l.account_b IN (SELECT account_id FROM inv_acct)""")
    devp = q(cur, """SELECT substr(device_id, 2)::BIGINT, account_a, account_b,
                            EXTRACT(EPOCH FROM ts_a AT TIME ZONE 'UTC')::BIGINT, EXTRACT(EPOCH FROM ts_b AT TIME ZONE 'UTC')::BIGINT,
                            ROUND(gap_seconds)::INT FROM device_linkage_pairs""")
    ipclasses = ["SINGLE_ACCOUNT", "SMALL_SHARED", "MODERATE_SHARED", "HIGH_DENSITY_SHARED"]
    ipp = q(cur, """SELECT (ip_address - '0.0.0.0'::INET)::BIGINT, account_a, account_b,
                           EXTRACT(EPOCH FROM ts_a AT TIME ZONE 'UTC')::BIGINT, EXTRACT(EPOCH FROM ts_b AT TIME ZONE 'UTC')::BIGINT,
                           ROUND(gap_seconds)::INT, network_class
                    FROM ip_linkage_pairs WHERE network_class <> 'HIGH_DENSITY_SHARED'""")
    paths = q(cur, """
        SELECT p.account_path, EXTRACT(EPOCH FROM p.start_ts AT TIME ZONE 'UTC')::BIGINT, p.elapsed_minutes::FLOAT8,
               p.total_value_ngn::FLOAT8, p.fraud_txn_in_path
        FROM money_flow_paths p
        WHERE p.account_path && (SELECT array_agg(account_id) FROM inv_acct)""")
    rings = q(cur, """
        SELECT r.ring_id, r.risk_tier, r.ring_risk_score, r.network_score, r.label_score, r.account_count,
               r.device_links, r.ip_links, r.money_flow_links, r.connection_reasons, r.labelled_fraud_accounts,
               r.suspicious_txn_count, r.total_txn_count, r.total_value_ngn::FLOAT8,
               array_agg(m.account_id ORDER BY m.account_id)
        FROM fraud_rings r JOIN fraud_ring_membership m USING (ring_id)
        WHERE r.risk_tier IN ('CRITICAL','HIGH','MEDIUM')
        GROUP BY r.ring_id ORDER BY r.ring_id""")

    # ------------------------------------------------- account index
    acct_set = set()
    for r in tx:
        acct_set.update((r[1], r[2]))
    for r in links:
        acct_set.update((r[0], r[1]))
    for r in devp + ipp:
        acct_set.update((r[1], r[2]))
    for r in paths:
        acct_set.update(r[0])
    for r in rings:
        acct_set.update(r[14])
    acct_ids = sorted(acct_set)
    aidx = {v: i for i, v in enumerate(acct_ids)}
    cur.execute("CREATE TEMP TABLE dash_acct (account_id BIGINT PRIMARY KEY)")
    cur.executemany("INSERT INTO dash_acct VALUES (%s)", [(v,) for v in acct_ids])
    prof = q(cur, """
        SELECT d.account_id, p.investigation_status, COALESCE(p.ring_id, -1),
               CASE p.ring_risk_tier WHEN 'LOW' THEN 0 WHEN 'MEDIUM' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'CRITICAL' THEN 3 ELSE -1 END,
               p.has_fraud_label::INT, p.labelled_fraud_txn_count, p.max_txn_risk_score, a.sent_txn_count, a.received_txn_count
        FROM dash_acct d JOIN account_risk_profile p USING (account_id) JOIN accounts a USING (account_id)
        ORDER BY d.account_id""")
    assert [p[0] for p in prof] == acct_ids
    potential = {r[0] for r in rings}
    ring_col = np.array([p[2] if p[2] in potential else -1 for p in prof], dtype=np.int32)
    ringtier = np.array([p[3] if p[2] in potential else -1 for p in prof], dtype=np.int32)
    accts = {
        "id": acct_ids,
        "cols": {"status": "u8", "ring": "i32", "ringtier": "i32", "label": "u8", "labelled": "u16", "maxscore": "u8", "sent": "u16", "recv": "u16"},
        "data": {
            "status": enc(np.array([STATUSES.index(p[1]) for p in prof], dtype=np.uint8)),
            "ring": enc(ring_col), "ringtier": enc(ringtier),
            "label": enc(np.array([p[4] for p in prof], dtype=np.uint8)),
            "labelled": enc(np.array([p[5] for p in prof], dtype=np.uint16)),
            "maxscore": enc(np.array([p[6] for p in prof], dtype=np.uint8)),
            "sent": enc(np.array([p[7] for p in prof], dtype=np.uint16)),
            "recv": enc(np.array([p[8] for p in prof], dtype=np.uint16)),
        },
    }

    fbit = {f: 1 << i for i, f in enumerate(flags)}
    t = list(zip(*tx))
    txd = {
        "cols": {"id": "u32", "snd": "u32", "rcv": "u32", "ts": "u32", "amt": "f64", "dev": "u32", "ip": "u32", "mg": "u8",
                 "ty": "u8", "tier": "u8", "score": "u8", "fraud": "u8", "vel": "u8", "flags": "u16"},
        "data": {
            "id": enc(np.array(t[0], dtype=np.uint32)),
            "snd": enc(np.array([aidx[v] for v in t[1]], dtype=np.uint32)),
            "rcv": enc(np.array([aidx[v] for v in t[2]], dtype=np.uint32)),
            "ts": enc(np.array([v - TS0 for v in t[3]], dtype=np.uint32)),
            "amt": enc(np.array(t[4], dtype=np.float64)),
            "dev": enc(np.array(t[5], dtype=np.uint32)),
            "ip": enc(np.array(t[6], dtype=np.uint32)),
            "mg": enc(np.array(t[7], dtype=np.uint8)),
            "ty": enc(np.array(t[8], dtype=np.uint8)),
            "tier": enc(np.array(t[9], dtype=np.uint8)),
            "score": enc(np.array(t[10], dtype=np.uint8)),
            "fraud": enc(np.array(t[11], dtype=np.uint8)),
            "vel": enc(np.array(t[12], dtype=np.uint8)),
            "flags": enc(np.array([sum(fbit.get(c, 0) for c in s.split("|") if c) for s in t[13]], dtype=np.uint16)),
        },
    }

    links_out = {"a": [aidx[r[0]] for r in links], "b": [aidx[r[1]] for r in links], "t": [LINKS.index(r[2]) for r in links],
                 "ev": [r[3] for r in links], "gap": [r[4] for r in links], "dir": [r[5] for r in links]}
    devp_out = {"dev": [r[0] for r in devp], "a": [aidx[r[1]] for r in devp], "b": [aidx[r[2]] for r in devp],
                "ta": [r[3] - TS0 for r in devp], "tb": [r[4] - TS0 for r in devp], "gap": [r[5] for r in devp]}
    ipp_out = {"ip": [r[0] for r in ipp], "a": [aidx[r[1]] for r in ipp], "b": [aidx[r[2]] for r in ipp],
               "ta": [r[3] - TS0 for r in ipp], "tb": [r[4] - TS0 for r in ipp], "gap": [r[5] for r in ipp],
               "cls": [ipclasses.index(r[6]) for r in ipp]}
    paths_out = [{"acc": [aidx[x] for x in r[0]], "start": r[1] - TS0, "mins": round(r[2], 1), "value": round(r[3], 2), "fraud": r[4]}
                 for r in paths]
    rings_out = [{"id": r[0], "tier": r[1], "score": r[2], "net": r[3], "lab": r[4], "n": r[5], "dl": r[6], "il": r[7], "ml": r[8],
                  "reasons": r[9], "labelled": r[10], "susp": r[11], "ntx": r[12], "value": round(r[13], 2),
                  "members": [aidx[x] for x in r[14]]} for r in rings]

    # ------------------------------------------------- meta / narrative from actual results
    kpi = dict(q(cur, "SELECT kpi, value FROM kpi_summary"))
    fp = {r[0]: r for r in q(cur, "SELECT flag_code, lift, relative_risk, fraud_rate_flagged_pct, flagged_txns FROM flag_performance")}
    net_lifts = ", ".join(f"{FLAG_META[c][1]} {float(fp[c][1]):.2f}x" for c in
                          ["SHARED_DEVICE_1H", "SHARED_IP_10M", "RAPID_PASS_THROUGH", "PRIOR_FRAUD_LINK", "HIGH_VELOCITY"] if fp[c][1] is not None)
    nd = fp["NEW_DEVICE_SRC"]
    rv = q(cur, "SELECT band, observed_to_expected, oe_ci95_low, oe_ci95_high FROM ring_validation ORDER BY band_order")
    rv_txt = "; ".join(f"{b}: O/E {float(o):.2f} (95% CI {float(lo):.2f}-{float(hi):.2f})" for b, o, lo, hi in rv if o is not None)
    period = q(cur, "SELECT MIN(txn_ts)::DATE, MAX(txn_ts)::DATE FROM transactions")[0]
    meta = {
        "n_txn": int(kpi["total_transactions"]), "n_accounts": int(kpi["total_accounts"]),
        "period": f"{period[0]} to {period[1]}", "overall_fraud_rate": float(kpi["fraud_rate_pct"]) / 100,
        "ts_epoch_offset": TS0, "generated": dt.date.today().isoformat(),
        "source_short": "Nigerian Financial Transactions & Fraud Detection Dataset (public, synthetic)",
        "source_url": "https://huggingface.co/datasets/electricsheepafrica/Nigerian-Financial-Transactions-and-Fraud-Detection-Dataset",
        "callout": (
            "<b>Reading this console honestly.</b> The source dataset is synthetic and publicly released; <code>is_fraud</code> is the publisher's label. "
            f"Network evidence is real within the data but shows almost no association with the label (lift: {net_lifts}). "
            f"The only strong separator is the publisher's <code>new_device_transaction</code> flag (lift {float(nd[1]):.2f}x; zero labelled fraud without it) - "
            "but that flag is False exactly on each account's first generated transaction, so it encodes 'not the account's first transaction', not device behaviour. "
            f"Size-adjusted ring validation (observed / expected labelled members): {rv_txt}. "
            "Rings and tiers are therefore investigation <i>leads</i>, not evidence of organised fraud."),
        "footer": (
            "<b>Definitions.</b> Fraud label = source <code>is_fraud</code>. Rule tier = sum of explainable flag weights "
            "(LOW &lt;25, MEDIUM 25-49, HIGH 50-64, CRITICAL &ge;65; HIGH+CRITICAL sized to ~100 reviews/day). Flagged account = sent &ge;1 HIGH/CRITICAL transaction. "
            "Potential fraud ring = connected group of &ge;3 accounts linked by shared device (&le;1 h), shared low/moderate-density IP (&le;10 min) "
            "or money-flow chains (&le;24 h), linked by at least two independent evidence types and scored MEDIUM or above. "
            "The rising monthly count of HIGH/CRITICAL transactions is mechanical: PRIOR_FRAUD_LINK and AMOUNT_SPIKE need history that accumulates through the year. High-density IPs (&gt;50 accounts, carrier-NAT-like) are treated as legitimate shared networks. "
            "Amounts are in Nigerian naira (NGN). All figures are computed in your browser from <code>assets/fraud_data.js</code>, produced by "
            "<code>sql/fraud_network.sql</code> on PostgreSQL 16 over all 5,000,000 transactions."),
    }
    dims = {"months": months, "mgroups": mgroups, "types": types, "tiers": TIERS, "statuses": STATUSES, "ipclasses": ipclasses,
            "flags": [{"code": f, "label": FLAG_META[f][0], "short": FLAG_META[f][1], "weight": int(fdef[f][1]),
                       "desc": fdef[f][2], "family": fdef[f][3]} for f in flags]}
    data = {"meta": meta, "dims": dims, "cube": cube_out, "tx": txd, "accts": accts, "links": links_out,
            "devpairs": devp_out, "ippairs": ipp_out, "paths": paths_out, "rings": rings_out}
    # ------------------------------------------------- account history shards (loaded on demand)
    # Full sent + received history of every investigation account (flagged or potential-ring
    # member), split into N_SHARDS files by account_id % N_SHARDS.
    hist = q(cur, """
        WITH h AS (
            SELECT i.account_id, t.transaction_id, t.receiver_account AS cp, 1 AS dir
            FROM inv_acct i JOIN transactions t ON t.sender_account = i.account_id
            UNION ALL
            SELECT i.account_id, t.transaction_id, t.sender_account, 0
            FROM inv_acct i JOIN transactions t ON t.receiver_account = i.account_id)
        SELECT h.account_id, substr(t.transaction_id, 2)::BIGINT, EXTRACT(EPOCH FROM t.txn_ts AT TIME ZONE 'UTC')::BIGINT,
               ROUND(t.amount_ngn * 100)::BIGINT, h.cp, h.dir,
               CASE t.transaction_type WHEN 'DEPOSIT' THEN 0 WHEN 'PAYMENT' THEN 1 WHEN 'TRANSFER' THEN 2 ELSE 3 END,
               CASE r.risk_tier WHEN 'LOW' THEN 0 WHEN 'MEDIUM' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END,
               r.risk_score, l.is_fraud::INT, COALESCE(r.flag_list, ''),
               substr(t.device_id, 2)::BIGINT, (t.ip_address - '0.0.0.0'::INET)::BIGINT
        FROM h JOIN transactions t USING (transaction_id)
        JOIN transaction_risk_scores r USING (transaction_id) JOIN fraud_labels l USING (transaction_id)
        ORDER BY h.account_id, t.txn_ts, t.transaction_id""")
    hdir = OUT.parent / "history"
    hdir.mkdir(parents=True, exist_ok=True)
    for old in hdir.glob("h_*.js"):
        old.unlink()
    shards: dict[int, list] = {k: [] for k in range(N_SHARDS)}
    for r in hist:
        shards[r[0] % N_SHARDS].append(r)
    hist_bytes = 0
    for k, rows_k in shards.items():
        acc, start, count = [], [], []
        for i, r in enumerate(rows_k):
            if not acc or acc[-1] != r[0]:
                acc.append(r[0]); start.append(i); count.append(0)
            count[-1] += 1
        c = list(zip(*rows_k)) if rows_k else [[] for _ in range(13)]
        payload = {
            "acc": acc, "start": start, "count": count,
            "cols": {"id": "u32", "ts": "u32", "amt": "u32", "cp": "f64", "dir": "u8", "ty": "u8", "tier": "u8",
                     "score": "u8", "fraud": "u8", "flags": "u16", "dev": "u32", "ip": "u32"},
            "data": {"id": enc(np.array(c[1], dtype=np.uint32)), "ts": enc(np.array([v - TS0 for v in c[2]], dtype=np.uint32)),
                     "amt": enc(np.array(c[3], dtype=np.uint32)), "cp": enc(np.array(c[4], dtype=np.float64)),
                     "dir": enc(np.array(c[5], dtype=np.uint8)), "ty": enc(np.array(c[6], dtype=np.uint8)),
                     "tier": enc(np.array(c[7], dtype=np.uint8)), "score": enc(np.array(c[8], dtype=np.uint8)),
                     "fraud": enc(np.array(c[9], dtype=np.uint8)),
                     "flags": enc(np.array([sum(fbit.get(x, 0) for x in s.split("|") if x) for s in c[10]], dtype=np.uint16)),
                     "dev": enc(np.array(c[11], dtype=np.uint32)), "ip": enc(np.array(c[12], dtype=np.uint32))},
        }
        p = hdir / f"h_{k:03d}.js"
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"/* account history shard {k} (account_id % {N_SHARDS}) - generated */\n")
            f.write(f"window.__fraudHist && window.__fraudHist({k}, ")
            json.dump(payload, f, separators=(",", ":"))
            f.write(");\n")
        hist_bytes += p.stat().st_size
    data["meta"]["n_shards"] = N_SHARDS
    print(f"history rows {len(hist):,} in {N_SHARDS} shards, {hist_bytes / 1e6:.1f} MB")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("/* Generated by scripts/04_build_dashboard_data.py - do not edit by hand. */\n")
        f.write("window.FRAUD_DATA = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    print(f"cube rows {len(rows):,} | inv txns {len(tx):,} | accounts {len(acct_ids):,} | links {len(links):,} | "
          f"paths {len(paths):,} | rings {len(rings):,} | {OUT.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
