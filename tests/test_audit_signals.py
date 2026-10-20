"""Coaching-audit signal fixes: training_load.py, normalizer.py, intervals.py."""

from __future__ import annotations

from datetime import date, timedelta
import unittest
from unittest import mock

from triathlon_agent.intervals import (
    IntervalsAdapterError,
    LiveIntervalsAdapter,
    MockIntervalsAdapter,
    recent_activity_ids,
)
from triathlon_agent.normalizer import (
    _activity_signals,
    _classify_periodization_state,
    _derive_wellness_baseline,
    _merge_race_calendars,
    _normalize_discipline,
    _planned_intensity,
    _single_prior_plan_adherence,
    classify_note_clause,
    extract_activity_note_flags,
    normalize_snapshot,
)
from triathlon_agent.training_load import (
    _is_quality,
    acute_window_end,
    assess_daily_signals,
    assess_efficiency_trend,
    assess_eftp_trend,
    assess_fitness,
    assess_ftp_staleness,
    assess_hrv_gate,
    assess_intensity_distribution,
    assess_overreaching,
    assess_ramp,
    assess_return_from_break,
    assess_run_load,
    assess_training_load,
    assess_underload,
    assess_volume,
    elevation_loss,
    is_bike_relevant,
    is_race_activity,
    ramp_thresholds,
    subjective_rolling,
    tsb_positive_threshold,
    zone_seconds,
)


PLANNING_DAY = date(2026, 10, 6)  # Tuesday
WEEK_START = date(2026, 10, 12)


def _day(offset: int) -> date:
    return PLANNING_DAY - timedelta(days=offset)


def _act(
    offset: int,
    *,
    kind: str = "Run",
    minutes: float = 60,
    km: float = 10,
    load: float = 60,
    name: str = "Easy run",
    **extra,
) -> dict:
    return {
        "id": f"a{offset}-{kind}-{name}",
        "start_date_local": f"{_day(offset).isoformat()}T08:00:00",
        "type": kind,
        "name": name,
        "moving_time": minutes * 60,
        "distance": km * 1000,
        "icu_training_load": load,
        **extra,
    }


def _wellness(days: int, **fields) -> list[dict]:
    rows = []
    for offset in range(days - 1, -1, -1):
        row = {"id": _day(offset).isoformat()}
        for key, value in fields.items():
            row[key] = value(offset) if callable(value) else value
        rows.append(row)
    return rows


def _underload_kwargs(**overrides) -> dict:
    kwargs = {
        "fitness": {"status": "available", "ctl_ramp_per_week": -0.5, "days_tsb_above_5_last_14": 12},
        "quality": {"average_per_week": 0.5, "average_quality_rpe": None},
        "volume": {"ratio_to_reference": 0.5, "reference_hours": 8, "hours_per_week": 4},
        "hrv_gate": {"status": "normal"},
        "eftp": {"status": "available", "trend": "flat", "delta_pct": 0.5, "window_days": 42},
        "health_events": [],
        "active_phase": "build",
        "requested_week_type": None,
    }
    kwargs.update(overrides)
    return kwargs


def _covid_history() -> tuple[list[dict], list[dict]]:
    """Six steady weeks, 12 days off with COVID, then 3 easy days back."""
    activities = []
    for offset in range(16, 70):
        if offset % 2 == 0:
            activities.append(_act(offset, minutes=60, km=10, load=60))
        else:
            activities.append(_act(offset, kind="Ride", minutes=90, km=40, load=80, name="Endurance ride"))
    activities += [_act(offset, minutes=30, km=5, load=25) for offset in (1, 2, 3)]
    events = [
        {
            "kind": "sick",
            "name": "COVID",
            "start_date": _day(15).isoformat(),
            "end_date": _day(4).isoformat(),
            "duration_days": 12,
            "severity": "extended",
        }
    ]
    return activities, events


class ReturnFromBreakTests(unittest.TestCase):
    def test_covid_break_gives_reentry_ramp_and_caps(self) -> None:
        activities, events = _covid_history()
        result = assess_return_from_break(
            activities, PLANNING_DAY, health_events=events, week_start=WEEK_START
        )
        self.assertEqual(result["status"], "returning")
        self.assertGreaterEqual(result["days_off"], 12)
        self.assertIn("illness", result["source"])
        self.assertEqual(result["reentry_week"], 1)
        self.assertEqual(result["volume_cap_pct"], 50)
        self.assertAlmostEqual(result["pre_break_run_km"], 35.0, delta=1.0)
        self.assertAlmostEqual(result["run_km_cap"], result["pre_break_run_km"] * 0.5, delta=0.2)
        self.assertEqual(result["long_run_cap_min"], 30)
        self.assertIsNotNone(result["pre_break_weekly_min"])

    def test_activity_gap_alone_counts_and_holiday_counts(self) -> None:
        activities = [_act(offset, minutes=60) for offset in range(12, 60)]
        activities.append(_act(1, minutes=40))
        gap = assess_return_from_break(activities, PLANNING_DAY, week_start=WEEK_START)
        self.assertEqual(gap["status"], "returning")
        self.assertEqual(gap["source"], ["activity_gap"])
        holiday = [{"name": "Summer", "start_date": _day(11).isoformat(), "end_date": _day(2).isoformat()}]
        light = [_act(offset, minutes=60) for offset in range(12, 60)] + [
            _act(offset, minutes=20, name="Hotel run") for offset in (4, 8)
        ] + [_act(1, minutes=40)]
        with_holiday = assess_return_from_break(
            light, PLANNING_DAY, holidays=holiday, week_start=WEEK_START
        )
        self.assertEqual(with_holiday["status"], "returning")
        self.assertIn("holiday", with_holiday["source"])

    def test_ramp_flags_first_week_back_instead_of_normal_return(self) -> None:
        activities = [_act(offset, minutes=60, load=60) for offset in range(15, 50)]
        activities += [_act(offset, minutes=60, load=60) for offset in range(1, 6)]
        rfb = assess_return_from_break(activities, PLANNING_DAY, week_start=WEEK_START)
        self.assertEqual(rfb["status"], "returning")
        ramp = assess_ramp(activities, {"ctl_ramp_per_week": 0.0, "ctl": 40}, PLANNING_DAY, return_from_break=rfb)
        self.assertIn(ramp["status"], {"elevated", "high"})
        self.assertTrue(ramp["return_from_break"])
        self.assertFalse(any("normal return" in item for item in ramp["findings"]))
        self.assertTrue(any("Returning from a break" in item for item in ramp["findings"]))

    def test_ctl_drop_alone_is_returning(self) -> None:
        activities = [_act(offset, minutes=40) for offset in range(1, 80, 2)]
        result = assess_return_from_break(
            activities, PLANNING_DAY, fitness={"ctl_drop_pct_42d": 25.0}, week_start=WEEK_START
        )
        self.assertEqual(result["status"], "returning")
        self.assertEqual(result["source"], ["ctl_drop"])
        self.assertEqual(result["reentry_week"], 2)

    def test_no_break_is_none(self) -> None:
        activities = [_act(offset) for offset in range(1, 60, 2)]
        self.assertEqual(
            assess_return_from_break(activities, PLANNING_DAY, week_start=WEEK_START)["status"], "none"
        )

    def test_break_week_is_marked_in_periodization(self) -> None:
        windows = [{"week_start": f"w{i}", "load": 400.0, "minutes": 500, "partial": False} for i in range(3)]
        windows.append({"week_start": "w3", "load": 0.0, "minutes": 0, "partial": False})
        state = _classify_periodization_state(windows)
        self.assertTrue(state["classified_weeks"][-1]["break_week"])
        self.assertTrue(state["last_week_was_break"])


class UnderloadVetoTests(unittest.TestCase):
    def test_suppressed_hrv_makes_underload_physiological_hold(self) -> None:
        self.assertEqual(assess_underload(**_underload_kwargs())["status"], "detected")
        result = assess_underload(**_underload_kwargs(hrv_gate={"status": "suppressed"}))
        self.assertEqual(result["status"], "explained_physiological")
        self.assertEqual(result["load_action"], "hold")
        self.assertTrue(result["recommendation"].startswith("Hold"))

    def test_rhr_daily_signals_and_fatigue_also_veto(self) -> None:
        for extra in (
            {"resting_hr_delta": 6.0},
            {"daily_signals": {"status": "warning"}},
            {"subjective": {"fatigue": {"high": True, "mean": 3.3, "scale": "intervals_1_4"}}},
        ):
            self.assertEqual(
                assess_underload(**_underload_kwargs(**extra))["status"], "explained_physiological", extra
            )

    def test_stale_ftp_moves_load_markers_to_context(self) -> None:
        rides = [
            _act(offset, kind="Ride", minutes=90, load=70, icu_rolling_ftp=260, name="Ride")
            for offset in range(1, 28, 2)
        ]
        staleness = assess_ftp_staleness(rides, PLANNING_DAY, {"Ride": {"ftp": 300}})
        self.assertTrue(staleness["stale"])
        self.assertTrue(staleness["load_undercounted"])
        self.assertAlmostEqual(staleness["ride_load_correction_factor"], round((300 / 260) ** 2, 2))
        result = assess_underload(**_underload_kwargs(ftp_staleness=staleness))
        self.assertTrue(result["ftp_stale_load_undercounted"])
        counted = {item["marker"] for item in result["markers"]}
        self.assertNotIn("tsb_persistently_positive", counted)
        self.assertNotIn("ctl_flat_or_falling", counted)
        self.assertIn("ctl_flat_or_falling", {item["marker"] for item in result["context"]})

    def test_returning_from_break_explains_underload(self) -> None:
        result = assess_underload(
            **_underload_kwargs(return_from_break={"status": "returning", "reentry_week": 1, "volume_cap_pct": 50})
        )
        self.assertEqual(result["status"], "explained")


class EftpTrendTests(unittest.TestCase):
    def test_only_flat_eftp_counts_as_underload(self) -> None:
        down = {"status": "available", "trend": "down", "delta_pct": -6.0, "window_days": 42}
        result = assess_underload(**_underload_kwargs(eftp=down, bike_relevant=True))
        self.assertNotIn("eftp_not_rising", {item["marker"] for item in result["markers"]})
        self.assertIn("eftp_falling", {item["marker"] for item in result["context"]})
        flat = assess_underload(**_underload_kwargs(bike_relevant=True))
        self.assertIn("eftp_not_rising", {item["marker"] for item in flat["markers"]})

    def test_falling_eftp_under_high_load_is_overreaching(self) -> None:
        result = assess_overreaching(
            wellness=[],
            planning_date=PLANNING_DAY,
            fitness={"ctl": 60, "tsb": -25},
            ramp={"status": "high"},
            eftp={"status": "available", "trend": "down", "delta_pct": -6.0, "window_days": 42},
            efficiency={"status": "stable"},
            hrv_gate={"status": "normal"},
            daily_signals={"status": "ok"},
            rhr_delta_7d=None,
        )
        self.assertEqual(result["status"], "functional")
        self.assertTrue(any(item.startswith("eftp_down") for item in result["markers"]))

    def test_eftp_trend_ignores_future_rides(self) -> None:
        rides = [
            _act(30, kind="Ride", icu_rolling_ftp=280),
            _act(2, kind="Ride", icu_rolling_ftp=281),
            _act(-3, kind="Ride", icu_rolling_ftp=320),
        ]
        self.assertEqual(assess_eftp_trend(rides, PLANNING_DAY)["trend"], "flat")


class StaleSignalTests(unittest.TestCase):
    def test_hrv_gate_is_stale_when_newest_hrv_is_old(self) -> None:
        wellness = [row for row in _wellness(60, hrv=60) if row["id"] <= _day(4).isoformat()]
        gate = assess_hrv_gate(wellness, planning_date=PLANNING_DAY)
        self.assertEqual(gate["status"], "stale")
        self.assertIsNone(gate["extra_intensity_allowed"])
        fresh = assess_hrv_gate(_wellness(60, hrv=lambda i: 60 + i % 3), planning_date=PLANNING_DAY)
        self.assertEqual(fresh["status"], "normal")

    def test_fitness_uses_planning_date(self) -> None:
        wellness = _wellness(50, ctl=50, atl=50)
        wellness.append({"id": (PLANNING_DAY + timedelta(days=3)).isoformat(), "ctl": 80, "atl": 10})
        fitness = assess_fitness(wellness, PLANNING_DAY)
        self.assertEqual(fitness["ctl"], 50)
        self.assertEqual(fitness["age_days"], 0)
        self.assertFalse(fitness["stale"])

    def test_freshness_is_per_signal_and_ctl_rows_are_not_wellness(self) -> None:
        wellness = [row for row in _wellness(20, hrv=60, restingHR=48, sleepSecs=27000) if row["id"] <= _day(5).isoformat()]
        wellness += [{"id": _day(offset).isoformat(), "ctl": 50, "atl": 52} for offset in range(0, 5)]
        snapshot = _snapshot(activities=[_act(5)], wellness=wellness)
        freshness = snapshot["freshness"]
        self.assertEqual(freshness["latest_hrv_date"], _day(5).isoformat())
        self.assertEqual(freshness["latest_resting_hr_date"], _day(5).isoformat())
        self.assertEqual(freshness["latest_sleep_date"], _day(5).isoformat())
        self.assertEqual(freshness["status"], "stale")
        self.assertTrue(freshness["signal_warnings"])


class DailySignalTests(unittest.TestCase):
    def _wellness(self, rhr_bump: dict[int, float], hrv_drop: dict[int, float]) -> list[dict]:
        return _wellness(
            40,
            restingHR=lambda i: 48 + (i % 2) + rhr_bump.get(i, 0),
            hrv=lambda i: (60 + (i % 3)) * hrv_drop.get(i, 1.0),
        )

    def test_ok_warning_alert(self) -> None:
        self.assertEqual(assess_daily_signals(self._wellness({}, {}), PLANNING_DAY)["status"], "ok")
        warning = assess_daily_signals(self._wellness({0: 7, 1: 7}, {}), PLANNING_DAY)
        self.assertEqual(warning["status"], "warning")
        self.assertEqual(warning["days_flagged"], 2)
        self.assertGreaterEqual(warning["resting_hr_delta_bpm"], 5)
        alert = assess_daily_signals(self._wellness({0: 7, 1: 7}, {0: 0.6, 1: 0.6}), PLANNING_DAY)
        self.assertEqual(alert["status"], "alert")
        self.assertLessEqual(alert["hrv_z"], -1.5)
        self.assertEqual(alert["latest_signal_date"], PLANNING_DAY.isoformat())

    def test_stale_and_missing(self) -> None:
        old = [row for row in self._wellness({}, {}) if row["id"] <= _day(5).isoformat()]
        self.assertEqual(assess_daily_signals(old, PLANNING_DAY)["status"], "stale")
        self.assertEqual(assess_daily_signals([], PLANNING_DAY)["status"], "insufficient_data")

    def test_subjective_uses_three_day_rolling_mean(self) -> None:
        wellness = _wellness(10, fatigue=lambda i: {0: 4, 1: 3, 2: 3}.get(i, 1))
        rolling = subjective_rolling(wellness, "fatigue", PLANNING_DAY)
        self.assertAlmostEqual(rolling["mean"], 3.33, places=2)
        self.assertTrue(rolling["high"])
        single_spike = _wellness(10, fatigue=lambda i: 4 if i == 0 else 1)
        self.assertFalse(subjective_rolling(single_spike, "fatigue", PLANNING_DAY)["high"])


class RunLoadTests(unittest.TestCase):
    def test_run_acwr_and_week_caps(self) -> None:
        activities = [_act(offset, minutes=50, km=9, total_elevation_loss=100) for offset in range(8, 40, 2)]
        activities += [_act(offset, minutes=90, km=16, total_elevation_loss=400) for offset in (1, 3, 5)]
        run_load = assess_run_load(activities, PLANNING_DAY, week_start=WEEK_START)
        acwr = run_load["acwr"]
        self.assertGreater(acwr["km"], 1.5)
        self.assertEqual(acwr["status"], "high")
        caps = run_load["week_caps"]
        for key in ("max_run_km", "max_run_min", "max_descent_m", "max_long_run_min", "basis"):
            self.assertIn(key, caps)
        self.assertEqual(caps["max_long_run_min"], 105)

    def test_races_are_excluded_from_longest_run(self) -> None:
        activities = [_act(offset, minutes=70) for offset in range(2, 28, 3)]
        activities.append(_act(4, minutes=180, km=32, name="Trail race 30k"))
        run_load = assess_run_load(activities, PLANNING_DAY, week_start=WEEK_START)
        self.assertEqual(run_load["longest_run_min_28d"], 70.0)
        self.assertEqual(run_load["longest_race_min_28d"], 180.0)
        by_date = assess_run_load(
            [_act(4, minutes=150, name="Sunday run")], PLANNING_DAY, race_dates={_day(4).isoformat()}
        )
        self.assertEqual(by_date["longest_race_min_28d"], 150.0)
        self.assertTrue(is_race_activity({"name": "Leuven 10k", "race": True}))
        self.assertFalse(is_race_activity({"name": "Race pace intervals"}))
        self.assertFalse(is_race_activity({"name": "Long ride with race blocks"}))

    def test_long_run_cap_fallbacks(self) -> None:
        old = assess_run_load([_act(40, minutes=100)], PLANNING_DAY, week_start=WEEK_START)
        self.assertEqual(old["longest_run_min_28d"], 0.0)
        self.assertEqual(old["week_caps"]["max_long_run_min"], 70)
        none = assess_run_load([], PLANNING_DAY, week_start=WEEK_START)
        self.assertEqual(none["week_caps"]["max_long_run_min"], 45)
        self.assertIsNone(none["week_caps"]["max_run_km"])

    def test_return_from_break_tightens_caps(self) -> None:
        activities, events = _covid_history()
        result = assess_training_load(
            activities=activities,
            wellness=[],
            athlete_profile={},
            athlete_state={},
            health_events=events,
            planning_date=PLANNING_DAY,
            week_start=WEEK_START,
        )
        rfb = result["return_from_break"]
        caps = result["run_load"]["week_caps"]
        self.assertEqual(caps["max_run_km"], rfb["run_km_cap"])
        self.assertLessEqual(caps["max_long_run_min"], rfb["long_run_cap_min"])
        self.assertIn("return-from-break", caps["basis"])


class ElevationLossTests(unittest.TestCase):
    def test_missing_loss_falls_back_to_gain(self) -> None:
        self.assertEqual(elevation_loss({"total_elevation_gain": 300}), (300.0, True))
        self.assertEqual(elevation_loss({"total_elevation_gain": 300, "total_elevation_loss": 280}), (280.0, False))
        run_load = assess_run_load(
            [_act(3, total_elevation_gain=500)], PLANNING_DAY, week_start=WEEK_START
        )
        self.assertTrue(run_load["elevation_loss_estimated"])
        week = next(row for row in run_load["weeks"] if row["runs"])
        self.assertEqual(week["elevation_loss_m"], 500)
        self.assertTrue(week["elevation_loss_estimated"])
        signals = _activity_signals({"moving_time": 3600, "total_elevation_gain": 420})
        self.assertEqual(signals["elevation_loss_m"], 420)
        self.assertTrue(signals["elevation_loss_estimated"])


class WellnessSummaryTests(unittest.TestCase):
    def test_extra_fields_and_weight_trend(self) -> None:
        wellness = _wellness(
            35,
            stress=lambda i: 3 if i < 7 else 1,
            mood=2,
            motivation=1,
            injury=lambda i: 2 if i < 3 else 1,
            spO2=97,
            respiration=14.5,
            weight=lambda i: 72.0 - 0.05 * (27 - min(i, 27)),
            fatigue=2,
        )
        summary = _snapshot(activities=[], wellness=wellness)["recent_wellness_summary"]
        for key in ("stress", "mood", "motivation", "injury", "spo2", "respiration"):
            self.assertIn("mean_7d", summary[key])
            self.assertIn("scale", summary[key])
        self.assertEqual(summary["stress"]["mean_7d"], 3.0)
        self.assertEqual(summary["stress"]["baseline_28d"], 1.0)
        self.assertEqual(summary["spo2"]["latest"], 97.0)
        self.assertAlmostEqual(summary["weight_trend_kg_28d"], -1.2, places=1)
        self.assertEqual(summary["fatigue"]["mean_3d"], 2.0)

    def test_recent_wellness_is_date_based_and_empty_acute_is_insufficient(self) -> None:
        wellness = [row for row in _wellness(40, hrv=60) if row["id"] <= _day(10).isoformat()]
        snapshot = _snapshot(activities=[], wellness=wellness)
        self.assertEqual(snapshot["recent_wellness"], [])
        self.assertEqual(snapshot["derived_baselines"]["hrv"]["status"], "insufficient_data")
        self.assertIsNone(snapshot["derived_baselines"]["hrv"]["acute"])
        direct = _derive_wellness_baseline(
            wellness, "hrv", PLANNING_DAY, baseline_days=42, acute_days=7, minimum_entries=21
        )
        self.assertEqual(direct["acute_entries"], 0)


class ActivityNoteTests(unittest.TestCase):
    def test_flags_pain_illness_and_red_flags_in_en_and_nl(self) -> None:
        activities = [
            _act(1, description="Knie pijn op de afdaling"),
            _act(2, description="Felt ziek, beetje koorts"),
            _act(3, description="Chest pain on the last climb, had to stop"),
            _act(4, description="Duizelig na de intervallen"),
            _act(30, description="knee pain"),  # older than 14 days
        ]
        flags = extract_activity_note_flags(activities, PLANNING_DAY)
        kinds = {(item["date"], item["kind"]) for item in flags}
        self.assertIn((_day(1).isoformat(), "pain"), kinds)
        self.assertIn((_day(2).isoformat(), "illness"), kinds)
        self.assertIn((_day(3).isoformat(), "red_flag"), kinds)
        self.assertIn((_day(4).isoformat(), "red_flag"), kinds)
        self.assertEqual(len([item for item in flags if item["date"] == _day(30).isoformat()]), 0)
        knee = next(item for item in flags if item["kind"] == "pain")
        self.assertEqual(knee["body_part"], "knee")
        self.assertIn("excerpt", knee)

    def test_negations_are_ignored(self) -> None:
        for text in ("No pain at all", "Geen pijn", "ITB fine", "knee didn't hurt today", "pain-free long run", "Legs sore after the long run"):
            self.assertIsNone(classify_note_clause(text), text)
        self.assertEqual(classify_note_clause("left calf tight")[0], "pain")

    def test_comments_are_scanned_and_reach_the_snapshot(self) -> None:
        activity = _act(1, id="i123", description="Lovely run")
        snapshot = _snapshot(
            activities=[activity],
            wellness=[],
            activity_messages={"i123": [{"content": "hartkloppingen tijdens de tempo"}]},
            activity_messages_errors=["activity 9 comments: Intervals API error 404"],
        )
        self.assertEqual(snapshot["activity_note_flags"][0]["kind"], "red_flag")
        self.assertEqual(len(snapshot["activity_notes_errors"]), 1)


class OverreachingEfficiencyTests(unittest.TestCase):
    def test_non_functional_risk_needs_persistent_physiology(self) -> None:
        wellness = _wellness(70, hrv=lambda i: 45 if i < 14 else 60 + i % 3, restingHR=lambda i: 54 if i < 14 else 48)
        result = assess_overreaching(
            wellness=wellness,
            planning_date=PLANNING_DAY,
            fitness={"ctl": 60, "tsb": -5},
            ramp={"status": "ok"},
            eftp={"status": "available", "trend": "down", "delta_pct": -5.0, "window_days": 42},
            efficiency={"status": "declining", "ef_change_pct": -5.0},
            hrv_gate={"status": "suppressed"},
            daily_signals={"status": "ok"},
            rhr_delta_7d=6.0,
        )
        self.assertEqual(result["status"], "non_functional_risk")
        self.assertEqual(result["persistence"]["hrv_suppressed_weeks"], 2)
        none = assess_overreaching(
            wellness=[], planning_date=PLANNING_DAY, fitness={}, ramp={"status": "ok"},
            eftp={}, efficiency={}, hrv_gate={"status": "normal"}, daily_signals={"status": "ok"}, rhr_delta_7d=None,
        )
        self.assertEqual(none["status"], "none")

    def test_efficiency_trend_and_inverted_feel(self) -> None:
        activities = []
        for offset in range(1, 42, 2):
            recent = offset < 14
            activities.append(
                _act(offset, minutes=60, km=12 if recent else 10, average_heartrate=145, feel=4 if recent else 2)
            )
        trend = assess_efficiency_trend(activities, PLANNING_DAY)
        self.assertEqual(trend["status"], "improving")
        self.assertGreater(trend["ef_change_pct"], 3)
        # Feel went 2 -> 4: a higher number is WORSE on the Intervals scale.
        self.assertEqual(trend["feel_trend"]["direction"], "worsening")
        self.assertEqual(assess_efficiency_trend([], PLANNING_DAY)["status"], "insufficient_data")


class ZoneSourceTests(unittest.TestCase):
    def test_rides_use_power_zones_and_distribution_matches(self) -> None:
        ride = _act(
            2,
            kind="Ride",
            minutes=60,
            icu_hr_zone_times=[3600, 0, 0, 0, 0, 0, 0],
            icu_zone_times=[
                {"id": "Z1", "secs": 600}, {"id": "Z2", "secs": 1200}, {"id": "Z3", "secs": 600},
                {"id": "Z4", "secs": 1200}, {"id": "Z5", "secs": 0}, {"id": "SS", "secs": 1500},
            ],
        )
        self.assertEqual(zone_seconds(ride)["source"], "power")
        self.assertEqual(zone_seconds(ride)["above_lt1_s"], 1800)
        self.assertTrue(_is_quality(ride))
        distribution = assess_intensity_distribution([ride], PLANNING_DAY, ftp_stale=True)
        self.assertEqual(distribution["moderate_pct"], 50.0)
        self.assertEqual(distribution["zone_sources"], {"power": 1})
        self.assertIn("caveat", distribution)
        hr_only = {**ride, "icu_zone_times": None}
        self.assertEqual(zone_seconds(hr_only)["source"], "hr")


class RelativeThresholdTests(unittest.TestCase):
    def test_ramp_and_tsb_thresholds_scale_with_ctl(self) -> None:
        self.assertEqual(ramp_thresholds(30), (4.0, 6.0))
        self.assertEqual(ramp_thresholds(85), (6.8, 10.2))
        self.assertEqual(ramp_thresholds(None), (5.0, 8.0))
        self.assertEqual(tsb_positive_threshold(30), 3.0)
        self.assertEqual(tsb_positive_threshold(85), 8.5)
        low = assess_ramp([], {"ctl_ramp_per_week": 5.0, "ctl": 30}, PLANNING_DAY)
        high = assess_ramp([], {"ctl_ramp_per_week": 5.0, "ctl": 85}, PLANNING_DAY)
        self.assertEqual(low["status"], "elevated")
        self.assertEqual(high["status"], "ok")

    def test_tsb_positive_days_relative(self) -> None:
        wellness = _wellness(30, ctl=85, atl=79)  # TSB +6: fresh at CTL 50, not at CTL 85
        fitness = assess_fitness(wellness, PLANNING_DAY)
        self.assertEqual(fitness["days_tsb_positive_last_14"], 0)
        self.assertEqual(fitness["days_tsb_above_5_last_14"], 14)


class AcwrWindowTests(unittest.TestCase):
    def test_acute_ends_yesterday_and_chronic_is_uncoupled(self) -> None:
        self.assertEqual(acute_window_end([], PLANNING_DAY), _day(1))
        self.assertEqual(acute_window_end([_act(0)], PLANNING_DAY), PLANNING_DAY)
        activities = [_act(offset, load=50) for offset in range(1, 36)]
        ramp = assess_ramp(activities, {}, PLANNING_DAY)
        self.assertEqual(ramp["acute_window_end"], _day(1).isoformat())
        self.assertEqual(ramp["acute_7d_load"], 350.0)
        self.assertEqual(ramp["chronic_weekly_avg_load"], 350.0)
        self.assertEqual(ramp["acwr"], 1.0)
        self.assertEqual(ramp["chronic_coverage_days"], 28)

    def test_missing_chronic_is_insufficient(self) -> None:
        ramp = assess_ramp([_act(1, load=80)], {}, PLANNING_DAY)
        self.assertIsNone(ramp["acwr"])
        self.assertEqual(ramp["acwr_status"], "insufficient_data")


class DisciplineAndPairingTests(unittest.TestCase):
    def test_swim_variants_alias_to_swim(self) -> None:
        self.assertEqual(_normalize_discipline("OpenWaterSwim"), "Swim")
        self.assertEqual(_normalize_discipline("GravelRide"), "Ride")
        self.assertEqual(_normalize_discipline("Cyclocross"), "Ride")
        self.assertEqual(_normalize_discipline("TrailRun"), "Run")

    def test_paired_event_id_wins_over_heuristics(self) -> None:
        sessions = [
            {"date": "2026-09-29", "day": "Tuesday", "discipline": "Run", "session_name": "Easy Run", "duration_min": 45},
            {"date": "2026-09-30", "day": "Wednesday", "discipline": "Run", "session_name": "Tempo Run", "duration_min": 60},
        ]
        events = [
            {"id": 501, "external_id": "coach-2026-09-29-run-easy-run"},
            {"id": 502, "external_id": "coach-2026-09-30-run-tempo-run"},
        ]
        activities = [
            # Done on Tuesday but paired by Intervals to Wednesday's tempo.
            {"id": "x1", "start_date_local": "2026-09-29T07:00:00", "type": "Run", "name": "Tempo", "moving_time": 3600, "paired_event_id": 502},
            {"id": "x2", "start_date_local": "2026-09-30T07:00:00", "type": "Run", "name": "Easy", "moving_time": 2700, "paired_event_id": 501},
        ]
        result = _single_prior_plan_adherence({"final_plan": {"sessions": sessions}}, activities, events)
        reviews = {item["session_name"]: item for item in result["session_reviews"]}
        self.assertEqual(reviews["Tempo Run"]["actual_minutes"], 60.0)
        self.assertEqual(reviews["Easy Run"]["actual_minutes"], 45.0)
        swim = _single_prior_plan_adherence(
            {"final_plan": {"sessions": [{"date": "2026-09-29", "day": "Tuesday", "discipline": "Swim", "session_name": "OW", "duration_min": 40}]}},
            [{"start_date_local": "2026-09-29T18:00:00", "type": "OpenWaterSwim", "moving_time": 2400}],
        )
        self.assertEqual(swim["missed_sessions_count"], 0)


class QualityFloorTests(unittest.TestCase):
    def test_base_phase_and_ultra_race_lower_quality_floor(self) -> None:
        one_per_week = {"average_per_week": 1.0, "average_quality_rpe": None}
        build = assess_underload(**_underload_kwargs(quality=one_per_week))
        self.assertIn("low_quality_frequency", {item["marker"] for item in build["markers"]})
        base = assess_underload(**_underload_kwargs(quality=one_per_week, active_phase="base"))
        self.assertNotIn("low_quality_frequency", {item["marker"] for item in base["markers"]})
        trail_race = {"name": "Hill Trail", "distance": "Ultra Trail", "notes": "50 km", "priority": "A", "days_to_race": 210}
        ultra = assess_underload(**_underload_kwargs(quality=one_per_week, priority_race=trail_race))
        self.assertNotIn("low_quality_frequency", {item["marker"] for item in ultra["markers"]})
        self.assertEqual(ultra["quality_floor_per_week"], 1.0)


class VolumeReferenceTests(unittest.TestCase):
    def test_reference_excludes_comparison_window_and_break_weeks(self) -> None:
        activities = [_act(offset, minutes=60) for offset in range(0, 84) if not 35 <= offset < 49]
        volume = assess_volume(activities, PLANNING_DAY, {})
        # Weeks 5-12 at 7 h/week, two holiday weeks at 0 h are excluded.
        self.assertEqual(volume["recent_12_week_hours_per_week"], 7.0)
        self.assertEqual(len([w for w in volume["reference_weeks_excluded"] if w["reason"] == "break_week"]), 2)
        holiday = [(_day(76), _day(70))]
        with_span = assess_volume(activities, PLANNING_DAY, {}, excluded_spans=holiday)
        self.assertTrue(any(w["reason"] == "illness_or_holiday" for w in with_span["reference_weeks_excluded"]))
        # A light last month is compared with the earlier norm, not with itself.
        light = [_act(offset, minutes=30) for offset in range(0, 28)] + [_act(offset, minutes=60) for offset in range(28, 84)]
        self.assertEqual(assess_volume(light, PLANNING_DAY, {})["ratio_to_reference"], 0.5)


class QualityNameTests(unittest.TestCase):
    def test_test_hint_uses_word_boundaries(self) -> None:
        self.assertFalse(_is_quality({"type": "Run", "name": "Club contest social run"}))
        self.assertFalse(_is_quality({"type": "Run", "name": "Fastest easy loop"}))
        self.assertTrue(_is_quality({"type": "Run", "name": "30 min run test"}))
        self.assertTrue(_is_quality({"type": "Ride", "name": "Sweet spot intervals"}))
        self.assertEqual(_planned_intensity({"session_name": "Contest recce", "discipline": "Run"}), "easy")
        self.assertEqual(_planned_intensity({"session_name": "FTP test", "discipline": "Ride"}), "quality")


class IntervalsCalendarOnlyTests(unittest.TestCase):
    INTERVALS = [
        {"name": "Hill Trail", "date": "2027-05-08", "priority": "A", "distance": "Ultra Trail", "distance_source": "intervals_text", "notes": "", "source": "intervals"},
        {"name": "Spring 10k", "date": "2027-03-14", "priority": "C", "distance": "Run", "distance_source": "intervals_type", "notes": "", "source": "intervals"},
    ]

    def test_local_calendar_cannot_add_or_override_races(self) -> None:
        manual = [
            {"name": "Hill Trail", "date": "2027-05-09", "priority": "B", "distance": "Marathon", "notes": "Hilly, 2000 m D+", "demands": "descents"},
            {"name": "Spring 10k", "date": "2027-03-14", "priority": "A", "distance": "10k"},
            {"name": "Imaginary 70.3", "date": "2027-06-01", "priority": "A", "distance": "70.3"},
        ]
        merged, warnings = _merge_race_calendars(self.INTERVALS, manual)
        self.assertEqual(len(merged), 2)
        trail_race = next(item for item in merged if item["name"] == "Hill Trail")
        self.assertEqual(trail_race["date"], "2027-05-08")
        self.assertEqual(trail_race["priority"], "A")
        self.assertEqual(trail_race["distance"], "Ultra Trail")
        self.assertEqual(trail_race["notes"], "Hilly, 2000 m D+")
        self.assertEqual(trail_race["demands"], "descents")
        ten_k = next(item for item in merged if item["name"] == "Spring 10k")
        self.assertEqual(ten_k["priority"], "C")
        self.assertEqual(ten_k["distance"], "10k")  # filled: Intervals only had a sport type
        self.assertEqual(len(warnings), 1)
        self.assertIn("Imaginary 70.3 2027-06-01", warnings[0])
        self.assertIn("not on the Intervals calendar", warnings[0])

    def test_snapshot_races_only_from_intervals_and_state_race_stripped(self) -> None:
        snapshot = _snapshot(
            activities=[],
            wellness=[],
            events=[{"id": 1, "category": "RACE_A", "name": "Hill Trail", "type": "TrailRun", "description": "50 km", "start_date_local": "2027-05-08T00:00:00"}],
            race_calendar={"races": [{"name": "Local only", "date": "2026-11-01", "priority": "A", "distance": "Marathon"}]},
            athlete_state={"active_macro_cycle": {"phase": "base", "priority_race": {"name": "Old goal"}}},
        )
        self.assertEqual([race["name"] for race in snapshot["races"]], ["Hill Trail"])
        self.assertEqual([race["name"] for race in snapshot["race_calendar"]["races"]], ["Hill Trail"])
        self.assertEqual(len(snapshot["race_calendar_warnings"]), 1)
        cycle = snapshot["athlete_state"]["active_macro_cycle"]
        self.assertNotIn("priority_race", cycle)
        self.assertEqual(cycle["last_planned_priority_race"], {"name": "Old goal"})

    def test_bike_relevance_never_from_profile(self) -> None:
        self.assertFalse(is_bike_relevant(None, {"target_race_distance": "70.3"}))
        self.assertTrue(is_bike_relevant({"name": "X 70.3", "distance": "70.3"}))
        result = assess_underload(**_underload_kwargs())
        self.assertTrue(any("no race on the Intervals calendar" in item["evidence"] for item in result["context"]))
        load = assess_training_load(
            activities=[],
            wellness=[],
            athlete_profile={"target_race_distance": "70.3"},
            athlete_state={"active_macro_cycle": {"priority_race": {"name": "Old 70.3", "distance": "70.3", "days_to_race": 30}}},
            health_events=[],
            planning_date=PLANNING_DAY,
        )
        self.assertTrue(any(item["marker"] == "no_priority_race" for item in load["underload"]["context"]))


class IntervalsAdapterNoteTests(unittest.TestCase):
    def test_recent_activity_ids_window(self) -> None:
        activities = [
            {"id": "old", "start_date_local": "2026-09-01T08:00:00"},
            {"id": "new", "start_date_local": "2026-10-05T08:00:00"},
            {"start_date_local": "2026-10-04T08:00:00"},
        ]
        self.assertEqual(recent_activity_ids(activities, "2026-10-06"), ["new"])

    def test_live_comment_failures_are_non_fatal(self) -> None:
        adapter = LiveIntervalsAdapter("https://intervals.icu/api/v1", "key")

        def fake_request(method, path, params=None, payload=None):
            if path.endswith("/a1/messages"):
                return [{"content": "geen pijn"}]
            raise IntervalsAdapterError("Intervals API error 404: nope")

        adapter._request = fake_request  # type: ignore[method-assign]
        messages, errors = adapter.fetch_activity_messages(["a1", "a2"])
        self.assertEqual(messages, {"a1": [{"content": "geen pijn"}]})
        self.assertEqual(len(errors), 1)

    def test_mock_snapshot_has_message_fields(self) -> None:
        data = MockIntervalsAdapter("fixtures").fetch_snapshot("2026-03-08")
        self.assertEqual(data["activity_messages"], {})
        self.assertEqual(data["activity_messages_errors"], [])


class SundayCheckinTests(unittest.TestCase):
    """A Sunday check-in closes the week once Sunday's planned sessions are done."""

    SUNDAY = date(2026, 10, 4)

    def _windows(self, prior_plan_contexts: list[dict]) -> list[dict]:
        class FrozenDate(date):
            @classmethod
            def today(cls) -> date:
                return SundayCheckinTests.SUNDAY

        swim = {
            "id": "sun-swim",
            "start_date_local": f"{self.SUNDAY.isoformat()}T08:00:00",
            "type": "Swim",
            "name": "Morning swim",
            "moving_time": 2700,
            "distance": 2500,
            "icu_training_load": 35,
        }
        with mock.patch("triathlon_agent.utils.date", FrozenDate):
            snapshot = normalize_snapshot(
                {
                    "athlete_profile": {"name": "Test"},
                    "weekly_checkin": {"planning_date": self.SUNDAY.isoformat()},
                    "race_calendar": {"races": []},
                    "athlete_state": {},
                    "prior_plan_contexts": prior_plan_contexts,
                },
                {"activities": [swim], "wellness": [], "events": [], "sport_settings": []},
            )
        return snapshot["weekly_windows"]

    def test_week_stays_partial_while_the_sunday_long_run_is_still_to_come(self) -> None:
        plan = {
            "final_plan": {
                "sessions": [
                    {"day": "Sunday", "date": "2026-10-04", "discipline": "Swim", "session_name": "Swim"},
                    {"day": "Sunday", "date": "2026-10-04", "discipline": "Run", "session_name": "Long run"},
                ]
            }
        }
        self.assertTrue(self._windows([plan])[-1]["partial"])
        self.assertTrue(self._windows([])[-1]["partial"])

    def test_week_closes_when_every_sunday_session_is_done(self) -> None:
        plan = {"final_plan": {"sessions": [{"day": "Sunday", "date": "2026-10-04", "discipline": "Swim", "session_name": "Swim"}]}}
        self.assertFalse(self._windows([plan])[-1]["partial"])


def _snapshot(
    *,
    activities: list[dict],
    wellness: list[dict],
    events: list[dict] | None = None,
    race_calendar: dict | None = None,
    athlete_state: dict | None = None,
    **intervals_extra,
) -> dict:
    return normalize_snapshot(
        {
            "athlete_profile": {"name": "Test"},
            "weekly_checkin": {"planning_date": PLANNING_DAY.isoformat()},
            "race_calendar": race_calendar or {"races": []},
            "athlete_state": athlete_state or {},
        },
        {
            "activities": activities,
            "wellness": wellness,
            "events": events or [],
            "sport_settings": [],
            **intervals_extra,
        },
    )


if __name__ == "__main__":
    unittest.main()
