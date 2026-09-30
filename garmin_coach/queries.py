"""Consultas y análisis sobre garmin.db. Devuelven texto compacto para Claude."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running")
BIKE_TYPES = ("cycling", "mountain_biking", "road_biking", "gravel_cycling", "e_bike_fitness",
              "e_bike_mountain", "indoor_cycling", "virtual_ride")
STRENGTH_TYPES = ("strength_training",)

def _today() -> date:
    """Fecha de referencia (sustituible en tests)."""
    return date.today()


# ---------- formato ----------


def fmt_pace(s_km: float | None) -> str:
    if not s_km:
        return "-"
    s = int(round(s_km))
    return f"{s // 60}:{s % 60:02d}/km"


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}" if h else f"{m}:{s:02d}"


def fmt_race(sec: float | None) -> str:
    if not sec:
        return "-"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _n(v: Any, nd: int = 1) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}".rstrip("0").rstrip(".") if nd else str(round(v))
    return str(v)


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = [" | ".join(headers)]
    for r in rows:
        out.append(" | ".join("-" if c is None else str(c) for c in r))
    return "\n".join(out)


def _placeholders(seq: Sequence[Any]) -> str:
    return ",".join("?" * len(seq))


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([dwmy])", s)
    if m:  # formato relativo: 30d, 8w, 6m, 1y
        n, u = int(m.group(1)), m.group(2)
        days = n * {"d": 1, "w": 7, "m": 30, "y": 365}[u]
        return _today() - timedelta(days=days)
    return date.fromisoformat(s[:10])


def _type_filter(tipo: str | None) -> tuple[str, list[str]]:
    if not tipo:
        return "", []
    t = tipo.strip().lower()
    groups = {
        "correr": RUN_TYPES, "running": RUN_TYPES, "carrera": RUN_TYPES,
        "bici": BIKE_TYPES, "cycling": BIKE_TYPES, "ciclismo": BIKE_TYPES,
        "gym": STRENGTH_TYPES, "fuerza": STRENGTH_TYPES, "gimnasio": STRENGTH_TYPES,
        "snowboard": ("resort_snowboarding", "backcountry_snowboarding"),
    }
    if t in groups:
        vals = list(groups[t])
        return f" AND type IN ({_placeholders(vals)})", vals
    return " AND type LIKE ?", [f"%{t}%"]


# ---------- actividades ----------


def get_activities(conn: sqlite3.Connection, desde: str | None = None, hasta: str | None = None,
                   tipo: str | None = None, limite: int = 50) -> str:
    d0 = _parse_date(desde, _today() - timedelta(days=28))
    d1 = _parse_date(hasta, _today())
    where, params = _type_filter(tipo)
    rows = conn.execute(
        f"""SELECT activity_id, start_local, type, name, distance_m, duration_s, pace_s_km, avg_hr, max_hr,
                   elev_gain_m, avg_power, avg_cadence, te_aerobic, te_anaerobic, training_load,
                   hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s
            FROM activities WHERE date BETWEEN ? AND ? {where}
            ORDER BY start_local DESC LIMIT ?""",
        [d0.isoformat(), d1.isoformat(), *params, max(1, min(limite, 500))],
    ).fetchall()
    if not rows:
        return f"No hay actividades entre {d0} y {d1}" + (f" de tipo '{tipo}'" if tipo else "") + "."
    out = []
    for r in rows:
        zones = [r[f"hr_z{i}_s"] or 0 for i in range(1, 6)]
        tot = sum(zones)
        zpct = "/".join(f"{round(100 * z / tot)}" for z in zones) if tot else "-"
        out.append((
            r["activity_id"], (r["start_local"] or "")[:16], r["type"],
            _n((r["distance_m"] or 0) / 1000, 2) if r["distance_m"] else "-",
            fmt_dur(r["duration_s"]), fmt_pace(r["pace_s_km"]), _n(r["avg_hr"], 0), _n(r["max_hr"], 0),
            _n(r["elev_gain_m"], 0), _n(r["avg_power"], 0),
            f"{_n(r['te_aerobic'])}/{_n(r['te_anaerobic'])}", _n(r["training_load"], 0), zpct,
        ))
    head = f"{len(rows)} actividades ({d0} → {d1}). Ritmo = ajustado por pendiente. Zonas FC = % tiempo Z1/Z2/Z3/Z4/Z5.\n"
    return head + table(
        ["id", "inicio", "tipo", "km", "tiempo", "ritmo", "FCmed", "FCmax", "desnivel+", "W", "TE aer/ana", "carga", "zonas%"],
        out,
    )


def get_activity_detail(conn: sqlite3.Connection, activity_id: int) -> str:
    a = conn.execute("SELECT * FROM activities WHERE activity_id=?", (activity_id,)).fetchone()
    if not a:
        return f"No existe la actividad {activity_id} en la base de datos."
    lines = [
        f"{a['name']} — {a['type']} — {a['start_local']}",
        f"Distancia {_n((a['distance_m'] or 0) / 1000, 2)} km · tiempo {fmt_dur(a['duration_s'])} "
        f"(en movimiento {fmt_dur(a['moving_s'])}) · ritmo {fmt_pace(a['pace_s_km'])}",
        f"FC media/máx {_n(a['avg_hr'], 0)}/{_n(a['max_hr'], 0)} · desnivel +{_n(a['elev_gain_m'], 0)}/-{_n(a['elev_loss_m'], 0)} m"
        f" · calorías {_n(a['calories'], 0)}",
        f"Potencia media/NP/máx {_n(a['avg_power'], 0)}/{_n(a['norm_power'], 0)}/{_n(a['max_power'], 0)} W · "
        f"cadencia {_n(a['avg_cadence'], 0)} · zancada {_n(a['avg_stride_m'], 2)} m · "
        f"contacto {_n(a['avg_gct_ms'], 0)} ms · osc. vertical {_n(a['avg_vert_osc_cm'])} cm",
        f"Training Effect aeróbico/anaeróbico {_n(a['te_aerobic'])}/{_n(a['te_anaerobic'])} ({a['te_label'] or '-'})"
        f" · carga {_n(a['training_load'], 0)} · VO2max {_n(a['vo2max'])} · Body Battery {_n(a['body_battery_diff'], 0)}",
    ]
    zones = conn.execute(
        "SELECT zone, seconds, low_bpm FROM activity_hr_zones WHERE activity_id=? ORDER BY zone", (activity_id,)
    ).fetchall()
    if zones:
        tot = sum(z["seconds"] or 0 for z in zones) or 1
        lines.append("Zonas FC: " + " · ".join(
            f"Z{z['zone']}(≥{z['low_bpm']}) {fmt_dur(z['seconds'])} {round(100 * (z['seconds'] or 0) / tot)}%"
            for z in zones))
    laps = conn.execute(
        "SELECT * FROM activity_laps WHERE activity_id=? ORDER BY lap_index", (activity_id,)
    ).fetchall()
    if laps:
        lines.append(f"\nVueltas ({len(laps)}):")
        lines.append(table(
            ["#", "km", "tiempo", "ritmo", "ritmo GAP", "FCmed", "FCmax", "W", "cad", "desn+", "tipo"],
            [(l["lap_index"], _n((l["distance_m"] or 0) / 1000, 2), fmt_dur(l["duration_s"]), fmt_pace(l["pace_s_km"]),
              fmt_pace(1000 / l["avg_gap_speed_mps"]) if l["avg_gap_speed_mps"] else "-",
              _n(l["avg_hr"], 0), _n(l["max_hr"], 0), _n(l["avg_power"], 0), _n(l["avg_cadence"], 0),
              _n(l["elev_gain_m"], 0), l["intensity_type"] or "") for l in laps[:60]],
        ))
    sets = conn.execute(
        "SELECT exercise, reps, weight_kg FROM strength_sets WHERE activity_id=? ORDER BY set_index", (activity_id,)
    ).fetchall()
    if sets:
        lines.append("\nSeries de fuerza:")
        grouped: dict[str, list[str]] = {}
        for s in sets:
            grouped.setdefault(s["exercise"] or "?", []).append(
                f"{s['reps'] or '?'}" + (f"×{_n(s['weight_kg'])}kg" if s["weight_kg"] else ""))
        lines += [f"  {ex}: {', '.join(v)}" for ex, v in grouped.items()]
    if not a["details_synced"]:
        lines.append("\n(Detalle pendiente de sincronizar: vueltas/zonas pueden faltar.)")
    return "\n".join(lines)


# ---------- salud diaria ----------


def get_daily_health(conn: sqlite3.Connection, desde: str | None = None, hasta: str | None = None) -> str:
    d0 = _parse_date(desde, _today() - timedelta(days=14))
    d1 = _parse_date(hasta, _today())
    rows = conn.execute(
        """SELECT h.*, t.readiness_score, t.readiness_level FROM daily_health h
           LEFT JOIN training_daily t USING(date)
           WHERE h.date BETWEEN ? AND ? ORDER BY h.date""",
        (d0.isoformat(), d1.isoformat()),
    ).fetchall()
    if not rows:
        return f"No hay datos de salud entre {d0} y {d1}."
    out = []
    for r in rows:
        sleep_h = r["sleep_s"] / 3600 if r["sleep_s"] else None
        phases = (
            f"{round(100 * (r['deep_s'] or 0) / r['sleep_s'])}/{round(100 * (r['rem_s'] or 0) / r['sleep_s'])}"
            if r["sleep_s"] else "-"
        )
        out.append((
            r["date"], _n(sleep_h, 1), _n(r["sleep_score"], 0), phases,
            _n(r["hrv_last_night"], 0), r["hrv_status"] or "-", _n(r["resting_hr"], 0),
            f"{_n(r['bb_high'], 0)}/{_n(r['bb_low'], 0)}", _n(r["stress_avg"], 0), _n(r["steps"], 0),
            _n(r["readiness_score"], 0),
        ))
    base = conn.execute(
        "SELECT hrv_baseline_low, hrv_baseline_high FROM daily_health WHERE hrv_baseline_low IS NOT NULL "
        "AND date <= ? ORDER BY date DESC LIMIT 1", (d1.isoformat(),)
    ).fetchone()
    head = f"Salud diaria {d0} → {d1}. Sueño en horas; fases = % profundo/REM."
    if base:
        head += f" Rango HRV equilibrado: {_n(base[0], 0)}-{_n(base[1], 0)} ms."
    return head + "\n" + table(
        ["fecha", "sueño h", "punt.", "prof/REM%", "HRV", "estado HRV", "FCrep", "BB máx/mín", "estrés", "pasos", "readiness"],
        out,
    )


# ---------- carga y riesgos ----------


def daily_loads(conn: sqlite3.Connection, end: date, days: int) -> list[float]:
    start = end - timedelta(days=days - 1)
    rows = dict(conn.execute(
        "SELECT date, SUM(training_load) FROM activities WHERE date BETWEEN ? AND ? GROUP BY date",
        (start.isoformat(), end.isoformat()),
    ).fetchall())
    return [float(rows.get((start + timedelta(days=i)).isoformat()) or 0) for i in range(days)]


def acwr(conn: sqlite3.Connection, end: date | None = None) -> dict[str, float | None]:
    """Ratio agudo:crónico con la carga de cada actividad (media diaria 7 d / media diaria 28 d)."""
    end = end or _today()
    loads = daily_loads(conn, end, 28)
    acute = sum(loads[-7:]) / 7
    chronic = sum(loads) / 28
    return {
        "acute_7d": round(sum(loads[-7:]), 0),
        "chronic_28d_weekly": round(chronic * 7, 0),
        "acwr": round(acute / chronic, 2) if chronic > 0 else None,
    }


def risk_flags(conn: sqlite3.Connection, end: date | None = None) -> list[str]:
    end = end or _today()
    flags: list[str] = []

    r = acwr(conn, end)
    if r["acwr"] is not None:
        if r["acwr"] > 1.5:
            flags.append(f"🔴 Carga aguda:crónica {r['acwr']} (>1,5): subida brusca, riesgo de lesión elevado.")
        elif r["acwr"] > 1.3:
            flags.append(f"🟠 Carga aguda:crónica {r['acwr']} (1,3-1,5): progresión agresiva, vigilar.")
        elif r["acwr"] < 0.8:
            flags.append(f"🟡 Carga aguda:crónica {r['acwr']} (<0,8): carga baja, se pierde forma.")

    h = conn.execute(
        "SELECT date, hrv_last_night, hrv_baseline_low FROM daily_health WHERE hrv_last_night IS NOT NULL "
        "AND date BETWEEN ? AND ? ORDER BY date", ((end - timedelta(days=13)).isoformat(), end.isoformat()),
    ).fetchall()
    if len(h) >= 3:
        last3 = h[-3:]
        below = [x for x in last3 if x["hrv_baseline_low"] and x["hrv_last_night"] < x["hrv_baseline_low"]]
        falling = last3[0]["hrv_last_night"] > last3[1]["hrv_last_night"] > last3[2]["hrv_last_night"]
        avg = sum(x["hrv_last_night"] for x in h) / len(h)
        if len(below) >= 2:
            flags.append(f"🔴 HRV por debajo de tu rango base {len(below)} de las últimas 3 noches.")
        elif falling and last3[2]["hrv_last_night"] < 0.9 * avg:
            flags.append(f"🟠 HRV cayendo 3 noches seguidas (última {_n(last3[2]['hrv_last_night'], 0)} ms, "
                         f"media 14 d {_n(avg, 0)} ms).")

    s = conn.execute(
        "SELECT sleep_s, sleep_score FROM daily_health WHERE sleep_s IS NOT NULL AND date BETWEEN ? AND ?",
        ((end - timedelta(days=6)).isoformat(), end.isoformat()),
    ).fetchall()
    if len(s) >= 3:
        avg_h = sum(x["sleep_s"] for x in s) / len(s) / 3600
        short = sum(1 for x in s if x["sleep_s"] < 6.5 * 3600)
        if avg_h < 7 or short >= 3:
            flags.append(f"🟠 Sueño acumulado bajo: media {avg_h:.1f} h en {len(s)} noches, {short} por debajo de 6,5 h.")
        scores = [x["sleep_score"] for x in s if x["sleep_score"]]
        if scores and sum(scores) / len(scores) < 60:
            flags.append(f"🟠 Puntuación de sueño media {sum(scores) / len(scores):.0f} (<60).")

    rhr = conn.execute(
        "SELECT date, resting_hr FROM daily_health WHERE resting_hr IS NOT NULL AND date BETWEEN ? AND ? ORDER BY date",
        ((end - timedelta(days=27)).isoformat(), end.isoformat()),
    ).fetchall()
    if len(rhr) >= 10:
        base = sum(x["resting_hr"] for x in rhr[:-3]) / len(rhr[:-3])
        recent = sum(x["resting_hr"] for x in rhr[-3:]) / 3
        if recent > base + 5:
            flags.append(f"🟠 FC en reposo elevada: {recent:.0f} lpm (últimos 3 días) frente a {base:.0f} de base.")

    z = conn.execute(
        f"""SELECT SUM(hr_z1_s), SUM(hr_z2_s), SUM(hr_z3_s), SUM(hr_z4_s), SUM(hr_z5_s) FROM activities
            WHERE type IN ({_placeholders(RUN_TYPES)}) AND date BETWEEN ? AND ?""",
        [*RUN_TYPES, (end - timedelta(days=27)).isoformat(), end.isoformat()],
    ).fetchone()
    tot = sum(v or 0 for v in z)
    if tot > 3600:
        easy = ((z[0] or 0) + (z[1] or 0)) / tot
        hard = ((z[3] or 0) + (z[4] or 0)) / tot
        if hard > 0.4:
            flags.append(f"🟠 Distribución de intensidad: {hard:.0%} del tiempo de carrera en Z4-Z5 y solo "
                         f"{easy:.0%} en Z1-Z2 (últimas 4 semanas). Falta base aeróbica suave o las zonas están mal configuradas.")

    last_health = conn.execute("SELECT MAX(date) FROM daily_health WHERE steps IS NOT NULL OR sleep_s IS NOT NULL").fetchone()[0]
    if last_health and (end - date.fromisoformat(last_health)).days >= 2:
        flags.append(f"ℹ️ Sin datos de salud desde {last_health}: ¿reloj sin llevar o sin sincronizar?")
    return flags


def _latest(conn: sqlite3.Connection, table_: str, col: str) -> tuple[Any, str | None]:
    r = conn.execute(f"SELECT {col}, date FROM {table_} WHERE {col} IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    return (r[0], r[1]) if r else (None, None)


def get_training_status(conn: sqlite3.Connection) -> str:
    today = _today()
    lines = ["ESTADO DE ENTRENAMIENTO (valores más recientes, con su fecha)"]
    for label, col, fmt in [
        ("VO2max", "vo2max", lambda v: _n(v)),
        ("Estado Garmin", "training_status", str),
        ("Training Readiness", "readiness_score", lambda v: _n(v, 0)),
        ("Nivel readiness", "readiness_level", str),
        ("Tiempo de recuperación (h)", "recovery_time_h", lambda v: _n(v)),
        ("Carga aguda Garmin", "acute_load", lambda v: _n(v, 0)),
        ("Carga crónica Garmin", "chronic_load", lambda v: _n(v, 0)),
        ("Ratio A:C Garmin", "acwr", lambda v: _n(v, 2)),
        ("Puntuación de resistencia", "endurance_score", lambda v: _n(v, 0)),
        ("Puntuación de subidas", "hill_score", lambda v: _n(v, 0)),
    ]:
        v, d = _latest(conn, "training_daily", col)
        if v is not None:
            lines.append(f"  {label}: {fmt(v)} ({d})")

    bal = conn.execute(
        "SELECT date, load_aerobic_low, load_aerobic_high, load_anaerobic, load_balance_feedback FROM training_daily "
        "WHERE load_balance_feedback IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    if bal:
        lines.append(f"  Balance de carga 4 sem. ({bal['date']}): aeróbica baja {_n(bal[1], 0)}, aeróbica alta "
                     f"{_n(bal[2], 0)}, anaeróbica {_n(bal[3], 0)} → {bal[4]}")

    r = acwr(conn, today)
    lines.append(f"  Carga calculada (suma de cargas de actividad): 7 d = {_n(r['acute_7d'], 0)}, "
                 f"media semanal 28 d = {_n(r['chronic_28d_weekly'], 0)}, ratio A:C = {_n(r['acwr'], 2)}")

    th = conn.execute("SELECT * FROM thresholds ORDER BY date DESC LIMIT 1").fetchone()
    if th:
        floors = json.loads(th["hr_zone_floors"]) if th["hr_zone_floors"] else None
        lines.append(f"  Umbral de lactato: {_n(th['lt_hr'], 0)} lpm a {fmt_pace(th['lt_pace_s_km'])} · "
                     f"FTP carrera {_n(th['run_ftp_w'], 0)} W ({_n(th['power_to_weight'], 2)} W/kg, {_n(th['weight_kg'])} kg)")
        if floors:
            lines.append(f"  Zonas FC configuradas en Garmin (FCmáx {_n(th['hr_max_used'], 0)}): "
                         + " · ".join(f"Z{i + 1}≥{f}" for i, f in enumerate(floors)))

    rp = conn.execute("SELECT * FROM race_predictions ORDER BY date DESC LIMIT 1").fetchone()
    if rp:
        old = conn.execute("SELECT * FROM race_predictions WHERE date <= ? ORDER BY date DESC LIMIT 1",
                           ((date.fromisoformat(rp["date"]) - timedelta(days=28)).isoformat(),)).fetchone()
        def cmp(k: str) -> str:
            s = fmt_race(rp[k])
            if old and old[k] and rp[k]:
                diff = rp[k] - old[k]
                s += f" ({'+' if diff > 0 else ''}{int(diff)} s vs 4 sem.)"
            return s
        lines.append(f"  Predicciones ({rp['date']}): 5K {cmp('time_5k_s')} · 10K {cmp('time_10k_s')} · "
                     f"media {cmp('time_half_s')} · maratón {cmp('time_full_s')}")

    flags = risk_flags(conn, today)
    lines.append("\nALERTAS:" if flags else "\nALERTAS: ninguna detectada.")
    lines += [f"  {f}" for f in flags]

    last_sync = conn.execute("SELECT value FROM sync_state WHERE key='last_sync'").fetchone()
    lines.append(f"\nÚltima sincronización: {last_sync[0] if last_sync else 'nunca'}")
    return "\n".join(lines)


# ---------- resumen semanal ----------


def weekly_rows(conn: sqlite3.Connection, semanas: int = 8) -> list[dict[str, Any]]:
    """Datos numéricos por semana (lunes-domingo), de la más antigua a la actual."""
    semanas = max(1, min(semanas, 260))
    today = _today()
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=semanas - 1)
    out = []
    for i in range(semanas):
        ws = first + timedelta(weeks=i)
        we = ws + timedelta(days=6)
        p = (ws.isoformat(), we.isoformat())
        acts = conn.execute("SELECT * FROM activities WHERE date BETWEEN ? AND ?", p).fetchall()
        runs = [a for a in acts if a["type"] in RUN_TYPES]
        bikes = [a for a in acts if a["type"] in BIKE_TYPES]
        grouped = {a["activity_id"] for a in runs + bikes} | {
            a["activity_id"] for a in acts if a["type"] in STRENGTH_TYPES}
        run_km = sum((a["distance_m"] or 0) for a in runs) / 1000
        run_t = sum((a["duration_s"] or 0) for a in runs)
        zs = [sum((a[f"hr_z{z}_s"] or 0) for a in runs) for z in range(1, 6)]
        h = conn.execute(
            "SELECT AVG(resting_hr), AVG(hrv_last_night), AVG(sleep_s)/3600.0, AVG(sleep_score) FROM daily_health "
            "WHERE date BETWEEN ? AND ?", p).fetchone()
        out.append({
            "week": ws.isoformat(),
            "runs": len(runs),
            "run_km": round(run_km, 2),
            "run_s": round(run_t),
            "pace_s_km": round(run_t / run_km, 1) if run_km else None,
            "long_km": round(max(((a["distance_m"] or 0) / 1000 for a in runs), default=0), 2),
            "z_easy_s": round(zs[0] + zs[1]),
            "z_mid_s": round(zs[2]),
            "z_hard_s": round(zs[3] + zs[4]),
            "gym": sum(1 for a in acts if a["type"] in STRENGTH_TYPES),
            "bikes": len(bikes),
            "bike_km": round(sum((b["distance_m"] or 0) for b in bikes) / 1000, 1),
            "bike_elev": round(sum((b["elev_gain_m"] or 0) for b in bikes)),
            "others": sorted({a["type"] for a in acts if a["activity_id"] not in grouped}),
            "load": round(sum((a["training_load"] or 0) for a in acts)),
            "rhr": round(h[0], 1) if h[0] else None,
            "hrv": round(h[1], 1) if h[1] else None,
            "sleep_h": round(h[2], 2) if h[2] else None,
            "sleep_score": round(h[3]) if h[3] else None,
        })
    return out


def get_weekly_summary(conn: sqlite3.Connection, semanas: int = 8) -> str:
    semanas = max(1, min(semanas, 104))
    out = []
    for w in weekly_rows(conn, semanas):
        zt = w["z_easy_s"] + w["z_mid_s"] + w["z_hard_s"]
        dist = (f"{round(100 * w['z_easy_s'] / zt)}/{round(100 * w['z_mid_s'] / zt)}/{round(100 * w['z_hard_s'] / zt)}"
                if zt else "-")
        out.append((
            w["week"], w["runs"], _n(float(w["run_km"]), 1), fmt_dur(w["run_s"]) if w["run_s"] else "-",
            fmt_pace(w["pace_s_km"]), _n(float(w["long_km"]), 1) if w["long_km"] else "-", dist, w["gym"],
            f"{w['bikes']}/{_n(w['bike_km'], 0)}km/{w['bike_elev']}m" if w["bikes"] else "-",
            ",".join(w["others"]) or "-",
            w["load"], _n(w["rhr"], 0), _n(w["hrv"], 0), _n(w["sleep_h"], 1), _n(w["sleep_score"], 0),
        ))
    head = (f"Resumen semanal (lunes a domingo), últimas {semanas} semanas. Ritmo medio sin ajustar; "
            "intensidad carrera = % tiempo Z1-2 / Z3 / Z4-5. Bici = salidas/km/desnivel.\n")
    return head + table(
        ["semana", "carreras", "km", "tiempo", "ritmo", "tirada larga", "int. %", "gym", "bici", "otros",
         "carga", "FCrep", "HRV", "sueño h", "punt. sueño"], out)


# ---------- tendencias ----------

METRICS: dict[str, tuple[str, str, str]] = {
    # nombre: (tabla, expresión, agregación)
    "vo2max": ("training_daily", "vo2max", "AVG"),
    "fc_reposo": ("daily_health", "resting_hr", "AVG"),
    "hrv": ("daily_health", "hrv_last_night", "AVG"),
    "sueno_horas": ("daily_health", "sleep_s/3600.0", "AVG"),
    "sueno_puntuacion": ("daily_health", "sleep_score", "AVG"),
    "body_battery_max": ("daily_health", "bb_high", "AVG"),
    "estres": ("daily_health", "stress_avg", "AVG"),
    "pasos": ("daily_health", "steps", "AVG"),
    "readiness": ("training_daily", "readiness_score", "AVG"),
    "carga_aguda": ("training_daily", "acute_load", "AVG"),
    "carga_cronica": ("training_daily", "chronic_load", "AVG"),
    "resistencia": ("training_daily", "endurance_score", "AVG"),
    "peso": ("body_composition", "weight_kg", "AVG"),
    "pred_5k": ("race_predictions", "time_5k_s", "AVG"),
    "pred_10k": ("race_predictions", "time_10k_s", "AVG"),
    "pred_media": ("race_predictions", "time_half_s", "AVG"),
    "pred_maraton": ("race_predictions", "time_full_s", "AVG"),
    "km_carrera": ("activities", "distance_m/1000.0", "SUM"),
    "ritmo_carrera": ("activities", "pace_s_km", "AVG"),
    "fc_carrera": ("activities", "avg_hr", "AVG"),
    "potencia_carrera": ("activities", "avg_power", "AVG"),
    "cadencia_carrera": ("activities", "avg_cadence", "AVG"),
    "carga_entreno": ("activities", "training_load", "SUM"),
}
_RUN_ONLY = {"km_carrera", "ritmo_carrera", "fc_carrera", "potencia_carrera", "cadencia_carrera"}


def get_trends(conn: sqlite3.Connection, metrica: str, periodo: str = "90d") -> str:
    key = metrica.strip().lower().replace(" ", "_").replace("ñ", "n")
    if key not in METRICS:
        return f"Métrica desconocida '{metrica}'. Disponibles: {', '.join(METRICS)}"
    tbl, expr, agg = METRICS[key]
    if periodo.strip().lower() in ("todo", "all"):
        start = date(2000, 1, 1)
    else:
        start = _parse_date(periodo, _today() - timedelta(days=90))
    span = (_today() - start).days
    if span <= 45:
        bucket, blabel = "date", "día"
    elif span <= 400:
        bucket, blabel = "date(date, '-' || ((strftime('%w', date) + 6) % 7) || ' days')", "semana"
    else:
        bucket, blabel = "substr(date, 1, 7)", "mes"
    where = f" AND type IN ({_placeholders(RUN_TYPES)})" if key in _RUN_ONLY else ""
    params: list[Any] = [start.isoformat()] + (list(RUN_TYPES) if where else [])
    rows = conn.execute(
        f"SELECT {bucket} AS b, {agg}({expr}) AS v, COUNT({expr}) AS n FROM {tbl} "
        f"WHERE date >= ? AND {expr} IS NOT NULL{where} GROUP BY b ORDER BY b",
        params,
    ).fetchall()
    if not rows:
        return f"Sin datos de '{key}' desde {start}."
    is_time = key.startswith("pred_")
    is_pace = key == "ritmo_carrera"
    f = fmt_race if is_time else (fmt_pace if is_pace else (lambda v: _n(v, 2 if key in ("vo2max", "sueno_horas") else 1)))
    vals = [r["v"] for r in rows]
    first, last = vals[0], vals[-1]
    change = last - first
    stats = (f"{key} por {blabel} desde {start}: inicio {f(first)} → último {f(last)} "
             f"(cambio {'+' if change > 0 else ''}{_n(change, 1)}{' s' if is_time or is_pace else ''}); "
             f"mín {f(min(vals))}, máx {f(max(vals))}. ")
    if len(rows) > 60:  # compacta: primeras y últimas filas
        rows = rows[:10] + rows[-40:]
        stats += "(Se muestran los 10 primeros y 40 últimos periodos.)"
    return stats + "\n" + table([blabel, key, "n"], [(r["b"], f(r["v"]), r["n"]) for r in rows])


# ---------- SQL de solo lectura ----------

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
# Algunas versiones de SQLite reportan también RECURSIVE en los CTE recursivos.
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def _authorizer(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def query_sql(conn: sqlite3.Connection, consulta: str, max_filas: int = 200) -> str:
    sql = consulta.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", sql):
        return "Solo se permiten consultas SELECT (o WITH … SELECT)."
    if ";" in sql:
        return "Solo se permite una sentencia por consulta."
    conn.set_authorizer(_authorizer)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_filas + 1)
    except sqlite3.DatabaseError as e:
        return f"Error SQL: {e}"
    finally:
        conn.set_authorizer(None)
    more = len(rows) > max_filas
    rows = rows[:max_filas]

    def cell(v: Any) -> Any:
        if isinstance(v, float):
            return round(v, 3)
        if isinstance(v, str) and len(v) > 300:
            return v[:300] + "…"
        return v

    txt = table(cols, [[cell(v) for v in r] for r in rows])
    return f"{len(rows)} filas" + (f" (truncado a {max_filas})" if more else "") + "\n" + txt


SCHEMA_HELP = """Tablas principales (fechas YYYY-MM-DD locales):
- activities(activity_id, date, start_local, type, name, duration_s, moving_s, distance_m, avg_speed_mps, avg_gap_speed_mps,
  pace_s_km, avg_hr, max_hr, elev_gain_m, avg_cadence, avg_power, norm_power, avg_gct_ms, avg_vert_osc_cm, calories,
  te_aerobic, te_anaerobic, te_label, training_load, vo2max, hr_z1_s..hr_z5_s, lap_count)
- activity_laps(activity_id, lap_index, distance_m, duration_s, pace_s_km, avg_gap_speed_mps, avg_hr, max_hr, avg_power, avg_cadence, elev_gain_m, intensity_type)
- activity_hr_zones(activity_id, zone, seconds, low_bpm)
- strength_sets(activity_id, set_index, exercise, reps, weight_kg, duration_s)
- daily_health(date, steps, resting_hr, stress_avg, bb_high, bb_low, bb_charged, bb_drained, sleep_s, deep_s, light_s, rem_s,
  awake_s, sleep_score, sleep_quality, respiration, hrv_last_night, hrv_weekly_avg, hrv_status, hrv_baseline_low/high, intensity_min_*)
- training_daily(date, readiness_score, readiness_level, recovery_time_h, training_status, acute_load, chronic_load, acwr,
  vo2max, load_aerobic_low/high, load_anaerobic, load_balance_feedback, endurance_score, hill_score)
- race_predictions(date, time_5k_s, time_10k_s, time_half_s, time_full_s)
- thresholds(date, lt_hr, lt_pace_s_km, run_ftp_w, hr_max_used, hr_zone_floors)
- body_composition(date, weight_kg, ...), personal_records(type_id, label, value, activity_id, date)
- raw_responses(endpoint, key, fetched_at, json) — JSON crudo; usa json_extract(json, '$.campo')
Tipos de actividad frecuentes: running, strength_training, cycling, mountain_biking, e_bike_fitness, resort_snowboarding, walking."""
