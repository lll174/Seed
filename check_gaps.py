#!/usr/bin/env python3
"""
check_gaps.py — untersucht die Lücken in den Zeitreihen der Recorder-Datenbank.

    python3 check_gaps.py --db hl_liq.db --coin BTC

Beantwortet die Frage, die das Audit offen lässt: fehlende Minuten sind dort
nur eine Summe. Entscheidend ist, ob sie am Stück am Anfang liegen (dann ist
es ein Startversatz und kostet nur Backtest-Zeitraum) oder über den ganzen
Zeitraum verstreut (dann setzt der Simulator laufend aus, weil er Einstiege
verwirft, sobald der Markpreis älter als `max_ctx_age` ist).

Öffnet die Datenbank schreibgeschützt und ändert nichts.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

# Tabelle -> (erwarteter Takt in Sekunden, ab wann eine Lücke stört)
SERIES = {
    "bars":         (60, 120),
    "ctx_bars":     (60, 120),    # Markpreis: Grenze aus Features.usable()
    "bars10":       (10, 60),
    "book_summary": (5, 30),      # Grenze aus Features.usable()
    "proximity":    (60, 120),
    "heatmap":      (600, 1500),
}


def utc(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M")


def dur(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s/60:.0f} min"
    return f"{s/3600:.1f} h"


def timestamps(con, table: str, coin: str) -> list[int]:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if "ts" not in cols:
        return []
    if "coin" in cols:
        rows = con.execute(
            f"SELECT DISTINCT ts FROM {table} WHERE coin=? ORDER BY ts", (coin,))
    else:
        rows = con.execute(f"SELECT DISTINCT ts FROM {table} ORDER BY ts")
    return [r[0] for r in rows]


def analyse(con, table: str, coin: str, tick: int, limit: int, t_ref: int | None):
    ts = timestamps(con, table, coin)
    if len(ts) < 2:
        print(f"  {table:14s} keine oder zu wenige Daten")
        return None

    start, end = ts[0], ts[-1]
    span = end - start
    gaps = [(a, b - a) for a, b in zip(ts, ts[1:]) if b - a > limit]
    lost = sum(g for _, g in gaps)

    # Startversatz gegenüber der Referenzreihe (üblicherweise `bars`)
    offset = (start - t_ref) if t_ref else 0

    print(f"  {table:14s} {len(ts):>8,} Stempel  "
          f"{utc(start)} bis {utc(end)}  ({dur(span)})")
    print(f"  {'':14s} Takt erwartet {tick} s, "
          f"Lücken über {limit} s: {len(gaps)}")

    if offset > limit:
        print(f"  {'':14s} beginnt {dur(offset)} nach `bars` — Startversatz")

    if gaps:
        big = sorted(gaps, key=lambda g: -g[1])[:5]
        print(f"  {'':14s} verlorene Zeit gesamt {dur(lost)} "
              f"({lost/span*100:.1f} % der Spanne)")
        print(f"  {'':14s} größte Lücken:")
        for at, g in big:
            print(f"  {'':16s} {utc(at)}  +{dur(g)}")

        # Verstreut oder gebündelt? Anteil der Lücken in der ersten Stunde.
        early = sum(g for at, g in gaps if at - start < 3600)
        if early / lost > 0.8:
            print(f"  {'':14s} -> fast alles in der ersten Stunde: Anlaufphase")
        elif len(gaps) > 20:
            print(f"  {'':14s} -> verstreut über den Zeitraum")
    return {"start": start, "end": end, "gaps": len(gaps), "lost": lost,
            "span": span}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="hl_liq.db")
    ap.add_argument("--coin", default="BTC")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    present = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    print(f"Datenbank: {a.db}   Coin: {a.coin}\n")
    ref = None
    if "bars" in present:
        t = timestamps(con, "bars", a.coin)
        ref = t[0] if t else None

    results = {}
    for table, (tick, limit) in SERIES.items():
        if table not in present:
            print(f"  {table:14s} Tabelle fehlt")
            continue
        r = analyse(con, table, a.coin, tick, limit, ref)
        if r:
            results[table] = r
        print()

    # Der Bereich, den der Simulator im vollen Modus tatsächlich nutzen kann
    need = ["bars", "ctx_bars", "book_summary", "heatmap", "proximity"]
    have = [results[t] for t in need if t in results]
    if len(have) == len(need):
        lo = max(r["start"] for r in have)
        hi = min(r["end"] for r in have)
        print("Nutzbarer Bereich im vollen Modus (engster gemeinsamer Nenner):")
        print(f"  {utc(lo)} bis {utc(hi)}   ({dur(max(0, hi-lo))})")
        binder = min(need, key=lambda t: -results[t]["start"])
        print(f"  begrenzt durch den späten Beginn von `{binder}`")
    else:
        fehlt = [t for t in need if t not in results]
        print(f"Voller Modus nicht möglich — es fehlt: {', '.join(fehlt)}")

    # Konkrete Empfehlung
    ctx = results.get("ctx_bars")
    book = results.get("book_summary")
    print("\nFolgerung für den Simulator:")
    for name, r, param, grenze in (("ctx_bars", ctx, "max_ctx_age", 120),
                                   ("book_summary", book, "max_book_age", 30)):
        if not r:
            continue
        if r["gaps"] > 20:
            print(f"  {name}: {r['gaps']} Lücken über {grenze} s — "
                  f"`{param}` in Features.usable() anheben, sonst verwirft "
                  f"der Bot laufend Einstiege.")
        else:
            print(f"  {name}: nur {r['gaps']} Lücken — {param} bei "
                  f"{grenze} s unbedenklich.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
