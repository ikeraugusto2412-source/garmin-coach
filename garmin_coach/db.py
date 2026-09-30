"""Base de datos SQLite local (garmin.db)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .auth import PROJECT_ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_responses (
    endpoint   TEXT NOT NULL,
    key        TEXT NOT NULL,          -- fecha, rango o id de actividad
    fetched_at TEXT NOT NULL,
    json       TEXT NOT NULL,
    PRIMARY KEY (endpoint, key)
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id        INTEGER PRIMARY KEY,
    date               TEXT NOT NULL,  -- YYYY-MM-DD local
    start_local        TEXT,
    start_gmt          TEXT,
    type               TEXT,           -- running, strength_training, mountain_biking...
    name               TEXT,
    duration_s         REAL,
    moving_s           REAL,
    distance_m         REAL,
    avg_speed_mps      REAL,
    max_speed_mps      REAL,
    avg_gap_speed_mps  REAL,           -- ritmo ajustado por pendiente
    pace_s_km          REAL,           -- solo deportes a pie
    avg_hr             REAL,
    max_hr             REAL,
    elev_gain_m        REAL,
    elev_loss_m        REAL,
    avg_cadence        REAL,
    avg_power          REAL,
    max_power          REAL,
    norm_power         REAL,
    avg_stride_m       REAL,
    avg_gct_ms         REAL,
    avg_vert_osc_cm    REAL,
    avg_vert_ratio     REAL,
    calories           REAL,
    te_aerobic         REAL,
    te_anaerobic       REAL,
    te_label           TEXT,
    training_load      REAL,
    vo2max             REAL,
    body_battery_diff  REAL,
    hr_z1_s REAL, hr_z2_s REAL, hr_z3_s REAL, hr_z4_s REAL, hr_z5_s REAL,
    lap_count          INTEGER,
    details_synced     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(type);

CREATE TABLE IF NOT EXISTS activity_laps (
    activity_id       INTEGER NOT NULL,
    lap_index         INTEGER NOT NULL,
    distance_m        REAL,
    duration_s        REAL,
    avg_speed_mps     REAL,
    avg_gap_speed_mps REAL,
    pace_s_km         REAL,
    avg_hr            REAL,
    max_hr            REAL,
    avg_power         REAL,
    avg_cadence       REAL,
    elev_gain_m       REAL,
    elev_loss_m       REAL,
    intensity_type    TEXT,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS activity_hr_zones (
    activity_id INTEGER NOT NULL,
    zone        INTEGER NOT NULL,
    seconds     REAL,
    low_bpm     INTEGER,
    PRIMARY KEY (activity_id, zone)
);

CREATE TABLE IF NOT EXISTS strength_sets (
    activity_id  INTEGER NOT NULL,
    set_index    INTEGER NOT NULL,
    exercise     TEXT,               -- categoría más probable (BENCH_PRESS...)
    exercise_name TEXT,
    reps         INTEGER,
    weight_kg    REAL,
    duration_s   REAL,
    start_time   TEXT,
    PRIMARY KEY (activity_id, set_index)
);

CREATE TABLE IF NOT EXISTS daily_health (
    date                 TEXT PRIMARY KEY,
    steps                INTEGER,
    distance_m           REAL,
    total_kcal           REAL,
    active_kcal          REAL,
    resting_hr           REAL,
    min_hr               REAL,
    max_hr               REAL,
    stress_avg           REAL,
    stress_max           REAL,
    bb_charged           REAL,
    bb_drained           REAL,
    bb_high              REAL,
    bb_low               REAL,
    intensity_min_moderate REAL,
    intensity_min_vigorous REAL,
    sleep_s              REAL,
    deep_s               REAL,
    light_s              REAL,
    rem_s                REAL,
    awake_s              REAL,
    sleep_score          REAL,
    sleep_quality        TEXT,
    sleep_need_min       REAL,
    sleep_avg_hr         REAL,
    respiration          REAL,
    hrv_last_night       REAL,
    hrv_weekly_avg       REAL,
    hrv_5min_high        REAL,
    hrv_status           TEXT,
    hrv_baseline_low     REAL,
    hrv_baseline_high    REAL
);

CREATE TABLE IF NOT EXISTS training_daily (
    date                 TEXT PRIMARY KEY,
    readiness_score      REAL,
    readiness_level      TEXT,
    readiness_feedback   TEXT,
    recovery_time_h      REAL,
    training_status      TEXT,          -- PRODUCTIVE, MAINTAINING, DETRAINING...
    fitness_trend        INTEGER,
    acute_load           REAL,
    chronic_load         REAL,
    chronic_load_min     REAL,
    chronic_load_max     REAL,
    acwr                 REAL,
    acwr_status          TEXT,
    vo2max               REAL,
    load_aerobic_low     REAL,          -- carga mensual por foco
    load_aerobic_high    REAL,
    load_anaerobic       REAL,
    load_balance_feedback TEXT,
    endurance_score      REAL,
    hill_score           REAL
);

CREATE TABLE IF NOT EXISTS race_predictions (
    date        TEXT PRIMARY KEY,
    time_5k_s   REAL,
    time_10k_s  REAL,
    time_half_s REAL,
    time_full_s REAL
);

CREATE TABLE IF NOT EXISTS body_composition (
    date         TEXT PRIMARY KEY,
    weight_kg    REAL,
    bmi          REAL,
    body_fat_pct REAL,
    body_water_pct REAL,
    muscle_mass_kg REAL,
    bone_mass_kg REAL
);

CREATE TABLE IF NOT EXISTS thresholds (
    date              TEXT PRIMARY KEY,     -- fecha de la captura
    lt_hr             REAL,
    lt_speed_mps      REAL,
    lt_pace_s_km      REAL,
    run_ftp_w         REAL,
    power_to_weight   REAL,
    weight_kg         REAL,
    hr_max_used       REAL,
    hr_zone_floors    TEXT                  -- JSON [z1..z5]
);

CREATE TABLE IF NOT EXISTS personal_records (
    type_id       INTEGER PRIMARY KEY,
    label         TEXT,
    activity_type TEXT,
    value         REAL,
    activity_id   INTEGER,
    date          TEXT
);

-- Plan y recomendaciones que guarda el entrenador (Claude) mediante las herramientas MCP.
CREATE TABLE IF NOT EXISTS coach_plans (
    plan_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    goal        TEXT,
    summary     TEXT,               -- markdown sencillo
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_sessions (
    session_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL REFERENCES coach_plans(plan_id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- suave | calidad | tirada_larga | gimnasio | cruzado | descanso | competicion
    title        TEXT NOT NULL,
    description  TEXT,
    distance_km  REAL,
    duration_min REAL,
    target_pace  TEXT,              -- p. ej. "5:50-6:10"
    target_hr    TEXT               -- p. ej. "<150" o "150-160"
);
CREATE INDEX IF NOT EXISTS idx_coach_sessions_date ON coach_sessions(date);

CREATE TABLE IF NOT EXISTS coach_notes (
    note_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    kind       TEXT NOT NULL,       -- recomendacion | analisis | aviso | objetivo
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,       -- markdown sencillo
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    ts       TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status   TEXT NOT NULL,     -- ok | empty | error
    detail   TEXT
);
"""


def db_path() -> Path:
    return Path(os.getenv("GARMIN_DB", PROJECT_ROOT / "garmin.db")).expanduser()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def connect_readonly(path: str | Path | None = None) -> sqlite3.Connection:
    """Conexión de solo lectura a nivel de SQLite (mode=ro + query_only)."""
    p = Path(path or db_path())
    if not p.exists():
        # Crea el esquema vacío para que las consultas no fallen antes del primer sync.
        connect(p).close()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_raw(conn: sqlite3.Connection, endpoint: str, key: str, data: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_responses(endpoint, key, fetched_at, json) VALUES (?,?,?,?)",
        (endpoint, key, now_iso(), json.dumps(data, ensure_ascii=False, default=str)),
    )


def upsert(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]], pk: tuple[str, ...]) -> int:
    """Inserta o actualiza filas; los valores None no pisan datos existentes."""
    n = 0
    for row in rows:
        row = {k: v for k, v in row.items() if v is not None or k in pk}
        if not row:
            continue
        cols = list(row)
        updates = [c for c in cols if c not in pk]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        if updates:
            sql += f" ON CONFLICT({', '.join(pk)}) DO UPDATE SET " + ", ".join(
                f"{c}=excluded.{c}" for c in updates
            )
        else:
            sql += " ON CONFLICT DO NOTHING"
        conn.execute(sql, [row[c] for c in cols])
        n += 1
    return n


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?,?)", (key, value))


def log_sync(conn: sqlite3.Connection, endpoint: str, status: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sync_log(ts, endpoint, status, detail) VALUES (?,?,?,?)",
        (now_iso(), endpoint, status, (detail or "")[:500]),
    )
