"""Sincronización Garmin Connect → SQLite.

- Primera ejecución (o --full): todo el histórico.
- Siguientes: incremental desde la última fecha sincronizada, re-descargando
  los últimos días porque Garmin completa datos con retraso.
- Reanudable: el progreso se confirma por bloques, así que si un 429 corta la
  sincronización, la siguiente continúa donde se quedó.

Nota: nunca escribe en stdout (el servidor MCP usa stdout como transporte).
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from garminconnect import Garmin

from . import db
from . import transform as tf
from .api import Caller, CallResult

CHUNK_DAYS = 28          # límite de Garmin en la mayoría de endpoints por rango
OVERLAP_DAYS = 3         # días que se re-descargan en cada incremental


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _chunks(start: date, end: date, size: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def _days(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


@dataclass
class SyncSummary:
    mode: str
    activities_new: int = 0
    activities_detailed: int = 0
    days_health: int = 0
    days_training: int = 0
    failures: list[str] = field(default_factory=list)
    range: tuple[str, str] | None = None

    def text(self) -> str:
        lines = [
            f"Sincronización {self.mode} completada"
            + (f" ({self.range[0]} → {self.range[1]})" if self.range else ""),
            f"  Actividades nuevas/actualizadas: {self.activities_new}",
            f"  Actividades con detalle (splits, zonas, series): {self.activities_detailed}",
            f"  Bloques de salud diaria procesados: {self.days_health}",
            f"  Días de métricas de entrenamiento: {self.days_training}",
        ]
        if self.failures:
            uniq = sorted(set(self.failures))
            lines.append(f"  Endpoints sin datos o con error ({len(uniq)}), ver tabla sync_log:")
            lines += [f"    - {f}" for f in uniq[:15]]
        return "\n".join(lines)


class Syncer:
    def __init__(
        self,
        client: Garmin,
        conn: sqlite3.Connection,
        caller: Caller | None = None,
        progress: Callable[[str], None] = _stderr,
        today: date | None = None,
    ) -> None:
        self.g = client
        self.conn = conn
        self.caller = caller or Caller()
        self.progress = progress
        self.today = today or date.today()

    # ---------- utilidades ----------

    def _call(self, endpoint: str, key: str, fn: Callable[..., Any], *args: Any) -> CallResult:
        res = self.caller.call(f"{endpoint}:{key}", fn, *args)
        if res.ok:
            if not res.empty:
                db.save_raw(self.conn, endpoint, key, res.data)
        else:
            db.log_sync(self.conn, endpoint, "error", f"{key}: {res.error}")
            self.summary.failures.append(f"{endpoint} ({(res.error or '')[:80]})")
        return res

    def _upsert(self, table: str, rows: list[dict[str, Any]], pk: tuple[str, ...] = ("date",)) -> int:
        return db.upsert(self.conn, table, rows, pk)

    # ---------- punto de entrada ----------

    def run(self, full: bool = False) -> SyncSummary:
        first_run = db.get_state(self.conn, "daily_synced_until") is None
        self.summary = SyncSummary(mode="completa" if (full or first_run) else "incremental")

        history_start, detail_since = self._bounds(refresh=full or first_run)
        if full or first_run:
            health_from = history_start
            perday_from = detail_since
            act_from = _d(db.get_state(self.conn, "first_activity_date") or history_start.isoformat())
        else:
            last = _d(db.get_state(self.conn, "daily_synced_until"))
            health_from = last - timedelta(days=OVERLAP_DAYS)
            last_pd = db.get_state(self.conn, "perday_synced_until")
            perday_from = max(detail_since, _d(last_pd) - timedelta(days=OVERLAP_DAYS)) if last_pd else detail_since
            last_act = self.conn.execute("SELECT MAX(date) FROM activities").fetchone()[0]
            act_from = _d(last_act) - timedelta(days=OVERLAP_DAYS) if last_act else history_start
        self.summary.range = (min(health_from, act_from).isoformat(), self.today.isoformat())

        self.progress(f"▶ Sincronización {self.summary.mode}: actividades desde {act_from}, "
                      f"salud desde {health_from}, métricas diarias desde {perday_from}")
        self.sync_activities(act_from)
        self.sync_activity_details()
        self.sync_daily_ranges(health_from)
        self.sync_perday(perday_from)
        self.sync_snapshots()

        db.set_state(self.conn, "last_sync", db.now_iso())
        db.log_sync(self.conn, "sync", "ok", self.summary.mode)
        self.conn.commit()
        return self.summary

    def _bounds(self, refresh: bool) -> tuple[date, date]:
        """Fecha de inicio del histórico y desde cuándo pedir métricas día a día."""
        hs = db.get_state(self.conn, "history_start")
        ds = db.get_state(self.conn, "detail_since")
        if hs and ds and not refresh:
            return _d(hs), _d(ds)

        reg_dates: list[date] = []
        res = self._call("devices", "all", self.g.get_devices)
        for dev in res.data or []:
            ts = dev.get("registeredDate")
            if ts:
                reg_dates.append(datetime.fromtimestamp(ts / 1000).date())

        first_act: date | None = None
        n = self._call("count_activities", "all", self.g.count_activities)
        if n.ok and n.data:
            oldest = self._call("oldest_activity", "all", self.g.get_activities, int(n.data) - 1, 1)
            if oldest.ok and oldest.data:
                first_act = _d(oldest.data[0]["startTimeLocal"])
                db.set_state(self.conn, "first_activity_date", first_act.isoformat())

        # Salud diaria desde el primer reloj; métricas avanzadas desde el reloj actual.
        history_start = min(reg_dates) if reg_dates else (first_act or self.today - timedelta(days=365))
        detail_since = max(reg_dates) if reg_dates else self.today - timedelta(days=180)
        db.set_state(self.conn, "history_start", history_start.isoformat())
        db.set_state(self.conn, "detail_since", detail_since.isoformat())
        self.conn.commit()
        return history_start, detail_since

    # ---------- actividades ----------

    def sync_activities(self, start: date) -> None:
        for a, b in _chunks(start, self.today, 180):
            res = self._call("activities", f"{a}/{b}", self.g.get_activities_by_date, a.isoformat(), b.isoformat())
            for act in res.data or []:
                db.save_raw(self.conn, "activity", str(act["activityId"]), act)
                self.summary.activities_new += self._upsert("activities", [tf.activity_row(act)], ("activity_id",))
            self.conn.commit()
        self.progress(f"  ✓ actividades: {self.summary.activities_new}")

    def sync_activity_details(self) -> None:
        pending = self.conn.execute(
            "SELECT activity_id, type FROM activities WHERE details_synced=0 ORDER BY date"
        ).fetchall()
        if pending:
            self.progress(f"  … detalle de {len(pending)} actividades (≈{len(pending) * 2} peticiones)")
        for i, row in enumerate(pending, 1):
            aid, typ = row["activity_id"], row["type"]
            ok = True
            r = self._call("activity_splits", str(aid), self.g.get_activity_splits, aid)
            ok &= r.ok or "404" in (r.error or "")
            if r.ok:
                self._upsert("activity_laps", tf.lap_rows(aid, r.data, typ), ("activity_id", "lap_index"))
            r = self._call("activity_hr_zones", str(aid), self.g.get_activity_hr_in_timezones, aid)
            ok &= r.ok or "404" in (r.error or "")
            if r.ok:
                self._upsert("activity_hr_zones", tf.hr_zone_rows(aid, r.data), ("activity_id", "zone"))
            if typ and "strength" in typ:
                r = self._call("activity_exercise_sets", str(aid), self.g.get_activity_exercise_sets, aid)
                ok &= r.ok or "404" in (r.error or "")
                if r.ok:
                    self._upsert("strength_sets", tf.strength_set_rows(aid, r.data), ("activity_id", "set_index"))
            if ok:
                self.conn.execute("UPDATE activities SET details_synced=1 WHERE activity_id=?", (aid,))
                self.summary.activities_detailed += 1
            self.conn.commit()
            if i % 20 == 0:
                self.progress(f"    {i}/{len(pending)}")

    # ---------- salud diaria por rangos ----------

    def sync_daily_ranges(self, start: date) -> None:
        detail_since = _d(db.get_state(self.conn, "detail_since") or start.isoformat())
        chunks = list(_chunks(start, self.today))
        self.progress(f"  … salud diaria en {len(chunks)} bloques de {CHUNK_DAYS} días")
        for i, (a, b) in enumerate(chunks, 1):
            s, e = a.isoformat(), b.isoformat()
            key = f"{s}/{e}"
            r = self._call("sleep_daily", key, self.g.get_sleep_daily, s, e)
            self._upsert("daily_health", tf.sleep_daily_rows(r.data))
            r = self._call("hrv_range", key, self.g.get_hrv_data_range, s, e)
            self._upsert("daily_health", tf.hrv_rows(r.data))
            r = self._call("rhr_daily", key, self.g.get_rhr_daily, s, e)
            self._upsert("daily_health", tf.rhr_rows(r.data))
            r = self._call("daily_steps", key, self.g.get_daily_steps, s, e)
            self._upsert("daily_health", tf.steps_rows(r.data))
            r = self._call("body_battery", key, self.g.get_body_battery, s, e)
            self._upsert("daily_health", tf.body_battery_rows(r.data))
            r = self._call("max_metrics", key, self.g.get_max_metrics_range, s, e)
            self._upsert("training_daily", tf.max_metrics_rows(r.data))
            if b >= detail_since:
                r = self._call("race_predictions", key, self.g.get_race_predictions, s, e, "daily")
                self._upsert("race_predictions", tf.race_prediction_rows(r.data))
                r = self._call("endurance_score", key, self.g.get_endurance_score, s, e)
                self._upsert("training_daily", tf.endurance_rows(r.data))
                r = self._call("hill_score", key, self.g.get_hill_score, s, e)
                self._upsert("training_daily", tf.hill_rows(r.data))
            self.summary.days_health += 1
            db.set_state(self.conn, "daily_synced_until", e)
            self.conn.commit()
            if i % 10 == 0:
                self.progress(f"    bloque {i}/{len(chunks)} ({e})")

        # Peso: el endpoint admite rangos largos.
        for a, b in _chunks(start, self.today, 365):
            r = self._call("body_composition", f"{a}/{b}", self.g.get_body_composition, a.isoformat(), b.isoformat())
            self._upsert("body_composition", tf.body_comp_rows(r.data))
        self.conn.commit()
        self.progress("  ✓ salud diaria")

    # ---------- métricas día a día (solo reloj actual) ----------

    def sync_perday(self, start: date) -> None:
        days = list(_days(start, self.today))
        self.progress(f"  … métricas diarias de entrenamiento: {len(days)} días (≈{len(days) * 3} peticiones)")
        for i, day in enumerate(days, 1):
            d = day.isoformat()
            r = self._call("user_summary", d, self.g.get_user_summary, d)
            if r.ok and not r.empty:
                self._upsert("daily_health", [tf.user_summary_row(d, r.data)])
            r = self._call("training_readiness", d, self.g.get_training_readiness, d)
            if r.ok and not r.empty:
                self._upsert("training_daily", [tf.readiness_row(d, r.data)])
            r = self._call("training_status", d, self.g.get_training_status, d)
            if r.ok and not r.empty:
                self._upsert("training_daily", [tf.training_status_row(d, r.data)])
            self.summary.days_training += 1
            db.set_state(self.conn, "perday_synced_until", d)
            self.conn.commit()
            if i % 15 == 0:
                self.progress(f"    {i}/{len(days)} ({d})")

    # ---------- instantáneas ----------

    def sync_snapshots(self) -> None:
        d = self.today.isoformat()
        lt = self._call("lactate_threshold", d, self.g.get_lactate_threshold)
        zones = self._call("heart_rate_zones", d, self.g.get_heart_rate_zones)
        if lt.ok or zones.ok:
            self._upsert("thresholds", [tf.thresholds_row(d, lt.data, zones.data)])
        pr = self._call("personal_records", d, self.g.get_personal_record)
        if pr.ok:
            self._upsert("personal_records", tf.personal_record_rows(pr.data), ("type_id",))
        rp = self._call("race_predictions_latest", d, self.g.get_race_predictions)
        if rp.ok:
            self._upsert("race_predictions", tf.race_prediction_rows(rp.data))
        self.conn.commit()


def run_sync(client: Garmin, full: bool = False, conn: sqlite3.Connection | None = None,
             progress: Callable[[str], None] = _stderr) -> SyncSummary:
    own = conn is None
    conn = conn or db.connect()
    try:
        return Syncer(client, conn, progress=progress).run(full=full)
    finally:
        if own:
            conn.close()
