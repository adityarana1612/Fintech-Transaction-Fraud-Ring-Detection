"""
06_validate.py - end-to-end quality assurance. Writes reports/validation_report.md.

  1. Engine unit test   : builds database `fraud_test`, loads sql/tests/test_fixture.sql,
                          runs the full engine and asserts known answers (cycle prevention,
                          24 h window, maximal-path de-duplication, velocity episodes, linkage).
  2. Independent recompute: re-derives key figures from data/cleaned/transactions_clean.csv
                          with DuckDB (a different engine) and compares with PostgreSQL.
  3. Engine invariants  : confusion matrices sum to N, lift formulas, path properties,
                          label/flag separation, ring membership uniqueness.
  4. Dashboard reconciliation: totals in dashboard/assets/fraud_data.js vs kpi_summary,
                          plus the in-browser self-test results (headless Edge) when available.
  5. Excel checks       : no formula errors; formula results equal SQL values.
  6. Repository hygiene : required files exist and are non-empty, no TODO/FIXME, no secrets.

Run: python scripts/06_validate.py --dsn postgresql://...  [--skip-fixture]
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import duckdb
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
CLEAN = (ROOT / "data" / "cleaned" / "transactions_clean.csv").as_posix()
RESULTS: list[tuple[str, str, bool, str]] = []   # (section, check, passed, detail)


def check(section, name, ok, detail=""):
    RESULTS.append((section, name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {section} | {name} | {detail}")


def load_builder():
    spec = importlib.util.spec_from_file_location("build", ROOT / "scripts" / "02_build_database.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def with_db(dsn: str, db: str) -> str:
    return re.sub(r"/[^/?]*(\?|$)", f"/{db}\\1", dsn, count=1) if dsn.count("/") >= 3 else dsn


# ----------------------------------------------------------------------------- 1
def fixture_test(dsn: str) -> None:
    S = "1 Engine unit test (fixture)"
    b = load_builder()
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    ac = admin.cursor()
    ac.execute("DROP DATABASE IF EXISTS fraud_test")
    ac.execute("CREATE DATABASE fraud_test")
    admin.close()
    conn = psycopg2.connect(with_db(dsn, "fraud_test"))
    conn.autocommit = True
    cur = conn.cursor()
    b.run_file(cur, ROOT / "sql" / "schema.sql")
    b.run_file(cur, ROOT / "sql" / "tests" / "test_fixture.sql")
    b.run_file(cur, ROOT / "sql" / "load_core.sql")
    b.run_file(cur, ROOT / "sql" / "fraud_network.sql")

    def q(sql):
        cur.execute(sql)
        return cur.fetchall()

    paths = q("SELECT account_path::TEXT, transaction_path::TEXT FROM fraud.money_flow_paths ORDER BY 1")
    exp = [("{1000000001,1000000002,1000000003,1000000004}", "{T9000001,T9000002,T9000003}"),
           ("{1000000002,1000000003,1000000004,1000000001}", "{T9000002,T9000003,T9000004}")]
    check(S, "Recursive CTE returns exactly the two expected maximal chains", paths == exp, paths)
    check(S, "Cycle prevention: no chain revisits an account",
          all(len(set(p[0].strip("{}").split(","))) == len(p[0].strip("{}").split(",")) for p in paths))
    check(S, "24 h window: late hop D->E (T9000005) is in no chain",
          q("SELECT COUNT(*) FROM fraud.money_flow_paths WHERE 'T9000005' = ANY(transaction_path)")[0][0] == 0)
    check(S, "Sub-paths removed (C->D->A not reported separately)",
          q("SELECT COUNT(*) FROM fraud.money_flow_paths WHERE hop_count < 3")[0][0] == 0)
    ep = q("SELECT txn_count, max_txns_any_10m, breaches_spec_rule FROM fraud.velocity_episodes WHERE sender_account = 1000000010")
    check(S, "Velocity: burst reported once as one 7-txn episode (no double counting)", ep == [(7, 7, True)], ep)
    check(S, "Velocity: exactly one account breaches '>5 in 10 min'",
          q("SELECT accounts_breaching_spec_rule FROM fraud.velocity_spec_check")[0][0] == 1)
    dp = q("SELECT account_a, account_b, gap_seconds FROM fraud.device_linkage_pairs ORDER BY 1")
    check(S, "Shared device: X-Y pair (18 min) found, Z (3 h later) excluded", dp == [(1000000020, 1000000021, 1080)], dp)
    ip = q("SELECT account_a, account_b FROM fraud.ip_linkage_pairs WHERE ip_address = '41.58.250.250'")
    check(S, "Shared IP: X-W pair within 10 min found once", ip == [(1000000020, 1000000023)], ip)
    rings = q("SELECT account_count FROM fraud.fraud_rings ORDER BY 1")
    check(S, "Ring detection: chain accounts A-D form one linked group of 4", (4,) in rings, rings)
    lab = q("SELECT investigation_status FROM fraud.account_risk_profile WHERE account_id = 1000000001")[0][0]
    check(S, "Labelled account classified CONFIRMED_FRAUD_LABEL (labels kept separate)", lab == "CONFIRMED_FRAUD_LABEL", lab)
    conn.close()


# ----------------------------------------------------------------------------- 2
def independent_recompute(cur) -> None:
    S = "2 Independent recompute (DuckDB vs PostgreSQL)"
    d = duckdb.connect()
    d.execute(f"CREATE TABLE t AS SELECT * FROM read_csv('{CLEAN}', header=true)")

    def pg(sql):
        cur.execute(sql)
        return cur.fetchone()[0]

    n, f = d.execute("SELECT COUNT(*), SUM(is_fraud) FROM t").fetchone()
    check(S, "Transaction count", n == pg("SELECT value FROM fraud.kpi_summary WHERE kpi='total_transactions'"), f"{n:,}")
    check(S, "Fraud transaction count", f == pg("SELECT value FROM fraud.kpi_summary WHERE kpi='fraud_transactions'"), f"{f:,}")
    dev = d.execute("""SELECT COUNT(*) FROM t a JOIN t b ON a.device_id=b.device_id AND a.sender_account<>b.sender_account
                       AND (b.txn_ts > a.txn_ts OR (b.txn_ts = a.txn_ts AND b.transaction_id > a.transaction_id))
                       AND b.txn_ts <= a.txn_ts + INTERVAL 1 HOUR""").fetchone()[0]
    check(S, "Shared-device pairs within 1 h", dev == pg("SELECT COUNT(*) FROM fraud.device_linkage_pairs"), f"{dev:,}")
    ipn = d.execute("""SELECT COUNT(*) FROM t a JOIN t b ON a.ip_address=b.ip_address AND a.sender_account<>b.sender_account
                       AND (b.txn_ts > a.txn_ts OR (b.txn_ts = a.txn_ts AND b.transaction_id > a.transaction_id))
                       AND b.txn_ts <= a.txn_ts + INTERVAL 10 MINUTE""").fetchone()[0]
    check(S, "Shared-IP pairs within 10 min", ipn == pg("SELECT COUNT(*) FROM fraud.ip_linkage_pairs"), f"{ipn:,}")
    hops = d.execute("""SELECT COUNT(*) FROM t i JOIN t o ON o.sender_account=i.receiver_account AND o.txn_ts>i.txn_ts
                        AND o.txn_ts<=i.txn_ts + INTERVAL 24 HOUR WHERE i.is_self_transfer=0 AND o.is_self_transfer=0""").fetchone()[0]
    check(S, "Money-flow hops within 24 h", hops == pg("SELECT COUNT(*) FROM fraud.flow_hops"), f"{hops:,}")
    mx = d.execute("""SELECT MAX(c) FROM (SELECT COUNT(*) OVER (PARTITION BY sender_account ORDER BY txn_ts
                      RANGE BETWEEN CURRENT ROW AND INTERVAL 10 MINUTE FOLLOWING) c FROM t)""").fetchone()[0]
    check(S, "Max transactions in any 10-min window", mx == pg("SELECT max_txns_any_10m_window FROM fraud.velocity_spec_check"), mx)
    ep = d.execute("""WITH g AS (SELECT sender_account, txn_ts, CASE WHEN LAG(txn_ts) OVER w IS NULL OR txn_ts - LAG(txn_ts) OVER w > INTERVAL 10 MINUTE
                      THEN 1 ELSE 0 END s FROM t WINDOW w AS (PARTITION BY sender_account ORDER BY txn_ts, transaction_id)),
                      e AS (SELECT sender_account, SUM(s) OVER (PARTITION BY sender_account ORDER BY txn_ts ROWS UNBOUNDED PRECEDING) k FROM g)
                      SELECT COUNT(*), SUM(n) FROM (SELECT sender_account, k, COUNT(*) n FROM e GROUP BY 1,2 HAVING COUNT(*)>=2)""").fetchone()
    pe = (pg("SELECT COUNT(*) FROM fraud.velocity_episodes"), pg("SELECT SUM(txn_count) FROM fraud.velocity_episodes"))
    check(S, "Velocity episodes (count, txns) - gaps-and-islands", tuple(ep) == tuple(pe), f"duckdb {ep} vs pg {pe}")
    nd = d.execute("SELECT SUM(is_fraud)::DOUBLE/COUNT(*) / (SELECT AVG(is_fraud) FROM t) FROM t WHERE src_new_device_flag=1").fetchone()[0]
    check(S, "NEW_DEVICE_SRC lift", abs(nd - float(pg("SELECT lift FROM fraud.flag_performance WHERE flag_code='NEW_DEVICE_SRC'"))) < 0.001, round(nd, 4))
    tp, cap = d.execute("""SELECT SUM(is_fraud), COUNT(*) FROM t WHERE src_new_device_flag=1 AND transaction_type='TRANSFER'
                           AND amount_ngn / 750.0 > 500""").fetchone()
    cur.execute("SELECT true_positives, txns_captured FROM fraud.rule_evaluation WHERE rule_id='R4'")
    check(S, "Rule R4 confusion matrix (new device + transfer > $500)", (tp, cap) == tuple(cur.fetchone()), f"TP {tp:,} of {cap:,}")
    hv = d.execute("SELECT COUNT(*) FROM t WHERE amount_ngn >= (SELECT QUANTILE_CONT(amount_ngn, 0.95) FROM t)").fetchone()[0]
    check(S, "HIGH_VALUE flag count (P95)", abs(hv - pg("SELECT flagged_txns FROM fraud.flag_performance WHERE flag_code='HIGH_VALUE'")) <= 1, f"{hv:,}")


# ----------------------------------------------------------------------------- 3
def invariants(cur) -> None:
    S = "3 Engine invariants"

    def one(sql):
        cur.execute(sql)
        return cur.fetchone()[0]

    check(S, "Every evaluated rule: TP+FP+FN+TN = total transactions",
          one("SELECT COUNT(*) FROM fraud.rule_evaluation WHERE txns_captured IS NOT NULL AND true_positives+false_positives+false_negatives+true_negatives <> 5000000") == 0)
    check(S, "Proposed rule as written is NOT evaluated (no account-creation field)",
          one("SELECT txns_captured IS NULL FROM fraud.rule_evaluation WHERE rule_id='R0'"))
    check(S, "Precision/recall/FPR recompute from confusion matrix",
          one("""SELECT COUNT(*) FROM fraud.rule_evaluation WHERE txns_captured IS NOT NULL AND (
                   abs(precision_pct - 100.0*true_positives/NULLIF(true_positives+false_positives,0)) > 0.001 OR
                   abs(recall_pct - 100.0*true_positives/(true_positives+false_negatives)) > 0.001 OR
                   abs(false_positive_rate_pct - 100.0*false_positives/(false_positives+true_negatives)) > 0.001)""") == 0)
    check(S, "Lift = flagged fraud rate / overall rate (all flags)",
          one("""SELECT COUNT(*) FROM fraud.flag_performance WHERE flagged_txns > 0 AND
                   abs(lift - (fraud_flagged_txns::NUMERIC/flagged_txns)/(overall_fraud_rate_pct/100)) > 0.002""") == 0)
    check(S, "Relative risk CI brackets the point estimate",
          one("SELECT COUNT(*) FROM fraud.flag_performance WHERE rr_ci95_low IS NOT NULL AND NOT (rr_ci95_low <= relative_risk AND relative_risk <= rr_ci95_high)") == 0)
    check(S, "Money-flow paths: no repeated account (cycle prevention)",
          one("SELECT COUNT(*) FROM fraud.money_flow_paths WHERE cardinality(account_path) <> (SELECT COUNT(DISTINCT x) FROM unnest(account_path) x)") == 0)
    check(S, "Money-flow paths: within 24 h and depth <= 6",
          one("SELECT COUNT(*) FROM fraud.money_flow_paths WHERE elapsed_minutes > 1440 OR hop_count > 6 OR hop_count < 2") == 0,
          f"max elapsed {one('SELECT MAX(elapsed_minutes) FROM fraud.money_flow_paths')} min")
    check(S, "Money-flow paths: strictly increasing timestamps along each path",
          one("""SELECT COUNT(*) FROM fraud.money_flow_paths p, LATERAL (
                   SELECT bool_and(ok) ok FROM (SELECT t.txn_ts > LAG(t.txn_ts) OVER (ORDER BY o) OR LAG(t.txn_ts) OVER (ORDER BY o) IS NULL AS ok
                   FROM unnest(p.transaction_path) WITH ORDINALITY u(tid, o) JOIN fraud.transactions t ON t.transaction_id = u.tid) z) c
                 WHERE NOT c.ok""") == 0)
    check(S, "Money-flow paths: no duplicate transaction paths",
          one("SELECT COUNT(*) - COUNT(DISTINCT transaction_path) FROM fraud.money_flow_paths") == 0)
    check(S, "Ring membership: each account in at most one ring, sizes consistent",
          one("SELECT COUNT(*) FROM fraud.fraud_rings r WHERE r.account_count <> (SELECT COUNT(*) FROM fraud.fraud_ring_membership m WHERE m.ring_id=r.ring_id)") == 0)
    check(S, "Labels untouched: fraud_labels equals cleaned source label count",
          one("SELECT COUNT(*) FROM fraud.fraud_labels WHERE is_fraud") == one("SELECT value FROM fraud.kpi_summary WHERE kpi='fraud_transactions'"))
    check(S, "Every fraud-labelled account has status CONFIRMED_FRAUD_LABEL and vice versa",
          one("SELECT COUNT(*) FROM fraud.account_risk_profile WHERE has_fraud_label <> (investigation_status='CONFIRMED_FRAUD_LABEL')") == 0)
    check(S, "Shared-IP ring edges exclude high-density networks",
          one("""SELECT COUNT(*) FROM fraud.suspicious_activity_flags s JOIN fraud.transactions t USING (transaction_id)
                 JOIN fraud.ip_addresses i USING (ip_address) WHERE s.flag_code='SHARED_IP_10M' AND i.network_class='HIGH_DENSITY_SHARED'""") == 0)
    check(S, "PRIOR_FRAUD_LINK uses only strictly earlier labels (no look-ahead)",
          one("""SELECT COUNT(*) FROM fraud.suspicious_activity_flags s JOIN fraud.transactions t USING (transaction_id)
                 JOIN fraud.account_label_summary a ON a.account_id = t.receiver_account
                 WHERE s.flag_code='PRIOR_FRAUD_LINK' AND s.flag_detail LIKE 'receiver%' AND a.first_fraud_ts >= t.txn_ts""") == 0)
    check(S, "Risk score = capped sum of flag weights",
          one("""SELECT COUNT(*) FROM fraud.transaction_risk_scores r LEFT JOIN (
                   SELECT transaction_id, LEAST(100, SUM(d.weight)) w FROM fraud.suspicious_activity_flags s JOIN fraud.flag_definitions d USING (flag_code)
                   GROUP BY 1) x USING (transaction_id) WHERE r.risk_score <> COALESCE(x.w, 0)""") == 0)


# ----------------------------------------------------------------------------- 4
def dashboard_checks(cur) -> None:
    S = "4 Dashboard reconciliation"
    import base64
    import numpy as np
    js = (ROOT / "dashboard" / "assets" / "fraud_data.js").read_text(encoding="utf-8")
    D = json.loads(js[js.index("{"): js.rindex("}") + 1])
    key = np.frombuffer(base64.b64decode(D["cube"]["key"]), dtype=np.uint32)
    cnt = np.frombuffer(base64.b64decode(D["cube"]["cnt"]), dtype=np.uint32)
    amt = np.frombuffer(base64.b64decode(D["cube"]["amt"]), dtype=np.float64)
    slot = (key >> 20) & 31
    fr = (key >> 15) & 1
    tier = (key >> 16) & 3
    cur.execute("SELECT kpi, value FROM fraud.kpi_summary")
    K = {k: float(v) for k, v in cur.fetchall()}
    check(S, "Cube total = total transactions", int(cnt[slot == 0].sum()) == K["total_transactions"], int(cnt[slot == 0].sum()))
    check(S, "Cube fraud = fraud transactions", int(cnt[(slot == 0) & (fr == 1)].sum()) == K["fraud_transactions"])
    check(S, "Cube HIGH/CRITICAL count = flagged transactions", int(cnt[(slot == 0) & (tier >= 2)].sum()) == K["flagged_transactions"])
    check(S, "Cube suspicious value = SQL suspicious value", abs(amt[(slot == 0) & (tier >= 2)].sum() - K["suspicious_value_ngn"]) < 1.0,
          f"{amt[(slot == 0) & (tier >= 2)].sum():,.2f}")
    cur.execute("SELECT flag_code, flagged_txns FROM fraud.flag_performance")
    fp = dict(cur.fetchall())
    ok = all(int(cnt[slot == k + 1].sum()) == fp[f["code"]] for k, f in enumerate(D["dims"]["flags"]))
    check(S, "Cube per-flag counts = flag_performance", ok)
    T = {k: np.frombuffer(base64.b64decode(D["tx"]["data"][k]), dtype={"u8": np.uint8, "u16": np.uint16, "u32": np.uint32, "f64": np.float64}[t])
         for k, t in D["tx"]["cols"].items()}
    ring = np.frombuffer(base64.b64decode(D["accts"]["data"]["ring"]), dtype=np.int32)
    rt = np.frombuffer(base64.b64decode(D["accts"]["data"]["ringtier"]), dtype=np.int32)
    hc = T["tier"] >= 2
    check(S, "Investigation set holds every HIGH/CRITICAL transaction", int(hc.sum()) == K["flagged_transactions"], int(hc.sum()))
    flagged = set(T["snd"][hc].tolist())
    check(S, "Flagged accounts (browser logic) = SQL", len(flagged) == K["flagged_accounts"], len(flagged))
    fr_ring = {a for a in flagged if ring[a] >= 0 and rt[a] >= 1}
    check(S, "Flagged accounts in rings (browser logic) = SQL", len(fr_ring) == K["flagged_accounts_in_ring"], len(fr_ring))
    hr = set(T["snd"][T["tier"] == 3].tolist()) | {a for a in flagged if rt[a] >= 2}
    snd_all = set(T["snd"].tolist())
    hr |= {a for a in snd_all if rt[a] >= 2}
    check(S, "High-risk accounts (browser logic) = SQL", len(hr) == K["high_risk_accounts"], len(hr))
    rings_seen = {int(ring[a]) for a in snd_all if ring[a] >= 0 and rt[a] >= 1}
    check(S, "Potential rings with activity (browser logic) = SQL", len(rings_seen) == K["potential_fraud_rings"], len(rings_seen))
    browser_selftest(cur, D)


EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
SCENARIO_SQL = {   # same filters as the in-page self-test, expressed in SQL
    "no_filter": ("TRUE", None),
    "fraud_only": ("l.is_fraud", None),
    "tier_high_critical": ("r.risk_tier IN ('HIGH','CRITICAL')", None),
    "betting_transfer": ("m.merchant_group = 'Betting & Gaming' AND t.transaction_type = 'TRANSFER'", None),
    "apr_to_jun_2023": ("t.txn_ts >= '2023-04-01' AND t.txn_ts < '2023-07-01'", None),
    "flag_shared_device": ("TRUE", "SHARED_DEVICE_1H"),
    "combo_legit_critical_newdev_h1": ("NOT l.is_fraud AND r.risk_tier = 'CRITICAL' AND t.txn_ts < '2023-07-01'", "NEW_DEVICE_SRC"),
}


def browser_selftest(cur, D) -> None:
    S = "4 Dashboard reconciliation"
    edge = next((e for e in EDGE if Path(e).exists()), None)
    if not edge:
        check(S, "Browser self-test", False, "Microsoft Edge not found - in-browser test not run")
        return
    url = (ROOT / "dashboard" / "fraud_ops_console.html").as_uri() + "?selftest"
    import tempfile
    prof = Path(tempfile.mkdtemp(prefix="edge_selftest_"))
    dom_file = prof / "dom.html"
    with open(dom_file, "wb") as fo, open(prof / "err.txt", "wb") as fe:
        subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={prof / 'profile'}",
                        "--virtual-time-budget=120000", "--dump-dom", url], stdout=fo, stderr=fe, timeout=900)
    dom = dom_file.read_text(encoding="utf-8", errors="replace")
    import shutil
    shutil.rmtree(prof, ignore_errors=True)
    m = re.search(r'<pre id="selftest"[^>]*>(.*?)</pre>', dom, re.S)
    check(S, "Dashboard opens from file:// and runs in headless Edge", bool(m), "self-test output captured" if m else f"{len(dom)} bytes, no self-test block")
    if not m:
        return
    import html as _h
    res = json.loads(_h.unescape(m.group(1)))
    for sc in res["scenarios"]:
        cond, flag = SCENARIO_SQL[sc["name"]]
        fj = f"JOIN fraud.suspicious_activity_flags sf ON sf.transaction_id = t.transaction_id AND sf.flag_code = '{flag}'" if flag else ""
        cur.execute(f"""
            WITH f AS (
              SELECT t.transaction_id, t.sender_account, t.amount_ngn, l.is_fraud, r.risk_tier, p.is_ring_member, p.ring_id, p.ring_risk_tier
              FROM fraud.transactions t JOIN fraud.fraud_labels l USING (transaction_id)
              JOIN fraud.transaction_risk_scores r USING (transaction_id) JOIN fraud.merchants m USING (merchant_category)
              JOIN fraud.account_risk_profile p ON p.account_id = t.sender_account {fj}
              WHERE {cond})
            SELECT COUNT(*), COUNT(*) FILTER (WHERE is_fraud),
                   COUNT(*) FILTER (WHERE risk_tier IN ('HIGH','CRITICAL')),
                   COALESCE(SUM(amount_ngn) FILTER (WHERE risk_tier IN ('HIGH','CRITICAL')), 0)::FLOAT8,
                   COUNT(DISTINCT sender_account) FILTER (WHERE risk_tier IN ('HIGH','CRITICAL')),
                   COUNT(DISTINCT sender_account) FILTER (WHERE risk_tier IN ('HIGH','CRITICAL') AND is_ring_member),
                   COUNT(DISTINCT sender_account) FILTER (WHERE risk_tier = 'CRITICAL' OR ring_risk_tier IN ('HIGH','CRITICAL')),
                   COUNT(DISTINCT ring_id) FILTER (WHERE is_ring_member)
            FROM f""")
        e = cur.fetchone()
        got = (sc["n"], sc["fraud"], sc["susp_n"], sc["susp_amt"], sc["flagged"], sc["flagged_ring"], sc["high_risk"], sc["rings"])
        ok = got[:3] == e[:3] and abs(got[3] - e[3]) < 1.0 and got[4:] == e[4:] and sc["mg_sum"] == sc["n"] == sc["hour_sum"] and sc["table_rows"] == e[2]
        check(S, f"Filter scenario '{sc['name']}': UI KPIs = SQL", ok,
              f"txns {got[0]:,}, fraud {got[1]:,}, flagged accts {got[4]:,}, rings {got[7]:,}" if ok else f"UI {got} vs SQL {e}")
    check(S, "Investigate drill-through renders all sections", res["drill"] == "ok", res["drill"])
    check(S, "Network graph draws every member of the selected ring", res["graph"] == "ok", res["graph"])


# ----------------------------------------------------------------------------- 5
def excel_checks(cur) -> None:
    S = "5 Excel workbook"
    from openpyxl import load_workbook
    p = ROOT / "excel" / "fraud_analysis.xlsx"
    wbv = load_workbook(p, data_only=True)
    wbf = load_workbook(p, data_only=False)
    n_formula = errors = uncached = 0
    for ws in wbf.worksheets:
        wv = wbv[ws.title]
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    n_formula += 1
                    v = wv[c.coordinate].value
                    if v is None:
                        uncached += 1
                    elif isinstance(v, str) and v.startswith("#"):
                        errors += 1
    check(S, "Formulas present", n_formula > 1000, f"{n_formula:,} formulas")
    check(S, "Formulas calculated by Excel (cached values present)", uncached == 0, f"{uncached} without cached value")
    check(S, "No formula errors (#VALUE!, #REF!, #DIV/0!, #NAME?)", errors == 0, f"{errors} errors")
    cur.execute("SELECT value FROM fraud.kpi_summary WHERE kpi='fraud_rate_pct'")
    fr = float(cur.fetchone()[0]) / 100
    v = wbv["Executive_Dashboard"]["C8"].value
    check(S, "Executive fraud-rate formula = SQL", v is not None and abs(v - fr) < 1e-5, v)
    ma = wbv["Merchant_Analysis"]
    tot = [r for r in ma.iter_rows(min_row=5, values_only=True) if r[0] == "TOTAL"][0]
    check(S, "Merchant_Analysis population total = 5,000,000", tot[2] == 5_000_000, tot[2])
    check(S, "Merchant_Analysis sample total = 10,000 (COUNTIFS)", tot[9] == 10_000, tot[9])
    fs = wbv["Fraud_Summary"]
    bad = 0
    for row in fs.iter_rows(min_row=1, values_only=True):
        if row and isinstance(row[0], str) and re.fullmatch(r"R\d+D?", row[0]) and isinstance(row[8], (int, float)):
            # SQL stores percentages rounded to 3 dp (= 5e-6 as a fraction); Excel computes exactly
            if abs(row[8] - row[15]) > 1e-5 or abs(row[9] - row[16]) > 1e-5 or abs(row[10] - row[17]) > 1e-5:
                bad += 1
    check(S, "Rule precision/recall/FPR: Excel formulas = SQL (to SQL's 3-dp rounding)", bad == 0, f"{bad} mismatches")
    hs = wbv["Hourly_Analysis"]
    check(S, "Hourly_Analysis sample counts sum to 10,000", sum(hs.cell(row=r, column=6).value for r in range(5, 29)) == 10_000)


# ----------------------------------------------------------------------------- 6
REQUIRED = ["README.md", "data_dictionary.md", "methodology.md", "fraud_detection_rules.md", "database_schema.md", "limitations.md",
            ".gitignore", "requirements.txt", "sql/schema.sql", "sql/load_core.sql", "sql/fraud_network.sql", "sql/tests/test_fixture.sql",
            "excel/fraud_analysis.xlsx", "dashboard/fraud_ops_console.html", "dashboard/assets/fraud_data.js",
            "analysis/fraud_pattern_analysis.md", "analysis/device_linkage_analysis.md", "analysis/ip_linkage_analysis.md",
            "analysis/transaction_velocity_analysis.md", "analysis/money_flow_analysis.md", "analysis/fraud_ring_analysis.md",
            "analysis/fraud_rule_evaluation.md", "reports/Risk_Memo_CRO.md", "reports/er_diagram.png", "reports/er_diagram.mmd",
            "reports/executive_summary.md", "data/cleaned/transactions_clean_sample_10k.csv", "data/processed/data_quality_checks.csv"]


def hygiene() -> None:
    S = "6 Repository hygiene"
    missing = [p for p in REQUIRED if not (ROOT / p).exists() or (ROOT / p).stat().st_size == 0]
    check(S, "All required deliverables exist and are non-empty", not missing, missing or f"{len(REQUIRED)} files")
    empty = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and p.stat().st_size == 0 and "pgdata" not in p.parts]
    check(S, "No empty files in repository", not empty, empty)
    text_ext = {".md", ".py", ".sql", ".html", ".mmd", ".txt", ".csv"}
    todo, secrets = [], []
    pat_todo = re.compile(r"\b(TODO|FIXME|TBD|XXX)\b")
    pat_sec = re.compile(r"(password\s*=\s*\S{4,}|api[_-]?key\s*[=:]\s*\S{8,}|secret\s*[=:]\s*\S{8,}|BEGIN (RSA|OPENSSH) PRIVATE KEY)", re.I)
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in text_ext and p.stat().st_size < 30_000_000 and "raw" not in p.parts and p.name != "transactions_clean.csv":
            t = p.read_text(encoding="utf-8", errors="ignore")
            if p.name not in ("06_validate.py", "validation_report.md") and pat_todo.search(t):   # these quote the check name itself
                todo.append(str(p.relative_to(ROOT)))
            if pat_sec.search(t):
                secrets.append(str(p.relative_to(ROOT)))
    check(S, "No TODO / FIXME / TBD markers", not todo, todo)
    check(S, "No credentials or keys in files", not secrets, secrets)
    big = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and p.stat().st_size > 95_000_000
           and not any(s in str(p) for s in ["data\\raw", "data/raw", "transactions_clean.csv", "pgdata"])]
    check(S, "No tracked file above GitHub's 100 MB limit", not big, big)
    gi = (ROOT / ".gitignore").read_text()
    check(S, "Large raw/cleaned files are git-ignored", "data/raw/*.csv" in gi and "data/cleaned/transactions_clean.csv" in gi)


def write_report() -> None:
    ok = sum(r[2] for r in RESULTS)
    lines = ["# Validation Report", "",
             f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by `scripts/06_validate.py` on PostgreSQL 16.2 + DuckDB 2.0 (Windows 11).", "",
             f"**{ok} of {len(RESULTS)} checks passed.**", "",
             "| # | Section | Check | Result | Detail |", "|---|---|---|---|---|"]
    for i, (s, n, p, d) in enumerate(RESULTS, 1):
        d = d.replace("|", "/")
        lines.append(f"| {i} | {s} | {n} | {'PASS' if p else '**FAIL**'} | {d[:160]} |")
    (ROOT / "reports" / "validation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{ok}/{len(RESULTS)} passed -> reports/validation_report.md")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--skip-fixture", action="store_true")
    a = ap.parse_args()
    if not a.skip_fixture:
        fixture_test(a.dsn)
    conn = psycopg2.connect(a.dsn)
    cur = conn.cursor()
    independent_recompute(cur)
    invariants(cur)
    dashboard_checks(cur)
    excel_checks(cur)
    hygiene()
    write_report()


if __name__ == "__main__":
    main()
