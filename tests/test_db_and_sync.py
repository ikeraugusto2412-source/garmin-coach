"""Tests de la capa de base de datos, transformaciones y sincronización."""

from __future__ import annotations

import sqlite3

import pytest

from garmin_coach import db
from garmin_coach import transform as tf
from garmin_coach.api import Caller, RateLimited
from garmin_coach.sync import Syncer

from .conftest import TODAY, FakeGarmin, fake_activity


def quiet(_msg: str) -> None:
    pass


# ---------- base de datos ----------

def test_schema_creates_all_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("activities", "activity_laps", "activity_hr_zones", "strength_sets", "daily_health",
              "training_daily", "race_predictions", "body_composition", "thresholds", "personal_records",
              "raw_responses", "sync_state", "sync_log"):
        assert t in names


def test_upsert_merges_without_overwriting_with_null(conn):
    db.upsert(conn, "daily_health", [{"date": "2026-09-01", "steps": 5000, "resting_hr": 50}], ("date",))
    db.upsert(conn, "daily_health", [{"date": "2026-09-01", "steps": None, "sleep_s": 25000}], ("date",))
    r = conn.execute("SELECT steps, resting_hr, sleep_s FROM daily_health").fetchone()
    assert tuple(r) == (5000, 50, 25000)


def test_readonly_connection_rejects_writes(tmp_path):
    path = tmp_path / "ro.db"
    db.connect(path).close()
    ro = db.connect_readonly(path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO sync_state VALUES ('a','b')")
    ro.close()


def test_raw_and_state_roundtrip(conn):
    db.save_raw(conn, "x", "k", {"a": [1, 2]})
    db.set_state(conn, "last", "2026-09-30")
    assert db.get_state(conn, "last") == "2026-09-30"
    assert conn.execute("SELECT json_extract(json, '$.a[1]') FROM raw_responses").fetchone()[0] == 2


# ---------- transformaciones ----------

def test_activity_row_computes_pace_and_stride():
    row = tf.activity_row(fake_activity(1, TODAY))
    assert row["pace_s_km"] == 330.0
    assert row["avg_stride_m"] == 1.05
    assert row["date"] == "2026-09-30"
    bike = tf.activity_row(fake_activity(2, TODAY, typ="mountain_biking"))
    assert bike["pace_s_km"] is None


def test_strength_sets_skip_rest_and_convert_grams():
    rows = tf.strength_set_rows(5, FakeGarmin([]).get_activity_exercise_sets(5))
    assert [r["exercise"] for r in rows] == ["SQUAT", "BENCH_PRESS"]
    assert rows[0]["weight_kg"] == 60.0
    assert rows[1]["weight_kg"] is None  # 0 = no registrado


def test_thresholds_fix_lactate_speed_units():
    g = FakeGarmin([])
    row = tf.thresholds_row("2026-09-30", g.get_lactate_threshold(), g.get_heart_rate_zones())
    assert row["lt_speed_mps"] == 3.3
    assert row["lt_pace_s_km"] == pytest.approx(303.0, abs=0.1)
    assert row["hr_max_used"] == 195


def test_training_status_ignores_stale_dates():
    data = FakeGarmin([]).get_training_status("2026-09-20")
    assert tf.training_status_row("2026-09-21", data) == {"date": "2026-09-21"}
    row = tf.training_status_row("2026-09-20", data)
    assert row["training_status"] == "PRODUCTIVE" and row["vo2max"] == 44.6 and row["acwr"] == 1.1


def test_user_summary_ignores_days_without_wellness():
    row = tf.user_summary_row("2026-09-29", {"totalKilocalories": 2000.0, "averageStressLevel": -1,
                                             "includesWellnessData": False})
    assert row["total_kcal"] is None and row["stress_avg"] is None


# ---------- sincronización ----------

def test_full_then_incremental_sync(conn, fast_caller, activities):
    g = FakeGarmin(activities)
    s = Syncer(g, conn, caller=fast_caller, progress=quiet, today=TODAY).run()
    assert s.mode == "completa"
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == len(activities)
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE details_synced=0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM strength_sets").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM activity_laps").fetchone()[0] == 2 * (len(activities))
    h = conn.execute("SELECT * FROM daily_health WHERE date='2026-09-30'").fetchone()
    assert h["sleep_s"] == 7 * 3600 and h["hrv_last_night"] == 65 and h["steps"] == 9100 and h["bb_high"] == 92
    t = conn.execute("SELECT * FROM training_daily WHERE date='2026-09-30'").fetchone()
    assert t["readiness_score"] == 70 and t["training_status"] == "PRODUCTIVE"
    assert db.get_state(conn, "daily_synced_until") == "2026-09-30"
    assert conn.execute("SELECT COUNT(*) FROM personal_records").fetchone()[0] == 1

    # Incremental: solo pide los últimos días y no repite el detalle de actividades.
    g2 = FakeGarmin(activities + [fake_activity(9999, TODAY, load=150)])
    s2 = Syncer(g2, conn, caller=fast_caller, progress=quiet, today=TODAY).run()
    assert s2.mode == "incremental"
    detail_calls = [c for c in g2.calls if c[0] == "get_activity_splits"]
    assert detail_calls == [("get_activity_splits", (9999,))]
    perday = {c[1][0] for c in g2.calls if c[0] == "get_user_summary"}
    assert min(perday) == "2026-09-27"  # 3 días de solape
    assert "get_devices" not in {c[0] for c in g2.calls}


def test_failing_endpoint_is_logged_and_sync_continues(conn, fast_caller, activities):
    g = FakeGarmin(activities, fail={"get_hrv_data_range", "get_activity_exercise_sets"})
    s = Syncer(g, conn, caller=fast_caller, progress=quiet, today=TODAY).run()
    assert any("hrv_range" in f for f in s.failures)
    assert conn.execute("SELECT COUNT(*) FROM sync_log WHERE status='error'").fetchone()[0] > 0
    # El resto de datos llega igualmente.
    assert conn.execute("SELECT COUNT(*) FROM daily_health WHERE sleep_s IS NOT NULL").fetchone()[0] > 300
    # La actividad de fuerza queda pendiente para reintentar.
    assert conn.execute("SELECT type FROM activities WHERE details_synced=0").fetchall()[0][0] == "strength_training"


def test_rate_limit_waits_then_aborts_and_resume_works(conn, activities):
    from garminconnect import GarminConnectTooManyRequestsError

    waits: list[float] = []
    g = FakeGarmin(activities)
    original = g.get_body_battery
    state = {"n": 0}

    def flaky(s, e):
        state["n"] += 1
        if state["n"] >= 3:
            raise GarminConnectTooManyRequestsError("429")
        return original(s, e)

    g.get_body_battery = flaky
    caller = Caller(delay=0, sleep=waits.append, max_429_retries=2, base_429_wait=10)
    with pytest.raises(RateLimited):
        Syncer(g, conn, caller=caller, progress=quiet, today=TODAY).run()
    assert waits == [10, 20]
    done_until = db.get_state(conn, "daily_synced_until")
    assert done_until is not None  # se guardó el progreso de los bloques completos

    g.get_body_battery = original
    Syncer(g, conn, caller=Caller(delay=0, sleep=lambda s: None), progress=quiet, today=TODAY).run()
    assert db.get_state(conn, "daily_synced_until") == "2026-09-30"
