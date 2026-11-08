"""
03_build_excel.py
-----------------------------------------------------------------------------
Builds excel/fraud_analysis.xlsx from the prepared data and the PostgreSQL
engine outputs.

Two kinds of numbers appear in the workbook, always labelled:
  * POPULATION (SQL)  - computed by PostgreSQL over all 5,000,000 transactions,
                        written as values (Excel cannot hold 5M rows).
  * SAMPLE (formula)  - live Excel formulas (COUNTIFS / SUMIFS / AVERAGEIFS / IF /
                        IFERROR) over the 10,000-row random sample on
                        Cleaned_Transactions, so the workbook's logic is auditable.

Run: python scripts/03_build_excel.py [--dsn ...]
-----------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import psycopg2
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv"
SAMPLE = ROOT / "data" / "cleaned" / "transactions_clean_sample_10k.csv"
OUT = ROOT / "excel" / "fraud_analysis.xlsx"

NAVY, NAVY2, GREY, WHITE = "0B2545", "13315C", "F2F4F8", "FFFFFF"
HFONT = Font(bold=True, color=WHITE)
HFILL = PatternFill("solid", fgColor=NAVY)
SUBFILL = PatternFill("solid", fgColor="DCE4F0")
POPFILL = PatternFill("solid", fgColor="E8F1FB")
SAMPFILL = PatternFill("solid", fgColor="FFF4E0")
THIN = Side(style="thin", color="C9D2E0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TITLE = Font(bold=True, size=15, color=NAVY)
NOTE = Font(italic=True, size=9, color="5A6577")


def connect(dsn):
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        import pgserver
        dsn = pgserver.get_server(str(ROOT / "data" / "pgdata"), cleanup_mode=None).get_uri()
    return psycopg2.connect(dsn)


def header(ws, row, col, names, fill=HFILL, font=HFONT):
    for j, n in enumerate(names):
        c = ws.cell(row=row, column=col + j, value=n)
        c.fill, c.font, c.border = fill, font, BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def title(ws, text, sub=None):
    ws["A1"] = text
    ws["A1"].font = TITLE
    if sub:
        ws["A2"] = sub
        ws["A2"].font = NOTE
    ws.sheet_view.showGridLines = False


def widths(ws, w):
    for i, x in enumerate(w, start=1):
        ws.column_dimensions[get_column_letter(i)].width = x


def box(ws, r1, c1, r2, c2):
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            ws.cell(row=r, column=c).border = BORDER


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    a = ap.parse_args()
    conn = connect(a.dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO fraud, public; SET work_mem='256MB';")

    def q(sql):
        cur.execute(sql)
        return cur.fetchall()

    wb = Workbook()

    # ------------------------------------------------------------ 1 Raw_Transactions
    ws = wb.active
    ws.title = "Raw_Transactions"
    with open(RAW, newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        hdr = next(rd)
        raw_rows = [next(rd) for _ in range(2000)]
    title(ws, "Raw_Transactions - first 2,000 rows of the source file, unmodified (all values as text)",
          "Source: V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv (5,000,000 rows, Hugging Face, GPL). "
          "The raw file is never edited; see data/processed/data_quality_checks.csv for validation results.")
    header(ws, 4, 1, hdr)
    for i, r in enumerate(raw_rows, start=5):
        for j, v in enumerate(r, start=1):
            ws.cell(row=i, column=j, value=v)
    ws.freeze_panes = "A5"
    widths(ws, [11, 26, 13, 13, 12, 26, 13, 9, 8, 16, 12, 10, 8, 9, 14, 16, 11, 13, 9, 11, 13])

    # ------------------------------------------------------------ 2 Cleaned_Transactions
    with open(SAMPLE, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        sample = list(rd)
    ids = [r["transaction_id"] for r in sample]
    cur.execute("CREATE TEMP TABLE xl_ids (transaction_id TEXT PRIMARY KEY)")
    cur.executemany("INSERT INTO xl_ids VALUES (%s)", [(i,) for i in ids])
    extra = {r[0]: r[1:] for r in q("""
        SELECT x.transaction_id, r.risk_score, r.risk_tier, COALESCE(r.flag_list, ''), f.cnt_10m_back, f.cnt_24h_back, f.sender_seq
        FROM xl_ids x JOIN transaction_risk_scores r USING (transaction_id) JOIN txn_features f USING (transaction_id)""")}
    ws = wb.create_sheet("Cleaned_Transactions")
    title(ws, "Cleaned_Transactions - 10,000-row random sample of the cleaned dataset (seed 42) + engine outputs",
          "Formula sheets (Merchant_Analysis, Hourly_Analysis, Behavioral_Flags, Fraud_Summary) reference this table. "
          "is_fraud = SOURCE label. risk_score / risk_tier / flag_list = RULE outputs from sql/fraud_network.sql.")
    cols = ["transaction_id", "txn_ts", "sender_account", "receiver_account", "transaction_type", "merchant_category",
            "merchant_group", "location", "device_channel", "payment_channel", "device_id", "ip_address", "amount_ngn",
            "is_fraud", "fraud_type", "src_new_device_flag", "bvn_linked", "sender_persona", "txn_hour",
            "risk_score", "risk_tier", "flag_list", "txns_trailing_10m", "txns_trailing_24h", "sender_txn_seq", "amount_usd_est"]
    header(ws, 4, 1, cols)
    for i, r in enumerate(sample, start=5):
        e = extra[r["transaction_id"]]
        vals = [r["transaction_id"], r["txn_ts"][:19], int(r["sender_account"]), int(r["receiver_account"]), r["transaction_type"],
                r["merchant_category"], r["merchant_group"], r["location"], r["device_channel"], r["payment_channel"],
                r["device_id"], r["ip_address"], float(r["amount_ngn"]), int(r["is_fraud"]), r["fraud_type"] or None,
                int(r["src_new_device_flag"]), int(r["bvn_linked"]), r["sender_persona"], int(r["txn_hour"]),
                int(e[0]), e[1], e[2], int(e[3]), int(e[4]), int(e[5])]
        for j, v in enumerate(vals, start=1):
            ws.cell(row=i, column=j, value=v)
        ws.cell(row=i, column=26, value=f"=M{i}/Executive_Dashboard!$C$27")     # NGN -> USD with the documented FX assumption
        ws.cell(row=i, column=13).number_format = "#,##0.00"
        ws.cell(row=i, column=26).number_format = "#,##0.00"
    n_last = 4 + len(sample)
    ws.freeze_panes = "B5"
    widths(ws, [11, 19, 13, 13, 12, 24, 18, 12, 9, 13, 10, 15, 14, 8, 15, 9, 8, 12, 8, 9, 10, 40, 9, 9, 9, 12])
    ws.conditional_formatting.add(f"N5:N{n_last}", CellIsRule(operator="equal", formula=["1"], fill=PatternFill("solid", fgColor="F8D0D0")))
    ws.conditional_formatting.add(f"U5:U{n_last}", CellIsRule(operator="equal", formula=['"CRITICAL"'], fill=PatternFill("solid", fgColor="F4B6B6")))
    ws.conditional_formatting.add(f"U5:U{n_last}", CellIsRule(operator="equal", formula=['"HIGH"'], fill=PatternFill("solid", fgColor="F9D7C3")))
    rng = lambda c: f"Cleaned_Transactions!${c}$5:${c}${n_last}"
    AMT, FR, MC, MG, HR, TY, TIER, FL, V24, SC = rng("M"), rng("N"), rng("F"), rng("G"), rng("S"), rng("E"), rng("U"), rng("V"), rng("X"), rng("T")

    # ------------------------------------------------------------ 3 Data_Dictionary
    ws = wb.create_sheet("Data_Dictionary")
    title(ws, "Data_Dictionary - cleaned transaction fields", "Full documentation: data_dictionary.md")
    dd = [
        ("transaction_id", "VARCHAR(12)", "Source", "Unique transaction id (T + digits). PK of transactions."),
        ("txn_ts", "TIMESTAMP", "Source (timestamp)", "Transaction time, microsecond precision, 2023-01-01 to 2024-01-28. No time zone in source."),
        ("sender_account", "BIGINT", "Source", "10-digit originating account. FK -> accounts."),
        ("receiver_account", "BIGINT", "Source", "10-digit beneficiary account. FK -> accounts. Enables money-flow analysis."),
        ("transaction_type", "VARCHAR", "Source, upper-cased", "DEPOSIT / PAYMENT / TRANSFER / WITHDRAWAL."),
        ("merchant_category", "VARCHAR", "Source", "Merchant / biller name (21 values)."),
        ("merchant_group", "VARCHAR", "Derived mapping", "11 analytical groups (e.g. Betting & Gaming, Cash Access)."),
        ("location", "VARCHAR", "Source", "Nigerian city (10 values)."),
        ("device_channel", "VARCHAR", "Source device_used, upper-cased", "ATM / MOBILE / POS / WEB."),
        ("payment_channel", "VARCHAR", "Source, normalised", "BANK_TRANSFER / CARD / MOBILE_APP / USSD."),
        ("device_id", "VARCHAR(8)", "Source device_hash, trimmed + upper", "Device fingerprint D + 7 digits. Near-unique per transaction (see device analysis)."),
        ("ip_address", "INET", "Source, octets validated", "IPv4. Four Nigerian carrier ranges; 102.89.x.x behaves like carrier NAT."),
        ("amount_ngn", "NUMERIC(14,2)", "Source amount_ngn", "Amount in Nigerian naira. All > 0."),
        ("is_fraud", "0/1", "Source label", "GROUND-TRUTH label published with the dataset. Never altered."),
        ("fraud_type", "VARCHAR", "Source", "Account Takeover / Identity Fraud when is_fraud = 1, else blank."),
        ("src_new_device_flag", "0/1", "Source new_device_transaction", "Publisher-provided flag. Cannot be reproduced from device_id history."),
        ("bvn_linked", "0/1", "Source", "Bank Verification Number linked."),
        ("sender_persona", "VARCHAR", "Source", "Student / Trader / Salary Earner (constant per account)."),
        ("txn_hour", "0-23", "Derived from txn_ts", "Hour of day."),
        ("risk_score", "0-100", "RULE output", "Sum of flag weights (sql/fraud_network.sql E2). Not a probability."),
        ("risk_tier", "TEXT", "RULE output", "LOW < 25, MEDIUM 25-44, HIGH 45-64, CRITICAL >= 65."),
        ("flag_list", "TEXT", "RULE output", "Pipe-separated weighted behavioural flags."),
        ("txns_trailing_10m / 24h", "INT", "Derived (window functions)", "Sender's transactions in the trailing window including this one."),
        ("sender_txn_seq", "INT", "Derived (ROW_NUMBER)", "Order of this transaction in the sender's history."),
        ("amount_usd_est", "Formula", "Derived in Excel", "amount_ngn / FX assumption on Executive_Dashboard!C27 (NGN 750 per USD)."),
    ]
    header(ws, 4, 1, ["Field", "Type", "Origin", "Description"])
    for i, r in enumerate(dd, start=5):
        for j, v in enumerate(r, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.border = BORDER
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if "RULE" in r[2]:
            for j in range(1, 5):
                ws.cell(row=i, column=j).fill = SAMPFILL
        if r[2] == "Source label":
            for j in range(1, 5):
                ws.cell(row=i, column=j).fill = PatternFill("solid", fgColor="F8D0D0")
    widths(ws, [26, 15, 30, 95])

    # ------------------------------------------------------------ 4 Fraud_Sample
    ws = wb.create_sheet("Fraud_Sample")
    title(ws, "Fraud_Sample - representative random sample of 1,000 HIGH/CRITICAL rule-tier transactions (population)",
          "Sampled with SQL ORDER BY md5(transaction_id). Shows how often rule-flagged transactions actually carry the source fraud label.")
    fs = q("""
        SELECT t.transaction_id, t.txn_ts::TEXT, t.sender_account, t.receiver_account, t.transaction_type, t.merchant_category,
               t.amount_ngn::FLOAT8, t.device_id, host(t.ip_address), r.risk_score, r.risk_tier, r.flag_list, l.is_fraud::INT, COALESCE(l.fraud_type, '')
        FROM transactions t JOIN transaction_risk_scores r USING (transaction_id) JOIN fraud_labels l USING (transaction_id)
        WHERE r.risk_tier IN ('HIGH','CRITICAL')
        ORDER BY md5(t.transaction_id) LIMIT 1000""")
    fcols = ["transaction_id", "txn_ts", "sender_account", "receiver_account", "type", "merchant_category", "amount_ngn", "device_id",
             "ip_address", "risk_score", "risk_tier", "flag_list", "is_fraud (label)", "fraud_type", "Outcome (formula)", "amount_usd_est"]
    header(ws, 4, 1, fcols)
    for i, r in enumerate(fs, start=5):
        for j, v in enumerate(r, start=1):
            ws.cell(row=i, column=j, value=v)
        ws.cell(row=i, column=15, value=f'=IF(M{i}=1,"True positive (label = fraud)","False positive (no label)")')
        ws.cell(row=i, column=16, value=f"=G{i}/Executive_Dashboard!$C$27")
        ws.cell(row=i, column=7).number_format = "#,##0.00"
        ws.cell(row=i, column=16).number_format = "#,##0.00"
    fl = 4 + len(fs)
    ws.freeze_panes = "B5"
    widths(ws, [11, 26, 13, 13, 11, 24, 14, 10, 15, 8, 10, 46, 9, 16, 28, 12])
    ws.conditional_formatting.add(f"O5:O{fl}", FormulaRule(formula=[f'$M5=1'], fill=PatternFill("solid", fgColor="F8D0D0")))
    ws.conditional_formatting.add(f"J5:J{fl}", DataBarRule(start_type="num", start_value=0, end_type="num", end_value=100, color="5B8FD9"))
    ws["R4"], ws["S4"] = "Sample summary", "Value"
    header(ws, 4, 18, ["Sample summary", "Value"])
    summ = [("Transactions in sample", f"=COUNTA(A5:A{fl})"),
            ("Labelled fraud (true positives)", f"=COUNTIFS(M5:M{fl},1)"),
            ("No fraud label (false positives)", f"=COUNTIFS(M5:M{fl},0)"),
            ("Precision in sample", f"=IFERROR(S6/S5,0)"),
            ("CRITICAL tier in sample", f'=COUNTIFS(K5:K{fl},"CRITICAL")'),
            ("Precision - CRITICAL only", f'=IFERROR(COUNTIFS(K5:K{fl},"CRITICAL",M5:M{fl},1)/S9,0)'),
            ("Average amount (NGN)", f"=AVERAGE(G5:G{fl})"),
            ("Average amount - labelled fraud", f"=IFERROR(AVERAGEIFS(G5:G{fl},M5:M{fl},1),0)")]
    for i, (k, v) in enumerate(summ, start=5):
        ws.cell(row=i, column=18, value=k).border = BORDER
        c = ws.cell(row=i, column=19, value=v)
        c.border = BORDER
        c.number_format = "0.00%" if "Precision" in k else "#,##0"
    ws.column_dimensions["R"].width = 34
    ws.column_dimensions["S"].width = 14

    # ------------------------------------------------------------ 5 Merchant_Analysis
    ws = wb.create_sheet("Merchant_Analysis")
    title(ws, "Merchant_Analysis - fraud rate by merchant category",
          "Blue columns = POPULATION (SQL, 5,000,000 txns, values). Orange columns = SAMPLE formulas over Cleaned_Transactions (10,000 txns).")
    pop = q("""SELECT m.merchant_group, t.merchant_category, COUNT(*), SUM(l.is_fraud::INT), SUM(t.amount_ngn)::FLOAT8,
                      AVG(t.amount_ngn) FILTER (WHERE l.is_fraud)::FLOAT8, AVG(t.amount_ngn) FILTER (WHERE NOT l.is_fraud)::FLOAT8,
                      COUNT(*) FILTER (WHERE r.risk_tier IN ('HIGH','CRITICAL'))
               FROM transactions t JOIN merchants m USING (merchant_category) JOIN fraud_labels l USING (transaction_id)
               JOIN transaction_risk_scores r USING (transaction_id)
               GROUP BY 1, 2 ORDER BY 1, 2""")
    mh = ["Merchant group", "Merchant category", "Txns (pop)", "Fraud txns (pop)", "Fraud rate (pop)", "Value NGN (pop)",
          "Avg fraud amt (pop)", "Avg legit amt (pop)", "HIGH/CRIT txns (pop)", "Txns (sample)", "Fraud (sample)", "Fraud rate (sample)",
          "Avg amount (sample)", "Rate vs overall (sample)", "Signal?"]
    header(ws, 4, 1, mh)
    for c in range(3, 10):
        ws.cell(row=4, column=c).fill = PatternFill("solid", fgColor="1F5A9E")
    for c in range(10, 16):
        ws.cell(row=4, column=c).fill = PatternFill("solid", fgColor="B5651D")
    for i, r in enumerate(pop, start=5):
        ws.cell(row=i, column=1, value=r[0]); ws.cell(row=i, column=2, value=r[1])
        ws.cell(row=i, column=3, value=r[2]); ws.cell(row=i, column=4, value=r[3])
        ws.cell(row=i, column=5, value=f"=IFERROR(D{i}/C{i},0)")
        ws.cell(row=i, column=6, value=r[4]); ws.cell(row=i, column=7, value=r[5]); ws.cell(row=i, column=8, value=r[6]); ws.cell(row=i, column=9, value=r[7])
        ws.cell(row=i, column=10, value=f"=COUNTIFS({MC},B{i})")
        ws.cell(row=i, column=11, value=f"=COUNTIFS({MC},B{i},{FR},1)")
        ws.cell(row=i, column=12, value=f"=IFERROR(K{i}/J{i},0)")
        ws.cell(row=i, column=13, value=f"=IFERROR(AVERAGEIFS({AMT},{MC},B{i}),0)")
        ws.cell(row=i, column=14, value=f"=IFERROR(L{i}/$L${5 + len(pop)},0)")
        ws.cell(row=i, column=15, value=f'=IF(ABS(E{i}/$E${5 + len(pop)}-1)>0.1,"Possible signal","No material difference")')
        for c, fmt in [(3, "#,##0"), (4, "#,##0"), (5, "0.000%"), (6, "#,##0"), (7, "#,##0"), (8, "#,##0"), (9, "#,##0"), (10, "#,##0"),
                       (11, "#,##0"), (12, "0.00%"), (13, "#,##0"), (14, "0.00")]:
            ws.cell(row=i, column=c).number_format = fmt
    tr = 5 + len(pop)
    ws.cell(row=tr, column=1, value="TOTAL").font = Font(bold=True)
    for c, f in [(3, f"=SUM(C5:C{tr - 1})"), (4, f"=SUM(D5:D{tr - 1})"), (5, f"=IFERROR(D{tr}/C{tr},0)"), (6, f"=SUM(F5:F{tr - 1})"),
                 (9, f"=SUM(I5:I{tr - 1})"), (10, f"=SUM(J5:J{tr - 1})"), (11, f"=SUM(K5:K{tr - 1})"), (12, f"=IFERROR(K{tr}/J{tr},0)"),
                 (13, f"=AVERAGE({AMT})")]:
        cc = ws.cell(row=tr, column=c, value=f)
        cc.font = Font(bold=True)
        cc.number_format = {5: "0.000%", 12: "0.00%"}.get(c, "#,##0")
    box(ws, 4, 1, tr, 15)
    ws.conditional_formatting.add(f"E5:E{tr - 1}", ColorScaleRule(start_type="min", start_color="F7FBFF", end_type="max", end_color="D03B3B"))
    ws.conditional_formatting.add(f"L5:L{tr - 1}", ColorScaleRule(start_type="min", start_color="F7FBFF", end_type="max", end_color="D03B3B"))
    widths(ws, [20, 26, 12, 12, 11, 18, 14, 14, 12, 11, 10, 11, 13, 12, 22])
    ch = BarChart(); ch.type = "bar"; ch.style = 10
    ch.title = "Fraud rate by merchant category (population)"; ch.y_axis.title = "Fraud rate"; ch.y_axis.numFmt = "0.0%"
    ch.add_data(Reference(ws, min_col=5, min_row=4, max_row=tr - 1), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=2, min_row=5, max_row=tr - 1))
    ch.height, ch.width, ch.legend = 12, 20, None
    ws.add_chart(ch, f"B{tr + 3}")

    # ------------------------------------------------------------ 6 Hourly_Analysis
    ws = wb.create_sheet("Hourly_Analysis")
    title(ws, "Hourly_Analysis - fraud rate by hour of day",
          "Blue = POPULATION (SQL). Orange = SAMPLE formulas. Volume is ~4x higher 09:00-17:59, but the fraud RATE is flat across hours.")
    hp = q("""SELECT t.txn_hour, COUNT(*), SUM(l.is_fraud::INT), AVG(t.amount_ngn)::FLOAT8
              FROM transactions t JOIN fraud_labels l USING (transaction_id) GROUP BY 1 ORDER BY 1""")
    header(ws, 4, 1, ["Hour", "Txns (pop)", "Fraud (pop)", "Fraud rate (pop)", "Avg amount (pop)", "Txns (sample)", "Fraud (sample)",
                      "Fraud rate (sample)", "Share of daily volume (pop)", "Night window?"])
    for i, r in enumerate(hp, start=5):
        ws.cell(row=i, column=1, value=r[0]); ws.cell(row=i, column=2, value=r[1]); ws.cell(row=i, column=3, value=r[2])
        ws.cell(row=i, column=4, value=f"=IFERROR(C{i}/B{i},0)"); ws.cell(row=i, column=5, value=r[3])
        ws.cell(row=i, column=6, value=f"=COUNTIFS({HR},A{i})"); ws.cell(row=i, column=7, value=f"=COUNTIFS({HR},A{i},{FR},1)")
        ws.cell(row=i, column=8, value=f"=IFERROR(G{i}/F{i},0)"); ws.cell(row=i, column=9, value=f"=B{i}/SUM($B$5:$B$28)")
        ws.cell(row=i, column=10, value=f'=IF(A{i}<=4,"00:00-04:59 (UNUSUAL_HOUR flag)","Not flagged")')
        for c, fmt in [(2, "#,##0"), (3, "#,##0"), (4, "0.000%"), (5, "#,##0"), (6, "#,##0"), (7, "#,##0"), (8, "0.00%"), (9, "0.0%")]:
            ws.cell(row=i, column=c).number_format = fmt
    box(ws, 4, 1, 28, 10)
    ws.conditional_formatting.add("D5:D28", ColorScaleRule(start_type="min", start_color="F7FBFF", end_type="max", end_color="D03B3B"))
    ws.conditional_formatting.add("B5:B28", DataBarRule(start_type="min", end_type="max", color="5B8FD9"))
    widths(ws, [8, 12, 11, 13, 15, 12, 12, 14, 15, 30])
    lc = LineChart(); lc.title = "Fraud rate by hour (population)"; lc.y_axis.numFmt = "0.0%"; lc.y_axis.title = "Fraud rate"; lc.x_axis.title = "Hour"
    lc.add_data(Reference(ws, min_col=4, min_row=4, max_row=28), titles_from_data=True)
    lc.set_categories(Reference(ws, min_col=1, min_row=5, max_row=28)); lc.height, lc.width, lc.legend = 9, 18, None
    lc.y_axis.scaling.min = 0
    ws.add_chart(lc, "L4")
    bc = BarChart(); bc.title = "Transaction volume by hour (population)"; bc.y_axis.title = "Transactions"
    bc.add_data(Reference(ws, min_col=2, min_row=4, max_row=28), titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=1, min_row=5, max_row=28)); bc.height, bc.width, bc.legend = 9, 18, None
    ws.add_chart(bc, "L23")

    # ------------------------------------------------------------ 7 Behavioral_Flags
    ws = wb.create_sheet("Behavioral_Flags")
    title(ws, "Behavioral_Flags - flag frequency, fraud rate, lift and relative risk",
          "Population metrics from fraud.flag_performance (SQL). Sample columns use COUNTIFS with wildcards on flag_list. "
          "Lift = fraud rate among flagged / overall rate. Association only - not causation.")
    fp = q("""SELECT flag_code, flag_family, weight, data_basis, flagged_txns, fraud_flagged_txns, pct_of_all_txns, fraud_rate_flagged_pct,
                     fraud_rate_unflagged_pct, overall_fraud_rate_pct, lift, relative_risk, rr_ci95_low, rr_ci95_high, recall_pct
              FROM flag_performance ORDER BY relative_risk DESC NULLS LAST""")
    header(ws, 4, 1, ["Flag", "Family", "Weight", "Basis", "Flagged txns (pop)", "Fraud among flagged (pop)", "% of all txns",
                      "Fraud rate flagged", "Fraud rate unflagged", "Overall rate", "Lift", "Relative risk", "RR 95% CI low",
                      "RR 95% CI high", "Recall (share of all fraud)", "Flagged (sample)", "Fraud flagged (sample)", "Fraud rate (sample)", "Verdict"])
    for i, r in enumerate(fp, start=5):
        vals = [r[0], r[1], r[2], r[3], r[4], r[5], float(r[6]) / 100, None if r[7] is None else float(r[7]) / 100,
                float(r[8]) / 100, float(r[9]) / 100, None if r[10] is None else float(r[10]), None if r[11] is None else float(r[11]),
                None if r[12] is None else float(r[12]), None if r[13] is None else float(r[13]), float(r[14]) / 100]
        for j, v in enumerate(vals, start=1):
            ws.cell(row=i, column=j, value=v)
        weighted = r[2] > 0
        ws.cell(row=i, column=16, value=f'=COUNTIFS({FL},"*{r[0]}*")' if weighted else "n/a (weight 0, not in flag_list)")
        ws.cell(row=i, column=17, value=f'=COUNTIFS({FL},"*{r[0]}*",{FR},1)' if weighted else "")
        ws.cell(row=i, column=18, value=f"=IFERROR(Q{i}/P{i},0)" if weighted else "")
        ws.cell(row=i, column=19, value=f'=IF(M{i}>1,"Positive association",IF(N{i}<1,"Negative association","No significant association"))')
        for c, fmt in [(5, "#,##0"), (6, "#,##0"), (7, "0.00%"), (8, "0.000%"), (9, "0.000%"), (10, "0.000%"), (11, "0.000"), (12, "0.000"),
                       (13, "0.000"), (14, "0.000"), (15, "0.00%"), (16, "#,##0"), (17, "#,##0"), (18, "0.00%")]:
            ws.cell(row=i, column=c).number_format = fmt
    fe = 4 + len(fp)
    box(ws, 4, 1, fe, 19)
    ws.conditional_formatting.add(f"L5:L{fe}", ColorScaleRule(start_type="num", start_value=0.8, start_color="5B8FD9", mid_type="num", mid_value=1,
                                                             mid_color="F7F7F7", end_type="num", end_value=1.3, end_color="D03B3B"))
    ws.conditional_formatting.add(f"S5:S{fe}", CellIsRule(operator="equal", formula=['"Positive association"'], fill=PatternFill("solid", fgColor="F8D0D0")))
    widths(ws, [24, 11, 7, 13, 13, 13, 9, 10, 10, 9, 8, 9, 9, 9, 11, 12, 12, 11, 26])
    bc = BarChart(); bc.type = "bar"; bc.title = "Lift by behavioural flag (1.0 = no better than chance)"; bc.y_axis.title = "Lift"
    bc.add_data(Reference(ws, min_col=11, min_row=4, max_row=fe), titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=1, min_row=5, max_row=fe)); bc.height, bc.width, bc.legend = 10, 20, None
    ws.add_chart(bc, f"A{fe + 3}")
    tp = q("SELECT risk_tier, min_score, max_score, txns, pct_of_txns, fraud_txns, fraud_rate_pct, lift, cumulative_recall_pct, cumulative_review_pct FROM tier_performance ORDER BY min_score DESC")
    r0 = fe + 25
    ws.cell(row=r0 - 1, column=1, value="Rule-tier performance (population)").font = Font(bold=True, color=NAVY)
    header(ws, r0, 1, ["Tier", "Min score", "Max score", "Txns", "% of txns", "Fraud txns", "Fraud rate", "Lift", "Cumulative recall", "Cumulative review %"])
    for i, r in enumerate(tp, start=r0 + 1):
        vals = [r[0], r[1], r[2], r[3], float(r[4]) / 100, r[5], float(r[6]) / 100, float(r[7]), float(r[8]) / 100, float(r[9]) / 100]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.border = BORDER
            c.number_format = {4: "#,##0", 5: "0.00%", 6: "#,##0", 7: "0.000%", 8: "0.000", 9: "0.00%", 10: "0.00%"}.get(j, "General")

    # ------------------------------------------------------------ 8 Account_Velocity
    ws = wb.create_sheet("Account_Velocity")
    vs = q("SELECT * FROM velocity_spec_check")[0]
    vsn = [d[0] for d in cur.description]
    title(ws, "Account_Velocity - transaction velocity and high-velocity accounts",
          "Episodes = gaps-and-islands grouping (new episode when the gap to the previous txn > 10 minutes): each transaction counted once.")
    header(ws, 4, 1, ["Velocity check (population)", "Value"])
    for i, (k, v) in enumerate(zip(vsn, vs), start=5):
        ws.cell(row=i, column=1, value=k.replace("_", " ")).border = BORDER
        c = ws.cell(row=i, column=2, value=float(v) if v is not None else None)
        c.border = BORDER
        c.number_format = "#,##0.0" if "median" in k else "#,##0"
    ws.cell(row=5 + len(vsn), column=1, value="Brief rule '> 5 txns in 10 min' breached?").font = Font(bold=True)
    ws.cell(row=5 + len(vsn), column=2, value=f'=IF(B8>0,"YES","NO - zero accounts")').font = Font(bold=True, color="B00020")
    vd = q("SELECT window_label, txns_in_window, txn_count, fraud_txns, fraud_rate_pct FROM velocity_distribution ORDER BY window_label, txns_in_window")
    r0 = 5 + len(vsn) + 3
    ws.cell(row=r0 - 1, column=1, value="Velocity distribution (population): sender's txns in trailing window").font = Font(bold=True, color=NAVY)
    header(ws, r0, 1, ["Window", "Txns in window", "Transactions", "Fraud txns", "Fraud rate", "Sample count (formula)"])
    for i, r in enumerate(vd, start=r0 + 1):
        vals = [r[0], r[1], r[2], r[3], float(r[4]) / 100]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.border = BORDER
            c.number_format = {3: "#,##0", 4: "#,##0", 5: "0.000%"}.get(j, "General")
        col = {"trailing_10m": rng("W"), "trailing_24h": V24}.get(r[0])
        ws.cell(row=i, column=6, value=f"=COUNTIFS({col},B{i})" if col else "n/a (1 h not in sample sheet)").border = BORDER
    ep = q("""SELECT sender_account, episode_no, txn_count, start_ts::TEXT, end_ts::TEXT, duration_seconds::FLOAT8, max_txns_any_10m,
                     total_value_ngn::FLOAT8, avg_value_ngn::FLOAT8, transaction_types, unique_beneficiaries, fraud_txns
              FROM velocity_episodes ORDER BY max_txns_any_10m DESC, txn_count DESC, duration_seconds, sender_account LIMIT 500""")
    c0 = 9
    ws.cell(row=3, column=c0, value="Top 500 velocity episodes (densest first)").font = Font(bold=True, color=NAVY)
    header(ws, 4, c0, ["Account", "Episode #", "Txn count", "Start", "End", "Duration (s)", "Max txns in any 10 min", "Total value NGN",
                       "Avg value NGN", "Transaction types", "Unique beneficiaries", "Fraud txns", "Avg gap (s) - formula", "Breaches brief rule?"])
    for i, r in enumerate(ep, start=5):
        for j, v in enumerate(r, start=c0):
            ws.cell(row=i, column=j, value=v)
        ws.cell(row=i, column=c0 + 12, value=f"=IFERROR({get_column_letter(c0 + 5)}{i}/({get_column_letter(c0 + 2)}{i}-1),0)")
        ws.cell(row=i, column=c0 + 13, value=f'=IF({get_column_letter(c0 + 6)}{i}>5,"YES","no")')
        for jj, fmt in [(c0 + 5, "#,##0"), (c0 + 7, "#,##0"), (c0 + 8, "#,##0"), (c0 + 12, "#,##0")]:
            ws.cell(row=i, column=jj).number_format = fmt
    ee = 4 + len(ep)
    ws.conditional_formatting.add(f"{get_column_letter(c0 + 6)}5:{get_column_letter(c0 + 6)}{ee}",
                                  ColorScaleRule(start_type="min", start_color="FFFFFF", end_type="max", end_color="EC835A"))
    ws.conditional_formatting.add(f"{get_column_letter(c0 + 11)}5:{get_column_letter(c0 + 11)}{ee}",
                                  CellIsRule(operator="greaterThan", formula=["0"], fill=PatternFill("solid", fgColor="F8D0D0")))
    widths(ws, [44, 14, 13, 11, 11, 18, 2, 2, 12, 9, 9, 26, 26, 11, 12, 15, 14, 22, 11, 9, 12, 12])
    sr = ee + 3
    ws.cell(row=sr, column=c0, value="Episode summary (formulas over the 500 listed)").font = Font(bold=True, color=NAVY)
    for i, (k, f) in enumerate([("Episodes listed", f"=COUNTA({get_column_letter(c0)}5:{get_column_letter(c0)}{ee})"),
                                ("Episodes with 3 txns in 10 min", f"=COUNTIFS({get_column_letter(c0 + 6)}5:{get_column_letter(c0 + 6)}{ee},3)"),
                                ("Episodes containing labelled fraud", f'=COUNTIFS({get_column_letter(c0 + 11)}5:{get_column_letter(c0 + 11)}{ee},">0")'),
                                ("Total value in listed episodes (NGN)", f"=SUM({get_column_letter(c0 + 7)}5:{get_column_letter(c0 + 7)}{ee})")], start=sr + 1):
        ws.cell(row=i, column=c0, value=k)
        ws.cell(row=i, column=c0 + 3, value=f).number_format = "#,##0"

    # ------------------------------------------------------------ 9 Fraud_Summary
    ws = wb.create_sheet("Fraud_Summary")
    title(ws, "Fraud_Summary - fraud vs non-fraud comparison, amount distribution and rule evaluation",
          "Population (SQL) values in blue, sample formulas in orange.")
    cmp = q("""SELECT l.is_fraud::INT, COUNT(*), AVG(t.amount_ngn)::FLOAT8,
                      PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.amount_ngn)::FLOAT8,
                      PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.amount_ngn)::FLOAT8,
                      AVG(t.src_new_device_flag::INT)::FLOAT8, AVG((t.transaction_type='TRANSFER')::INT)::FLOAT8,
                      AVG((t.txn_hour<=4)::INT)::FLOAT8, AVG((r.risk_tier IN ('HIGH','CRITICAL'))::INT)::FLOAT8
               FROM transactions t JOIN fraud_labels l USING (transaction_id) JOIN transaction_risk_scores r USING (transaction_id)
               GROUP BY 1 ORDER BY 1 DESC""")
    header(ws, 4, 1, ["Metric", "Fraud (pop)", "Not fraud (pop)", "Fraud (sample)", "Not fraud (sample)", "Ratio fraud/not (pop)"])
    labels = ["Transactions", "Average amount NGN", "Median amount NGN", "P95 amount NGN", "Share with source new-device flag",
              "Share TRANSFER", "Share 00:00-04:59", "Share in HIGH/CRITICAL rule tier"]
    sform = [(f"=COUNTIFS({FR},1)", f"=COUNTIFS({FR},0)"),
             (f"=AVERAGEIFS({AMT},{FR},1)", f"=AVERAGEIFS({AMT},{FR},0)"),
             ("n/a", "n/a"), ("n/a", "n/a"),
             (f"=IFERROR(COUNTIFS({FR},1,{rng('P')},1)/D5,0)", f"=IFERROR(COUNTIFS({FR},0,{rng('P')},1)/E5,0)"),
             (f'=IFERROR(COUNTIFS({FR},1,{TY},"TRANSFER")/D5,0)', f'=IFERROR(COUNTIFS({FR},0,{TY},"TRANSFER")/E5,0)'),
             (f'=IFERROR(COUNTIFS({FR},1,{HR},"<=4")/D5,0)', f'=IFERROR(COUNTIFS({FR},0,{HR},"<=4")/E5,0)'),
             (f'=IFERROR((COUNTIFS({FR},1,{TIER},"HIGH")+COUNTIFS({FR},1,{TIER},"CRITICAL"))/D5,0)',
              f'=IFERROR((COUNTIFS({FR},0,{TIER},"HIGH")+COUNTIFS({FR},0,{TIER},"CRITICAL"))/E5,0)')]
    for i, lab in enumerate(labels):
        rr = 5 + i
        ws.cell(row=rr, column=1, value=lab)
        ws.cell(row=rr, column=2, value=cmp[0][1 + i]); ws.cell(row=rr, column=3, value=cmp[1][1 + i])
        ws.cell(row=rr, column=4, value=sform[i][0]); ws.cell(row=rr, column=5, value=sform[i][1])
        ws.cell(row=rr, column=6, value=f"=IFERROR(B{rr}/C{rr},0)")
        fmt = "#,##0" if i < 4 else "0.00%"
        for c in (2, 3, 4, 5):
            ws.cell(row=rr, column=c).number_format = fmt
            ws.cell(row=rr, column=c).fill = POPFILL if c < 4 else SAMPFILL
        ws.cell(row=rr, column=6).number_format = "0.000"
    box(ws, 4, 1, 12, 6)
    # amount distribution
    bands = [0, 5000, 25000, 100000, 250000, 500000, 1000000, 2500000, 5000000, 1e12]
    bl = ["< 5K", "5K-25K", "25K-100K", "100K-250K", "250K-500K", "500K-1M", "1M-2.5M", "2.5M-5M", "> 5M"]
    case = " ".join(f"WHEN t.amount_ngn < {bands[k + 1]} THEN {k}" for k in range(len(bl)))
    ad = q(f"""SELECT CASE {case} END AS b, COUNT(*) FILTER (WHERE l.is_fraud), COUNT(*) FILTER (WHERE NOT l.is_fraud)
               FROM transactions t JOIN fraud_labels l USING (transaction_id) GROUP BY 1 ORDER BY 1""")
    r0 = 16
    ws.cell(row=r0 - 1, column=1, value="Fraud transaction amount distribution (NGN bands)").font = Font(bold=True, color=NAVY)
    header(ws, r0, 1, ["Amount band", "Lower NGN", "Upper NGN", "Fraud txns (pop)", "Legit txns (pop)", "Fraud share of band (pop)",
                       "% of all fraud (pop)", "% of all legit (pop)", "Fraud (sample)", "Legit (sample)"])
    for k, r in enumerate(ad):
        rr = r0 + 1 + k
        ws.cell(row=rr, column=1, value=bl[k]); ws.cell(row=rr, column=2, value=bands[k]); ws.cell(row=rr, column=3, value=bands[k + 1] if k < len(bl) - 1 else 999999999)
        ws.cell(row=rr, column=4, value=r[1]); ws.cell(row=rr, column=5, value=r[2])
        ws.cell(row=rr, column=6, value=f"=IFERROR(D{rr}/(D{rr}+E{rr}),0)")
        ws.cell(row=rr, column=7, value=f"=D{rr}/SUM($D${r0 + 1}:$D${r0 + len(bl)})")
        ws.cell(row=rr, column=8, value=f"=E{rr}/SUM($E${r0 + 1}:$E${r0 + len(bl)})")
        ws.cell(row=rr, column=9, value=f'=COUNTIFS({AMT},">="&B{rr},{AMT},"<"&C{rr},{FR},1)')
        ws.cell(row=rr, column=10, value=f'=COUNTIFS({AMT},">="&B{rr},{AMT},"<"&C{rr},{FR},0)')
        for c, fmt in [(2, "#,##0"), (3, "#,##0"), (4, "#,##0"), (5, "#,##0"), (6, "0.000%"), (7, "0.0%"), (8, "0.0%")]:
            ws.cell(row=rr, column=c).number_format = fmt
    re_ = r0 + len(bl)
    box(ws, r0, 1, re_, 10)
    bc = BarChart(); bc.title = "Amount distribution: fraud vs legitimate (% of each class)"; bc.y_axis.numFmt = "0%"
    bc.add_data(Reference(ws, min_col=7, max_col=8, min_row=r0, max_row=re_), titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=1, min_row=r0 + 1, max_row=re_)); bc.height, bc.width = 9, 18
    ws.add_chart(bc, "L4")
    # rule evaluation
    rv = q("""SELECT rule_id, rule_name, evaluation_status, txns_captured, true_positives, false_positives, false_negatives, true_negatives,
                     precision_pct, recall_pct, false_positive_rate_pct, pct_volume_for_review, lift_vs_base_rate, false_positives_per_fraud_caught,
                     reviews_per_day FROM rule_evaluation ORDER BY rule_order""")
    r1 = re_ + 4
    ws.cell(row=r1 - 1, column=1, value="Fraud-rule evaluation (population, sql/fraud_network.sql section I) - precision/recall recomputed by formula").font = Font(bold=True, color=NAVY)
    header(ws, r1, 1, ["Rule", "Description", "Status", "Captured", "TP", "FP", "FN", "TN", "Precision (SQL)", "Recall (SQL)", "FPR (SQL)",
                       "% volume reviewed", "Lift", "FP per TP", "Reviews/day", "Precision (formula)", "Recall (formula)", "FPR (formula)"])
    for i, r in enumerate(rv, start=r1 + 1):
        vals = list(r)
        for j, v in enumerate(vals, start=1):
            if j >= 9 and j <= 12 and v is not None:
                v = float(v) / 100
            elif j >= 13 and v is not None:
                v = float(v)
            ws.cell(row=i, column=j, value=v)
        ws.cell(row=i, column=16, value=f'=IF(D{i}="","n/a",IFERROR(E{i}/(E{i}+F{i}),0))')
        ws.cell(row=i, column=17, value=f'=IF(D{i}="","n/a",IFERROR(E{i}/(E{i}+G{i}),0))')
        ws.cell(row=i, column=18, value=f'=IF(D{i}="","n/a",IFERROR(F{i}/(F{i}+H{i}),0))')
        for c, fmt in [(4, "#,##0"), (5, "#,##0"), (6, "#,##0"), (7, "#,##0"), (8, "#,##0"), (9, "0.00%"), (10, "0.00%"), (11, "0.00%"),
                       (12, "0.00%"), (13, "0.00"), (14, "0.0"), (15, "#,##0.0"), (16, "0.00%"), (17, "0.00%"), (18, "0.00%")]:
            ws.cell(row=i, column=c).number_format = fmt
    box(ws, r1, 1, r1 + len(rv), 18)
    widths(ws, [30, 34, 26, 12, 10, 12, 10, 12, 11, 10, 10, 11, 8, 9, 10, 12, 12, 11])

    # ------------------------------------------------------------ 10 Executive_Dashboard
    ws = wb.create_sheet("Executive_Dashboard", 0)
    kpi = dict(q("SELECT kpi, value FROM kpi_summary"))
    title(ws, "Executive Dashboard - Transaction Fraud Ring Detection",
          "Population = all 5,000,000 transactions (PostgreSQL). Labels = source is_fraud. Flags / tiers / rings = rule outputs (investigation leads).")
    header(ws, 4, 2, ["Key indicator", "Value", "Definition"])
    tiles = [("Total transactions", kpi["total_transactions"], "#,##0", "All source transactions analysed"),
             ("Total accounts", kpi["total_accounts"], "#,##0", "Seen as sender or receiver"),
             ("Fraud transactions (label)", kpi["fraud_transactions"], "#,##0", "Source is_fraud = True"),
             ("Fraud rate", "=C7/C5", "0.000%", "Formula: fraud / total"),
             ("Fraud-labelled accounts", kpi["fraud_labelled_accounts"], "#,##0", "Sent >= 1 labelled txn"),
             ("Flagged transactions (HIGH/CRIT)", kpi["flagged_transactions"], "#,##0", "Rule tier HIGH or CRITICAL"),
             ("Flagged share of volume", "=C10/C5", "0.00%", "Formula"),
             ("Suspicious value (NGN)", kpi["suspicious_value_ngn"], "#,##0", "Value of HIGH/CRITICAL txns"),
             ("Flagged accounts", kpi["flagged_accounts"], "#,##0", ">= 1 HIGH/CRITICAL sent txn"),
             ("Flagged accounts in potential rings", kpi["flagged_accounts_in_ring"], "#,##0", "Ring tier MEDIUM+"),
             ("High-risk accounts", kpi["high_risk_accounts"], "#,##0", "CRITICAL txn or HIGH/CRITICAL ring"),
             ("Linked groups (3+ accounts)", kpi["linked_groups_3plus"], "#,##0", "Connected components"),
             ("Potential fraud rings (MEDIUM+)", kpi["potential_fraud_rings"], "#,##0", "Scored linked groups"),
             ("HIGH/CRITICAL rings", kpi["high_critical_rings"], "#,##0", "Investigation priority"),
             ("Open investigation cases", kpi["investigation_cases"], "#,##0", "Ring + account cases"),
             ("Money-flow hops (24 h)", kpi["money_flow_hops_24h"], "#,##0", "A->B then B->C within 24 h"),
             ("Maximal money-flow chains", kpi["money_flow_maximal_paths"], "#,##0", "Recursive CTE output"),
             ("Accounts breaching '>5 txns in 10 min'", kpi["velocity_spec_breaches"], "#,##0", "Brief's velocity rule"),
             ]
    for i, (k, v, fmt, d) in enumerate(tiles, start=5):
        ws.cell(row=i, column=2, value=k).font = Font(bold=True)
        c = ws.cell(row=i, column=3, value=float(v) if not isinstance(v, str) else v)
        c.number_format = fmt
        c.font = Font(bold=True, size=12, color=NAVY)
        ws.cell(row=i, column=4, value=d).font = NOTE
    box(ws, 4, 2, 4 + len(tiles), 4)
    ws.cell(row=26, column=2, value="Assumptions").font = Font(bold=True, color=NAVY)
    ws.cell(row=27, column=2, value="FX assumption (NGN per USD)")
    ws.cell(row=27, column=3, value=750).number_format = "#,##0"
    ws.cell(row=27, column=4, value="Used only to translate the $500 rule threshold; change here and USD columns update").font = NOTE
    ws.cell(row=28, column=2, value="$500 rule threshold in NGN")
    ws.cell(row=28, column=3, value="=500*C27").number_format = "#,##0"
    box(ws, 27, 2, 28, 4)
    ws.cell(row=30, column=2, value="Live sample checks (formulas)").font = Font(bold=True, color=NAVY)
    checks = [("Sample fraud rate (Cleaned_Transactions)", f"=IFERROR(COUNTIFS({FR},1)/COUNTA({rng('A')}),0)", "0.000%"),
              ("Sample HIGH/CRITICAL share", f'=IFERROR((COUNTIFS({TIER},"HIGH")+COUNTIFS({TIER},"CRITICAL"))/COUNTA({rng("A")}),0)', "0.00%"),
              ("Precision in Fraud_Sample", "=Fraud_Sample!S8", "0.00%"),
              ("Proposed $500 rule - status", '=Fraud_Summary!C' + str(r1 + 1), "General"),
              ("Best evaluated precision (rule table)", f"=MAX(Fraud_Summary!I{r1 + 1}:I{r1 + len(rv)})", "0.00%")]
    for i, (k, f, fmt) in enumerate(checks, start=31):
        ws.cell(row=i, column=2, value=k)
        c = ws.cell(row=i, column=3, value=f)
        c.number_format = fmt
        c.font = Font(bold=True, color=NAVY)
    box(ws, 31, 2, 30 + len(checks), 3)
    ws.conditional_formatting.add("C5:C22", DataBarRule(start_type="min", end_type="max", color="9DB7DE"))
    widths(ws, [2, 40, 22, 60])
    # charts on the dashboard from other sheets
    msh = wb["Merchant_Analysis"]
    bc = BarChart(); bc.type = "bar"; bc.title = "Fraud rate by merchant category"; bc.y_axis.numFmt = "0.0%"
    bc.add_data(Reference(msh, min_col=5, min_row=4, max_row=4 + len(pop)), titles_from_data=True)
    bc.set_categories(Reference(msh, min_col=2, min_row=5, max_row=4 + len(pop))); bc.height, bc.width, bc.legend = 11, 16, None
    ws.add_chart(bc, "F4")
    hsh = wb["Hourly_Analysis"]
    lc = LineChart(); lc.title = "Fraud rate by hour"; lc.y_axis.numFmt = "0.0%"; lc.y_axis.scaling.min = 0
    lc.add_data(Reference(hsh, min_col=4, min_row=4, max_row=28), titles_from_data=True)
    lc.set_categories(Reference(hsh, min_col=1, min_row=5, max_row=28)); lc.height, lc.width, lc.legend = 8, 16, None
    ws.add_chart(lc, "F27")
    bsh = wb["Behavioral_Flags"]
    bc2 = BarChart(); bc2.type = "bar"; bc2.title = "Behavioural flag lift (1.0 = chance)"
    bc2.add_data(Reference(bsh, min_col=11, min_row=4, max_row=fe), titles_from_data=True)
    bc2.set_categories(Reference(bsh, min_col=1, min_row=5, max_row=fe)); bc2.height, bc2.width, bc2.legend = 11, 16, None
    ws.add_chart(bc2, "P4")

    for s in wb.worksheets:
        s.sheet_properties.tabColor = {"Executive_Dashboard": "0B2545", "Raw_Transactions": "8A94A6", "Cleaned_Transactions": "5B8FD9",
                                       "Data_Dictionary": "8A94A6"}.get(s.title, "B5651D")
    wb.calculation.fullCalcOnLoad = True
    order = ["Raw_Transactions", "Cleaned_Transactions", "Data_Dictionary", "Fraud_Sample", "Merchant_Analysis", "Hourly_Analysis",
             "Behavioral_Flags", "Account_Velocity", "Fraud_Summary", "Executive_Dashboard"]
    wb._sheets = [wb[n] for n in order]
    wb.active = 9
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"saved {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
