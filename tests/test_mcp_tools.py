"""Tests de las herramientas MCP y del análisis, sobre una base de datos simulada."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from garmin_coach import db, queries
from garmin_coach.sync import Syncer

from .conftest import TODAY, FakeGarmin, fake_activity


@pytest.fixture
def seeded(tmp_path, monkeypatch, activities):
    path = tmp_path / "garmin.db"
    monkeypatch.setenv("GARMIN_DB", str(path))
    monkeypatch.setattr(queries, "_today", lambda: TODAY)
    conn = db.connect(path)
    from garmin_coach.api import Caller

    Syncer(FakeGarmin(activities), conn, caller=Caller(delay=0, sleep=lambda s: None),
           progress=lambda m: None, today=TODAY).run()
    yield conn
    conn.close()


def call(name: str, args: dict | None = None) -> str:
    from garmin_coach.server import mcp

    res = asyncio.run(mcp.call_tool(name, args or {}))
    assert not res.is_error, res
    return res.content[0].text


def test_tools_are_registered_with_readonly_hints():
    from garmin_coach.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    expected = {"sync_garmin", "get_profile", "get_activities", "get_activity_detail", "get_daily_health",
                "get_training_status", "get_weekly_summary", "get_trends", "query_sql"}
    assert expected <= set(tools)
    assert tools["query_sql"].annotations.read_only_hint is True
    assert "activities(" in tools["query_sql"].description


def test_get_activities_filters_by_type(seeded):
    out = call("get_activities", {"desde": "2026-08-01", "tipo": "correr"})
    assert "running" in out and "strength_training" not in out
    assert "5:30/km" in out
    gym = call("get_activities", {"desde": "30d", "tipo": "gym"})
    assert "strength_training" in gym and "running" not in gym.split("\n", 2)[2]


def test_get_activity_detail_includes_laps_zones_and_sets(seeded):
    aid = seeded.execute("SELECT activity_id FROM activities WHERE type='strength_training'").fetchone()[0]
    out = call("get_activity_detail", {"activity_id": aid})
    assert "SQUAT: 10×60kg" in out and "Zonas FC" in out
    run_id = seeded.execute("SELECT activity_id FROM activities WHERE type='running' LIMIT 1").fetchone()[0]
    assert "Vueltas (2)" in call("get_activity_detail", {"activity_id": run_id})
    assert "No existe" in call("get_activity_detail", {"activity_id": 1})


def test_daily_health_and_weekly_summary(seeded):
    out = call("get_daily_health", {"desde": "2026-09-25", "hasta": "2026-09-30"})
    assert out.count("\n2026-09-") == 6 and "Rango HRV equilibrado: 58-75" in out
    weekly = call("get_weekly_summary", {"semanas": 4})
    lines = weekly.strip().split("\n")
    assert len(lines) == 2 + 4
    assert "| 3 | 30 |" in weekly  # 3 carreras de 10 km en una semana completa


def test_training_status_and_trends(seeded):
    out = call("get_training_status")
    assert "VO2max: 44.6" in out and "Umbral de lactato: 172 lpm a 5:03/km" in out
    assert "Predicciones" in out
    tr = call("get_trends", {"metrica": "vo2max", "periodo": "30d"})
    assert tr.startswith("vo2max por día")
    assert "desconocida" in call("get_trends", {"metrica": "nope"})
    assert "por mes" in call("get_trends", {"metrica": "km_carrera", "periodo": "todo"})


def test_query_sql_allows_select_and_blocks_writes(seeded):
    ok = call("query_sql", {"consulta": "SELECT type, COUNT(*) n FROM activities GROUP BY type ORDER BY n DESC"})
    assert ok.startswith("3 filas") and "running | 18" in ok
    cte = call("query_sql", {"consulta": "WITH x AS (SELECT 1 AS a) SELECT a FROM x"})
    assert "1 filas" in cte
    for bad in (
        "DELETE FROM activities",
        "SELECT 1; DROP TABLE activities",
        "WITH x AS (SELECT 1) DELETE FROM activities",
        "SELECT * FROM pragma_table_info('activities') ; PRAGMA writable_schema=1",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "UPDATE activities SET name='x'",
    ):
        res = call("query_sql", {"consulta": bad})
        assert res.startswith(("Solo se permite", "Error SQL")), (bad, res)
    assert seeded.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 20


def test_risk_flags_detect_load_spike_hrv_and_sleep(conn, monkeypatch):
    monkeypatch.setattr(queries, "_today", lambda: TODAY)
    rows = []
    for i in range(28):  # carga crónica baja: 50/día
        rows.append({"activity_id": i, "date": (TODAY - timedelta(days=i)).isoformat(), "type": "running",
                     "training_load": 50 if i >= 7 else 200, "hr_z4_s": 2000, "hr_z5_s": 1000, "hr_z2_s": 500})
    db.upsert(conn, "activities", rows, ("activity_id",))
    for i, (hrv, sleep) in enumerate([(70, 8), (68, 7.5), (66, 6), (55, 5.5), (54, 6), (52, 5)]):
        d = (TODAY - timedelta(days=5 - i)).isoformat()
        db.upsert(conn, "daily_health", [{"date": d, "hrv_last_night": hrv, "hrv_baseline_low": 60,
                                          "sleep_s": sleep * 3600, "steps": 1}], ("date",))
    flags = "\n".join(queries.risk_flags(conn, TODAY))
    assert "Carga aguda:crónica 2.29" in flags  # (200*7/7) / ((200*7+50*21)/28)
    assert "HRV por debajo" in flags
    assert "Sueño acumulado bajo" in flags
    assert "Distribución de intensidad" in flags


def test_prompt_includes_profile_and_question():
    from garmin_coach.server import mcp

    res = asyncio.run(mcp.get_prompt("entrenador", {"pregunta": "¿Estoy listo para un 10K a 4:30/km?"}))
    text = res.messages[0].content.text
    assert "PERFIL DEL DEPORTISTA" in text and "4:30/km" in text
