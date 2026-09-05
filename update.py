#!/usr/bin/env python3
"""
update.py — spielt ein neues Projektarchiv ein und startet die Dienste neu.

    python3 update.py                          # neuestes hl_liq_project*.zip aus ~/Downloads
    python3 update.py ~/Downloads/hl_liq_project.zip
    python3 update.py --no-db-backup           # ohne vorheriges Datenbank-Backup

Ablauf:
  1. Archiv finden (auch hl_liq_project-2.zip, -3.zip … — Browser hängen das an)
  2. Datenbank-Backup über hl_recorder.py --backup
  3. Sicherungskopie der bisherigen Skripte
  4. Dienste stoppen
  5. Dateien aus dem Archiv einspielen (Datenbank, Backups, Logs bleiben unberührt)
  6. Syntaxprüfung aller Skripte — schlägt sie fehl, wird die Sicherung zurückgespielt
  7. systemd-Units neu erzeugen und installieren
  8. Dienste starten und prüfen

Nur die Befehle, die es brauchen, laufen über sudo (systemctl, Kopieren nach
/etc/systemd). Das Skript selbst als normaler Benutzer starten.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVICES = ["hl-recorder", "hl-binance", "hl-dashboard", "hl-watchdog"]
PROJECT_FILES = {"install.py", "requirements.txt", "README.md", "run_all.py",
                 "hl_recorder.py", "hl_viz.py", "hl_binance.py", "hl_timestats.py",
                 "hl_watchdog.py", "hl_backtest.py", "macro_corr.py", "update.py",
                 "migrate_db.py"}
OK, WARN, FAIL = "  [ok]   ", "  [!]    ", "  [FEHLER]"


def run(cmd, sudo=False, check=False, capture=True):
    if sudo and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def have_systemd() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def unit_arg(flag: str) -> str | None:
    """Wert eines Arguments aus der installierten Recorder-Unit, z. B. --backup-dir."""
    unit = Path("/etc/systemd/system/hl-recorder.service")
    if unit.exists():
        import re
        m = re.search(flag + r"\s+(\S+)", unit.read_text(errors="replace"))
        if m:
            return m.group(1)
    return None


def db_path(explicit: str | None) -> Path:
    """
    Wo liegt die Datenbank? Vorrang: --db, dann die installierte Unit
    (dort steht der Pfad nach einem Umzug), zuletzt neben den Skripten.
    """
    if explicit:
        return Path(explicit).expanduser()
    unit = Path("/etc/systemd/system/hl-recorder.service")
    if unit.exists():
        import re
        m = re.search(r"--db\s+(\S+)", unit.read_text(errors="replace"))
        if m:
            return Path(m.group(1))
    return ROOT / "hl_liq.db"


def find_archive(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            sys.exit(f"Archiv nicht gefunden: {p}")
        return p
    candidates = []
    homes = [Path.home()]
    if os.environ.get("SUDO_USER"):
        homes.append(Path("/home") / os.environ["SUDO_USER"])
    dirs = [h / "Downloads" for h in homes] + [ROOT, ROOT.parent, Path.cwd()]
    for d in dirs:
        candidates += [Path(x) for x in glob.glob(str(d / "hl_liq_project*.zip"))]
    if not candidates:
        sys.exit("Kein hl_liq_project*.zip gefunden. Pfad angeben:\n"
                 "  python3 update.py /pfad/zum/archiv.zip")
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if len(candidates) > 1:
        print(f"  {len(candidates)} Archive gefunden, nehme das neueste:")
    return newest


def service_state(name: str) -> str:
    r = run(["systemctl", "is-active", name])
    return r.stdout.strip() or "unbekannt"


def drop_root() -> None:
    """
    Unter sudo gestartet: als der eigentliche Benutzer neu starten.

    Sonst gehören Backup, Sicherung und Bytecode danach root, und die Dienste,
    die als normaler Benutzer laufen, können sie nicht mehr überschreiben.
    Ausserdem zeigt ~ unter sudo auf /root, wo kein Downloads-Ordner liegt.
    """
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user and user != "root":
        print(f"Als root gestartet — wechsle zu Benutzer '{user}' "
              "(sudo wird nur für die Dienste gebraucht).\n")
        os.execvp("sudo", ["sudo", "-u", user, "-H", sys.executable] + sys.argv)
    if os.geteuid() == 0:
        print(f"{WARN}Läuft als root. Dateien gehören danach root — falls die Dienste "
              "als anderer Benutzer laufen, Besitz danach anpassen.\n")


def clean_pycache() -> None:
    """Bytecode-Cache entfernen, notfalls mit sudo, wenn er root gehört."""
    pc = ROOT / "__pycache__"
    if not pc.exists():
        return
    try:
        shutil.rmtree(pc)
    except PermissionError:
        r = run(["rm", "-rf", str(pc)], sudo=True)
        if r.returncode == 0:
            print(f"{OK}root-gehöriges __pycache__ entfernt (sudo)")
        else:
            print(f"{WARN}__pycache__ gehört root und ließ sich nicht entfernen")


def syntax_ok(path: Path) -> str | None:
    """Reine Syntaxprüfung ohne Schreiben auf die Platte. None = in Ordnung."""
    import ast
    try:
        ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        return None
    except SyntaxError as e:
        return f"{path.name}, Zeile {e.lineno}: {e.msg}"


def main() -> None:
    drop_root()
    p = argparse.ArgumentParser(description="Projekt aktualisieren")
    p.add_argument("archive", nargs="?", help="ZIP-Datei; sonst automatische Suche")
    p.add_argument("--no-db-backup", action="store_true")
    p.add_argument("--db", help="Datenbankpfad; sonst aus der installierten Unit")
    args = p.parse_args()
    py = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv").exists() else sys.executable

    print("=" * 62)
    print(" Update hl_liq_project")
    print("=" * 62)

    # 1. Archiv
    print("\n1. Archiv")
    archive = find_archive(args.archive)
    age = (time.time() - archive.stat().st_mtime) / 60
    print(f"{OK}{archive}  ({archive.stat().st_size/1024:.0f} kB, vor {age:.0f} min)")
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n.endswith(".py") or n.endswith((".md", ".txt"))]
        inner = {Path(n).name for n in names}
        missing = {"hl_recorder.py", "hl_viz.py", "install.py"} - inner
        if missing:
            sys.exit(f"Archiv unvollständig, es fehlen: {', '.join(sorted(missing))}")
        print(f"{OK}{len(inner)} Projektdateien im Archiv")

    # 2. Datenbank-Backup
    print("\n2. Datenbank")
    db = db_path(args.db)
    print(f"        Datenbank: {db}")
    if args.no_db_backup:
        print(f"{WARN}Backup übersprungen (--no-db-backup)")
    elif not db.exists():
        print(f"{WARN}keine Datenbank vorhanden, nichts zu sichern")
    else:
        print("        Backup läuft (kann bei großer Datenbank eine Minute dauern) …")
        bdir = unit_arg("--backup-dir")
        r = run([py, str(ROOT / "hl_recorder.py"), "--db", str(db), "--backup"]
                + (["--backup-dir", bdir] if bdir else []))
        last = [l for l in r.stdout.splitlines() if l.strip()][-1:] or [r.stderr.strip()[-200:]]
        if r.returncode == 0:
            print(f"{OK}{last[0]}")
        else:
            print(f"{FAIL}{last[0]}")
            if input("        Trotzdem fortfahren? [j/N] ").strip().lower() != "j":
                sys.exit(1)

    # 3. Sicherung der Skripte
    print("\n3. Sicherung")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = ROOT / "updates" / f"vor_{stamp}"
    bak.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in PROJECT_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, bak / f); n += 1
    print(f"{OK}{n} Dateien nach {bak.relative_to(ROOT)}")

    # 4. Dienste stoppen
    print("\n4. Dienste")
    active = []
    if have_systemd():
        for s in SERVICES:
            if service_state(s) == "active":
                active.append(s)
        if active:
            print(f"        stoppe {', '.join(active)} (sudo) …")
            run(["systemctl", "stop"] + active, sudo=True, check=True)
            print(f"{OK}gestoppt")
        else:
            print(f"{WARN}kein Dienst aktiv")
    else:
        print(f"{WARN}kein systemd — Dienste werden nicht verwaltet")

    # 5. Einspielen
    print("\n5. Einspielen")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
        src_files = {Path(f).name: Path(f) for f in glob.glob(f"{tmp}/**/*", recursive=True)
                     if Path(f).is_file()}
        changed, same = [], []
        for name in sorted(PROJECT_FILES):
            if name not in src_files:
                continue
            dst = ROOT / name
            new = src_files[name].read_bytes()
            if dst.exists() and dst.read_bytes() == new:
                same.append(name)
            else:
                dst.write_bytes(new); changed.append(name)
            if name.endswith(".py"):
                dst.chmod(dst.stat().st_mode | 0o111)
    for f in changed:
        print(f"{OK}{f:<20} aktualisiert")
    if same:
        print(f"        {len(same)} Dateien unverändert")
    # Reste alter Downloads mit -2/-3-Suffix aufräumen
    for junk in glob.glob(str(ROOT / "hl_*-[0-9].py")) + glob.glob(str(ROOT / "hl_*-[0-9][0-9].py")):
        os.remove(junk); print(f"{OK}Altlast entfernt: {Path(junk).name}")
    clean_pycache()

    # 6. Syntaxprüfung
    print("\n6. Prüfung")
    errors = [e for e in (syntax_ok(ROOT / f) for f in sorted(PROJECT_FILES)
                          if f.endswith(".py") and (ROOT / f).exists()) if e]
    if errors:
        print(f"{FAIL}Syntaxfehler — Sicherung wird zurückgespielt")
        for e in errors:
            print(f"        {e}")
        for f in bak.iterdir():
            shutil.copy2(f, ROOT / f.name)
        if active:
            run(["systemctl", "start"] + active, sudo=True)
        sys.exit(1)
    print(f"{OK}alle Skripte syntaktisch in Ordnung")
    clean_pycache()
    for marker, label in (("fvgtoggle", "FVG-Schalter"), ("computeSignal", "Konfluenz"),
                          ("kpi_backup", "Backup-Kachel")):
        has = marker in (ROOT / "hl_viz.py").read_text(errors="replace")
        print(f"{OK if has else WARN}Dashboard enthält {label}")

    # 7. Units erneuern
    print("\n7. systemd-Units")
    if have_systemd():
        bdir = unit_arg("--backup-dir")
        ldir = None
        unit = Path("/etc/systemd/system/hl-recorder.service")
        if unit.exists():
            import re
            m = re.search(r"append:(\S+)/recorder\.log", unit.read_text(errors="replace"))
            ldir = m.group(1) if m else None
        r = run([sys.executable, str(ROOT / "install.py"), "--units-only",
                 "--db", str(db)] + (["--backup-dir", bdir] if bdir else [])
                + (["--log-dir", ldir] if ldir else []))
        units = sorted((ROOT / "systemd").glob("*.service"))
        if r.returncode != 0 or not units:
            print(f"{FAIL}Units konnten nicht erzeugt werden\n{r.stderr[-400:]}")
        else:
            run(["cp"] + [str(u) for u in units] + ["/etc/systemd/system/"], sudo=True, check=True)
            run(["systemctl", "daemon-reload"], sudo=True, check=True)
            print(f"{OK}{len(units)} Units installiert: "
                  + ", ".join(u.stem for u in units))
    else:
        print(f"{WARN}übersprungen (kein systemd)")

    # 8. Starten und prüfen
    print("\n8. Start")
    if have_systemd():
        to_start = [s for s in SERVICES
                    if run(["systemctl", "is-enabled", s]).stdout.strip() in ("enabled", "enabled-runtime")]
        if not to_start:
            to_start = active
        if to_start:
            run(["systemctl", "start"] + to_start, sudo=True, check=True)
            time.sleep(6)
            all_ok = True
            for s in to_start:
                st = service_state(s)
                all_ok &= st == "active"
                print(f"{OK if st == 'active' else FAIL}{s:<16} {st}")
            if not all_ok:
                print("\n        Ein Dienst läuft nicht. Ursache:")
                for s in to_start:
                    if service_state(s) != "active":
                        r = run(["journalctl", "-u", s, "-n", "15", "--no-pager"], sudo=True)
                        print("        " + r.stdout.strip().replace("\n", "\n        ")[-900:])
        else:
            print(f"{WARN}kein Dienst eingerichtet — python3 install.py --enable")

    print("\n" + "=" * 62)
    print(" Fertig. Im Browser mit Strg+F5 neu laden (nicht nur F5).")
    print(f" Rückgängig: Dateien aus {bak.relative_to(ROOT)} zurückkopieren.")
    print("=" * 62)


if __name__ == "__main__":
    main()
