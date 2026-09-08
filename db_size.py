#!/usr/bin/env python3
"""
db_size.py — welche Tabelle belegt wie viel Platz?

    python3 db_size.py                 # Datenbankpfad aus der installierten Unit
    python3 db_size.py /pfad/hl_liq.db

Nutzt SQLites dbstat-Tabelle (Seiten je Tabelle und Index) und ordnet die
Tabellen den Projekten zu: Recorder, Binance-Modul, oder fremd (z. B. der
Marktsimulator aus dem anderen Projekt).
"""

import re
import sqlite3
import sys
from pathlib import Path

RECORDER = {"addresses", "heatmap", "bars", "bars10", "ctx_bars", "book_summary", "book_ticks",
            "proximity", "liquidations", "tick_events", "ticks", "mark_ticks", "whale_positions",
            "coverage", "snapshot_meta", "cluster_events", "magnet_events", "meta", "trigger_orders",
            "time_heatmap", "klines30"}
BINANCE = {"bnc_liquidations", "bnc_metrics", "bnc_bars", "daily_bars"}


def db_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
    unit = Path("/etc/systemd/system/hl-recorder.service")
    if unit.exists():
        m = re.search(r"--db\s+(\S+)", unit.read_text(errors="replace"))
        if m:
            return m.group(1)
    return "hl_liq.db"


def human(n):
    for u in ("B", "kB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def main():
    db = db_path()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    total = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
    print(f"Datenbank {db}: {human(total)} (ohne WAL)\n")
    try:
        rows = con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    except sqlite3.OperationalError:
        sys.exit("dbstat nicht verfügbar — SQLite ohne DBSTAT_VTAB gebaut.")
    # Indizes ihrer Tabelle zuschlagen
    idx = {r[0]: r[1] for r in con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")}
    size = {}
    for name, b in rows:
        size[idx.get(name, name)] = size.get(idx.get(name, name), 0) + b
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    groups = {"Recorder": 0, "Binance-Modul": 0, "fremd": 0}
    print(f"  {'Tabelle':<22} {'Größe':>10} {'Zeilen':>12}   Zuordnung")
    for t in sorted(tables, key=lambda t: -size.get(t, 0)):
        b = size.get(t, 0)
        if b < 64 * 1024:
            continue
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        who = "Recorder" if t in RECORDER else "Binance-Modul" if t in BINANCE else "fremd"
        groups[who] += b
        print(f"  {t:<22} {human(b):>10} {n:>12,}   {who}")
    print()
    for k, v in groups.items():
        print(f"  {k:<14} {human(v):>10}  ({v / total * 100:.0f} %)")
    con.close()


if __name__ == "__main__":
    main()
