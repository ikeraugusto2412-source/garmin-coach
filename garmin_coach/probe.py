"""Sonda: consulta todos los endpoints relevantes para los últimos N días,
guarda el JSON crudo en probe_output/ y muestra qué llega realmente.

Sirve para decidir el modelo de datos antes de construir la sincronización
completa y para ver qué endpoints no soporta tu dispositivo.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from garminconnect import Garmin

from .api import Caller, CallResult
from .auth import PROJECT_ROOT

OUT_DIR = PROJECT_ROOT / "probe_output"


def _range_calls(g: Garmin, start: str, end: str) -> list[tuple[str, Any, tuple, dict]]:
    return [
        ("user_profile", g.get_user_profile, (), {}),
        ("devices", g.get_devices, (), {}),
        ("activities", g.get_activities_by_date, (start, end), {}),
        ("sleep_daily", g.get_sleep_daily, (start, end), {}),
        ("hrv_range", g.get_hrv_data_range, (start, end), {}),
        ("rhr_daily", g.get_rhr_daily, (start, end), {}),
        ("body_battery", g.get_body_battery, (start, end), {}),
        ("daily_steps", g.get_daily_steps, (start, end), {}),
        ("max_metrics_range", g.get_max_metrics_range, (start, end), {}),
        ("body_composition", g.get_body_composition, (start, end), {}),
        ("race_predictions_latest", g.get_race_predictions, (), {}),
        ("race_predictions_daily", g.get_race_predictions, (start, end, "daily"), {}),
        ("lactate_threshold", g.get_lactate_threshold, (), {}),
        ("heart_rate_zones", g.get_heart_rate_zones, (), {}),
        ("personal_records", g.get_personal_record, (), {}),
        ("endurance_score", g.get_endurance_score, (start, end), {}),
        ("hill_score", g.get_hill_score, (start, end), {}),
    ]


def _day_calls(g: Garmin, d: str) -> list[tuple[str, Any, tuple, dict]]:
    return [
        (f"user_summary/{d}", g.get_user_summary, (d,), {}),
        (f"sleep/{d}", g.get_sleep_data, (d,), {}),
        (f"stress/{d}", g.get_stress_data, (d,), {}),
        (f"training_readiness/{d}", g.get_training_readiness, (d,), {}),
        (f"training_status/{d}", g.get_training_status, (d,), {}),
        (f"hrv/{d}", g.get_hrv_data, (d,), {}),
    ]


def _activity_calls(g: Garmin, aid: int) -> list[tuple[str, Any, tuple, dict]]:
    return [
        (f"activity/{aid}", g.get_activity, (aid,), {}),
        (f"activity_splits/{aid}", g.get_activity_splits, (aid,), {}),
        (f"activity_hr_zones/{aid}", g.get_activity_hr_in_timezones, (aid,), {}),
        (f"activity_power_zones/{aid}", g.get_activity_power_in_timezones, (aid,), {}),
    ]


def _shape(data: Any) -> str:
    if isinstance(data, list):
        first = data[0] if data else None
        keys = list(first.keys())[:8] if isinstance(first, dict) else []
        return f"lista[{len(data)}] claves: {', '.join(keys)}"
    if isinstance(data, dict):
        keys = list(data.keys())
        more = f" (+{len(keys) - 10})" if len(keys) > 10 else ""
        return f"dict claves: {', '.join(keys[:10])}{more}"
    return type(data).__name__


def _save(res: CallResult) -> None:
    path = OUT_DIR / (res.name.replace("/", "__") + ".json")
    path.write_text(json.dumps(res.data, ensure_ascii=False, indent=2, default=str))


def run_probe(client: Garmin, days: int = 7) -> list[CallResult]:
    OUT_DIR.mkdir(exist_ok=True)
    end = date.today()
    start = end - timedelta(days=days - 1)
    s, e = start.isoformat(), end.isoformat()
    caller = Caller()
    results: list[CallResult] = []

    def run(calls: list[tuple[str, Any, tuple, dict]]) -> None:
        for name, fn, args, kwargs in calls:
            print(f"  · {name} ...", flush=True)
            res = caller.call(name, fn, *args, **kwargs)
            results.append(res)
            if res.ok:
                _save(res)

    print(f"Sondeando Garmin del {s} al {e}\n")
    run(_range_calls(client, s, e))
    # Endpoints diarios: solo hoy y ayer para no gastar peticiones.
    for d in (e, (end - timedelta(days=1)).isoformat()):
        run(_day_calls(client, d))

    acts = next((r.data for r in results if r.name == "activities" and r.ok), None) or []
    if acts:
        run(_activity_calls(client, acts[0]["activityId"]))

    _print_report(results, acts)
    return results


def _print_report(results: list[CallResult], acts: list[dict]) -> None:
    print("\n" + "=" * 78)
    print("RESULTADO DE LA SONDA")
    print("=" * 78)
    for r in results:
        if not r.ok:
            mark, info = "✗", r.error
        elif r.empty:
            mark, info = "∅", "vacío (sin datos para este rango/dispositivo)"
        else:
            mark, info = "✓", _shape(r.data)
        print(f"{mark} {r.name:<38} {info}"[:160])

    if acts:
        print(f"\nActividades de los últimos días ({len(acts)}):")
        for a in acts:
            t = (a.get("activityType") or {}).get("typeKey")
            km = (a.get("distance") or 0) / 1000
            mins = (a.get("duration") or 0) / 60
            print(
                f"  {a.get('startTimeLocal', '')[:16]}  {t:<22} {km:6.2f} km  "
                f"{mins:5.0f} min  FC {a.get('averageHR')}/{a.get('maxHR')}  "
                f"TE {a.get('aerobicTrainingEffect')}/{a.get('anaerobicTrainingEffect')}  "
                f"carga {a.get('activityTrainingLoad')}"
            )
    print(f"\nJSON crudo guardado en: {OUT_DIR}")
