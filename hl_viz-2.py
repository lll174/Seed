#!/usr/bin/env python3
"""
hl_viz.py — Visualisierung für die von hl_recorder.py aufgezeichneten Daten.

Drei Modi:
    python hl_viz.py --coin BTC                  # Heatmap als PNG
    python hl_viz.py --coin BTC --depth          # aktueller Snapshot als Balken
    python hl_viz.py --serve                     # Live-Dashboard im Browser

Das Dashboard läuft parallel zum Recorder auf derselben SQLite-Datei
(WAL-Modus, gleichzeitiges Lesen ist unproblematisch) und aktualisiert sich
selbst. Es braucht keine Internetverbindung und keine JS-Bibliotheken.
"""

from __future__ import annotations

import argparse
import json
import os
import math
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DB_PATH = "hl_liq.db"


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Datenabfrage
# ---------------------------------------------------------------------------

def load_grid(con, coin: str, hours: int):
    """Heatmap als Gitter (Zeitachse x Preisachse) plus Preisreihe."""
    t_from = int(time.time()) - hours * 3600

    heat = con.execute(
        "SELECT ts, bucket_px, side, notional FROM heatmap "
        "WHERE coin=? AND ts>=? ORDER BY ts", (coin, t_from)).fetchall()
    bars = con.execute(
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE coin=? AND ts>=? ORDER BY ts", (coin, t_from)).fetchall()
    liqs = con.execute(
        "SELECT ts, px, sz, side FROM liquidations "
        "WHERE coin=? AND ts>=? ORDER BY ts", (coin, t_from)).fetchall()

    if not heat or not bars:
        return None

    times = sorted({r["ts"] for r in heat})
    levels = sorted({r["bucket_px"] for r in heat})
    t_idx = {t: i for i, t in enumerate(times)}
    l_idx = {p: i for i, p in enumerate(levels)}

    grid = [[0.0] * len(times) for _ in levels]
    sides = [[""] * len(times) for _ in levels]
    for r in heat:
        i, j = l_idx[r["bucket_px"]], t_idx[r["ts"]]
        grid[i][j] += r["notional"]
        sides[i][j] = r["side"]

    return {
        "times": times, "levels": levels, "grid": grid, "sides": sides,
        "price": [(r["ts"], r["close"]) for r in bars],
        "liqs": [(r["ts"], r["px"], r["px"] * r["sz"], r["side"]) for r in liqs],
    }


def load_depth(con, coin: str):
    """Aktuellster Snapshot: Cluster ober- und unterhalb des Kurses."""
    row = con.execute(
        "SELECT MAX(ts) t FROM heatmap WHERE coin=?", (coin,)).fetchone()
    if not row or not row["t"]:
        return None
    ts = row["t"]
    rows = con.execute(
        "SELECT bucket_px, side, notional, n_pos FROM heatmap "
        "WHERE coin=? AND ts=? ORDER BY bucket_px", (coin, ts)).fetchall()
    px = con.execute(
        "SELECT close FROM bars WHERE coin=? ORDER BY ts DESC LIMIT 1",
        (coin,)).fetchone()
    vol = con.execute(
        "SELECT SUM(volume*close) v FROM bars WHERE coin=? AND ts>=?",
        (coin, ts - 3600)).fetchone()
    return {
        "ts": ts,
        "price": px["close"] if px else None,
        "vol_1h": (vol["v"] or 0) if vol else 0,
        "rows": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Statische Grafiken
# ---------------------------------------------------------------------------

def load_whales(con, coin: str, hours: int = 72):
    """Aktuelle Wale, Ereignisse und Netto-Exposure-Zeitreihe."""
    try:
        con.execute("SELECT 1 FROM whale_positions LIMIT 1")
    except sqlite3.OperationalError:
        return {"current": [], "events": [], "exposure": [], "price": []}
    t_from = int(time.time()) - hours * 3600

    # letzter Zustand je Wal: jüngste Zeile, die kein close ist
    current = con.execute("""
        SELECT w.addr, w.coin, w.side, w.size, w.pos_value, w.entry_px, w.liq_px,
               w.leverage, w.upnl, w.mark_px, w.ts,
               (SELECT MIN(ts) FROM whale_positions o
                 WHERE o.addr=w.addr AND o.coin=w.coin AND o.event='open'
                   AND o.ts <= w.ts
                   AND NOT EXISTS (SELECT 1 FROM whale_positions c
                                   WHERE c.addr=w.addr AND c.coin=w.coin
                                     AND c.event='close' AND c.ts>o.ts AND c.ts<=w.ts))
               AS opened_at
        FROM whale_positions w
        WHERE w.ts = (SELECT MAX(ts) FROM whale_positions x
                      WHERE x.addr=w.addr AND x.coin=w.coin)
          AND w.event != 'close'
        ORDER BY w.pos_value DESC""").fetchall()

    events = con.execute("""
        SELECT ts, addr, event, side, pos_value, delta_usd, mark_px, leverage
        FROM whale_positions
        WHERE coin=? AND ts>=? AND event != 'snap' ORDER BY ts""",
        (coin, t_from)).fetchall()

    # Netto-Exposure je Snapshot: long positiv, short negativ
    exposure = con.execute("""
        SELECT ts,
               SUM(CASE WHEN side='long'  THEN pos_value ELSE 0 END) AS long_usd,
               SUM(CASE WHEN side='short' THEN pos_value ELSE 0 END) AS short_usd,
               COUNT(*) AS n
        FROM whale_positions
        WHERE coin=? AND ts>=? AND event='snap'
        GROUP BY ts ORDER BY ts""", (coin, t_from)).fetchall()

    price = con.execute(
        "SELECT ts, close FROM bars WHERE coin=? AND ts>=? ORDER BY ts",
        (coin, t_from)).fetchall()[::5]          # jede 5. Minute reicht fürs Bild

    return {
        "current": [dict(r) for r in current],
        "events": [dict(r) for r in events],
        "exposure": [dict(r) for r in exposure],
        "price": [(r["ts"], r["close"]) for r in price],
    }


def find_fvgs(candles: list[dict], min_pct: float = 0.0005) -> list[dict]:
    """
    Fair Value Gaps: Dreikerzen-Lücken, rein aus den Kerzen berechnet.

    Bullisch: Tief der dritten Kerze über dem Hoch der ersten.
    Bärisch:  Hoch der dritten Kerze unter dem Tief der ersten.
    Die Zone zwischen beiden ist die Lücke. Sie gilt als "gemildert", sobald
    spätere Kerzen wieder hineinlaufen: teilweise (Anteil), oder ganz, wenn
    der Kurs die ferne Kante erreicht. Nichts davon wird gespeichert.
    """
    gaps = []
    for i in range(2, len(candles)):
        a, b, c = candles[i - 2], candles[i - 1], candles[i]
        if c["l"] > a["h"]:
            kind, top, bot = "bull", c["l"], a["h"]
        elif c["h"] < a["l"]:
            kind, top, bot = "bear", a["l"], c["h"]
        else:
            continue
        mid = (top + bot) / 2
        if mid <= 0 or (top - bot) / mid < min_pct:
            continue
        gaps.append({"i": i - 1, "t": b["t"], "kind": kind, "top": top, "bot": bot,
                     "size_pct": (top - bot) / mid, "fill_i": None, "fill_pct": 0.0})

    for g in gaps:
        span = g["top"] - g["bot"]
        for j in range(g["i"] + 2, len(candles)):
            c = candles[j]
            if g["kind"] == "bull":
                if c["l"] <= g["bot"]:
                    g["fill_i"], g["fill_pct"] = j, 1.0
                    break
                if c["l"] < g["top"]:
                    g["fill_pct"] = max(g["fill_pct"], (g["top"] - c["l"]) / span)
            else:
                if c["h"] >= g["top"]:
                    g["fill_i"], g["fill_pct"] = j, 1.0
                    break
                if c["h"] > g["bot"]:
                    g["fill_pct"] = max(g["fill_pct"], (c["h"] - g["bot"]) / span)
    return gaps


_HEALTH_CACHE: dict = {"ts": 0, "data": None}


def load_health(db: str, max_age: int = 3600) -> dict:
    """
    Backup-Status und Zustand der laufenden Datenbank. quick_check kann bei
    großen Dateien Sekunden dauern, deshalb höchstens einmal pro Stunde.
    """
    now = int(time.time())
    if _HEALTH_CACHE["data"] and now - _HEALTH_CACHE["ts"] < max_age:
        return _HEALTH_CACHE["data"]

    backup = None
    bdir = os.path.join(os.path.dirname(os.path.abspath(db)), "dbbackup")
    try:
        with open(os.path.join(bdir, "backup.json")) as f:
            backup = json.load(f)
    except (OSError, ValueError):
        pass

    db_ok, detail = None, ""
    try:
        con = connect(db)
        t0 = time.time()
        res = con.execute("PRAGMA quick_check").fetchone()[0]
        con.close()
        db_ok = res == "ok"
        detail = "" if db_ok else str(res)[:120]
        check_s = round(time.time() - t0, 1)
    except sqlite3.Error as e:
        db_ok, detail, check_s = False, str(e)[:120], 0

    data = {"checked_at": now, "db_ok": db_ok, "db_detail": detail,
            "check_seconds": check_s, "backup": backup,
            "db_size": os.path.getsize(db) if os.path.exists(db) else 0}
    _HEALTH_CACHE.update(ts=now, data=data)
    return data


def load_chart(con, coin: str, minutes: int = 240, agg: int = 1, top: int = 14,
               fvg_min: float = 0.0005):
    """Kerzen aus den 1-Minuten-Bars plus die stärksten aktuellen Level."""
    t_from = int(time.time()) - minutes * 60
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM bars "
        "WHERE coin=? AND ts>=? ORDER BY ts", (coin, t_from)).fetchall()

    candles, cur = [], None
    step = max(agg, 1) * 60
    for r in rows:
        b = r["ts"] // step * step
        if cur is None or b != cur["t"]:
            if cur:
                candles.append(cur)
            cur = {"t": b, "o": r["open"], "h": r["high"],
                   "l": r["low"], "c": r["close"], "v": r["volume"]}
        else:
            cur["h"] = max(cur["h"], r["high"])
            cur["l"] = min(cur["l"], r["low"])
            cur["c"] = r["close"]
            cur["v"] += r["volume"]
    if cur:
        candles.append(cur)

    # 60-Minuten-Trend aus den Minutenbars, unabhängig vom Chart-Raster
    trend = None
    if len(rows) >= 61:
        c_now, c_then = rows[-1]["close"], rows[-61]["close"]
        if c_then:
            trend = c_now / c_then - 1

    # RSI(14) nach Wilder auf dem angezeigten Kerzenraster
    rsi = None
    closes = [c["c"] for c in candles]
    if len(closes) >= 16:
        gains, losses = [], []
        for a, b in zip(closes[:-1], closes[1:]):
            d_ = b - a
            gains.append(max(d_, 0.0)); losses.append(max(-d_, 0.0))
        n = 14
        ag = sum(gains[:n]) / n
        al = sum(losses[:n]) / n
        for g_, l_ in zip(gains[n:], losses[n:]):
            ag = (ag * (n - 1) + g_) / n
            al = (al * (n - 1) + l_) / n
        rsi = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    d = load_depth(con, coin)
    levels = []
    if d and d["price"]:
        px = d["price"]
        near = [r for r in d["rows"] if 0.88 * px < r["bucket_px"] < 1.15 * px]
        near.sort(key=lambda r: -r["notional"])
        for r in near[:top]:
            levels.append({
                "px": r["bucket_px"], "side": r["side"],
                "notional": r["notional"], "n_pos": r["n_pos"],
                "dist": (r["bucket_px"] - px) / px,
                # Verhältnis zum Stundenvolumen: entscheidet, ob ein Level trägt
                "x_vol": (r["notional"] / d["vol_1h"]) if d["vol_1h"] else 0,
            })

    try:
        events = [dict(r) for r in con.execute(
            "SELECT t_trigger, reason, move_pct, px_extreme, n_ticks, n_liqs "
            "FROM tick_events WHERE coin=? ORDER BY t_trigger DESC LIMIT 5", (coin,))]
    except sqlite3.OperationalError:
        events = []

    fvgs = find_fvgs(candles, fvg_min)
    last = candles[-1]["c"] if candles else 0
    for g in fvgs:
        g["dist"] = ((g["bot"] if g["kind"] == "bull" else g["top"]) / last - 1) if last else 0

    return {
        "candles": candles, "levels": levels,
        "tick_events": events,
        "fvgs": fvgs,
        "price": d["price"] if d else None,
        "vol_1h": d["vol_1h"] if d else 0,
        "snapshot_ts": d["ts"] if d else 0,
        "trend_60m": trend,
        "rsi14": rsi,
        "rsi_tf": f"{agg} min" if agg < 60 else f"{agg // 60} h",
    }


def plot_heatmap(con, coin: str, hours: int, out: str,
                 threshold: float = 0.70, gamma: float = 2.6) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.dates import DateFormatter, date2num

    d = load_grid(con, coin, hours)
    if d is None:
        print(f"Keine Daten für {coin} in den letzten {hours} h.")
        return

    z = np.array(d["grid"], dtype=float)

    # Perzentil-Zuordnung statt Normierung aufs Maximum: entscheidend ist der
    # Rang einer Zelle unter allen anderen. Anschließend eine Potenz, damit
    # die Masse dunkel bleibt und nur die stärksten Cluster leuchten.
    pos = z[z > 0]
    if pos.size == 0:
        print("Nur leere Zellen.")
        return
    srt = np.sort(pos)
    rank = np.searchsorted(srt, z, side="right") / srt.size
    rank[z <= 0] = 0.0
    vis = rank >= threshold
    q = np.zeros_like(rank)
    q[vis] = ((rank[vis] - threshold) / max(1e-9, 1 - threshold)) ** gamma
    q = np.ma.masked_where(~vis, q)

    cmap = LinearSegmentedColormap.from_list("liq", [
        (0.00, "#04060f"), (0.22, "#0a288c"), (0.45, "#0096c8"),
        (0.68, "#3cdcb4"), (0.85, "#ffd60a"), (1.00, "#ffffeb")])
    cmap.set_bad("#04060f")

    x = date2num([np.datetime64(t, "s") for t in d["times"]])
    y = np.array(d["levels"])

    fig, ax = plt.subplots(figsize=(15, 8.5))
    ax.set_facecolor("#04060f")
    mesh = ax.pcolormesh(x, y, q, cmap=cmap, vmin=0, vmax=1, shading="nearest")

    pt = date2num([np.datetime64(t, "s") for t, _ in d["price"]])
    ax.plot(pt, [p for _, p in d["price"]], color="#ffffff", lw=1.5,
            label="Kurs", zorder=5)

    if d["liqs"]:
        lt = date2num([np.datetime64(t, "s") for t, *_ in d["liqs"]])
        lp = [p for _, p, *_ in d["liqs"]]
        ls = [max(6, min(220, n / 4000)) for *_, n, _ in d["liqs"]]
        ax.scatter(lt, lp, s=ls, facecolor="none", edgecolor="#ff3b6b",
                   lw=1.1, zorder=6, label="Liquidationen")

    prices = [p for _, p in d["price"]]
    ax.set_ylim(min(prices) * 0.90, max(prices) * 1.10)
    ax.xaxis.set_major_formatter(DateFormatter("%d.%m %H:%M"))
    ax.set_title(f"{coin} — Liquidationscluster, letzte {hours} h · "
                 f"nur oberhalb Perzentil {threshold*100:.0f}", fontsize=12)
    ax.set_ylabel("Preis")
    ax.grid(alpha=0.10, ls=":")
    ax.legend(loc="upper left", framealpha=0.6)
    cb = fig.colorbar(mesh, ax=ax, pad=0.01)
    cb.set_label("relative Clusterstärke (Rang unter allen Zellen)")
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(out, dpi=130, facecolor="#0d1117")
    print(f"Gespeichert: {out}")


def plot_depth(con, coin: str, out: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = load_depth(con, coin)
    if not d or not d["rows"] or not d["price"]:
        print(f"Kein aktueller Snapshot für {coin}.")
        return

    px, v1h = d["price"], d["vol_1h"]
    rows = [r for r in d["rows"] if 0.8 * px < r["bucket_px"] < 1.25 * px]
    if not rows:
        print("Keine Cluster im relevanten Preisbereich.")
        return

    fig, ax = plt.subplots(figsize=(9, 11))
    for r in rows:
        col = "#e04b4b" if r["side"] == "long" else "#3ba55d"
        ax.barh(r["bucket_px"], r["notional"], height=px * 0.0022,
                color=col, alpha=0.85)

    ax.axhline(px, color="#00e5ff", lw=2)
    ax.text(ax.get_xlim()[1], px, f"  {px:,.2f}", va="center",
            color="#00808f", fontsize=10, fontweight="bold")

    if v1h > 0:
        ax.axvline(v1h * 0.5, color="grey", ls="--", lw=1)
        ax.text(v1h * 0.5, ax.get_ylim()[1], " 0.5x 1h-Vol",
                rotation=90, va="top", fontsize=8, color="grey")

    ax.set_title(f"{coin} — Cluster um den Kurs\n"
                 f"rot = Long-Liquidationen, grün = Short-Liquidationen\n"
                 f"Stand {time.strftime('%d.%m. %H:%M', time.localtime(d['ts']))}",
                 fontsize=11)
    ax.set_xlabel("Notional (USD)")
    ax.grid(axis="x", alpha=0.2, ls=":")
    plt.tight_layout()
    plt.savefig(out, dpi=130)
    print(f"Gespeichert: {out}")


# ---------------------------------------------------------------------------
# Live-Dashboard
# ---------------------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HL Liquidationen</title>
<style>
 table.w{border-collapse:collapse;font-size:11px;min-width:min(100%,900px)}
 table.w th{color:#8b949e;font-weight:500;text-align:left;padding:4px 10px;
   border-bottom:1px solid #21262d;font-size:10px;text-transform:uppercase;white-space:nowrap}
 table.w td{padding:5px 10px;border-bottom:1px solid #161b22;white-space:nowrap}
 table.w th.n,table.w td.n{text-align:right;font-variant-numeric:tabular-nums}
 .mono{font-family:ui-monospace,monospace;font-size:10px;color:#8b949e}
 body{background:#0d1117;color:#c9d1d9;font:13px system-ui,sans-serif;margin:0;padding:16px}
 h1{font-size:17px;margin:0 0 3px} h2{font-size:13px;margin:0 0 2px;color:#e6edf3}
 .sub{color:#8b949e;font-size:11px;line-height:1.45}
 canvas{background:#01030a;border:1px solid #21262d;border-radius:6px;width:100%;
        display:block;cursor:crosshair}
 .row{display:flex;gap:16px;flex-wrap:wrap;margin-top:6px}
 .col{flex:1;min-width:330px}
 .panel{margin-top:20px}
 .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0 6px}
 label.lbl{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.4px}
 select,button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;
   border-radius:5px;padding:5px 9px;font-size:12px}
 button.tog{cursor:pointer;min-width:64px;font-weight:600}
 button.tog.on{background:#1f3d2a;border-color:#3ba55d;color:#7ee787}
 button.tog.off{background:#21262d;color:#8b949e}
 .kpi{display:flex;gap:20px;flex-wrap:wrap;margin:12px 0;padding:10px 12px;
      background:#161b22;border:1px solid #21262d;border-radius:6px}
 .kpi div span{display:block;color:#8b949e;font-size:10px;text-transform:uppercase;
               letter-spacing:.4px}
 .kpi b{font-size:17px;font-weight:600}
 .warn{color:#d29922;font-size:11px;margin-top:14px}
 .leg{display:flex;gap:14px;align-items:center;font-size:10px;color:#8b949e;
      margin:5px 0;flex-wrap:wrap}
 .sw{display:inline-block;width:11px;height:11px;border-radius:2px;
     margin-right:4px;vertical-align:-1px}
 .grad{width:110px;height:9px;border-radius:2px;background:linear-gradient(90deg,
   #04060f,#0a288c,#0096c8,#3cdcb4,#ffd60a,#ffffeb)}
 #tip{position:fixed;z-index:99;pointer-events:none;display:none;
      background:#0d1117;border:1px solid #30363d;border-radius:8px;
      padding:9px 12px;font-size:12px;box-shadow:0 6px 20px #000a;max-width:260px}
 #tip .t{color:#8b949e;font-size:10px;margin-bottom:5px}
 #tip .r{display:flex;justify-content:space-between;gap:16px;margin:2px 0}
 #tip .r i{font-style:normal;color:#8b949e}
 #tip .r b{font-weight:600}
</style></head><body>
<h1>Hyperliquid — Liquidationscluster</h1>
<div class="sub">Echte Positionen aus der Kette, keine Schätzung. Kurs und Cluster
 aktualisieren sich automatisch. Tippen oder mit der Maus über eine Grafik fahren
 zeigt die Werte an der Stelle.</div>

<div class="bar">
 <label class="lbl" for="coin">Coin</label><select id="coin"></select>
 <label class="lbl" for="tf">Chart-Raster</label>
 <select id="tf"><option value="1|120">1 min · 2 h</option>
  <option value="5|720" selected>5 min · 12 h</option>
  <option value="15|2880">15 min · 2 Tage</option>
  <option value="60|10080">1 h · 7 Tage</option></select>
 <button id="fvgtoggle" class="tog on" title="Fair Value Gaps ein- oder ausblenden">FVG an</button>
 <label class="lbl" for="fvg">Mindestgröße</label>
 <select id="fvg"><option value="0.0005" selected>0,05 %</option>
  <option value="0.001">0,1 %</option>
  <option value="0.0025">0,25 %</option></select>
 <label class="lbl" for="hours">Heatmap-Zeitraum</label>
 <select id="hours"><option value="6">6 h</option><option value="24" selected>24 h</option>
  <option value="72">3 Tage</option><option value="168">7 Tage</option></select>
 <button onclick="load()">Neu laden</button>
</div>

<div class="kpi" id="kpi"></div>

<div class="panel">
 <h2>Live-Chart — Kurs mit aktuellen Liquidationsmarken</h2>
 <div class="sub">Waagerechte Linien sind Preisniveaus, auf denen gehebelte
  Positionen zwangsliquidiert würden. Rechts steht je Linie das Notional und der
  Abstand zum Kurs.</div>
 <div class="leg">
  <span><i class="sw" style="background:#3ba55d"></i>Short-Liquidationen (über dem Kurs)</span>
  <span><i class="sw" style="background:#e04b4b"></i>Long-Liquidationen (unter dem Kurs)</span>
  <span>durchgezogen = ab halbem Stundenvolumen · gestrichelt = darunter</span>
  <span><i class="sw" style="background:rgba(59,165,93,.35);border:1px solid #3ba55d"></i>bullisches FVG</span>
  <span><i class="sw" style="background:rgba(224,75,75,.35);border:1px solid #e04b4b"></i>bärisches FVG</span>
  <span>kräftig = offen · blass = gefüllt</span>
 </div>
 <canvas id="chart" height="400"></canvas>
 <div class="sub" id="chartinfo" style="margin-top:5px"></div>
</div>

<div class="panel">
 <h2>Heatmap — Clusterentwicklung über die Zeit</h2>
 <div class="sub">Waagerecht die Zeit, senkrecht der Preis. Die Farbe zeigt den
  Rang einer Zelle unter allen anderen, nicht den Absolutwert — sonst würde ein
  einzelner Großcluster die Skala verzerren.</div>
 <div class="bar">
  <label class="lbl" for="thr">Liquiditäts-Schwelle</label>
  <input type="range" id="thr" min="0" max="97" value="70" style="width:190px">
  <span class="sub" id="thrval" style="min-width:92px">Perzentil 70</span>
 </div>
 <div class="leg">
  <span>schwach<i class="grad" style="margin:0 6px;display:inline-block"></i>stark</span>
  <span><i class="sw" style="background:#fff"></i>Kursverlauf</span>
  <span><i class="sw" style="background:#ff3b6b"></i>eingetretene Liquidation</span>
 </div>
 <canvas id="heat" height="430"></canvas>
 <div class="sub" id="heatinfo" style="margin-top:5px"></div>
</div>

<div class="panel">
 <h2>Momentaufnahme — Cluster rund um den aktuellen Kurs</h2>
 <div class="sub">Senkrecht der Preis, Balkenlänge das Notional. Die gestrichelte
  Linie markiert das halbe Stundenvolumen: Cluster links davon werden vom Markt
  eher geschluckt, ohne den Kurs zu bewegen.</div>
 <div class="leg">
  <span><i class="sw" style="background:#3ba55d"></i>Short-Liquidationen</span>
  <span><i class="sw" style="background:#e04b4b"></i>Long-Liquidationen</span>
  <span><i class="sw" style="background:#00e5ff"></i>aktueller Kurs</span>
 </div>
 <canvas id="depth" height="430"></canvas>
 <div class="sub" id="depthinfo" style="margin-top:5px"></div>
</div>

<div class="panel">
 <h2>Wale — Positionen ab 20 Mio. USD</h2>
 <div class="sub">Oben die Netto-Ausrichtung aller Wale (grün = mehr Long, rot = mehr
  Short) gegen den Kurs. Darunter jede aktuell offene Wal-Position mit Einstieg,
  Liquidationspreis und Abstand dorthin — über alle Coins, der gewählte ist hervorgehoben.
  Ganz unten die letzten Änderungen im gewählten Coin.
  <b>Vorsicht bei der Deutung:</b> Ein großer Short kann eine abgesicherte
  Spot-Position sein und sagt dann nichts über die Erwartung des Halters.</div>
 <div class="leg">
  <span><i class="sw" style="background:#3ba55d"></i>Long-Exposure</span>
  <span><i class="sw" style="background:#e04b4b"></i>Short-Exposure</span>
  <span><i class="sw" style="background:#fff"></i>Kurs</span>
  <span>▲ öffnet / stockt auf &nbsp; ▼ reduziert / schließt</span>
 </div>
 <canvas id="whalechart" height="260"></canvas>
 <div id="whaletable" style="margin-top:10px;overflow-x:auto"></div>
 <div id="whaleevents" class="sub" style="margin-top:8px"></div>
</div>

<div class="warn" id="warn"></div>
<div id="tip"></div>

<script>
const $=id=>document.getElementById(id);
let LAST=null, LAST_CHART=null, LAST_HEALTH=null, GEO={}, FVG_ON=true;
function setFvg(on){
 FVG_ON=on; const b=$('fvgtoggle'); if(!b) return;
 b.textContent=on?'FVG an':'FVG aus'; b.className='tog '+(on?'on':'off');
 if(LAST_CHART) drawChart(LAST_CHART);
}
function fillHealth(h){
 const b=$('kpi_backup'), d=$('kpi_db'); if(!b||!d) return;
 const bk=h.backup;
 if(!bk){ b.textContent='noch keines'; b.style.color='#d29922'; b.title='läuft 5 min nach Recorder-Start'; }
 else if(bk.ok){
  const age=(Date.now()/1000-bk.ts)/3600;
  b.textContent=hm(bk.ts); b.style.color=age<26?'#3ba55d':age<50?'#d29922':'#e04b4b';
  b.title=`vor ${age.toFixed(1)} h · ${(bk.size/1e6).toFixed(0)} MB · ${(bk.bars||0).toLocaleString('de-DE')} Bars`;
 } else {
  b.textContent='fehlgeschlagen'; b.style.color='#e04b4b';
  b.title=(bk.error||'')+(bk.last_good?` · gutes Backup von ${hm(bk.last_good)} liegt vor`:'');
 }
 if(h.db_ok===true){ d.textContent='OK'; d.style.color='#3ba55d';
  d.title=`quick_check bestanden · ${(h.db_size/1e6).toFixed(0)} MB · geprüft ${hm(h.checked_at)}`; }
 else if(h.db_ok===false){ d.textContent='BESCHÄDIGT'; d.style.color='#e04b4b';
  d.title=h.db_detail||'quick_check fehlgeschlagen'; }
 else { d.textContent='—'; d.style.color=''; }
}
async function loadHealth(){
 try{const r=await fetch('/health'); LAST_HEALTH=await r.json(); fillHealth(LAST_HEALTH);}catch(e){}
}
function fillIndicators(d){
 const t=$('kpi_trend'), r=$('kpi_rsi'), l=$('kpi_rsi_lbl');
 if(!t||!r) return;
 if(d.trend_60m==null){t.textContent='—';t.style.color='';}
 else{const v=d.trend_60m*100;
  t.textContent=(v>=0?'▲ +':'▼ ')+v.toFixed(2)+' %';
  t.style.color=Math.abs(v)<0.15?'#c9d1d9':v>0?'#3ba55d':'#e04b4b';}
 if(d.rsi14==null){r.textContent='—';r.style.color='';}
 else{const v=d.rsi14; r.textContent=v.toFixed(1);
  r.style.color=v>=70?'#e04b4b':v<=30?'#3ba55d':'#c9d1d9';
  r.title=v>=70?'überkauft':v<=30?'überverkauft':'neutral';}
 if(l) l.textContent='RSI 14 · '+(d.rsi_tf||'');
}
function fit(c){const r=c.getBoundingClientRect();const d=devicePixelRatio||1;
 c.width=r.width*d;c.height=c.height*d;const x=c.getContext('2d');x.scale(d,d);
 return [x,r.width,c.height/d];}
function fmt(n){return n>=1e9?(n/1e9).toFixed(2)+' Mrd':n>=1e6?(n/1e6).toFixed(1)+' Mio'
 :n>=1e3?(n/1e3).toFixed(0)+'k':(+n).toFixed(0);}
function px(n){return n>=1000?n.toLocaleString('de-DE',{maximumFractionDigits:2})
 :(+n).toFixed(4);}
function hm(ts){const d=new Date(ts*1000);
 return String(d.getDate()).padStart(2,'0')+'.'+String(d.getMonth()+1).padStart(2,'0')
  +'. '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}

// Farbverlauf dunkelblau -> blau -> cyan -> gelb -> weiss.
// Der dunkle Fuss ist entscheidend: ohne ihn leuchtet die ganze Flaeche.
const RAMP=[[0,4,10,32],[.22,10,40,140],[.45,0,150,200],
            [.68,60,220,180],[.85,255,214,10],[1,255,255,235]];
function heatColor(p){
 for(let i=1;i<RAMP.length;i++){
  if(p<=RAMP[i][0]){const a=RAMP[i-1],b=RAMP[i],t=(p-a[0])/(b[0]-a[0]||1);
   return [Math.round(a[1]+(b[1]-a[1])*t),Math.round(a[2]+(b[2]-a[2])*t),
           Math.round(a[3]+(b[3]-a[3])*t)];}}
 return [255,255,235];
}
// Perzentil-Zuordnung: entscheidend ist der Rang unter allen Zellen.
function makeScale(values){
 const v=values.filter(x=>x>0); if(!v.length) return null;
 const step=Math.max(1,Math.floor(v.length/20000)), s=[];
 for(let i=0;i<v.length;i+=step) s.push(v[i]);
 s.sort((a,b)=>a-b);
 const BINS=256, brk=new Float64Array(BINS);
 for(let i=0;i<BINS;i++) brk[i]=s[Math.min(s.length-1,Math.floor(i/BINS*s.length))];
 return {brk,min:s[0],max:s[s.length-1],
  pct(x){let lo=0,hi=BINS-1; if(x<=this.brk[0])return 0;
   while(lo<hi){const m=(lo+hi+1)>>1; if(this.brk[m]<=x)lo=m;else hi=m-1;}
   return lo/(BINS-1);}};
}

function drawChart(d){
 const [g,W,H]=fit($('chart')); g.clearRect(0,0,W,H);
 if(!d||!d.candles.length){g.fillStyle='#8b949e';
  g.fillText('noch keine Kursdaten — der Recorder muss erst laufen',12,24);return;}
 const C=d.candles, L=(d.levels||[]);
 const R=68,Lm=8,T=12,B=26, pw=W-Lm-R, ph=H-T-B;
 let lo=Math.min(...C.map(c=>c.l)), hi=Math.max(...C.map(c=>c.h));
 L.forEach(l=>{if(Math.abs(l.dist)<0.035){lo=Math.min(lo,l.px);hi=Math.max(hi,l.px);}});
 const pad=(hi-lo)*0.08||1; lo-=pad; hi+=pad;
 const Y=p=>T+(hi-p)/(hi-lo)*ph;
 const cw=Math.max(1.5,pw/C.length), bw=Math.max(1,cw*0.62);
 const FV=FVG_ON?(d.fvgs||[]):[];
 GEO.chart={C,L,Lm,T,pw,ph,cw,lo,hi,H,B,R,W,FV};

 g.strokeStyle='#161b22';g.lineWidth=1;
 for(let k=0;k<=5;k++){const y=T+ph*k/5;g.beginPath();g.moveTo(Lm,y);g.lineTo(W-R,y);g.stroke();}

 // Fair Value Gaps: von der mittleren Kerze bis zur Füllung, sonst bis zum Rand
 FV.forEach(f=>{const x0=Lm+f.i*cw, x1=f.fill_i!=null?Lm+f.fill_i*cw+cw:W-R;
  const y0=Y(f.top), y1=Y(f.bot); if(y1<T||y0>H-B) return;
  const open=f.fill_i==null, a=open?0.22:0.07;
  g.fillStyle=f.kind==='bull'?`rgba(59,165,93,${a})`:`rgba(224,75,75,${a})`;
  g.fillRect(x0,Math.max(T,y0),Math.max(1,x1-x0),Math.min(H-B,y1)-Math.max(T,y0));
  if(open){g.strokeStyle=f.kind==='bull'?'rgba(59,165,93,.7)':'rgba(224,75,75,.7)';
   g.lineWidth=1;g.setLineDash([2,3]);g.strokeRect(x0,y0,x1-x0,y1-y0);g.setLineDash([]);
   // Teilfüllung als Balken an der Innenkante
   if(f.fill_pct>0){g.fillStyle='rgba(255,255,255,.12)';
    const fh=(y1-y0)*f.fill_pct; g.fillRect(x0,f.kind==='bull'?y0:y1-fh,x1-x0,fh);}}});
 const mxN=Math.max(...L.map(l=>l.notional),1);
 L.forEach(l=>{const y=Y(l.px); if(y<T||y>H-B)return;
  const a=0.25+0.65*(l.notional/mxN);
  g.strokeStyle=l.side==='long'?`rgba(224,75,75,${a})`:`rgba(59,165,93,${a})`;
  g.lineWidth=l.x_vol>=0.5?2:1; g.setLineDash(l.x_vol>=0.5?[]:[3,3]);
  g.beginPath();g.moveTo(Lm,y);g.lineTo(W-R,y);g.stroke();g.setLineDash([]);
  g.fillStyle=l.side==='long'?'#e04b4b':'#3ba55d';g.font='9px system-ui';
  g.fillText(fmt(l.notional),W-R+3,y-2);
  g.fillStyle='#6e7681';g.fillText((l.dist*100).toFixed(1)+'%',W-R+3,y+8);});
 C.forEach((c,i)=>{const x=Lm+i*cw+cw/2, up=c.c>=c.o;
  g.strokeStyle=up?'#3ba55d':'#e04b4b'; g.fillStyle=g.strokeStyle; g.lineWidth=1;
  g.beginPath();g.moveTo(x,Y(c.h));g.lineTo(x,Y(c.l));g.stroke();
  const y1=Y(Math.max(c.o,c.c)),y2=Y(Math.min(c.o,c.c));
  g.fillRect(x-bw/2,y1,bw,Math.max(1,y2-y1));});
 if(d.price){const y=Y(d.price);
  g.strokeStyle='#00e5ff';g.lineWidth=1;g.setLineDash([5,4]);
  g.beginPath();g.moveTo(Lm,y);g.lineTo(W-R,y);g.stroke();g.setLineDash([]);
  g.fillStyle='#00e5ff';g.fillRect(W-R,y-7,R,14);
  g.fillStyle='#010409';g.font='bold 10px system-ui';g.fillText(px(d.price),W-R+3,y+3);}
 g.fillStyle='#6e7681';g.font='9px system-ui';
 for(let k=0;k<4;k++){const i=Math.floor(k/3*(C.length-1));
  g.fillText(hm(C[i].t), Lm+i*cw, H-8);}
 g.fillText('Preis (USD) rechts · Zeit unten', Lm, T+10);
 const te=(d.tick_events||[]);
 const openF=FV.filter(f=>f.fill_i==null);
 const nearest=openF.slice().sort((a,b)=>Math.abs(a.dist)-Math.abs(b.dist))[0];
 const fvtxt=FV.length?` · <b>FVG:</b> ${openF.length} offen (${openF.filter(f=>f.kind==='bull').length} bull / `
   +`${openF.filter(f=>f.kind==='bear').length} bear), ${FV.length-openF.length} gefüllt`
   +(nearest?` · nächstes ${nearest.kind} ${px(nearest.bot)}–${px(nearest.top)} `
     +`(${(nearest.dist*100).toFixed(2)} %, ${(nearest.fill_pct*100).toFixed(0)} % gefüllt)`:''):'';
 $('chartinfo').innerHTML=`${C.length} Kerzen · ${L.length} Liquidationsmarken`
  +` · Stundenvolumen ${fmt(d.vol_1h||0)} USD`+fvtxt
  +(te.length?`<br><b>Tick-Aufzeichnungen:</b> `+te.map(e=>
    `${hm(e.t_trigger/1000)} <span style="color:${e.move_pct<0?'#e04b4b':'#3ba55d'}">`
    +`${(e.move_pct*100).toFixed(2)} %</span> (${e.reason}, ${e.n_ticks.toLocaleString('de-DE')} Ticks, `
    +`${e.n_liqs} Liqs)`).join(' · '):'');
}

function drawHeat(d){
 const [g,W,H]=fit($('heat')); g.clearRect(0,0,W,H);
 if(!d||!d.times.length){g.fillStyle='#8b949e';g.fillText('keine Daten',12,22);return;}
 const P=d.price.map(p=>p[1]), lo=Math.min(...P)*0.94, hi=Math.max(...P)*1.06;
 const L=52,R=12,T=10,B=26, pw=W-L-R, ph=H-T-B;
 const t0=d.times[0], t1=d.times[d.times.length-1];
 const X=t=>L+(t-t0)/(t1-t0+1)*pw, Y=p=>T+(hi-p)/(hi-lo)*ph;
 g.fillStyle='#01030a'; g.fillRect(L,T,pw,ph);
 const flat=[]; d.grid.forEach(r=>r.forEach(v=>{if(v>0)flat.push(v)}));
 const sc=makeScale(flat), thr=(+$('thr').value)/100;
 const cw=Math.max(1.5,pw/d.times.length), chh=Math.max(1.5,ph*0.0025/((hi-lo)/hi));
 GEO.heat={d,L,T,pw,ph,lo,hi,t0,t1,sc,thr,H,B};
 let shown=0;
 d.grid.forEach((row,i)=>{const y=Y(d.levels[i]); if(y<T-4||y>H-B+4)return;
  row.forEach((v,j)=>{ if(v<=0||!sc)return;
   const p=sc.pct(v); if(p<thr)return;
   const q=Math.pow((p-thr)/(1-thr||1),2.6);
   const c=heatColor(q);
   g.fillStyle=`rgba(${c[0]},${c[1]},${c[2]},${0.35+0.65*q})`;
   g.fillRect(X(d.times[j]),y-chh/2,cw,chh); shown++;});});
 g.strokeStyle='#ffffff';g.lineWidth=1.6;g.beginPath();
 d.price.forEach((p,i)=>{const x=X(p[0]),y=Y(p[1]);i?g.lineTo(x,y):g.moveTo(x,y)});
 g.stroke();
 g.strokeStyle='#ff3b6b';g.lineWidth=1.2;
 d.liqs.forEach(l=>{const r=Math.max(2,Math.min(14,l[2]/6000));
  g.beginPath();g.arc(X(l[0]),Y(l[1]),r,0,7);g.stroke();});
 g.fillStyle='#8b949e';g.font='10px system-ui';
 for(let k=0;k<=4;k++){const p=lo+(hi-lo)*k/4;g.fillText(px(p).slice(0,9),4,Y(p)+3);}
 g.fillStyle='#6e7681';g.font='9px system-ui';
 for(let k=0;k<=4;k++){const t=t0+(t1-t0)*k/4;
  g.fillText(hm(t), Math.min(X(t),W-R-52), H-8);}
 g.fillStyle='#8b949e';g.font='9px system-ui';
 g.save();g.translate(11,T+ph/2);g.rotate(-Math.PI/2);
 g.textAlign='center';g.fillText('Preis (USD)',0,0);g.restore();
 $('heatinfo').textContent=sc
  ? `${shown.toLocaleString('de-DE')} von ${flat.length.toLocaleString('de-DE')} Zellen `
    +`sichtbar · unterhalb Perzentil ${Math.round(thr*100)} ausgeblendet `
    +`· Notional je Zelle ${fmt(sc.min)}–${fmt(sc.max)} USD` : '';
}

function drawDepth(d){
 const [g,W,H]=fit($('depth')); g.clearRect(0,0,W,H);
 if(!d||!d.rows.length){g.fillStyle='#8b949e';g.fillText('kein Snapshot',12,22);return;}
 const p0=d.price, rows=d.rows.filter(r=>r.bucket_px>p0*0.85&&r.bucket_px<p0*1.18);
 if(!rows.length){g.fillStyle='#8b949e';g.fillText('keine Cluster im Bereich',12,22);return;}
 const lo=p0*0.85, hi=p0*1.18, mx=Math.max(...rows.map(r=>r.notional));
 const L=62,R=10,T=10,B=26, pw=W-L-R, ph=H-T-B;
 const Y=p=>T+(hi-p)/(hi-lo)*ph;
 GEO.depth={rows,L,T,pw,ph,lo,hi,mx,p0,vol:d.vol_1h,H,B};
 rows.forEach(r=>{const w=r.notional/mx*pw, y=Y(r.bucket_px);
  g.fillStyle=r.side==='long'?'rgba(224,75,75,.85)':'rgba(59,165,93,.85)';
  g.fillRect(L,y-2,w,4);});
 if(d.vol_1h>0){const x=L+(d.vol_1h*0.5)/mx*pw;
  if(x<W-R){g.strokeStyle='#8b949e';g.setLineDash([4,4]);g.beginPath();
   g.moveTo(x,T);g.lineTo(x,H-B);g.stroke();g.setLineDash([]);
   g.fillStyle='#8b949e';g.font='9px system-ui';g.fillText('halbes Stundenvolumen',x+3,T+10);}}
 g.strokeStyle='#00e5ff';g.lineWidth=2;g.beginPath();
 g.moveTo(L,Y(p0));g.lineTo(W-R,Y(p0));g.stroke();
 g.fillStyle='#8b949e';g.font='10px system-ui';
 for(let k=0;k<=6;k++){const p=lo+(hi-lo)*k/6;g.fillText(px(p).slice(0,9),4,Y(p)+3);}
 g.fillStyle='#6e7681';g.font='9px system-ui';
 for(let k=0;k<=3;k++){const v=mx*k/3;g.fillText(fmt(v),L+pw*k/3,H-8);}
 g.fillText('Notional (USD)',L+pw/2-30,H-16);
 const up=rows.filter(r=>r.side==='short').reduce((a,b)=>a+b.notional,0);
 const dn=rows.filter(r=>r.side==='long').reduce((a,b)=>a+b.notional,0);
 $('depthinfo').textContent=`${rows.length} Cluster · über dem Kurs ${fmt(up)} USD `
  +`· darunter ${fmt(dn)} USD · Stand ${hm(d.ts)}`;
}

function drawWhales(d){
 const [g,W,H]=fit($('whalechart')); g.clearRect(0,0,W,H);
 const E=d.exposure||[], P=d.price||[], coin=$('coin').value;
 const C=d.current||[];
 if(!E.length){g.fillStyle='#8b949e';g.font='12px system-ui';
  g.fillText(C.length
   ? `Zeitreihe für ${coin} entsteht mit den nächsten Snapshots — die Tabelle unten ist schon gefüllt`
   : 'noch keine Wal-Positionen erfasst — Wale ab 20 Mio. USD erscheinen hier, sobald der Recorder sie findet',12,24);
  drawWhaleTable(d); return;}
 const L=8,R=64,T=10,B=24, pw=W-L-R, ph=H-T-B;
 const t0=E[0].ts, t1=E[E.length-1].ts;
 const X=t=>L+(t-t0)/(t1-t0+1)*pw;
 const mxE=Math.max(...E.map(e=>Math.max(e.long_usd,e.short_usd)),1);
 const Ye=v=>T+ph/2-v/mxE*(ph/2-4);
 // Exposure als gespiegelte Flaechen um die Mittellinie
 g.fillStyle='rgba(59,165,93,.55)'; g.beginPath(); g.moveTo(X(t0),Ye(0));
 E.forEach(e=>g.lineTo(X(e.ts),Ye(e.long_usd))); g.lineTo(X(t1),Ye(0)); g.fill();
 g.fillStyle='rgba(224,75,75,.55)'; g.beginPath(); g.moveTo(X(t0),Ye(0));
 E.forEach(e=>g.lineTo(X(e.ts),Ye(-e.short_usd))); g.lineTo(X(t1),Ye(0)); g.fill();
 g.strokeStyle='#30363d'; g.beginPath(); g.moveTo(L,Ye(0)); g.lineTo(W-R,Ye(0)); g.stroke();
 // Kurs auf eigener Skala rechts
 if(P.length){const pv=P.map(p=>p[1]), plo=Math.min(...pv), phi=Math.max(...pv);
  const Yp=p=>T+(phi-p)/(phi-plo||1)*ph;
  g.strokeStyle='#fff';g.lineWidth=1.3;g.beginPath();
  P.forEach((p,i)=>{const x=X(p[0]),y=Yp(p[1]); if(x<L||x>W-R)return; i?g.lineTo(x,y):g.moveTo(x,y)});
  g.stroke();
  g.fillStyle='#8b949e';g.font='9px system-ui';
  g.fillText(px(phi),W-R+3,T+9); g.fillText(px(plo),W-R+3,T+ph);
  // Ereignismarker
  (d.events||[]).forEach(ev=>{const x=X(ev.ts); if(x<L||x>W-R)return;
   const up=ev.event==='open'||ev.event==='add'||(ev.event==='flip');
   const near=P.reduce((a,b)=>Math.abs(b[0]-ev.ts)<Math.abs(a[0]-ev.ts)?b:a);
   const y=Yp(near[1]);
   g.fillStyle=ev.side==='long'?'#3ba55d':'#e04b4b';
   g.beginPath(); if(up){g.moveTo(x,y-9);g.lineTo(x-5,y-1);g.lineTo(x+5,y-1);}
   else{g.moveTo(x,y+9);g.lineTo(x-5,y+1);g.lineTo(x+5,y+1);} g.fill();});}
 g.fillStyle='#8b949e';g.font='9px system-ui';
 g.fillText('+'+fmt(mxE),L+2,T+9); g.fillText('-'+fmt(mxE),L+2,T+ph-2);
 g.fillStyle='#6e7681'; g.fillText(hm(t0),L,H-8); g.fillText(hm(t1),W-R-60,H-8);
 drawWhaleTable(d);
}

function drawWhaleTable(d){
 const C=d.current||[], coin=$('coin').value;
 const short=a=>a.slice(0,6)+'…'+a.slice(-4);
 const age=s=>{const h=(Date.now()/1000-s)/3600; return h<1?Math.round(h*60)+' min':h<48?h.toFixed(1)+' h':(h/24).toFixed(1)+' T';};
 $('whaletable').innerHTML=C.length?`<table class="w"><tr>
  <th>Wallet</th><th>Coin</th><th>Seite</th><th class="n">Wert</th><th class="n">Hebel</th>
  <th class="n">Einstieg</th><th class="n">Liq.-Preis</th><th class="n">Abstand</th>
  <th class="n">unreal. PnL</th><th>offen seit</th><th>Stand</th></tr>`
  +C.map(w=>{const dist=w.mark_px?(w.liq_px-w.mark_px)/w.mark_px*100:0;
   const dc=Math.abs(dist)<3?'#e04b4b':Math.abs(dist)<8?'#d29922':'#c9d1d9';
   const hl=w.coin===coin?' style="background:#161b22"':'';
   return `<tr${hl}><td class="mono">${short(w.addr)}</td>
    <td><b>${w.coin}</b></td>
    <td style="color:${w.side==='long'?'#3ba55d':'#e04b4b'}">${w.side}</td>
    <td class="n"><b>${fmt(w.pos_value)}</b></td><td class="n">${(+w.leverage).toFixed(0)}x</td>
    <td class="n">${px(w.entry_px)}</td><td class="n">${px(w.liq_px)}</td>
    <td class="n" style="color:${dc}">${dist>0?'+':''}${dist.toFixed(1)} %</td>
    <td class="n" style="color:${w.upnl>=0?'#3ba55d':'#e04b4b'}">${w.upnl>=0?'+':''}${fmt(w.upnl)}</td>
    <td>${w.opened_at?age(w.opened_at):'—'}</td><td class="mono">${age(w.ts)} alt</td></tr>`}).join('')
  +'</table>':'<div class="sub">Aktuell keine offene Position über der Schwelle.</div>';
 const other=C.filter(w=>w.coin!==coin).length;
 if(C.length&&!C.some(w=>w.coin===coin))
  $('whaletable').innerHTML=`<div class="sub" style="margin-bottom:6px">Kein Wal in ${coin} — `
   +`die ${other} unten halten ihre großen Positionen in anderen Coins.</div>`+$('whaletable').innerHTML;

 const ev=(d.events||[]).slice(-12).reverse();
 const name={open:'eröffnet',add:'stockt auf',reduce:'reduziert',close:'schließt',flip:'dreht auf'};
 $('whaleevents').innerHTML=ev.length?'<b>Letzte Änderungen:</b><br>'+ev.map(e=>
  `${hm(e.ts)} · <span class="mono">${short(e.addr)}</span> ${name[e.event]||e.event} `
  +`<span style="color:${e.side==='long'?'#3ba55d':'#e04b4b'}">${e.side}</span> `
  +`${e.delta_usd>=0?'+':''}${fmt(e.delta_usd)} USD bei ${px(e.mark_px)}`).join('<br>')
  :'noch keine Änderungen im Zeitraum';
}

// ---- Tooltip -------------------------------------------------------------
function showTip(ev, title, rows){
 const t=$('tip');
 t.innerHTML=`<div class="t">${title}</div>`+rows.map(
  r=>`<div class="r"><i>${r[0]}</i><b style="color:${r[2]||'#c9d1d9'}">${r[1]}</b></div>`).join('');
 t.style.display='block';
 const w=t.offsetWidth,h=t.offsetHeight;
 let x=ev.clientX+14, y=ev.clientY-h-10;
 if(x+w>innerWidth-8) x=ev.clientX-w-14;
 if(y<8) y=ev.clientY+18;
 t.style.left=x+'px'; t.style.top=y+'px';
}
function hideTip(){ $('tip').style.display='none'; }

function pos(ev,c){const r=c.getBoundingClientRect();
 return {x:ev.clientX-r.left, y:ev.clientY-r.top};}

function tipHeat(ev){
 const G=GEO.heat; if(!G) return hideTip();
 const {x,y}=pos(ev,$('heat'));
 if(x<G.L||x>G.L+G.pw||y<G.T||y>G.T+G.ph) return hideTip();
 const d=G.d;
 const t=G.t0+(x-G.L)/G.pw*(G.t1-G.t0);
 let j=0,best=1e18; d.times.forEach((tt,k)=>{const e=Math.abs(tt-t);if(e<best){best=e;j=k}});
 const p=G.hi-(y-G.T)/G.ph*(G.hi-G.lo);
 let i=0,bp=1e18; d.levels.forEach((lv,k)=>{const e=Math.abs(lv-p);if(e<bp){bp=e;i=k}});
 const v=d.grid[i][j], side=d.sides[i]?d.sides[i][j]:'';
 const pr=d.price.reduce((a,b)=>Math.abs(b[0]-t)<Math.abs(a[0]-t)?b:a);
 const rows=[['Zeit',hm(d.times[j])],['Preisniveau',px(d.levels[i])+' USD'],
             ['Kurs damals',px(pr[1])+' USD']];
 if(v>0){
  rows.push(['Notional',fmt(v)+' USD']);
  if(G.sc) rows.push(['Rang',(G.sc.pct(v)*100).toFixed(0)+'. Perzentil']);
  if(side) rows.push(['Art', side==='long'?'Long-Liquidationen':'Short-Liquidationen',
                      side==='long'?'#e04b4b':'#3ba55d']);
 } else rows.push(['Notional','kein Cluster','#6e7681']);
 showTip(ev,'Heatmap',rows);
}

function tipChart(ev){
 const G=GEO.chart; if(!G) return hideTip();
 const {x,y}=pos(ev,$('chart'));
 if(x<G.Lm||x>G.Lm+G.pw||y<G.T||y>G.T+G.ph) return hideTip();
 const i=Math.max(0,Math.min(G.C.length-1,Math.floor((x-G.Lm)/G.cw)));
 const c=G.C[i];
 const p=G.hi-(y-G.T)/G.ph*(G.hi-G.lo);
 const rows=[['Eröffnung',px(c.o)],['Hoch',px(c.h)],['Tief',px(c.l)],
             ['Schluss',px(c.c),c.c>=c.o?'#3ba55d':'#e04b4b'],
             ['Volumen',fmt(c.v)]];
 const near=G.L.filter(l=>Math.abs(l.px-p)/p<0.004)
             .sort((a,b)=>Math.abs(a.px-p)-Math.abs(b.px-p))[0];
 if(near) rows.push(['Marke '+px(near.px),fmt(near.notional)+' USD · '
   +near.x_vol.toFixed(2)+'x Vol', near.side==='long'?'#e04b4b':'#3ba55d']);
 const inGap=(G.FV||[]).filter(f=>p>=f.bot&&p<=f.top&&i>=f.i&&(f.fill_i==null||i<=f.fill_i))[0];
 if(inGap) rows.push(['FVG '+(inGap.kind==='bull'?'bullisch':'bärisch'),
   px(inGap.bot)+'–'+px(inGap.top)+' · '+(inGap.size_pct*100).toFixed(2)+' % · '
   +(inGap.fill_i==null?(inGap.fill_pct*100).toFixed(0)+' % gefüllt, offen':'gefüllt'),
   inGap.kind==='bull'?'#3ba55d':'#e04b4b']);
 showTip(ev,hm(c.t),rows);
}

function tipDepth(ev){
 const G=GEO.depth; if(!G) return hideTip();
 const {x,y}=pos(ev,$('depth'));
 if(y<G.T||y>G.T+G.ph) return hideTip();
 const p=G.hi-(y-G.T)/G.ph*(G.hi-G.lo);
 const r=G.rows.reduce((a,b)=>Math.abs(b.bucket_px-p)<Math.abs(a.bucket_px-p)?b:a);
 const dist=(r.bucket_px-G.p0)/G.p0;
 showTip(ev,'Cluster bei '+px(r.bucket_px)+' USD',[
  ['Notional',fmt(r.notional)+' USD'],
  ['Positionen',r.n_pos],
  ['Abstand zum Kurs',(dist*100).toFixed(2)+' %'],
  ['Anteil Stundenvol.',G.vol?(r.notional/G.vol).toFixed(2)+'x':'—'],
  ['Art',r.side==='long'?'Long-Liquidationen':'Short-Liquidationen',
   r.side==='long'?'#e04b4b':'#3ba55d']]);
}

for(const [id,fn] of [['heat',tipHeat],['chart',tipChart],['depth',tipDepth]]){
 const c=$(id);
 c.addEventListener('mousemove',fn);
 c.addEventListener('mouseleave',hideTip);
 c.addEventListener('click',fn);
 c.addEventListener('touchstart',e=>{e.preventDefault();fn(e.touches[0]);},{passive:false});
 c.addEventListener('touchmove',e=>{e.preventDefault();fn(e.touches[0]);},{passive:false});
 c.addEventListener('touchend',()=>setTimeout(hideTip,2500));
}

async function loadChart(){
 const coin=$('coin').value, [agg,mins]=$('tf').value.split('|');
 const fvg=$('fvg')?$('fvg').value:'0.0005';
 try{const r=await fetch(`/chart?coin=${coin}&agg=${agg}&minutes=${mins}&fvg=${fvg}`);
  LAST_CHART=await r.json();
  drawChart(LAST_CHART);
  fillIndicators(LAST_CHART);
 }catch(e){ console.error('Chart:', e); }
}
async function load(){
 loadChart();
 const coin=$('coin').value, h=$('hours').value;
 const r=await fetch(`/data?coin=${coin}&hours=${h}`); const d=await r.json();
 LAST=d.grid; drawHeat(d.grid); drawDepth(d.depth);
 try{const rw=await fetch(`/whales?coin=${coin}&hours=${Math.max(72,h)}`);
  drawWhales(await rw.json());}catch(e){}
 const dp=d.depth||{rows:[]};
 const up=dp.rows.filter(r=>r.side==='short').reduce((a,b)=>a+b.notional,0);
 const dn=dp.rows.filter(r=>r.side==='long').reduce((a,b)=>a+b.notional,0);
 $('kpi').innerHTML=`
  <div><span>Kurs</span><b>${dp.price?px(dp.price):'—'}</b></div>
  <div><span>Short-Liqs darüber</span><b style="color:#3ba55d">${fmt(up)}</b></div>
  <div><span>Long-Liqs darunter</span><b style="color:#e04b4b">${fmt(dn)}</b></div>
  <div><span>Volumen 1 h</span><b>${fmt(dp.vol_1h||0)}</b></div>
  <div><span>Adressen erfasst</span><b>${d.stats.addresses.toLocaleString('de-DE')}</b></div>
  <div><span>mit offener Position</span><b>${d.stats.positions.toLocaleString('de-DE')}</b></div>
  <div><span>60-Minuten-Trend</span><b id="kpi_trend">—</b></div>
  <div><span id="kpi_rsi_lbl">RSI 14</span><b id="kpi_rsi">—</b></div>
  <div><span>Letztes Backup</span><b id="kpi_backup">—</b></div>
  <div><span>Datenbank</span><b id="kpi_db">—</b></div>`;
 if(LAST_CHART) fillIndicators(LAST_CHART);
 if(LAST_HEALTH) fillHealth(LAST_HEALTH);
 $('warn').textContent=d.stats.days<14
  ? `Aufzeichnung läuft erst ${d.stats.days.toFixed(1)} Tage — die Abdeckung der `
    +`Adressen ist noch unvollständig, absolute Zahlen fallen zu niedrig aus.`:'';
}
$('thr').oninput=()=>{$('thrval').textContent='Perzentil '+$('thr').value;
                      if(LAST) drawHeat(LAST);};
fetch('/coins').then(r=>r.json()).then(cs=>{
 $('coin').innerHTML=cs.map(c=>`<option>${c}</option>`).join('');
 load(); loadHealth();
 setInterval(load,20000); setInterval(loadChart,5000); setInterval(loadHealth,3600000);
 $('coin').onchange=$('hours').onchange=load;
 $('tf').onchange=loadChart; $('fvg').onchange=loadChart;
 $('fvgtoggle').onclick=()=>setFvg(!FVG_ON);
 addEventListener('resize',()=>{loadChart(); if(LAST) drawHeat(LAST);});
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    db = DB_PATH

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        try:
            if u.path == "/":
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif u.path == "/coins":
                con = connect(self.db)
                cs = [r[0] for r in con.execute(
                    "SELECT DISTINCT coin FROM bars ORDER BY coin")]
                con.close()
                self._send(json.dumps(cs or ["BTC"]).encode(), "application/json")
            elif u.path == "/health":
                self._send(json.dumps(load_health(self.db)).encode(), "application/json")
            elif u.path == "/whales":
                q = parse_qs(u.query)
                con = connect(self.db)
                payload = load_whales(con, q.get("coin", ["BTC"])[0],
                                      int(q.get("hours", ["72"])[0]))
                con.close()
                self._send(json.dumps(payload).encode(), "application/json")
            elif u.path == "/chart":
                q = parse_qs(u.query)
                con = connect(self.db)
                payload = load_chart(
                    con,
                    q.get("coin", ["BTC"])[0],
                    int(q.get("minutes", ["240"])[0]),
                    int(q.get("agg", ["1"])[0]),
                    fvg_min=float(q.get("fvg", ["0.0005"])[0]))
                con.close()
                self._send(json.dumps(payload).encode(), "application/json")
            elif u.path == "/data":
                q = parse_qs(u.query)
                coin = q.get("coin", ["BTC"])[0]
                hours = int(q.get("hours", ["24"])[0])
                con = connect(self.db)
                span = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM bars").fetchone()
                stats = {
                    "addresses": con.execute("SELECT COUNT(*) FROM addresses").fetchone()[0],
                    "positions": con.execute(
                        "SELECT COUNT(*) FROM addresses WHERE notional>0").fetchone()[0],
                    "days": ((span["b"] - span["a"]) / 86400) if span["a"] else 0,
                }
                payload = {"grid": load_grid(con, coin, hours),
                           "depth": load_depth(con, coin), "stats": stats}
                con.close()
                self._send(json.dumps(payload).encode(), "application/json")
            else:
                self.send_error(404)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, *a) -> None:
        pass


def serve(db: str, port: int, host: str = "127.0.0.1") -> None:
    Handler.db = db
    try:
        srv = HTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno == 98:                      # EADDRINUSE
            print(f"Port {port} ist bereits belegt.\n")
            print("Meist läuft das Dashboard schon — als systemd-Dienst oder aus")
            print("einem früheren Aufruf. Beides zusammen geht nicht.\n")
            print("  Wer hält den Port?     sudo ss -ltnp | grep " + str(port))
            print("  Dienst-Status:         systemctl status hl-dashboard")
            print("  Dienst anhalten:       sudo systemctl stop hl-dashboard")
            print(f"  oder anderen Port:     --port {port + 1}")
            raise SystemExit(2)
        raise
    if host not in ("127.0.0.1", "localhost"):
        print("Achtung: Das Dashboard ist im Netzwerk erreichbar und hat keine "
              "Authentifizierung. Nur im vertrauenswürdigen Heimnetz nutzen.")
    print(f"Dashboard: http://{host}:{port}  (Strg-C beendet)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbeendet")


# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Visualisierung der Liquidationsdaten")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--coin", default="BTC")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--depth", action="store_true", help="aktuellen Snapshot zeichnen")
    p.add_argument("--threshold", type=float, default=0.70,
                   help="Perzentil-Schwelle: darunter wird nichts gezeichnet")
    p.add_argument("--out", default=None)
    p.add_argument("--serve", action="store_true")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 macht das Dashboard im LAN erreichbar (Pi!)")
    args = p.parse_args()

    if args.serve:
        serve(args.db, args.port, args.host)
        return

    con = connect(args.db)
    if args.depth:
        plot_depth(con, args.coin, args.out or f"{args.coin}_depth.png")
    else:
        plot_heatmap(con, args.coin, args.hours,
                     args.out or f"{args.coin}_heatmap.png", args.threshold)
    con.close()


if __name__ == "__main__":
    main()
