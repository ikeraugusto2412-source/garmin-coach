"""Servidor MCP (stdio) que expone garmin.db a Claude.

Ejecutar: python -m garmin_coach.server
IMPORTANTE: nada debe escribir en stdout salvo el protocolo MCP.
"""

from __future__ import annotations

import logging
import sys
from contextlib import closing
from pathlib import Path

from datetime import date, timedelta
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field
from mcp_types import ToolAnnotations

from . import coach, db, queries
from .auth import PROJECT_ROOT

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

COACH_FILE = PROJECT_ROOT / "COACH.md"
PROFILE_FILE = PROJECT_ROOT / "perfil.md"

INSTRUCTIONS = """Datos de Garmin Connect del usuario (actividades, salud diaria y métricas de rendimiento) en una base SQLite local.
Actúas como su entrenador personal. Antes de dar cualquier consejo de entrenamiento:
1) llama a get_profile (objetivos, lesiones, disponibilidad) y sigue su metodología;
2) revisa get_training_status, get_weekly_summary(8) y get_daily_health de las últimas 2 semanas.
Si los datos parecen desactualizados, ofrece ejecutar sync_garmin. Responde en español.
Cuando propongas un plan de entrenamiento, guárdalo con save_training_plan y las recomendaciones clave con
save_coach_note, para que el deportista las vea en su web; después ofrece publish_web para actualizarla.
Antes de planificar una semana nueva, revisa get_training_plan para ver qué se cumplió del plan anterior."""

mcp = MCPServer("garmin-coach", instructions=INSTRUCTIONS)
RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _read(path: Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def _ro():
    return closing(db.connect_readonly())


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=True), structured_output=False)
async def sync_garmin() -> str:
    """Descarga de Garmin Connect lo nuevo desde la última sincronización (incremental) y lo guarda en la base local.
    Tarda de segundos a un par de minutos. La primera sincronización completa debe hacerse desde la terminal."""
    from .api import RateLimited
    from .auth import AuthError, get_client
    from .sync import run_sync

    def work() -> str:
        conn = db.connect()
        try:
            if db.get_state(conn, "daily_synced_until") is None:
                return ("Aún no se ha hecho la sincronización inicial. Ejecuta en una terminal:\n"
                        f"cd {PROJECT_ROOT} && .venv/bin/python -m garmin_coach sync --full")
            client = get_client(interactive=False)
            return run_sync(client, full=False, conn=conn).text()
        except AuthError as e:
            return f"Error de autenticación: {e}"
        except RateLimited as e:
            return f"Garmin ha limitado las peticiones (429). {e}"
        finally:
            conn.close()

    return await anyio.to_thread.run_sync(work)


@mcp.tool(annotations=RO, structured_output=False)
def get_profile() -> str:
    """Perfil del deportista (objetivos, edad, experiencia, lesiones, disponibilidad) y metodología del entrenador.
    Léelo SIEMPRE antes de aconsejar."""
    profile = _read(PROFILE_FILE, "(perfil.md no existe: pide al usuario que copie perfil.example.md a perfil.md "
                                  "y lo rellene. Mientras tanto, pregunta objetivos, disponibilidad y lesiones.)")
    coach = _read(COACH_FILE, "")
    return f"# PERFIL DEL DEPORTISTA\n{profile}\n\n# METODOLOGÍA DEL ENTRENADOR\n{coach}"


@mcp.tool(annotations=RO, structured_output=False)
def get_activities(desde: str | None = None, hasta: str | None = None, tipo: str | None = None,
                   limite: int = 50) -> str:
    """Lista de actividades con métricas clave (distancia, ritmo, FC, desnivel, potencia, Training Effect, carga, % zonas FC).
    desde/hasta: fecha YYYY-MM-DD o relativo ('30d', '8w', '6m', '1y'); por defecto últimos 28 días.
    tipo: 'correr', 'gym', 'bici', 'snowboard' o un typeKey de Garmin (p. ej. 'mountain_biking')."""
    with _ro() as conn:
        return queries.get_activities(conn, desde, hasta, tipo, limite)


@mcp.tool(annotations=RO, structured_output=False)
def get_activity_detail(activity_id: int) -> str:
    """Detalle completo de una actividad: resumen, dinámica de carrera, tiempo por zona de FC, vueltas/splits y series de fuerza."""
    with _ro() as conn:
        return queries.get_activity_detail(conn, activity_id)


@mcp.tool(annotations=RO, structured_output=False)
def get_daily_health(desde: str | None = None, hasta: str | None = None) -> str:
    """Salud diaria: sueño (horas, puntuación, fases), HRV y su estado, FC en reposo, Body Battery, estrés, pasos, readiness.
    Por defecto últimos 14 días. Fechas YYYY-MM-DD o relativas ('30d')."""
    with _ro() as conn:
        return queries.get_daily_health(conn, desde, hasta)


@mcp.tool(annotations=RO, structured_output=False)
def get_training_status() -> str:
    """Estado actual: VO2max, estado de entrenamiento, readiness, carga aguda/crónica y ratio A:C (Garmin y calculado),
    balance de carga, umbral de lactato, zonas de FC, predicciones de carrera y ALERTAS de riesgo."""
    with _ro() as conn:
        return queries.get_training_status(conn)


@mcp.tool(annotations=RO, structured_output=False)
def get_weekly_summary(semanas: int = 8) -> str:
    """Resumen por semana (lunes-domingo): nº carreras, km, tiempo, ritmo, tirada larga, distribución de intensidad,
    sesiones de gym, bici, otros deportes, carga total, y medias de FC reposo, HRV y sueño."""
    with _ro() as conn:
        return queries.get_weekly_summary(conn, semanas)


@mcp.tool(annotations=RO, structured_output=False)
def get_trends(metrica: str, periodo: str = "90d") -> str:
    """Evolución de una métrica, agregada por día (≤45 d), semana (≤400 d) o mes.
    metrica: vo2max, fc_reposo, hrv, sueno_horas, sueno_puntuacion, body_battery_max, estres, pasos, readiness,
    carga_aguda, carga_cronica, resistencia, peso, pred_5k, pred_10k, pred_media, pred_maraton,
    km_carrera, ritmo_carrera, fc_carrera, potencia_carrera, cadencia_carrera, carga_entreno.
    periodo: '30d', '12w', '6m', '1y', 'todo' o una fecha de inicio YYYY-MM-DD."""
    with _ro() as conn:
        return queries.get_trends(conn, metrica, periodo)


@mcp.tool(annotations=RO, structured_output=False, description=(
    "Consulta SQL de SOLO LECTURA (SELECT/WITH) sobre garmin.db para análisis a medida. Máx. 200 filas. "
    "Cualquier escritura está bloqueada.\n\n" + queries.SCHEMA_HELP))
def query_sql(consulta: str) -> str:
    with _ro() as conn:
        return queries.query_sql(conn, consulta)


# ---------- plan y notas del entrenador (escritura) ----------

WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

SessionKind = Literal["suave", "calidad", "tirada_larga", "gimnasio", "cruzado", "descanso", "competicion"]


class Sesion(BaseModel):
    fecha: str = Field(description="Fecha YYYY-MM-DD")
    tipo: SessionKind = Field(description="suave, calidad, tirada_larga, gimnasio, cruzado, descanso o competicion")
    titulo: str = Field(description="Título corto, p. ej. 'Rodaje suave 8 km' o 'Series 6x1000'")
    descripcion: str | None = Field(None, description="Detalle: calentamiento, bloques, recuperación, ejercicios… "
                                                      "Admite markdown sencillo (listas con '- ', **negrita**)")
    distancia_km: float | None = Field(None, description="Distancia objetivo en km")
    duracion_min: float | None = Field(None, description="Duración objetivo en minutos")
    ritmo: str | None = Field(None, description="Ritmo objetivo, p. ej. '5:50-6:10' (min/km)")
    fc: str | None = Field(None, description="FC objetivo, p. ej. '<150' o '165-175'")


@mcp.tool(annotations=WRITE, structured_output=False)
def save_training_plan(titulo: str, sesiones: list[Sesion], resumen: str = "", objetivo: str = "") -> str:
    """Guarda un plan de entrenamiento (normalmente una semana) para que el deportista lo vea en su web.
    Sustituye las sesiones que hubiera en esas mismas fechas. Incluye también los días de descanso.
    resumen: explicación del plan y del porqué (markdown sencillo). objetivo: foco de la semana."""
    with closing(db.connect()) as conn:
        try:
            r = coach.save_plan(conn, titulo, [x.model_dump() for x in sesiones], resumen, objetivo)
        except coach.PlanError as e:
            return f"No se guardó el plan: {e}"
    extra = f" Sustituye {r['replaced']} sesiones anteriores en esas fechas." if r["replaced"] else ""
    return (f"Plan guardado (id {r['plan_id']}): {r['sessions']} sesiones del {r['start']} al {r['end']}.{extra} "
            "Aparecerá en la web en la próxima publicación (publish_web o la actualización diaria).")


@mcp.tool(annotations=WRITE, structured_output=False)
def save_coach_note(titulo: str, contenido: str,
                    tipo: Literal["recomendacion", "analisis", "aviso", "objetivo"] = "recomendacion",
                    fijar: bool = False) -> str:
    """Guarda una recomendación, análisis, aviso u objetivo del entrenador para mostrarlo en la web.
    contenido admite markdown sencillo. fijar=True la mantiene arriba (p. ej. pautas de ritmos o zonas)."""
    with closing(db.connect()) as conn:
        try:
            nid = coach.save_note(conn, titulo, contenido, tipo, fijar)
        except coach.PlanError as e:
            return f"No se guardó la nota: {e}"
    return f"Nota guardada (id {nid}). Aparecerá en la web en la próxima publicación."


@mcp.tool(annotations=RO, structured_output=False)
def get_training_plan(desde: str | None = None, hasta: str | None = None) -> str:
    """Plan guardado frente a lo realizado: cada sesión con su estado (hecho / pendiente / no hecho) y la actividad
    real que la cumplió. Por defecto, la semana pasada, la actual y la siguiente. Incluye las notas del entrenador."""
    today = queries._today()
    monday = today - timedelta(days=today.weekday())
    d0 = queries._parse_date(desde, monday - timedelta(days=7))
    d1 = queries._parse_date(hasta, monday + timedelta(days=13))
    with _ro() as conn:
        return coach.get_plan_text(conn, d0, d1) + "\n\nNOTAS DEL ENTRENADOR\n" + coach.notes_text(conn)


@mcp.tool(annotations=DELETE, structured_output=False)
def delete_training_plan(plan_id: int) -> str:
    """Borra un plan y todas sus sesiones (el id aparece en get_training_plan)."""
    with closing(db.connect()) as conn:
        return "Plan borrado." if coach.delete_plan(conn, plan_id) else f"No existe el plan {plan_id}."


@mcp.tool(annotations=DELETE, structured_output=False)
def delete_coach_note(nota_id: int) -> str:
    """Borra una nota del entrenador (el id aparece en get_training_plan)."""
    with closing(db.connect()) as conn:
        return "Nota borrada." if coach.delete_note(conn, nota_id) else f"No existe la nota {nota_id}."


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=True), structured_output=False)
async def publish_web() -> str:
    """Regenera la web cifrada con los datos, el plan y las notas actuales y la publica en GitHub Pages.
    Tarda unos segundos; GitHub tarda 1-2 minutos más en mostrar la nueva versión."""
    import subprocess

    def work() -> str:
        # En un proceso aparte: así se usa siempre el código actual del disco, aunque este servidor lleve
        # días abierto con una versión anterior en memoria.
        r = subprocess.run([sys.executable, "-m", "garmin_coach", "web", "--publish"], cwd=PROJECT_ROOT,
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return f"No se pudo publicar: {out[-600:]}"
        return out + "\nEl deportista la verá al abrir la web o al tocar el indicador de sincronización."

    return await anyio.to_thread.run_sync(work)


@mcp.prompt(name="entrenador", title="Entrenador personal",
            description="Activa el modo entrenador: carga perfil y metodología, y analiza tus datos antes de responder.")
def entrenador(pregunta: str = "¿Cómo voy y qué debería hacer esta semana?") -> str:
    return (
        f"{_read(COACH_FILE, '')}\n\n---\n# PERFIL DEL DEPORTISTA (perfil.md)\n{_read(PROFILE_FILE, '(vacío)')}\n\n---\n"
        f"Sigue el protocolo de análisis de arriba con las herramientas de garmin-coach y después responde a:\n\n{pregunta}"
    )


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
