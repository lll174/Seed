"""
hl_simdash.py — Weboberfläche für die Handelssimulation, Port 8766.

    python hl_simdash.py --db hl_liq.db --host 0.0.0.0

Liest die Recorder-Datenbank nur lesend. Es werden keine echten Orders
gesendet; alle Käufe und Verkäufe sind simuliert.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from hl_sim import Engine, SimConfig, parse_time, utc
from hl_candles import CandleEngine, choose, discover, load_rows

DB_PATH = "hl_liq.db"
RESULTS_DB = "hl_sim_results.db"


# ---------------------------------------------------------------------------
# Sitzung
# ---------------------------------------------------------------------------


class Session:
    def __init__(self):
        self.cfg = SimConfig()
        self.engine: Engine | None = None
        self.thread: threading.Thread | None = None
        self.mode = "idle"          # idle | backtest | live
        self.stop = threading.Event()
        self.error: str | None = None
        self.result: dict | None = None
        self.lock = threading.Lock()

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _make_engine(self, kind: str = "full") -> Engine:
        cls = CandleEngine if kind == "candles" else Engine
        eng = cls(DB_PATH, self.cfg, RESULTS_DB or None)
        eng.conn = ro_conn()
        if kind == "candles":
            # Quellen auf der neuen Verbindung erneut bestimmen
            eng.sources = discover(eng.conn)
            eng.source = choose(eng.sources, self.cfg.candle_source, self.cfg.coin)
        return eng

    def start_backtest(self, t_from: int, t_to: int, kind: str = "full"):
        if self.busy():
            return {"error": "Es läuft bereits ein Durchgang."}
        self.stop.clear()
        self.error = self.result = None
        self.mode = "candles" if kind == "candles" else "backtest"

        def work():
            try:
                self.engine = self._make_engine(kind)
                self.result = self.engine.run_backtest(t_from, t_to)
            except Exception as e:                      # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.mode = "idle"

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return {"ok": True}

    def start_live(self):
        if self.busy():
            return {"error": "Es läuft bereits ein Durchgang."}
        self.stop.clear()
        self.error = self.result = None
        self.mode = "live"

        def work():
            try:
                self.engine = self._make_engine()
                self.engine.run_live(stop_flag=self.stop.is_set)
            except Exception as e:                      # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.mode = "idle"

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return {"ok": True}

    def halt(self):
        self.stop.set()
        if self.engine:
            self.engine.running = False
        return {"ok": True}


SESSION = Session()


def ro_conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                        timeout=5.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# Chartdaten
# ---------------------------------------------------------------------------


def chart_data(coin: str, t_from: int, t_to: int, agg_min: int,
               src: str = "bars") -> dict:
    """
    Die Kursquelle muss dieselbe sein, auf der die Simulation handelt.
    Im Kerzen-Modus ist das klines30 oder bnc_bars, nicht `bars` — sonst
    zeigt der Chart nichts, sobald der Cursor vor den Recorder-Start laeuft.
    """
    conn = ro_conn()
    step = agg_min * 60
    rows: list[tuple] = []
    used = "bars"

    if src == "candles":
        srcs = discover(conn)
        chosen = choose(srcs, SESSION.cfg.candle_source, coin)
        if chosen:
            used = chosen.table
            rows = load_rows(conn, chosen, coin, t_from, t_to)
    if not rows:
        used = "bars" if src != "candles" else used
        cur = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM bars
               WHERE coin=? AND ts>=? AND ts<=? ORDER BY ts""",
            (coin, t_from, t_to))
        rows = [(r["ts"], r["open"], r["high"], r["low"], r["close"],
                 r["volume"] or 0) for r in cur.fetchall()]
        if src == "candles" and rows:
            used = "bars"

    buckets: dict[int, dict] = {}
    for ts, o, h, l, c, v in rows:
        k = (ts // step) * step
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"ts": k, "o": o, "h": h, "l": l, "c": c, "v": v}
        else:
            b["h"] = max(b["h"], h)
            b["l"] = min(b["l"], l)
            b["c"] = c
            b["v"] += v
    candles = [buckets[k] for k in sorted(buckets)]

    cur = conn.execute(
        "SELECT MAX(ts) t FROM heatmap WHERE coin=? AND ts<=?", (coin, t_to))
    row = cur.fetchone()
    clusters = []
    snap = row["t"] if row else None
    if snap:
        cur = conn.execute(
            """SELECT bucket_px, side, notional FROM heatmap
               WHERE coin=? AND ts=? ORDER BY notional DESC LIMIT 60""",
            (coin, snap))
        clusters = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"candles": candles, "clusters": clusters, "from": t_from, "to": t_to,
            "source": used,
            "cluster_ts": snap, "cluster_age_s": (t_to - snap) if snap else None}


# ---------------------------------------------------------------------------
# Feldbeschriftungen für das Formular
# ---------------------------------------------------------------------------

FIELDS = [
    ("Kapital", [
        ("budget_usd", "Einsatzsumme", "USD"),
        ("order_pct", "Tranche je Kauf", "% der Einsatzsumme"),
        ("order_usd", "feste Tranche, falls oben 0", "USD"),
        ("min_order_usd", "Kleinste Order", "USD"),
        ("max_lots", "Einstiegsplätze", ""),
    ]),
    ("Einstieg", [
        ("entry_min_score", "Nötige Punkte (volle Daten)", "max. 7"),
        ("candles_min_score", "Nötige Punkte (Kerzen-Test)", "max. 4"),
        ("min_seconds_between_buys", "Mindestabstand zwischen Käufen", "s"),
        ("dip_buy_pct", "Nachkauf ab Rücksetzer", "%"),
        ("dip_confirm_seconds", "Wartezeit auf Bodenbestätigung", "s"),
        ("max_add_above_avg_pct", "Nachkauf höchstens über Einstand", "%"),
        ("rsi_buy", "RSI-Schwelle für Kauf", ""),
        ("rsi_grid_min", "Raster für RSI und FVG", "min"),
        ("fvg_near_pct", "FVG gilt als am Kurs bis", "%"),
        ("book_imb_buy", "Buch-Imbalance für Kauf", ""),
        ("whale_ratio", "Wal-Verhältnis long/short", ""),
    ]),
    ("Cluster und Verkauf", [
        ("cluster_big_ratio", "Cluster gilt als groß ab × Stundenvolumen", ""),
        ("hold_for_cluster_pct", "Leiter aussetzen, wenn Cluster näher als", "%"),
        ("cluster_far_pct", "Cluster ignorieren ab Entfernung", "%"),
        ("cluster_band_pct", "Kurs gilt als im Cluster bis", "%"),
        ("max_cluster_age_s", "Cluster-Snapshot höchstens alt", "s"),
        ("ladder_rearm_factor", "Leiterstufe erneut ab × Positionsgröße", ""),
        ("sell_into_cluster_pct", "Verkauf im Cluster", "%"),
        ("sell_on_squeeze_pct", "Verkauf beim Squeeze", "%"),
        ("rsi_sell", "RSI-Schwelle für Ausstieg", ""),
    ]),
    ("Absicherung", [
        ("crash_drop_pct", "Notausstieg bei Kursverlust", "%"),
        ("crash_window_s", "gemessen über", "s"),
        ("crash_sell_pct", "Notverkauf", "%"),
        ("depth_collapse_ratio", "Bid-Tiefe eingebrochen unter × Median", ""),
        ("stop_loss_pct", "Stop unter Einstand (0 = aus)", "%"),
        ("time_stop_hours", "Zeitstop (0 = aus)", "h"),
        ("reversal_min_horizons", "Negative Horizonte für Trendumkehr", ""),
    ]),
    ("Kerzen-Test", [
        ("candles_pullback_pct", "Rücksetzer vom Hoch ab", "%"),
        ("candles_pullback_hours", "Hoch gemessen über", "h"),
        ("candles_trend_min", "Übergeordneter Trendhorizont", "min"),
        ("candles_slippage_bps", "Pauschale Slippage", "bps"),
    ]),
    ("Ausführung", [
        ("fee_bps_taker", "Taker-Gebühr", "bps"),
        ("step_seconds", "Backtest-Auflösung", "s"),
        ("live_poll_seconds", "Live-Takt", "s"),
    ]),
]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "hl_simdash"

    def log_message(self, *a):  # ruhiges Log
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = (obj if isinstance(obj, bytes)
                else json.dumps(obj, ensure_ascii=False).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(PAGE.encode(), ctype="text/html")

            if u.path == "/api/config":
                return self._send({"config": SESSION.cfg.to_dict(),
                                   "fields": FIELDS})

            if u.path == "/api/range":
                eng = Engine(DB_PATH, SESSION.cfg)
                eng.conn = ro_conn()
                lo, hi = eng.usable_range()
                ce = CandleEngine(DB_PATH, SESSION.cfg)
                ce.conn = ro_conn()
                ce.sources = discover(ce.conn)
                ce.source = choose(ce.sources, SESSION.cfg.candle_source,
                                   SESSION.cfg.coin)
                clo, chi = ce.usable_range()
                return self._send({
                    "tables": eng.data_range(),
                    "full": {"from": lo, "to": hi,
                             "from_s": utc(lo) if lo else None,
                             "to_s": utc(hi) if hi else None},
                    "candles": {"from": clo, "to": chi,
                                "from_s": utc(clo) if clo else None,
                                "to_s": utc(chi) if chi else None,
                                **ce.source_info()},
                })

            if u.path == "/api/state":
                eng = SESSION.engine
                if not eng:
                    return self._send({"mode": SESSION.mode, "empty": True})
                with SESSION.lock:
                    return self._send({
                        "mode": SESSION.mode,
                        "progress": round(eng.progress, 3),
                        "cursor": eng.last_view.ts if eng.last_view else None,
                        "error": SESSION.error,
                        "result": SESSION.result,
                        "summary": eng.summary(),
                        "lots": eng.lots_view(),
                        "marks": eng.marks[-500:],
                        "equity": eng.equity[-1500:],
                        "journal": eng.journal[-200:][::-1],
                        "note": (eng.last_view and {
                            "rsi": round(eng.last_view.rsi, 1)
                            if eng.last_view.rsi else None,
                            "cluster_age": eng.last_view.clusters.get("age_s"),
                            "imb": eng.last_view.f.imb_50,
                        }) or {},
                    })

            if u.path == "/api/chart":
                coin = q.get("coin", [SESSION.cfg.coin])[0]
                now = int(time.time())
                t_to = int(q.get("to", [now])[0])
                t_from = int(q.get("from", [t_to - 6 * 3600])[0])
                agg = int(q.get("agg", [5])[0])
                src = q.get("src", ["bars"])[0]
                return self._send(chart_data(coin, t_from, t_to, agg, src))

        except Exception as e:                          # noqa: BLE001
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "unbekannter Pfad"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/config":
                SESSION.cfg = SimConfig.from_dict(payload)
                return self._send({"ok": True, "config": SESSION.cfg.to_dict()})

            if u.path == "/api/backtest":
                t_from = parse_time(str(payload.get("from", "2026-09-01")))
                t_to = parse_time(str(payload.get("to", "now")))
                kind = payload.get("kind", "full")
                return self._send(SESSION.start_backtest(t_from, t_to, kind))

            if u.path == "/api/live":
                return self._send(SESSION.start_live())

            if u.path == "/api/stop":
                return self._send(SESSION.halt())

        except Exception as e:                          # noqa: BLE001
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "unbekannter Pfad"}, 404)


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Handelssimulation — Liquidationscluster</title>
<style>
:root{
  --ground:#131a20; --panel:#1c242c; --panel2:#222c35; --rule:#2c3742;
  --text:#d6dee6; --muted:#7e8c99;
  --up:#3e9e8c; --down:#c0604e;
  --short:#e0a33e; --long:#8c6fd1;
  --buy:#4fb89c; --sell:#d96c8a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--text);
  font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.num{font-variant-numeric:tabular-nums}
header{display:flex;gap:18px;align-items:baseline;flex-wrap:wrap;
  padding:14px 20px;border-bottom:1px solid var(--rule);background:var(--panel)}
header h1{font-size:16px;font-weight:600;margin:0;letter-spacing:.01em}
header .sub{color:var(--muted);font-size:12.5px}
header .spacer{flex:1}
.stat{display:flex;flex-direction:column}
.stat b{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}
.stat span{font-size:11.5px;color:var(--muted)}
.pos{color:var(--up)} .neg{color:var(--down)}
main{display:grid;grid-template-columns:290px 1fr 300px;gap:1px;
  background:var(--rule);min-height:calc(100vh - 58px)}
section{background:var(--ground);padding:16px 18px;overflow:auto}
h2{font-size:12.5px;font-weight:600;color:var(--muted);margin:0 0 10px}
fieldset{border:none;border-top:1px solid var(--rule);margin:0 0 14px;padding:12px 0 0}
legend{display:none}
.grp{font-size:12px;color:var(--muted);margin:14px 0 8px}
label{display:flex;justify-content:space-between;align-items:center;gap:10px;
  margin-bottom:7px;font-size:12.5px}
label span{flex:1;color:var(--text)}
label em{color:var(--muted);font-style:normal;font-size:11px}
input,select{background:var(--panel2);border:1px solid var(--rule);color:var(--text);
  border-radius:3px;padding:5px 7px;width:88px;text-align:right;
  font:inherit;font-variant-numeric:tabular-nums}
input:focus,select:focus{outline:2px solid var(--short);outline-offset:1px}
input.wide{width:130px;text-align:left}
button{background:var(--panel2);border:1px solid var(--rule);color:var(--text);
  border-radius:3px;padding:7px 13px;font:inherit;cursor:pointer}
button:hover{background:#2a3540}
button.go{background:var(--up);border-color:var(--up);color:#0d1613;font-weight:600}
button.halt{background:var(--down);border-color:var(--down);color:#1a0f0c}
button:disabled{opacity:.45;cursor:default}
.row{display:flex;gap:8px;align-items:center;margin-bottom:9px;flex-wrap:wrap}
canvas{width:100%;display:block;border:1px solid var(--rule);border-radius:3px;
  background:var(--panel)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;font-weight:500;color:var(--muted);padding:5px 6px;
  border-bottom:1px solid var(--rule);position:sticky;top:0;background:var(--ground)}
td{padding:5px 6px;border-bottom:1px solid #232c34;font-variant-numeric:tabular-nums}
td.r,th.r{text-align:right}
.tag{display:inline-block;width:15px;height:15px;line-height:15px;text-align:center;
  border-radius:2px;font-size:10px;font-weight:700;color:#101820}
.tag.b{background:var(--buy)} .tag.s{background:var(--sell)}
.jrn{max-height:240px;overflow:auto;font-size:12px}
.jrn div{padding:5px 0;border-bottom:1px solid #232c34;display:flex;gap:9px}
.jrn time{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.ladder input{width:64px}
.hint{color:var(--muted);font-size:11.5px;margin:6px 0 12px}
.follow{display:flex;align-items:center;gap:6px;font-size:12.5px;
  color:var(--muted);margin:0}
.follow input{width:auto;margin:0}
.follow span{flex:0 0 auto}
.bar{height:3px;background:var(--panel2);border-radius:2px;overflow:hidden;margin:8px 0}
.bar i{display:block;height:100%;background:var(--short);width:0}
@media(max-width:1100px){main{grid-template-columns:1fr}}
</style></head><body>

<header>
  <div><h1>Handelssimulation</h1>
    <div class="sub" id="range">Datenbereich wird geladen …</div></div>
  <div class="spacer"></div>
  <div class="stat"><b id="s-equity" class="num">—</b><span>Kontostand</span></div>
  <div class="stat"><b id="s-deployed" class="num">—</b><span>eingesetzt</span></div>
  <div class="stat"><b id="s-cash" class="num">—</b><span>frei</span></div>
  <div class="stat"><b id="s-real" class="num">—</b><span>realisiert</span></div>
  <div class="stat"><b id="s-unreal" class="num">—</b><span>offen</span></div>
  <div class="stat"><b id="s-trades" class="num">—</b><span>Käufe / Verkäufe</span></div>
  <div class="stat"><b id="s-fees" class="num">—</b><span>Gebühren</span></div>
</header>

<main>
<section>
  <h2>Parameter</h2>
  <div class="row">
    <select id="mode">
      <option value="candles">Kerzen-Test — nur Chartdaten</option>
      <option value="full">Volle Daten — mit Clustern und Buch</option>
      <option value="live">Live mitlaufen</option>
    </select>
  </div>
  <div class="row" id="daterow">
    <input class="wide" id="t-from" value="2026-09-01">
    <input class="wide" id="t-to" value="now">
  </div>
  <div class="row">
    <button class="go" id="run">Simulation starten</button>
    <button class="halt" id="stop">Anhalten</button>
  </div>
  <div class="bar"><i id="prog"></i></div>
  <div class="hint" id="msg"></div>

  <div class="grp">Verkaufsleiter</div>
  <div class="hint">Prozentwerte beziehen sich auf die Ausgangsposition, nicht
    auf den Restbestand. Die Leiter setzt aus, solange ein großes Short-Cluster
    nah über dem Kurs steht.</div>
  <div id="ladder" class="ladder"></div>
  <div class="row"><button id="ladder-add">Stufe hinzufügen</button>
    <span class="hint" id="runner" style="margin:0"></span></div>

  <div id="form"></div>
</section>

<section>
  <div class="row">
    <span class="sub" id="chart-note"></span>
    <div class="spacer" style="flex:1"></div>
    <select id="agg">
      <option value="1">1 min</option><option value="5">5 min</option>
      <option value="15">15 min</option><option value="30" selected>30 min</option>
      <option value="60">1 h</option><option value="240">4 h</option>
      <option value="1440">1 Tag</option>
    </select>
    <select id="span">
      <option value="6">6 h</option><option value="24">24 h</option>
      <option value="72">3 Tage</option><option value="168" selected>7 Tage</option>
      <option value="720">30 Tage</option><option value="2160">90 Tage</option>
      <option value="8760">1 Jahr</option>
    </select>
    <label class="follow"><input type="checkbox" id="follow" checked>
      <span>mitlaufen</span></label>
    <button id="jump" title="Zum aktuellen Zeitpunkt springen">jetzt</button>
  </div>
  <canvas id="chart" height="430"></canvas>
  <h2 style="margin-top:18px">Handelsprotokoll</h2>
  <div class="jrn" id="journal"></div>
</section>

<section>
  <h2>Offene Tranchen <span id="slots" style="float:right"></span></h2>
  <table id="lots"><thead><tr>
    <th>Zeit</th><th class="r">Kurs</th><th class="r">USD</th><th class="r">P/L</th>
  </tr></thead><tbody></tbody></table>
  <h2 style="margin-top:20px">Abgeschlossen</h2>
  <table id="closed"><thead><tr>
    <th></th><th>Zeit</th><th class="r">Kurs</th><th class="r">Ergebnis</th>
  </tr></thead><tbody></tbody></table>
</section>
</main>

<script>
const $ = s => document.querySelector(s);
let CFG = {}, FIELDS = [], MARKS = [], CHART = null;

const fmtUsd = v => (v==null?'—':(v<0?'-':'')+'$'+Math.abs(v).toLocaleString('de-DE',
  {minimumFractionDigits:2,maximumFractionDigits:2}));
const fmtPx = v => v==null?'—':v.toLocaleString('de-DE',{maximumFractionDigits:0});
const hhmm = ts => new Date(ts*1000).toLocaleTimeString('de-DE',
  {hour:'2-digit',minute:'2-digit'});
const stamp = (ts, span) => span > 36*3600
  ? new Date(ts*1000).toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'})
  : hhmm(ts);
const sign = (el,v) => {el.className = 'num ' + (v>0?'pos':v<0?'neg':'')};

// ---------- Formular ----------
function buildForm(){
  const box = $('#form'); box.innerHTML = '';
  for (const [group, items] of FIELDS){
    const g = document.createElement('div');
    g.className = 'grp'; g.textContent = group; box.appendChild(g);
    for (const [key,label,unit] of items){
      const l = document.createElement('label');
      l.innerHTML = `<span>${label}${unit?` <em>${unit}</em>`:''}</span>`;
      const i = document.createElement('input');
      i.type='number'; i.step='any'; i.value = CFG[key]; i.dataset.key = key;
      i.addEventListener('change', ()=>{ CFG[key] = parseFloat(i.value); pushCfg(); });
      l.appendChild(i); box.appendChild(l);
    }
  }
}
function buildLadder(){
  const box = $('#ladder'); box.innerHTML='';
  (CFG.ladder||[]).forEach((st,ix)=>{
    const l = document.createElement('label');
    l.innerHTML = `<span>ab <em>%</em></span>`;
    const a = document.createElement('input'); a.type='number'; a.step='any';
    a.value = st.gain_pct;
    const b = document.createElement('input'); b.type='number'; b.step='any';
    b.value = st.sell_pct;
    const sp = document.createElement('span'); sp.innerHTML = 'verkaufe <em>%</em>';
    sp.style.flex='0 0 auto';
    a.onchange = ()=>{ CFG.ladder[ix].gain_pct = parseFloat(a.value);
                       pushCfg(); buildLadder(); };
    b.onchange = ()=>{ CFG.ladder[ix].sell_pct = parseFloat(b.value);
                       pushCfg(); buildLadder(); };
    const del = document.createElement('button');
    del.textContent='×'; del.style.padding='2px 8px';
    del.onclick = ()=>{ CFG.ladder.splice(ix,1); pushCfg(); buildLadder(); };
    l.append(a, sp, b, del); box.appendChild(l);
  });
  const sold = (CFG.ladder||[]).reduce((s,x)=>s+(+x.sell_pct||0),0);
  const rest = 100 - sold;
  $('#runner').textContent = rest > 0
    ? `${rest.toFixed(0)} % laufen bis zur Trendumkehr weiter`
    : `Leiter verkauft ${sold.toFixed(0)} % — kein Rest für den Trendlauf`;
}
$('#ladder-add').onclick = ()=>{
  CFG.ladder.push({gain_pct:5, sell_pct:20}); pushCfg(); buildLadder(); };

async function pushCfg(){
  await fetch('/api/config',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(CFG)});
}

// ---------- Steuerung ----------
let RANGE = null;
function applyRange(){
  const m = $('#mode').value;
  $('#daterow').style.display = m==='live' ? 'none' : 'flex';
  if (!RANGE) return;
  if (m === 'live'){
    $('#range').textContent = 'Live: der Bot folgt den Daten, '
      + 'die der Recorder gerade schreibt.';
    return;
  }
  const r = m==='candles' ? RANGE.candles : RANGE.full;
  if (!r || !r.from_s){
    $('#range').textContent = m==='candles'
      ? 'Keine Kerzentabelle gefunden.'
      : 'Keine vollständigen Daten gefunden.';
    return;
  }
  $('#range').textContent = (m==='candles'
      ? `Kerzenquelle ${r.chosen||'?'} · `
      : 'Cluster, Buch und Wale · ')
    + `${r.from_s} bis ${r.to_s} UTC`;
  $('#t-from').value = r.from_s.slice(0,10);
  $('#t-to').value = 'now';
}
$('#mode').onchange = applyRange;
$('#run').onclick = async () => {
  $('#msg').textContent = '';
  const kind = $('#mode').value, live = kind === 'live';
  const r = await fetch(live ? '/api/live' : '/api/backtest', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({from:$('#t-from').value, to:$('#t-to').value, kind})});
  const j = await r.json();
  if (j.error) { $('#msg').textContent = j.error; return; }
  RUNMODE = kind === 'live' ? 'live' : kind;
  CHART = null;                      // Fenster neu aufbauen, Quelle wechselt
  $('#follow').checked = true;
};
$('#stop').onclick = () => fetch('/api/stop',{method:'POST'});
$('#agg').onchange = $('#span').onchange = loadChart;

// ---------- Chart ----------
let CURSOR = null, POS = {}, loading = false, RUNMODE = null;
const follow = () => $('#follow').checked;

async function loadChart(anchor){
  if (loading) return;
  loading = true;
  try {
    const hours = parseInt($('#span').value), agg = parseInt($('#agg').value);
    const to = anchor || CURSOR || Math.floor(Date.now()/1000);
    const src = (RUNMODE === 'candles' || $('#mode').value === 'candles')
      ? 'candles' : 'bars';
    const r = await fetch(
      `/api/chart?from=${to - hours*3600}&to=${to}&agg=${agg}&src=${src}`);
    CHART = await r.json();
    draw();
  } finally { loading = false; }
}

// Nachladen, sobald der Simulationszeitpunkt aus dem geladenen Fenster
// herauslaeuft oder sich merklich darin bewegt hat.
function followCursor(){
  if (!follow() || !CURSOR) return false;
  const hours = parseInt($('#span').value);
  const to = (CHART && CHART.to) || 0;
  if (CURSOR > to || CURSOR < to - hours*3600 ||
      (CURSOR - to) > -1 || (to - CURSOR) > hours*3600*0.08) {
    loadChart(CURSOR);
    return true;
  }
  return false;
}

$('#jump').onclick = () => { $('#follow').checked = true; loadChart(CURSOR); };
function draw(){
  const cv = $('#chart'), ctx = cv.getContext('2d');
  const w = cv.width = cv.clientWidth * devicePixelRatio;
  const h = cv.height = 430 * devicePixelRatio;
  ctx.scale(1,1); ctx.clearRect(0,0,w,h);
  if(!CHART || !CHART.candles || !CHART.candles.length){
    ctx.fillStyle='#7e8c99'; ctx.font=`${13*devicePixelRatio}px sans-serif`;
    ctx.fillText('Keine Kursdaten in diesem Zeitraum.', 20*devicePixelRatio, 40);
    if (CHART && CHART.from) {
      const f = new Date(CHART.from*1000).toLocaleString('de-DE');
      const t = new Date(CHART.to*1000).toLocaleString('de-DE');
      ctx.fillText(`${f} – ${t}  (Quelle: ${CHART.source||'?'})`,
                   20*devicePixelRatio, 66*devicePixelRatio);
      $('#chart-note').textContent = `${f} – ${t} · keine Kerzen in dieser Quelle`;
    }
    return; }

  const cs = CHART.candles, padR = 108*devicePixelRatio, padB = 26*devicePixelRatio;
  const W = w - padR, H = h - padB;
  let lo = Math.min(...cs.map(c=>c.l)), hi = Math.max(...cs.map(c=>c.h));
  (CHART.clusters||[]).forEach(c=>{ if(c.bucket_px>lo*0.97 && c.bucket_px<hi*1.03){
    lo=Math.min(lo,c.bucket_px); hi=Math.max(hi,c.bucket_px);} });
  const pad = (hi-lo)*0.06; lo-=pad; hi+=pad;
  const t0 = cs[0].ts, t1 = cs[cs.length-1].ts || t0+1;
  const X = t => (t-t0)/(t1-t0||1)*W;
  const Y = p => H - (p-lo)/(hi-lo)*H;

  // Cluster als Dichtebalken am rechten Rand — das Kernbild dieses Systems
  const maxN = Math.max(1,...(CHART.clusters||[]).map(c=>c.notional));
  (CHART.clusters||[]).forEach(c=>{
    const y = Y(c.bucket_px); if(y<0||y>H) return;
    const bw = (c.notional/maxN) * (padR-14*devicePixelRatio);
    ctx.fillStyle = c.side==='short' ? 'rgba(224,163,62,.5)' : 'rgba(140,111,209,.5)';
    ctx.fillRect(W+6*devicePixelRatio, y-2*devicePixelRatio, bw, 4*devicePixelRatio);
  });

  // Kerzen
  const cw = Math.max(1.5*devicePixelRatio, W/cs.length*0.62);
  cs.forEach(c=>{
    const x = X(c.ts), up = c.c>=c.o;
    ctx.strokeStyle = ctx.fillStyle = up ? '#3e9e8c' : '#c0604e';
    ctx.lineWidth = Math.max(1, devicePixelRatio);
    ctx.beginPath(); ctx.moveTo(x, Y(c.h)); ctx.lineTo(x, Y(c.l)); ctx.stroke();
    const yo=Y(c.o), yc=Y(c.c);
    ctx.fillRect(x-cw/2, Math.min(yo,yc), cw, Math.max(1.5,Math.abs(yc-yo)));
  });

  // Preisachse
  ctx.font = `${11*devicePixelRatio}px ui-monospace,monospace`;
  ctx.fillStyle = '#7e8c99'; ctx.textAlign='left';
  for(let i=0;i<=4;i++){
    const p = lo + (hi-lo)*i/4, y = Y(p);
    ctx.strokeStyle='#232c34'; ctx.beginPath();
    ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke();
    ctx.fillText(fmtPx(p), W+6*devicePixelRatio, y-4*devicePixelRatio);
  }
  // Zeitachse
  ctx.textAlign='center';
  for(let i=0;i<=5;i++){
    const t = t0 + (t1-t0)*i/5;
    ctx.fillText(stamp(t, t1-t0), X(t), H+17*devicePixelRatio);
  }

  const dpr = devicePixelRatio;
  // Einstand und laufender Kurs als Orientierung im Bild
  const line = (p, color, label) => {
    if (!p) return;
    const y = Y(p); if (y < 0 || y > H) return;
    ctx.save();
    ctx.setLineDash([5*dpr, 5*dpr]);
    ctx.strokeStyle = color; ctx.lineWidth = Math.max(1, dpr);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.restore();
    ctx.font = `${11*dpr}px ui-monospace,monospace`;
    ctx.textAlign = 'left'; ctx.fillStyle = color;
    ctx.fillText(`${label} ${fmtPx(p)}`, 5*dpr, y - 5*dpr);
  };
  if (POS.size > 0) line(POS.avg, '#7e8c99', 'Einstand');
  line(POS.px, '#d6dee6', 'Kurs');

  // Zeitcursor: wo die Simulation gerade steht
  if (CURSOR && CURSOR >= t0 && CURSOR <= t1) {
    const x = X(CURSOR);
    ctx.strokeStyle = 'rgba(224,163,62,.6)'; ctx.lineWidth = Math.max(1, dpr);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillStyle = '#e0a33e'; ctx.textAlign = 'right';
    ctx.font = `${11*dpr}px ui-monospace,monospace`;
    ctx.fillText(hhmm(CURSOR), x - 5*dpr, 13*dpr);
  }

  // Kauf- und Verkaufsmarken
  MARKS.filter(m=>m.ts>=t0-300 && m.ts<=t1+300).forEach(m=>{
    const x = X(m.ts), y = Y(m.px), buy = m.side==='B';
    const r = 8*devicePixelRatio;
    ctx.fillStyle = buy ? '#4fb89c' : '#d96c8a';
    ctx.beginPath();
    ctx.arc(x, y + (buy? r*1.8 : -r*1.8), r, 0, 7); ctx.fill();
    ctx.fillStyle = '#101820'; ctx.textAlign='center';
    ctx.font = `700 ${10*devicePixelRatio}px sans-serif`;
    ctx.fillText(m.side, x, y + (buy? r*1.8+3.5*devicePixelRatio
                                    : -r*1.8+3.5*devicePixelRatio));
  });

  const d0 = new Date(t0*1000).toLocaleString('de-DE',
    {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const d1 = new Date(t1*1000).toLocaleString('de-DE',
    {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const cl = CHART.cluster_age_s!=null
    ? ` · Cluster-Snapshot ${Math.round(CHART.cluster_age_s/60)} min alt, `
      + `orange = Short darüber, violett = Long darunter` : '';
  $('#chart-note').textContent = `${d0} – ${d1} · ${CHART.source||''}`
    + (follow() ? ' · läuft mit' : ' · angehalten') + cl;
}

// ---------- Zustand ----------
async function tick(){
  const s = await (await fetch('/api/state')).json();
  if (s.empty){ $('#msg').textContent =
    'Noch kein Durchgang gestartet.'; return; }
  $('#prog').style.width = ((s.progress||0)*100)+'%';
  if (s.error) $('#msg').textContent = s.error;
  else if (s.result && s.result.error) $('#msg').textContent = s.result.error;
  else if (s.result && s.result.clipped) $('#msg').textContent =
    'Zeitraum wurde auf die tatsächlich vorhandenen Daten beschnitten.';

  const q = s.summary || {};
  $('#s-equity').textContent = fmtUsd(q.equity);
  $('#s-deployed').textContent = q.deployed==null ? '—'
    : `${fmtUsd(q.deployed)} · ${(q.deployed_pct||0).toFixed(0)}%`;
  $('#s-cash').textContent = fmtUsd(q.cash);
  $('#s-real').textContent = fmtUsd(q.realized); sign($('#s-real'), q.realized);
  $('#s-unreal').textContent = fmtUsd(q.unrealized); sign($('#s-unreal'), q.unrealized);
  $('#s-trades').textContent = `${q.n_buys||0} / ${q.n_sells||0}`;
  $('#s-fees').textContent = q.fees==null ? '—'
    : `${fmtUsd(q.fees)} · ${(q.fees_pct_budget||0).toFixed(1)}%`;
  $('#s-fees').className = 'num ' + ((q.fees_pct_budget||0) > 10 ? 'neg' : '');

  MARKS = s.marks || [];
  if (s.mode && s.mode !== 'idle') RUNMODE = s.mode;
  CURSOR = s.cursor || (s.equity && s.equity.length
    ? s.equity[s.equity.length-1].ts : null);
  POS = {avg: q.avg_px, px: q.last_px, size: q.open_size};

  const lb = $('#lots tbody'); lb.innerHTML='';
  $('#slots').textContent = `${(s.lots||[]).length} / ${q.max_lots||'—'}`
    + (q.next_order ? ` · nächste ${fmtUsd(q.next_order)}` : ' · kein Kapital frei');
  (s.lots||[]).forEach(l=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${l.t.slice(11,16)}</td><td class="r">${fmtPx(l.px)}</td>
      <td class="r">${l.usd.toFixed(0)}</td>
      <td class="r ${l.pnl>0?'pos':l.pnl<0?'neg':''}">${l.pnl_pct.toFixed(2)}%</td>`;
    lb.appendChild(tr);
  });

  const cb = $('#closed tbody'); cb.innerHTML='';
  MARKS.slice().reverse().filter(m=>m.side==='S').slice(0,25).forEach(m=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><span class="tag s">S</span></td>
      <td>${hhmm(m.ts)}</td><td class="r">${fmtPx(m.px)}</td>
      <td class="r ${m.pnl>0?'pos':'neg'}">${fmtUsd(m.pnl)}</td>`;
    cb.appendChild(tr);
  });

  const j = $('#journal'); j.innerHTML='';
  (s.journal||[]).forEach(e=>{
    const d = document.createElement('div');
    const tag = e.kind==='buy' ? '<span class="tag b">B</span>'
              : e.kind==='sell' ? '<span class="tag s">S</span>' : '';
    d.innerHTML = `<time>${e.t.slice(5,16)}</time>${tag}<span>${e.text}</span>`;
    j.appendChild(d);
  });
  if (!followCursor()) draw();
}

(async function init(){
  const c = await (await fetch('/api/config')).json();
  CFG = c.config; FIELDS = c.fields; buildForm(); buildLadder();
  RANGE = await (await fetch('/api/range')).json();
  applyRange();
  await loadChart();
  setInterval(tick, 1500); tick();
})();
</script></body></html>
"""


def main() -> None:
    global DB_PATH, RESULTS_DB
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="hl_liq.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--results", default="hl_sim_results.db",
                   help="Datei fuer Trades und Protokoll ('' schaltet ab)")
    a = p.parse_args()
    DB_PATH = a.db
    RESULTS_DB = a.results
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Handelssimulation auf http://{a.host}:{a.port}  (DB: {DB_PATH})")
    print("Simulation only — es werden keine echten Orders gesendet.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
