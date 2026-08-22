#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_seedgen.py  —  Hochsicherer BIP39-Seed-Generator mit Hardware-Entropie
===========================================================================
Zielplattform : Raspberry Pi 5, Raspberry Pi OS "Trixie" (64-bit)
Hardware      : 1) BCM2712 Hardware-RNG            (/dev/hwrng, einmalig)
                2) RadiationD-v1.1 (CAJOE) Geiger  (GPIO-Pulse, Zerfallstiming)
                3) MLX90640 IR-Waermebildsensor    (I2C 0x33, Sensorrauschen)
                4) Praezisionswuerfel              (manuelle Eingabe, LETZTE Quelle)

Krypto-Schema (Kurzfassung, Details siehe README-Abschnitt unten):
    H_hw   = SHA-512( DOM|"hwrng"  | rohdaten_hwrng )
    H_dec  = SHA-512( DOM|"decay"  | alle_zerfalls_timestamps_ns )
    H_cam  = SHA-512( DOM|"cam"    | alle_rohframes )
    H_os   = SHA-512( DOM|"os"     | os.urandom(64) )        # Defense-in-Depth
    POOL   = SHA-512( DOM|"pool"   | H_hw | H_dec | H_cam | H_os )
    H_dice = SHA-512( DOM|"dice"   | wuerfel_als_bytes )
    FINAL  = HMAC-SHA512( key = H_dice , msg = POOL )          # Robuster 2-Quellen-Kombinierer
    ENTROPY= FINAL[:16] (12 Woerter) bzw. FINAL[:32] (24 Woerter)
    SEED   = BIP39(ENTROPY)

Eigenschaften des Kombinierers:
  * Ist AUCH NUR EINE der beiden Seiten (Wuerfel ODER Hardware-Pool)
    unvorhersagbar, ist das Ergebnis unvorhersagbar.
  * Die Wuerfel gehen als HMAC-*Schluessel* ein => hardwareunabhaengige,
    letzte Entropiequelle, wie gefordert.

Sicherheit / Fail-Safe:
  * Jede Quelle hat Gesundheitspruefungen. Faellt eine Quelle aus oder
    liefert offensichtlich defekte Daten => sofortiger ABBRUCH mit Meldung.
  * BIP39-Wortliste wird per SHA-256 gegen den offiziellen Hash verifiziert.
  * Selbsttest gegen offizielle BIP39-Testvektoren via --selftest.

Aufruf:
  sudo python3 btc_seedgen.py                  # interaktiv, echte Hardware
  python3 btc_seedgen.py --selftest            # Krypto-Selbsttests
  python3 btc_seedgen.py --mock --yes ...      # NUR ZUM TESTEN, simulierte HW
"""

import argparse
import hashlib
import hmac
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------
# Konstanten
# ----------------------------------------------------------------------------
VERSION            = "1.0"
DOMAIN             = b"BTC-SEEDGEN-v1"          # Domain-Separation-Tag
GPIO_GEIGER        = 17                          # BCM-Nummer (phys. Pin 11)
MLX_I2C_BUS        = 1
MLX_I2C_ADDR       = 0x33
WORDLIST_SHA256    = ("2f5eed53a4727b4bf8880d8f3f199efc"
                      "90e58503646d9ff8eff3a2ed3b24dbda")   # offizielle english.txt
DECAY_DEBOUNCE_NS  = 30_000_000   # 30 ms Totzeit: Audio-Burst = 1 Zerfall
DECAY_TIMEOUT_S    = 360      # Abbruch, wenn so lange kein Zerfall registriert wird
HWRNG_BYTES        = 64       # 512 Bit, einmalige Entnahme
CAM_ENTROPY_PER_FRAME = 64    # konservativ kreditierte Bits pro Frame
CAM_MIN_DIFF          = 0.85  # Frame nur akzeptieren, wenn >=85% Pixel anders
CAM_ACCEPT_TIMEOUT_S  = 300   # 5 min Zeit fuer den User, per Handbewegung
                              # einen akzeptablen Frame zu erzeugen
CAM_FROZEN_DIFF       = 0.05  # darunter gilt ein Frame als 'eingefroren'
CAM_FROZEN_LIMIT      = 200   # so viele eingefrorene Frames in Folge = Defekt
DECAY_BITS_PER_EVENT  = 2     # konservativ kreditierte Bits pro Zerfallsereignis
DICE_BITS_PER_ROLL    = math.log2(6)   # ~2.585 Bit pro Wurf
DICE_MIN_ROLLS        = 32    # hartes Minimum (~83 Bit); 100 empfohlen

# ----------------------------------------------------------------------------
# Konsolen-Hilfsfunktionen (ANSI)
# ----------------------------------------------------------------------------
class C:
    RESET  = "\033[0m";  BOLD = "\033[1m";  DIM = "\033[2m"
    RED    = "\033[31m"; GRN  = "\033[32m"; YEL = "\033[33m"
    CYA    = "\033[36m"; MAG  = "\033[35m"; WHT = "\033[97m"

def term_width() -> int:
    try:
        return max(60, min(100, shutil.get_terminal_size().columns))
    except Exception:
        return 78

def hr(ch="─"):
    print(C.DIM + ch * term_width() + C.RESET)

def banner():
    w = term_width()
    line = "═" * (w - 2)
    print(C.CYA + "╔" + line + "╗" + C.RESET)
    t = f"BTC SEED GENERATOR v{VERSION}  ·  Hardware-Entropie  ·  BIP39"
    print(C.CYA + "║" + C.BOLD + t.center(w - 2) + C.RESET + C.CYA + "║" + C.RESET)
    print(C.CYA + "╚" + line + "╝" + C.RESET)

def status_line(idx, total, name, state, detail=""):
    sym = {"ok": C.GRN + "✔", "run": C.YEL + "▶", "wait": C.DIM + "·",
           "fail": C.RED + "✘"}[state]
    print(f" [{idx}/{total}] {sym}{C.RESET} {C.BOLD}{name:<28}{C.RESET} {detail}")

def progress(label, cur, total, extra=""):
    w = 28
    frac = 0 if total == 0 else min(1.0, cur / total)
    filled = int(frac * w)
    bar = "▓" * filled + "░" * (w - filled)
    sys.stdout.write(f"\r   {label:<12} {C.CYA}{bar}{C.RESET} "
                     f"{cur}/{total} ({frac*100:5.1f}%) {extra}   ")
    sys.stdout.flush()

def fail_abort(source, reason):
    print("\n")
    hr("━")
    print(C.RED + C.BOLD + f" ✘ ABBRUCH — Entropiequelle ausgefallen: {source}" + C.RESET)
    print(C.RED + f"   Grund: {reason}" + C.RESET)
    print(C.RED + "   Es wurde KEIN Seed erzeugt. Bitte Hardware pruefen und neu starten." + C.RESET)
    hr("━")
    sys.exit(2)

# ----------------------------------------------------------------------------
# Domain-separiertes Hashing
# ----------------------------------------------------------------------------
def dsha512(tag: str, data: bytes) -> bytes:
    h = hashlib.sha512()
    h.update(DOMAIN + b"|" + tag.encode() + b"|")
    h.update(len(data).to_bytes(8, "big"))
    h.update(data)
    return h.digest()

# ----------------------------------------------------------------------------
# Entropiequellen
# ----------------------------------------------------------------------------
@dataclass
class SourceResult:
    name: str
    digest: bytes
    raw_len: int
    credited_bits: float
    info: str = ""

# ---------- 1) BCM2712 Hardware-RNG -----------------------------------------
def collect_hwrng(mock: bool, transparent: bool = False) -> SourceResult:
    if mock:
        raw = os.urandom(HWRNG_BYTES)
    else:
        path = "/dev/hwrng"
        if not os.path.exists(path):
            fail_abort("BCM2712 HWRNG", f"{path} existiert nicht (Kernel/Treiber pruefen).")
        try:
            with open(path, "rb") as f:
                raw = f.read(HWRNG_BYTES)
        except PermissionError:
            fail_abort("BCM2712 HWRNG", "Keine Leserechte auf /dev/hwrng — Skript mit sudo starten.")
        except OSError as e:
            fail_abort("BCM2712 HWRNG", f"Lesefehler: {e}")
    if len(raw) != HWRNG_BYTES:
        fail_abort("BCM2712 HWRNG", f"Nur {len(raw)} von {HWRNG_BYTES} Bytes gelesen.")
    # Gesundheitspruefung: grobe Diversitaet & Monobit
    uniq = len(set(raw))
    ones = sum(bin(b).count("1") for b in raw)
    total_bits = HWRNG_BYTES * 8
    if uniq < 32:
        fail_abort("BCM2712 HWRNG", f"Verdaechtige Ausgabe: nur {uniq} verschiedene Bytewerte.")
    if not (0.30 * total_bits < ones < 0.70 * total_bits):
        fail_abort("BCM2712 HWRNG", f"Monobit-Test fehlgeschlagen ({ones}/{total_bits} Einsen).")
    res = SourceResult("BCM2712 HWRNG", dsha512("hwrng", raw), len(raw),
                       credited_bits=total_bits / 2,   # konservativ 50% kreditiert
                       info=f"{HWRNG_BYTES} B gelesen, {uniq} uniq, Monobit ok")
    if transparent:
        print(C.DIM + "   Rohdaten (512 Bit, einmalige Entnahme):" + C.RESET)
        for i in range(0, HWRNG_BYTES, 16):
            print(f"     {raw[i:i+16].hex()}")
        print(f"   H_hw = {res.digest.hex()}")
    return res

# ---------- 2) Radioaktiver Zerfall (CAJOE / GPIO) ---------------------------
def find_rp1_chip(lgpio, gpio_nr):
    """Findet den 40-Pin-Header des Pi 5 (RP1, 54 Leitungen) dynamisch.
    Die Chipnummer variiert je nach Kernel (0, 4, 15, ...)."""
    kandidaten = []
    for n in range(0, 32):
        try:
            h = lgpio.gpiochip_open(n)
        except Exception:
            continue
        label = ""
        try:
            info = lgpio.gpio_get_chip_info(h)
            label = str(info).lower()
        except Exception:
            pass
        if "rp1" in label or "54" in label.replace(",", " ").split():
            kandidaten.insert(0, (n, h))      # rp1 bevorzugt an den Anfang
        else:
            kandidaten.append((n, h))
    handle = None
    edge_used = None
    for n, h in kandidaten:
        if handle is None:
            try:
                # Ruhepegel messen -> Flankenrichtung automatisch bestimmen
                lgpio.gpio_claim_input(h, gpio_nr, lgpio.SET_PULL_NONE)
                ruhe = lgpio.gpio_read(h, gpio_nr)
                lgpio.gpio_free(h, gpio_nr)
                edge = lgpio.RISING_EDGE if ruhe == 0 else lgpio.FALLING_EDGE
                lgpio.gpio_claim_alert(h, gpio_nr, edge, lgpio.SET_PULL_NONE)
                handle, edge_used = h, edge
                continue
            except Exception:
                pass
        try: lgpio.gpiochip_close(h)
        except Exception: pass
    return (handle, edge_used) if handle is not None else (None, None)


def collect_decay(n_events: int, mock: bool, transparent: bool = False) -> SourceResult:
    timestamps = []
    t_start = time.monotonic()

    if mock:
        # Simulierter Poisson-Prozess ~30 CPM (NUR ZUM TESTEN)
        import random
        t = time.monotonic_ns()
        for i in range(n_events):
            t += int(random.expovariate(0.5) * 1e6)  # gerafft fuer Testlauf
            timestamps.append(t)
            if transparent:
                d = (t - timestamps[-2]) / 1e9 if i else 0.0
                print(f"   Ereignis {i+1:4d}/{n_events}: t = {t} ns   Δ = {d:8.3f} s [MOCK]")
            elif i % max(1, n_events // 50) == 0 or i == n_events - 1:
                cpm = (i + 1) / max(1e-9, (time.monotonic() - t_start)) * 60
                progress("Zerfall", i + 1, n_events, f"CPM≈{cpm:7.1f} [MOCK]")
            time.sleep(0.002)
    else:
        try:
            import lgpio
        except ImportError:
            fail_abort("Radiozerfall (CAJOE)",
                       "Python-Modul 'lgpio' fehlt: sudo apt install python3-lgpio")
        handle, edge = find_rp1_chip(lgpio, GPIO_GEIGER)
        if handle is None:
            fail_abort("Radiozerfall (CAJOE)",
                       f"GPIO{GPIO_GEIGER} konnte auf keinem gpiochip belegt werden "
                       "(gpiodetect ausfuehren: Chip mit Label 'pinctrl-rp1' fehlt?).")

        last_event = time.monotonic()
        def _cb(chip, gpio, level, ts_ns):
            nonlocal last_event
            if timestamps and ts_ns - timestamps[-1] < DECAY_DEBOUNCE_NS:
                return                       # Flanke gehoert zum selben Klick-Burst
            timestamps.append(ts_ns)
            last_event = time.monotonic()

        cb = lgpio.callback(handle, GPIO_GEIGER, edge, _cb)
        try:
            shown = -1
            shown_evt = 0
            while len(timestamps) < n_events:
                time.sleep(0.2)
                n = len(timestamps)
                if n != shown:
                    el = time.monotonic() - t_start
                    cpm = n / max(1e-9, el) * 60
                    eta = (n_events - n) / max(1e-9, cpm) * 60
                    if transparent:
                        while shown_evt < n:
                            ts = timestamps[shown_evt]
                            d = (ts - timestamps[shown_evt-1]) / 1e9 if shown_evt else 0.0
                            print(f"   Ereignis {shown_evt+1:4d}/{n_events}: t = {ts} ns   "
                                  f"Δ = {d:8.3f} s   (CPM={cpm:5.1f}, ETA {eta/60:5.1f} min)")
                            shown_evt += 1
                    else:
                        progress("Zerfall", n, n_events,
                                 f"CPM={cpm:5.1f}  ETA {eta/60:5.1f} min")
                    shown = n
                if time.monotonic() - last_event > DECAY_TIMEOUT_S:
                    fail_abort("Radiozerfall (CAJOE)",
                               f"{DECAY_TIMEOUT_S}s ohne Zerfallsereignis — "
                               "Verkabelung/Board/Roehrenspannung pruefen.")
        except KeyboardInterrupt:
            fail_abort("Radiozerfall (CAJOE)", "Vom Benutzer abgebrochen (Strg+C).")
        finally:
            try:
                cb.cancel(); lgpio.gpiochip_close(handle)
            except Exception:
                pass

    print()
    if len(timestamps) < n_events:
        fail_abort("Radiozerfall (CAJOE)", "Zu wenige Ereignisse erfasst.")
    # Gesundheitspruefung: Intervalle muessen variieren (kein festgeklemmter Pin)
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]
    if len(set(deltas)) < max(4, len(deltas) // 10):
        fail_abort("Radiozerfall (CAJOE)",
                   "Pulsintervalle nahezu konstant — Signal sieht nicht nach Zerfall aus "
                   "(Stoerquelle/Oszillator am Pin?).")
    raw = b"".join(t.to_bytes(8, "big", signed=False) for t in timestamps)
    dur = time.monotonic() - t_start
    res = SourceResult("Radiozerfall (CAJOE)", dsha512("decay", raw), len(raw),
                       credited_bits=n_events * DECAY_BITS_PER_EVENT,
                       info=f"{n_events} Ereignisse in {dur/60:.1f} min")
    if transparent:
        print(f"   H_dec = {res.digest.hex()}")
    return res

# ---------- 3) MLX90640 IR-Kamera (I2C, Roh-Frames) --------------------------
def _mlx_read_words(bus, reg: int, n_words: int) -> bytes:
    """16-Bit-Registeradresse schreiben, dann n_words*2 Bytes lesen."""
    from smbus2 import i2c_msg
    out = bytearray()
    CHUNK = 24  # Worte pro Transaktion (konservativ)
    off = 0
    while off < n_words:
        n = min(CHUNK, n_words - off)
        w = i2c_msg.write(MLX_I2C_ADDR, [(reg + off) >> 8, (reg + off) & 0xFF])
        r = i2c_msg.read(MLX_I2C_ADDR, n * 2)
        bus.i2c_rdwr(w, r)
        out += bytes(r)
        off += n
    return bytes(out)

def _hand_hinweis():
    print(C.YEL + C.BOLD + "   >>> Bitte die Hand in ca. 60 cm Abstand ZUFAELLIG "
          "vor der Kamera bewegen! <<<" + C.RESET)

def frame_diff_ratio(a: bytes, b: bytes) -> float:
    """Anteil der 768 Bildpixel (16-Bit-Worte), die sich unterscheiden."""
    n = min(768, min(len(a), len(b)) // 2)
    diff = sum(1 for i in range(n) if a[2*i:2*i+2] != b[2*i:2*i+2])
    return diff / max(1, n)

def collect_camera(n_frames: int, mock: bool, transparent: bool = False) -> SourceResult:
    frames = []
    rejected = 0
    _hand_hinweis()
    print(C.DIM + f"   (Frames werden nur akzeptiert, wenn sich >= {CAM_MIN_DIFF*100:.0f}% "
          "der Pixel zum letzten AKZEPTIERTEN Frame unterscheiden)" + C.RESET)
    if mock:
        for i in range(n_frames):
            f = os.urandom(832 * 2)
            d = frame_diff_ratio(f, frames[-1]) if frames else 1.0
            frames.append(f)
            if transparent:
                h = hashlib.sha256(f).hexdigest()
                print(C.GRN + "   ✔ AKZEPTIERT" + C.RESET +
                      f" Frame {i+1:3d}/{n_frames}: SHA256 {h[:16]}…  Diff {d*100:5.1f}%  "
                      f"Rohworte {f[:6].hex()} [MOCK]")
            else:
                progress("IR-Kamera", i + 1, n_frames, "[MOCK]")
            if (i + 1) % 5 == 0 and i + 1 < n_frames:
                if not transparent:
                    print()
                _hand_hinweis()
            time.sleep(0.01)
    else:
        try:
            from smbus2 import SMBus
        except ImportError:
            fail_abort("MLX90640", "Python-Modul 'smbus2' fehlt: "
                       "sudo apt install python3-smbus2  (oder pip install smbus2)")
        try:
            bus = SMBus(MLX_I2C_BUS)
        except OSError as e:
            fail_abort("MLX90640", f"I2C-Bus {MLX_I2C_BUS} nicht verfuegbar: {e} "
                       "(I2C in raspi-config aktivieren).")
        try:
            consec_reject = 0
            frozen_count = 0
            last_accept = time.monotonic()
            while len(frames) < n_frames:
                # Auf neues Frame warten (Statusregister 0x8000, Bit 3)
                t0 = time.monotonic()
                while True:
                    st = _mlx_read_words(bus, 0x8000, 1)
                    if st[1] & 0x08:
                        break
                    if time.monotonic() - t0 > 5:
                        fail_abort("MLX90640", "Timeout beim Warten auf neues Frame — "
                                   "Sensor antwortet nicht (Adresse 0x33?).")
                    time.sleep(0.02)
                frame = _mlx_read_words(bus, 0x0400, 832)   # kompletter RAM-Block
                # Statusbit "new data" loeschen
                from smbus2 import i2c_msg
                wmsg = i2c_msg.write(MLX_I2C_ADDR, [0x80, 0x00, 0x00, 0x30])
                bus.i2c_rdwr(wmsg)
                # 51%-Filter: nur deutlich veraenderte Frames akzeptieren
                d = frame_diff_ratio(frame, frames[-1]) if frames else 1.0
                if frames and d < CAM_MIN_DIFF:
                    rejected += 1
                    consec_reject += 1
                    # Healthcheck: wirklich EINGEFRORENES Bild (Sensordefekt) ...
                    if d < CAM_FROZEN_DIFF:
                        frozen_count += 1
                        if frozen_count >= CAM_FROZEN_LIMIT:
                            fail_abort("MLX90640",
                                       f"{CAM_FROZEN_LIMIT} eingefrorene Frames in Folge "
                                       f"(Diff < {CAM_FROZEN_DIFF*100:.0f}%) — Sensor defekt?")
                    else:
                        frozen_count = 0
                    # ... aber dem Menschen 5 Minuten Zeit fuer die Handbewegung geben
                    warte = time.monotonic() - last_accept
                    if warte > CAM_ACCEPT_TIMEOUT_S:
                        fail_abort("MLX90640",
                                   f"{CAM_ACCEPT_TIMEOUT_S/60:.0f} Minuten ohne akzeptablen Frame "
                                   f"(>= {CAM_MIN_DIFF*100:.0f}% Diff) — Kamera frei? Hand bewegt?")
                    if transparent:
                        print(C.RED + f"   ✘ VERWORFEN (nicht verwendet): Diff nur "
                              f"{d*100:5.1f}% < {CAM_MIN_DIFF*100:.0f}%" + C.RESET +
                              C.DIM + f"   [noch {max(0, CAM_ACCEPT_TIMEOUT_S-warte):3.0f} s Zeit]"
                              + C.RESET)
                    if consec_reject % 3 == 0:
                        if not transparent:
                            print()
                        _hand_hinweis()
                    continue
                consec_reject = 0
                frozen_count = 0
                last_accept = time.monotonic()
                frames.append(frame)
                if transparent:
                    h = hashlib.sha256(frame).hexdigest()
                    print(C.GRN + f"   ✔ AKZEPTIERT" + C.RESET +
                          f" Frame {len(frames):3d}/{n_frames}: SHA256 {h[:16]}…  "
                          f"Diff {d*100:5.1f}%  Rohworte {frame[:6].hex()}")
                else:
                    progress("IR-Kamera", len(frames), n_frames,
                             f"Diff {d*100:4.0f}%  verworfen: {rejected}")
                if len(frames) % 5 == 0 and len(frames) < n_frames:
                    if not transparent:
                        print()
                    _hand_hinweis()
        except OSError as e:
            fail_abort("MLX90640", f"I2C-Fehler: {e}")
        finally:
            try: bus.close()
            except Exception: pass
    print()
    # Gesundheitspruefung: Frames duerfen weder leer/konstant noch identisch sein
    if any(len(set(f)) < 8 for f in frames):
        fail_abort("MLX90640", "Frame mit nahezu konstantem Inhalt — Sensor defekt?")
    if n_frames >= 2 and len({hashlib.sha256(f).digest() for f in frames}) < n_frames:
        fail_abort("MLX90640", "Identische Frames erkannt — kein Sensorrauschen vorhanden.")
    raw = b"".join(frames)
    res = SourceResult("MLX90640 IR-Kamera", dsha512("cam", raw), len(raw),
                       credited_bits=n_frames * CAM_ENTROPY_PER_FRAME,
                       info=f"{n_frames} Frames akzeptiert, {rejected} verworfen ({len(raw)} B)")
    if transparent:
        print(f"   H_cam = {res.digest.hex()}")
    return res

# ---------- 4) Wuerfel (manuell, LETZTE Quelle) ------------------------------
def dice_to_bytes(rolls) -> bytes:
    val = 0
    for r in rolls:
        val = val * 6 + (r - 1)
    n = max(1, (val.bit_length() + 7) // 8)
    return len(rolls).to_bytes(4, "big") + val.to_bytes(n, "big")

def collect_dice(n_rolls: int, target_bits: int, mock: bool, transparent: bool = False) -> SourceResult:
    rolls = []
    if mock:
        import random
        rolls = [random.randint(1, 6) for _ in range(n_rolls)]
        print(f"   Wuerfel: {n_rolls} simulierte Wuerfe [MOCK]")
    else:
        print(C.BOLD + f"\n   Bitte {n_rolls} Wuerfelwuerfe eingeben (Ziffern 1-6)." + C.RESET)
        print(C.DIM + "   Eingabe einzeln oder als Block (z.B. 415263...). "
              "'u' = letzten Wurf loeschen." + C.RESET)
        while len(rolls) < n_rolls:
            progress("Wuerfel", len(rolls), n_rolls)
            try:
                s = input("\n   > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                fail_abort("Wuerfel", "Eingabe abgebrochen.")
            if s == "u":
                if rolls: rolls.pop()
                continue
            for ch in s:
                if ch in "123456":
                    if len(rolls) < n_rolls:
                        rolls.append(int(ch))
                elif ch in " ,;":
                    continue
                else:
                    print(C.YEL + f"   Ignoriere ungueltiges Zeichen: '{ch}'" + C.RESET)
        progress("Wuerfel", len(rolls), n_rolls); print()
    # Gesundheitspruefung
    counts = {v: rolls.count(v) for v in range(1, 7)}
    if len(set(rolls)) == 1:
        fail_abort("Wuerfel", "Alle Wuerfe identisch — das ist keine Zufallsquelle.")
    worst = max(counts.values()) / len(rolls)
    if n_rolls >= 60 and worst > 0.40:
        fail_abort("Wuerfel", f"Eine Augenzahl macht {worst*100:.0f}% aller Wuerfe aus — "
                   "Wuerfel/Eingabe pruefen.")
    bits = n_rolls * DICE_BITS_PER_ROLL
    if bits < target_bits:
        print(C.YEL + f"   ⚠ Hinweis: Wuerfel liefern nur ~{bits:.0f} Bit "
              f"(< {target_bits} Bit Ziel). Fuer volle Hardware-Unabhaengigkeit "
              f"mind. {math.ceil(target_bits / DICE_BITS_PER_ROLL)} Wuerfe verwenden." + C.RESET)
    raw = dice_to_bytes(rolls)
    res = SourceResult("Wuerfel (manuell)", dsha512("dice", raw), len(raw),
                       credited_bits=bits,
                       info=f"{n_rolls} Wuerfe, Verteilung {counts}")
    if transparent:
        print(f"   Wuerfelfolge : {''.join(str(r) for r in rolls)}")
        print(f"   Kodiert (hex): {raw.hex()}")
        print(f"   H_dice = {res.digest.hex()}")
    return res

# ----------------------------------------------------------------------------
# Kombinierer + BIP39
# ----------------------------------------------------------------------------
def combine(hw: SourceResult, dec: SourceResult, cam: SourceResult,
            dice: SourceResult, n_bytes: int, transparent: bool = False) -> bytes:
    h_os = dsha512("os", os.urandom(64))                    # Defense-in-Depth
    pool = dsha512("pool", hw.digest + dec.digest + cam.digest + h_os)
    final = hmac.new(dice.digest, pool, hashlib.sha512).digest()
    if transparent:
        print(C.DIM + "   — Transparenz: alle Zwischenwerte des Kombinierers —" + C.RESET)
        print(f"   H_hw   = {hw.digest.hex()}")
        print(f"   H_dec  = {dec.digest.hex()}")
        print(f"   H_cam  = {cam.digest.hex()}")
        print(f"   H_os   = {h_os.hex()}")
        print(f"   POOL   = {pool.hex()}")
        print(f"   H_dice = {dice.digest.hex()}  (HMAC-Schluessel)")
        print(f"   FINAL  = HMAC-SHA512(H_dice, POOL) = {final.hex()}")
        print(f"   ENTROPY= FINAL[:{n_bytes}] = {final[:n_bytes].hex()}")
    return final[:n_bytes]

def bip39_indices(entropy: bytes):
    ent_bits = len(entropy) * 8
    cs_bits  = ent_bits // 32
    cs = hashlib.sha256(entropy).digest()[0] >> (8 - cs_bits)
    num = (int.from_bytes(entropy, "big") << cs_bits) | cs
    total = ent_bits + cs_bits
    return [(num >> (total - 11 * (i + 1))) & 0x7FF for i in range(total // 11)]

def load_wordlist():
    """Sucht english.txt neben dem Skript oder im 'mnemonic'-Paket; verifiziert Hash."""
    candidates = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "english.txt")]
    try:
        import mnemonic as _m
        candidates.append(os.path.join(os.path.dirname(_m.__file__),
                                       "wordlist", "english.txt"))
    except ImportError:
        pass
    for p in candidates:
        if os.path.exists(p):
            data = open(p, "rb").read()
            if hashlib.sha256(data).hexdigest() != WORDLIST_SHA256:
                fail_abort("BIP39-Wortliste",
                           f"SHA-256 von {p} stimmt NICHT mit der offiziellen Liste "
                           "ueberein — Datei manipuliert oder beschaedigt!")
            words = data.decode("utf-8").split()
            if len(words) != 2048:
                fail_abort("BIP39-Wortliste", f"{p}: erwartet 2048 Woerter, gefunden {len(words)}.")
            return words, p
    return None, None

# ----------------------------------------------------------------------------
# Selbsttest (BIP39-Vektoren + Kombinierer)
# ----------------------------------------------------------------------------
def selftest():
    print("Selbsttest laeuft …")
    # 1) Offizieller BIP39-Vektor: 16x 0x00 -> Indizes [0]*11 + [3]
    #    ("abandon"x11 + "about", da 'about' Index 3 der Wortliste ist)
    idx = bip39_indices(b"\x00" * 16)
    assert idx == [0] * 11 + [3], f"Vektor 0x00 fehlgeschlagen: {idx}"
    # 2) Vektor 16x 0xFF: erste 11 Indizes = 2047, letzter = (0x7F<<4)|cs
    idx = bip39_indices(b"\xff" * 16)
    cs = hashlib.sha256(b"\xff" * 16).digest()[0] >> 4
    assert idx[:11] == [2047] * 11 and idx[11] == (0x7F << 4) | cs, "Vektor 0xFF fehlgeschlagen"
    # 3) Laengen: 128 Bit -> 12 Indizes, 256 Bit -> 24 Indizes
    assert len(bip39_indices(os.urandom(16))) == 12
    assert len(bip39_indices(os.urandom(32))) == 24
    # 4) Checksummen-Roundtrip fuer 256 Bit
    e = os.urandom(32)
    idx = bip39_indices(e)
    num = 0
    for i in idx: num = (num << 11) | i
    ent = (num >> 8).to_bytes(32, "big")
    cs  = num & 0xFF
    assert ent == e and cs == hashlib.sha256(e).digest()[0], "Roundtrip 256 fehlgeschlagen"
    # 5) Kombinierer: deterministisch & sensitiv auf jede Quelle
    def sr(tag, data): return SourceResult(tag, dsha512(tag, data), len(data), 0)
    a = combine(sr("hwrng", b"A"), sr("decay", b"B"), sr("cam", b"C"), sr("dice", b"D"), 32)
    # (combine mischt os.urandom -> zwei Aufrufe muessen sich unterscheiden)
    b = combine(sr("hwrng", b"A"), sr("decay", b"B"), sr("cam", b"C"), sr("dice", b"D"), 32)
    assert a != b and len(a) == 32
    # 6) HMAC-Kombinierer-Kern deterministisch pruefen (ohne urandom-Anteil)
    pool = dsha512("pool", b"x")
    k1 = hmac.new(dsha512("dice", b"D1"), pool, hashlib.sha512).digest()
    k2 = hmac.new(dsha512("dice", b"D2"), pool, hashlib.sha512).digest()
    assert k1 != k2
    # 7) Wuerfel-Kodierung
    assert dice_to_bytes([1, 1, 1]) == (3).to_bytes(4, "big") + b"\x00"
    assert dice_to_bytes([6]) == (1).to_bytes(4, "big") + b"\x05"
    # 8) Wortliste (falls vorhanden) verifizieren
    words, path = load_wordlist()
    if words:
        assert words[0] == "abandon" and words[3] == "about" and words[2047] == "zoo"
        print(f"  Wortliste verifiziert: {path}")
    else:
        print("  Hinweis: keine english.txt gefunden — Wortlistentest uebersprungen.")
    print(C.GRN + "  ✔ Alle Selbsttests bestanden." + C.RESET)



# ----------------------------------------------------------------------------
# WLAN-Deaktivierung beim Start (Ethernet bleibt unberuehrt)
# ----------------------------------------------------------------------------
def disable_wifi(args):
    """Blockiert WLAN via rfkill (Fallback: nmcli). Kabelgebundenes Ethernet
    ist davon nicht betroffen. Mit --keep-wifi ueberspringbar."""
    if getattr(args, "keep_wifi", False):
        print(C.YEL + " ⚠ WLAN bleibt auf Wunsch AKTIV (--keep-wifi) — fuer echte "
              "Seeds nicht empfohlen!" + C.RESET)
        return
    import subprocess
    methode = None
    try:
        subprocess.run(["rfkill", "block", "wlan"], check=True,
                       capture_output=True, timeout=10)
        methode = "rfkill"
    except Exception:
        try:
            subprocess.run(["nmcli", "radio", "wifi", "off"], check=True,
                           capture_output=True, timeout=10)
            methode = "nmcli"
        except Exception:
            methode = None
    if methode:
        # Verifizieren, soweit moeglich
        status = ""
        try:
            out = subprocess.run(["rfkill", "list", "wlan"], capture_output=True,
                                 text=True, timeout=10).stdout
            if "Soft blocked: yes" in out:
                status = " (verifiziert: Soft blocked)"
        except Exception:
            pass
        print(C.GRN + f" ✔ WLAN deaktiviert via {methode}{status}. "
              "Ethernet bleibt unberuehrt." + C.RESET)
        print(C.DIM + "   Wieder aktivieren nach dem Lauf: sudo rfkill unblock wlan" + C.RESET)
    else:
        if args.mock:
            print(C.YEL + " ⚠ WLAN-Deaktivierung nicht moeglich (rfkill/nmcli fehlen) "
                  "— im MOCK-Modus toleriert." + C.RESET)
            return
        print(C.RED + C.BOLD + " ⚠ WLAN konnte NICHT deaktiviert werden "
              "(rfkill/nmcli fehlgeschlagen — sudo? rfkill installiert?)." + C.RESET)
        if args.yes:
            fail_abort("WLAN-Deaktivierung", "Automatikmodus (--yes) bricht ohne "
                       "bestaetigte Netztrennung ab.")
        antwort = input("   Trotzdem fortfahren? Nur 'ja' setzt fort: ").strip().lower()
        if antwort != "ja":
            fail_abort("WLAN-Deaktivierung", "Vom Benutzer abgebrochen — bitte WLAN "
                       "manuell trennen und neu starten.")

# ----------------------------------------------------------------------------
# Hardware-Initialisierungstest (Healthcheck vor Beginn)
# ----------------------------------------------------------------------------
def startup_healthcheck(mock: bool):
    """Jede Hardware-Quelle muss 10 unterschiedliche Signale in Folge liefern,
    die Daten werden angezeigt. Erst danach laeuft das Skript weiter."""
    hr()
    print(C.BOLD + " Hardware-Initialisierungstest — je 10 Signale pro Quelle" + C.RESET)
    print(C.DIM + " (Wuerfel sind eine manuelle Quelle und werden hier nicht getestet)" + C.RESET)

    # ---- Check 1/3: BCM2712 HWRNG ------------------------------------------
    print(C.BOLD + "\n [Check 1/3] BCM2712 HWRNG" + C.RESET)
    proben = []
    for i in range(10):
        if mock:
            raw = os.urandom(8)
        else:
            try:
                with open("/dev/hwrng", "rb") as f:
                    raw = f.read(8)
            except Exception as e:
                fail_abort("HWRNG (Initialisierungstest)", f"Lesefehler: {e}")
        if not raw or len(raw) != 8:
            fail_abort("HWRNG (Initialisierungstest)", "Unvollstaendige Probe gelesen.")
        proben.append(raw)
        print(f"   Probe {i+1:2d}/10: {raw.hex()}")
    if len(set(proben)) != 10:
        fail_abort("HWRNG (Initialisierungstest)",
                   "Mindestens zwei von 10 Proben identisch — RNG liefert keine frischen Daten.")
    print(C.GRN + "   ✔ 10/10 unterschiedliche Zufallsproben" + C.RESET)

    # ---- Check 2/3: Radiozerfall (CAJOE) -----------------------------------
    print(C.BOLD + "\n [Check 2/3] Radiozerfall (CAJOE) — warte auf 10 Ereignisse …" + C.RESET)
    ts_list = []
    if mock:
        import random
        t = time.monotonic_ns()
        for i in range(10):
            t += int(random.expovariate(0.5) * 1e9)
            ts_list.append(t)
            d = (ts_list[-1] - ts_list[-2]) / 1e9 if i else 0.0
            print(f"   Signal {i+1:2d}/10: t = {t} ns   Δ = {d:7.3f} s  [MOCK]")
    else:
        try:
            import lgpio
        except ImportError:
            fail_abort("Radiozerfall (Initialisierungstest)",
                       "Python-Modul 'lgpio' fehlt: sudo apt install python3-lgpio")
        handle, edge = find_rp1_chip(lgpio, GPIO_GEIGER)
        if handle is None:
            fail_abort("Radiozerfall (Initialisierungstest)",
                       f"GPIO{GPIO_GEIGER} konnte nicht belegt werden.")
        def _cb(chip, gpio, level, ts_ns):
            if ts_list and ts_ns - ts_list[-1] < DECAY_DEBOUNCE_NS:
                return
            ts_list.append(ts_ns)
        cb = lgpio.callback(handle, GPIO_GEIGER, edge, _cb)
        gezeigt = 0
        letzte_aktivitaet = time.monotonic()
        try:
            while gezeigt < 10:
                time.sleep(0.1)
                while gezeigt < min(len(ts_list), 10):
                    t = ts_list[gezeigt]
                    d = (t - ts_list[gezeigt-1]) / 1e9 if gezeigt else 0.0
                    print(f"   Signal {gezeigt+1:2d}/10: t = {t} ns   Δ = {d:7.3f} s")
                    gezeigt += 1
                    letzte_aktivitaet = time.monotonic()
                if time.monotonic() - letzte_aktivitaet > DECAY_TIMEOUT_S:
                    fail_abort("Radiozerfall (Initialisierungstest)",
                               f"{DECAY_TIMEOUT_S}s ohne Zerfallsereignis.")
        except KeyboardInterrupt:
            fail_abort("Radiozerfall (Initialisierungstest)", "Vom Benutzer abgebrochen.")
        finally:
            try:
                cb.cancel(); lgpio.gpiochip_close(handle)
            except Exception:
                pass
    if sorted(ts_list) != ts_list or len(set(ts_list)) != len(ts_list):
        fail_abort("Radiozerfall (Initialisierungstest)",
                   "Zeitstempel nicht streng aufsteigend — Messung unplausibel.")
    deltas = [round((b - a) / 1e9, 2) for a, b in zip(ts_list, ts_list[1:])]
    if len(set(deltas)) < 3:
        fail_abort("Radiozerfall (Initialisierungstest)",
                   "Pulsintervalle nahezu konstant — Stoerquelle statt Zerfall?")
    print(C.GRN + "   ✔ 10/10 Ereignisse, Intervalle unregelmaessig (zerfallstypisch)" + C.RESET)

    # ---- Check 3/3: MLX90640 IR-Kamera -------------------------------------
    print(C.BOLD + "\n [Check 3/3] MLX90640 IR-Kamera" + C.RESET)
    hashes = []
    if mock:
        for i in range(10):
            frame = os.urandom(832 * 2)
            h = hashlib.sha256(frame).hexdigest()
            hashes.append(h)
            print(f"   Frame {i+1:2d}/10: SHA256 {h[:16]}…  (erste Rohworte: {frame[:6].hex()}) [MOCK]")
    else:
        try:
            from smbus2 import SMBus, i2c_msg
        except ImportError:
            fail_abort("MLX90640 (Initialisierungstest)",
                       "Python-Modul 'smbus2' fehlt: sudo apt install python3-smbus2")
        try:
            bus = SMBus(MLX_I2C_BUS)
        except OSError as e:
            fail_abort("MLX90640 (Initialisierungstest)", f"I2C-Bus nicht verfuegbar: {e}")
        try:
            for i in range(10):
                t0 = time.monotonic()
                while True:
                    st = _mlx_read_words(bus, 0x8000, 1)
                    if st[1] & 0x08:
                        break
                    if time.monotonic() - t0 > 5:
                        fail_abort("MLX90640 (Initialisierungstest)",
                                   "Timeout beim Warten auf neues Frame (Adresse 0x33?).")
                    time.sleep(0.02)
                frame = _mlx_read_words(bus, 0x0400, 832)
                wmsg = i2c_msg.write(MLX_I2C_ADDR, [0x80, 0x00, 0x00, 0x30])
                bus.i2c_rdwr(wmsg)
                if len(set(frame)) < 8:
                    fail_abort("MLX90640 (Initialisierungstest)",
                               "Frame mit nahezu konstantem Inhalt — Sensor defekt?")
                h = hashlib.sha256(frame).hexdigest()
                hashes.append(h)
                print(f"   Frame {i+1:2d}/10: SHA256 {h[:16]}…  (erste Rohworte: {bytes(frame[:6]).hex()})")
        except OSError as e:
            fail_abort("MLX90640 (Initialisierungstest)", f"I2C-Fehler: {e}")
        finally:
            try: bus.close()
            except Exception: pass
    if len(set(hashes)) != 10:
        fail_abort("MLX90640 (Initialisierungstest)",
                   "Identische Frames erkannt — kein lebendiges Sensorrauschen.")
    print(C.GRN + "   ✔ 10/10 unterschiedliche Frames (Sensorrauschen vorhanden)" + C.RESET)

    hr()
    print(C.GRN + C.BOLD + " ✔ HARDWARE-CHECK BESTANDEN — alle Quellen liefern lebendige Signale."
          + C.RESET)

# ----------------------------------------------------------------------------
# Interaktive Konfiguration
# ----------------------------------------------------------------------------
def ask_int(prompt, default, lo, hi):
    while True:
        s = input(f"   {prompt} [{default}]: ").strip()
        if not s:
            return default
        try:
            v = int(s)
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(C.YEL + f"   Bitte Zahl zwischen {lo} und {hi} eingeben." + C.RESET)

def min_decay_events(target_bits: int) -> int:
    """Mindestzahl Zerfallsereignisse, damit die konservativ kreditierten
    2 Bit/Ereignis allein schon die Ziel-Entropie abdecken."""
    return math.ceil(target_bits / DECAY_BITS_PER_EVENT)

def configure(args):
    if args.yes:
        target = 256 if args.words == 24 else 128
        mind = min_decay_events(target)
        if args.decay < mind:
            fail_abort("Konfiguration",
                       f"--decay {args.decay} unterschreitet das Minimum von {mind} "
                       f"Ereignissen ({mind} x {DECAY_BITS_PER_EVENT} Bit = {target} Bit).")
        if args.dice < DICE_MIN_ROLLS:
            fail_abort("Konfiguration",
                       f"--dice {args.dice} unterschreitet das Minimum von "
                       f"{DICE_MIN_ROLLS} Wuerfelwuerfen.")
        return args.words, args.decay, args.frames, args.dice, args.transparenz
    print(C.BOLD + "\n Modusauswahl" + C.RESET)
    print("   [1] STANDARD-MODUS    — kompakte Fortschrittsanzeigen")
    print("   [2] TRANSPARENZ-MODUS — alle Quelldaten & Hashwerte in Echtzeit")
    transparent = ask_int("Modus waehlen", 2 if args.transparenz else 1, 1, 2) == 2
    if transparent:
        print(C.YEL + "   ⚠ TRANSPARENZ-MODUS: Es werden geheime Zwischenwerte "
              "(bis hin zur Seed-Entropie) am Bildschirm angezeigt.\n"
              "     Sicherstellen, dass niemand mitliest/mitfilmt!" + C.RESET)
    print(C.BOLD + "\n Konfiguration" + C.RESET)
    words = 24 if ask_int("Seed-Laenge: 12 oder 24 Woerter", args.words, 12, 24) >= 18 else 12
    target = 256 if words == 24 else 128
    rec_dice = math.ceil(target / DICE_BITS_PER_ROLL)
    print(C.DIM + f"   Ziel-Entropie: {target} Bit" + C.RESET)
    mind = min_decay_events(target)
    print(C.DIM + f"   Minimum Zerfallsereignisse: {mind}  "
          f"({mind} x {DECAY_BITS_PER_EVENT} Bit konservativ = {target} Bit)" + C.RESET)
    decay  = ask_int(f"Zerfallsereignisse (Minimum {mind}, Empf. ≥{max(512, 2*target)}; "
                     f"~17 CPM ⇒ {max(512,2*target)/17:.0f} min)",
                     max(args.decay, mind), mind, 100000)

    frames = ask_int("Kamera-Frames MLX90640 (Empf. ≥16)", args.frames, 2, 1000)
    print(C.DIM + f"   Minimum Wuerfelwuerfe: {DICE_MIN_ROLLS} (~{DICE_MIN_ROLLS*DICE_BITS_PER_ROLL:.0f} Bit) — "
          f"fuer volle Hardware-Unabhaengigkeit {rec_dice} empfohlen" + C.RESET)
    dice   = ask_int(f"Wuerfelwuerfe (Minimum {DICE_MIN_ROLLS}, Empf. ≥{rec_dice})",
                     max(args.dice, DICE_MIN_ROLLS), DICE_MIN_ROLLS, 1000)
    return words, decay, frames, dice, transparent

# ----------------------------------------------------------------------------
# Hauptablauf
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="BIP39-Seed-Generator mit Hardware-Entropie")
    ap.add_argument("--selftest", action="store_true", help="Krypto-Selbsttests ausfuehren")
    ap.add_argument("--mock", action="store_true",
                    help="Hardware simulieren — NUR ZUM TESTEN, NIE fuer echte Seeds!")
    ap.add_argument("--yes", action="store_true", help="Konfigurationsfragen ueberspringen")
    ap.add_argument("--keep-wifi", action="store_true",
                    help="WLAN NICHT deaktivieren (nicht empfohlen)")
    ap.add_argument("--transparenz", action="store_true",
                    help="Transparenz-Modus: alle Quelldaten & Hashes anzeigen")
    ap.add_argument("--words",  type=int, default=24, choices=(12, 24))
    ap.add_argument("--decay",  type=int, default=1024)
    ap.add_argument("--frames", type=int, default=32)
    ap.add_argument("--dice",   type=int, default=100)
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    banner()
    selftest()          # Selbsttest laeuft VOR jeder echten Erzeugung
    hr()
    disable_wifi(args)
    hr()

    if args.mock:
        print(C.RED + C.BOLD + " ⚠  MOCK-MODUS: simulierte Hardware — "
              "erzeugte Seeds NIEMALS fuer echte Wallets verwenden!" + C.RESET)

    words_list, wl_path = load_wordlist()
    if words_list is None and not args.mock:
        fail_abort("BIP39-Wortliste",
                   "Keine verifizierbare english.txt gefunden. Bitte die offizielle "
                   "BIP39-Wortliste als 'english.txt' neben das Skript legen "
                   "(SHA-256 wird automatisch geprueft).")

    startup_healthcheck(args.mock)

    n_words, n_decay, n_frames, n_dice, transparent = configure(args)
    if transparent:
        print(C.CYA + C.BOLD + " ► TRANSPARENZ-MODUS AKTIV" + C.RESET)
    target_bits = 256 if n_words == 24 else 128
    n_bytes = target_bits // 8

    hr()
    print(C.BOLD + " Ablaufplan (feste Reihenfolge)" + C.RESET)
    status_line(1, 4, "BCM2712 HWRNG",        "wait", "einmalige Entnahme (512 Bit)")
    status_line(2, 4, "Radiozerfall (CAJOE)", "wait", f"{n_decay} Ereignisse")
    status_line(3, 4, "MLX90640 IR-Kamera",   "wait", f"{n_frames} Roh-Frames")
    status_line(4, 4, "Wuerfel (manuell)",    "wait", f"{n_dice} Wuerfe — letzte Quelle")
    hr()

    results = []
    t0 = time.monotonic()

    print(C.BOLD + "\n Phase 1/4 — BCM2712 HWRNG" + C.RESET)
    r = collect_hwrng(args.mock, transparent); results.append(r)
    status_line(1, 4, r.name, "ok", r.info)

    print(C.BOLD + "\n Phase 2/4 — Radioaktiver Zerfall" + C.RESET)
    print(C.DIM + "   (Dauer haengt von der Zaehlrate ab — bei Hintergrundstrahlung "
          "typ. 30–120 min fuer die empfohlene Ereigniszahl)" + C.RESET)
    r = collect_decay(n_decay, args.mock, transparent); results.append(r)
    status_line(2, 4, r.name, "ok", r.info)

    print(C.BOLD + "\n Phase 3/4 — MLX90640 IR-Kamera" + C.RESET)
    r = collect_camera(n_frames, args.mock, transparent); results.append(r)
    status_line(3, 4, r.name, "ok", r.info)

    print(C.BOLD + "\n Phase 4/4 — Wuerfel (hardwareunabhaengige letzte Quelle)" + C.RESET)
    r = collect_dice(n_dice, target_bits, args.mock, transparent); results.append(r)
    status_line(4, 4, r.name, "ok", r.info)

    hw, dec, cam, dice = results
    hr()
    print(C.BOLD + " Entropie-Bilanz (konservativ kreditiert)" + C.RESET)
    total_credit = 0.0
    for s in results:
        total_credit += s.credited_bits
        print(f"   {s.name:<24} {s.credited_bits:8.0f} Bit   ({s.raw_len} B roh)")
    print(f"   {'SUMME':<24} {total_credit:8.0f} Bit   (Ziel: {target_bits} Bit)")
    if total_credit < 2 * target_bits:
        fail_abort("Entropie-Bilanz",
                   f"Kreditierte Gesamtentropie ({total_credit:.0f} Bit) unter dem "
                   f"Sicherheitsminimum von {2*target_bits} Bit — Parameter erhoehen.")

    print(C.BOLD + "\n Kombiniere: FINAL = HMAC-SHA512(key=H(Wuerfel), msg=Pool(HWRNG,Zerfall,Kamera,OS))"
          + C.RESET)
    entropy = combine(hw, dec, cam, dice, n_bytes, transparent)
    idx = bip39_indices(entropy)

    dur = (time.monotonic() - t0) / 60
    hr("━")
    print(C.GRN + C.BOLD + f" ✔ SEED ERZEUGT  ({n_words} Woerter, {target_bits} Bit, "
          f"Dauer {dur:.1f} min)" + C.RESET)
    hr("━")
    if words_list:
        for i in range(0, len(idx), 4):
            row = "   ".join(f"{C.DIM}{j+1:2d}.{C.RESET} {C.BOLD}{words_list[idx[j]]:<10}{C.RESET}"
                             for j in range(i, min(i + 4, len(idx))))
            print("  " + row)
    else:
        print(C.YEL + "  (Keine Wortliste — zeige BIP39-Wortindizes:)" + C.RESET)
        print("  " + " ".join(str(i) for i in idx))
    hr("━")
    print(C.RED + C.BOLD + "  WICHTIG:" + C.RESET + C.RED +
          " Seed NUR handschriftlich auf Papier/Metall sichern. Kein Foto,\n"
          "  kein Cloud-Backup, keine Datei. Bildschirm danach leeren (reset/clear),\n"
          "  Geraet offline lassen. Seed vor Nutzung auf einem zweiten, unabhaengigen\n"
          "  Offline-Geraet (z.B. Hardware-Wallet) verifizieren." + C.RESET)

if __name__ == "__main__":
    main()
