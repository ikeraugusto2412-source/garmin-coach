"""Transformaciones puras: JSON de Garmin → filas de la base de datos."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

FOOT_SPORTS = ("running", "trail_running", "treadmill_running", "track_running", "walking", "hiking")

PR_LABELS = {
    1: "1 km",
    2: "1 milla",
    3: "5 km",
    4: "10 km",
    5: "Media maratón",
    6: "Maratón",
    7: "Carrera más larga (m)",
    8: "Salida en bici más larga (m)",
    9: "Mayor desnivel en bici (m)",
    10: "Mejor potencia 20 min (W)",
    11: "40 km en bici",
    12: "Más pasos en un día",
    13: "Más pasos en una semana",
    14: "Más pasos en un mes",
    15: "Racha de objetivos de pasos (días)",
    16: "Objetivo de pasos más largo",
}


def pace_from_speed(speed_mps: float | None) -> float | None:
    """m/s → segundos por km."""
    if not speed_mps or speed_mps <= 0:
        return None
    return round(1000 / speed_mps, 1)


def _round(v: Any, nd: int = 2) -> Any:
    return round(v, nd) if isinstance(v, float) else v


def activity_row(a: dict[str, Any]) -> dict[str, Any]:
    t = (a.get("activityType") or {}).get("typeKey")
    start_local = a.get("startTimeLocal")
    speed = a.get("averageSpeed")
    gap = a.get("avgGradeAdjustedSpeed")
    cadence = a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute")
    is_foot = t in FOOT_SPORTS
    return {
        "activity_id": a["activityId"],
        "date": (start_local or "")[:10],
        "start_local": start_local,
        "start_gmt": a.get("startTimeGMT"),
        "type": t,
        "name": a.get("activityName"),
        "duration_s": _round(a.get("duration"), 1),
        "moving_s": _round(a.get("movingDuration"), 1),
        "distance_m": _round(a.get("distance"), 1),
        "avg_speed_mps": _round(speed, 3),
        "max_speed_mps": _round(a.get("maxSpeed"), 3),
        "avg_gap_speed_mps": _round(gap, 3),
        "pace_s_km": pace_from_speed(gap or speed) if is_foot else None,
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "elev_gain_m": _round(a.get("elevationGain"), 1),
        "elev_loss_m": _round(a.get("elevationLoss"), 1),
        "avg_cadence": _round(cadence, 1),
        "avg_power": a.get("avgPower"),
        "max_power": a.get("maxPower"),
        "norm_power": a.get("normPower"),
        "avg_stride_m": _round((a.get("avgStrideLength") or 0) / 100, 2) or None,
        "avg_gct_ms": _round(a.get("avgGroundContactTime"), 1),
        "avg_vert_osc_cm": _round(a.get("avgVerticalOscillation"), 2),
        "avg_vert_ratio": _round(a.get("avgVerticalRatio"), 2),
        "calories": a.get("calories"),
        "te_aerobic": _round(a.get("aerobicTrainingEffect"), 1),
        "te_anaerobic": _round(a.get("anaerobicTrainingEffect"), 1),
        "te_label": a.get("trainingEffectLabel"),
        "training_load": _round(a.get("activityTrainingLoad"), 1),
        "vo2max": a.get("vO2MaxValue"),
        "body_battery_diff": a.get("differenceBodyBattery"),
        **{f"hr_z{i}_s": _round(a.get(f"hrTimeInZone_{i}"), 0) for i in range(1, 6)},
        "lap_count": a.get("lapCount"),
    }


def lap_rows(activity_id: int, splits: dict[str, Any], activity_type: str | None) -> list[dict[str, Any]]:
    is_foot = activity_type in FOOT_SPORTS
    rows = []
    for i, lap in enumerate((splits or {}).get("lapDTOs") or []):
        speed = lap.get("averageSpeed")
        gap = lap.get("avgGradeAdjustedSpeed")
        rows.append(
            {
                "activity_id": activity_id,
                "lap_index": lap.get("lapIndex", i + 1),
                "distance_m": _round(lap.get("distance"), 1),
                "duration_s": _round(lap.get("duration"), 1),
                "avg_speed_mps": _round(speed, 3),
                "avg_gap_speed_mps": _round(gap, 3),
                "pace_s_km": pace_from_speed(speed) if is_foot else None,
                "avg_hr": lap.get("averageHR"),
                "max_hr": lap.get("maxHR"),
                "avg_power": lap.get("averagePower"),
                "avg_cadence": _round(lap.get("averageRunCadence") or lap.get("averageBikeCadence"), 1),
                "elev_gain_m": _round(lap.get("elevationGain"), 1),
                "elev_loss_m": _round(lap.get("elevationLoss"), 1),
                "intensity_type": lap.get("intensityType"),
            }
        )
    return rows


def hr_zone_rows(activity_id: int, zones: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "activity_id": activity_id,
            "zone": z.get("zoneNumber"),
            "seconds": _round(z.get("secsInZone"), 0),
            "low_bpm": z.get("zoneLowBoundary"),
        }
        for z in zones or []
        if z.get("zoneNumber") is not None
    ]


def strength_set_rows(activity_id: int, data: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for i, s in enumerate((data or {}).get("exerciseSets") or []):
        if s.get("setType") != "ACTIVE":
            continue
        ex = max(s.get("exercises") or [{}], key=lambda e: e.get("probability") or 0)
        weight = s.get("weight")
        rows.append(
            {
                "activity_id": activity_id,
                "set_index": i,
                "exercise": ex.get("category"),
                "exercise_name": ex.get("name"),
                "reps": s.get("repetitionCount"),
                # Garmin devuelve el peso en gramos; 0 = no registrado.
                "weight_kg": round(weight / 1000, 1) if weight else None,
                "duration_s": _round(s.get("duration"), 1),
                "start_time": s.get("startTime"),
            }
        )
    return rows


# ---------- salud diaria ----------

def sleep_daily_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for it in items or []:
        v = it.get("values") or {}
        rows.append(
            {
                "date": it.get("calendarDate"),
                "sleep_s": v.get("totalSleepTimeInSeconds"),
                "deep_s": v.get("deepTime"),
                "light_s": v.get("lightTime"),
                "rem_s": v.get("remTime"),
                "awake_s": v.get("awakeTime"),
                "sleep_score": v.get("sleepScore"),
                "sleep_quality": v.get("sleepScoreQuality"),
                "sleep_need_min": v.get("sleepNeed"),
                "sleep_avg_hr": v.get("avgHeartRate"),
                "respiration": v.get("respiration"),
                "resting_hr": v.get("restingHeartRate"),
                "hrv_last_night": v.get("avgOvernightHrv"),
            }
        )
    return [r for r in rows if r["date"]]


def hrv_rows(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for h in (data or {}).get("hrvSummaries") or []:
        b = h.get("baseline") or {}
        rows.append(
            {
                "date": h.get("calendarDate"),
                "hrv_last_night": h.get("lastNightAvg"),
                "hrv_weekly_avg": h.get("weeklyAvg"),
                "hrv_5min_high": h.get("lastNight5MinHigh"),
                "hrv_status": h.get("status"),
                "hrv_baseline_low": b.get("balancedLow"),
                "hrv_baseline_high": b.get("balancedUpper"),
            }
        )
    return [r for r in rows if r["date"]]


def rhr_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"date": r.get("calendarDate"), "resting_hr": r.get("value")}
        for r in items or []
        if r.get("calendarDate") and r.get("value")
    ]


def steps_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"date": r.get("calendarDate"), "steps": r.get("totalSteps"), "distance_m": r.get("totalDistance")}
        for r in items or []
        if r.get("calendarDate")
    ]


def body_battery_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for d in items or []:
        levels = [v[1] for v in d.get("bodyBatteryValuesArray") or [] if len(v) > 1 and v[1] is not None]
        rows.append(
            {
                "date": d.get("date"),
                "bb_charged": d.get("charged"),
                "bb_drained": d.get("drained"),
                "bb_high": max(levels) if levels else None,
                "bb_low": min(levels) if levels else None,
            }
        )
    return [r for r in rows if r["date"]]


def user_summary_row(d: str, s: dict[str, Any] | None) -> dict[str, Any]:
    s = s or {}
    stress = s.get("averageStressLevel")
    return {
        "date": d,
        "steps": s.get("totalSteps"),
        "distance_m": s.get("totalDistanceMeters"),
        "total_kcal": s.get("totalKilocalories") if s.get("includesWellnessData") else None,
        "active_kcal": s.get("activeKilocalories") if s.get("includesWellnessData") else None,
        "resting_hr": s.get("restingHeartRate"),
        "min_hr": s.get("minHeartRate"),
        "max_hr": s.get("maxHeartRate"),
        "stress_avg": stress if stress is not None and stress >= 0 else None,
        "stress_max": s.get("maxStressLevel"),
        "bb_high": s.get("bodyBatteryHighestValue"),
        "bb_low": s.get("bodyBatteryLowestValue"),
        "bb_charged": s.get("bodyBatteryChargedValue"),
        "bb_drained": s.get("bodyBatteryDrainedValue"),
        "intensity_min_moderate": s.get("moderateIntensityMinutes") if s.get("includesWellnessData") else None,
        "intensity_min_vigorous": s.get("vigorousIntensityMinutes") if s.get("includesWellnessData") else None,
    }


# ---------- entrenamiento ----------

def readiness_row(d: str, data: list[dict[str, Any]] | dict[str, Any] | None) -> dict[str, Any]:
    items = data if isinstance(data, list) else ([data] if data else [])
    # Si hay varias lecturas en el día, la de la mañana (primera) es la de referencia.
    items = sorted(items, key=lambda x: x.get("timestampLocal") or "")
    r = items[0] if items else {}
    rec = r.get("recoveryTime")
    return {
        "date": d,
        "readiness_score": r.get("score"),
        "readiness_level": r.get("level"),
        "readiness_feedback": r.get("feedbackShort"),
        "recovery_time_h": round(rec / 60, 1) if rec is not None else None,
    }


def training_status_row(d: str, data: dict[str, Any] | None) -> dict[str, Any]:
    data = data or {}
    row: dict[str, Any] = {"date": d}
    vo2 = ((data.get("mostRecentVO2Max") or {}).get("generic") or {})
    if vo2.get("calendarDate") == d:
        row["vo2max"] = vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue")

    status_map = ((data.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {})
    st = _primary(status_map)
    if st and st.get("calendarDate") == d:
        acute = st.get("acuteTrainingLoadDTO") or {}
        row.update(
            {
                "training_status": st.get("trainingStatusFeedbackPhrase"),
                "fitness_trend": st.get("fitnessTrend"),
                "acute_load": acute.get("dailyTrainingLoadAcute"),
                "chronic_load": acute.get("dailyTrainingLoadChronic"),
                "chronic_load_min": acute.get("minTrainingLoadChronic"),
                "chronic_load_max": acute.get("maxTrainingLoadChronic"),
                "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
                "acwr_status": acute.get("acwrStatus"),
            }
        )

    bal_map = ((data.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {})
    bal = _primary(bal_map)
    if bal and bal.get("calendarDate") == d:
        row.update(
            {
                "load_aerobic_low": _round(bal.get("monthlyLoadAerobicLow"), 1),
                "load_aerobic_high": _round(bal.get("monthlyLoadAerobicHigh"), 1),
                "load_anaerobic": _round(bal.get("monthlyLoadAnaerobic"), 1),
                "load_balance_feedback": bal.get("trainingBalanceFeedbackPhrase"),
            }
        )
    return row


def _primary(device_map: dict[str, Any]) -> dict[str, Any] | None:
    vals = [v for v in device_map.values() if isinstance(v, dict)]
    for v in vals:
        if v.get("primaryTrainingDevice"):
            return v
    return vals[0] if vals else None


def max_metrics_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for m in items or []:
        g = m.get("generic") or {}
        if g.get("calendarDate") and (g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue")):
            rows.append({"date": g["calendarDate"], "vo2max": g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue")})
    return rows


def endurance_rows(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    gm = (data or {}).get("groupMap") or {}
    return [{"date": d, "endurance_score": v.get("groupAverage")} for d, v in gm.items() if v.get("groupAverage")]


def hill_rows(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        {"date": h.get("calendarDate"), "hill_score": h.get("overallScore")}
        for h in (data or {}).get("hillScoreDTOList") or []
        if h.get("calendarDate") and h.get("overallScore") is not None
    ]


def race_prediction_rows(items: list[dict[str, Any]] | dict[str, Any] | None) -> list[dict[str, Any]]:
    items = items if isinstance(items, list) else ([items] if items else [])
    return [
        {
            "date": p.get("calendarDate"),
            "time_5k_s": p.get("time5K"),
            "time_10k_s": p.get("time10K"),
            "time_half_s": p.get("timeHalfMarathon"),
            "time_full_s": p.get("timeMarathon"),
        }
        for p in items
        if p.get("calendarDate")
    ]


def body_comp_rows(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for w in (data or {}).get("dateWeightList") or []:
        d = w.get("calendarDate")
        if not d and w.get("date"):
            d = datetime.fromtimestamp(w["date"] / 1000).date().isoformat()
        g = lambda k: round(w[k] / 1000, 2) if w.get(k) else None  # noqa: E731  gramos → kg
        rows.append(
            {
                "date": d,
                "weight_kg": g("weight"),
                "bmi": _round(w.get("bmi"), 1),
                "body_fat_pct": _round(w.get("bodyFat"), 1),
                "body_water_pct": _round(w.get("bodyWater"), 1),
                "muscle_mass_kg": g("muscleMass"),
                "bone_mass_kg": g("boneMass"),
            }
        )
    return [r for r in rows if r["date"]]


def thresholds_row(d: str, lt: dict[str, Any] | None, zones: list[dict[str, Any]] | None) -> dict[str, Any]:
    lt = lt or {}
    shr = lt.get("speed_and_heart_rate") or {}
    pw = lt.get("power") or {}
    # Garmin guarda la velocidad del umbral en decenas de m/s (0.33 → 3.3 m/s).
    speed = shr.get("speed")
    speed = speed * 10 if speed and speed < 1 else speed
    z = next((z for z in zones or [] if z.get("sport") in ("RUNNING", "DEFAULT")), None) or {}
    floors = [z.get(f"zone{i}Floor") for i in range(1, 6)] if z else None
    return {
        "date": d,
        "lt_hr": shr.get("heartRate"),
        "lt_speed_mps": _round(speed, 3),
        "lt_pace_s_km": pace_from_speed(speed),
        "run_ftp_w": pw.get("functionalThresholdPower"),
        "power_to_weight": pw.get("powerToWeight"),
        "weight_kg": pw.get("weight"),
        "hr_max_used": z.get("maxHeartRateUsed"),
        "hr_zone_floors": json.dumps(floors) if floors else None,
    }


def personal_record_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for p in items or []:
        tid = p.get("typeId")
        if tid is None:
            continue
        ts = p.get("activityStartDateTimeLocalFormatted") or p.get("actStartDateTimeInGMTFormatted") or ""
        rows.append(
            {
                "type_id": tid,
                "label": PR_LABELS.get(tid, f"tipo {tid}"),
                "activity_type": p.get("activityType"),
                "value": _round(p.get("value"), 1),
                "activity_id": p.get("activityId"),
                "date": ts[:10] or None,
            }
        )
    return rows
