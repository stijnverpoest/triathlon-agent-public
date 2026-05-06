from __future__ import annotations

from datetime import date as calendar_date
from typing import Any

from .utils import day_name


def _session(
    day,
    discipline,
    name,
    objective,
    duration,
    intensity,
    priority,
    primary_metric,
    zone_focus,
    blocks,
    notes,
    success,
    cut_first,
    training_method="General",
    daniels_type=None,
    coggan_level=None,
    expected_if=None,
    estimated_tss=None,
    variant_id=None,
):
    payload = {
        "day": day_name(day),
        "date": day.isoformat(),
        "discipline": discipline,
        "session_name": name,
        "objective": objective,
        "duration_min": duration,
        "intensity_guidance": intensity,
        "priority": priority,
        "primary_metric": primary_metric,
        "zone_focus": zone_focus,
        "blocks": blocks,
        "execution_notes": notes,
        "success_criteria": success,
        "cut_first": cut_first,
        "training_method": training_method,
    }
    if daniels_type is not None:
        payload["daniels_type"] = daniels_type
    if coggan_level is not None:
        payload["coggan_level"] = coggan_level
    if expected_if is not None:
        payload["expected_if"] = expected_if
    if estimated_tss is not None:
        payload["estimated_tss"] = estimated_tss
    if variant_id is not None:
        payload["variant_id"] = variant_id
    return payload


def _build_payload_session(
    day, discipline: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return _session(
        day,
        discipline,
        payload["name"],
        payload["objective"],
        payload["duration"],
        payload["intensity"],
        payload["priority"],
        payload["primary_metric"],
        payload["zone_focus"],
        payload["blocks"],
        payload["notes"],
        payload["success"],
        payload["cut_first"],
        training_method=payload.get("training_method", "General"),
        daniels_type=payload.get("daniels_type"),
        coggan_level=payload.get("coggan_level"),
        expected_if=payload.get("expected_if"),
        estimated_tss=payload.get("estimated_tss"),
        variant_id=payload.get("variant_id"),
    )


def _context_text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).strip().lower()


def _priority_race_distance(session_context: dict[str, Any] | None) -> str:
    if not session_context:
        return "generic"
    priority_race = session_context.get("priority_race") or {}
    text = _context_text(
        priority_race.get("distance"),
        session_context.get("target_race_distance"),
    )
    if any(token in text for token in ("70.3", "half", "middle")):
        return "70.3"
    if "olympic" in text:
        return "olympic"
    if "sprint" in text:
        return "sprint"
    return "generic"


def _goal_focuses(session_context: dict[str, Any] | None) -> set[str]:
    if not session_context:
        return set()
    text = _context_text(
        session_context.get("primary_performance_goal"),
        session_context.get("secondary_goal"),
        " ".join(session_context.get("race_specific_needs", [])),
        session_context.get("phase_focus"),
    )
    focuses: set[str] = set()
    if any(
        token in text
        for token in (
            "better run",
            "unlock the run",
            "converting strong bike fitness into a better run",
            "run durability",
        )
    ):
        focuses.add("run_off_bike")
    if any(
        token in text
        for token in (
            "open-water",
            "open water",
            "swim pace",
            "swim execution",
            "pool swim pace",
        )
    ):
        focuses.add("swim_execution")
    if any(token in text for token in ("durability", "fueling", "4 to 5 hours")):
        focuses.add("durability")
    return focuses


def _status_flags(session_context: dict[str, Any] | None) -> set[str]:
    if not session_context:
        return set()
    flags: set[str] = set()
    readiness_status = str(session_context.get("readiness_status", "")).lower()
    week_classification = str(session_context.get("week_classification", "")).lower()
    if readiness_status and readiness_status != "green":
        flags.add("caution")
    if week_classification in {"absorb", "recovery", "sharpen"}:
        flags.add("caution")
    pain_text = _context_text(session_context.get("pain_or_niggles"))
    if any(token in pain_text for token in ("back", "itb", "tightness", "niggle")):
        flags.add("mobility_bias")
    avg_sleep = session_context.get("avg_sleep_hours")
    try:
        if avg_sleep is not None and float(avg_sleep) < 7.0:
            flags.add("sleep_limited")
            flags.add("caution")
    except (TypeError, ValueError):
        pass
    return flags


def _rotation_slot(
    session_context: dict[str, Any] | None, kind: str, pool_size: int
) -> int:
    if pool_size <= 1:
        return 0
    if not session_context:
        return 0
    seed = sum(ord(char) for char in kind)
    planning_date = session_context.get("planning_date")
    if planning_date:
        try:
            plan_day = calendar_date.fromisoformat(str(planning_date))
            iso_year, iso_week, _ = plan_day.isocalendar()
            seed += (iso_year * 53) + iso_week
        except ValueError:
            pass
    try:
        seed += int(session_context.get("friel_block_week") or 0)
    except (TypeError, ValueError):
        pass
    return seed % pool_size


def _recent_signatures(session_context: dict[str, Any] | None, kind: str) -> set[str]:
    if not session_context:
        return set()
    signatures = session_context.get("recent_session_signatures_by_kind", {})
    return {
        str(item).strip().lower()
        for item in signatures.get(kind, [])
        if str(item).strip()
    }


def _pick_variant(
    kind: str,
    variants: list[dict[str, Any]],
    session_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not variants:
        raise ValueError(f"No variants configured for session kind: {kind}")
    if not session_context:
        return variants[0]
    recent = _recent_signatures(session_context, kind)
    filtered = [
        variant
        for variant in variants
        if variant["id"].lower() not in recent and variant["name"].lower() not in recent
    ]
    pool = filtered or variants
    return pool[_rotation_slot(session_context, kind, len(pool))]


def build_recovery_session(day, duration: int, **_: Any) -> dict[str, Any]:
    return _session(
        day,
        "Recovery",
        "Full Rest + Mobility",
        "Absorb the previous block and arrive fresher for the next real work.",
        duration,
        "Very easy mobility only",
        "support",
        "notes",
        "recovery",
        [
            {
                "section": "Reset",
                "steps": [
                    {
                        "duration": f"{duration}m",
                        "target": "Mobility and breathing reset",
                    }
                ],
            }
        ],
        [
            "Keep the day intentionally light.",
            "Use walking and mobility only if they leave you feeling better.",
        ],
        "You feel better at night than you did in the morning.",
        "Skip all optional mobility before touching the rest of the week.",
        training_method="Friel",
    )


def build_easy_run_session(
    day, duration: int, *, name: str = "Aerobic Reset Run", **_: Any
) -> dict[str, Any]:
    warmup = 10
    cooldown = 5
    main = max(duration - warmup - cooldown, 15)
    actual = warmup + main + cooldown
    return _session(
        day,
        "Run",
        name,
        "Keep run frequency alive without spending energy that should go to the rest of the week.",
        actual,
        "All aerobic, conversational breathing",
        "support",
        "HR",
        "aerobic",
        [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z1 HR"}],
            },
            {
                "section": "Main Set",
                "steps": [{"duration": f"{main}m", "target": "Z2 HR"}],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 HR"}],
            },
        ],
        [
            "Stay flat and relaxed.",
            "This should feel like frequency and circulation, not hidden quality.",
        ],
        "The run stays relaxed, breathing stays conversational, and you finish fresher than you started.",
        "Trim the main aerobic block by 5-10 minutes before shortening the warmup or cooldown.",
        training_method="Daniels",
        daniels_type="E",
    )


def _quality_run_threshold_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    base_warmup = 15
    cooldown = 10
    repeat_count = max(2, min(4 if not lighter_week else 3, (duration - 25) // 7))
    repeat_count = max(repeat_count, 2)
    main_block = repeat_count * 7
    extra = max(duration - (base_warmup + main_block + cooldown), 0)
    warmup = base_warmup + extra
    actual = warmup + main_block + cooldown
    return {
        "id": "run_threshold_repeats",
        "name": "Threshold Pace Repeats",
        "objective": "Touch race-relevant run pace while keeping the total run dose small enough to absorb.",
        "duration": actual,
        "intensity": "Threshold controlled by pace, not by heroics",
        "priority": "key",
        "primary_metric": "Pace",
        "zone_focus": "threshold",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z2 Pace"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "5m", "target": "98-102% Pace"},
                    {"duration": "2m", "target": "Z1 Pace"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 Pace"}],
            },
        ],
        "notes": [
            "Choose a flat route or track-like loop.",
            "Stay controlled and stop one rep early if form or niggle feedback worsens.",
        ],
        "success": "Pace stays controlled, posture remains relaxed, and the run never turns into a grind.",
        "cut_first": f"Cut the {'third' if lighter_week else 'fourth'} repeat first.",
        "training_method": "Daniels",
        "daniels_type": "T",
        "variant_id": "run_threshold_repeats",
    }


def _quality_run_cruise_payload(duration: int, *, lighter_week: bool) -> dict[str, Any]:
    warmup = 15
    cooldown = 10
    repeat_count = 3 if lighter_week else 4
    while repeat_count > 2 and duration - (warmup + cooldown + repeat_count * 10) < 0:
        repeat_count -= 1
    actual = warmup + cooldown + repeat_count * 10
    return {
        "id": "run_cruise_intervals",
        "name": "Cruise Intervals",
        "objective": "Build durable threshold economy that supports a stronger run off the bike.",
        "duration": actual,
        "intensity": "Smooth threshold pressure that never tips into straining",
        "priority": "key",
        "primary_metric": "Pace",
        "zone_focus": "threshold economy",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z2 Pace"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "8m", "target": "96-100% Pace"},
                    {"duration": "2m", "target": "Z1 Pace"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 Pace"}],
            },
        ],
        "notes": [
            "Settle into repeatable rhythm rather than chasing one flashy interval.",
            "This should feel like durable race support, not 5k-style strain.",
        ],
        "success": "Each cruise interval lands at a repeatable effort and the last one looks like the first.",
        "cut_first": "Cut the final cruise interval first.",
        "training_method": "Daniels",
        "daniels_type": "T",
        "variant_id": "run_cruise_intervals",
    }


def _quality_run_progression_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    warmup = 15 if lighter_week else 18
    cooldown = 10
    steady = max(duration - warmup - cooldown, 24)
    actual = warmup + steady + cooldown
    first_block = max(10, steady // 2)
    second_block = max(12, steady - first_block)
    return {
        "id": "run_progression_tempo",
        "name": "Progression Tempo Run",
        "objective": "Build steady race-relevant run durability without the stop-start cost of short repetitions.",
        "duration": actual,
        "intensity": "Start controlled, then progress to strong-but-smooth tempo",
        "priority": "key",
        "primary_metric": "Pace",
        "zone_focus": "tempo progression",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z2 Pace"}],
            },
            {
                "section": "Main Set",
                "steps": [
                    {"duration": f"{first_block}m", "target": "95-98% Pace"},
                    {"duration": f"{second_block}m", "target": "98-101% Pace"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 Pace"}],
            },
        ],
        "notes": [
            "Run the first half with patience so the progression is earned, not forced.",
            "This is the right choice when you want durable work without extra stop-start stress.",
        ],
        "success": "The pace progresses smoothly while posture and breathing stay organized.",
        "cut_first": "Shorten the final progression block before trimming the warmup.",
        "training_method": "Daniels",
        "daniels_type": "T",
        "variant_id": "run_progression_tempo",
    }


def _quality_run_float_payload(duration: int, *, lighter_week: bool) -> dict[str, Any]:
    warmup = 18
    cooldown = 10
    repeat_count = 3 if lighter_week else 4
    while repeat_count > 2 and duration - (warmup + cooldown + repeat_count * 7) < 0:
        repeat_count -= 1
    actual = warmup + cooldown + repeat_count * 7
    return {
        "id": "run_race_float_intervals",
        "name": "Race-Pace Float Intervals",
        "objective": "Sharpen olympic-distance pace control while keeping the run economical instead of ragged.",
        "duration": actual,
        "intensity": "Controlled race-pace pressure with short float recoveries",
        "priority": "key",
        "primary_metric": "Pace",
        "zone_focus": "race pace control",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z2 Pace"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "5m", "target": "100-103% Pace"},
                    {"duration": "2m", "target": "90-93% Pace"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 Pace"}],
            },
        ],
        "notes": [
            "Keep the float honest but controlled rather than fully easy.",
            "If you cannot regain posture during the float, the race-pace work is too hard.",
        ],
        "success": "Race pace feels rhythmic and the float never becomes survival jogging.",
        "cut_first": "Cut the last work-float cycle first.",
        "training_method": "Daniels",
        "daniels_type": "T",
        "variant_id": "run_race_float_intervals",
    }


def build_quality_run_session(
    day,
    duration: int,
    *,
    lighter_week: bool,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    default_payload = _quality_run_threshold_payload(
        duration, lighter_week=lighter_week
    )
    if not session_context:
        return _build_payload_session(day, "Run", default_payload)

    distance = _priority_race_distance(session_context)
    goals = _goal_focuses(session_context)
    flags = _status_flags(session_context)
    phase = str(session_context.get("macro_phase", "")).lower()

    if "caution" in flags:
        variants = [
            _quality_run_progression_payload(duration, lighter_week=lighter_week),
            _quality_run_cruise_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    elif phase in {"race_specific", "taper"} and distance == "olympic":
        variants = [
            _quality_run_float_payload(duration, lighter_week=lighter_week),
            default_payload,
            _quality_run_progression_payload(duration, lighter_week=lighter_week),
        ]
    elif distance == "70.3" or "run_off_bike" in goals:
        variants = [
            _quality_run_cruise_payload(duration, lighter_week=lighter_week),
            _quality_run_progression_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    elif phase == "base":
        variants = [
            _quality_run_progression_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    else:
        variants = [
            default_payload,
            _quality_run_float_payload(duration, lighter_week=lighter_week),
            _quality_run_cruise_payload(duration, lighter_week=lighter_week),
        ]

    payload = _pick_variant("quality_run", variants, session_context)
    return _build_payload_session(day, "Run", payload)


def _threshold_ride_build_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    warmup = 15
    cooldown = 10
    finish = 0 if duration < 70 else 10
    repeat_count = max(2, min(3, (duration - warmup - cooldown - finish) // 12))
    repeat_count = max(repeat_count, 2)
    extra = max(duration - (warmup + cooldown + finish + repeat_count * 12), 0)
    warmup += extra
    actual = warmup + cooldown + finish + repeat_count * 12
    return {
        "id": "ride_threshold_build",
        "name": "Threshold Bike Build",
        "objective": "Reinforce sustainable bike power without turning the session into a brute-force test.",
        "duration": actual,
        "intensity": "Controlled threshold, repeatable from start to finish",
        "priority": "key",
        "primary_metric": "%FTP",
        "zone_focus": "threshold",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "8m", "target": "95-100%", "cadence": "85-95rpm"},
                    {"duration": "4m", "target": "55-60%"},
                ],
            },
            *(
                [
                    {
                        "section": "Finish",
                        "steps": [
                            {
                                "duration": f"{finish}m",
                                "target": "88-92%",
                                "cadence": "90rpm",
                            }
                        ],
                    }
                ]
                if finish
                else []
            ),
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "50-55%"}],
            },
        ],
        "notes": [
            "Keep power smooth rather than aggressive.",
            "If the final repeat fades, stay controlled instead of forcing watts.",
        ],
        "success": "All threshold blocks stay honest and even with no late-session power spikes.",
        "cut_first": "Drop the finish block before dropping threshold repeats.",
        "training_method": "Coggan",
        "coggan_level": "L4 Threshold",
        "expected_if": 0.8 if lighter_week else 0.82,
        "estimated_tss": 74 if lighter_week else 88,
        "variant_id": "ride_threshold_build",
    }


def _threshold_ride_over_under_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    warmup = 15
    cooldown = 10
    repeat_count = 2 if lighter_week else 3
    repeat_block = 16
    while (
        repeat_count > 2
        and duration - (warmup + cooldown + repeat_count * repeat_block) < 0
    ):
        repeat_count -= 1
    extra = max(duration - (warmup + cooldown + repeat_count * repeat_block), 0)
    warmup += extra
    actual = warmup + cooldown + repeat_count * repeat_block
    return {
        "id": "ride_over_under_control",
        "name": "Over-Under Control Ride",
        "objective": "Lift threshold control without turning the session into a death march.",
        "duration": actual,
        "intensity": "Race-relevant pressure with smooth changes around threshold",
        "priority": "key",
        "primary_metric": "%FTP",
        "zone_focus": "threshold control",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "4m", "target": "90-92%", "cadence": "90rpm"},
                    {"duration": "4m", "target": "98-101%", "cadence": "85-90rpm"},
                    {"duration": "4m", "target": "90-92%", "cadence": "90rpm"},
                    {"duration": "4m", "target": "55-60%"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "50-55%"}],
            },
        ],
        "notes": [
            "Keep the under portions controlled enough that the over segments stay smooth.",
            "This is about composure around threshold, not about maximal suffering.",
        ],
        "success": "Power changes stay smooth and you never need to force the overs.",
        "cut_first": "Drop the final over-under cycle first.",
        "training_method": "Coggan",
        "coggan_level": "L4 Threshold",
        "expected_if": 0.79 if lighter_week else 0.82,
        "estimated_tss": 72 if lighter_week else 86,
        "variant_id": "ride_over_under_control",
    }


def _threshold_ride_cruise_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    warmup = 15
    cooldown = 10
    repeat_count = 2
    interval_duration = 14 if lighter_week else 18
    recovery_duration = 5
    total_main = repeat_count * (interval_duration + recovery_duration)
    extra = max(duration - (warmup + cooldown + total_main), 0)
    warmup += extra
    actual = warmup + cooldown + total_main
    return {
        "id": "ride_race_power_cruise",
        "name": "Race-Power Cruise Ride",
        "objective": "Build controlled race-power durability that leaves enough run in the legs afterward.",
        "duration": actual,
        "intensity": "Steady sweet-spot to low-threshold pressure",
        "priority": "key",
        "primary_metric": "%FTP",
        "zone_focus": "race power durability",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {
                        "duration": f"{interval_duration}m",
                        "target": "88-92%",
                        "cadence": "88-92rpm",
                    },
                    {"duration": f"{recovery_duration}m", "target": "55-60%"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "50-55%"}],
            },
        ],
        "notes": [
            "Ride the work blocks like controlled race rehearsal, not threshold hero work.",
            "This is the right choice when the goal is stronger bike-to-run transfer.",
        ],
        "success": "Power stays metronomic and the work never compromises posture or breathing rhythm.",
        "cut_first": "Trim the second cruise block before touching the first.",
        "training_method": "Coggan",
        "coggan_level": "L4 Threshold",
        "expected_if": 0.77 if lighter_week else 0.8,
        "estimated_tss": 68 if lighter_week else 82,
        "variant_id": "ride_race_power_cruise",
    }


def _threshold_ride_primer_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    del duration, lighter_week
    warmup = 18
    cooldown = 10
    return {
        "id": "ride_threshold_primers",
        "name": "Threshold Primers Ride",
        "objective": "Preserve race-relevant sharpness while keeping total fatigue low.",
        "duration": 55,
        "intensity": "Short threshold work with full control and fast exit",
        "priority": "key",
        "primary_metric": "%FTP",
        "zone_focus": "priming",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "repeat": 2,
                "steps": [
                    {"duration": "6m", "target": "94-98%", "cadence": "88-92rpm"},
                    {"duration": "4m", "target": "55-60%"},
                ],
            },
            {
                "section": "Openers",
                "repeat": 3,
                "steps": [
                    {"duration": "1m", "target": "103-106%", "cadence": "95rpm"},
                    {"duration": "2m", "target": "55-60%"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "50-55%"}],
            },
        ],
        "notes": [
            "The goal is snap without residue.",
            "Finish feeling more ready to race or train hard again, not flattened.",
        ],
        "success": "The work feels crisp and the legs feel better at the end than mid-session.",
        "cut_first": "Drop the last opener before touching the threshold work.",
        "training_method": "Coggan",
        "coggan_level": "L4 Threshold",
        "expected_if": 0.76,
        "estimated_tss": 58,
        "variant_id": "ride_threshold_primers",
    }


def build_threshold_ride_session(
    day,
    duration: int,
    *,
    lighter_week: bool,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    default_payload = _threshold_ride_build_payload(duration, lighter_week=lighter_week)
    if not session_context:
        return _build_payload_session(day, "Ride", default_payload)

    distance = _priority_race_distance(session_context)
    goals = _goal_focuses(session_context)
    flags = _status_flags(session_context)
    phase = str(session_context.get("macro_phase", "")).lower()

    if "caution" in flags:
        variants = [
            _threshold_ride_cruise_payload(duration, lighter_week=True),
            default_payload,
        ]
    elif phase in {"race_specific", "taper"}:
        variants = [
            _threshold_ride_primer_payload(duration, lighter_week=lighter_week),
            _threshold_ride_cruise_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    elif distance == "70.3" or "run_off_bike" in goals:
        variants = [
            _threshold_ride_cruise_payload(duration, lighter_week=lighter_week),
            default_payload,
            _threshold_ride_over_under_payload(duration, lighter_week=lighter_week),
        ]
    elif distance == "olympic":
        variants = [
            _threshold_ride_over_under_payload(duration, lighter_week=lighter_week),
            default_payload,
            _threshold_ride_cruise_payload(duration, lighter_week=lighter_week),
        ]
    else:
        variants = [
            default_payload,
            _threshold_ride_over_under_payload(duration, lighter_week=lighter_week),
        ]

    payload = _pick_variant("threshold_ride", variants, session_context)
    return _build_payload_session(day, "Ride", payload)


def build_endurance_ride_session(day, duration: int, **_: Any) -> dict[str, Any]:
    warmup = 15
    cooldown = 10
    main = max(duration - warmup - cooldown, 30)
    actual = warmup + main + cooldown
    return _session(
        day,
        "Ride",
        "Endurance Ride",
        "Keep bike rhythm alive while letting fatigue come down.",
        actual,
        "Steady endurance only, no threshold work",
        "support",
        "%FTP",
        "endurance",
        [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-68%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "steps": [
                    {"duration": f"{main}m", "target": "65-72%", "cadence": "90-95rpm"}
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "50-55%"}],
            },
        ],
        [
            "Keep pressure low and cadence smooth.",
            "You should finish this feeling better than after any real key bike session.",
        ],
        "Power stays smooth, breathing stays easy, and the legs feel looser afterward.",
        "Shorten the main endurance block before touching the warmup or cooldown.",
        training_method="Coggan",
        coggan_level="L2 Endurance",
        expected_if=0.62,
        estimated_tss=48,
    )


def build_support_ride_session(day, duration: int, **_: Any) -> dict[str, Any]:
    warmup = 10
    repeat_count = 3 if duration >= 58 else 2
    recovery_between = 3
    cooldown = 10
    set_minutes = repeat_count * (8 + recovery_between)
    finish = max(duration - warmup - set_minutes - cooldown, 0)
    actual = warmup + set_minutes + finish + cooldown
    blocks: list[dict[str, Any]] = [
        {
            "section": "Warmup",
            "steps": [
                {"duration": f"{warmup}m", "target": "55-68%", "cadence": "90rpm"}
            ],
        },
        {
            "section": "Main Set",
            "repeat": repeat_count,
            "steps": [
                {"duration": "8m", "target": "78-84%", "cadence": "60-70rpm"},
                {"duration": f"{recovery_between}m", "target": "55-60%"},
            ],
        },
    ]
    if finish:
        blocks.append(
            {
                "section": "Finish",
                "steps": [
                    {"duration": f"{finish}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            }
        )
    blocks.append(
        {
            "section": "Cooldown",
            "steps": [{"duration": f"{cooldown}m", "target": "50-55%"}],
        }
    )
    return _session(
        day,
        "Ride",
        "Low-Cadence Strength Ride",
        "Add a medium bike touch without stealing the legs needed for the weekend.",
        actual,
        "Steady upper-end endurance with controlled torque, never a grind",
        "support",
        "%FTP",
        "strength endurance",
        blocks,
        [
            "Keep the torque work smooth and seated rather than muscling the pedals.",
            "Finish feeling switched on, not flattened for Saturday.",
        ],
        "The work feels sturdy and controlled, and the legs still feel ready for the weekend.",
        "Drop the final endurance block before removing a low-cadence repeat.",
        training_method="Coggan",
        coggan_level="L3 Tempo",
        expected_if=0.72,
        estimated_tss=55,
    )


def _club_swim_quality_payload(
    duration: int, *, lighter_week: bool, club_session_distance_m: int | None
) -> dict[str, Any]:
    warmup = 10 if duration >= 35 else 8
    repeat_count = max(2, min(4, max((duration - warmup - 3) // 7, 2)))
    cooldown = max(duration - warmup - (repeat_count * 7), 3)
    actual = warmup + (repeat_count * 7) + cooldown
    notes = [
        "Keep the leg demand low and the feel for the water high.",
        "Stay technically tidy rather than chasing the lane pace.",
    ]
    if club_session_distance_m:
        notes.append(f"Expect roughly {club_session_distance_m / 1000:g}k total.")
    return {
        "id": "club_swim_quality_density",
        "name": "Club Swim - Controlled Aerobic"
        if lighter_week
        else "Club Swim - Quality Density",
        "objective": "Preserve the club-swim routine and feel for the water without creating unnecessary fatigue.",
        "duration": actual,
        "intensity": "Controlled aerobic swim with clean technique",
        "priority": "support",
        "primary_metric": "notes",
        "zone_focus": "aerobic technique",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Easy swim + drills"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "6m", "target": "Smooth aerobic swimming"},
                    {"duration": "1m", "target": "Easy swim"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Easy swim"}],
            },
        ],
        "notes": notes,
        "success": "You leave the pool feeling smoother and better, not worked over.",
        "cut_first": "Reduce one main-set repeat before trimming the warmup or cooldown.",
        "training_method": "Friel",
        "variant_id": "club_swim_quality_density",
    }


def _club_swim_form_payload(
    duration: int, *, lighter_week: bool, club_session_distance_m: int | None
) -> dict[str, Any]:
    del lighter_week
    warmup = 10 if duration >= 35 else 8
    repeat_count = 3 if duration >= 50 else 2
    cooldown = max(duration - warmup - (repeat_count * 8), 4)
    actual = warmup + (repeat_count * 8) + cooldown
    notes = [
        "Use the fast lane to keep posture, breathing, and sighting rhythm tidy under pressure.",
        "Every few lengths, practice a low, quick sight rather than lifting the whole head.",
    ]
    if club_session_distance_m:
        notes.append(f"Expect roughly {club_session_distance_m / 1000:g}k total.")
    return {
        "id": "club_swim_form_pressure",
        "name": "Club Swim - Form Under Pressure",
        "objective": "Use the group swim to improve open-water transfer without adding useless fatigue.",
        "duration": actual,
        "intensity": "Mostly aerobic with short form-pressure segments",
        "priority": "support",
        "primary_metric": "notes",
        "zone_focus": "swim execution",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Easy swim + drills"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "5m", "target": "Strong aerobic swim with tidy form"},
                    {"duration": "1m", "target": "Easy swim + sighting cues"},
                    {"duration": "2m", "target": "Pull buoy or paddles, smooth catch"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Easy swim"}],
            },
        ],
        "notes": notes,
        "success": "Form stays compact and breathing stays calm even when the lane pace rises.",
        "cut_first": "Drop one form-pressure repeat before trimming the warmup or cooldown.",
        "training_method": "Friel",
        "variant_id": "club_swim_form_pressure",
    }


def _pool_swim_technique_payload(duration: int) -> dict[str, Any]:
    warmup = 6
    repeat_count = max(2, min(3, max((duration - warmup - 4) // 6, 2)))
    cooldown = max(duration - warmup - (repeat_count * 6), 4)
    actual = warmup + (repeat_count * 6) + cooldown
    return {
        "id": "pool_swim_technique_support",
        "name": "Pool Swim - Technique Support",
        "objective": "Use the fixed pool-access day to keep swim continuity with almost no extra training cost.",
        "duration": actual,
        "intensity": "Easy technique support",
        "priority": "support",
        "primary_metric": "notes",
        "zone_focus": "aerobic technique",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Easy swim + drills"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "5m", "target": "Smooth aerobic swimming"},
                    {"duration": "1m", "target": "Easy swim"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Easy swim"}],
            },
        ],
        "notes": [
            "Use the pool to restore posture and rhythm, not to chase splits.",
            "Keep this easy enough that the other session of the day stays primary.",
        ],
        "success": "You finish looser through the shoulders and no more tired in the legs.",
        "cut_first": "Drop one main-set repeat before reducing the warmup or cooldown.",
        "training_method": "Friel",
        "variant_id": "pool_swim_technique_support",
    }


def _pool_swim_skills_payload(duration: int) -> dict[str, Any]:
    warmup = 6
    repeat_count = 3 if duration >= 30 else 2
    cooldown = max(duration - warmup - (repeat_count * 6), 4)
    actual = warmup + (repeat_count * 6) + cooldown
    return {
        "id": "pool_swim_open_water_skills",
        "name": "Pool Swim - Open-Water Skills Support",
        "objective": "Use the easy pool day to reinforce relaxed breathing, posture, and open-water transfer.",
        "duration": actual,
        "intensity": "Easy skill-oriented swim",
        "priority": "support",
        "primary_metric": "notes",
        "zone_focus": "swim execution",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Easy swim + drills"}],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "4m", "target": "Smooth swim with relaxed exhale"},
                    {"duration": "1m", "target": "Easy swim + sighting cue"},
                    {"duration": "1m", "target": "Pull buoy, tall posture"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Easy swim"}],
            },
        ],
        "notes": [
            "Practice calm breathing and a low, quick sight rather than muscling the water.",
            "The point is transfer to race execution, not accumulating swim stress.",
        ],
        "success": "Breathing stays relaxed and the stroke stays long even when you add the skill cues.",
        "cut_first": "Drop one skill repeat before trimming the warmup or cooldown.",
        "training_method": "Friel",
        "variant_id": "pool_swim_open_water_skills",
    }


def build_club_swim_session(
    day,
    duration: int,
    *,
    lighter_week: bool,
    club_session_distance_m: int | None = None,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    default_payload = _club_swim_quality_payload(
        duration,
        lighter_week=lighter_week,
        club_session_distance_m=club_session_distance_m,
    )
    if not session_context:
        return _build_payload_session(day, "Swim", default_payload)

    goals = _goal_focuses(session_context)
    flags = _status_flags(session_context)
    phase = str(session_context.get("macro_phase", "")).lower()
    if "swim_execution" in goals and "caution" not in flags and phase != "taper":
        variants = [
            _club_swim_form_payload(
                duration,
                lighter_week=lighter_week,
                club_session_distance_m=club_session_distance_m,
            ),
            default_payload,
        ]
    else:
        variants = [default_payload]
    payload = _pick_variant("swim_club", variants, session_context)
    return _build_payload_session(day, "Swim", payload)


def build_pool_swim_session(
    day,
    duration: int,
    *,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    default_payload = _pool_swim_technique_payload(duration)
    if not session_context:
        return _build_payload_session(day, "Swim", default_payload)

    goals = _goal_focuses(session_context)
    flags = _status_flags(session_context)
    variants = [default_payload]
    if "swim_execution" in goals and "caution" not in flags:
        variants = [_pool_swim_skills_payload(duration), default_payload]

    payload = _pick_variant("swim_pool", variants, session_context)
    return _build_payload_session(day, "Swim", payload)


def _strength_durability_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    if duration <= 25 or lighter_week:
        actual = max(20, duration)
        repeat_count = 1 if actual <= 20 else 2
        warmup_steps = [
            {"duration": "5m", "target": "Mobility, calf prep, glute activation"}
        ]
        main_steps = [
            {"duration": "5 reps", "target": "Split squat each side @ RPE 6"},
            {"duration": "5 reps", "target": "Single-leg RDL each side @ RPE 6"},
            {"duration": "20s", "target": "Side plank each side"},
        ]
    else:
        actual = max(30, duration)
        repeat_count = 2
        warmup_steps = [
            {"duration": "8m", "target": "Mobility, calf prep, glute activation"}
        ]
        main_steps = [
            {"duration": "6 reps", "target": "Split squat each side @ RPE 6"},
            {"duration": "6 reps", "target": "Single-leg RDL each side @ RPE 6"},
            {"duration": "8 reps", "target": "Seated row @ RPE 6"},
            {"duration": "30s", "target": "Side plank each side"},
        ]
    return {
        "id": "strength_durability",
        "name": "Gym Durability Session",
        "objective": "Support tissue resilience and posture under load without burying the legs before the weekend.",
        "duration": actual,
        "intensity": "Moderate lifting only, no grinders",
        "priority": "support",
        "primary_metric": "notes",
        "zone_focus": "strength",
        "blocks": [
            {"section": "Warmup", "steps": warmup_steps},
            {"section": "Main Set", "repeat": repeat_count, "steps": main_steps},
            {
                "section": "Finish",
                "steps": [{"duration": "5m", "target": "Calf and hip mobility"}],
            },
        ],
        "notes": [
            "Leave at least two reps in reserve on every loaded movement.",
            "The goal is freshness plus durability, not gym fatigue.",
        ],
        "success": "You leave the gym feeling switched on rather than heavy-legged.",
        "cut_first": "Drop one round of the main set before dropping mobility.",
        "training_method": "Friel",
        "variant_id": "strength_durability",
    }


def _strength_trunk_payload(duration: int, *, lighter_week: bool) -> dict[str, Any]:
    actual = max(25, 30 if lighter_week else duration)
    repeat_count = 2 if actual >= 25 else 1
    return {
        "id": "strength_trunk_stability",
        "name": "Trunk Stability + Durability",
        "objective": "Support lower-back comfort, hip control, and run durability without lingering soreness.",
        "duration": actual,
        "intensity": "Controlled durability work only, never a grind",
        "priority": "support",
        "primary_metric": "notes",
        "zone_focus": "mobility and stability",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {
                        "duration": "6m",
                        "target": "Breathing reset, hip mobility, glute activation",
                    }
                ],
            },
            {
                "section": "Main Set",
                "repeat": repeat_count,
                "steps": [
                    {
                        "duration": "6 reps",
                        "target": "Goblet split squat each side @ RPE 5-6",
                    },
                    {
                        "duration": "6 reps",
                        "target": "Single-leg hinge each side @ RPE 5-6",
                    },
                    {"duration": "30s", "target": "Dead bug or hollow hold"},
                    {"duration": "20s", "target": "Side plank each side"},
                ],
            },
            {
                "section": "Finish",
                "steps": [{"duration": "5m", "target": "Thoracic and hip mobility"}],
            },
        ],
        "notes": [
            "Move crisply and leave the session feeling more organized, not more tired.",
            "Bias control, trunk stiffness, and hip position over loading.",
        ],
        "success": "Posture feels better afterward and nothing in the back or hips feels stirred up.",
        "cut_first": "Drop the final round before cutting the warmup or mobility finish.",
        "training_method": "Friel",
        "variant_id": "strength_trunk_stability",
    }


def build_strength_session(
    day,
    duration: int,
    *,
    lighter_week: bool,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    default_payload = _strength_durability_payload(duration, lighter_week=lighter_week)
    if not session_context:
        return _build_payload_session(day, "WeightTraining", default_payload)

    flags = _status_flags(session_context)
    variants = [default_payload]
    if "mobility_bias" in flags or "sleep_limited" in flags:
        variants = [
            _strength_trunk_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]

    payload = _pick_variant("strength", variants, session_context)
    return _build_payload_session(day, "WeightTraining", payload)


def _long_ride_recovery_payload(duration: int) -> dict[str, Any]:
    warmup = 20
    finish = 25 if duration >= 145 else 20
    main = max(duration - warmup - finish, 60)
    actual = warmup + main + finish
    return {
        "id": "long_ride_endurance_recovery",
        "name": "Long Endurance Ride",
        "objective": "Use available weekend time for durable aerobic work without sneaking intensity back in.",
        "duration": actual,
        "intensity": "Controlled endurance only, no race-pace work",
        "priority": "support",
        "primary_metric": "%FTP",
        "zone_focus": "endurance",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "steps": [
                    {"duration": f"{main}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Cooldown",
                "steps": [
                    {"duration": f"{finish}m", "target": "55-65%", "cadence": "90rpm"}
                ],
            },
        ],
        "notes": [
            "Stay disciplined and keep it all aerobic.",
            "The goal is time in zone two, not race-rhythm work.",
        ],
        "success": "Heart rate and power stay controlled throughout and you finish with good posture and fuel control.",
        "cut_first": "Shorten the final endurance block before touching the main endurance block.",
        "training_method": "Coggan",
        "coggan_level": "L2 Endurance",
        "expected_if": 0.64,
        "estimated_tss": 102,
        "variant_id": "long_ride_endurance_recovery",
    }


def _long_ride_race_rhythm_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    warmup = 20
    finish = 25 if duration >= 145 else 20
    repeat_count = 1 if lighter_week else 2
    while repeat_count > 0 and duration - (warmup + finish + (repeat_count * 25)) < 40:
        repeat_count -= 1
    reserved = warmup + finish + (repeat_count * 25)
    main = max(duration - reserved, 30)
    actual = warmup + finish + main + (repeat_count * 25)
    return {
        "id": "long_ride_race_rhythm",
        "name": "Long Ride with Race Rhythm",
        "objective": "Build endurance while rehearsing the controlled race power that matters most right now.",
        "duration": actual,
        "intensity": "Mostly endurance with short race-rhythm blocks",
        "priority": "support" if lighter_week else "key",
        "primary_metric": "%FTP",
        "zone_focus": "endurance plus race rhythm",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "steps": [
                    {"duration": f"{main}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Race Rhythm",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "15m", "target": "85-90%", "cadence": "88-92rpm"},
                    {"duration": "10m", "target": "60-65%"},
                ],
            },
            {
                "section": "Finish",
                "steps": [
                    {"duration": f"{finish}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
        ],
        "notes": [
            "Keep fueling from the first hour onward.",
            "The race-rhythm blocks should feel composed, not forced.",
        ],
        "success": "You complete the race-rhythm work without drifting above target.",
        "cut_first": "Shorten the final endurance block first.",
        "training_method": "Coggan",
        "coggan_level": "L2 Endurance",
        "expected_if": 0.69 if lighter_week else 0.71,
        "estimated_tss": 95 if lighter_week else 118,
        "variant_id": "long_ride_race_rhythm",
    }


def _long_ride_fueling_payload(duration: int, *, lighter_week: bool) -> dict[str, Any]:
    warmup = 20
    finish = 20
    repeat_count = 1 if lighter_week else 2
    block_size = 35
    while (
        repeat_count > 1
        and duration - (warmup + finish + repeat_count * block_size) < 45
    ):
        repeat_count -= 1
    main = max(duration - warmup - finish - repeat_count * block_size, 40)
    actual = warmup + main + finish + repeat_count * block_size
    return {
        "id": "long_ride_fueling_simulation",
        "name": "Long Ride - Fueling Simulation",
        "objective": "Build 70.3-specific bike durability while rehearsing steady fueling and controlled output.",
        "duration": actual,
        "intensity": "Endurance with long controlled race-power blocks",
        "priority": "support" if lighter_week else "key",
        "primary_metric": "%FTP",
        "zone_focus": "durability and fueling",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "steps": [
                    {"duration": f"{main}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Fueling Block",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "20m", "target": "80-85%", "cadence": "88-92rpm"},
                    {"duration": "15m", "target": "65-70%", "cadence": "90rpm"},
                ],
            },
            {
                "section": "Finish",
                "steps": [
                    {"duration": f"{finish}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
        ],
        "notes": [
            "Treat fueling as part of the workout, not as an afterthought.",
            "Hold the race-power work at a level that still leaves a run in the legs.",
        ],
        "success": "Fueling stays on schedule and power stays controlled instead of drifting upward.",
        "cut_first": "Trim the final endurance block before cutting a fueling block.",
        "training_method": "Coggan",
        "coggan_level": "L2 Endurance",
        "expected_if": 0.68 if lighter_week else 0.7,
        "estimated_tss": 92 if lighter_week else 116,
        "variant_id": "long_ride_fueling_simulation",
    }


def _long_ride_olympic_payload(duration: int, *, lighter_week: bool) -> dict[str, Any]:
    warmup = 20
    finish = 20
    repeat_count = 2 if lighter_week else 3
    block_size = 20
    while (
        repeat_count > 2
        and duration - (warmup + finish + repeat_count * block_size) < 35
    ):
        repeat_count -= 1
    main = max(duration - warmup - finish - repeat_count * block_size, 35)
    actual = warmup + main + finish + repeat_count * block_size
    return {
        "id": "long_ride_olympic_blocks",
        "name": "Long Ride with Olympic Build Blocks",
        "objective": "Keep endurance growing while layering in olympic-distance race control.",
        "duration": actual,
        "intensity": "Endurance with repeated controlled race-power blocks",
        "priority": "support" if lighter_week else "key",
        "primary_metric": "%FTP",
        "zone_focus": "endurance plus olympic specificity",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [
                    {"duration": f"{warmup}m", "target": "55-70%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Main Set",
                "steps": [
                    {"duration": f"{main}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
            {
                "section": "Build Block",
                "repeat": repeat_count,
                "steps": [
                    {"duration": "12m", "target": "88-92%", "cadence": "88-92rpm"},
                    {"duration": "8m", "target": "60-65%"},
                ],
            },
            {
                "section": "Finish",
                "steps": [
                    {"duration": f"{finish}m", "target": "65-72%", "cadence": "90rpm"}
                ],
            },
        ],
        "notes": [
            "The build blocks should feel strong but still rhythmical.",
            "Stay aero and composed rather than turning this into a threshold smash-fest.",
        ],
        "success": "You land each build block without overshooting and still finish the ride controlled.",
        "cut_first": "Drop the last build block before trimming the endurance backbone.",
        "training_method": "Coggan",
        "coggan_level": "L2 Endurance",
        "expected_if": 0.7 if lighter_week else 0.72,
        "estimated_tss": 98 if lighter_week else 120,
        "variant_id": "long_ride_olympic_blocks",
    }


def build_long_ride_session(
    day,
    duration: int,
    *,
    lighter_week: bool,
    recovery_week: bool,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    if recovery_week:
        return _build_payload_session(
            day, "Ride", _long_ride_recovery_payload(duration)
        )

    default_payload = _long_ride_race_rhythm_payload(
        duration, lighter_week=lighter_week
    )
    if not session_context:
        return _build_payload_session(day, "Ride", default_payload)

    distance = _priority_race_distance(session_context)
    goals = _goal_focuses(session_context)
    flags = _status_flags(session_context)
    if "caution" in flags:
        variants = [default_payload]
    elif distance == "70.3" or "durability" in goals or "run_off_bike" in goals:
        variants = [
            _long_ride_fueling_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    elif distance == "olympic":
        variants = [
            _long_ride_olympic_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    else:
        variants = [default_payload]

    payload = _pick_variant("long_ride", variants, session_context)
    return _build_payload_session(day, "Ride", payload)


def _long_run_aerobic_payload(
    duration: int, *, lighter_week: bool, recovery_week: bool
) -> dict[str, Any]:
    warmup = 10
    cooldown = 10
    main = max(duration - warmup - cooldown, 40)
    actual = warmup + main + cooldown
    return {
        "id": "long_run_aerobic",
        "name": "Long Easy Run" if recovery_week else "Long Aerobic Run",
        "objective": "Keep the long-run routine and aerobic durability while staying absorbable for the whole week.",
        "duration": actual,
        "intensity": "Steady aerobic control throughout",
        "priority": "support" if lighter_week else "key",
        "primary_metric": "HR",
        "zone_focus": "aerobic durability",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z1 HR"}],
            },
            {
                "section": "Main Set",
                "steps": [{"duration": f"{main}m", "target": "Z2 HR"}],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 HR"}],
            },
        ],
        "notes": [
            "Flat to gently rolling route only.",
            "Stay disciplined with heart rate from the first 20 minutes.",
        ],
        "success": "The whole run stays aerobic and you feel normal again later in the day.",
        "cut_first": "Shorten the main block by 10-15 minutes before trimming the warmup or cooldown.",
        "training_method": "Daniels",
        "daniels_type": "E",
        "variant_id": "long_run_aerobic",
    }


def _long_run_steady_finish_payload(
    duration: int, *, lighter_week: bool
) -> dict[str, Any]:
    warmup = 10
    cooldown = 10
    finish = 10 if lighter_week else 15
    main = max(duration - warmup - cooldown - finish, 35)
    actual = warmup + main + finish + cooldown
    return {
        "id": "long_run_steady_finish",
        "name": "Long Run with Steady Finish",
        "objective": "Build durable run economy under light fatigue without turning the long run into a race.",
        "duration": actual,
        "intensity": "Mostly aerobic with a controlled steady finish",
        "priority": "support" if lighter_week else "key",
        "primary_metric": "HR",
        "zone_focus": "aerobic durability with finish control",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z1 HR"}],
            },
            {
                "section": "Main Set",
                "steps": [{"duration": f"{main}m", "target": "Z2 HR"}],
            },
            {
                "section": "Steady Finish",
                "steps": [{"duration": f"{finish}m", "target": "Upper Z2 HR"}],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 HR"}],
            },
        ],
        "notes": [
            "Only lift the effort if the whole run has stayed comfortable up to that point.",
            "The finish should feel controlled and tall, not like a late race.",
        ],
        "success": "The final steady block feels composed and you still recover normally later in the day.",
        "cut_first": "Remove the steady finish before shortening the aerobic backbone.",
        "training_method": "Daniels",
        "daniels_type": "E",
        "variant_id": "long_run_steady_finish",
    }


def _long_run_strides_payload(duration: int) -> dict[str, Any]:
    warmup = 10
    cooldown = 10
    strides = 12
    main = max(duration - warmup - cooldown - strides, 35)
    actual = warmup + main + strides + cooldown
    return {
        "id": "long_run_strides",
        "name": "Long Aerobic Run + Strides",
        "objective": "Keep long-run durability while touching a little coordination and leg speed.",
        "duration": actual,
        "intensity": "Aerobic throughout with brief relaxed strides late",
        "priority": "key",
        "primary_metric": "HR",
        "zone_focus": "aerobic durability and coordination",
        "blocks": [
            {
                "section": "Warmup",
                "steps": [{"duration": f"{warmup}m", "target": "Z1 HR"}],
            },
            {
                "section": "Main Set",
                "steps": [{"duration": f"{main}m", "target": "Z2 HR"}],
            },
            {
                "section": "Strides",
                "repeat": 6,
                "steps": [
                    {"duration": "20s", "target": "Fast relaxed strides"},
                    {"duration": "100s", "target": "Easy jog"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": f"{cooldown}m", "target": "Z1 HR"}],
            },
        ],
        "notes": [
            "Keep the strides smooth and relaxed rather than sprinty.",
            "If the long run has felt heavy, skip the strides and keep it purely aerobic.",
        ],
        "success": "The aerobic work stays easy and the strides add snap without residual fatigue.",
        "cut_first": "Skip the strides before shortening the aerobic run.",
        "training_method": "Daniels",
        "daniels_type": "E",
        "variant_id": "long_run_strides",
    }


def build_long_run_session(
    day,
    duration: int,
    *,
    lighter_week: bool,
    recovery_week: bool,
    session_context: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    default_payload = _long_run_aerobic_payload(
        duration,
        lighter_week=lighter_week,
        recovery_week=recovery_week,
    )
    if recovery_week or not session_context:
        return _build_payload_session(day, "Run", default_payload)

    distance = _priority_race_distance(session_context)
    goals = _goal_focuses(session_context)
    flags = _status_flags(session_context)
    phase = str(session_context.get("macro_phase", "")).lower()

    if "caution" in flags:
        variants = [default_payload]
    elif distance == "70.3" or "run_off_bike" in goals:
        variants = [
            _long_run_steady_finish_payload(duration, lighter_week=lighter_week),
            default_payload,
        ]
    elif phase in {"race_specific", "taper"} and distance == "olympic":
        variants = [_long_run_strides_payload(duration), default_payload]
    else:
        variants = [default_payload]

    payload = _pick_variant("long_run", variants, session_context)
    return _build_payload_session(day, "Run", payload)


SESSION_LIBRARY: dict[str, dict[str, Any]] = {
    "recovery": {
        "discipline": "Recovery",
        "reduction_priority": 8,
        "builder": build_recovery_session,
    },
    "easy_run": {
        "discipline": "Run",
        "reduction_priority": 1,
        "builder": build_easy_run_session,
    },
    "quality_run": {
        "discipline": "Run",
        "reduction_priority": 3,
        "builder": build_quality_run_session,
    },
    "threshold_ride": {
        "discipline": "Ride",
        "reduction_priority": 5,
        "builder": build_threshold_ride_session,
    },
    "endurance_ride": {
        "discipline": "Ride",
        "reduction_priority": 4,
        "builder": build_endurance_ride_session,
    },
    "support_ride": {
        "discipline": "Ride",
        "reduction_priority": 3,
        "builder": build_support_ride_session,
    },
    "swim_club": {
        "discipline": "Swim",
        "reduction_priority": 0,
        "builder": build_club_swim_session,
    },
    "swim_pool": {
        "discipline": "Swim",
        "reduction_priority": 0,
        "builder": build_pool_swim_session,
    },
    "strength": {
        "discipline": "WeightTraining",
        "reduction_priority": 2,
        "builder": build_strength_session,
    },
    "long_ride": {
        "discipline": "Ride",
        "reduction_priority": 7,
        "builder": build_long_ride_session,
    },
    "long_run": {
        "discipline": "Run",
        "reduction_priority": 6,
        "builder": build_long_run_session,
    },
}


def reduction_priority(kind: str) -> int:
    return int(SESSION_LIBRARY.get(kind, {}).get("reduction_priority", 9))


def build_session(
    kind: str,
    day,
    duration: int,
    *,
    lighter_week: bool,
    recovery_week: bool,
    **kwargs: Any,
) -> dict[str, Any]:
    if kind not in SESSION_LIBRARY:
        raise ValueError(f"Unsupported session archetype: {kind}")
    builder = SESSION_LIBRARY[kind]["builder"]
    return builder(
        day,
        duration,
        lighter_week=lighter_week,
        recovery_week=recovery_week,
        **kwargs,
    )
