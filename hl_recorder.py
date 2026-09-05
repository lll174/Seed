#!/usr/bin/env python3
"""
hl_recorder.py — Zeichnet Hyperliquid-Liquidationscluster live auf und labelt sie.

Warum: Historische Positionsdaten gibt es öffentlich nicht. Wer testen will, ob
Liquidationscluster Vorhersagekraft haben, muss selbst vorwärts aufzeichnen.

Wie es funktioniert:
  1. WebSocket 'trades' liefert zu jedem Trade die beteiligten Wallet-Adressen
     -> daraus wächst über die Zeit ein Adressregister.
  2. 'clearinghouseState' pro Adresse liefert offene Positionen inkl. echtem
     liquidationPx -> keine Schätzung wie bei CEX-Heatmaps.
  3. Alle Liquidationspreise werden in Preis-Buckets aggregiert -> Heatmap.
  4. Parallel laufen 1-Minuten-Bars aus dem Trade-Feed mit, damit später
     Forward-Returns berechnet werden können.
  5. Echte Liquidations-Fills (Feld 'liquidation' im Trade) werden separat geloggt.

Modi:
    python hl_recorder.py --coins BTC ETH HYPE          # aufzeichnen
    python hl_recorder.py --simulate                    # Testdaten erzeugen
    python hl_recorder.py --analyze --coin BTC          # Cluster-Events labeln
    python hl_recorder.py --status                      # DB-Überblick

Abhängigkeiten: pip install websockets aiohttp pandas numpy
"""

from __future__ import annotations

import argparse
import asyncio
import random
import json
import math
import os
import random
import signal
import sqlite3
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass

API = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"
DB_PATH = "hl_liq.db"

# clearinghouseState hat Rate-Limit-Gewicht 2; das IP-Budget liegt bei 1200/min.
# Wir bleiben bewusst deutlich darunter, um Platz für andere Requests zu lassen.
DEFAULT_POLLS_PER_MIN = 320


# ---------------------------------------------------------------------------
# Datenbank
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS addresses (
    addr        TEXT PRIMARY KEY,
    first_seen  INTEGER,
    last_trade  INTEGER,
    last_poll   INTEGER DEFAULT 0,
    notional    REAL    DEFAULT 0,   -- letzte bekannte Positionsgröße, steuert Poll-Priorität
    misses      INTEGER DEFAULT 0,   -- Polls ohne offene Position
    dexes       TEXT    DEFAULT '',  -- Perp-Dexe, auf denen die Adresse handelt
    pos_value   REAL    DEFAULT 0    -- Positionswert zum Markpreis: steuert die Stufe
);
CREATE INDEX IF NOT EXISTS ix_addr_poll ON addresses(last_poll);

-- Aggregierte Heatmap: das primäre Trainingsdatum
CREATE TABLE IF NOT EXISTS heatmap (
    ts          INTEGER,
    coin        TEXT,
    bucket_px   REAL,
    side        TEXT,     -- 'long' = Long-Liquidationen (unter Kurs), 'short' = darüber
    notional    REAL,
    n_pos       INTEGER,
    PRIMARY KEY (ts, coin, bucket_px, side)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_heat_coin_ts ON heatmap(coin, ts);

-- 1-Minuten-Bars aus dem Trade-Feed, für Forward-Returns
CREATE TABLE IF NOT EXISTS bars (
    ts      INTEGER,
    coin    TEXT,
    open    REAL, high REAL, low REAL, close REAL,
    volume  REAL,
    trades  INTEGER,
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

-- Tatsächlich eingetretene Liquidationen
CREATE TABLE IF NOT EXISTS liquidations (
    ts        INTEGER,
    coin      TEXT,
    user      TEXT,
    method    TEXT,
    mark_px   REAL,
    px        REAL,
    sz        REAL,
    side      TEXT,
    tid       INTEGER PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS ix_liq_coin_ts ON liquidations(coin, ts);

-- Ergebnis des Analyse-Laufs
CREATE TABLE IF NOT EXISTS cluster_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER,
    coin            TEXT,
    cluster_px      REAL,
    side            TEXT,
    notional        REAL,
    notional_x_vol  REAL,   -- Cluster relativ zum 1h-Volumen: der eigentliche Schwellenwert
    dist_pct        REAL,   -- Abstand bei Erkennung
    px_at_touch     REAL,
    fwd_1h          REAL, fwd_4h REAL, fwd_24h REAL,
    max_up_4h       REAL, max_dn_4h REAL,
    penetrated      INTEGER, -- wurde das Cluster durchlaufen?
    liq_notional_1h REAL     -- tatsächlich liquidiertes Volumen danach
);

-- Tick-Aufzeichnung bei Kaskaden und Flash-Bewegungen. Ein Ringpuffer hält
-- die letzten Minuten aller Rohtrades im Speicher; erkennt der Recorder eine
-- schnelle Bewegung oder einen Liquidationsschub, schreibt er das Fenster
-- davor und danach weg. Damit lassen sich solche Ereignisse später Tick für
-- Tick nachspielen, ohne die Datenbank dauerhaft zu fluten.
CREATE TABLE IF NOT EXISTS tick_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    coin        TEXT,
    t_trigger   INTEGER,   -- ms
    t_start     INTEGER,   -- ms, Beginn des gesicherten Fensters
    t_end       INTEGER,   -- ms
    reason      TEXT,      -- move60, flash10, liqburst
    move_pct    REAL,      -- Bewegung, die ausgelöst hat
    px_before   REAL,
    px_trigger  REAL,
    px_extreme  REAL,      -- extremster Preis im Fenster
    n_ticks     INTEGER,
    n_liqs      INTEGER,
    complete    INTEGER    -- 1 = Nachlauf vollständig gesichert
);
CREATE TABLE IF NOT EXISTS ticks (
    event_id INTEGER,
    ts_ms    INTEGER,
    coin     TEXT,
    px       REAL,
    sz       REAL,
    side     TEXT,         -- A = Verkauf trifft Bid, B = Kauf trifft Ask
    liq      INTEGER,      -- 1 = Liquidations-Fill
    tid      INTEGER,
    PRIMARY KEY (event_id, ts_ms, tid)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS mark_ticks (
    event_id  INTEGER,
    ts_ms     INTEGER,
    coin      TEXT,
    mark_px   REAL,
    oracle_px REAL,
    PRIMARY KEY (event_id, ts_ms)
) WITHOUT ROWID;

-- Orderbuch-Kennzahlen alle 5 s: die eigentlichen Vorläufer eines Flash-Crash.
-- Tiefe in USD innerhalb 10/25/50/100 Basispunkten je Seite, Spread, Imbalance.
CREATE TABLE IF NOT EXISTS book_summary (
    ts        INTEGER,
    coin      TEXT,
    bid       REAL,  ask REAL,
    spread_bps REAL,
    bid_10 REAL, ask_10 REAL,     -- Tiefe in USD innerhalb 10 bps
    bid_25 REAL, ask_25 REAL,
    bid_50 REAL, ask_50 REAL,
    bid_100 REAL, ask_100 REAL,
    imb_50  REAL,                 -- (bid_50 - ask_50) / (bid_50 + ask_50)
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

-- Vollständige Buch-Snapshots (Top 10 je Seite) nur in Ereignisfenstern
CREATE TABLE IF NOT EXISTS book_ticks (
    event_id INTEGER,
    ts_ms    INTEGER,
    coin     TEXT,
    levels   TEXT,                -- JSON [[bids],[asks]] als [px, sz]
    PRIMARY KEY (event_id, ts_ms)
) WITHOUT ROWID;

-- 10-Sekunden-Bars mit Seitenaufteilung: Aggressor-Verkäufe gegen -Käufe
CREATE TABLE IF NOT EXISTS bars10 (
    ts      INTEGER,
    coin    TEXT,
    open REAL, high REAL, low REAL, close REAL,
    vol_buy  REAL,                -- Taker kauft (trifft Ask)
    vol_sell REAL,                -- Taker verkauft (trifft Bid)
    trades   INTEGER,
    n_liq    INTEGER,
    liq_notional REAL,
    max_sz   REAL,
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

-- Nähe zum nächsten großen Cluster, je Minute aus den Positionen im Speicher
CREATE TABLE IF NOT EXISTS proximity (
    ts        INTEGER,
    coin      TEXT,
    px        REAL,
    up_px     REAL,  up_notional REAL,  up_dist REAL,
    dn_px     REAL,  dn_notional REAL,  dn_dist REAL,
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

-- Marktkontext je Minute aus activeAssetCtx: Liquidationen lösen am
-- Markpreis aus, nicht am Handelspreis. Ohne diese Reihe misst die
-- Berührungserkennung den falschen Preis.
CREATE TABLE IF NOT EXISTS ctx_bars (
    ts        INTEGER,
    coin      TEXT,
    mark_px   REAL,
    oracle_px REAL,
    mid_px    REAL,
    funding   REAL,      -- aktuelle stündliche Funding-Rate
    oi        REAL,      -- Open Interest in Coin-Einheiten (eine Seite)
    premium   REAL,
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

-- Abdeckung: welchen Anteil des Open Interest sehen wir überhaupt?
CREATE TABLE IF NOT EXISTS coverage (
    ts            INTEGER,
    coin          TEXT,
    tracked_size  REAL,   -- Summe |size| aller verfolgten Positionen (beide Seiten)
    oi            REAL,   -- offizielles OI, eine Seite
    ratio         REAL,   -- tracked_size / (2 * oi)
    PRIMARY KEY (ts, coin)
) WITHOUT ROWID;

-- Wal-Protokoll: nur Änderungen und ein Zeitreihenpunkt je Snapshot
CREATE TABLE IF NOT EXISTS whale_positions (
    ts         INTEGER,
    addr       TEXT,
    coin       TEXT,
    event      TEXT,      -- open, add, reduce, close, flip, snap
    side       TEXT,
    size       REAL,
    pos_value  REAL,      -- USD zum Markpreis
    entry_px   REAL,
    liq_px     REAL,
    leverage   REAL,
    upnl       REAL,      -- unrealisierter Gewinn/Verlust
    mark_px    REAL,
    delta_usd  REAL,      -- Veränderung des Positionswerts gegenüber dem Vorzustand
    PRIMARY KEY (ts, addr, coin, event)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_whale_coin_ts ON whale_positions(coin, ts);
CREATE INDEX IF NOT EXISTS ix_whale_addr ON whale_positions(addr, ts);

-- Qualitätsstempel je Snapshot: erlaubt später, schwache Snapshots auszuschließen
CREATE TABLE IF NOT EXISTS snapshot_meta (
    ts             INTEGER PRIMARY KEY,
    n_addresses    INTEGER,   -- bekannte Adressen insgesamt
    n_with_pos     INTEGER,   -- davon mit offener Position
    n_positions    INTEGER,   -- in den Snapshot eingegangene Positionen
    n_stale_dropped INTEGER,  -- wegen Überalterung ausgeschlossen
    age_median     INTEGER,   -- Sekunden seit letzter Abfrage, Median
    age_p90        INTEGER,
    backlog        INTEGER,   -- überfällige Adressen
    uptime         INTEGER    -- Sekunden seit Prozessstart: klein = nach Neustart
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db_connect(path: str = DB_PATH, synchronous: str = "NORMAL") -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA synchronous={synchronous}")
    con.execute("PRAGMA temp_store=MEMORY")      # Sortierpuffer im RAM, nicht auf der SD-Karte
    con.executescript(SCHEMA)
    # Migration älterer Datenbanken
    cols = {r[1] for r in con.execute("PRAGMA table_info(addresses)")}
    if "dexes" not in cols:
        con.execute("ALTER TABLE addresses ADD COLUMN dexes TEXT DEFAULT ''")
    if "pos_value" not in cols:
        con.execute("ALTER TABLE addresses ADD COLUMN pos_value REAL DEFAULT 0")
    # Bestehende Positionen ohne Positionswert: Notional als Näherung übernehmen,
    # sonst gelten alle bis zur nächsten Abfrage als Staub -- auch die Wale.
    con.execute("UPDATE addresses SET pos_value = notional "
                "WHERE pos_value = 0 AND notional > 0")
    hcols = {r[1] for r in con.execute("PRAGMA table_info(heatmap)")}
    if "lev_wavg" not in hcols:
        con.execute("ALTER TABLE heatmap ADD COLUMN lev_wavg REAL")
        con.execute("ALTER TABLE heatmap ADD COLUMN entry_wavg REAL")
    if "cross_share" not in hcols:
        con.execute("ALTER TABLE heatmap ADD COLUMN cross_share REAL")
    con.commit()
    return con


# ---------------------------------------------------------------------------
# Rate-Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-Bucket. Kapazität in Requests pro Minute."""

    def __init__(self, per_min: int):
        self.rate = per_min / 60.0
        self.capacity = max(per_min / 6.0, 5.0)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    coins: list[str]  # Klarnamen, Symbole werden aufgelöst
    bucket_pct: float = 0.0025      # Bucket-Breite = 0.25 % des Kurses
    snapshot_sec: int = 600         # Heatmap alle 10 Minuten schreiben
    flush_sec: int = 10             # Commit-Takt: begrenzt den Verlust bei Stromausfall
    synchronous: str = "NORMAL"     # FULL = jeder Commit sofort auf Platte
    polls_per_min: int = DEFAULT_POLLS_PER_MIN
    max_addresses: int = 20_000
    # Vier Stufen nach Positionswert zum Markpreis. Das Budget fließt nach
    # oben: Wale werden fast live verfolgt, Staub gar nicht mehr regelmäßig.
    whale_notional: float = 20_000_000
    whale_refresh: int = 60
    hot_notional: float = 250_000
    hot_refresh: int = 180
    min_notional: float = 25_000    # darunter: nur noch alle 2 h nachgesehen
    cold_refresh: int = 1800
    drop_after_misses: int = 5      # Adresse ohne Position mehrfach -> selten pollen
    max_pos_age: int = 3600         # ältere Positionsdaten gehen nicht in den Snapshot
    # Tick-Aufzeichnung bei Ereignissen
    tick_buffer_min: int = 30       # so viele Minuten Rohtrades bleiben im Speicher
    tick_pre_sec: int = 300         # Vorlauf, der beim Auslösen gesichert wird
    tick_post_sec: int = 600        # Nachlauf
    tick_move_pct: float = 0.010    # Auslöser: 1 % in 60 s ...
    tick_flash_pct: float = 0.004   # ... oder 0,4 % in 10 s ...
    tick_liq_burst: int = 5         # ... oder 5 Liquidations-Fills in 30 s
    backup_dir: str = "dbbackup"    # tägliches, verifiziertes Backup
    discover_all: bool = True       # Adressen aus ALLEN Perp-Feeds sammeln


class Recorder:
    def __init__(self, con: sqlite3.Connection, cfg: Settings):
        self.con = con
        self.cfg = cfg
        self.limiter = RateLimiter(cfg.polls_per_min)
        self.positions: dict[str, list[dict]] = {}   # addr -> offene Positionen
        self.bars: dict[tuple[int, str], dict] = {}  # (minute, coin) -> OHLCV
        self.mid: dict[str, float] = {}
        self.symbol_of: dict[str, str] = {}          # BTC -> BTC, XMR -> felix:XMR
        self.coin_of: dict[str, str] = {}            # Symbol -> Klarname
        self.dex_of: dict[str, str] = {}             # Symbol -> Dex ("" = Standard)
        self.dexes: list[str] = [""]
        self.addr_dex: dict[str, set[str]] = {}      # Adresse -> gesehene Dexe
        self.polled_at: dict[str, int] = {}           # Adresse -> Zeit der letzten Antwort
        self.whale_state: dict[str, dict[str, dict]] = {}  # Adresse -> Coin -> Position
        self.ticks: dict[str, deque] = {}             # Coin -> Ringpuffer Rohtrades
        self.mark_hist: dict[str, deque] = {}         # Coin -> Ringpuffer Markpreise
        self.sec_px: dict[str, deque] = {}            # Coin -> (Sekunde, Preis) für Auslöser
        self.liq_times: dict[str, deque] = {}         # Coin -> Zeitpunkte von Liquidations-Fills
        self.tick_rec: dict[str, dict] = {}           # Coin -> laufende Aufzeichnung
        self.books: dict[str, deque] = {}             # Coin -> Ringpuffer (ts_ms, levels_json)
        self.book_last_summary: dict[str, int] = {}   # Coin -> letzte Kennzahl-Sekunde
        self.bars10: dict[tuple[int, str], dict] = {} # (10-s-Slot, Coin) -> Aggregat
        self.next_control: float = time.time() + random.uniform(3600, 6 * 3600)
        self.ctx: dict[str, dict] = {}                # Coin -> letzter Marktkontext
        self._last_coverage: dict[str, float] = {}
        self._last_prox: float = 0.0
        self.ctx_pending: dict[tuple[int, str], dict] = {}  # (Minute, Coin) -> Kontext
        self.started_at = int(time.time())
        self.stop = asyncio.Event()
        self.stats = defaultdict(int)
        self.session = None

    # -- HTTP ---------------------------------------------------------------

    async def post(self, payload: dict, weighted: bool = True) -> dict | list | None:
        import aiohttp
        if weighted:
            await self.limiter.acquire()
        for attempt in range(3):
            try:
                async with self.session.post(API, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 429:
                        self.stats["rate_limited"] += 1
                        await asyncio.sleep(2 + attempt * 3)
                        continue
                    r.raise_for_status()
                    return await r.json()
            except Exception:
                self.stats["http_error"] += 1
                await asyncio.sleep(1 + attempt * 2)
        return None

    # -- WebSocket: Trades, Adressen, Bars, Liquidationen --------------------

    async def resolve_symbols(self) -> None:
        """
        Ordnet jedem gewünschten Coin sein Handelssymbol und seinen Perp-Dex zu.

        Der Standard-Dex nutzt schlichte Namen ("BTC"). HIP-3-Märkte wie XMR
        laufen auf einem eigenen Dex und tragen dessen Namen als Präfix
        ("felix:XMR"). Wichtiger noch: Positionen auf einem HIP-3-Dex erscheinen
        nur, wenn clearinghouseState mit dem passenden dex-Parameter abgefragt
        wird — sonst fehlen sie vollständig.
        """
        wanted = {c.upper() for c in self.cfg.coins}
        found: dict[str, tuple[str, str]] = {}   # coin -> (symbol, dex)

        # Standard-Dex
        meta = await self.post({"type": "meta"}, weighted=False)
        for a in (meta or {}).get("universe", []):
            name = str(a.get("name", "")).upper()
            if name in wanted:
                found[name] = (a["name"], "")

        # HIP-3-Dexe durchsuchen, falls noch etwas fehlt
        if len(found) < len(wanted):
            dexs = await self.post({"type": "perpDexs"}, weighted=False)
            for d in (dexs or []):
                if not isinstance(d, dict) or not d.get("name"):
                    continue          # erster Eintrag ist der Standard-Dex (null)
                dex = d["name"]
                m = await self.post({"type": "meta", "dex": dex}, weighted=False)
                for a in (m or {}).get("universe", []):
                    short = str(a.get("name", "")).split(":")[-1].upper()
                    if short in wanted and short not in found:
                        sym = a["name"] if ":" in str(a["name"]) else f"{dex}:{a['name']}"
                        found[short] = (sym, dex)
                if len(found) == len(wanted):
                    break

        for coin in sorted(wanted):
            if coin in found:
                sym, dex = found[coin]
                self.symbol_of[coin] = sym
                self.coin_of[sym] = coin
                self.dex_of[sym] = dex
                tag = f"HIP-3 auf '{dex}'" if dex else "Standard-Dex"
                print(f"  {coin:<6} -> {sym:<18} ({tag})")
            else:
                print(f"  {coin:<6} -> nicht gefunden, wird übersprungen")

        # Zur Adress-Entdeckung alle Standard-Perps abonnieren. Wer BTC hält,
        # handelt vielleicht gerade nur SOL -- ohne breite Beobachtung bleibt
        # seine BTC-Position unsichtbar. Bars entstehen weiterhin nur für die
        # verfolgten Coins, sonst wächst die Datenbank zu schnell.
        self.discovery_symbols: list[str] = []
        if self.cfg.discover_all and meta:
            tracked = set(self.symbol_of.values())
            self.discovery_symbols = [
                a["name"] for a in meta.get("universe", [])
                if a.get("name") and a["name"] not in tracked
                and not a.get("isDelisted")]
            print(f"  + {len(self.discovery_symbols)} weitere Feeds nur zur "
                  "Adress-Entdeckung")

        self.dexes = sorted({d for d in self.dex_of.values()})
        if len(self.dexes) > 1:
            print(f"  Hinweis: {len(self.dexes)} Perp-Dexe. Adressen, die auf "
                  "mehreren handeln, brauchen entsprechend mehr Abfragen.")

    async def run_ws(self) -> None:
        import websockets
        backoff = 1
        while not self.stop.is_set():
            try:
                async with websockets.connect(WS, ping_interval=20, max_size=2**23) as ws:
                    symbols = (list(self.symbol_of.values()) or self.cfg.coins) \
                              + self.discovery_symbols
                    for sym in symbols:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": sym},
                        }))
                    # Marktkontext und Orderbuch nur für die verfolgten Coins
                    for sym in self.symbol_of.values():
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "activeAssetCtx", "coin": sym},
                        }))
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": sym},
                        }))
                    print(f"WS verbunden, {len(symbols)} Feeds abonniert.")
                    backoff = 1
                    while not self.stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        msg = json.loads(raw)
                        ch = msg.get("channel")
                        if ch == "trades":
                            for t in msg.get("data", []):
                                self.on_trade(t)
                        elif ch == "activeAssetCtx":
                            self.on_ctx(msg.get("data") or {})
                        elif ch == "l2Book":
                            self.on_book(msg.get("data") or {})
            except asyncio.TimeoutError:
                print("WS still, reconnect …")
            except Exception as e:
                if not self.stop.is_set():
                    print(f"WS-Fehler: {e} — reconnect in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    def on_trade(self, t: dict) -> None:
        symbol = t.get("coin")
        # HIP-3-Symbole ("felix:XMR") auf den Klarnamen normalisieren, damit
        # Bars und Heatmap denselben Schlüssel benutzen.
        coin = self.coin_of.get(symbol) or str(symbol).split(":")[-1]
        dex = self.dex_of.get(symbol, "")
        try:
            px, sz = float(t["px"]), float(t["sz"])
            ts = int(t["time"])
        except (KeyError, TypeError, ValueError):
            return

        tracked = coin in self.symbol_of
        self.stats["trades"] += 1
        if not tracked:
            self.stats["discovery_trades"] += 1

        if tracked:
            self.mid[coin] = px
            is_liq = bool(t.get("liquidation"))
            self.buffer_tick(coin, ts, px, sz, t.get("side"), is_liq,
                             int(t.get("tid") or 0))
            # 10-Sekunden-Bar mit Aggressorseite
            slot = ts // 10_000 * 10
            k = (slot, coin)
            b10 = self.bars10.get(k)
            side_sell = str(t.get("side")) == "A"      # A = Verkauf trifft Bid
            if b10 is None:
                self.bars10[k] = {"o": px, "h": px, "l": px, "c": px,
                                  "vb": 0.0 if side_sell else sz,
                                  "vs": sz if side_sell else 0.0,
                                  "n": 1, "nl": int(is_liq),
                                  "ln": px * sz if is_liq else 0.0, "mx": sz}
            else:
                b10["h"] = max(b10["h"], px); b10["l"] = min(b10["l"], px); b10["c"] = px
                if side_sell: b10["vs"] += sz
                else:         b10["vb"] += sz
                b10["n"] += 1; b10["mx"] = max(b10["mx"], sz)
                if is_liq:
                    b10["nl"] += 1; b10["ln"] += px * sz
            # 1-Minuten-Bar fortschreiben
            minute = ts // 60_000 * 60
            key = (minute, coin)
            b = self.bars.get(key)
            if b is None:
                self.bars[key] = {"o": px, "h": px, "l": px, "c": px, "v": sz, "n": 1}
            else:
                b["h"] = max(b["h"], px); b["l"] = min(b["l"], px)
                b["c"] = px; b["v"] += sz; b["n"] += 1

        # Adressen einsammeln, inklusive des Dex, auf dem sie gehandelt haben
        for addr in (t.get("users") or []):
            if isinstance(addr, str) and addr.startswith("0x"):
                self.seen_address(addr, ts, dex)

        # Echte Liquidation?
        liq = t.get("liquidation")
        if liq:
            self.stats["liquidations"] += 1
            try:
                self.con.execute(
                    "INSERT OR IGNORE INTO liquidations VALUES (?,?,?,?,?,?,?,?,?)",
                    (ts, coin, liq.get("liquidatedUser"), liq.get("method"),
                     float(liq.get("markPx") or 0), px, sz, t.get("side"), int(t.get("tid", 0))),
                )
            except Exception as e:
                self.stats["liq_store_errors"] += 1
                if self.stats["liq_store_errors"] <= 3:
                    print(f"Liquidation nicht gespeichert: {e}")

    # -- Tick-Aufzeichnung -------------------------------------------------

    def buffer_tick(self, coin: str, ts: int, px: float, sz: float,
                    side: str, liq: bool, tid: int) -> None:
        c = self.cfg
        buf = self.ticks.get(coin)
        if buf is None:
            buf = self.ticks[coin] = deque()
            self.sec_px[coin] = deque(maxlen=130)
            self.liq_times[coin] = deque(maxlen=200)
        buf.append((ts, px, sz, side, liq, tid))
        cutoff = ts - c.tick_buffer_min * 60_000
        while buf and buf[0][0] < cutoff:
            buf.popleft()

        # Sekundenraster für die Bewegungsprüfung
        sec = ts // 1000
        sp = self.sec_px[coin]
        if not sp or sp[-1][0] != sec:
            sp.append((sec, px))
        if liq:
            self.liq_times[coin].append(ts)

        # Auslösebedingungen prüfen -- gilt für Start UND Verlängerung
        reason, move, ref = None, 0.0, px
        p60 = self._px_ago(sp, sec, 60)
        p10 = self._px_ago(sp, sec, 10)
        if p60 and abs(px / p60 - 1) >= c.tick_move_pct:
            reason, move, ref = "move60", px / p60 - 1, p60
        elif p10 and abs(px / p10 - 1) >= c.tick_flash_pct:
            reason, move, ref = "flash10", px / p10 - 1, p10
        else:
            lt = self.liq_times[coin]
            recent = sum(1 for x in lt if ts - x <= 30_000)
            if recent >= c.tick_liq_burst:
                reason, move = "liqburst", (px / p60 - 1) if p60 else 0.0
                ref = p60 or px

        rec = self.tick_rec.get(coin)
        if rec:
            rec["extreme"] = (min(rec["extreme"], px) if rec["move"] < 0
                              else max(rec["extreme"], px))
            # Nachlauf nur verlängern, wenn die Bewegung WEITERGEHT. Bei jedem
            # Trade zu verlängern hieße bei BTC: die Aufzeichnung endet nie.
            if reason:
                rec["end"] = max(rec["end"], ts + c.tick_post_sec * 1000)
            return

        if reason:
            self.tick_rec[coin] = {
                "trigger": ts, "start": ts - c.tick_pre_sec * 1000,
                "end": ts + c.tick_post_sec * 1000, "reason": reason,
                "move": move, "px_before": ref, "px_trigger": px, "extreme": px,
            }
            self.stats["tick_events"] += 1
            print(f"  Tick-Aufzeichnung {coin}: {reason} {move:+.2%} bei {px}")

    @staticmethod
    def _px_ago(sp: deque, sec: int, ago: int) -> float | None:
        """Preis vor 'ago' Sekunden aus dem Sekundenraster, oder None."""
        target = sec - ago
        best = None
        for s_, p_ in sp:
            if s_ <= target:
                best = p_
            else:
                break
        return best

    def finish_tick_recordings(self, force: bool = False) -> None:
        """Abgeschlossene Aufzeichnungen aus dem Puffer in die Datenbank schreiben."""
        now_ms = int(time.time() * 1000)
        for coin, rec in list(self.tick_rec.items()):
            if not force and now_ms < rec["end"]:
                continue
            buf = self.ticks.get(coin, ())
            rows = [(t, px, sz, sd, int(lq), tid) for t, px, sz, sd, lq, tid in buf
                    if rec["start"] <= t <= rec["end"]]
            marks = [(t, m, o) for t, m, o in self.mark_hist.get(coin, ())
                     if rec["start"] <= t <= rec["end"]]
            books = [(t, lv) for t, lv in self.books.get(coin, ())
                     if rec["start"] <= t <= rec["end"]]
            cur = self.con.execute(
                "INSERT INTO tick_events (coin, t_trigger, t_start, t_end, reason, "
                "move_pct, px_before, px_trigger, px_extreme, n_ticks, n_liqs, complete) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (coin, rec["trigger"], rec["start"], rec["end"], rec["reason"],
                 rec["move"], rec["px_before"], rec["px_trigger"], rec["extreme"],
                 len(rows), sum(r[4] for r in rows), int(not force)))
            eid = cur.lastrowid
            self.con.executemany(
                "INSERT OR IGNORE INTO ticks VALUES (?,?,?,?,?,?,?,?)",
                [(eid, t, coin, px, sz, sd, lq, tid) for t, px, sz, sd, lq, tid in rows])
            self.con.executemany(
                "INSERT OR IGNORE INTO mark_ticks VALUES (?,?,?,?,?)",
                [(eid, t, coin, m, o) for t, m, o in marks])
            self.con.executemany(
                "INSERT OR IGNORE INTO book_ticks VALUES (?,?,?,?)",
                [(eid, t, coin, lv) for t, lv in books])
            del self.tick_rec[coin]
            print(f"  Tick-Aufzeichnung {coin} gesichert ({rec['reason']}): "
                  f"{len(rows):,} Trades, {len(marks):,} Markpreise, "
                  f"{len(books):,} Buch-Snapshots, Extrem {rec['extreme']}")

    def on_book(self, d: dict) -> None:
        """
        Orderbuch: Kennzahlen alle 5 s dauerhaft, Top-10-Snapshots im Ringpuffer.

        Die Tiefe nahe am Kurs ist der verlässlichste Vorläufer eines
        Flash-Crash -- sie dünnt aus, bevor sich der Preis bewegt.
        """
        symbol = d.get("coin")
        coin = self.coin_of.get(symbol) or str(symbol).split(":")[-1]
        if coin not in self.symbol_of:
            return
        levels = d.get("levels") or []
        if len(levels) != 2 or not levels[0] or not levels[1]:
            return
        try:
            ts = int(d.get("time") or time.time() * 1000)
            bids = [(float(l["px"]), float(l["sz"])) for l in levels[0][:10]]
            asks = [(float(l["px"]), float(l["sz"])) for l in levels[1][:10]]
        except (TypeError, ValueError, KeyError):
            return
        self.stats["book_updates"] += 1

        # Ringpuffer für Ereignisfenster
        buf = self.books.get(coin)
        if buf is None:
            buf = self.books[coin] = deque()
        buf.append((ts, json.dumps([[[p, z] for p, z in bids], [[p, z] for p, z in asks]],
                                   separators=(",", ":"))))
        cutoff = ts - self.cfg.tick_buffer_min * 60_000
        while buf and buf[0][0] < cutoff:
            buf.popleft()

        # Kennzahlen alle 5 Sekunden
        sec5 = ts // 5000 * 5
        if self.book_last_summary.get(coin) == sec5:
            return
        self.book_last_summary[coin] = sec5
        bid, ask = bids[0][0], asks[0][0]
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2

        def depth(side, bps):
            lim = mid * bps / 10_000
            return sum(p * z for p, z in side if abs(p - mid) <= lim)

        b10, a10 = depth(bids, 10), depth(asks, 10)
        b25, a25 = depth(bids, 25), depth(asks, 25)
        b50, a50 = depth(bids, 50), depth(asks, 50)
        b100, a100 = depth(bids, 100), depth(asks, 100)
        imb = (b50 - a50) / (b50 + a50) if (b50 + a50) > 0 else 0.0
        self.con.execute(
            "INSERT OR REPLACE INTO book_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sec5, coin, bid, ask, (ask - bid) / mid * 10_000,
             b10, a10, b25, a25, b50, a50, b100, a100, imb))

    def on_ctx(self, d: dict) -> None:
        """Markpreis, Oracle, Funding und OI pro Minute festhalten."""
        symbol = d.get("coin")
        coin = self.coin_of.get(symbol) or str(symbol).split(":")[-1]
        c = d.get("ctx") or {}
        try:
            row = {
                "mark": float(c.get("markPx") or 0),
                "oracle": float(c.get("oraclePx") or 0),
                "mid": float(c.get("midPx") or 0),
                "funding": float(c.get("funding") or 0),
                "oi": float(c.get("openInterest") or 0),
                "premium": float(c.get("premium") or 0),
            }
        except (TypeError, ValueError):
            return
        if row["mark"] <= 0:
            return
        self.ctx[coin] = row
        self.stats["ctx_updates"] += 1
        mh = self.mark_hist.get(coin)
        if mh is None:
            mh = self.mark_hist[coin] = deque()
        now_ms = int(time.time() * 1000)
        mh.append((now_ms, row["mark"], row["oracle"]))
        cutoff = now_ms - self.cfg.tick_buffer_min * 60_000
        while mh and mh[0][0] < cutoff:
            mh.popleft()
        minute = int(time.time()) // 60 * 60
        self.ctx_pending[(minute, coin)] = row     # letzter Wert der Minute gewinnt

    def flush_bars10(self, final: bool = False) -> None:
        cur = int(time.time()) // 10 * 10
        done = [k for k in self.bars10 if final or k[0] < cur]
        if done:
            self.con.executemany(
                "INSERT OR REPLACE INTO bars10 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(k[0], k[1], b["o"], b["h"], b["l"], b["c"], b["vb"], b["vs"],
                  b["n"], b["nl"], b["ln"], b["mx"])
                 for k in done for b in [self.bars10.pop(k)]])

    def write_proximity(self) -> None:
        """Nächstes großes Cluster über und unter dem Kurs, je Minute."""
        now = int(time.time()) // 60 * 60
        rows = []
        for coin in self.symbol_of:
            px = self.ref_px(coin)
            if not px:
                continue
            # Buckets aus den Positionen im Speicher, nur innerhalb 5 %
            agg: dict[tuple[float, str], float] = defaultdict(float)
            for addr, positions in self.positions.items():
                for p in positions:
                    if p["coin"] != coin or p["liq_px"] <= 0:
                        continue
                    if abs(p["liq_px"] / px - 1) > 0.05:
                        continue
                    agg[(self.bucket_of(p["liq_px"]), p["side"])] += p["notional"]
            ups = [(b, n) for (b, sd), n in agg.items() if sd == "short" and b > px]
            dns = [(b, n) for (b, sd), n in agg.items() if sd == "long" and b < px]
            up = max(ups, key=lambda x: x[1]) if ups else (None, 0.0)
            dn = max(dns, key=lambda x: x[1]) if dns else (None, 0.0)
            rows.append((now, coin, px,
                         up[0], up[1], (up[0] / px - 1) if up[0] else None,
                         dn[0], dn[1], (1 - dn[0] / px) if dn[0] else None))
        if rows:
            self.con.executemany(
                "INSERT OR REPLACE INTO proximity VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def maybe_control_window(self) -> None:
        """
        Kontrollfenster: ein zufälliges Fenster ohne Auslöser, täglich ein- bis
        zweimal. Ohne diese Fälle kennt eine spätere Simulation nur die Treffer,
        nie die Fehlalarme.
        """
        if time.time() < self.next_control:
            return
        now_ms = int(time.time() * 1000)
        c = self.cfg
        for coin in self.symbol_of:
            if coin in self.tick_rec:
                continue
            self.tick_rec[coin] = {
                "trigger": now_ms, "start": now_ms - c.tick_pre_sec * 1000,
                "end": now_ms + c.tick_post_sec * 1000, "reason": "control",
                "move": 0.0, "px_before": self.ref_px(coin),
                "px_trigger": self.ref_px(coin), "extreme": self.ref_px(coin),
            }
        self.next_control = time.time() + random.uniform(10 * 3600, 20 * 3600)
        print("  Kontrollfenster gestartet (zufälliges Fenster ohne Auslöser)")

    def flush_ctx(self, final: bool = False) -> None:
        cur_min = int(time.time()) // 60 * 60
        done = [k for k in self.ctx_pending if final or k[0] < cur_min]
        if done:
            self.con.executemany(
                "INSERT OR REPLACE INTO ctx_bars VALUES (?,?,?,?,?,?,?,?)",
                [(k[0], k[1], r["mark"], r["oracle"], r["mid"], r["funding"],
                  r["oi"], r["premium"])
                 for k in done for r in [self.ctx_pending.pop(k)]])

    def seen_address(self, addr: str, ts: int, dex: str = "") -> None:
        known = self.addr_dex.setdefault(addr, set())
        known.add(dex)
        self.con.execute(
            "INSERT INTO addresses(addr, first_seen, last_trade, dexes) VALUES (?,?,?,?) "
            "ON CONFLICT(addr) DO UPDATE SET last_trade=excluded.last_trade, "
            "dexes=excluded.dexes",
            (addr, ts, ts, ",".join(sorted(known))),
        )

    # -- Positions-Polling ---------------------------------------------------

    def due_addresses(self, limit: int) -> list[str]:
        """
        Drei Gruppen mit festem Budgetanteil, damit keine die andere verdrängt:

          A  Adressen MIT Position: fällige Wale zuerst, dann der Rest nach
             Überfälligkeit relativ zum Intervall seiner Stufe.             ~65 %
          B  Nie abgefragte Adressen, älteste zuerst. Ohne eigenen Anteil
             würden sie bei unendlicher "Überfälligkeit" alles verdrängen
             -- oder, umgekehrt sortiert, nie drankommen.                    ~25 %
          C  Bekannt leere Adressen: nur, wenn sie seit der letzten Abfrage
             wieder gehandelt haben (last_trade > last_poll), sonst höchstens
             einmal am Tag. Eine leere Adresse ohne neuen Trade kann keine
             neue Position haben -- wir sehen ja alle Feeds.                ~10 %
        """
        now = int(time.time())
        c = self.cfg
        n_a, n_b = int(limit * 0.65), int(limit * 0.25)
        n_c = limit - n_a - n_b

        a = [r[0] for r in self.con.execute(
            """
            SELECT addr,
                   (? - last_poll) * 1.0 / CASE
                       WHEN pos_value >= ? THEN ?
                       WHEN pos_value >= ? THEN ?
                       WHEN pos_value >= ? THEN ?
                       ELSE ? END AS overdue
            FROM addresses
            WHERE last_poll > 0 AND pos_value > 0 AND overdue > 1.0
            ORDER BY (pos_value >= ?) DESC, overdue DESC LIMIT ?
            """,
            (now, c.whale_notional, c.whale_refresh, c.hot_notional, c.hot_refresh,
             c.min_notional, c.cold_refresh, c.cold_refresh * 4,
             c.whale_notional, limit))]

        b = [r[0] for r in self.con.execute(
            "SELECT addr FROM addresses WHERE last_poll = 0 ORDER BY first_seen ASC LIMIT ?",
            (limit,))]

        c_ = [r[0] for r in self.con.execute(
            """
            SELECT addr FROM addresses
            WHERE last_poll > 0 AND pos_value <= 0
              AND (last_trade / 1000 > last_poll OR ? - last_poll > 86400)
            ORDER BY last_trade DESC LIMIT ?
            """, (now, limit))]

        # Anteile zuweisen, Rest auffüllen
        out = a[:n_a] + b[:n_b] + c_[:n_c]
        if len(out) < limit:
            rest = limit - len(out)
            for extra in (a[n_a:], b[n_b:], c_[n_c:]):
                out += extra[:rest]
                rest = limit - len(out)
                if rest <= 0:
                    break
        return out

    async def poll_address(self, addr: str) -> None:
        now = int(time.time())
        # Nur die Dexe abfragen, auf denen die Adresse tatsächlich gehandelt hat.
        dexes = self.addr_dex.get(addr) or {""}
        out, total, total_value, any_response = [], 0.0, 0.0, False

        for dex in sorted(dexes):
            req = {"type": "clearinghouseState", "user": addr}
            if dex:
                req["dex"] = dex
            data = await self.post(req)
            if data is None:
                continue
            any_response = True

            for ap in data.get("assetPositions", []):
                p = ap.get("position", {})
                liq = p.get("liquidationPx")
                szi = p.get("szi")
                if liq is None or szi is None:
                    continue                     # z. B. unhebelte Position
                try:
                    liq_px, size = float(liq), float(szi)
                except (TypeError, ValueError):
                    continue
                if liq_px <= 0 or size == 0:
                    continue
                notional = abs(size) * liq_px
                raw = str(p.get("coin", ""))
                lev = p.get("leverage") or {}
                try:
                    lev_val = float(lev.get("value") or 0)
                except (TypeError, ValueError, AttributeError):
                    lev_val = 0.0
                is_cross = str(lev.get("type", "cross")).lower() == "cross"
                try:
                    entry = float(p.get("entryPx") or 0)
                except (TypeError, ValueError):
                    entry = 0.0
                try:
                    pos_val = abs(float(p.get("positionValue") or 0))
                    upnl = float(p.get("unrealizedPnl") or 0)
                except (TypeError, ValueError):
                    pos_val, upnl = 0.0, 0.0
                out.append({
                    "coin": self.coin_of.get(raw) or raw.split(":")[-1],
                    "size": size,
                    "liq_px": liq_px,
                    "notional": notional,
                    "side": "long" if size > 0 else "short",
                    "lev": lev_val,
                    "entry": entry,
                    "pos_value": pos_val,
                    "upnl": upnl,
                    "cross": is_cross,
                })
                total += notional
                total_value += pos_val
            self.stats["polls"] += 1

        if not any_response:
            self.con.execute("UPDATE addresses SET last_poll=? WHERE addr=?", (now, addr))
            return

        self.polled_at[addr] = now
        self.track_whales(addr, out, now)
        if out:
            self.positions[addr] = out
            self.con.execute(
                "UPDATE addresses SET last_poll=?, notional=?, pos_value=?, misses=0 "
                "WHERE addr=?", (now, total, total_value, addr))
        else:
            self.positions.pop(addr, None)
            self.con.execute(
                "UPDATE addresses SET last_poll=?, notional=0, pos_value=0, "
                "misses=misses+1 WHERE addr=?", (now, addr))

    def ref_px(self, coin: str) -> float:
        """Letzter Handelspreis, sonst Markpreis -- direkt nach dem Start
        gibt es für ruhige Coins noch keinen Trade, aber schon einen Kontext."""
        return self.mid.get(coin) or (self.ctx.get(coin) or {}).get("mark", 0.0)

    def track_whales(self, addr: str, positions: list[dict], now: int) -> None:
        """
        Protokolliert Positionen über der Wal-Schwelle -- aber nur Änderungen.

        Hysterese: Wer einmal Wal war, bleibt in Beobachtung, bis er unter die
        Hälfte der Schwelle fällt. Sonst flackert eine Position, die um die
        20 Mio. pendelt, ständig zwischen "neu" und "geschlossen".
        """
        thr = self.cfg.whale_notional
        cur = {p["coin"]: p for p in positions if p.get("pos_value", 0) > 0}
        prev = self.whale_state.get(addr, {})
        rows = []

        for coin, p in cur.items():
            was = prev.get(coin)
            val = p["pos_value"]
            if was is None:
                if val < thr:
                    continue
                event = "open"
                delta = val
            else:
                if val < thr * 0.5:
                    continue                          # fällt unten raus -> close
                if p["side"] != was["side"]:
                    event = "flip"
                elif abs(val - was["pos_value"]) / max(was["pos_value"], 1) < 0.02:
                    continue                          # unter 2 % Änderung: Rauschen
                else:
                    event = "add" if val > was["pos_value"] else "reduce"
                delta = val - was["pos_value"] if event != "flip" else val
            rows.append((now, addr, coin, event, p["side"], abs(p["size"]), val,
                         p.get("entry", 0), p["liq_px"], p.get("lev", 0),
                         p.get("upnl", 0), self.ref_px(coin), delta))
            prev[coin] = p

        for coin, was in list(prev.items()):
            p = cur.get(coin)
            if p is None or p["pos_value"] < thr * 0.5:
                rows.append((now, addr, coin, "close", was["side"], abs(was["size"]),
                             p["pos_value"] if p else 0, was.get("entry", 0),
                             was["liq_px"], was.get("lev", 0),
                             p.get("upnl", 0) if p else 0, self.ref_px(coin),
                             -was["pos_value"]))
                del prev[coin]

        if prev:
            self.whale_state[addr] = prev
        else:
            self.whale_state.pop(addr, None)
        if rows:
            self.con.executemany(
                "INSERT OR REPLACE INTO whale_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            self.stats["whale_events"] += len(rows)

    def restore_whales(self) -> None:
        """
        Wal-Zustand aus dem Protokoll wiederherstellen.

        Ohne das wäre nach jedem Neustart jeder bestehende Wal ein "neuer" --
        lauter falsche open-Ereignisse mit falschem Zeitstempel.
        """
        rows = self.con.execute("""
            SELECT w.addr, w.coin, w.side, w.size, w.pos_value, w.entry_px,
                   w.liq_px, w.leverage, w.upnl
            FROM whale_positions w
            WHERE w.ts = (SELECT MAX(ts) FROM whale_positions x
                          WHERE x.addr = w.addr AND x.coin = w.coin)
              AND w.event != 'close'""").fetchall()
        for addr, coin, side, size, val, entry, liq, lev, upnl in rows:
            self.whale_state.setdefault(addr, {})[coin] = {
                "coin": coin, "side": side, "size": size if side == "long" else -size,
                "pos_value": val, "entry": entry, "liq_px": liq,
                "lev": lev, "upnl": upnl, "notional": abs(size) * liq,
            }
        if rows:
            print(f"  {len(rows)} Wal-Positionen aus dem Protokoll übernommen")

    def snapshot_whales(self, now: int) -> None:
        """Zeitreihenpunkt je Wal, damit sich Exposure gegen Kurs zeichnen lässt."""
        rows = []
        for addr, coins in self.whale_state.items():
            for coin, p in coins.items():
                rows.append((now, addr, coin, "snap", p["side"], abs(p["size"]),
                             p["pos_value"], p.get("entry", 0), p["liq_px"],
                             p.get("lev", 0), p.get("upnl", 0),
                             self.ref_px(coin), 0.0))
        if rows:
            self.con.executemany(
                "INSERT OR REPLACE INTO whale_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)

    async def run_poller(self) -> None:
        while not self.stop.is_set():
            batch = self.due_addresses(max(int(self.cfg.polls_per_min / 12), 10))
            if not batch:
                await asyncio.sleep(5)
                continue
            await asyncio.gather(*(self.poll_address(a) for a in batch))
            self.con.commit()

    # -- Snapshots -----------------------------------------------------------

    def bucket_of(self, liq_px: float) -> float:
        """
        Logarithmisches Preisraster: die Bucket-Grenzen sind über die Zeit
        stabil, unabhängig davon, wo der Kurs gerade steht. Ohne das würde
        das Raster mit dem Kurs mitwandern und Snapshots wären nicht
        vergleichbar.
        """
        step = math.log1p(self.cfg.bucket_pct)
        return round(math.exp(round(math.log(liq_px) / step) * step), 8)

    def write_snapshot(self) -> None:
        """
        Aggregiert alle bekannten Liquidationspreise in Preis-Buckets.

        Positionen, deren letzte Abfrage zu lange her ist, bleiben draußen:
        Ein Wallet kann längst geschlossen haben, ohne dass wir es wissen.
        Lieber ein etwas dünnerer, dafür ehrlicher Snapshot.
        """
        now = int(time.time())
        # Zelle -> [notional, n, sum(notional*lev), sum(notional*entry), lev_n, cross_notional]
        agg: dict[tuple[str, float, str], list[float]] = defaultdict(
            lambda: [0.0, 0, 0.0, 0.0, 0.0, 0.0])
        n_pos, n_stale = 0, 0
        tracked_size: dict[str, float] = defaultdict(float)

        for addr, positions in self.positions.items():
            age = now - self.polled_at.get(addr, 0)
            if age > self.cfg.max_pos_age:
                n_stale += len(positions)
                continue
            for p in positions:
                if p["liq_px"] <= 0:
                    continue
                cell = agg[(p["coin"], self.bucket_of(p["liq_px"]), p["side"])]
                cell[0] += p["notional"]
                cell[1] += 1
                if p.get("lev"):
                    cell[2] += p["notional"] * p["lev"]
                    cell[4] += p["notional"]
                if p.get("entry"):
                    cell[3] += p["notional"] * p["entry"]
                if p.get("cross", True):
                    cell[5] += p["notional"]
                tracked_size[p["coin"]] += abs(p["size"])
                n_pos += 1

        if agg:
            self.con.executemany(
                "INSERT OR REPLACE INTO heatmap VALUES (?,?,?,?,?,?,?,?,?)",
                [(now, coin, bx, side, v[0], v[1],
                  (v[2] / v[4]) if v[4] else None,
                  (v[3] / v[0]) if v[0] and v[3] else None,
                  (v[5] / v[0]) if v[0] else None)
                 for (coin, bx, side), v in agg.items()],
            )

        # Abdeckung: verfolgte Positionsgröße gegen offizielles Open Interest.
        # OI zählt eine Seite, unsere Summe beide -- daher der Faktor 2.
        cov_rows = []
        for coin in self.symbol_of:
            oi = (self.ctx.get(coin) or {}).get("oi", 0)
            if oi > 0:
                cov_rows.append((now, coin, tracked_size.get(coin, 0.0), oi,
                                 tracked_size.get(coin, 0.0) / (2 * oi)))
        if cov_rows:
            self.con.executemany(
                "INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?)", cov_rows)
            self._last_coverage = {c: r for _, c, _, _, r in cov_rows}

        self.snapshot_whales(now)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('rate_limited', ?)",
                         (str(self.stats["rate_limited"]),))
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('polls_total', ?)",
                         (str(self.stats["polls"]),))
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('polls_ts', ?)", (str(now),))
        lag = self.poll_lag()
        self.con.execute(
            "INSERT OR REPLACE INTO snapshot_meta VALUES (?,?,?,?,?,?,?,?,?)",
            (now,
             self.con.execute("SELECT COUNT(*) FROM addresses").fetchone()[0],
             lag["hot"] + lag["cold"], n_pos, n_stale,
             lag["median"], lag["p90"], lag["backlog"], now - self.started_at))

        self.flush_bars()
        self.flush_bars10()
        self.flush_ctx()
        self.con.commit()

    def poll_lag(self) -> dict:
        """
        Wie aktuell sind die gespeicherten Positionen wirklich?

        Der Snapshot ist ein Mosaik aus unterschiedlich alten Abfragen. Wenn der
        Poller dem Fälligkeitsplan hinterherhinkt, dehnen sich die Abstände still
        aus — diese Zahlen machen das sichtbar.
        """
        now = int(time.time())
        ages = [now - r[0] for r in self.con.execute(
            "SELECT last_poll FROM addresses WHERE notional > 0 AND last_poll > 0")]
        c = self.cfg
        backlog = self.con.execute(
            """SELECT COUNT(*) FROM addresses
               WHERE last_poll > 0 AND pos_value > 0
                 AND (? - last_poll) > CASE
                   WHEN pos_value >= ? THEN ?
                   WHEN pos_value >= ? THEN ?
                   WHEN pos_value >= ? THEN ?
                   ELSE ? END""",
            (now, c.whale_notional, c.whale_refresh, c.hot_notional, c.hot_refresh,
             c.min_notional, c.cold_refresh, c.cold_refresh * 4)).fetchone()[0]
        never = self.con.execute(
            "SELECT COUNT(*) FROM addresses WHERE last_poll = 0").fetchone()[0]
        whales = self.con.execute(
            "SELECT COUNT(*) FROM addresses WHERE pos_value >= ?",
            (c.whale_notional,)).fetchone()[0]
        hot = self.con.execute(
            "SELECT COUNT(*) FROM addresses WHERE pos_value >= ? AND pos_value < ?",
            (c.hot_notional, c.whale_notional)).fetchone()[0]
        cold = self.con.execute(
            "SELECT COUNT(*) FROM addresses WHERE pos_value >= ? AND pos_value < ?",
            (c.min_notional, c.hot_notional)).fetchone()[0]

        ages.sort()
        med = ages[len(ages) // 2] if ages else 0
        p90 = ages[int(len(ages) * 0.9)] if ages else 0
        # benötigtes gegen verfügbares Budget, jeweils Abfragen pro Stunde
        need = (whales * 3600 / max(c.whale_refresh, 1)
                + hot * 3600 / max(c.hot_refresh, 1)
                + cold * 3600 / max(c.cold_refresh, 1))
        have = c.polls_per_min * 60
        return {"median": med, "p90": p90, "backlog": backlog, "never": never,
                "whales": whales, "hot": hot, "cold": cold,
                "load": need / have if have else 0}

    def flush_bars(self, final: bool = False) -> None:
        """Abgeschlossene Minuten-Bars schreiben; beim Beenden auch die angefangene."""
        cur_min = int(time.time()) // 60 * 60
        done = [k for k in self.bars if final or k[0] < cur_min]
        if done:
            self.con.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                [(k[0], k[1], b["o"], b["h"], b["l"], b["c"], b["v"], b["n"])
                 for k in done for b in [self.bars.pop(k)]],
            )

    async def run_flush(self) -> None:
        """
        Häufiger Commit, damit ein Stromausfall wenig kostet.

        Ohne das lägen Bars und Liquidationen bis zu einem Snapshot-Intervall
        nur im Arbeitsspeicher beziehungsweise in einer offenen Transaktion.
        Bei einem geplanten Neustart geht ohnehin nichts verloren, bei einem
        harten Stromausfall aber sehr wohl.
        """
        last_checkpoint = time.time()
        while not self.stop.is_set():
            await asyncio.sleep(self.cfg.flush_sec)
            if self.stop.is_set():
                break
            try:
                self.flush_bars()
                self.flush_bars10()
                self.flush_ctx()
                self.maybe_control_window()
                self.finish_tick_recordings()
                if time.time() - self._last_prox >= 60:
                    self.write_proximity()
                    self._last_prox = time.time()
                self.con.commit()
                # WAL gelegentlich zusammenfalten, sonst wächst sie unbegrenzt
                if time.time() - last_checkpoint > 900:
                    self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    last_checkpoint = time.time()
            except sqlite3.Error as e:
                print(f"Schreibfehler: {e}")

    async def run_backup(self) -> None:
        """Einmal täglich, im Hintergrund-Thread, damit der Recorder weiterläuft."""
        interval = 24 * 3600
        await asyncio.sleep(300)                   # nicht direkt beim Start
        while not self.stop.is_set():
            st = backup_status(self.cfg.backup_dir)
            last = st.get("ts", 0) if st and st.get("ok") else 0
            if time.time() - last >= interval:
                print(f"[{time.strftime('%H:%M:%S')}] Backup läuft …")
                res = await asyncio.to_thread(do_backup, DB_PATH, self.cfg.backup_dir)
                if res["ok"]:
                    print(f"           Backup ok: {res['size']/1e6:.0f} MB in "
                          f"{res['seconds']} s -> {self.cfg.backup_dir}/{BACKUP_NAME}")
                else:
                    print(f"           BACKUP FEHLGESCHLAGEN: {res['error']}"
                          + (" — älteres gutes Backup bleibt erhalten"
                             if res.get("last_good") else ""))
            for _ in range(1800):                  # alle 30 min nachsehen
                if self.stop.is_set():
                    return
                await asyncio.sleep(1)

    async def run_snapshots(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.cfg.snapshot_sec)
            if self.stop.is_set():
                break
            self.write_snapshot()
            n_addr = self.con.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
            tracked = sum(len(v) for v in self.positions.values())
            lag = self.poll_lag()
            print(f"[{time.strftime('%H:%M:%S')}] Adressen {n_addr:>6} | "
                  f"Positionen {tracked:>6} | Polls {self.stats['polls']:>7} | "
                  f"Trades {self.stats['trades']:>8} | Liqs {self.stats['liquidations']:>5}"
                  + (f" | 429er {self.stats['rate_limited']}" if self.stats['rate_limited'] else "")
                  + (f" | davon Entdeckung {self.stats['discovery_trades']:,}"
                     if self.stats['discovery_trades'] else "")
                  + (f" | Wal-Events {self.stats['whale_events']}"
                     if self.stats['whale_events'] else "")
                  + (f" | Tick-Events {self.stats['tick_events']}"
                     if self.stats['tick_events'] else ""))
            if self._last_coverage:
                print("           Abdeckung des Open Interest: "
                      + "  ".join(f"{c} {r*100:.0f}%" for c, r in
                                  sorted(self._last_coverage.items())))
            print(f"           Datenalter: Median {lag['median']//60:>3} min, "
                  f"90%-Quantil {lag['p90']//60:>3} min | "
                  f"überfällig {lag['backlog']:>5} | neu {lag['never']:>6} | "
                  f"Auslastung {lag['load']*100:>3.0f}%"
                  f" ({lag['whales']} Wale / {lag['hot']} groß / {lag['cold']} mittel)")
            if lag["load"] > 0.9 or lag["backlog"] > 200:
                print("           ACHTUNG: Der Poller kommt nicht mehr nach. Die "
                      "tatsächlichen Abstände sind größer als eingestellt.")
                print("           Abhilfe: --polls-per-min erhöhen (Limit beachten), "
                      "--hot-refresh verlängern oder --hot-notional anheben.")

    # -- Start ---------------------------------------------------------------

    async def run(self) -> None:
        import aiohttp
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('started', ?)",
                         (str(int(time.time())),))
        self.con.commit()
        # Wenige, langlebige Verbindungen statt bis zu 100 gleichzeitiger:
        # Jede neue TCP+TLS-Verbindung kostet CPU (Hitze) und einen Eintrag
        # in der NAT-Tabelle des Routers. Ohne Begrenzung öffnete jede
        # Abfragerunde bis zu 80 neue -- rund 20.000 pro Stunde. Ein voller
        # Router blockiert dann JEDES Gerät im Haus, minutenlang, auch
        # nachdem der Recorder gestoppt ist.
        connector = aiohttp.TCPConnector(
            limit=6, limit_per_host=6,
            keepalive_timeout=120,        # länger als der Rundentakt, sonst schläft alles ein
            ttl_dns_cache=600,
            enable_cleanup_closed=True)
        async with aiohttp.ClientSession(connector=connector) as s:
            self.session = s
            print("Löse Handelssymbole auf …")
            print("Verbindungen: höchstens 6 gleichzeitig, wiederverwendet")
            await self.resolve_symbols()
            # bekannte Dex-Zuordnungen aus einem früheren Lauf übernehmen
            for addr, dxs in self.con.execute(
                    "SELECT addr, dexes FROM addresses WHERE dexes != ''"):
                self.addr_dex[addr] = set(dxs.split(","))
            self.restore_whales()
            print()
            tasks = [asyncio.create_task(t) for t in
                     (self.run_ws(), self.run_poller(), self.run_snapshots(),
                      self.run_flush(), self.run_backup())]
            await self.stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.finish_tick_recordings(force=True)
        self.write_snapshot()
        # angefangene Minute nicht verlieren
        self.flush_bars(final=True); self.flush_bars10(final=True); self.flush_ctx(final=True)
        self.con.commit()
        print("\nSauber beendet. Daten in", DB_PATH)


# ---------------------------------------------------------------------------
# Analyse: Cluster-Berührungen labeln
# ---------------------------------------------------------------------------

def analyze(con: sqlite3.Connection, coin: str, min_x_vol: float = 0.5,
            touch_pct: float = 0.0035, max_gap: int = 120) -> None:
    """
    Sucht Momente, in denen der Kurs ein signifikantes Cluster berührt, und
    misst, was danach passiert ist. min_x_vol = Cluster-Notional relativ zum
    1h-Volumen; darunter wird ein Cluster einfach absorbiert.
    """
    import numpy as np
    import pandas as pd

    bars = pd.read_sql(
        "SELECT * FROM bars WHERE coin=? ORDER BY ts", con, params=(coin,))
    n_heat = con.execute(
        "SELECT COUNT(*) FROM heatmap WHERE coin=?", (coin,)).fetchone()[0]
    if len(bars) < 120 or not n_heat:
        print(f"Zu wenig Daten für {coin}: {len(bars)} Bars, {n_heat} Heatmap-Zeilen.")
        print("Der Recorder sollte einige Wochen laufen, bevor das aussagekräftig wird.")
        return

    bars = bars.set_index("ts")
    px = bars["close"]
    vol_notional = (bars["volume"] * bars["close"]).rolling(60, min_periods=10).sum()

    liq = pd.read_sql(
        "SELECT ts, px, sz FROM liquidations WHERE coin=? ORDER BY ts", con, params=(coin,))
    liq["notional"] = liq["px"] * liq["sz"]

    events = []
    skipped_gap = 0                             # Snapshots ohne passende Kerze
    active: dict[tuple[float, str], int] = {}   # (bucket, side) -> letzte Berührung

    # Die Heatmap wird fensterweise aus der Datenbank gelesen statt komplett
    # geladen. Bei 90 Tagen sind das mehrere Millionen Zeilen — auf einem Pi
    # mit 4 GB wäre das sonst der Engpass.
    span = con.execute("SELECT MIN(ts), MAX(ts) FROM heatmap WHERE coin=?",
                       (coin,)).fetchone()
    window = 3 * 86400
    print(f"Verarbeite {n_heat:,} Heatmap-Zeilen in "
          f"{max(1, (span[1]-span[0])//window + 1)} Fenstern …")

    for w_start in range(span[0], span[1] + 1, window):
        rows = con.execute(
            "SELECT ts, bucket_px, side, notional FROM heatmap "
            "WHERE coin=? AND ts>=? AND ts<? ORDER BY ts",
            (coin, w_start, w_start + window)).fetchall()

        by_ts: dict[int, list] = {}
        for r in rows:
            by_ts.setdefault(r[0], []).append(r)
        del rows

        for snap_ts in sorted(by_ts):
            # nächstgelegener Bar-Preis zum Snapshot
            i = px.index.searchsorted(snap_ts)
            if i <= 0 or i >= len(px) - 1:
                continue
            # Eine Bar entsteht nur, wenn gehandelt wurde. In dünnen Märkten
            # kann die nächste Kerze Minuten entfernt liegen -- dann gehört
            # ihr Preis nicht zu diesem Snapshot und das Ereignis wird
            # verworfen statt mit falschem Bezug gezählt.
            if abs(int(px.index[i]) - snap_ts) > max_gap:
                skipped_gap += 1
                continue
            now_px = px.iloc[i]
            v1h = vol_notional.iloc[i]
            if not v1h or math.isnan(v1h) or v1h <= 0:
                continue

            for _, bpx, side, notional in by_ts[snap_ts]:
                x_vol = notional / v1h
                if x_vol < min_x_vol:
                    continue                      # zu klein, wird absorbiert

                dist = (bpx - now_px) / now_px
                # Long-Liquidationen liegen unter dem Kurs, Short-Liqs darüber
                if side == "long" and dist > 0:
                    continue
                if side == "short" and dist < 0:
                    continue
                if abs(dist) > touch_pct:
                    continue                      # noch nicht berührt

                key = (round(bpx, 6), side)
                if snap_ts - active.get(key, 0) < 3600:
                    continue                      # Entprellung: max. 1 Event/h je Cluster
                active[key] = snap_ts

                fwd = {}
                for label, secs in (("1h", 3600), ("4h", 14400), ("24h", 86400)):
                    j = px.index.searchsorted(snap_ts + secs)
                    fwd[label] = (px.iloc[j] / now_px - 1) if j < len(px) else np.nan

                j4 = px.index.searchsorted(snap_ts + 14400)
                seg = bars.iloc[i:j4] if j4 > i else bars.iloc[i:i + 1]
                max_up = seg["high"].max() / now_px - 1
                max_dn = seg["low"].min() / now_px - 1

                pen = int((seg["low"].min() < bpx) if side == "long"
                          else (seg["high"].max() > bpx))
                liq_after = liq[(liq.ts >= snap_ts) & (liq.ts < snap_ts + 3600)]["notional"].sum()

                events.append({
                    "ts": snap_ts, "coin": coin, "cluster_px": bpx, "side": side,
                    "notional": notional, "notional_x_vol": x_vol, "dist_pct": dist,
                    "px_at_touch": now_px,
                    "fwd_1h": fwd["1h"], "fwd_4h": fwd["4h"], "fwd_24h": fwd["24h"],
                    "max_up_4h": max_up, "max_dn_4h": max_dn,
                    "penetrated": pen, "liq_notional_1h": liq_after,
                })

    if skipped_gap:
        print(f"  {skipped_gap:,} Snapshots übersprungen: keine Kursbar innerhalb "
              f"von {max_gap} s. Betrifft vor allem dünne Märkte und Nachtstunden.")

    if not events:
        print("Keine Cluster-Berührungen gefunden. Schwelle --min-x-vol senken "
              "oder länger aufzeichnen.")
        return

    df = pd.DataFrame(events)
    # Alte Ereignisse dieses Coins ersetzen, sonst verdoppelt jeder Lauf die Tabelle
    con.execute("DELETE FROM cluster_events WHERE coin=?", (coin,))
    df.to_sql("cluster_events", con, if_exists="append", index=False)
    con.commit()

    print(f"\n{len(df)} Cluster-Berührungen für {coin}")
    print("=" * 60)
    for side in ("long", "short"):
        s = df[df.side == side]
        if s.empty:
            continue
        # "Reversal" = Kurs bewegt sich vom Cluster weg
        rev = (s.fwd_4h > 0) if side == "long" else (s.fwd_4h < 0)
        print(f"\n{side.upper()}-Cluster  (n={len(s)})")
        print(f"  Reversal-Quote 4h    {rev.mean():.1%}")
        print(f"  durchbrochen         {s.penetrated.mean():.1%}")
        print(f"  Ø Bewegung 4h        {s.fwd_4h.mean():+.2%}")
        print(f"  Median 4h            {s.fwd_4h.median():+.2%}")
        print(f"  Ø bei Reversal       {s.fwd_4h[rev].mean():+.2%}")
        print(f"  Ø bei Durchbruch     {s.fwd_4h[~rev].mean():+.2%}")
        won, lost = s.fwd_4h[rev].abs().mean(), s.fwd_4h[~rev].abs().mean()
        if won and lost and lost > 0:
            edge = rev.mean() * won - (1 - rev.mean()) * lost
            print(f"  Payoff-Ratio         {won/lost:.2f}")
            print(f"  Erwartungswert       {edge:+.3%} pro Event  <- das ist die Zahl")

    print("\nHinweis: Ohne Gebühren und Slippage. Bei ~0.07 % Taker plus Spread "
          "muss der Erwartungswert deutlich über 0.2 % liegen, um zu tragen.")


# ---------------------------------------------------------------------------
# Simulation zum Testen der Pipeline
# ---------------------------------------------------------------------------

def simulate(con: sqlite3.Connection, coin: str = "BTC", days: int = 20) -> None:
    """
    Erzeugt Fake-Daten zum Testen des Analysepfads. Cluster liegen auf festen
    absoluten Preisniveaus und verändern sich nur langsam — so wie echte
    Positionen. Der Kurs wandert dann in sie hinein.
    """
    rng = random.Random(42)
    t0 = int(time.time()) - days * 86400
    n = days * 1440
    p0 = 60_000.0

    # Preisreihe
    price = p0
    prices, rows_b = [], []
    for i in range(n):
        ts = t0 + i * 60
        price *= math.exp(rng.gauss(0, 0.0012))
        hi = price * (1 + abs(rng.gauss(0, 0.0006)))
        lo = price * (1 - abs(rng.gauss(0, 0.0006)))
        prices.append(price)
        rows_b.append((ts, coin, price, hi, lo, price,
                       abs(rng.gauss(4, 2)), rng.randint(5, 80)))

    # Persistente Cluster auf festem logarithmischem Preisraster
    step = math.log1p(0.0025)
    levels: dict[int, float] = {}

    def level_notional(k: int) -> float:
        if k not in levels:
            levels[k] = abs(rng.gauss(0, 1)) * 3e6
        levels[k] *= math.exp(rng.gauss(0, 0.02))   # driftet langsam
        return levels[k]

    rows_h = []
    for i in range(0, n, 5):                          # alle 5 Minuten
        ts, px = t0 + i * 60, prices[i]
        base = round(math.log(px) / step)
        for k in range(-40, 41):
            if k == 0:
                continue
            lv = round(math.exp((base + k) * step), 8)
            notional = level_notional(base + k) * math.exp(-abs(k) / 15)
            if notional > 5e4:
                side = "long" if k < 0 else "short"
                rows_h.append((ts, coin, lv, side, notional, rng.randint(1, 40),
                               rng.uniform(3, 25), lv * rng.uniform(0.9, 1.1),
                               rng.uniform(0.5, 1.0)))

    con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows_b)
    con.executemany("INSERT OR REPLACE INTO heatmap VALUES (?,?,?,?,?,?,?,?,?)", rows_h)
    con.commit()
    print(f"Simulation: {len(rows_b)} Bars, {len(rows_h)} Heatmap-Zeilen für {coin}.")
    print("Achtung: reines Rauschen ohne echten Zusammenhang — testet nur die Pipeline.")


# ---------------------------------------------------------------------------
# Magnet-Test: zieht das größere Cluster den Kurs an?
# ---------------------------------------------------------------------------

MAGNET_SCHEMA = """
CREATE TABLE IF NOT EXISTS magnet_events (
    ts            INTEGER,
    coin          TEXT,
    horizon_h     INTEGER,
    px0           REAL,
    up_px         REAL,  up_notional REAL,  up_dist REAL,   -- größtes Short-Cluster oben
    dn_px         REAL,  dn_notional REAL,  dn_dist REAL,   -- größtes Long-Cluster unten
    bigger        TEXT,   -- 'up' oder 'dn': wo liegt mehr Notional
    closer        TEXT,   -- 'up' oder 'dn': was ist näher
    disagree      INTEGER,
    first_hit     TEXT,   -- 'up', 'dn' oder 'none' innerhalb des Horizonts
    t_hit_min     INTEGER,
    ret_h         REAL,   -- Rendite am Horizontende, signiert Richtung 'bigger'
    PRIMARY KEY (ts, coin, horizon_h)
) WITHOUT ROWID;
"""


def magnet_analyze(con: sqlite3.Connection, coin: str, horizons=(4, 24),
                   range_pct: float = 0.06, min_x_vol: float = 0.3,
                   step_min: int = 60, max_gap: int = 120) -> None:
    """
    Kernthese: Liquidationscluster wirken als Magnet, der Kurs läuft auf sie zu.

    Prüfung je Zeitpunkt: größtes Short-Cluster oberhalb, größtes Long-Cluster
    unterhalb (innerhalb range_pct). Welches wird zuerst erreicht?

    Drei Vorhersageregeln im Vergleich:
      Größe     -> das Cluster mit mehr Notional wird zuerst erreicht  (These)
      Nähe      -> das nähere wird zuerst erreicht                     (Geometrie)
      Zufall    -> 50 %

    Entscheidend ist die Teilmenge, in der Größe und Nähe VERSCHIEDENE Ziele
    nennen. Nur dort ist die These von der Geometrie unterscheidbar.
    """
    import numpy as np
    import pandas as pd

    con.executescript(MAGNET_SCHEMA)
    bars = pd.read_sql(
        "SELECT ts, high, low, close, volume FROM bars WHERE coin=? ORDER BY ts",
        con, params=(coin,)).set_index("ts")
    if len(bars) < 2000:
        print(f"Zu wenig Kursdaten für {coin}: {len(bars)} Bars.")
        return
    ts_arr = bars.index.to_numpy()
    hi_arr, lo_arr, cl_arr = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    vol_notional = (bars["volume"] * bars["close"]).rolling(60, min_periods=10).sum().to_numpy()

    snaps = [r[0] for r in con.execute(
        "SELECT DISTINCT ts FROM heatmap WHERE coin=? ORDER BY ts", (coin,))]
    if not snaps:
        print("Keine Heatmap-Snapshots.")
        return
    # Ausdünnen: Snapshots im Abstand step_min, sonst überlappen die Horizonte massiv
    sampled, last = [], -1e18
    for t in snaps:
        if t - last >= step_min * 60:
            sampled.append(t); last = t

    rows, skipped = [], 0
    for snap_ts in sampled:
        i = int(np.searchsorted(ts_arr, snap_ts))
        if i >= len(ts_arr) or abs(int(ts_arr[i]) - snap_ts) > max_gap:
            skipped += 1
            continue
        px0 = cl_arr[i]
        v1h = vol_notional[i]
        if not v1h or np.isnan(v1h) or v1h <= 0:
            continue
        cl = con.execute(
            "SELECT bucket_px, side, notional FROM heatmap WHERE coin=? AND ts=?",
            (coin, snap_ts)).fetchall()
        up = [(b, n) for b, sd, n in cl if sd == "short" and px0 < b <= px0 * (1 + range_pct)
              and n / v1h >= min_x_vol]
        dn = [(b, n) for b, sd, n in cl if sd == "long" and px0 * (1 - range_pct) <= b < px0
              and n / v1h >= min_x_vol]
        if not up or not dn:
            continue
        up_px, up_n = max(up, key=lambda x: x[1])
        dn_px, dn_n = max(dn, key=lambda x: x[1])
        up_d, dn_d = up_px / px0 - 1, 1 - dn_px / px0
        bigger = "up" if up_n > dn_n else "dn"
        closer = "up" if up_d < dn_d else "dn"

        for h in horizons:
            j = int(np.searchsorted(ts_arr, snap_ts + h * 3600))
            if j >= len(ts_arr):
                continue
            seg_hi, seg_lo = hi_arr[i:j], lo_arr[i:j]
            hit_up = np.argmax(seg_hi >= up_px) if (seg_hi >= up_px).any() else None
            hit_dn = np.argmax(seg_lo <= dn_px) if (seg_lo <= dn_px).any() else None
            if hit_up is None and hit_dn is None:
                first, t_hit = "none", None
            elif hit_dn is None or (hit_up is not None and hit_up <= hit_dn):
                first, t_hit = "up", int(hit_up)
            else:
                first, t_hit = "dn", int(hit_dn)
            ret = (cl_arr[j] / px0 - 1) * (1 if bigger == "up" else -1)
            rows.append((snap_ts, coin, h, px0, up_px, up_n, up_d, dn_px, dn_n, dn_d,
                         bigger, closer, int(bigger != closer), first, t_hit, ret))

    if not rows:
        print("Keine auswertbaren Zeitpunkte (beide Seiten brauchen ein Cluster "
              f"über {min_x_vol}x Stundenvolumen innerhalb {range_pct:.0%}).")
        return
    con.execute("DELETE FROM magnet_events WHERE coin=?", (coin,))
    con.executemany("INSERT OR REPLACE INTO magnet_events VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    df = pd.DataFrame(rows, columns=[
        "ts", "coin", "h", "px0", "up_px", "up_n", "up_d", "dn_px", "dn_n", "dn_d",
        "bigger", "closer", "disagree", "first", "t_hit", "ret"])

    print(f"\nMagnet-Test {coin}: {len(sampled)} Zeitpunkte im {step_min}-min-Raster"
          + (f", {skipped} ohne passende Kursbar" if skipped else ""))
    print("=" * 70)
    for h in horizons:
        d = df[df.h == h]
        hit = d[d["first"] != "none"]
        if len(hit) < 10:
            print(f"\n  Horizont {h} h: nur {len(hit)} Treffer, zu wenig.")
            continue
        acc_size = (hit["first"] == hit["bigger"]).mean()
        acc_dist = (hit["first"] == hit["closer"]).mean()
        dis = hit[hit.disagree == 1]
        print(f"\n  Horizont {h} h  ·  {len(d)} Zeitpunkte, {len(hit)} mit erreichtem Cluster "
              f"({len(hit)/len(d):.0%})")
        print(f"    Regel Größe   trifft {acc_size:>6.1%}")
        print(f"    Regel Nähe    trifft {acc_dist:>6.1%}")
        print(f"    Zufall               50.0%")
        if len(dis) >= 10:
            far_first = (dis["first"] == dis["bigger"]).to_numpy()
            # Geometrische Erwartung (Gambler's Ruin, driftloser Pfad): das
            # fernere von zwei Niveaus wird mit d_nah / (d_nah + d_fern) zuerst
            # erreicht. DAS ist die Messlatte, nicht 50 %.
            d_big = np.where(dis.bigger == "up", dis.up_d, dis.dn_d).astype(float)
            d_cls = np.where(dis.bigger == "up", dis.dn_d, dis.up_d).astype(float)
            p_geo = d_cls / (d_cls + d_big)
            expected, var = p_geo.sum(), (p_geo * (1 - p_geo)).sum()
            observed = far_first.sum()
            z = (observed - expected) / math.sqrt(var) if var > 0 else 0.0
            pval = math.erfc(abs(z) / math.sqrt(2))
            print(f"\n    Entscheidende Teilmenge: Größe und Nähe uneins (n={len(dis)})")
            print(f"      größeres, ferneres Cluster zuerst erreicht: "
                  f"{far_first.mean():.1%}")
            print(f"      geometrisch erwartet ohne Magnet:          "
                  f"{expected/len(dis):.1%}")
            print(f"      Überschuss {observed-expected:+.1f} Fälle, z = {z:+.2f}, p = {pval:.2}")
            if z > 1.96:
                print("      -> mehr als die Geometrie erklärt. Das wäre Magnetwirkung.")
            elif z < -1.96:
                print("      -> weniger als die Geometrie erklärt. Große Cluster stoßen eher ab.")
            else:
                print("      -> im Rahmen der Geometrie. Kein Beleg für einen Magneten.")
        else:
            print(f"\n    Nur {len(dis)} Fälle, in denen Größe und Nähe uneins sind — "
                  "das ist die Teilmenge, die zählt. Noch zu wenig.")
        print(f"    Ø Rendite Richtung größeres Cluster nach {h} h: {d['ret'].mean():+.2%}")

    # Zieht die Masse auch dann, wenn sie nicht erreicht wird?
    d24 = df[df.h == max(horizons)]
    if len(d24) >= 30:
        d24 = d24.assign(ratio=np.log(np.maximum(d24.up_n, 1) / np.maximum(d24.dn_n, 1)))
        q = pd.qcut(d24.ratio, 4, labels=False, duplicates="drop")
        print(f"\n  Nach Notional-Verhältnis oben/unten (Quartile), Horizont {max(horizons)} h:")
        for k, g in d24.groupby(q):
            toward_up = (g["first"] == "up").mean()
            print(f"    Quartil {int(k)+1}: oben/unten = {np.exp(g.ratio.mean()):5.2f}x  "
                  f"-> zuerst oben erreicht {toward_up:.0%}  (n={len(g)})")
        print("  Steigt der Anteil mit dem Verhältnis, zieht die Masse.")

    print("\n  Vorbehalte: Cluster liegen an alten Hochs und Tiefs, und ein Zufallspfad")
    print("  kehrt zu alten Niveaus zurück. Erst der Vergleich mit der Nähe-Regel")
    print("  in der uneinigen Teilmenge unterscheidet Magnet von Geometrie.")


def _wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)


# ---------------------------------------------------------------------------
# Wal-Auswertung: haben Wal-Aktionen Vorhersagekraft?
# ---------------------------------------------------------------------------

def whale_analyze(con: sqlite3.Connection, coin: str) -> None:
    """
    Prüft, ob nach Wal-Aktionen der Kurs in die "richtige" Richtung läuft.

    Der entscheidende Vergleich ist die Kontrollgruppe: dieselbe Messung an
    zufälligen Zeitpunkten ohne Wal-Aktion. Erst der Unterschied zwischen
    beiden sagt etwas -- nicht die Trefferquote der Wale allein, denn in
    einem steigenden Markt "haben" auch zufällige Longs meist recht.

    Und selbst ein echter Unterschied beweist kein Insiderwissen: Wale
    bewegen den Kurs auch durch ihre eigene Order.
    """
    import random
    import numpy as np
    import pandas as pd

    ev = pd.read_sql(
        "SELECT ts, addr, event, side, pos_value, delta_usd FROM whale_positions "
        "WHERE coin=? AND event IN ('open','add','reduce','close','flip') ORDER BY ts",
        con, params=(coin,))
    bars = pd.read_sql("SELECT ts, close, high, low FROM bars WHERE coin=? ORDER BY ts",
                       con, params=(coin,)).set_index("ts")
    if len(ev) < 20 or len(bars) < 1000:
        print(f"Zu wenig Daten: {len(ev)} Wal-Ereignisse, {len(bars)} Bars.")
        print("Für eine belastbare Aussage braucht es Dutzende Ereignisse je Richtung.")
        return
    px = bars["close"]

    def fwd(ts: int, secs: int) -> float:
        i = px.index.searchsorted(ts); j = px.index.searchsorted(ts + secs)
        if i >= len(px) or j >= len(px) or abs(int(px.index[i]) - ts) > 180:
            return np.nan
        return px.iloc[j] / px.iloc[i] - 1

    # Implizite Richtung: Long öffnen/aufstocken = bullisch, Short öffnen = bärisch,
    # reduzieren/schließen = Gegenrichtung der Position.
    def direction(r) -> int:
        bull = 1 if r.side == "long" else -1
        return bull if r.event in ("open", "add", "flip") else -bull

    ev["dir"] = ev.apply(direction, axis=1)
    for h, secs in (("1h", 3600), ("4h", 14400), ("24h", 86400)):
        ev[f"r{h}"] = ev.ts.apply(lambda t: fwd(int(t), secs))
        ev[f"hit{h}"] = np.sign(ev[f"r{h}"]) == ev["dir"]
        ev[f"signed{h}"] = ev[f"r{h}"] * ev["dir"]

    # Kontrollgruppe: gleich viele zufällige Zeitpunkte, zufällige Richtung
    rng = random.Random(7)
    lo, hi = int(px.index[0]), int(px.index[-1]) - 86400
    ctrl = pd.DataFrame({"ts": [rng.randint(lo, hi) for _ in range(max(len(ev) * 5, 200))]})
    ctrl["dir"] = [rng.choice((1, -1)) for _ in range(len(ctrl))]
    for h, secs in (("1h", 3600), ("4h", 14400), ("24h", 86400)):
        ctrl[f"r{h}"] = ctrl.ts.apply(lambda t: fwd(int(t), secs))
        ctrl[f"hit{h}"] = np.sign(ctrl[f"r{h}"]) == ctrl["dir"]
        ctrl[f"signed{h}"] = ctrl[f"r{h}"] * ctrl["dir"]

    print(f"\nWal-Ereignisse {coin}: {len(ev)} (Kontrolle: {len(ctrl)} Zufallszeitpunkte)")
    print("=" * 66)
    print(f"  {'':<10}{'Treffer':>9}{'Kontrolle':>11}{'Ø signiert':>13}{'Kontrolle':>11}")
    for h in ("1h", "4h", "24h"):
        e = ev[f"hit{h}"].dropna(); c = ctrl[f"hit{h}"].dropna()
        print(f"  {h:<10}{e.mean():>8.1%}{c.mean():>11.1%}"
              f"{ev[f'signed{h}'].mean():>+13.2%}{ctrl[f'signed{h}'].mean():>+11.2%}")

    print("\n  Nach Ereignistyp (4 h):")
    for evt, grp in ev.groupby("event"):
        if len(grp) >= 5:
            print(f"    {evt:<8} n={len(grp):<4} Treffer {grp['hit4h'].mean():.0%}  "
                  f"Ø signiert {grp['signed4h'].mean():+.2%}")

    # Reagiert der Kurs schon in der Minute der Aktion? Dann ist es Impact, nicht Wissen.
    ev["r5m"] = ev.ts.apply(lambda t: fwd(int(t), 300)) * ev["dir"]
    print(f"\n  Bewegung in den ersten 5 Minuten, signiert: {ev['r5m'].mean():+.3%}")
    print("  Groß und positiv = die Wal-Order bewegt den Kurs selbst (Impact).")
    print("  Nahe null, aber 4h/24h positiv = eher Information als Impact.")

    print("\n  Einordnung: Ein Vorsprung gegenüber der Kontrolle ist noch kein")
    print("  Insiderwissen. Ein großer Short kann eine abgesicherte Spot-Position")
    print("  sein, und was jeder sehen kann, ist im Kurs meist schon drin.")


# ---------------------------------------------------------------------------
# Export: kompaktes Paket zum Weitergeben oder Hochladen
# ---------------------------------------------------------------------------

def export_bundle(con: sqlite3.Connection, out_path: str,
                  coin: str | None = None, with_ticks: bool = False) -> None:
    """
    Schreibt ein ZIP mit allem, was zur Auswertung nötig ist -- ohne die
    Rohdaten. Die Datenbank wird nach Monaten hunderte Megabyte groß und ist
    nicht mehr handhabbar; die Ereignisse und Kennzahlen daraus passen in
    wenige hundert Kilobyte.

    Enthalten:
      cluster_events.csv   die gelabelten Berührungen, ein Datensatz je Ereignis
      daily_coverage.csv   je Coin und Tag: Bars, Snapshots, Liquidationen
      time_heatmap.csv     die Wochentag-x-Uhrzeit-Statistik, falls vorhanden
      binance_hourly.csv   stündlich aggregierte Binance-Liquidationen
      meta.json            Abdeckung, Zeitraum, Datenqualität
    """
    import csv
    import io
    import zipfile

    def rows_to_csv(cur) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([d[0] for d in cur.description])
        w.writerows(cur.fetchall())
        return buf.getvalue()

    def table_exists(name: str) -> bool:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None

    where, args = ("WHERE coin=?", (coin,)) if coin else ("", ())
    parts: dict[str, str] = {}

    # --- Ereignisse, mit abgeleiteten Zeitspalten für die Auswertung
    if table_exists("cluster_events"):
        cur = con.execute(f"""
            SELECT *,
                   CAST(strftime('%w', ts, 'unixepoch') AS INTEGER) AS weekday_sun0,
                   CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) AS hour_utc,
                   CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) * 2
                     + CAST(strftime('%M', ts, 'unixepoch') AS INTEGER) / 30
                     AS slot_utc,
                   datetime(ts, 'unixepoch') AS ts_utc
            FROM cluster_events {where} ORDER BY ts""", args)
        parts["cluster_events.csv"] = rows_to_csv(cur)

    # --- Tagesabdeckung: zeigt Lücken und Ausfälle auf einen Blick
    cur = con.execute(f"""
        SELECT date(ts,'unixepoch') AS tag, coin,
               COUNT(*) AS bars, SUM(volume) AS volumen,
               MIN(low) AS tief, MAX(high) AS hoch
        FROM bars {where} GROUP BY tag, coin ORDER BY tag, coin""", args)
    parts["daily_coverage.csv"] = rows_to_csv(cur)

    cur = con.execute(f"""
        SELECT date(ts,'unixepoch') AS tag, coin,
               COUNT(DISTINCT ts) AS snapshots,
               COUNT(*) AS zeilen,
               SUM(notional) / COUNT(DISTINCT ts) AS notional_je_snapshot
        FROM heatmap {where} GROUP BY tag, coin ORDER BY tag, coin""", args)
    parts["daily_heatmap.csv"] = rows_to_csv(cur)

    if table_exists("tick_events"):
        parts["tick_events.csv"] = rows_to_csv(con.execute(
            f"SELECT id, datetime(t_trigger/1000,'unixepoch') AS trigger_utc, * "
            f"FROM tick_events {where} ORDER BY t_trigger", args))
        if with_ticks:
            parts["ticks.csv"] = rows_to_csv(con.execute(
                f"SELECT * FROM ticks {where} ORDER BY event_id, ts_ms", args))
            parts["mark_ticks.csv"] = rows_to_csv(con.execute(
                f"SELECT * FROM mark_ticks {where} ORDER BY event_id, ts_ms", args))
            if table_exists("book_ticks"):
                parts["book_ticks.csv"] = rows_to_csv(con.execute(
                    f"SELECT * FROM book_ticks {where} ORDER BY event_id, ts_ms", args))
    for t in ("book_summary", "bars10", "proximity"):
        if table_exists(t) and with_ticks:
            parts[f"{t}.csv"] = rows_to_csv(con.execute(
                f"SELECT * FROM {t} {where} ORDER BY ts", args))

    if table_exists("magnet_events"):
        parts["magnet_events.csv"] = rows_to_csv(con.execute(
            f"SELECT datetime(ts,'unixepoch') AS ts_utc, * FROM magnet_events {where} "
            "ORDER BY ts", args))

    if table_exists("whale_positions"):
        parts["whale_events.csv"] = rows_to_csv(con.execute(
            f"SELECT datetime(ts,'unixepoch') AS ts_utc, * FROM whale_positions "
            f"{where.replace('coin', 'whale_positions.coin') if where else ''} "
            "ORDER BY ts", args))

    if table_exists("coverage"):
        parts["coverage.csv"] = rows_to_csv(con.execute(
            f"SELECT datetime(ts,'unixepoch') AS ts_utc, * FROM coverage {where} ORDER BY ts",
            args))
    if table_exists("ctx_bars"):
        parts["market_context_hourly.csv"] = rows_to_csv(con.execute(f"""
            SELECT strftime('%Y-%m-%dT%H', ts, 'unixepoch') AS stunde, coin,
                   AVG(mark_px) AS mark, AVG(oracle_px) AS oracle,
                   AVG(funding) AS funding, AVG(oi) AS oi, AVG(premium) AS premium
            FROM ctx_bars {where} GROUP BY stunde, coin ORDER BY stunde, coin""", args))

    if table_exists("snapshot_meta"):
        parts["snapshot_quality.csv"] = rows_to_csv(con.execute(
            "SELECT datetime(ts,'unixepoch') AS ts_utc, * FROM snapshot_meta ORDER BY ts"))

    if table_exists("time_heatmap"):
        parts["time_heatmap.csv"] = rows_to_csv(
            con.execute("SELECT * FROM time_heatmap ORDER BY coin, threshold, "
                        "weekday, slot"))

    if table_exists("bnc_liquidations"):
        parts["binance_hourly.csv"] = rows_to_csv(con.execute("""
            SELECT strftime('%Y-%m-%dT%H', ts/1000, 'unixepoch') AS stunde, coin,
                   COUNT(*) AS anzahl,
                   SUM(CASE WHEN side='long'  THEN notional ELSE 0 END) AS long_liq,
                   SUM(CASE WHEN side='short' THEN notional ELSE 0 END) AS short_liq
            FROM bnc_liquidations GROUP BY stunde, coin ORDER BY stunde, coin"""))

    # --- Metadaten: Zeitraum, Umfang, Datenqualität
    now = int(time.time())
    span = con.execute("SELECT MIN(ts), MAX(ts) FROM bars").fetchone()
    ages = sorted(now - r[0] for r in con.execute(
        "SELECT last_poll FROM addresses WHERE notional>0 AND last_poll>0"))
    per_coin = {}
    for c, n, lo, hi in con.execute(
            "SELECT coin, COUNT(*), MIN(ts), MAX(ts) FROM bars GROUP BY coin"):
        snaps = con.execute(
            "SELECT COUNT(DISTINCT ts) FROM heatmap WHERE coin=?", (c,)).fetchone()[0]
        per_coin[c] = {"bars": n, "snapshots": snaps,
                       "von": datetime_utc(lo), "bis": datetime_utc(hi),
                       "abdeckung_prozent": round(
                           n / max((hi - lo) / 60, 1) * 100, 1)}
    meta = {
        "erstellt": datetime_utc(now),
        "zeitraum": {"von": datetime_utc(span[0]) if span[0] else None,
                     "bis": datetime_utc(span[1]) if span[1] else None,
                     "tage": round((span[1] - span[0]) / 86400, 1) if span[0] else 0},
        "coins": per_coin,
        "adressen": con.execute("SELECT COUNT(*) FROM addresses").fetchone()[0],
        "adressen_mit_position": con.execute(
            "SELECT COUNT(*) FROM addresses WHERE notional>0").fetchone()[0],
        "datenalter_median_min": (ages[len(ages)//2] // 60) if ages else None,
        "datenalter_p90_min": (ages[int(len(ages)*0.9)] // 60) if ages else None,
        "cluster_events": con.execute(
            "SELECT COUNT(*) FROM cluster_events").fetchone()[0]
            if table_exists("cluster_events") else 0,
        "hinweis": "Aggregate und gelabelte Ereignisse, keine Rohdaten. "
                   "Die vollständige Datenbank verbleibt auf dem Aufzeichnungsrechner.",
    }
    parts["meta.json"] = json.dumps(meta, indent=2, ensure_ascii=False)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, content in parts.items():
            z.writestr(name, content)

    size = os.path.getsize(out_path)
    print(f"\nExport: {out_path} ({size/1024:.0f} kB)")
    for name, content in parts.items():
        lines = content.count("\n")
        print(f"  {name:<22} {lines:>8,} Zeilen")
    if meta["cluster_events"] == 0:
        print("\nAchtung: keine cluster_events enthalten. Erst --analyze laufen "
              "lassen, sonst fehlt der eigentliche Inhalt.")
    if size > 20_000_000:
        print("\nDas Paket ist recht groß. Mit --coin BTC auf einen Coin "
              "einschränken.")


# ---------------------------------------------------------------------------
# Backup: täglich, verifiziert, atomar ersetzt
# ---------------------------------------------------------------------------

BACKUP_NAME = "hl_liq_backup.db"


def do_backup(db_path: str, backup_dir: str) -> dict:
    """
    Konsistente Kopie im laufenden Betrieb über die SQLite-Backup-API.

    Reihenfolge ist entscheidend: erst in eine temporäre Datei kopieren, dann
    die Kopie mit integrity_check prüfen, erst dann das alte Backup atomar
    ersetzen. Ist die Quelle bereits beschädigt, scheitert die Prüfung und
    das letzte gute Backup bleibt unangetastet.
    """
    os.makedirs(backup_dir, exist_ok=True)
    tmp = os.path.join(backup_dir, BACKUP_NAME + ".tmp")
    final = os.path.join(backup_dir, BACKUP_NAME)
    info_path = os.path.join(backup_dir, "backup.json")
    t0 = time.time()
    result = {"ts": int(t0), "ok": False, "size": 0, "seconds": 0.0, "error": None}

    def clean_tmp():
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(tmp + suffix)
            except OSError:
                pass

    try:
        clean_tmp()
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(tmp)
        # gedrosselt: 2 MB je Schritt, danach 50 ms Pause -> höchstens ~40 MB/s.
        # Ungebremst legt ein 1-GB-Backup einen Pi für die Dauer lahm.
        # (Der sleep-Parameter der API pausiert nur bei gesperrter Quelle;
        #  die Pause je Schritt muss in den Fortschritts-Callback.)
        src.backup(dst, pages=512, progress=lambda status, remaining, total: time.sleep(0.05))
        src.close()
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        n_bars = dst.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        dst.close()
        if check != "ok":
            clean_tmp()
            result["error"] = f"Kopie fehlerhaft: {check[:120]}"
        else:
            os.replace(tmp, final)                # atomar: alt weg, neu da
            for suffix in ("-wal", "-shm"):       # Reste der Kopie
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass
            result.update(ok=True, size=os.path.getsize(final), bars=n_bars)
    except Exception as e:
        result["error"] = str(e)
        clean_tmp()
    result["seconds"] = round(time.time() - t0, 1)
    try:
        prev = json.load(open(info_path)) if os.path.exists(info_path) else {}
        if not result["ok"]:
            # Das letzte gute Backup bleibt liegen -- den Zeitpunkt mitführen
            result["last_good"] = prev.get("ts") if prev.get("ok") else prev.get("last_good")
        json.dump(result, open(info_path, "w"), indent=2)
    except OSError:
        pass
    return result


def resolve_backup_dir(con: "sqlite3.Connection | None", db_path: str,
                       explicit: str | None = None) -> str:
    """
    Eine Quelle für alle: der Recorder trägt seinen Backup-Ordner in meta ein,
    Dashboard, Wächter und Audit lesen ihn dort. Vorrang: --backup-dir,
    dann meta, dann 'dbbackup' neben der Datenbank.
    """
    if explicit:
        return os.path.abspath(explicit)
    if con is not None:
        try:
            r = con.execute("SELECT v FROM meta WHERE k='backup_dir'").fetchone()
            if r and r[0]:
                return r[0]
        except sqlite3.Error:
            pass
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "dbbackup")


def device_of(path: str) -> int:
    """st_dev des nächsten existierenden Vorfahren -- auch für Pfade, die es noch nicht gibt."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return os.stat(p).st_dev


def same_device(a: str, b: str) -> bool:
    try:
        return device_of(a) == device_of(b)
    except OSError:
        return True


def backup_status(backup_dir: str) -> dict | None:
    p = os.path.join(backup_dir, "backup.json")
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Audit: passen die Tabellen zueinander?
# ---------------------------------------------------------------------------

def audit(con: sqlite3.Connection) -> bool:
    """
    Prüft die Datenbank auf innere Widersprüche. Der Wächter fragt, ob die
    Daten frisch sind; hier geht es darum, ob sie *stimmen*: Ausrichtung der
    Zeitstempel, Vollständigkeit der Begleittabellen, Konsistenz zwischen
    Heatmap und Qualitätsstempel, Lebenszyklen der Wale, Duplikate.
    """
    ok_all = True
    now = int(time.time())
    OK, WARN, FAIL = "  ok   ", "  !    ", "  FEHLT"

    def has(t):
        return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()

    def line(level, name, detail):
        nonlocal ok_all
        if level == FAIL:
            ok_all = False
        print(f"{level} {name:<22} {detail}")

    print("\nAudit der Datenbank")
    print("-" * 70)

    # --- Bars: Minutenausrichtung, Lücken
    for coin, n, mis, lo, hi in con.execute(
            "SELECT coin, COUNT(*), SUM(ts % 60 != 0), MIN(ts), MAX(ts) "
            "FROM bars GROUP BY coin"):
        span_min = (hi - lo) // 60 + 1
        gaps = span_min - n
        level = OK if mis == 0 and gaps / max(span_min, 1) < 0.05 else WARN
        line(level, f"Bars {coin}",
             f"{n:,} Kerzen, {mis} nicht minutengenau, "
             f"{gaps:,} fehlende Minuten ({gaps/max(span_min,1):.1%})")
    if not con.execute("SELECT 1 FROM bars LIMIT 1").fetchone():
        line(FAIL, "Bars", "leer")

    # --- Marktkontext gegen Handelspreis
    if has("ctx_bars"):
        r = con.execute("""
            SELECT b.coin, COUNT(*),
                   AVG(ABS(c.mark_px / b.close - 1)),
                   SUM(ABS(c.mark_px / b.close - 1) > 0.005)
            FROM bars b JOIN ctx_bars c ON c.ts = b.ts AND c.coin = b.coin
            WHERE c.mark_px > 0 GROUP BY b.coin""").fetchall()
        if not r:
            line(WARN, "Markpreis", "noch keine ctx_bars — Feed activeAssetCtx aktiv?")
        for coin, n, dev, big in r:
            nb = con.execute("SELECT COUNT(*) FROM bars WHERE coin=?", (coin,)).fetchone()[0]
            level = OK if big / max(n, 1) < 0.02 and n / max(nb, 1) > 0.8 else WARN
            line(level, f"Markpreis {coin}",
                 f"{n:,} von {nb:,} Minuten, Ø Abweichung zum Trade "
                 f"{dev*100:.3f} %, {big} Minuten über 0,5 %")

    # --- Heatmap gegen Qualitätsstempel: Positionszahl muss exakt übereinstimmen
    if has("snapshot_meta"):
        r = con.execute("""
            SELECT COUNT(*),
                   SUM(m.ts IS NULL),
                   SUM(m.ts IS NOT NULL AND h.n != m.n_positions)
            FROM (SELECT ts, SUM(n_pos) AS n FROM heatmap GROUP BY ts) h
            LEFT JOIN snapshot_meta m ON m.ts = h.ts""").fetchone()
        n, no_meta, mismatch = r
        if n:
            level = OK if no_meta == 0 and mismatch == 0 else (WARN if mismatch == 0 else FAIL)
            line(level, "Snapshot-Konsistenz",
                 f"{n:,} Snapshots, {no_meta} ohne Stempel, "
                 f"{mismatch} mit abweichender Positionszahl")
        else:
            line(WARN, "Snapshot-Konsistenz", "noch keine Snapshots")

        # Snapshot-Abstände
        ts = [r[0] for r in con.execute("SELECT ts FROM snapshot_meta ORDER BY ts")]
        if len(ts) > 2:
            d = sorted(b - a for a, b in zip(ts, ts[1:]))
            med = d[len(d) // 2]
            odd = sum(1 for x in d if x > med * 1.5 or x < med * 0.5)
            line(OK if odd / len(d) < 0.05 else WARN, "Snapshot-Takt",
                 f"Median {med//60} min, {odd} von {len(d)} Abstände unregelmäßig")
            restarts = con.execute(
                "SELECT COUNT(*) FROM snapshot_meta WHERE uptime < 900").fetchone()[0]
            line(OK, "Neustarts", f"{restarts} Snapshots innerhalb 15 min nach Start "
                                  "(in der Auswertung ausschließbar)")

    # --- Abdeckung
    if has("coverage"):
        for coin, n, avg, mx in con.execute(
                "SELECT coin, COUNT(*), AVG(ratio), MAX(ratio) FROM coverage GROUP BY coin"):
            level = OK if mx <= 1.2 and avg >= 0.25 else WARN
            line(level, f"Abdeckung {coin}",
                 f"{n:,} Messungen, Ø {avg*100:.0f} %, max {mx*100:.0f} %"
                 + ("  — über 120 % deutet auf falsches OI oder Doppelzählung" if mx > 1.2 else "")
                 + ("  — unter 25 % kaum repräsentativ" if avg < 0.25 else ""))
        if not con.execute("SELECT 1 FROM coverage LIMIT 1").fetchone():
            line(WARN, "Abdeckung", "noch keine Messung — braucht ctx_bars mit OI")

    # --- Adressen
    r = con.execute("""SELECT COUNT(*), SUM(last_poll = 0), SUM(pos_value > 0),
                              SUM(pos_value >= 20000000)
                       FROM addresses""").fetchone()
    if r[0]:
        never = r[1] or 0
        line(OK if never / r[0] < 0.5 else WARN, "Adressen",
             f"{r[0]:,} bekannt, {never:,} nie abgefragt ({never/r[0]:.0%}), "
             f"{r[2] or 0:,} mit Position, {r[3] or 0} Wale")

    # --- Wal-Lebenszyklen: kein open auf open, kein close ohne open
    if has("whale_positions"):
        bad_open = bad_close = 0
        state = {}
        for addr, coin, ev in con.execute(
                "SELECT addr, coin, event FROM whale_positions "
                "WHERE event != 'snap' ORDER BY ts, addr, coin"):
            k = (addr, coin); is_open = state.get(k, False)
            if ev == "open":
                if is_open:
                    bad_open += 1
                state[k] = True
            elif ev == "close":
                if not is_open:
                    bad_close += 1
                state[k] = False
            elif not is_open:
                bad_close += 1         # add/reduce/flip ohne open
                state[k] = True
        n = con.execute("SELECT COUNT(*) FROM whale_positions WHERE event != 'snap'").fetchone()[0]
        if n:
            level = OK if bad_open == 0 and bad_close == 0 else FAIL
            line(level, "Wal-Lebenszyklen",
                 f"{n} Ereignisse, {bad_open} doppelte Eröffnungen, "
                 f"{bad_close} Änderungen ohne Eröffnung"
                 + ("  — typisch für Neustart ohne Wiederherstellung" if bad_open else ""))
        else:
            line(OK, "Wal-Lebenszyklen", "noch keine Wal-Ereignisse")

    # --- Liquidationen
    r = con.execute("""SELECT COUNT(*), SUM(side NOT IN ('A','B')),
                              SUM(px <= 0 OR sz <= 0) FROM liquidations""").fetchone()
    if r[0]:
        line(OK if not r[1] and not r[2] else WARN, "Liquidationen",
             f"{r[0]:,} Ereignisse, {r[1] or 0} mit unbekannter Seite, "
             f"{r[2] or 0} mit ungültigem Preis/Menge")

    # --- Cluster-Events: Duplikate, Bezug zu Snapshots
    if has("cluster_events"):
        n = con.execute("SELECT COUNT(*) FROM cluster_events").fetchone()[0]
        if n:
            dup = n - con.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT ts, coin, cluster_px, side "
                "FROM cluster_events)").fetchone()[0]
            orphan = con.execute(
                "SELECT COUNT(*) FROM cluster_events e WHERE NOT EXISTS "
                "(SELECT 1 FROM heatmap h WHERE h.ts = e.ts AND h.coin = e.coin)").fetchone()[0]
            line(OK if dup == 0 and orphan == 0 else FAIL, "Cluster-Events",
                 f"{n:,} Ereignisse, {dup} Duplikate, {orphan} ohne Snapshot")

    # --- Backup
    bdir = resolve_backup_dir(con, DB_PATH)
    st = backup_status(bdir)
    if st and st.get("ok"):
        age_h = (now - st["ts"]) / 3600
        line(OK if age_h < 26 else WARN, "Backup",
             f"vor {age_h:.1f} h, {st['size']/1e6:.0f} MB, "
             f"{st.get('bars', 0):,} Bars in {bdir}/{BACKUP_NAME}")
    elif st:
        lg = st.get("last_good")
        line(WARN, "Backup", f"letzter Versuch fehlgeschlagen: {st.get('error')}"
             + (f" — gutes Backup von vor {(now-lg)/3600:.1f} h liegt noch vor" if lg else ""))
    else:
        line(WARN, "Backup", "noch keines — läuft 5 min nach Recorder-Start, dann täglich")

    # --- Orderbuch-Kennzahlen
    if has("book_summary"):
        r = con.execute("SELECT COUNT(*), MAX(ts), SUM(spread_bps <= 0 OR bid_50 < 0) "
                        "FROM book_summary").fetchone()
        if r[0]:
            age = (now - r[1]) // 60
            line(OK if age < 10 and not r[2] else WARN, "Orderbuch",
                 f"{r[0]:,} Kennzahlen, jüngste vor {age} min, {r[2] or 0} ungültig")
        else:
            line(WARN, "Orderbuch", "noch keine Kennzahlen — Feed l2Book aktiv?")
    if has("bars10"):
        r = con.execute("SELECT COUNT(*), MAX(ts) FROM bars10").fetchone()
        if r[0]:
            line(OK if now - r[1] < 300 else WARN, "10-s-Bars",
                 f"{r[0]:,} Bars, jüngste vor {(now - r[1])//60} min")

    # --- Tick-Aufzeichnungen
    if has("tick_events"):
        r = con.execute("""SELECT COUNT(*), SUM(complete = 0), SUM(n_ticks = 0),
                                  MIN(n_ticks), AVG(n_ticks) FROM tick_events""").fetchone()
        if r[0]:
            level = OK if not r[1] and not r[2] else WARN
            line(level, "Tick-Aufzeichnungen",
                 f"{r[0]} Ereignisse, {r[1] or 0} unvollständig, {r[2] or 0} leer, "
                 f"Ø {r[4]:,.0f} Ticks")
        else:
            line(OK, "Tick-Aufzeichnungen", "noch keine — löst bei 1 %/60 s, "
                                            "0,4 %/10 s oder 5 Liqs/30 s aus")

    # --- Binance
    for t, label in (("bnc_liquidations", "Binance Liqs"), ("bnc_metrics", "Binance Metriken")):
        if has(t):
            r = con.execute(f"SELECT COUNT(*), MAX(ts) FROM {t}").fetchone()
            if r[0]:
                ts_s = r[1] // 1000 if r[1] > 1e11 else r[1]
                age = (int(time.time()) - ts_s) // 60
                line(OK if age < 120 else WARN, label, f"{r[0]:,} Zeilen, jüngste vor {age} min")

    print("-" * 70)
    print("Ergebnis:", "keine Widersprüche gefunden" if ok_all
          else "FEHLER gefunden — Details oben")
    return ok_all


def datetime_utc(ts) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def status(con: sqlite3.Connection) -> None:
    q = lambda s: con.execute(s).fetchone()
    print("Datenbank:", DB_PATH, f"({os.path.getsize(DB_PATH)/1e6:.1f} MB)")
    print(f"  Adressen           {q('SELECT COUNT(*) FROM addresses')[0]:,}")
    print(f"  davon mit Position {q('SELECT COUNT(*) FROM addresses WHERE notional>0')[0]:,}")
    print(f"  Heatmap-Zeilen     {q('SELECT COUNT(*) FROM heatmap')[0]:,}")
    print(f"  Bars               {q('SELECT COUNT(*) FROM bars')[0]:,}")
    print(f"  Liquidationen      {q('SELECT COUNT(*) FROM liquidations')[0]:,}")
    now = int(time.time())
    ages = sorted(now - r[0] for r in con.execute(
        "SELECT last_poll FROM addresses WHERE notional > 0 AND last_poll > 0"))
    if ages:
        print(f"  Datenalter Median  {ages[len(ages)//2]//60} min")
        print(f"  Datenalter 90%     {ages[int(len(ages)*0.9)]//60} min")
        if ages[int(len(ages)*0.9)] > 3600:
            print("  -> über eine Stunde alt: der Poller hängt hinterher")
    r = q("SELECT MIN(ts), MAX(ts) FROM bars")
    if r[0]:
        span = (r[1] - r[0]) / 86400
        print(f"  Zeitraum           {span:.1f} Tage")
        if span < 14:
            print("  -> für belastbare Statistik deutlich zu wenig")


# ---------------------------------------------------------------------------

def main() -> None:
    global DB_PATH
    p = argparse.ArgumentParser(description="Hyperliquid Liquidations-Recorder")
    p.add_argument("--coins", nargs="+", default=["BTC", "ETH", "HYPE", "ZEC", "XMR", "PAXG"])
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--bucket-pct", type=float, default=0.0025)
    p.add_argument("--snapshot-sec", type=int, default=600,
                   help="Abstand der Heatmap-Snapshots in Sekunden")
    p.add_argument("--flush-sec", type=int, default=10,
                   help="Commit-Takt in Sekunden")
    p.add_argument("--durable", action="store_true",
                   help="synchronous=FULL: kein Verlust bei Stromausfall, "
                        "dafür mehr Schreibzugriffe")
    p.add_argument("--no-discover-all", action="store_true",
                   help="Adressen nur aus den verfolgten Coins sammeln")
    p.add_argument("--max-pos-age", type=int, default=3600,
                   help="ältere Positionsdaten gehen nicht in den Snapshot (Sekunden)")
    p.add_argument("--tick-move", type=float, default=0.010,
                   help="Tick-Auslöser: Bewegung in 60 s, Anteil (0.01 = 1 %%)")
    p.add_argument("--tick-flash", type=float, default=0.004,
                   help="Tick-Auslöser: Bewegung in 10 s")
    p.add_argument("--tick-liqs", type=int, default=5,
                   help="Tick-Auslöser: Liquidations-Fills in 30 s")
    p.add_argument("--tick-pre", type=int, default=300, help="Vorlauf in Sekunden")
    p.add_argument("--tick-post", type=int, default=600, help="Nachlauf in Sekunden")
    p.add_argument("--polls-per-min", type=int, default=DEFAULT_POLLS_PER_MIN,
                   help="Abfragebudget; clearinghouseState hat Gewicht 2 bei 1200/min")
    p.add_argument("--hot-refresh", type=int, default=180,
                   help="Abstand für große Positionen in Sekunden")
    p.add_argument("--cold-refresh", type=int, default=1800,
                   help="Abstand für kleine Positionen in Sekunden")
    p.add_argument("--whale-notional", type=float, default=20_000_000,
                   help="ab diesem Positionswert (USD) gilt ein Wallet als Wal")
    p.add_argument("--whale-refresh", type=int, default=60,
                   help="Abstand für Wale in Sekunden")
    p.add_argument("--hot-notional", type=float, default=250_000,
                   help="ab welchem Positionswert eine Adresse als groß gilt")
    p.add_argument("--min-notional", type=float, default=25_000,
                   help="darunter nur noch alle 2 Stunden abfragen")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--coin", default="BTC", help="Coin für --analyze")
    p.add_argument("--min-x-vol", type=float, default=0.5)
    p.add_argument("--max-gap", type=int, default=120,
                   help="größter erlaubter Abstand Snapshot zu Kursbar in Sekunden")
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--audit", action="store_true",
                   help="Datenbank auf innere Widersprüche prüfen")
    p.add_argument("--backup", action="store_true",
                   help="sofort ein verifiziertes Backup erstellen")
    p.add_argument("--backup-dir", default=None,
                   help="Zielordner, Standard: dbbackup neben der Datenbank")
    p.add_argument("--magnet-analyze", action="store_true",
                   help="Kernthese prüfen: zieht das größere Cluster den Kurs an?")
    p.add_argument("--magnet-range", type=float, default=0.06,
                   help="Suchbereich für Cluster ober-/unterhalb, Anteil vom Kurs")
    p.add_argument("--magnet-step", type=int, default=60,
                   help="Abstand der Prüfzeitpunkte in Minuten")
    p.add_argument("--whale-analyze", action="store_true",
                   help="Vorhersagekraft von Wal-Aktionen gegen Kontrollgruppe prüfen")
    p.add_argument("--with-ticks", action="store_true",
                   help="beim Export auch die Tick-Aufzeichnungen mitgeben (groß)")
    p.add_argument("--export", metavar="DATEI.zip",
                   help="Ereignisse und Kennzahlen als kompaktes ZIP ausgeben")
    args = p.parse_args()

    DB_PATH = args.db
    con = db_connect(DB_PATH, "FULL" if getattr(args, "durable", False) else "NORMAL")

    if args.status:
        status(con); return
    backup_dir = resolve_backup_dir(con, DB_PATH, args.backup_dir)
    same_disk = same_device(os.path.dirname(os.path.abspath(DB_PATH)), backup_dir)
    if args.backup:
        if args.backup_dir:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('backup_dir', ?)", (backup_dir,))
            con.commit()
        con.close()
        if same_disk:
            print("Hinweis: Backup liegt auf demselben Datenträger wie die Datenbank "
                  "— schützt vor Beschädigung, nicht vor Ausfall des Mediums.")
        res = do_backup(DB_PATH, backup_dir)
        if res["ok"]:
            print(f"Backup ok: {res['size']/1e6:.1f} MB, {res.get('bars', 0):,} Bars, "
                  f"{res['seconds']} s -> {backup_dir}/{BACKUP_NAME}")
        else:
            print(f"Backup FEHLGESCHLAGEN: {res['error']}")
        sys.exit(0 if res["ok"] else 1)
    if args.audit:
        sys.exit(0 if audit(con) else 1)
    if args.magnet_analyze:
        magnet_analyze(con, args.coin, range_pct=args.magnet_range,
                       min_x_vol=args.min_x_vol, step_min=args.magnet_step,
                       max_gap=args.max_gap); return
    if args.whale_analyze:
        whale_analyze(con, args.coin); return
    if args.export:
        export_bundle(con, args.export, args.coin if args.coin != "BTC" or
                      "--coin" in sys.argv else None, args.with_ticks)
        return
    if args.simulate:
        simulate(con); return
    if args.analyze:
        analyze(con, args.coin, args.min_x_vol, max_gap=args.max_gap); return

    if args.polls_per_min > 500:
        print(f"--polls-per-min {args.polls_per_min} überschreitet das Hyperliquid-Limit "
              "(1200 Gewicht/min, je Abfrage 2). Gedeckelt auf 500.")
        args.polls_per_min = 500
    cfg = Settings(coins=args.coins, bucket_pct=args.bucket_pct,
                   snapshot_sec=args.snapshot_sec, polls_per_min=args.polls_per_min,
                   hot_refresh=args.hot_refresh, cold_refresh=args.cold_refresh,
                   hot_notional=args.hot_notional, flush_sec=args.flush_sec,
                   whale_notional=args.whale_notional, whale_refresh=args.whale_refresh,
                   min_notional=args.min_notional,
                   synchronous="FULL" if args.durable else "NORMAL",
                   discover_all=not args.no_discover_all,
                   max_pos_age=args.max_pos_age,
                   tick_move_pct=args.tick_move, tick_flash_pct=args.tick_flash,
                   tick_liq_burst=args.tick_liqs, tick_pre_sec=args.tick_pre,
                   tick_post_sec=args.tick_post, backup_dir=backup_dir)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('backup_dir', ?)", (backup_dir,))
    con.commit()
    if same_disk:
        print(f"Backup-Ordner: {backup_dir} (gleicher Datenträger wie die Datenbank)")
    else:
        print(f"Backup-Ordner: {backup_dir} (anderer Datenträger als die Datenbank)")
    rec = Recorder(con, cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, rec.stop.set)
        except NotImplementedError:
            pass
    print(f"Recorder läuft für {', '.join(cfg.coins)}. Abbruch mit Strg-C.")
    print("Die ersten Minuten sind leer — das Adressregister muss erst wachsen.\n")
    try:
        loop.run_until_complete(rec.run())
    except KeyboardInterrupt:
        rec.stop.set()
    finally:
        con.close()


if __name__ == "__main__":
    main()
