"""
05_er_diagram.py - renders reports/er_diagram.png and writes reports/er_diagram.mmd
Both describe the schema in sql/schema.sql (core layer = observed data,
derived layer = rule-engine output).
Run: python scripts/05_er_diagram.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports"

# entity -> (x, y, layer, [columns])   PK = primary key, FK = foreign key
E = {
    "merchants": (0.0, 6.3, "core", ["PK merchant_category", "merchant_group"]),
    "devices": (0.0, 9.6, "core", ["PK device_id", "first_seen_ts / last_seen_ts", "txn_count, account_count"]),
    "ip_addresses": (0.0, 2.6, "core", ["PK ip_address (INET)", "network_prefix16 (CIDR)", "txn_count, account_count", "network_class (derived)"]),
    "transactions": (4.8, 5.0, "core", ["PK transaction_id", "FK sender_account", "FK receiver_account", "FK merchant_category",
                                         "FK device_id", "FK ip_address", "txn_ts, amount_ngn", "transaction_type, channels",
                                         "src_new_device_flag, bvn_linked"]),
    "fraud_labels": (4.8, 0.9, "core", ["PK,FK transaction_id", "is_fraud", "fraud_type", "label_source"]),
    "accounts": (9.6, 9.2, "core", ["PK account_id", "sender_persona", "first_seen_ts (NOT creation)", "last_seen_ts",
                                     "sent / received_txn_count"]),
    "account_device_mapping": (4.8, 12.0, "core", ["PK,FK account_id", "PK,FK device_id", "first/last_seen_ts, txn_count"]),
    "account_ip_mapping": (4.8, 9.6, "core", ["PK,FK account_id", "PK,FK ip_address", "first/last_seen_ts, txn_count"]),
    "flag_definitions": (14.4, 0.9, "derived", ["PK flag_code", "flag_family, weight", "data_basis"]),
    "suspicious_activity_flags": (9.6, 0.9, "derived", ["PK,FK transaction_id", "PK,FK flag_code", "flag_detail"]),
    "transaction_risk_scores": (9.6, 3.3, "derived", ["PK,FK transaction_id", "risk_score, risk_tier", "flag_count, flag_list"]),
    "money_flow_paths": (14.4, 5.0, "derived", ["PK path_id", "FK root_transaction_id", "account_path[], transaction_path[]",
                                                  "hop_count, elapsed_minutes", "total_value_ngn"]),
    "account_links": (14.4, 11.6, "derived", ["PK,FK account_a", "PK,FK account_b", "PK link_type", "evidence_count, min_gap_seconds"]),
    "fraud_rings": (19.2, 8.0, "derived", ["PK ring_id", "component_id", "account_count, link counts", "connection_reasons",
                                            "network_score, label_score", "ring_risk_score, risk_tier"]),
    "fraud_ring_membership": (14.4, 8.2, "derived", ["PK,FK ring_id", "PK,FK account_id (UNIQUE)", "degree, link_types", "has_fraud_label"]),
    "account_risk_profile": (9.6, 12.4, "derived", ["PK,FK account_id", "FK ring_id", "has_fraud_label (source)", "is_flagged, is_ring_member",
                                                     "investigation_status"]),
    "investigation_cases": (19.2, 4.2, "derived", ["PK case_id", "FK ring_id (0..1)", "FK account_id (0..1) -> accounts", "priority_rank, risk_tier",
                                                    "case_reason, case_status"]),
    "engine_parameters": (19.2, 0.9, "derived", ["PK param_name", "param_value, unit", "rationale"]),
}
W = 3.3
ROW_H = 0.32
HEAD_H = 0.45


def box_h(name):
    return HEAD_H + ROW_H * len(E[name][3]) + 0.1


def anchor(name, side):
    x, y, _, _ = E[name]
    h = box_h(name)
    return {"l": (x, y + h / 2), "r": (x + W, y + h / 2), "t": (x + W / 2, y + h), "b": (x + W / 2, y)}[side]


# (from, side, to, side, label at from, label at to, caption)
REL = [
    ("merchants", "r", "transactions", "l", "1", "N", ""),
    ("devices", "r", "transactions", "l", "1", "N", ""),
    ("ip_addresses", "r", "transactions", "l", "1", "N", ""),
    ("accounts", "l", "transactions", "r", "1", "N", "sends / receives = money flow"),
    ("transactions", "b", "fraud_labels", "t", "1", "1", ""),
    ("devices", "t", "account_device_mapping", "l", "1", "N", ""),
    ("accounts", "t", "account_device_mapping", "r", "1", "N", ""),
    ("ip_addresses", "r", "account_ip_mapping", "l", "1", "N", ""),
    ("accounts", "l", "account_ip_mapping", "r", "1", "N", ""),
    ("transactions", "r", "transaction_risk_scores", "l", "1", "1", ""),
    ("transactions", "b", "suspicious_activity_flags", "l", "1", "N", ""),
    ("flag_definitions", "l", "suspicious_activity_flags", "r", "1", "N", ""),
    ("transactions", "r", "money_flow_paths", "l", "1", "N", "root txn"),
    ("accounts", "r", "account_links", "l", "1", "N", "account_a / account_b"),
    ("accounts", "r", "fraud_ring_membership", "l", "1", "0..1", ""),
    ("fraud_rings", "l", "fraud_ring_membership", "r", "1", "N", ""),
    ("accounts", "t", "account_risk_profile", "b", "1", "1", ""),
    ("fraud_rings", "t", "account_risk_profile", "t", "0..1", "N", ""),
    ("fraud_rings", "b", "investigation_cases", "t", "0..1", "N", ""),
]

COL = {"core": ("#0b2545", "#13315c", "#e8eef6"), "derived": ("#5c2a0b", "#7a3b12", "#fbefe6")}


def draw_png():
    fig, ax = plt.subplots(figsize=(24, 15), dpi=110)
    ax.set_xlim(-0.3, 22.8)
    ax.set_ylim(0.5, 15.6)
    ax.axis("off")
    for a, sa, b, sb, la, lb, cap in REL:
        (x1, y1), (x2, y2) = anchor(a, sa), anchor(b, sb)
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle="-", color="#6b7a90", lw=1.2,
                                    connectionstyle=f"arc3,rad={0.45 if (a, b) == ('fraud_rings', 'account_risk_profile') else 0.05}"))
        if la:
            ax.text(x1 + (x2 - x1) * 0.08, y1 + (y2 - y1) * 0.08 + 0.12, la, fontsize=9, color="#b00020", weight="bold")
        if lb:
            ax.text(x1 + (x2 - x1) * 0.9, y1 + (y2 - y1) * 0.9 + 0.12, lb, fontsize=9, color="#b00020", weight="bold")
        if cap:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 - 0.25, cap, fontsize=8, color="#44546a", style="italic", ha="center", zorder=6,
                    bbox=dict(fc="white", ec="none", pad=1))
    for name, (x, y, layer, cols) in E.items():
        h = box_h(name)
        edge, head, body = COL[layer]
        ax.add_patch(FancyBboxPatch((x, y), W, h, boxstyle="round,pad=0.02", fc=body, ec=edge, lw=1.6, zorder=3))
        ax.add_patch(FancyBboxPatch((x, y + h - HEAD_H), W, HEAD_H, boxstyle="round,pad=0.02", fc=head, ec=edge, lw=1.6, zorder=4))
        ax.text(x + W / 2, y + h - HEAD_H / 2, name, ha="center", va="center", color="white", fontsize=11, weight="bold", zorder=5)
        for i, c in enumerate(cols):
            weight = "bold" if c.startswith("PK") else "normal"
            ax.text(x + 0.12, y + h - HEAD_H - 0.22 - i * ROW_H, c, fontsize=8.6, va="center", zorder=5,
                    weight=weight, color="#1f2d3d", family="monospace")
    ax.text(0, 15.3, "Fintech Transaction Fraud Ring Detection - PostgreSQL schema `fraud`", fontsize=16, weight="bold", color="#0b2545")
    ax.text(0, 14.85, "Blue = CORE layer (observed source data)    Orange = DERIVED layer (rule-engine output, never a fraud label)    "
                      "Red labels = cardinality", fontsize=10, color="#44546a")
    fig.savefig(OUT / "er_diagram.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


MMD = """%% ER diagram - schema `fraud` (sql/schema.sql). Render with any Mermaid viewer (GitHub renders .mmd in markdown code blocks).
%% CORE = observed source data; DERIVED = output of sql/fraud_network.sql (rule-based, not ground truth)
erDiagram
    merchants ||--o{ transactions : "categorises"
    devices ||--o{ transactions : "used by"
    ip_addresses ||--o{ transactions : "originates"
    accounts ||--o{ transactions : "sends (sender_account)"
    accounts ||--o{ transactions : "receives (receiver_account) = money flow"
    transactions ||--|| fraud_labels : "source label"
    accounts ||--o{ account_device_mapping : "uses"
    devices ||--o{ account_device_mapping : "used by"
    accounts ||--o{ account_ip_mapping : "uses"
    ip_addresses ||--o{ account_ip_mapping : "used by"
    transactions ||--o{ suspicious_activity_flags : "DERIVED flags"
    flag_definitions ||--o{ suspicious_activity_flags : "defines"
    transactions ||--|| transaction_risk_scores : "DERIVED score"
    transactions ||--o{ money_flow_paths : "DERIVED root of chain"
    accounts ||--o{ account_links : "DERIVED account_a / account_b"
    accounts ||--o| fraud_ring_membership : "DERIVED member of"
    fraud_rings ||--|{ fraud_ring_membership : "has members"
    accounts ||--|| account_risk_profile : "DERIVED profile"
    fraud_rings |o--o{ account_risk_profile : "ring of account"
    fraud_rings |o--o{ investigation_cases : "RING case"
    accounts |o--o{ investigation_cases : "ACCOUNT case"

    merchants { varchar merchant_category PK  varchar merchant_group }
    accounts { bigint account_id PK  varchar sender_persona  timestamp first_seen_ts "first OBSERVED, not creation"  timestamp last_seen_ts  int sent_txn_count  int received_txn_count }
    devices { varchar device_id PK  timestamp first_seen_ts  timestamp last_seen_ts  int txn_count  int account_count }
    ip_addresses { inet ip_address PK  cidr network_prefix16  int account_count  varchar network_class "derived context" }
    transactions { varchar transaction_id PK  timestamp txn_ts  bigint sender_account FK  bigint receiver_account FK  varchar merchant_category FK  varchar device_id FK  inet ip_address FK  numeric amount_ngn  varchar transaction_type  boolean src_new_device_flag }
    fraud_labels { varchar transaction_id PK,FK  boolean is_fraud  varchar fraud_type  varchar label_source }
    account_device_mapping { bigint account_id PK,FK  varchar device_id PK,FK  int txn_count }
    account_ip_mapping { bigint account_id PK,FK  inet ip_address PK,FK  int txn_count }
    flag_definitions { varchar flag_code PK  varchar flag_family  smallint weight  varchar data_basis }
    suspicious_activity_flags { varchar transaction_id PK,FK  varchar flag_code PK,FK  text flag_detail }
    transaction_risk_scores { varchar transaction_id PK,FK  smallint risk_score  varchar risk_tier  text flag_list }
    account_links { bigint account_a PK,FK  bigint account_b PK,FK  varchar link_type PK  int evidence_count }
    money_flow_paths { bigint path_id PK  varchar root_transaction_id FK  smallint hop_count  text path_text  numeric total_value_ngn }
    fraud_rings { int ring_id PK  bigint component_id  int account_count  text connection_reasons  smallint ring_risk_score  varchar risk_tier }
    fraud_ring_membership { int ring_id PK,FK  bigint account_id PK,FK  int degree  boolean has_fraud_label }
    account_risk_profile { bigint account_id PK,FK  int ring_id FK  boolean has_fraud_label  boolean is_flagged  varchar investigation_status }
    investigation_cases { varchar case_id PK  varchar case_type  int ring_id FK  bigint account_id FK  int priority_rank  varchar case_status }
    engine_parameters { varchar param_name PK  numeric param_value  text rationale }
"""

def mermaid_text() -> str:
    """Expand single-line entity blocks into one attribute per line (Mermaid syntax)."""
    out = []
    for line in MMD.splitlines():
        s = line.strip()
        if s.endswith("}") and "{" in s and not s.startswith("%%"):
            name, body = s[:-1].split("{", 1)
            out.append(f"    {name.strip()} {{")
            out.extend(f"        {attr.strip()}" for attr in body.split("  ") if attr.strip())
            out.append("    }")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "er_diagram.mmd").write_text(mermaid_text(), encoding="utf-8")
    draw_png()
    print("written reports/er_diagram.mmd and reports/er_diagram.png")
