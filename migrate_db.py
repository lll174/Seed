#!/usr/bin/env python3
"""
migrate_db.py — verschiebt die Datenbank auf ein anderes Speichermedium.

    python3 migrate_db.py                 # zeigt eingehängte Medien, fragt nach Auswahl
    python3 migrate_db.py --target /mnt/ssd/hl
    python3 migrate_db.py --dry-run       # nur anzeigen, was passieren würde

Ablauf, jeder Schritt wird geprüft, bei Fehlern wird zurückgerollt:
  1. Ziel wählen (eingehängte Medien mit Dateisystem, freiem Platz, fstab-Status)
  2. Dienste stoppen, warten bis kein Prozess mehr an der Datenbank hängt
  3. WAL einfalten, integrity_check an der Quelle
  4. Datenbank und Backup-Ordner kopieren
  5. integrity_check am Ziel, Zeilenzahlen vergleichen
  6. fstab-Eintrag anbieten, damit das Medium nach Neustart wieder da ist
  7. systemd-Units auf den neuen Pfad umstellen
  8. Dienste starten, prüfen, dass der Recorder am neuen Ort schreibt
  9. Alte Datenbank umbenennen (nicht löschen)

Dateisysteme ohne saubere Sperren (FAT, exFAT, NTFS) werden abgelehnt — SQLite
im WAL-Modus braucht ext4 oder ein anderes POSIX-Dateisystem.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVICES = ["hl-recorder", "hl-binance", "hl-dashboard", "hl-watchdog"]
UNIT = Path("/etc/systemd/system/hl-recorder.service")
BAD_FS = {"vfat", "fat", "fat32", "exfat", "ntfs", "ntfs3", "fuseblk", "msdos"}
OK, WARN, FAIL = "  [ok]   ", "  [!]    ", "  [FEHLER]"


def run(cmd, sudo=False, check=False):
    if sudo and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def have_systemd() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def drop_root() -> None:
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user and user != "root":
        os.execvp("sudo", ["sudo", "-u", user, "-H", sys.executable] + sys.argv)


def current_db() -> Path:
    if UNIT.exists():
        m = re.search(r"--db\s+(\S+)", UNIT.read_text(errors="replace"))
        if m:
            return Path(m.group(1))
    return ROOT / "hl_liq.db"


def device_of(path: Path) -> int:
    """st_dev des nächsten existierenden Vorfahren."""
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.stat(p).st_dev


def human(n: float) -> str:
    for u in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Zielmedien

def mounted_media() -> list[dict]:
    """Eingehängte Blockgeräte über lsblk. Root und Boot bleiben aussen vor."""
    r = run(["lsblk", "-J", "-o", "NAME,PATH,MOUNTPOINT,FSTYPE,SIZE,LABEL,TRAN,RM,UUID,MODEL"])
    if r.returncode != 0:
        return []
    out = []

    def walk(nodes, parent_tran=None, parent_model=None):
        for n in nodes:
            tran = n.get("tran") or parent_tran
            model = n.get("model") or parent_model
            mp = n.get("mountpoint")
            if mp and mp not in ("/", "/boot", "/boot/firmware") and not mp.startswith("/boot"):
                try:
                    st = os.statvfs(mp)
                    free = st.f_bavail * st.f_frsize
                except OSError:
                    free = 0
                out.append({"dev": n.get("path"), "mount": mp, "fs": (n.get("fstype") or "?").lower(),
                            "size": n.get("size"), "label": n.get("label") or "",
                            "tran": tran or "", "rm": n.get("rm") in (True, "1", 1),
                            "uuid": n.get("uuid") or "", "model": (model or "").strip(),
                            "free": free})
            walk(n.get("children", []), tran, model)

    walk(json.loads(r.stdout).get("blockdevices", []))
    return out


def in_fstab(mount: str, uuid: str) -> bool:
    try:
        txt = Path("/etc/fstab").read_text()
    except OSError:
        return False
    return any((uuid and f"UUID={uuid}" in l) or f" {mount} " in l + " "
               for l in txt.splitlines() if l.strip() and not l.startswith("#"))


def choose_target(explicit: str | None) -> tuple[Path, dict | None]:
    if explicit:
        p = Path(explicit).expanduser()
        media = mounted_media()
        # welches Medium liegt unter dem Pfad?
        best = None
        for m in media:
            if str(p).startswith(m["mount"].rstrip("/") + "/") or str(p) == m["mount"]:
                if best is None or len(m["mount"]) > len(best["mount"]):
                    best = m
        return p, best

    media = mounted_media()
    print("\nEingehängte Medien (ohne Systempartition):")
    if not media:
        print("  keines gefunden. USB-Medium einstecken, einhängen, erneut starten —")
        print("  oder einen Pfad angeben: python3 migrate_db.py --target /pfad")
        sys.exit(1)
    for i, m in enumerate(media, 1):
        bad = m["fs"] in BAD_FS
        fst = "in fstab" if in_fstab(m["mount"], m["uuid"]) else "NICHT in fstab"
        via = f"{m['tran'].upper()}" if m["tran"] else ("USB?" if m["rm"] else "intern")
        print(f"  {i}. {m['mount']:<28} {m['fs']:<6} {human(m['free']):>9} frei "
              f"von {m['size']:<6} {via:<6} {m['label'] or m['model']}  ·  {fst}"
              + ("   <- ungeeignet (kein POSIX-Dateisystem)" if bad else ""))
    print("  0. eigenen Pfad eingeben")
    while True:
        sel = input("\nZiel wählen: ").strip()
        if sel == "0":
            return Path(input("Pfad: ").strip()).expanduser(), None
        if sel.isdigit() and 1 <= int(sel) <= len(media):
            m = media[int(sel) - 1]
            return Path(m["mount"]) / "hl_liq", m


# ---------------------------------------------------------------------------
# Datenbank

def wait_free(db: Path, timeout: int = 60) -> bool:
    if shutil.which("fuser") is None:
        time.sleep(3)
        return True
    for _ in range(timeout):
        if run(["fuser", str(db)]).returncode != 0:      # rc 1 = niemand
            return True
        time.sleep(1)
    return False


def checkpoint(db: Path) -> None:
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def integrity(db: Path) -> tuple[bool, str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    r = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    return r == "ok", r


def counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabs}
    con.close()
    return out


def newest_bar(db: Path) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    r = con.execute("SELECT MAX(ts) FROM bars").fetchone()[0]
    con.close()
    return r or 0


def copy_with_progress(src: Path, dst: Path) -> None:
    total = src.stat().st_size
    done = 0
    t0 = time.time()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(8 << 20)
            if not chunk:
                break
            fo.write(chunk)
            done += len(chunk)
            if total > 64 << 20:
                pct = done / total * 100
                rate = done / max(time.time() - t0, 0.1)
                print(f"\r        {pct:5.1f} %  {human(rate)}/s   ", end="", flush=True)
        fo.flush()
        os.fsync(fo.fileno())
    if total > 64 << 20:
        print()


# ---------------------------------------------------------------------------

def main() -> None:
    drop_root()
    p = argparse.ArgumentParser(description="Datenbank auf anderes Medium verschieben")
    p.add_argument("--target", help="Zielordner (sonst Auswahl)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    py = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv").exists() else sys.executable

    print("=" * 64)
    print(" Datenbank-Migration")
    print("=" * 64)

    src = current_db()
    if not src.exists():
        sys.exit(f"Datenbank nicht gefunden: {src}")
    src_size = src.stat().st_size
    # Backup-Ordner: bisheriger Ort aus der Unit, sonst neben der Datenbank
    m_bak = re.search(r"--backup-dir\s+(\S+)", UNIT.read_text(errors="replace")) if UNIT.exists() else None
    src_bak = Path(m_bak.group(1)) if m_bak else src.parent / "dbbackup"
    bak_size = sum(f.stat().st_size for f in src_bak.rglob("*") if f.is_file()) if src_bak.exists() else 0
    print(f"\nQuelle: {src}  ({human(src_size)})")
    print(f"Backup: {src_bak}" + (f"  ({human(bak_size)})" if bak_size else "  (noch keines)"))

    # 1. Ziel
    target, medium = choose_target(args.target)
    dst = target / "hl_liq.db"
    print(f"\nZiel:   {dst}")
    print(f"Logs:   {target / 'logs'}  (recorder.log, binance.log, watchdog.log — WAL liegt "
          "automatisch neben der Datenbank)")
    if medium:
        print(f"        Medium {medium['dev']} ({medium['fs']}, {human(medium['free'])} frei, "
              f"{'in fstab' if in_fstab(medium['mount'], medium['uuid']) else 'nicht in fstab'})")
        if medium["fs"] in BAD_FS:
            print(f"\n{FAIL}Dateisystem {medium['fs']} ist für SQLite im WAL-Modus ungeeignet "
                  "(keine sauberen Sperren).")
            print("        Medium auf ext4 formatieren (löscht alle Daten darauf!):")
            print(f"          sudo umount {medium['mount']}")
            print(f"          sudo mkfs.ext4 -L hl {medium['dev']}")
            print(f"          sudo mkdir -p /mnt/hl && sudo mount {medium['dev']} /mnt/hl "
                  f"&& sudo chown $USER:$USER /mnt/hl")
            sys.exit(1)
        need = (src_size + bak_size) * 1.2
        if medium["free"] < need:
            sys.exit(f"{FAIL}Zu wenig Platz: {human(medium['free'])} frei, "
                     f"{human(need)} nötig (mit Reserve)")
    if dst.resolve() == src.resolve():
        sys.exit("Ziel ist die Quelle.")

    # Backup bleibt auf dem Pi. Läge es mit auf dem Zielmedium, wären Datenbank
    # und Sicherung beim Ausfall des Mediums gemeinsam weg.
    if device_of(src_bak) != device_of(target):
        backup_dir = src_bak                      # liegt schon woanders als das Ziel
    elif device_of(ROOT) != device_of(target):
        backup_dir = ROOT / "dbbackup"            # Projektordner auf der SD-Karte
    else:
        backup_dir = Path.home() / "dbbackup"
    same = device_of(backup_dir) == device_of(target)
    print(f"Backup danach: {backup_dir}  "
          + ("<- ACHTUNG gleicher Datenträger wie das Ziel" if same
             else "(bleibt auf dem Pi, getrennt vom Ziel)"))
    if dst.exists():
        sys.exit(f"Am Ziel liegt schon eine Datenbank: {dst}\n"
                 "Erst umbenennen oder anderen Ordner wählen.")
    if args.dry_run:
        print("\n--dry-run: nichts geändert.")
        return
    if input("\nMigration starten? [j/N] ").strip().lower() != "j":
        sys.exit(0)

    # 2. Dienste stoppen
    print("\n2. Dienste stoppen")
    active = []
    if have_systemd():
        active = [s for s in SERVICES if run(["systemctl", "is-active", s]).stdout.strip() == "active"]
        if active:
            run(["systemctl", "stop"] + active, sudo=True, check=True)
            print(f"{OK}gestoppt: {', '.join(active)}")
        else:
            print(f"{WARN}kein Dienst aktiv")
    if not wait_free(src):
        sys.exit(f"{FAIL}Datenbank wird noch von einem Prozess gehalten (fuser {src})")
    print(f"{OK}kein Prozess hält die Datenbank")

    def rollback(reason: str):
        print(f"\n{FAIL}{reason}")
        print("        Rückrollen: Dienste mit der alten Datenbank starten.")
        for f in (dst, dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm")):
            try:
                f.exists() and f.unlink()
            except OSError:
                pass
        if have_systemd() and active:
            run(["systemctl", "start"] + active, sudo=True)
        sys.exit(1)

    # 3. WAL einfalten, Quelle prüfen
    print("\n3. Quelle")
    try:
        checkpoint(src)
    except sqlite3.Error as e:
        rollback(f"Checkpoint fehlgeschlagen: {e}")
    leftovers = [f for f in (src.with_name(src.name + "-wal"), src.with_name(src.name + "-shm"))
                 if f.exists() and f.stat().st_size > 0]
    print(f"{OK if not leftovers else WARN}WAL eingefaltet"
          + (f" — Reste: {', '.join(f.name for f in leftovers)}" if leftovers else ""))
    ok, msg = integrity(src)
    if not ok:
        rollback(f"Quelle beschädigt: {msg[:120]} — nicht migrieren, Backup prüfen")
    src_counts = counts(src)
    print(f"{OK}integrity_check ok, {sum(src_counts.values()):,} Zeilen in {len(src_counts)} Tabellen")

    # 4. Kopieren
    print("\n4. Kopieren")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        rollback(f"Zielordner nicht anlegbar: {e}")
    if not os.access(target, os.W_OK):
        rollback(f"Kein Schreibrecht auf {target} — sudo chown $USER {target}")
    t0 = time.time()
    try:
        copy_with_progress(src, dst)
    except OSError as e:
        rollback(f"Kopieren fehlgeschlagen: {e}")
    print(f"{OK}Datenbank kopiert, {human(src_size)} in {time.time()-t0:.0f} s")
    if src_bak.exists() and src_bak != backup_dir:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for f in src_bak.iterdir():
            if f.is_file():
                shutil.copy2(f, backup_dir / f.name)
        print(f"{OK}Backup-Ordner nach {backup_dir} übernommen ({human(bak_size)})")
    else:
        print(f"{OK}Backup-Ordner bleibt: {backup_dir}")

    # 5. Ziel prüfen
    print("\n5. Ziel prüfen")
    ok, msg = integrity(dst)
    if not ok:
        rollback(f"Kopie beschädigt: {msg[:120]}")
    dst_counts = counts(dst)
    diff = {t: (src_counts.get(t), dst_counts.get(t)) for t in src_counts
            if src_counts.get(t) != dst_counts.get(t)}
    if diff:
        rollback(f"Zeilenzahlen weichen ab: {diff}")
    print(f"{OK}integrity_check ok, alle {len(src_counts)} Tabellen zeilengleich")

    # 6. fstab
    print("\n6. Einhängen nach Neustart")
    if medium and medium["uuid"] and not in_fstab(medium["mount"], medium["uuid"]):
        line = (f"UUID={medium['uuid']}  {medium['mount']}  {medium['fs']}  "
                f"defaults,noatime,nofail,x-systemd.device-timeout=10  0  2")
        print(f"{WARN}{medium['mount']} steht nicht in /etc/fstab — nach einem Neustart wäre das")
        print("        Medium nicht eingehängt und die Dienste fänden die Datenbank nicht.")
        print(f"        Vorgeschlagener Eintrag:\n          {line}")
        if input("        In /etc/fstab eintragen? [j/N] ").strip().lower() == "j":
            r = run(["sh", "-c", f"echo '{line}' >> /etc/fstab"], sudo=True)
            print(f"{OK if r.returncode == 0 else FAIL}fstab"
                  + ("" if r.returncode == 0 else f": {r.stderr.strip()}"))
    elif medium:
        print(f"{OK}Medium ist in fstab eingetragen")
    else:
        print(f"{WARN}Pfad ohne erkanntes Medium — sicherstellen, dass er nach Neustart existiert")

    # 7. Units -- Logs ebenfalls auf das Zielmedium
    log_dir = target / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    print("\n7. systemd-Units")
    if have_systemd():
        r = run([sys.executable, str(ROOT / "install.py"), "--units-only", "--db", str(dst),
                 "--backup-dir", str(backup_dir), "--log-dir", str(log_dir)])
        units = sorted((ROOT / "systemd").glob("*.service"))
        if r.returncode != 0 or not units:
            rollback(f"Units nicht erzeugbar: {r.stderr[-300:]}")
        run(["cp"] + [str(u) for u in units] + ["/etc/systemd/system/"], sudo=True, check=True)
        run(["systemctl", "daemon-reload"], sudo=True, check=True)
        txt = UNIT.read_text(errors="replace")
        got = re.search(r"--db\s+(\S+)", txt)
        gotb = re.search(r"--backup-dir\s+(\S+)", txt)
        if not got or Path(got.group(1)) != dst:
            rollback("Unit zeigt nicht auf den neuen Pfad")
        if not gotb or Path(gotb.group(1)) != backup_dir:
            rollback("Unit trägt nicht den Backup-Ordner")
        if f"append:{log_dir}/recorder.log" not in txt:
            rollback("Unit schreibt das Log nicht auf das Zielmedium")
        print(f"{OK}{len(units)} Units: Datenbank {dst}, Backup {backup_dir}, Logs {log_dir}")
    else:
        print(f"{WARN}kein systemd — Aufrufe künftig mit --db {dst}")

    # 8. Starten und kontrollieren
    print("\n8. Start")
    if have_systemd():
        to_start = [s for s in SERVICES
                    if run(["systemctl", "is-enabled", s]).stdout.strip().startswith("enabled")] or active
        if to_start:
            run(["systemctl", "start"] + to_start, sudo=True, check=True)
            before = newest_bar(dst)
            print("        warte 75 s, bis der Recorder am neuen Ort eine Kerze geschrieben hat …")
            for i in range(75):
                time.sleep(1)
            states = {s: run(["systemctl", "is-active", s]).stdout.strip() for s in to_start}
            for s, st in states.items():
                print(f"{OK if st == 'active' else FAIL}{s:<16} {st}")
            after = newest_bar(dst)
            wal = dst.with_name(dst.name + "-wal").exists()
            if after > before or wal:
                print(f"{OK}Recorder schreibt am neuen Ort"
                      + (f" (jüngste Kerze vor {int(time.time()-after)} s)" if after else ""))
            else:
                print(f"{WARN}noch keine neue Kerze am neuen Ort — Log prüfen: "
                      "journalctl -u hl-recorder -n 30")
            if any(st != "active" for st in states.values()):
                print(f"\n{FAIL}Nicht alle Dienste laufen. Alte Datenbank bleibt unangetastet.")
                for s, st in states.items():
                    if st != "active":
                        print(run(["journalctl", "-u", s, "-n", "12", "--no-pager"], sudo=True).stdout[-800:])
                sys.exit(1)

    # 9. Alte Datenbank umbenennen
    print("\n9. Alte Datenbank")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    old = src.with_name(f"{src.name}.migriert_{stamp}")
    src.rename(old)
    for suffix in ("-wal", "-shm"):
        f = src.with_name(src.name + suffix)
        f.exists() and f.unlink()
    if src_bak.exists() and src_bak != backup_dir:
        src_bak.rename(src_bak.with_name(f"dbbackup.migriert_{stamp}"))
        print(f"{OK}alter Backup-Ordner umbenannt nach dbbackup.migriert_{stamp}")
    print(f"{OK}umbenannt nach {old.name} — nach ein paar Tagen Betrieb löschen:")
    print(f"        rm {old}")

    print("\n" + "=" * 64)
    print(f" Fertig. Datenbank liegt jetzt unter {dst}")
    print(f" Prüfen: {py} hl_recorder.py --db {dst} --audit")
    print("=" * 64)


if __name__ == "__main__":
    main()
