#!/usr/bin/env python3
"""install_sim.py — erzeugt die optionale systemd-Unit fuer das Simulations-Dashboard.

    python3 install_sim.py --check      # Selbsttest ohne Aenderungen
    python3 install_sim.py --systemd    # systemd/hl-simdash.service schreiben
"""
import argparse, getpass, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UNIT = """[Unit]
Description=Handelssimulation (Liquidationscluster), Port 8766
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
ExecStart={python} {root}/hl_simdash.py --db {db} --host 0.0.0.0 --port 8766
Restart=always
RestartSec=10
StandardOutput=append:{root}/hl_simdash.log
StandardError=append:{root}/hl_simdash.log

[Install]
WantedBy=multi-user.target
"""

def find_db(explicit: str | None = None) -> Path | None:
    """
    Die Datenbank liegt nicht zwingend neben den Skripten: liegt hl_simbot als
    Unterordner in hl_liq_project, steht sie eine Ebene hoeher.
    """
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.exists() else None
    for cand in (ROOT / "hl_liq.db", ROOT.parent / "hl_liq.db"):
        if cand.exists():
            return cand
    return None


def check(db: str | None = None) -> int:
    bad = 0
    if sys.version_info < (3, 11):
        print(f"  [!] Python {sys.version_info.major}.{sys.version_info.minor}, 3.11+ noetig"); bad += 1
    else:
        print(f"  [ok] Python {sys.version_info.major}.{sys.version_info.minor}")
    for f in ("hl_botcore.py", "hl_sim.py", "hl_candles.py", "hl_simdash.py"):
        if (ROOT / f).exists():
            print(f"  [ok] {f}")
        else:
            print(f"  [!] {f} fehlt"); bad += 1
    found = find_db(db)
    print(f"  [{'ok' if found else '!'}] hl_liq.db "
          f"{found if found else 'nicht gefunden - mit --db angeben'}")
    r = subprocess.run([sys.executable, "-m", "compileall", "-q", str(ROOT)],
                       capture_output=True)
    print(f"  [{'ok' if r.returncode == 0 else '!'}] Syntaxpruefung")
    bad += r.returncode != 0
    t = ROOT / "test_logic.py"
    if t.exists():
        r = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, cwd=ROOT)
        print(f"  [{'ok' if r.returncode == 0 else '!'}] Logiktests: "
              f"{r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'keine Ausgabe'}")
        bad += r.returncode != 0
    return bad

def systemd(db: str | None = None) -> None:
    found = find_db(db)
    if not found:
        print("hl_liq.db nicht gefunden. Pfad mit --db angeben, sonst stuende "
              "in der Unit ein Pfad, den der Dienst nicht oeffnen kann.")
        raise SystemExit(2)
    d = ROOT / "systemd"; d.mkdir(exist_ok=True)
    p = d / "hl-simdash.service"
    p.write_text(UNIT.format(user=getpass.getuser(), root=ROOT,
                             python=sys.executable, db=found))
    print(f"geschrieben: {p}")
    print(f"  Arbeitsverzeichnis: {ROOT}")
    print(f"  Datenbank:          {found}\n\nInstallieren:")
    print(f"  sudo cp {p} /etc/systemd/system/")
    print( "  sudo systemctl daemon-reload && sudo systemctl enable --now hl-simdash")

if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--systemd", action="store_true")
    a.add_argument("--check", action="store_true")
    a.add_argument("--db", help="Pfad zu hl_liq.db, falls nicht daneben oder "
                                "eine Ebene hoeher")
    n = a.parse_args()
    if n.systemd: systemd(n.db)
    else: sys.exit(1 if check(n.db) else 0)
