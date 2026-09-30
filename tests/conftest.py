"""Fixtures con datos simulados (sin llamadas a Garmin)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from garmin_coach import db
from garmin_coach.api import Caller

TODAY = date(2026, 9, 30)


def iso(d: date) -> str:
    return d.isoformat()


def fake_activity(aid: int, day: date, typ: str = "running", km: float = 10.0, load: float = 100.0,
                  z: tuple[int, ...] = (600, 1800, 600, 300, 0)) -> dict[str, Any]:
    speed = 1000 / 330  # 5:30/km
    return {
        "activityId": aid,
        "activityName": f"Act {aid}",
        "activityType": {"typeKey": typ},
        "startTimeLocal": f"{iso(day)} 08:00:00",
        "startTimeGMT": f"{iso(day)} 06:00:00",
        "distance": km * 1000 if km else 0.0,
        "duration": km * 330 if km else 2700.0,
        "movingDuration": km * 325 if km else 2700.0,
        "averageSpeed": speed if km else 0.0,
        "avgGradeAdjustedSpeed": speed if km else None,
        "averageHR": 150.0,
        "maxHR": 175.0,
        "elevationGain": 50.0,
        "averageRunningCadenceInStepsPerMinute": 170.0 if typ == "running" else None,
        "avgPower": 250.0 if typ == "running" else None,
        "avgStrideLength": 105.0,
        "aerobicTrainingEffect": 3.2,
        "anaerobicTrainingEffect": 1.1,
        "activityTrainingLoad": load,
        "vO2MaxValue": 45.0,
        **{f"hrTimeInZone_{i + 1}": float(v) for i, v in enumerate(z)},
        "lapCount": 2,
    }


class FakeGarmin:
    """Imita los métodos de garminconnect.Garmin que usa la sincronización."""

    def __init__(self, activities: list[dict[str, Any]], fail: set[str] | None = None) -> None:
        self.activities = activities
        self.fail = fail or set()
        self.calls: list[tuple[str, tuple]] = []

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            from garminconnect import GarminConnectConnectionError

            raise GarminConnectConnectionError(f"API Error 400 - {name} no soportado")

    @staticmethod
    def _range(s: str, e: str):
        d, end = date.fromisoformat(s), date.fromisoformat(e)
        while d <= end:
            yield d
            d += timedelta(days=1)

    def get_devices(self):
        self._rec("get_devices")
        return [
            {"productDisplayName": "old", "registeredDate": 1757000000000},   # 2025-09-04
            {"productDisplayName": "fr965", "registeredDate": 1788000000000},  # 2026-08-29
        ]

    def count_activities(self):
        self._rec("count_activities")
        return len(self.activities)

    def get_activities(self, start, limit):
        self._rec("get_activities", start, limit)
        ordered = sorted(self.activities, key=lambda a: a["startTimeLocal"], reverse=True)
        return ordered[start:start + limit]

    def get_activities_by_date(self, s, e):
        self._rec("get_activities_by_date", s, e)
        return [a for a in self.activities if s <= a["startTimeLocal"][:10] <= e]

    def get_activity_splits(self, aid):
        self._rec("get_activity_splits", aid)
        lap = {"distance": 5000.0, "duration": 1650.0, "averageSpeed": 1000 / 330, "averageHR": 150.0,
               "maxHR": 170.0, "averagePower": 250.0, "averageRunCadence": 170.0, "intensityType": "ACTIVE"}
        return {"activityId": aid, "lapDTOs": [{**lap, "lapIndex": 1}, {**lap, "lapIndex": 2}]}

    def get_activity_hr_in_timezones(self, aid):
        self._rec("get_activity_hr_in_timezones", aid)
        return [{"zoneNumber": i, "secsInZone": 300.0 * i, "zoneLowBoundary": 100 + 20 * i} for i in range(1, 6)]

    def get_activity_exercise_sets(self, aid):
        self._rec("get_activity_exercise_sets", aid)
        return {"exerciseSets": [
            {"setType": "ACTIVE", "repetitionCount": 10, "weight": 60000.0, "duration": 40.0,
             "exercises": [{"category": "SQUAT", "probability": 90}, {"category": "LUNGE", "probability": 10}]},
            {"setType": "REST", "duration": 90.0, "exercises": []},
            {"setType": "ACTIVE", "repetitionCount": 8, "weight": 0.0, "duration": 30.0,
             "exercises": [{"category": "BENCH_PRESS", "probability": 70}]},
        ]}

    def get_sleep_daily(self, s, e):
        self._rec("get_sleep_daily", s, e)
        return [{"calendarDate": iso(d), "values": {"totalSleepTimeInSeconds": 7 * 3600, "deepTime": 5000,
                 "remTime": 6000, "lightTime": 13000, "awakeTime": 1200, "sleepScore": 78,
                 "sleepScoreQuality": "FAIR", "restingHeartRate": 52, "avgOvernightHrv": 65.0}}
                for d in self._range(s, e)]

    def get_hrv_data_range(self, s, e):
        self._rec("get_hrv_data_range", s, e)
        return {"hrvSummaries": [{"calendarDate": iso(d), "lastNightAvg": 65, "weeklyAvg": 64, "status": "BALANCED",
                                  "baseline": {"balancedLow": 58, "balancedUpper": 75}} for d in self._range(s, e)]}

    def get_rhr_daily(self, s, e):
        self._rec("get_rhr_daily", s, e)
        return [{"calendarDate": iso(d), "value": 52.0} for d in self._range(s, e)]

    def get_daily_steps(self, s, e):
        self._rec("get_daily_steps", s, e)
        return [{"calendarDate": iso(d), "totalSteps": 9000, "totalDistance": 7000} for d in self._range(s, e)]

    def get_body_battery(self, s, e):
        self._rec("get_body_battery", s, e)
        return [{"date": iso(d), "charged": 60, "drained": 55, "bodyBatteryValuesArray": [[1, 20], [2, 90]]}
                for d in self._range(s, e)]

    def get_max_metrics_range(self, s, e):
        self._rec("get_max_metrics_range", s, e)
        return [{"generic": {"calendarDate": e, "vo2MaxPreciseValue": 44.5}}]

    def get_race_predictions(self, s=None, e=None, _type=None):
        self._rec("get_race_predictions", s, e, _type)
        if s is None:
            return {"calendarDate": iso(TODAY), "time5K": 1480, "time10K": 3100, "timeHalfMarathon": 7550,
                    "timeMarathon": 17400}
        return [{"calendarDate": iso(d), "time5K": 1500, "time10K": 3120, "timeHalfMarathon": 7600,
                 "timeMarathon": 17500} for d in self._range(s, e)]

    def get_endurance_score(self, s, e):
        self._rec("get_endurance_score", s, e)
        return {"groupMap": {s: {"groupAverage": 5300}}}

    def get_hill_score(self, s, e):
        self._rec("get_hill_score", s, e)
        return {"hillScoreDTOList": [{"calendarDate": e, "overallScore": 13}]}

    def get_body_composition(self, s, e):
        self._rec("get_body_composition", s, e)
        return {"dateWeightList": [{"calendarDate": e, "weight": 62500.0, "bmi": 20.1}]}

    def get_user_summary(self, d):
        self._rec("get_user_summary", d)
        return {"calendarDate": d, "totalSteps": 9100, "restingHeartRate": 51, "averageStressLevel": 28,
                "bodyBatteryHighestValue": 92, "bodyBatteryLowestValue": 18, "includesWellnessData": True,
                "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 30}

    def get_training_readiness(self, d):
        self._rec("get_training_readiness", d)
        return [{"calendarDate": d, "timestampLocal": f"{d}T07:00:00", "score": 70, "level": "MODERATE",
                 "feedbackShort": "OK", "recoveryTime": 600}]

    def get_training_status(self, d):
        self._rec("get_training_status", d)
        return {
            "mostRecentVO2Max": {"generic": {"calendarDate": d, "vo2MaxPreciseValue": 44.6}},
            "mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {
                "calendarDate": d, "primaryTrainingDevice": True, "trainingStatusFeedbackPhrase": "PRODUCTIVE",
                "fitnessTrend": 1, "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 300,
                "dailyTrainingLoadChronic": 280, "dailyAcuteChronicWorkloadRatio": 1.1, "acwrStatus": "OPTIMAL"}}}},
            "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"1": {
                "calendarDate": d, "primaryTrainingDevice": True, "monthlyLoadAerobicLow": 100.0,
                "monthlyLoadAerobicHigh": 800.0, "monthlyLoadAnaerobic": 50.0,
                "trainingBalanceFeedbackPhrase": "AEROBIC_LOW_SHORTAGE"}}},
        }

    def get_lactate_threshold(self):
        self._rec("get_lactate_threshold")
        return {"speed_and_heart_rate": {"speed": 0.33, "heartRate": 172},
                "power": {"functionalThresholdPower": 290, "powerToWeight": 4.64, "weight": 62.5}}

    def get_heart_rate_zones(self):
        self._rec("get_heart_rate_zones")
        return [{"sport": "DEFAULT", "maxHeartRateUsed": 195, **{f"zone{i}Floor": 90 + 20 * i for i in range(1, 6)}}]

    def get_personal_record(self):
        self._rec("get_personal_record")
        return [{"typeId": 3, "activityType": "running", "value": 1459.0, "activityId": 1,
                 "activityStartDateTimeLocalFormatted": "2026-06-01T19:32:58.0"}]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def fast_caller():
    return Caller(delay=0, sleep=lambda s: None)


@pytest.fixture
def activities():
    acts = []
    aid = 1000
    for weeks_ago in range(6):
        for dow in (1, 3, 5):
            day = TODAY - timedelta(weeks=weeks_ago, days=dow)
            acts.append(fake_activity(aid, day))
            aid += 1
    acts.append(fake_activity(aid, TODAY - timedelta(days=2), typ="strength_training", km=0, load=20,
                              z=(2000, 500, 0, 0, 0)))
    acts.append(fake_activity(aid + 1, TODAY - timedelta(days=9), typ="mountain_biking", km=35, load=80))
    return acts
