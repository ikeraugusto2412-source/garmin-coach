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


DEFAULT_TIMES = "7:30,9:30,15:00,21:30"


def parse_times(spec: str) -> list[tuple[int, int]]:
    """'7:30,9:30,21' → [(7, 30), (9, 30), (21, 0)]"""
    out = []
    for part in spec.split(","):
        h, _, m = part.strip().partition(":")
        hour, minute = int(h), int(m or 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"Hora no válida: {part!r}")
        out.append((hour, minute))
    if not out:
        raise ValueError("Indica al menos una hora.")
    return sorted(set(out))


def plist_dict(times: list[tuple[int, int]]) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "garmin_coach", "daily"],
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times],
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(spec: str = DEFAULT_TIMES) -> str:
    times = parse_times(spec)
    LOG.parent.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # por si ya existía
    with PLIST.open("wb") as f:
        plistlib.dump(plist_dict(times), f)
    r = _launchctl("bootstrap", domain, str(PLIST))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap falló: {r.stderr.strip()}")
    return f"Actualización automática instalada a las {_fmt(times)}. Registro: {LOG}"


def _fmt(times: list[tuple[int, int]]) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in times)


def uninstall() -> str:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if PLIST.exists():
        PLIST.unlink()
    return "Tarea diaria eliminada."


def status() -> str:
    if not PLIST.exists():
        return "La tarea diaria no está instalada. Instálala con: python -m garmin_coach schedule install"
    with PLIST.open("rb") as f:
        cal = plistlib.load(f).get("StartCalendarInterval", [])
    cal = [cal] if isinstance(cal, dict) else cal
    times = [(c.get("Hour", 0), c.get("Minute", 0)) for c in cal]
    loaded = _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
    last = ""
    if LOG.exists():
        lines = [ln for ln in LOG.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("[")]
        last = "\nÚltimas líneas del registro:\n" + "\n".join(lines[-6:]) if lines else ""
    return (f"Actualización automática {'activa' if loaded else 'instalada pero NO cargada'} a las "
            f"{_fmt(times)}.{last}")
