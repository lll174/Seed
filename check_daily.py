#!/usr/bin/env python3
"""
check_daily.py — warum fehlen die MA-Kacheln?

    python3 check_daily.py

Findet den Datenbankpfad aus der installierten Unit, lädt die Tageskerzen
von Hand nach, zeigt die Tabelle und das Log des Binance-Dienstes, und
prüft die drei Quellen (Binance-Futures, Binance-Spot, Hyperliquid) direkt.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


def probe(label, url, data=None):
    """Eine Quelle direkt ansprechen und HTTP-Status berichten."""
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "hl-check/1.0",
                                     **({"Content-Type": "application/json"} if data else {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            n = len(json.loads(body)) if body[:1] in (b"[", b"{") else 0
            print(f"{OK}{label:<22} HTTP {r.status}, {n} Einträge")
            return True
    except urllib.error.HTTPError as e:
        print(f"{FAIL}{label:<22} HTTP {e.code} {e.reason}"
              + ("  <- Regionssperre" if e.code == 451 else ""))
    except Exception as e:
        print(f"{FAIL}{label:<22} {type(e).__name__}: {str(e)[:80]}")
    return False


def main():
    print("=" * 62 + "\n Diagnose Tageskerzen / MA-Kacheln\n" + "=" * 62)

    db = unit_arg("--db") or str(ROOT / "hl_liq.db")
    print(f"\nDatenbank: {db}  ({'vorhanden' if os.path.exists(db) else 'FEHLT'})")
    if not os.path.exists(db):
        sys.exit("Ohne Datenbank kann nichts geprüft werden.")

    # 1. Dateiversion
    print("\n1. Dateien")
    for f, marker in (("hl_binance.py", "backfill_daily"), ("hl_viz.py", "load_mas")):
        txt = (ROOT / f).read_text(errors="replace") if (ROOT / f).exists() else ""
        print(f"{OK if marker in txt else FAIL}{f:<16} "
              + ("neue Version" if marker in txt else f"ALTE Version — '{marker}' fehlt"))

    # 2. Tabelle
    print("\n2. Tabelle daily_bars")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    has = con.execute("SELECT 1 FROM sqlite_master WHERE name='daily_bars'").fetchone()
    if not has:
        print(f"{WARN}Tabelle existiert noch nicht — der Binance-Dienst hat die neue "
              "Version noch nie gestartet, oder der Backfill lief noch nie")
    else:
        rows = con.execute("SELECT coin, COUNT(*), source, MIN(ts), MAX(ts) FROM daily_bars "
                           "GROUP BY coin ORDER BY coin").fetchall()
        if not rows:
            print(f"{WARN}Tabelle vorhanden, aber leer")
        for c, n, src, a, b in rows:
            print(f"{OK}{c:<6} {n:>5} Tage  {time.strftime('%Y-%m-%d', time.gmtime(a))} … "
                  f"{time.strftime('%Y-%m-%d', time.gmtime(b))}  ({src})")
    con.close()

    # 3. Quellen direkt
    print("\n3. Quellen direkt vom Pi aus")
    fut = probe("Binance Futures", "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=3")
    spot = probe("Binance Spot", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=3")
    now = int(time.time() * 1000)
    hl = probe("Hyperliquid", "https://api.hyperliquid.xyz/info",
               json.dumps({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1d",
                           "startTime": now - 5 * 86_400_000, "endTime": now}}).encode())
    if not (fut or spot or hl):
        print(f"{FAIL}Keine Quelle erreichbar — Netzwerk prüfen (curl -4 -sI https://www.google.com)")

    # 4. Backfill von Hand
    print("\n4. Backfill von Hand")
    r = subprocess.run([PY, str(ROOT / "hl_binance.py"), "--backfill-daily", "--db", db],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout + r.stderr).strip()
    print("        " + out.replace("\n", "\n        ") if out else f"{WARN}keine Ausgabe")
    if r.returncode != 0:
        print(f"{FAIL}Backfill mit Exitcode {r.returncode} beendet")

    # 5. Ergebnis
    print("\n5. Ergebnis")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute("SELECT coin, COUNT(*) FROM daily_bars GROUP BY coin").fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    if rows:
        print(f"{OK}" + ", ".join(f"{c} {n}" for c, n in rows) + " Tageskerzen")
        print("        -> im Dashboard Strg+F5, die MA-Kacheln füllen sich")
    else:
        print(f"{FAIL}weiterhin keine Tageskerzen — bitte diese Ausgabe komplett schicken")

    # 6. Dienst-Log
    log_dir = None
    m = re.search(r"append:(\S+)/binance\.log",
                  Path("/etc/systemd/system/hl-binance.service").read_text(errors="replace")) \
        if Path("/etc/systemd/system/hl-binance.service").exists() else None
    log = Path(m.group(1)) / "binance.log" if m else ROOT / "binance.log"
    print(f"\n6. Dienst-Log ({log})")
    if log.exists():
        lines = log.read_text(errors="replace").splitlines()
        for l in lines[-12:]:
            print("        " + l[:120])
    else:
        print(f"{WARN}nicht gefunden")
    st = subprocess.run(["systemctl", "is-active", "hl-binance"], capture_output=True, text=True).stdout.strip()
    print(f"\n   hl-binance: {st}")


if __name__ == "__main__":
    main()
