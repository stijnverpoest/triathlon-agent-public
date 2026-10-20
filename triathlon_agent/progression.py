"""Progression principles for quality work.

There is no session library: the head coach designs every session. This module
only holds the *principles* the coach progresses along (dose ladders expressed
as time-in-zone) and reads what was actually prescribed and completed in prior
approved weeks, so each key session can build on the last one instead of
repeating it. `prompts/training_intensity_doctrine.md` is the prose version.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .coaching_limits import DOSE_GAP_STEP_BACK as COACHING_DOSE_GAP_STEP_BACK
from .race_selection import distance_bucket


# Each rung is (repeats, minutes per repeat). Rungs never fall in total time-in-zone
# by roughly 10-25% per step, the usual coaching progression for interval
# work (Coggan/Allen, Daniels, Friel). Recovery is a fraction of the work rep.
LADDERS: dict[str, dict[str, Any]] = {
    "bike_sweet_spot": {
        "label": "Sweet spot / 70.3 race power",
        "target": "88-93% FTP",
        "recovery_ratio": 0.25,
        "rungs": [(3, 10), (3, 12), (2, 20), (3, 15), (2, 25), (3, 20), (2, 30), (2, 40)],
        "coggan_level": "L3 Tempo",
    },
    "bike_threshold": {
        "label": "Threshold (FTP) intervals",
        "target": "95-100% FTP",
        "recovery_ratio": 0.4,
        "rungs": [(3, 8), (4, 8), (3, 12), (4, 10), (2, 20), (3, 15), (3, 20)],
        "coggan_level": "L4 Threshold",
    },
    "bike_vo2": {
        "label": "VO2max intervals",
        "target": "106-120% FTP",
        "recovery_ratio": 1.0,
        "rungs": [(5, 3), (6, 3), (5, 4), (6, 4), (5, 5)],
        "coggan_level": "L5 VO2max",
    },
    "run_threshold": {
        "label": "Daniels T (threshold / cruise intervals)",
        "target": "98-102% Pace",
        "recovery_ratio": 0.2,
        "rungs": [(4, 5), (5, 5), (3, 10), (3, 12), (4, 10), (3, 15), (2, 25)],
        "daniels_type": "T",
    },
    "run_interval": {
        "label": "Daniels I (VO2max intervals)",
        "target": "106-110% Pace",
        "recovery_ratio": 0.8,
        "rungs": [(5, 3), (6, 3), (5, 4), (4, 5), (5, 5)],
        "daniels_type": "I",
    },
    "run_race_pace": {
        "label": "Race-pace / steady running (HM, 70.3 run, marathon)",
        "target": "90-96% Pace",
        "recovery_ratio": 0.1,
        "rungs": [(2, 10), (3, 10), (2, 15), (3, 12), (2, 20), (3, 15), (2, 25)],
        "daniels_type": "T",
    },
    "run_uphill_threshold": {
        "label": "Uphill threshold (trail / ultra)",
        "target": "Z4 HR",
        "recovery_ratio": 0.5,
        "rungs": [(4, 6), (5, 6), (4, 8), (3, 12), (5, 8), (4, 12)],
        "daniels_type": "T",
    },
}

# Which ladders drive a priority-race distance, in order of importance.
FOCUS_BY_DISTANCE: dict[str, list[str]] = {
    "70.3": ["bike_sweet_spot", "run_threshold", "bike_threshold", "bike_vo2", "run_race_pace"],
    "ironman": ["bike_sweet_spot", "run_race_pace", "bike_threshold", "run_threshold"],
    "olympic": ["bike_threshold", "run_threshold", "bike_vo2", "run_interval"],
    "sprint": ["bike_vo2", "run_interval", "bike_threshold", "run_threshold"],
    "5k": ["run_interval", "run_threshold", "bike_vo2", "bike_threshold"],
    "10k": ["run_threshold", "run_interval", "run_race_pace", "bike_threshold"],
    "half_marathon": ["run_threshold", "run_interval", "run_race_pace", "bike_sweet_spot"],
    "marathon": ["run_race_pace", "run_threshold", "run_interval", "bike_sweet_spot"],
    # Shorter trail (< 42 km): climbing strength plus more speed than an ultra.
    # The long run with climbing is the weekend durability session, not a ladder.
    "trail": ["run_uphill_threshold", "run_threshold", "run_interval", "bike_sweet_spot"],
    "ultra_trail": ["run_uphill_threshold", "run_threshold", "bike_sweet_spot", "run_interval"],
    "generic": ["bike_threshold", "run_threshold", "bike_vo2", "run_interval"],
}

VO2_FAMILIES = {"bike_vo2", "run_interval"}
# Phase- and race-aware focus. The trail/ultra demand changes through the season:
# base = aerobic + threshold (+ strides in easy runs), build = climbing strength
# plus a 3-4 week I block, race-specific = uphill threshold while the long run
# carries race effort on climbs with progressive descending.
FOCUS_BY_PHASE: dict[str, dict[str, list[str]]] = {
    "ultra_trail": {
        "base": ["run_threshold", "bike_sweet_spot", "run_uphill_threshold"],
        "build": ["run_uphill_threshold", "bike_sweet_spot", "run_threshold", "bike_threshold"],
        "race_specific": ["run_uphill_threshold", "bike_sweet_spot", "bike_threshold"],
        "taper": ["run_uphill_threshold", "bike_sweet_spot"],
    },
    "trail": {
        "base": ["run_threshold", "bike_sweet_spot", "run_uphill_threshold"],
        "build": ["run_uphill_threshold", "run_threshold", "bike_sweet_spot", "bike_threshold"],
        "race_specific": ["run_uphill_threshold", "run_threshold", "bike_sweet_spot"],
        "taper": ["run_uphill_threshold", "run_threshold"],
    },
}
# The build phase opens with a short VO2max (Daniels I) block of this many load weeks.
I_BLOCK_WEEKS = 4

PHASE_START_RUNG = {
    "base": 0,
    "build": 2,
    "race_specific": 3,
    "taper": 2,
    "transition": 0,
}
# Detraining guard: (days without a dose, rungs to step back), longest gap first;
# None restarts at the phase entry rung.
DOSE_GAP_STEP_BACK = COACHING_DOSE_GAP_STEP_BACK
# A partial session counts as completed only from this executed fraction on.
PARTIAL_COMPLETED_FRACTION = 0.9
PARTIAL_STATUSES = {"partial", "shortened"}
# Session text that marks threshold work as climbing (uphill ladder, not flat T).
UPHILL_TOKENS = ("uphill", "hill", "climb", "incline", "heuvel", "helling")

PERCENT_RE = re.compile(r"(\d+)(?:\s*-\s*(\d+))?\s*%")
HR_ZONE_RE = re.compile(r"Z(\d)\s*HR", re.IGNORECASE)
DURATION_RE = re.compile(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?")


def race_distance_key(*texts: Any) -> str:
    """Ladder-focus key for a race; shares the buckets of `race_selection`."""
    bucket = distance_bucket(*texts)
    if bucket in FOCUS_BY_DISTANCE:
        return bucket
    text = " ".join(str(value or "") for value in texts).lower()
    if re.search(r"\bhm\b", text):
        return "half_marathon"
    return "generic"


def is_uphill_context(*texts: Any) -> bool:
    text = " ".join(str(value or "") for value in texts).lower()
    return any(token in text for token in UPHILL_TOKENS)


def _step_minutes(duration: Any) -> float:
    match = DURATION_RE.fullmatch(str(duration or "").strip())
    if not match or not any(match.groups()):
        return 0.0
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return hours * 60 + minutes + seconds / 60


def classify_target(discipline: str, target: Any, context: str = "") -> str | None:
    """Map a step target to a ladder family, or None for easy/recovery steps.

    `context` is the surrounding session text (name, focus, notes, block): run
    threshold work on a climb belongs to the uphill ladder, not the flat T ladder.
    """
    text = str(target or "")
    uphill = discipline == "Run" and is_uphill_context(context, text)
    zone = HR_ZONE_RE.search(text)
    if zone:
        level = int(zone.group(1))
        if discipline == "Run":
            if level >= 5:
                return "run_interval"
            if level == 4:
                return "run_uphill_threshold" if uphill else "run_threshold"
        if discipline == "Ride" and level >= 4:
            return "bike_threshold"
        return None
    match = PERCENT_RE.search(text)
    if not match:
        return None
    upper = int(match.group(2) or match.group(1))
    if discipline == "Ride":
        if upper >= 106:
            return "bike_vo2"
        if upper >= 95:
            return "bike_threshold"
        if upper >= 86:
            return "bike_sweet_spot"
        return None
    if discipline == "Run" and "pace" in text.lower():
        if upper >= 104:
            return "run_interval"
        if upper >= 97:
            return "run_uphill_threshold" if uphill else "run_threshold"
        if upper >= 90:
            return "run_race_pace"
    return None


def session_context(session: dict[str, Any]) -> str:
    """Free text around a session that tells what kind of work it is."""
    notes = session.get("execution_notes") or []
    if isinstance(notes, str):
        notes = [notes]
    tags = session.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]
    return " ".join(
        str(value or "")
        for value in (
            session.get("session_name"),
            session.get("zone_focus"),
            session.get("objective"),
            *notes,
            *tags,
        )
    )


def session_dose(session: dict[str, Any]) -> dict[str, float]:
    """Minutes per ladder family prescribed in a session.

    A declared `progression` object wins: its family and time-in-zone are what
    the coach prescribed, even when step targets were rescaled (stale FTP).
    Otherwise the blocks are read, with the session text as context.
    """
    dose: dict[str, float] = {}
    if session.get("threshold_test"):
        return dose
    progression = session.get("progression")
    if isinstance(progression, dict):
        family = progression.get("family")
        minutes = progression.get("time_in_zone_min")
        if family in LADDERS and minutes not in (None, ""):
            value = float(minutes)
            return {family: round(value, 1)} if value >= 4 else {}
    discipline = str(session.get("discipline") or "")
    context = session_context(session)
    for block in session.get("blocks", []) or []:
        repeat = int(block.get("repeat") or 1)
        block_context = f"{context} {block.get('section') or ''}"
        for step in block.get("steps", []) or []:
            family = classify_target(discipline, step.get("target"), block_context)
            if family is None:
                continue
            dose[family] = dose.get(family, 0.0) + repeat * _step_minutes(step.get("duration"))
    return {key: round(value, 1) for key, value in dose.items() if value >= 4}


def _review(
    session: dict[str, Any], reviews: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for review in reviews:
        if review.get("session_name") == session.get("session_name") and review.get(
            "date"
        ) == session.get("date"):
            return review
    return None


def _review_status(
    session: dict[str, Any], reviews: list[dict[str, Any]]
) -> str | None:
    review = _review(session, reviews)
    return review.get("status") if review else None


def _executed_fraction(review: dict[str, Any] | None) -> float | None:
    if not review:
        return None
    for key in ("completion_ratio", "pragmatic_ratio", "executed_fraction"):
        value = review.get(key)
        try:
            if value not in (None, ""):
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return None


def _declared_rung(session: dict[str, Any], family: str) -> int | None:
    progression = session.get("progression")
    if not isinstance(progression, dict) or progression.get("family") != family:
        return None
    try:
        rung = int(progression.get("rung"))
    except (TypeError, ValueError):
        return None
    top = len(LADDERS[family]["rungs"]) - 1
    return rung if 0 <= rung <= top else None


def quality_history(
    prior_plan_contexts: list[dict[str, Any]],
    prior_plan_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every prescribed quality dose with what was actually executed.

    `completed` means the planned dose was done (status completed*, or a
    partial/shortened session with >= 90% executed). A partial session below
    that is not completed: the rung is repeated, never progressed from.
    """
    reviews = [
        review
        for week in prior_plan_history
        for review in week.get("session_reviews", []) or []
    ]
    history = []
    for context in prior_plan_contexts:
        for session in (context.get("final_plan") or {}).get("sessions", []) or []:
            dose = session_dose(session)
            if not dose:
                continue
            review = _review(session, reviews)
            status = review.get("status") if review else None
            fraction = _executed_fraction(review)
            partial = status in PARTIAL_STATUSES
            if partial:
                completed = fraction is not None and fraction >= PARTIAL_COMPLETED_FRACTION
            else:
                completed = status is not None and str(status).startswith("completed")
            rungs = {}
            for family in dose:
                rung = _declared_rung(session, family)
                if rung is not None:
                    rungs[family] = rung
            history.append(
                {
                    "date": session.get("date"),
                    "discipline": session.get("discipline"),
                    "session_name": session.get("session_name"),
                    "dose_min_by_family": dose,
                    "rung_by_family": rungs,
                    "review_status": status,
                    "executed_fraction": fraction,
                    "partial": partial and not completed,
                    "completed": completed,
                }
            )
    history.sort(key=lambda item: str(item.get("date") or ""))
    return history


def _rung_at_or_above(family: str, minutes: float) -> int:
    rungs = LADDERS[family]["rungs"]
    for index, (reps, rep_min) in enumerate(rungs):
        if reps * rep_min >= minutes - 0.5:
            return index
    return len(rungs) - 1


def _rung_for_minutes(family: str, minutes: float) -> int:
    """Highest rung whose time-in-zone does not exceed the dose (minutes fallback).

    With equal-total rungs (e.g. 4x10 and 2x20) the later one is taken, so a
    minutes-only history never gets stuck on the first of the pair.
    """
    best = 0
    for index, (reps, rep_min) in enumerate(LADDERS[family]["rungs"]):
        if reps * rep_min <= minutes + 0.5:
            best = index
    return best


def current_rung(family: str, last: dict[str, Any] | None) -> int | None:
    """Rung of the last dose: the recorded rung index wins, minutes are the fallback."""
    if not last:
        return None
    top = len(LADDERS[family]["rungs"]) - 1
    try:
        rung = int(last.get("rung"))
        if 0 <= rung <= top:
            return rung
    except (TypeError, ValueError):
        pass
    return _rung_for_minutes(family, float(last.get("minutes") or 0))


def last_dose_by_family(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest dose per family; a later miss or a partial session means: repeat."""
    latest: dict[str, dict[str, Any]] = {}
    for item in history:
        for family, minutes in item["dose_min_by_family"].items():
            rung = (item.get("rung_by_family") or {}).get(family)
            if item.get("completed"):
                latest[family] = {
                    "date": item.get("date"),
                    "session_name": item.get("session_name"),
                    "minutes": minutes,
                    "rung": rung,
                    "completed": True,
                    "missed_since": [],
                }
            elif item.get("partial"):
                # Executed only in part: this is the rung to repeat.
                latest[family] = {
                    "date": item.get("date"),
                    "session_name": item.get("session_name"),
                    "minutes": minutes,
                    "rung": rung,
                    "completed": False,
                    "partial": True,
                    "executed_fraction": item.get("executed_fraction"),
                    "missed_since": [item.get("session_name")],
                }
            elif family in latest and minutes >= 0.8 * latest[family]["minutes"]:
                # Only a missed prescription of similar size says the rung was too big.
                latest[family]["missed_since"].append(item.get("session_name"))
    for entry in latest.values():
        # A miss after the last completed dose means: repeat, do not progress.
        entry["completed"] = not entry["missed_since"]
    return latest


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def days_since_dose(last: dict[str, Any] | None, planning_date: Any = None) -> int | None:
    if not last or planning_date is None:
        return None
    current = _as_date(planning_date)
    previous = _as_date(last.get("date"))
    if current is None or previous is None:
        return None
    return (current - previous).days


def gap_step_back(gap_days: int | None) -> int | None:
    """Rungs to step back after a break from a ladder family (detraining).

    Proportional to the gap: > 14 d one rung, > 28 d two, > 42 d three, and
    > 56 d returns None: restart at the phase entry rung.
    """
    if gap_days is None:
        return 0
    for threshold, rungs in DOSE_GAP_STEP_BACK:
        if gap_days > threshold:
            return rungs
    return 0


def entry_rung(family: str, phase: str) -> int:
    top = len(LADDERS[family]["rungs"]) - 1
    return max(0, min(PHASE_START_RUNG.get(phase, 1), top))


def next_rung(
    family: str,
    last: dict[str, Any] | None,
    *,
    phase: str,
    block_week: int,
    underload_status: str,
    readiness_status: str,
    planning_date: Any = None,
) -> int:
    """Pick the rung for this week's key session in one ladder family.

    Progression steps by rung index (recorded in the `progression` object),
    never by minutes alone, so equal-total rungs (4x10 -> 2x20) still move on.

    - completed last time -> one rung up (two when underload is detected)
    - missed / partial last time -> repeat that rung
    - yellow readiness -> repeat, never progress
    - gap since the dose: > 14 d one rung back, > 28 d two, > 42 d three,
      > 56 d restart at the phase entry rung
    - no history -> start from the phase's entry rung plus block week
    """
    top = len(LADDERS[family]["rungs"]) - 1
    if last is None:
        rung = PHASE_START_RUNG.get(phase, 1) + max(0, block_week - 1)
        if underload_status == "detected":
            rung += 1
        if readiness_status != "green":
            rung -= 1
        return max(0, min(rung, top))
    current = current_rung(family, last) or 0
    back = gap_step_back(days_since_dose(last, planning_date))
    if back is None:
        return min(current, entry_rung(family, phase))
    if back:
        return max(0, current - back)
    if readiness_status != "green" or not last.get("completed"):
        return current
    step = 2 if underload_status == "detected" else 1
    return min(current + step, top)


def ladder_dose(family: str, rung: int) -> dict[str, Any]:
    ladder = LADDERS[family]
    rung = max(0, min(rung, len(ladder["rungs"]) - 1))
    reps, rep_min = ladder["rungs"][rung]
    return {
        "family": family,
        "rung": rung,
        "repeats": reps,
        "rep_min": rep_min,
        "recovery_min": max(1, round(rep_min * ladder["recovery_ratio"])),
        "time_in_zone_min": reps * rep_min,
        "target": ladder["target"],
        "label": ladder["label"],
    }


def focus_families(distance: str, phase: str, *, i_block: bool = False) -> list[str]:
    """Ladder focus for the race bucket and phase, most important first.

    Trail/ultra follow `FOCUS_BY_PHASE`. Other buckets keep the distance focus:
    base pushes VO2 work to the end (pyramidal base). For trail/ultra the
    build phase opens with a 3-4 week I block (run intervals first).
    """
    by_phase = FOCUS_BY_PHASE.get(distance)
    if by_phase:
        focus = list(by_phase.get(phase) or by_phase["build"])
    else:
        focus = list(FOCUS_BY_DISTANCE.get(distance, FOCUS_BY_DISTANCE["generic"]))
        if phase == "base":
            focus = [family for family in focus if family not in VO2_FAMILIES] + [
                family for family in focus if family in VO2_FAMILIES
            ]
    if phase == "build" and i_block and by_phase:
        # Trail/ultra build opens with a 3-4 week Daniels I block as the run
        # quality; uphill threshold takes over afterwards.
        focus = ["run_interval"] + [family for family in focus if family != "run_interval"]
    return focus


def phase_load_weeks(
    snapshot: dict[str, Any], phase: str, block_week: int = 1
) -> int:
    """Completed load weeks so far in the current phase (support-dose progression).

    Read from the approved-week history in `athlete_state` so brick, descent,
    race-block, steady-finish and swim doses keep building across 4-week blocks
    instead of resetting each block. Recovery/holiday weeks pause the count;
    a week with poor adherence (< 60%) does not count; a return from a break
    steps back two weeks. Without history the block week is the fallback.
    """
    history = (snapshot.get("athlete_state") or {}).get("history") or []
    week_start = str(snapshot.get("week_start") or "9999-12-31")
    adherence = {
        str(item.get("planned_week_start")): item.get("adherence_score")
        for item in snapshot.get("prior_plan_history") or []
        if isinstance(item, dict)
    }
    entries = sorted(
        (item for item in history if str(item.get("week_start") or "") < week_start),
        key=lambda item: str(item.get("week_start") or ""),
    )
    if not entries:
        count = max(0, int(block_week or 1) - 1)
    else:
        count = 0
        for item in reversed(entries):
            if item.get("phase") not in (None, phase):
                break
            if item.get("classification") in {"recovery", "unload", "holiday", "transition"}:
                continue
            score = adherence.get(str(item.get("week_start")))
            try:
                if score is not None and float(score) < 0.6:
                    continue
            except (TypeError, ValueError):
                pass
            count += 1
    rfb = (snapshot.get("training_load") or {}).get("return_from_break") or {}
    if rfb.get("status") == "returning":
        count = max(0, count - 2)
    return count


def missed_session_rule(
    missed_day_index: int,
    next_quality_day_index: int | None,
) -> dict[str, str]:
    """What to do with a missed key session this week: never cram it.

    Missed early in the week (Mon-Wed) with 48h+ before the next quality
    session -> move it into that gap; otherwise drop it and repeat the rung
    next week.
    """
    gap = None if next_quality_day_index is None else next_quality_day_index - missed_day_index
    if missed_day_index <= 2 and (gap is None or gap >= 3):
        return {
            "action": "move",
            "rule": "Missed early in the week with 48h+ before the next quality session: move it, with an easy day either side.",
        }
    return {
        "action": "drop_and_repeat",
        "rule": "Missed late or too close to the next quality session: drop it, do not cram; next week repeats the same rung.",
    }


def assess_progression(
    prior_plan_contexts: list[dict[str, Any]],
    prior_plan_history: list[dict[str, Any]],
    *,
    planning_date: Any = None,
) -> dict[str, Any]:
    history = quality_history(prior_plan_contexts, prior_plan_history)
    latest = last_dose_by_family(history)
    suggestions = {}
    for family, last in latest.items():
        current = current_rung(family, last) or 0
        last["rung"] = current
        gap = days_since_dose(last, planning_date)
        if gap is not None:
            last["days_since_dose"] = gap
        back = gap_step_back(gap)
        if back is None:
            suggestions[family] = ladder_dose(family, 0)
            continue
        if back:
            suggestions[family] = ladder_dose(family, max(0, current - back))
            continue
        step = 1 if last.get("completed") else 0
        suggestions[family] = ladder_dose(family, current + step)
    return {
        "history": history[-12:],
        "last_dose_by_family": latest,
        "next_rung_if_green": suggestions,
        "rule": (
            "Each load week, a key session progresses one rung (by rung index, so equal-total "
            "rungs still move on) from the last completed dose of that family; repeat the rung "
            "after a miss, a partial session or in a yellow week; jump two rungs only when "
            "underload is detected and readiness is green; step back in proportion to the gap "
            "(> 14 d one rung, > 28 d two, > 42 d three, > 56 d restart at the phase entry rung)."
        ),
    }
