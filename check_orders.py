#!/usr/bin/env python3
"""
check_orders.py — warum werden so wenige Konten nach Orders gefragt?

    python3 check_orders.py

Prüft der Reihe nach: laufende Version und Schwelle, Startzeit des Recorders,
Verteilung der Positionswerte in der Datenbank, wer in den letzten 20 Minuten
tatsächlich abgefragt wurde, und ob die Kandidatenauswahl mit den Positionen
aus der Datenbank die erwartete Menge liefert.
"""

import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv").exists() else sys.executable
OK, WARN, FAIL = "  [ok]   ", "  [!]    ", "  [FEHLER]"


def unit_arg(flag):
    p = Path("/etc/systemd/system/hl-recorder.service")
    if p.exists():
        m = re.search(flag + r"\s+(\S+)", p.read_text(errors="replace"))
        if m:
            return m.group(1)
    return None


def main():
    print("=" * 64 + "\n Diagnose Order-Abfragen\n" + "=" * 64)
    db = unit_arg("--db") or str(ROOT / "hl_liq.db")
    print(f"\nDatenbank: {db}")
    if not os.path.exists(db):
        sys.exit("nicht gefunden")

    # 1. Version und Schwelle
    print("\n1. Laufender Code")
    src = (ROOT / "hl_recorder.py").read_text(errors="replace")
    m = re.search(r"stop_min_usd:\s*float\s*=\s*([0-9.e]+)", src)
    thr = float(m.group(1)) if m else None
    print(f"{OK if thr else FAIL}Schwelle in hl_recorder.py: {thr:,.0f} USD" if thr else f"{FAIL}Schwelle nicht gefunden — alte Datei?")
    unit_thr = unit_arg("--stop-min-usd")
    if unit_thr:
        print(f"{WARN}Unit überschreibt mit --stop-min-usd {unit_thr}")
        thr = float(unit_thr)
    r = subprocess.run(["systemctl", "show", "hl-recorder", "-p", "ActiveEnterTimestamp", "-p", "ActiveState"],
                       capture_output=True, text=True)
    print("        " + r.stdout.strip().replace("\n", " · ") if r.stdout else f"{WARN}systemctl nicht abfragbar")
    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime((ROOT / "hl_recorder.py").stat().st_mtime))
    print(f"        hl_recorder.py zuletzt geändert {mtime} — der Dienst muss NACH diesem Zeitpunkt gestartet sein")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    now = int(time.time())

    # 2. Verteilung der Positionswerte
    print("\n2. Positionswerte in der Datenbank (Stand der letzten Abfrage je Adresse)")
    fresh = con.execute("SELECT COUNT(*) FROM addresses WHERE pos_value > 0 AND last_poll >= ?", (now - 7200,)).fetchone()[0]
    print(f"        Adressen mit Position, in den letzten 2 h abgefragt: {fresh:,}")
    counts = {}
    for mn in (20e6, 5e6, 1e6, 5e5, 2.5e5, 1e5):
        n = con.execute("SELECT COUNT(*) FROM addresses WHERE pos_value >= ?", (mn,)).fetchone()[0]
        n_fresh = con.execute("SELECT COUNT(*) FROM addresses WHERE pos_value >= ? AND last_poll >= ?",
                              (mn, now - 3600)).fetchone()[0]
        counts[mn] = n
        print(f"        ab {mn/1e6:>5.2f} Mio: {n:>5} Konten, davon {n_fresh:>5} in der letzten Stunde abgefragt"
              f"  -> Zyklus bei 15/min: {max(n/15, 1):5.1f} min")
    expected = counts.get(thr, None) if thr in counts else con.execute(
        "SELECT COUNT(*) FROM addresses WHERE pos_value >= ?", (thr,)).fetchone()[0]

    # 3. Wer wurde tatsächlich gefragt?
    print("\n3. Order-Abfragen der letzten 20 Minuten")
    try:
        polled = con.execute("SELECT COUNT(DISTINCT addr) FROM trigger_orders WHERE ts > ?", (now - 1200,)).fetchone()[0]
        with_orders = con.execute("SELECT COUNT(DISTINCT addr) FROM trigger_orders WHERE ts > ? AND kind != 'none'",
                                  (now - 1200,)).fetchone()[0]
        last = con.execute("SELECT MAX(ts) FROM trigger_orders").fetchone()[0]
        per_coin = con.execute("SELECT coin, COUNT(DISTINCT addr) FROM trigger_orders WHERE ts > ? AND kind != 'none' "
                               "GROUP BY coin", (now - 1200,)).fetchall()
        print(f"        {polled} Konten abgefragt, {with_orders} davon mit offenen Orders"
              + (f", jüngste Abfrage vor {(now - last)//60} min" if last else ""))
        print("        mit Orders je Coin: " + (", ".join(f"{c} {n}" for c, n in per_coin) or "—"))
        # Positionswert der abgefragten Konten: liegt jemand unter der Schwelle? Fehlt jemand darüber?
        below = con.execute("""SELECT COUNT(*) FROM (SELECT DISTINCT t.addr FROM trigger_orders t
                               JOIN addresses a ON a.addr = t.addr WHERE t.ts > ? AND a.pos_value < ?)""",
                            (now - 1200, thr)).fetchone()[0]
        missing = con.execute("""SELECT COUNT(*) FROM addresses a WHERE a.pos_value >= ? AND a.last_poll >= ?
                                 AND NOT EXISTS (SELECT 1 FROM trigger_orders t WHERE t.addr = a.addr AND t.ts > ?)""",
                              (thr, now - 3600, now - 1200)).fetchone()[0]
        print(f"        abgefragt trotz Positionswert unter Schwelle: {below}  (Wale zählen immer)")
        print(f"        über Schwelle, kürzlich gepollt, aber NICHT nach Orders gefragt: {missing}")
        if expected is not None:
            ratio = polled / max(expected, 1)
            if expected <= polled + 5:
                verdict = OK + f"stimmig: {expected} Konten über der Schwelle, {polled} abgefragt"
            elif missing > 10 and expected / 15 <= 20:
                verdict = FAIL + f"{missing} Konten über der Schwelle werden ausgelassen, obwohl der Zyklus reichen würde"
            elif expected / 15 > 20:
                verdict = WARN + f"{expected} Konten brauchen {expected/15:.0f} min je Zyklus — 20-Minuten-Fenster zeigt nur einen Teil"
            else:
                verdict = WARN + f"{expected} über der Schwelle, {polled} abgefragt — Recorder womöglich frisch gestartet, Positionen noch nicht vollständig"
            print(verdict)
    except sqlite3.OperationalError as e:
        print(f"{FAIL}trigger_orders nicht lesbar: {e}")

    # 4. Kandidatenauswahl nachrechnen, wie der Recorder sie sieht
    print("\n4. Kandidatenauswahl nachgerechnet (aus der Datenbank, Wale + ab Schwelle)")
    whales = con.execute("SELECT COUNT(*) FROM addresses WHERE pos_value >= 20e6").fetchone()[0]
    big = con.execute("SELECT COUNT(*) FROM addresses WHERE pos_value >= ? AND pos_value < 20e6", (thr,)).fetchone()[0]
    print(f"        Wale {whales} + Konten ab Schwelle {big} = {whales + big} Kandidaten")
    print("        Der Recorder kennt Positionswerte nur der Adressen, die er seit seinem Start abgefragt hat —")
    print("        nach einem Neustart wächst diese Menge über Stunden. Vergleich mit Zeile 3 zeigt, ob das der Grund ist.")

    # 5. Log
    log_dir = None
    u = Path("/etc/systemd/system/hl-recorder.service")
    if u.exists():
        m = re.search(r"append:(\S+)/recorder\.log", u.read_text(errors="replace"))
        log_dir = m.group(1) if m else None
    log = Path(log_dir or ROOT) / "recorder.log"
    print(f"\n5. Recorder-Log ({log})")
    if log.exists():
        lines = log.read_text(errors="replace").splitlines()
        starts = [l for l in lines if "Verbindungen: höchstens" in l]
        print(f"        Starts im Log: {len(starts)}")
        for l in [l for l in lines if "Liq-Feeds" in l or "Wal-Orders" in l][-2:]:
            print("        " + l[:140])
    else:
        print(f"{WARN}nicht gefunden")
    con.close()


if __name__ == "__main__":
    main()
