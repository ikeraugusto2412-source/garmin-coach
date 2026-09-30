"""Web del panel: genera una página estática con los datos CIFRADOS y la publica en GitHub Pages.

La página publicada no contiene ningún dato legible: lleva un bloque cifrado con
AES-256-GCM cuya clave se deriva de tu contraseña (PBKDF2-SHA256, 600.000 iteraciones).
El navegador lo descifra localmente cuando introduces la contraseña.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import coach, db, queries
from .auth import PROJECT_ROOT

SITE_DIR = PROJECT_ROOT / "site"
TEMPLATE = Path(__file__).with_name("web_template.html")
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 10
HEALTH_DAYS = 120
WEEKS = 26


class WebError(RuntimeError):
    """Error con un mensaje pensado para el usuario."""


# ---------- configuración ----------

def _parse_hms(s: str | None) -> int | None:
    if not s:
        return None
    parts = [int(p) for p in s.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return h * 3600 + m * 60 + sec


def race_config() -> dict[str, Any] | None:
    load_dotenv(PROJECT_ROOT / ".env")
    d = os.getenv("RACE_DATE")
    if not d:
        return None
    return {
        "name": os.getenv("RACE_NAME", "Carrera objetivo"),
        "date": d,
        "distance_km": float(os.getenv("RACE_DISTANCE_KM", "21.0975")),
        "target_s": _parse_hms(os.getenv("RACE_TARGET")),
        "target_ok_s": _parse_hms(os.getenv("RACE_TARGET_OK")),
    }


def web_password() -> str:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    pw = os.getenv("WEB_PASSWORD", "")
    if not pw:
        raise WebError("Falta la contraseña de la web. Ejecuta: python -m garmin_coach web-password")
    if len(pw) < MIN_PASSWORD_LEN:
        raise WebError(f"La contraseña de la web debe tener al menos {MIN_PASSWORD_LEN} caracteres "
                       "(la página cifrada es pública). Cámbiala con: python -m garmin_coach web-password")
    return pw


def set_web_password(password: str, env_path: Path | None = None) -> None:
    env_path = env_path or PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [ln for ln in lines if not ln.startswith("WEB_PASSWORD=")]
    lines.append(f"WEB_PASSWORD={password}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


# ---------- datos ----------

def _latest(conn: sqlite3.Connection, table: str, cols: str, not_null: str) -> dict[str, Any] | None:
    r = conn.execute(
        f"SELECT date, {cols} FROM {table} WHERE {not_null} IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


def build_payload(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    race = race_config()

    # --- carrera objetivo ---
    pred = _latest(conn, "race_predictions", "time_5k_s, time_10k_s, time_half_s, time_full_s", "time_half_s")
    if race:
        race["days_left"] = (date.fromisoformat(race["date"]) - today).days
        dist = race["distance_km"]
        if race.get("target_s"):
            race["target_pace_s_km"] = round(race["target_s"] / dist, 1)
        if pred and abs(dist - 21.0975) < 0.5:
            race["pred_s"] = pred["time_half_s"]
            race["pred_date"] = pred["date"]
            race["pred_pace_s_km"] = round(pred["time_half_s"] / dist, 1)

    # --- hoy ---
    now = {
        "readiness": _latest(conn, "training_daily", "readiness_score AS v, readiness_level AS level, "
                             "readiness_feedback AS feedback, recovery_time_h", "readiness_score"),
        "hrv": _latest(conn, "daily_health", "hrv_last_night AS v, hrv_status AS status, hrv_baseline_low AS low, "
                       "hrv_baseline_high AS high", "hrv_last_night"),
        "sleep": _latest(conn, "daily_health", "sleep_s AS v, sleep_score AS score, deep_s, rem_s", "sleep_s"),
        "rhr": _latest(conn, "daily_health", "resting_hr AS v", "resting_hr"),
        "bb": _latest(conn, "daily_health", "bb_high AS v, bb_low AS low", "bb_high"),
        "status": _latest(conn, "training_daily", "training_status AS v", "training_status"),
        "vo2max": _latest(conn, "training_daily", "vo2max AS v", "vo2max"),
        "balance": _latest(conn, "training_daily", "load_aerobic_low AS low, load_aerobic_high AS high, "
                           "load_anaerobic AS anaerobic, load_balance_feedback AS v", "load_balance_feedback"),
        "load": queries.acwr(conn, today),
    }
    last_health = conn.execute(
        "SELECT MAX(date) FROM daily_health WHERE steps IS NOT NULL OR sleep_s IS NOT NULL").fetchone()[0]

    # --- salud diaria (serie continua de días) ---
    start = today - timedelta(days=HEALTH_DAYS - 1)
    rows = {r["date"]: r for r in conn.execute(
        "SELECT h.*, t.readiness_score FROM daily_health h LEFT JOIN training_daily t USING(date) WHERE h.date >= ?",
        (start.isoformat(),))}
    days = [(start + timedelta(days=i)).isoformat() for i in range(HEALTH_DAYS)]

    def col(name: str, fn=lambda v: v):
        return [fn(rows[d][name]) if d in rows and rows[d][name] is not None else None for d in days]

    health = {
        "days": days,
        "sleep_h": col("sleep_s", lambda v: round(v / 3600, 2)),
        "sleep_score": col("sleep_score"),
        "hrv": col("hrv_last_night"),
        "hrv_low": col("hrv_baseline_low"),
        "hrv_high": col("hrv_baseline_high"),
        "rhr": col("resting_hr"),
        "bb_high": col("bb_high"),
        "bb_low": col("bb_low"),
        "stress": col("stress_avg"),
        "steps": col("steps"),
        "readiness": col("readiness_score"),
    }

    # --- rendimiento ---
    th = conn.execute("SELECT * FROM thresholds ORDER BY date DESC LIMIT 1").fetchone()
    perf = {
        "vo2": [list(r) for r in conn.execute(
            "SELECT date, vo2max FROM training_daily WHERE vo2max IS NOT NULL ORDER BY date")],
        "pred": [list(r) for r in conn.execute(
            "SELECT date, time_5k_s, time_10k_s, time_half_s, time_full_s FROM race_predictions "
            "WHERE time_half_s IS NOT NULL ORDER BY date")],
        "thresholds": dict(th) if th else None,
        "prs": [dict(r) for r in conn.execute(
            "SELECT type_id, label, activity_type, value, date FROM personal_records ORDER BY type_id")],
        "monthly": [dict(r) for r in conn.execute(
            f"""SELECT substr(date,1,7) AS month,
                       ROUND(SUM(CASE WHEN type IN ({queries._placeholders(queries.RUN_TYPES)}) THEN distance_m END)/1000.0, 1) AS run_km,
                       SUM(type IN ({queries._placeholders(queries.RUN_TYPES)})) AS runs,
                       SUM(type IN ({queries._placeholders(queries.STRENGTH_TYPES)})) AS gym,
                       ROUND(SUM(training_load)) AS load
                FROM activities GROUP BY month ORDER BY month""",
            [*queries.RUN_TYPES, *queries.RUN_TYPES, *queries.STRENGTH_TYPES])],
    }
    perf["monthly"] = _fill_months(perf["monthly"])
    if th and th["hr_zone_floors"]:
        perf["thresholds"]["hr_zone_floors"] = json.loads(th["hr_zone_floors"])

    # --- actividades (sin coordenadas GPS) ---
    laps: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM activity_laps ORDER BY activity_id, lap_index"):
        laps.setdefault(r["activity_id"], []).append([
            r["lap_index"], r["distance_m"], r["duration_s"], r["pace_s_km"], r["avg_hr"], r["max_hr"],
            r["avg_power"], r["avg_cadence"], r["elev_gain_m"]])
    sets: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM strength_sets ORDER BY activity_id, set_index"):
        sets.setdefault(r["activity_id"], []).append([r["exercise"], r["reps"], r["weight_kg"]])
    activities = []
    for a in conn.execute("SELECT * FROM activities ORDER BY start_local DESC"):
        activities.append({
            "id": a["activity_id"], "date": a["date"], "start": a["start_local"], "type": a["type"],
            "name": a["name"], "km": round((a["distance_m"] or 0) / 1000, 2), "dur": a["duration_s"],
            "pace": a["pace_s_km"], "hr": a["avg_hr"], "maxhr": a["max_hr"], "elev": a["elev_gain_m"],
            "power": a["avg_power"], "cad": a["avg_cadence"], "te": [a["te_aerobic"], a["te_anaerobic"]],
            "te_label": a["te_label"], "load": a["training_load"], "cal": a["calories"],
            "gct": a["avg_gct_ms"], "vo": a["avg_vert_osc_cm"], "stride": a["avg_stride_m"],
            "zones": [a[f"hr_z{i}_s"] for i in range(1, 6)],
            "laps": laps.get(a["activity_id"], []), "sets": sets.get(a["activity_id"], []),
        })

    # --- plan y notas del entrenador ---
    plan_from, plan_to = today - timedelta(days=56), today + timedelta(days=56)
    plan = {
        "sessions": coach.sessions_with_status(conn, plan_from, plan_to, today),
        "plans": [dict(r) for r in conn.execute(
            "SELECT plan_id, title, goal, summary, start_date, end_date, created_at FROM coach_plans "
            "WHERE end_date >= ? ORDER BY start_date", (plan_from.isoformat(),))],
        "notes": [dict(r) for r in conn.execute(
            "SELECT note_id, date, kind, title, body, pinned FROM coach_notes "
            "ORDER BY pinned DESC, date DESC, note_id DESC LIMIT 40")],
    }

    last_sync = db.get_state(conn, "last_sync")
    return {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "last_sync": last_sync,
        "last_health": last_health,
        "race": race,
        "now": now,
        "alerts": queries.risk_flags(conn, today),
        "weeks": queries.weekly_rows(conn, WEEKS),
        "health": health,
        "perf": perf,
        "activities": activities,
        "plan": plan,
    }


def _fill_months(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rellena los meses sin actividad para que el eje del tiempo sea continuo."""
    if not rows:
        return rows
    by = {r["month"]: r for r in rows}
    y, m = map(int, rows[0]["month"].split("-"))
    ey, em = map(int, rows[-1]["month"].split("-"))
    out = []
    while (y, m) <= (ey, em):
        key = f"{y:04d}-{m:02d}"
        out.append(by.get(key, {"month": key, "run_km": None, "runs": 0, "gym": 0, "load": None}))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------- cifrado ----------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def encrypt_payload(data: dict[str, Any], password: str, salt: bytes,
                    iterations: int = PBKDF2_ITERATIONS) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode(), mtime=0)
    iv = os.urandom(12)
    ct = AESGCM(derive_key(password, salt, iterations)).encrypt(iv, raw, None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": iterations, "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def decrypt_payload(env: dict[str, Any], password: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key(password, base64.b64decode(env["salt"]), env["iter"])
    raw = AESGCM(key).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None)
    return json.loads(gzip.decompress(raw))


def _salt(conn: sqlite3.Connection) -> bytes:
    """Sal estable entre publicaciones, para que 'recordar en este móvil' siga funcionando."""
    s = db.get_state(conn, "web_salt")
    if not s:
        s = _b64(os.urandom(16))
        db.set_state(conn, "web_salt", s)
        conn.commit()
    return base64.b64decode(s)


# ---------- construcción ----------

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1f3a5f"/><rect x="10" y="16" width="44" height="32" rx="5" fill="#f4f6f8"/><circle cx="16" cy="22" r="2" fill="#1f3a5f"/><circle cx="48" cy="22" r="2" fill="#1f3a5f"/><text x="32" y="42" font-family="Arial Narrow,Arial,sans-serif" font-size="18" font-weight="700" text-anchor="middle" fill="#1f3a5f">21K</text></svg>"""


def build_site(out_dir: Path | None = None, password: str | None = None,
               conn: sqlite3.Connection | None = None, today: date | None = None) -> Path:
    out = Path(out_dir or SITE_DIR)
    password = password or web_password()
    own = conn is None
    conn = conn or db.connect()
    try:
        payload = build_payload(conn, today)
        env = encrypt_payload(payload, password, _salt(conn))
    finally:
        if own:
            conn.close()

    html = TEMPLATE.read_text(encoding="utf-8").replace("__PAYLOAD__", json.dumps(env))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "manifest.webmanifest").write_text(json.dumps({
        "name": "Mi entrenamiento", "short_name": "Entreno", "start_url": ".", "display": "standalone",
        "background_color": "#eef1f4", "theme_color": "#1f3a5f",
        "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }, ensure_ascii=False), encoding="utf-8")
    return out


# ---------- publicación en GitHub Pages ----------

def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def origin_url(root: Path = PROJECT_ROOT) -> str | None:
    r = _git("remote", "get-url", "origin", cwd=root)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def pages_url(remote: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def publish_site(site_dir: Path | None = None, remote: str | None = None) -> str:
    """Sube site/ a la rama gh-pages como un único commit (sin historial de versiones antiguas)."""
    site = Path(site_dir or SITE_DIR)
    if not (site / "index.html").exists():
        raise WebError("No hay web generada. Ejecuta primero: python -m garmin_coach web")
    remote = remote or origin_url()
    if not remote:
        raise WebError("Este proyecto aún no tiene repositorio en GitHub (git remote 'origin'). "
                       "Mira la sección 'Web en el móvil' del README.")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "site"
        shutil.copytree(site, work)
        steps = [
            ("init", "-q", "-b", "gh-pages"),
            ("add", "-A"),
            ("-c", "user.name=garmin-coach", "-c", "user.email=garmin-coach@users.noreply.github.com",
             "commit", "-q", "-m", f"Actualizar web {datetime.now():%Y-%m-%d %H:%M}"),
            ("push", "-q", "--force", remote, "gh-pages"),
        ]
        for args in steps:
            r = _git(*args, cwd=work)
            if r.returncode != 0:
                raise WebError(f"git {args[0] if args[0] != '-c' else 'commit'} falló: {r.stderr.strip()[:400]}")
    return pages_url(remote) or remote
