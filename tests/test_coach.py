"""Tests del plan y las notas del entrenador (herramientas MCP de escritura y cruce con lo realizado)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from garmin_coach import coach, db, queries, web
from garmin_coach.api import Caller
from garmin_coach.sync import Syncer

from .conftest import TODAY, FakeGarmin, fake_activity

d = lambda n: (TODAY + timedelta(days=n)).isoformat()  # noqa: E731


def _plan():
    return [
        {"fecha": d(-2), "tipo": "suave", "titulo": "Rodaje 10 km", "distancia_km": 10, "ritmo": "5:50-6:10", "fc": "<150"},
        {"fecha": d(-1), "tipo": "gimnasio", "titulo": "Fuerza piernas"},
        {"fecha": d(-1), "tipo": "calidad", "titulo": "Series 6x1000"},
        {"fecha": d(0), "tipo": "descanso", "titulo": "Descanso"},
        {"fecha": d(1), "tipo": "tirada_larga", "titulo": "Tirada 14 km", "distancia_km": 14,
         "descripcion": "- Primeros 10 km suaves\n- Últimos 4 km a ritmo de media"},
    ]


@pytest.fixture
def with_acts(conn):
    acts = [fake_activity(1, TODAY - timedelta(days=2), km=10.2),
            fake_activity(2, TODAY - timedelta(days=1), typ="strength_training", km=0)]
    db.upsert(conn, "activities", [__import__("garmin_coach.transform", fromlist=["x"]).activity_row(a) for a in acts],
              ("activity_id",))
    return conn


def test_save_plan_and_status_matching(with_acts):
    r = coach.save_plan(with_acts, "Semana base", _plan(), "Semana de volver a la rutina.", "Base aeróbica")
    assert r["sessions"] == 5 and r["start"] == d(-2) and r["end"] == d(1)
    rows = coach.sessions_with_status(with_acts, TODAY - timedelta(days=7), TODAY + timedelta(days=7), TODAY)
    st = {x["title"]: x["status"] for x in rows}
    assert st == {"Rodaje 10 km": "hecho", "Fuerza piernas": "hecho", "Series 6x1000": "no_hecho",
                  "Descanso": "descanso", "Tirada 14 km": "pendiente"}
    done = next(x for x in rows if x["title"] == "Rodaje 10 km")["done"]
    assert done["km"] == 10.2 and done["activity_id"] == 1


def test_new_plan_replaces_overlapping_dates(with_acts):
    coach.save_plan(with_acts, "v1", _plan())
    r = coach.save_plan(with_acts, "v2", [{"fecha": d(1), "tipo": "suave", "titulo": "Cambio: rodaje corto"}])
    assert r["replaced"] == 1
    titles = [x["title"] for x in coach.sessions_with_status(with_acts, TODAY - timedelta(days=7), TODAY + timedelta(days=7), TODAY)]
    assert "Tirada 14 km" not in titles and "Cambio: rodaje corto" in titles and "Rodaje 10 km" in titles


def test_validation_errors(conn):
    with pytest.raises(coach.PlanError, match="fecha"):
        coach.save_plan(conn, "x", [{"fecha": "mañana", "tipo": "suave", "titulo": "a"}])
    with pytest.raises(coach.PlanError, match="Tipo de sesión"):
        coach.save_plan(conn, "x", [{"fecha": d(0), "tipo": "natación", "titulo": "a"}])
    with pytest.raises(coach.PlanError, match="al menos una"):
        coach.save_plan(conn, "x", [])
    assert coach._norm_kind("Series") == "calidad" and coach._norm_kind("gym") == "gimnasio"


def test_notes_and_delete(conn):
    a = coach.save_note(conn, "Ritmos de referencia", "- Suave: 6:00\n- Umbral: 5:05", "objetivo", pinned=True)
    b = coach.save_note(conn, "Duerme más", "Media de 6,4 h esta semana.", "aviso")
    txt = coach.notes_text(conn)
    assert txt.index("Ritmos de referencia") < txt.index("Duerme más")  # la fijada va primero
    assert coach.delete_note(conn, b) and not coach.delete_note(conn, 999)
    with pytest.raises(coach.PlanError):
        coach.save_note(conn, "x", "y", "cotilleo")
    pid = coach.save_plan(conn, "p", _plan())["plan_id"]
    assert coach.delete_plan(conn, pid)
    assert conn.execute("SELECT COUNT(*) FROM coach_sessions").fetchone()[0] == 0
    assert a


def test_plan_text_reports_compliance(with_acts):
    coach.save_plan(with_acts, "Semana base", _plan())
    txt = coach.get_plan_text(with_acts, TODAY - timedelta(days=7), TODAY + timedelta(days=7), TODAY)
    assert "✓ hecho" in txt and "✗ no hecho" in txt and "Cumplimiento de sesiones pasadas: 2/3" in txt


# ---------- herramientas MCP ----------

@pytest.fixture
def server_db(tmp_path, monkeypatch, activities):
    path = tmp_path / "garmin.db"
    monkeypatch.setenv("GARMIN_DB", str(path))
    monkeypatch.setattr(queries, "_today", lambda: TODAY)
    conn = db.connect(path)
    Syncer(FakeGarmin(activities), conn, caller=Caller(delay=0, sleep=lambda s: None),
           progress=lambda m: None, today=TODAY).run()
    yield conn
    conn.close()


def call(name, args=None):
    from garmin_coach.server import mcp

    res = asyncio.run(mcp.call_tool(name, args or {}))
    assert not res.is_error, res
    return res.content[0].text


def test_mcp_plan_tools_roundtrip(server_db):
    out = call("save_training_plan", {
        "titulo": "Semana 1", "objetivo": "Base", "resumen": "Volver a correr suave.",
        "sesiones": [{"fecha": d(1), "tipo": "suave", "titulo": "Rodaje 8 km", "distancia_km": 8, "ritmo": "6:00"},
                     {"fecha": d(2), "tipo": "gimnasio", "titulo": "Fuerza"}]})
    assert "Plan guardado" in out and "2 sesiones" in out
    assert "Nota guardada" in call("save_coach_note", {"titulo": "Zonas", "contenido": "Rodajes < 150 lpm", "fijar": True})
    plan = call("get_training_plan", {"desde": d(-3), "hasta": d(7)})
    assert "Rodaje 8 km" in plan and "pendiente" in plan and "Zonas" in plan
    bad = call("save_training_plan", {"titulo": "x", "sesiones": [{"fecha": "no", "tipo": "suave", "titulo": "a"}]})
    assert bad.startswith("No se guardó el plan")


def test_coach_changes_publish_web_automatically(server_db, no_real_publish):
    call("save_training_plan", {"titulo": "x", "sesiones": [{"fecha": "no", "tipo": "suave", "titulo": "a"}]})
    assert no_real_publish == []  # un guardado fallido no publica
    call("save_training_plan", {"titulo": "S", "sesiones": [{"fecha": d(1), "tipo": "suave", "titulo": "Rodaje"}]})
    call("save_coach_note", {"titulo": "Zonas", "contenido": "Rodajes < 150 lpm"})
    assert len(no_real_publish) == 2
    assert "No existe" in call("delete_coach_note", {"nota_id": 999})
    assert len(no_real_publish) == 2
    assert "Nota borrada" in call("delete_coach_note", {"nota_id": 1})
    assert len(no_real_publish) == 3


def test_publish_soon_groups_consecutive_changes(monkeypatch):
    import importlib
    import time

    from garmin_coach import server

    real = importlib.reload(server)._publish_soon  # la versión sin parchear
    runs: list[int] = []

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(server, "_run_publish", lambda: runs.append(1) or R())
    monkeypatch.setattr(server, "PUBLISH_DELAY_S", 0.05)
    real(), real(), real()
    time.sleep(0.4)
    assert runs == [1]


def test_mcp_session_kind_schema_matches_backend():
    from garmin_coach.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    enum = tools["save_training_plan"].input_schema["$defs"]["Sesion"]["properties"]["tipo"]["enum"]
    assert set(enum) == set(coach.SESSION_KINDS)
    assert tools["delete_training_plan"].annotations.destructive_hint is True


def test_web_payload_includes_plan_and_notes(server_db):
    coach.save_plan(server_db, "Semana 1", [{"fecha": d(0), "tipo": "suave", "titulo": "Rodaje de hoy"}])
    coach.save_note(server_db, "Consejo", "Bebe agua.")
    p = web.build_payload(server_db, TODAY)
    assert p["plan"]["sessions"][0]["title"] == "Rodaje de hoy"
    assert p["plan"]["notes"][0]["title"] == "Consejo"
    assert p["plan"]["plans"][0]["title"] == "Semana 1"
