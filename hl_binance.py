#!/usr/bin/env python3
"""
hl_binance.py — Binance-Futures-Daten als Ergänzung zur Hyperliquid-Aufzeichnung.

Schreibt in dieselbe SQLite-Datei wie hl_recorder.py, aber in eigene Tabellen
mit Präfix bnc_. Die Quellen werden bewusst NICHT vermischt: Hyperliquid liefert
gemessene Positionen, Binance liefert Marktaggregate und Liquidationsereignisse.

Was rückwirkend geht (30 Tage):
  - Open Interest (Menge und Notional)
  - Long/Short-Verhältnis der Top-Konten und aller Konten
  - Taker-Buy/Sell-Verhältnis
  - Kerzen (die gehen sogar deutlich weiter zurück)

Was NICHT rückwirkend geht:
  - Liquidationen. Es gibt keinen öffentlichen Verlaufsendpunkt. Der Stream
    !forceOrder@arr sammelt nur vorwärts.

Und eine Einschränkung des Streams, die Binance selbst dokumentiert: Pro Symbol
wird nur die größte Order je 1000 ms gepusht. Während einer Kaskade untertreibt
der Stream also deutlich. Zeitpunkt und Richtung sind verlässlich, das
Gesamtvolumen nicht.

    python hl_binance.py --backfill            # 30 Tage nachladen
    python hl_binance.py                       # Liquidationen live mitschneiden
    python hl_binance.py --status
    python hl_binance.py --selftest            # Parser ohne Netz prüfen

Kein API-Key nötig — alle genutzten Endpunkte sind öffentlich.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict

FAPI = "https://fapi.binance.com"
WS = "wss://fstream.binance.com/ws/!forceOrder@arr"
DB_PATH = "hl_liq.db"
COINS = ["BTC", "ETH", "HYPE", "ZEC", "XMR", "PAXG"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS bnc_liquidations (
    ts       INTEGER,
    coin     TEXT,
    symbol   TEXT,
    side     TEXT,     -- 'long'  = Long wurde liquidiert (Zwangsverkauf)
                       -- 'short' = Short wurde liquidiert (Zwangskauf)
    price    REAL,
    qty      REAL,
    notional REAL,
    PRIMARY KEY (ts, symbol, side, price, qty)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_bl_coin_ts ON bnc_liquidations(coin, ts);

CREATE TABLE IF NOT EXISTS bnc_metrics (
    ts     INTEGER,
    coin   TEXT,
    metric TEXT,
    value  REAL,
    PRIMARY KEY (ts, coin, metric)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_bm_coin ON bnc_metrics(coin, metric, ts);

-- Tageskerzen für gleitende Durchschnitte (50/200 Tage, 50/200 Wochen).
-- Quelle je Coin: Binance-Futures, sonst Binance-Spot, sonst Hyperliquid.
-- PRIMARY KEY (ts, coin) -- ein Tag je Coin, Nachladen ersetzt statt zu doppeln.
CREATE TABLE IF NOT EXISTS daily_bars (
    ts     INTEGER,            -- Tagesbeginn UTC, Sekunden
    coin   TEXT,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    source TEXT,
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bnc_bars (
    ts     INTEGER,
    coin   TEXT,
    open   REAL, high REAL, low REAL, close REAL,
    volume REAL,
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;
"""


def db_connect(path: str = DB_PATH, synchronous: str = "NORMAL") -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA synchronous={synchronous}")
    con.execute("PRAGMA temp_store=MEMORY")      # Sortierpuffer im RAM, nicht auf der SD-Karte
    con.executescript(SCHEMA)
    return con


def get(path: str, params: dict | None = None, retries: int = 4, base: str = FAPI):
    from urllib.parse import urlencode
    url = f"{base}{path}" + (f"?{urlencode(params)}" if params else "")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hl-recorder/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):           # Rate-Limit / Bann
                time.sleep(5 * (attempt + 1))
                last = e
                continue
            if e.code == 451:                  # geografisch gesperrt
                raise RuntimeError(
                    "Binance verweigert den Zugriff aus dieser Region (HTTP 451)."
                ) from e
            last = e
            break
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Binance-Request {path} fehlgeschlagen: {last}")


# ---------------------------------------------------------------------------
# Reine Parser — ohne Netz testbar
# ---------------------------------------------------------------------------

def parse_force_order(msg: dict, sym2coin: dict[str, str]) -> tuple | None:
    """
    forceOrder-Event in eine Datenbankzeile umwandeln.

    Binance nennt die Seite der ZWANGSORDER, nicht die der Position:
    SELL bedeutet, dass ein Long zwangsverkauft wurde.
    """
    o = msg.get("o") or {}
    sym = o.get("s")
    coin = sym2coin.get(sym)
    if not coin:
        return None
    try:
        qty = float(o.get("l") or o.get("q") or 0)      # ausgeführte Menge
        px = float(o.get("ap") or o.get("p") or 0)      # Durchschnittspreis
        ts = int(o.get("T") or msg.get("E") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or px <= 0 or ts <= 0:
        return None
    side = "long" if str(o.get("S")).upper() == "SELL" else "short"
    return (ts, coin, sym, side, px, qty, px * qty)


def parse_oi(rows: list, coin: str) -> list[tuple]:
    out = []
    for r in rows:
        try:
            ts = int(r["timestamp"])
            out.append((ts, coin, "open_interest", float(r["sumOpenInterest"])))
            out.append((ts, coin, "oi_notional", float(r["sumOpenInterestValue"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_ratio(rows: list, coin: str, metric: str, field: str) -> list[tuple]:
    out = []
    for r in rows:
        try:
            out.append((int(r["timestamp"]), coin, metric, float(r[field])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_klines(rows: list, coin: str) -> list[tuple]:
    out = []
    for k in rows:
        try:
            out.append((int(k[0]) // 1000, coin, float(k[1]), float(k[2]),
                        float(k[3]), float(k[4]), float(k[5])))
        except (IndexError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Symbolauflösung
# ---------------------------------------------------------------------------

def resolve_symbols(coins: list[str]) -> dict[str, str]:
    """Coin -> Binance-Perp-Symbol. Nicht gelistete Coins fallen weg."""
    info = get("/fapi/v1/exchangeInfo")
    live = {}
    for s in info.get("symbols", []):
        if (s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"):
            live[s["baseAsset"].upper()] = s["symbol"]

    out = {}
    for c in coins:
        c = c.upper()
        if c in live:
            out[c] = live[c]
            print(f"  {c:<6} -> {live[c]}")
        else:
            print(f"  {c:<6} -> auf Binance-Futures nicht verfügbar, übersprungen")
    return out


# ---------------------------------------------------------------------------
# Tageskerzen für gleitende Durchschnitte
# ---------------------------------------------------------------------------

SPOT = "https://api.binance.com"
HL_INFO = "https://api.hyperliquid.xyz/info"
DAY = 86_400


def fetch_daily(coin: str, start_ts: int, futures_sym: str | None) -> tuple[list[tuple], str]:
    """
    Tageskerzen ab start_ts. Reihenfolge der Quellen:
    Binance-Futures (falls gelistet) -> Binance-Spot -> Hyperliquid candleSnapshot.
    Liefert (Zeilen, Quelle). Zeilen: (ts, coin, o, h, l, c, v, source).
    """
    now_ms = int(time.time() * 1000)

    def binance(base: str, path: str, sym: str, source: str):
        rows, cursor = [], start_ts * 1000
        while cursor < now_ms:
            chunk = get(path, {"symbol": sym, "interval": "1d", "limit": 1000,
                               "startTime": cursor}, base=base)
            if not isinstance(chunk, list) or not chunk:
                break
            for k in chunk:
                rows.append((int(k[0]) // 1000, coin, float(k[1]), float(k[2]),
                             float(k[3]), float(k[4]), float(k[5]), source))
            newest = int(chunk[-1][0])
            if newest <= cursor:
                break
            cursor = newest + DAY * 1000
            time.sleep(0.25)
        return rows

    if futures_sym:
        try:
            rows = binance(FAPI, "/fapi/v1/klines", futures_sym, "binance-futures")
            if rows:
                return rows, "binance-futures"
        except Exception as e:
            print(f"    {coin}: Futures-Kerzen nicht abrufbar ({e}), versuche Spot")
    try:
        rows = binance(SPOT, "/api/v3/klines", f"{coin}USDT", "binance-spot")
        if rows:
            return rows, "binance-spot"
    except Exception:
        pass
    # Hyperliquid: auch für Coins, die Binance nicht (mehr) führt
    try:
        body = json.dumps({"type": "candleSnapshot",
                           "req": {"coin": coin, "interval": "1d",
                                   "startTime": start_ts * 1000, "endTime": now_ms}}).encode()
        req = urllib.request.Request(HL_INFO, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode())
        rows = [(int(c["t"]) // 1000, coin, float(c["o"]), float(c["h"]), float(c["l"]),
                 float(c["c"]), float(c["v"]), "hyperliquid") for c in data]
        return rows, "hyperliquid"
    except Exception as e:
        print(f"    {coin}: auch Hyperliquid liefert keine Tageskerzen ({e})")
        return [], "keine"


def backfill_daily(con: sqlite3.Connection, coins: list[str], years: float = 5,
                   coin2sym: dict[str, str] | None = None) -> None:
    """
    Tageskerzen nachladen, nur was fehlt: ab dem letzten gespeicherten Tag minus
    zwei Tage (die laufende Kerze wird so ersetzt). INSERT OR REPLACE auf den
    Primärschlüssel (ts, coin) -- doppelte Einträge sind ausgeschlossen.
    """
    if coin2sym is None:
        try:
            coin2sym = resolve_symbols(coins)
        except Exception as e:
            print(f"  Symbolauflösung fehlgeschlagen ({e}), nutze Spot/Hyperliquid")
            coin2sym = {}
    horizon = int(time.time()) - int(years * 365.25 * DAY)
    total = 0
    for coin in coins:
        coin = coin.upper()
        last = con.execute("SELECT MAX(ts) FROM daily_bars WHERE coin=?", (coin,)).fetchone()[0]
        start = max(horizon, (last or 0) - 2 * DAY) if last else horizon
        rows, source = fetch_daily(coin, start, coin2sym.get(coin))
        if not rows:
            continue
        con.executemany("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        n = con.execute("SELECT COUNT(*), MIN(ts) FROM daily_bars WHERE coin=?", (coin,)).fetchone()
        total += len(rows)
        print(f"  {coin:<6} {len(rows):>5} Kerzen geladen ({source}), "
              f"gesamt {n[0]} Tage seit {time.strftime('%Y-%m-%d', time.gmtime(n[1]))}")
    print(f"  {total} Tageskerzen aktualisiert")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

# (Endpunkt, Metrikname, Feld im Ergebnis)
RATIOS = [
    ("/futures/data/topLongShortPositionRatio", "top_ls_position", "longShortRatio"),
    ("/futures/data/topLongShortAccountRatio", "top_ls_account", "longShortRatio"),
    ("/futures/data/globalLongShortAccountRatio", "global_ls_account", "longShortRatio"),
    ("/futures/data/takerlongshortRatio", "taker_ls", "buySellRatio"),
]


def backfill(con: sqlite3.Connection, coins: list[str], period: str = "15m",
             days: int = 30) -> None:
    print(f"Backfill {days} Tage, Raster {period}\n")
    sym = resolve_symbols(coins)
    if not sym:
        print("Keine nutzbaren Symbole.")
        return

    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    step_ms = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "4h": 14_400_000}[period]
    total = 0

    for coin, s in sym.items():
        print(f"\n{coin} ({s})")

        # Open Interest — je Abruf maximal 500 Punkte
        rows, cursor = [], start
        while cursor < now:
            chunk = get("/futures/data/openInterestHist",
                        {"symbol": s, "period": period, "limit": 500,
                         "startTime": cursor, "endTime": now})
            if not chunk:
                break
            rows += chunk
            newest = max(int(r["timestamp"]) for r in chunk)
            if newest <= cursor:
                break
            cursor = newest + step_ms
            time.sleep(0.3)
        data = parse_oi(rows, coin)
        con.executemany("INSERT OR REPLACE INTO bnc_metrics VALUES (?,?,?,?)", data)
        print(f"  Open Interest   {len(rows):>5} Punkte")
        total += len(data)

        # Positionierungs-Verhältnisse
        for path, metric, field in RATIOS:
            rows, cursor = [], start
            while cursor < now:
                try:
                    chunk = get(path, {"symbol": s, "period": period, "limit": 500,
                                       "startTime": cursor, "endTime": now})
                except RuntimeError as e:
                    print(f"  {metric:<16} nicht abrufbar ({e})")
                    chunk = []
                    break
                if not chunk:
                    break
                rows += chunk
                newest = max(int(r["timestamp"]) for r in chunk)
                if newest <= cursor:
                    break
                cursor = newest + step_ms
                time.sleep(0.3)
            data = parse_ratio(rows, coin, metric, field)
            con.executemany("INSERT OR REPLACE INTO bnc_metrics VALUES (?,?,?,?)", data)
            print(f"  {metric:<16}{len(rows):>5} Punkte")
            total += len(data)

        # Kerzen als Preiskontext
        rows, cursor = [], start
        while cursor < now:
            chunk = get("/fapi/v1/klines",
                        {"symbol": s, "interval": period, "limit": 1500,
                         "startTime": cursor, "endTime": now})
            if not chunk:
                break
            rows += chunk
            newest = int(chunk[-1][0])
            if newest <= cursor:
                break
            cursor = newest + step_ms
            time.sleep(0.3)
        bars = parse_klines(rows, coin)
        con.executemany("INSERT OR REPLACE INTO bnc_bars VALUES (?,?,?,?,?,?,?)", bars)
        print(f"  Kerzen          {len(bars):>5} Punkte")

        con.commit()

    print(f"\n{total} Metrikwerte gespeichert.")
    print("Liquidationen lassen sich nicht nachladen — dafür muss der Live-Modus laufen.")


# ---------------------------------------------------------------------------
# Live: Liquidationen + laufender OI
# ---------------------------------------------------------------------------

class Live:
    def __init__(self, con: sqlite3.Connection, sym2coin: dict[str, str],
                 coin2sym: dict[str, str], oi_interval: int = 600):
        self.con = con
        self.oi_interval = oi_interval
        self.extra_coins: list[str] = []      # Coins ohne Binance-Perp, Tageskerzen via Spot/HL
        self.sym2coin = sym2coin
        self.coin2sym = coin2sym
        self.stop = asyncio.Event()
        self.stats = defaultdict(int)
        self.pending = 0
        self.last_commit = time.time()

    async def run_ws(self) -> None:
        import websockets
        backoff = 1
        while not self.stop.is_set():
            try:
                async with websockets.connect(WS, ping_interval=20) as ws:
                    print(f"Binance-Stream verbunden, filtere auf "
                          f"{', '.join(sorted(self.coin2sym))}.")
                    backoff = 1
                    while not self.stop.is_set():
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
                        self.stats["events"] += 1
                        row = parse_force_order(msg, self.sym2coin)
                        if row:
                            self.con.execute(
                                "INSERT OR IGNORE INTO bnc_liquidations "
                                "VALUES (?,?,?,?,?,?,?)", row)
                            self.stats["stored"] += 1
                            self.stats["notional"] += row[6]
                            self.pending += 1
                        # Zeitnah committen: Kaskaden sind genau die Momente,
                        # in denen ein Stromausfall am meisten kosten würde.
                        if self.pending >= 25 or (
                                self.pending and time.time() - self.last_commit > 10):
                            self.con.commit()
                            self.pending = 0
                            self.last_commit = time.time()
            except asyncio.TimeoutError:
                print("Stream still (600 s ohne Nachricht), reconnect …")
            except Exception as e:
                if not self.stop.is_set():
                    print(f"Stream-Fehler: {e} — reconnect in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def run_oi(self) -> None:
        """Open Interest im eingestellten Takt fortschreiben."""
        while not self.stop.is_set():
            for coin, s in self.coin2sym.items():
                try:
                    r = get("/fapi/v1/openInterest", {"symbol": s})
                    ts = int(r.get("time", time.time() * 1000))
                    self.con.execute(
                        "INSERT OR REPLACE INTO bnc_metrics VALUES (?,?,?,?)",
                        (ts // 1000, coin, "open_interest", float(r["openInterest"])))
                except Exception:
                    pass
            self.con.commit()
            for _ in range(self.oi_interval):
                if self.stop.is_set():
                    return
                await asyncio.sleep(1)

    async def run_report(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.oi_interval)
            if self.stop.is_set():
                return
            self.con.commit()
            try:
                self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            print(f"[{time.strftime('%H:%M:%S')}] Events {self.stats['events']:>7} | "
                  f"gespeichert {self.stats['stored']:>6} | "
                  f"Notional {self.stats['notional']/1e6:>8.1f} Mio USD")

    async def run_daily(self) -> None:
        """Tageskerzen beim Start und danach alle 6 Stunden nachziehen."""
        await asyncio.sleep(20)
        while not self.stop.is_set():
            try:
                print(f"[{time.strftime('%H:%M:%S')}] Tageskerzen nachladen …")
                await asyncio.to_thread(backfill_daily, self.con, list(self.coin2sym) + self.extra_coins,
                                        5, self.coin2sym)
            except Exception as e:
                print(f"  Tageskerzen: {e}")
            for _ in range(6 * 3600):
                if self.stop.is_set():
                    return
                await asyncio.sleep(1)

    async def run(self) -> None:
        tasks = [asyncio.create_task(t) for t in
                 (self.run_ws(), self.run_oi(), self.run_report(), self.run_daily())]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.con.commit()
        print("\nSauber beendet.")


# ---------------------------------------------------------------------------

def status(con: sqlite3.Connection) -> None:
    q = lambda s, *a: con.execute(s, a).fetchone()
    print("Binance-Daten:")
    for coin, n, src, first in con.execute(
            "SELECT coin, COUNT(*), source, MIN(ts) FROM daily_bars GROUP BY coin ORDER BY coin"):
        print(f"  Tageskerzen {coin:<6} {n:>5} seit {time.strftime('%Y-%m-%d', time.gmtime(first))} ({src})")
    print(f"  Liquidationen   {q('SELECT COUNT(*) FROM bnc_liquidations')[0]:,}")
    print(f"  Metrikwerte     {q('SELECT COUNT(*) FROM bnc_metrics')[0]:,}")
    print(f"  Kerzen          {q('SELECT COUNT(*) FROM bnc_bars')[0]:,}")
    r = q("SELECT MIN(ts), MAX(ts) FROM bnc_metrics")
    if r[0]:
        print(f"  Metrik-Zeitraum {(r[1]-r[0])/86400:.1f} Tage")
    for coin, n, ntl in con.execute(
            "SELECT coin, COUNT(*), SUM(notional) FROM bnc_liquidations "
            "GROUP BY coin ORDER BY 3 DESC"):
        print(f"    {coin:<6} {n:>7} Liqs, {(ntl or 0)/1e6:>9.1f} Mio USD")


def selftest() -> bool:
    """Parser gegen Beispielantworten prüfen, ohne Netz."""
    ok = True

    row = parse_force_order({"e": "forceOrder", "E": 1568014460893, "o": {
        "s": "BTCUSDT", "S": "SELL", "q": "0.014", "p": "9910",
        "ap": "9910", "l": "0.014", "T": 1568014460893}}, {"BTCUSDT": "BTC"})
    exp = (1568014460893, "BTC", "BTCUSDT", "long", 9910.0, 0.014, 138.74)
    if row and row[:6] == exp[:6] and abs(row[6] - exp[6]) < 1e-6:
        print("  [ok]  forceOrder: SELL wird als Long-Liquidation erkannt")
    else:
        print(f"  [FEHLER] forceOrder: {row}")
        ok = False

    row = parse_force_order({"o": {"s": "ETHUSDT", "S": "BUY", "l": "2",
                                   "ap": "3000", "T": 1}}, {"ETHUSDT": "ETH"})
    if row and row[3] == "short" and row[6] == 6000:
        print("  [ok]  forceOrder: BUY wird als Short-Liquidation erkannt")
    else:
        print(f"  [FEHLER] forceOrder BUY: {row}")
        ok = False

    if parse_force_order({"o": {"s": "DOGEUSDT", "S": "BUY", "l": "1",
                                "ap": "1", "T": 1}}, {"BTCUSDT": "BTC"}) is None:
        print("  [ok]  fremde Symbole werden verworfen")
    else:
        print("  [FEHLER] Filter greift nicht")
        ok = False

    oi = parse_oi([{"symbol": "BTCUSDT", "sumOpenInterest": "20403.637",
                    "sumOpenInterestValue": "150570784.078",
                    "timestamp": 1583127900000}], "BTC")
    if len(oi) == 2 and oi[0][3] == 20403.637 and oi[1][2] == "oi_notional":
        print("  [ok]  openInterestHist wird korrekt zerlegt")
    else:
        print(f"  [FEHLER] parse_oi: {oi}")
        ok = False

    rt = parse_ratio([{"longShortRatio": "1.8105", "longAccount": "0.6442",
                       "shortAccount": "0.3558", "timestamp": 1583139600000}],
                     "BTC", "top_ls_position", "longShortRatio")
    if rt and abs(rt[0][3] - 1.8105) < 1e-9:
        print("  [ok]  Long/Short-Ratio wird korrekt zerlegt")
    else:
        print(f"  [FEHLER] parse_ratio: {rt}")
        ok = False

    kl = parse_klines([[1591258320000, "9640.7", "9642.4", "9640.6", "9642.0",
                        "206.429", 1591258379999, "0", 0, "0", "0", "0"]], "BTC")
    if kl and kl[0][0] == 1591258320 and kl[0][5] == 9642.0:
        print("  [ok]  Kerzen werden korrekt zerlegt (ms -> s)")
    else:
        print(f"  [FEHLER] parse_klines: {kl}")
        ok = False

    # Schreibpfad gegen eine temporäre Datenbank
    import tempfile, os
    tmp = os.path.join(tempfile.gettempdir(), f"_bnc_{os.getpid()}.db")
    try:
        con = db_connect(tmp)
        con.executemany("INSERT OR IGNORE INTO bnc_liquidations VALUES (?,?,?,?,?,?,?)",
                        [exp, exp])          # doppelt -> muss ignoriert werden
        con.executemany("INSERT OR REPLACE INTO bnc_metrics VALUES (?,?,?,?)", oi)
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM bnc_liquidations").fetchone()[0]
        con.close()
        if n == 1:
            print("  [ok]  Datenbankschema und Duplikatschutz")
        else:
            print(f"  [FEHLER] Duplikatschutz: {n} Zeilen")
            ok = False
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + suffix)
            except OSError:
                pass
    return ok


def main() -> None:
    p = argparse.ArgumentParser(description="Binance-Daten für die Liquidationsanalyse")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--coins", nargs="+", default=COINS)
    p.add_argument("--backfill", action="store_true", help="30 Tage nachladen")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--period", default="15m",
                   choices=["5m", "15m", "30m", "1h", "4h"])
    p.add_argument("--backfill-daily", action="store_true",
                   help="Tageskerzen für MA 50/200 Tage und 50/200 Wochen nachladen")
    p.add_argument("--years", type=float, default=5, help="Rückblick für --backfill-daily")
    p.add_argument("--oi-interval", type=int, default=600,
                   help="Abstand der Open-Interest-Abrufe in Sekunden")
    p.add_argument("--durable", action="store_true",
                   help="synchronous=FULL: kein Verlust bei Stromausfall")
    p.add_argument("--status", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        print("Parser-Selbsttest (ohne Netz)")
        sys.exit(0 if selftest() else 1)

    con = db_connect(args.db, "FULL" if args.durable else "NORMAL")

    if args.status:
        status(con)
        return

    if args.backfill_daily:
        print("Tageskerzen für gleitende Durchschnitte:")
        backfill_daily(con, args.coins, args.years)
        return

    if args.backfill:
        if args.days > 30:
            print("Hinweis: Open Interest und Ratios gibt es nur 30 Tage "
                  "rückwirkend. Nur die Kerzen reichen weiter zurück.")
        backfill(con, args.coins, args.period, args.days)
        return

    print("Löse Symbole auf …")
    coin2sym = resolve_symbols(args.coins)
    if not coin2sym:
        print("Nichts zu tun.")
        return
    sym2coin = {v: k for k, v in coin2sym.items()}

    live = Live(con, sym2coin, coin2sym, args.oi_interval)
    live.extra_coins = [c.upper() for c in args.coins if c.upper() not in coin2sym]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, live.stop.set)
        except NotImplementedError:
            pass
    print("\nLiquidationen werden mitgeschnitten. Abbruch mit Strg-C.")
    print("Hinweis: Binance pusht je Symbol nur die größte Order pro Sekunde.")
    print("Während Kaskaden ist das gespeicherte Volumen daher eine Untergrenze.\n")
    try:
        loop.run_until_complete(live.run())
    except KeyboardInterrupt:
        live.stop.set()
    finally:
        con.close()


if __name__ == "__main__":
    main()
