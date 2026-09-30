"""Plan de entrenamiento y notas del entrenador: guardado, consulta y cruce con lo realmente hecho."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from . import db
from .queries import BIKE_TYPES, RUN_TYPES, STRENGTH_TYPES, fmt_dur, fmt_pace, table

SESSION_KINDS = {
    "suave": "Rodaje suave",
    "calidad": "Calidad",
    "tirada_larga": "Tirada larga",
    "gimnasio": "Gimnasio",
    "cruzado": "Entreno cruzado",
    "descanso": "Descanso",
    "competicion": "Competición",
}
NOTE_KINDS = {"recomendacion": "Recomendación", "analisis": "Análisis", "aviso": "Aviso", "objetivo": "Objetivo"}
RUN_KINDS = {"suave", "calidad", "tirada_larga", "competicion"}


class PlanError(ValueError):
    """Datos del plan no válidos, con un mensaje claro para Claude."""


def _check_date(s: str, field: str) -> str:
    try:
        return date.fromisoformat(s.strip()[:10]).isoformat()
    except (ValueError, AttributeError) as e:
        raise PlanError(f"'{field}' debe ser una fecha YYYY-MM-DD (recibido: {s!r})") from e


def _norm_kind(kind: str) -> str:
    k = (kind or "").strip().lower().replace(" ", "_").replace("á", "a").replace("ó", "o").replace("í", "i")
    aliases = {"rodaje": "suave", "facil": "suave", "series": "calidad", "tempo": "calidad", "umbral": "calidad",
               "larga": "tirada_larga", "fondo": "tirada_larga", "gym": "gimnasio", "fuerza": "gimnasio",
               "bici": "cruzado", "carrera": "competicion", "reposo": "descanso"}
    k = aliases.get(k, k)
    if k not in SESSION_KINDS:
        raise PlanError(f"Tipo de sesión desconocido '{kind}'. Usa uno de: {', '.join(SESSION_KINDS)}")
    return k


def save_plan(conn: sqlite3.Connection, title: str, sessions: list[dict[str, Any]], summary: str = "",
              goal: str = "") -> dict[str, Any]:
    """Guarda un plan. Sustituye las sesiones de planes anteriores en las mismas fechas."""
    if not sessions:
        raise PlanError("El plan necesita al menos una sesión.")
    rows = []
    for i, s in enumerate(sessions, 1):
        if not s.get("titulo") and not s.get("title"):
            raise PlanError(f"La sesión {i} no tiene título.")
        rows.append({
            "date": _check_date(s.get("fecha") or s.get("date") or "", f"sesiones[{i}].fecha"),
            "kind": _norm_kind(s.get("tipo") or s.get("kind") or ""),
            "title": (s.get("titulo") or s.get("title")).strip(),
            "description": (s.get("descripcion") or s.get("description") or "").strip() or None,
            "distance_km": s.get("distancia_km"),
            "duration_min": s.get("duracion_min"),
            "target_pace": s.get("ritmo") or None,
            "target_hr": s.get("fc") or None,
        })
    start = min(r["date"] for r in rows)
    end = max(r["date"] for r in rows)
    replaced = conn.execute("SELECT COUNT(*) FROM coach_sessions WHERE date BETWEEN ? AND ?", (start, end)).fetchone()[0]
    conn.execute("DELETE FROM coach_sessions WHERE date BETWEEN ? AND ?", (start, end))
    # Planes que se quedan sin sesiones desaparecen.
    conn.execute("DELETE FROM coach_plans WHERE plan_id NOT IN (SELECT DISTINCT plan_id FROM coach_sessions)")
    cur = conn.execute(
        "INSERT INTO coach_plans(title, goal, summary, start_date, end_date, created_at) VALUES (?,?,?,?,?,?)",
        (title.strip(), goal.strip() or None, summary.strip() or None, start, end, db.now_iso()),
    )
    plan_id = cur.lastrowid
    for r in rows:
        conn.execute(
            "INSERT INTO coach_sessions(plan_id, date, kind, title, description, distance_km, duration_min, "
            "target_pace, target_hr) VALUES (?,?,?,?,?,?,?,?,?)",
            (plan_id, r["date"], r["kind"], r["title"], r["description"], r["distance_km"], r["duration_min"],
             r["target_pace"], r["target_hr"]),
        )
    conn.commit()
    return {"plan_id": plan_id, "start": start, "end": end, "sessions": len(rows), "replaced": replaced}


def save_note(conn: sqlite3.Connection, title: str, body: str, kind: str = "recomendacion",
              pinned: bool = False, when: str | None = None) -> int:
    k = (kind or "recomendacion").strip().lower().replace("ó", "o").replace("á", "a")
    if k not in NOTE_KINDS:
        raise PlanError(f"Tipo de nota desconocido '{kind}'. Usa uno de: {', '.join(NOTE_KINDS)}")
    if not title.strip() or not body.strip():
        raise PlanError("La nota necesita título y contenido.")
    d = _check_date(when, "fecha") if when else date.today().isoformat()
    cur = conn.execute(
        "INSERT INTO coach_notes(date, kind, title, body, pinned, created_at) VALUES (?,?,?,?,?,?)",
        (d, k, title.strip(), body.strip(), int(pinned), db.now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> bool:
    n = conn.execute("DELETE FROM coach_sessions WHERE plan_id=?", (plan_id,)).rowcount
    n += conn.execute("DELETE FROM coach_plans WHERE plan_id=?", (plan_id,)).rowcount
    conn.commit()
    return n > 0


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    n = conn.execute("DELETE FROM coach_notes WHERE note_id=?", (note_id,)).rowcount
    conn.commit()
    return n > 0


# ---------- plan frente a lo realizado ----------

def _matches(kind: str, activity_type: str | None) -> bool:
    t = activity_type or ""
    if kind in RUN_KINDS:
        return t in RUN_TYPES
    if kind == "gimnasio":
        return t in STRENGTH_TYPES
    if kind == "cruzado":
        return t not in RUN_TYPES and t not in STRENGTH_TYPES and bool(t)
    return False


def sessions_with_status(conn: sqlite3.Connection, start: date, end: date,
                         today: date | None = None) -> list[dict[str, Any]]:
    """Sesiones del plan con su estado: hecho | pendiente | no_hecho | descanso."""
    today = today or date.today()
    acts: dict[str, list[sqlite3.Row]] = {}
    for a in conn.execute(
        "SELECT activity_id, date, type, name, distance_m, duration_s, pace_s_km, avg_hr, training_load "
        "FROM activities WHERE date BETWEEN ? AND ? ORDER BY start_local", (start.isoformat(), end.isoformat())):
        acts.setdefault(a["date"], []).append(a)
    used: set[int] = set()
    out = []
    for s in conn.execute(
        "SELECT s.*, p.title AS plan_title FROM coach_sessions s JOIN coach_plans p USING(plan_id) "
        "WHERE s.date BETWEEN ? AND ? ORDER BY s.date, s.session_id", (start.isoformat(), end.isoformat())):
        row = dict(s)
        match = next((a for a in acts.get(s["date"], [])
                      if a["activity_id"] not in used and _matches(s["kind"], a["type"])), None)
        if match:
            used.add(match["activity_id"])
            row["status"] = "hecho"
            row["done"] = {
                "activity_id": match["activity_id"], "type": match["type"], "name": match["name"],
                "km": round((match["distance_m"] or 0) / 1000, 2), "dur": match["duration_s"],
                "pace": match["pace_s_km"], "hr": match["avg_hr"], "load": match["training_load"],
            }
        elif s["kind"] == "descanso":
            row["status"] = "descanso"
        elif date.fromisoformat(s["date"]) < today:
            row["status"] = "no_hecho"
        else:
            row["status"] = "pendiente"
        out.append(row)
    return out


def get_plan_text(conn: sqlite3.Connection, desde: date, hasta: date, today: date | None = None) -> str:
    rows = sessions_with_status(conn, desde, hasta, today)
    if not rows:
        return (f"No hay sesiones planificadas entre {desde} y {hasta}. "
                "Guarda un plan con save_training_plan.")
    status_txt = {"hecho": "✓ hecho", "pendiente": "pendiente", "no_hecho": "✗ no hecho", "descanso": "descanso"}
    lines = [f"Plan {desde} → {hasta} (plan: {rows[0]['plan_title']}, id {rows[0]['plan_id']})"]
    lines.append(table(
        ["id", "fecha", "tipo", "sesión", "objetivo", "estado", "realizado"],
        [(r["session_id"], r["date"], r["kind"], r["title"],
          " ".join(x for x in [f"{r['distance_km']} km" if r["distance_km"] else "",
                               f"{r['duration_min']:.0f} min" if r["duration_min"] else "",
                               f"@{r['target_pace']}" if r["target_pace"] else "",
                               f"FC {r['target_hr']}" if r["target_hr"] else ""] if x) or "-",
          status_txt[r["status"]],
          (f"{r['done']['km']} km, {fmt_dur(r['done']['dur'])}, {fmt_pace(r['done']['pace'])}, FC {r['done']['hr']}"
           if r.get("done") else "-")) for r in rows],
    ))
    planned = [r for r in rows if r["kind"] != "descanso" and date.fromisoformat(r["date"]) < (today or date.today())]
    if planned:
        done = sum(1 for r in planned if r["status"] == "hecho")
        lines.append(f"\nCumplimiento de sesiones pasadas: {done}/{len(planned)}")
    return "\n".join(lines)


def notes_text(conn: sqlite3.Connection, limit: int = 10) -> str:
    rows = conn.execute("SELECT * FROM coach_notes ORDER BY pinned DESC, date DESC, note_id DESC LIMIT ?",
                        (limit,)).fetchall()
    if not rows:
        return "No hay notas del entrenador guardadas."
    return "\n\n".join(f"[{r['note_id']}] {r['date']} · {NOTE_KINDS.get(r['kind'], r['kind'])}"
                       f"{' · fijada' if r['pinned'] else ''}\n{r['title']}\n{r['body']}" for r in rows)


def week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)
