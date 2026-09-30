"""Tarea diaria en macOS (launchd): sincroniza, regenera la web cifrada y la publica."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .auth import PROJECT_ROOT

LABEL = "com.garmincoach.daily"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
LOG = PROJECT_ROOT / "logs" / "daily.log"


def plist_dict(hour: int, minute: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "garmin_coach", "daily"],
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(hour: int = 8, minute: int = 0) -> str:
    LOG.parent.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # por si ya existía
    with PLIST.open("wb") as f:
        plistlib.dump(plist_dict(hour, minute), f)
    r = _launchctl("bootstrap", domain, str(PLIST))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap falló: {r.stderr.strip()}")
    return f"Tarea diaria instalada a las {hour:02d}:{minute:02d}. Registro: {LOG}"


def uninstall() -> str:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if PLIST.exists():
        PLIST.unlink()
    return "Tarea diaria eliminada."


def status() -> str:
    if not PLIST.exists():
        return "La tarea diaria no está instalada. Instálala con: python -m garmin_coach schedule install"
    with PLIST.open("rb") as f:
        cal = plistlib.load(f).get("StartCalendarInterval", {})
    loaded = _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
    last = ""
    if LOG.exists():
        lines = [ln for ln in LOG.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("[")]
        last = "\nÚltimas líneas del registro:\n" + "\n".join(lines[-6:]) if lines else ""
    return (f"Tarea diaria {'activa' if loaded else 'instalada pero NO cargada'} a las "
            f"{cal.get('Hour', 0):02d}:{cal.get('Minute', 0):02d}.{last}")
