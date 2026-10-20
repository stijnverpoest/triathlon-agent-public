from __future__ import annotations

import re
from copy import deepcopy
from datetime import timedelta
from typing import Any

from . import nutritionist as nutritionist_agent
from ..race_selection import select_priority_race
from ..utils import day_name, parse_date, slugify


DECISION_RE = re.compile(
    r"\b(decide|decision|choose|choosing|beslis\w*|beslissing|kiezen|keuze)\b", re.IGNORECASE
)
RACE_WORD_RE = re.compile(r"\b(race|races|wedstrijd\w*|goal|doel|entry|inschrijv\w*)\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DECISION_CLAUSE_SPLIT_RE = re.compile(r"[.;\n!?]")


def _priority_race_label(snapshot: dict[str, Any]) -> str | None:
    """The Intervals priority race as 'Name (YYYY-MM-DD)'; races only come from Intervals."""
    race = select_priority_race(snapshot)
    if not race or not race.get("name"):
        return None
    return f"{race['name']} ({race.get('date')})"


def _race_decision_question(text: str, snapshot: dict[str, Any]) -> str:
    if not RACE_WORD_RE.search(text):
        return text
    label = _priority_race_label(snapshot)
    if label is None:
        return f"{text} (no race is on your Intervals calendar yet: add it there first)"
    race = select_priority_race(snapshot) or {}
    if str(race.get("name", "")).lower() in text.lower():
        return text if str(race.get("date")) in text else f"{text} [{race.get('date')}]"
    return f"{text} (for {label} on your Intervals calendar)"


def build_open_decisions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Open decisions: carried-over unanswered ones plus 'decide ...' items from the check-in."""
    weekly = snapshot.get("weekly_checkin") or {}
    week_start = snapshot.get("week_start")
    default_due = (
        (parse_date(week_start) + timedelta(days=6)).isoformat() if week_start else None
    )
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    state = snapshot.get("athlete_state") or {}
    for stored in state.get("open_decisions") or []:
        if stored.get("status") != "open":
            continue
        question = str(stored.get("question") or "").strip()
        if not question or slugify(question) in seen:
            continue
        seen.add(slugify(question))
        decisions.append(
            {
                "question": question,
                "due_date": stored.get("due_date"),
                "options": list(stored.get("options") or []),
                "default_if_no_answer": stored.get("default_if_no_answer")
                or "Keep the current plan.",
                "carried_over": True,
            }
        )
    texts: list[str] = [str(weekly.get("athlete_notes") or "")]
    for field in ("time_constraints", "upcoming_events"):
        value = weekly.get(field)
        if isinstance(value, list):
            texts.extend(str(item) for item in value)
    answered = {
        slugify(str(key)) for key in (weekly.get("decision_answers") or {})
    } if isinstance(weekly.get("decision_answers"), dict) else set()
    for text in texts:
        for clause in DECISION_CLAUSE_SPLIT_RE.split(text):
            clause = clause.strip(" ,-")
            if not clause or not DECISION_RE.search(clause):
                continue
            question = _race_decision_question(clause[0].upper() + clause[1:], snapshot)
            key = slugify(question)
            if key in seen or key in answered:
                continue
            seen.add(key)
            due_match = ISO_DATE_RE.search(clause)
            decisions.append(
                {
                    "question": question,
                    "due_date": due_match.group(1) if due_match else default_due,
                    "options": [],
                    "default_if_no_answer": "Keep the current plan unchanged.",
                }
            )
    return decisions


def _rest_only_sessions(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]], message: str
) -> list[dict[str, Any]]:
    """A medical red flag replaces the week with rest / easy walking only."""
    dates = sorted({str(session.get("date")) for session in sessions if session.get("date")})
    week_start = snapshot.get("week_start")
    if week_start:
        first = parse_date(week_start)
        dates = [(first + timedelta(days=offset)).isoformat() for offset in range(7)]
    rest = []
    for date_text in dates:
        rest.append(
            {
                "day": day_name(parse_date(date_text)),
                "date": date_text,
                "discipline": "Recovery",
                "session_name": "Rest - medical red flag",
                "objective": "No training until a doctor has cleared you. Easy walking only if you feel well.",
                "duration_min": 0,
                "intensity_guidance": "Rest or easy walking only",
                "primary_metric": "notes",
                "blocks": [],
                "execution_notes": [message],
                "success_criteria": "You have seen a doctor and been cleared before any training resumes.",
                "cut_first": "Everything: rest is the plan this week.",
                "priority": "support",
                "push_to_intervals": False,
                "feel_cue": "If walking makes symptoms return, stop and rest.",
                "bail_out_rule": "If chest pain, palpitations, fainting or breathlessness occur, seek medical help immediately.",
            }
        )
    return rest


def _estimate_load(session: dict[str, Any]) -> int:
    explicit = session.get("estimated_load")
    if explicit is not None:
        return int(round(float(explicit)))
    if session.get("discipline") == "Ride" and session.get("estimated_tss") is not None:
        return int(round(float(session["estimated_tss"])))
    duration = int(session.get("duration_min", 0))
    factor = 0.45
    if session["discipline"] == "Ride":
        factor = 0.7 if session["priority"] == "key" else 0.55
    elif session["discipline"] == "Run":
        factor = 0.6 if session["primary_metric"] == "Pace" else 0.5
    elif session["discipline"] == "WeightTraining":
        factor = 0.25
    return int(round(duration * factor))


def _session_rank(session: dict[str, Any]) -> tuple[int, int]:
    discipline = session.get("discipline")
    name = str(session.get("session_name", "")).lower()
    if session.get("race_day"):
        return (-1, 0)
    if session.get("priority") == "key":
        return (0, -int(session.get("duration_min", 0) or 0))
    if discipline == "Ride" and "long" in name:
        return (1, -int(session.get("duration_min", 0) or 0))
    if discipline == "Run" and "long" in name:
        return (2, -int(session.get("duration_min", 0) or 0))
    if discipline == "Ride":
        return (3, -int(session.get("duration_min", 0) or 0))
    if discipline == "Run":
        return (4, -int(session.get("duration_min", 0) or 0))
    if discipline == "Swim":
        return (5, -int(session.get("duration_min", 0) or 0))
    if discipline == "WeightTraining":
        return (6, -int(session.get("duration_min", 0) or 0))
    return (7, -int(session.get("duration_min", 0) or 0))


def _protected_sessions(sessions: list[dict[str, Any]], limit: int = 3) -> list[str]:
    protected: list[str] = []
    seen: set[str] = set()
    for session in sorted(sessions, key=_session_rank):
        label = session["session_name"]
        if label in seen:
            continue
        seen.add(label)
        protected.append(label)
        if len(protected) >= limit:
            break
    return protected


def _refresh_phase_alignment(
    phase_alignment: dict[str, Any],
    macro: dict[str, Any],
    final_sessions: list[dict[str, Any]],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    refreshed = deepcopy(phase_alignment)
    quality_ride_count = sum(
        1
        for session in final_sessions
        if session.get("discipline") == "Ride"
        and (
            session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}
            or float(session.get("expected_if", 0) or 0) >= 0.78
            or session.get("priority") == "key"
        )
    )
    quality_run_count = sum(
        1
        for session in final_sessions
        if session.get("discipline") == "Run"
        and session.get("primary_metric") == "Pace"
    )
    long_sessions = [
        session
        for session in final_sessions
        if int(session.get("duration_min", 0) or 0) >= 70
    ]
    supporting_signals = [
        signal
        for signal in refreshed.get("supporting_signals", [])
        if "one clear bike quality session" not in signal
        and "explicit weekly time cap" not in signal
    ]
    test_names = [
        session["session_name"]
        for session in final_sessions
        if session.get("threshold_test")
    ]
    if macro.get("week_classification") == "recovery" and test_names:
        supporting_signals.append(
            f"The final week is recovery-oriented and closes with a Friel rest & test: {', '.join(test_names)}; everything else stays aerobic."
        )
    elif macro.get("week_classification") == "recovery":
        supporting_signals.append(
            "The final week is truly recovery-oriented: no threshold run remains, bike work is aerobic, and the longer weekend sessions stay controlled."
        )
    else:
        supporting_signals.append(
            f"The final week lands {quality_ride_count} bike quality session(s), {quality_run_count} run quality session(s), and {len(long_sessions)} longer durability session(s)."
        )
    if constraints.get("available_hours_override_min"):
        supporting_signals.append(
            "The final plan still respects the athlete's explicit weekly time cap."
        )
    refreshed["supporting_signals"] = list(dict.fromkeys(supporting_signals))
    return refreshed


def _apply_validator_edits(
    plan: dict[str, Any], validator: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    final_plan = deepcopy(plan)
    incorporated = []
    sessions_to_remove: list[tuple[str, str]] = []

    def matches(session: dict[str, Any], edit: dict[str, Any]) -> bool:
        if session["session_name"] != edit["session_name"]:
            return False
        if edit.get("date") and session.get("date") != edit["date"]:
            return False
        return True

    for edit in validator.get("proposed_edits", []):
        for session in final_plan["sessions"]:
            if not matches(session, edit):
                continue
            if edit.get("action") == "shorten_session":
                original_duration = int(session.get("duration_min", 0) or 0)
                new_duration = min(original_duration, int(edit["new_duration_min"]))
                if new_duration == original_duration:
                    break
                session["duration_min"] = new_duration
                if session.get("estimated_tss") is not None and original_duration:
                    ratio = new_duration / original_duration
                    session["estimated_tss"] = int(
                        round(float(session["estimated_tss"]) * ratio)
                    )
                incorporated.append(edit)
                break
            if edit.get("action") == "remove_session":
                sessions_to_remove.append((session["date"], session["session_name"]))
                incorporated.append(edit)
                break
            if edit.get("action") == "downgrade_ride_to_endurance":
                is_long_ride = "long" in str(session.get("session_name", "")).lower()
                session["session_name"] = (
                    "Long Endurance Ride" if is_long_ride else "Endurance Ride"
                )
                session["objective"] = (
                    "Use aerobic bike work to preserve rhythm while letting fatigue come down and execution stay smooth."
                )
                session["priority"] = "support"
                session["intensity_guidance"] = (
                    "Steady endurance only, no threshold work"
                )
                session["primary_metric"] = "%FTP"
                session["zone_focus"] = "endurance"
                session["coggan_level"] = "L2 Endurance"
                session["expected_if"] = 0.64 if is_long_ride else 0.62
                session["estimated_tss"] = max(
                    50 if is_long_ride else 40,
                    int(
                        round(
                            int(session.get("duration_min", 0) or 0)
                            * (0.68 if is_long_ride else 0.6)
                        )
                    ),
                )
                session["blocks"] = [
                    {
                        "section": "Warmup",
                        "steps": [
                            {"duration": "15m", "target": "55-68%", "cadence": "90rpm"}
                        ],
                    },
                    {
                        "section": "Main Set",
                        "steps": [
                            {
                                "duration": f"{max(int(session['duration_min']) - 25, 30)}m",
                                "target": "65-72%",
                                "cadence": "90-95rpm",
                            }
                        ],
                    },
                    {
                        "section": "Cooldown",
                        "steps": [{"duration": "10m", "target": "50-55%"}],
                    },
                ]
                session["execution_notes"] = [
                    "Keep pressure low and cadence smooth.",
                    "You should finish fresher than after a real key bike session.",
                ]
                session["success_criteria"] = (
                    "Power stays smooth, breathing stays easy, and the legs feel looser afterward."
                )
                session["cut_first"] = (
                    "Shorten the main endurance block before touching the warmup or cooldown."
                )
                incorporated.append(edit)
                break
            if edit.get("action") == "downgrade_run_to_easy":
                session["session_name"] = "Easy Run + Reset"
                session["objective"] = (
                    "Keep run frequency intact while letting the week stay genuinely absorbable."
                )
                session["priority"] = "support"
                session["intensity_guidance"] = "Easy aerobic running only"
                session["primary_metric"] = "HR"
                session["zone_focus"] = "aerobic"
                session["daniels_type"] = "E"
                session["blocks"] = [
                    {
                        "section": "Warmup",
                        "steps": [{"duration": "10m", "target": "Z1 HR"}],
                    },
                    {
                        "section": "Main Set",
                        "steps": [
                            {
                                "duration": f"{max(int(session['duration_min']) - 15, 20)}m",
                                "target": "Z2 HR",
                            }
                        ],
                    },
                    {
                        "section": "Cooldown",
                        "steps": [{"duration": "5m", "target": "Z1 HR"}],
                    },
                ]
                session["execution_notes"] = [
                    "Keep the route flat and relaxed.",
                    "This should feel like circulation and rhythm, not hidden quality.",
                ]
                session["success_criteria"] = (
                    "The whole run stays easy from the first minute and you finish feeling better than you started."
                )
                session["cut_first"] = (
                    "Trim the main aerobic block before shortening the warmup or cooldown."
                )
                incorporated.append(edit)
                break
            if edit.get("action") != "reduce_strength_load":
                continue
            session["session_name"] = edit["new_session_name"]
            session["duration_min"] = edit["new_duration_min"]
            session["intensity_guidance"] = (
                "Activation only, finish fresher than you started"
            )
            session["objective"] = (
                "Prime posture and tissue readiness without creating weekend fatigue."
            )
            session["blocks"] = [
                {
                    "section": "Warmup",
                    "steps": [
                        {
                            "duration": "8m",
                            "target": "Mobility, band activation, calf prep",
                        }
                    ],
                },
                {
                    "section": "Main Set",
                    "repeat": 2,
                    "steps": [
                        {"duration": "6 reps", "target": "Goblet squat @ RPE 5"},
                        {"duration": "8 reps", "target": "Row @ RPE 5"},
                        {"duration": "30s", "target": "Dead bug or plank variation"},
                    ],
                },
                {
                    "section": "Finish",
                    "steps": [
                        {"duration": "5m", "target": "Breathing reset and mobility"}
                    ],
                },
            ]
            session["execution_notes"] = [
                "Move crisply, never grind.",
                "Leave the gym feeling better than when you walked in.",
            ]
            session["success_criteria"] = (
                "The legs feel more awake, not heavier, going into Saturday."
            )
            session["cut_first"] = (
                "Drop the second round before cutting the warmup or mobility finish."
            )
            incorporated.append(edit)
            break
    if sessions_to_remove:
        final_plan["sessions"] = [
            session
            for session in final_plan["sessions"]
            if (session["date"], session["session_name"]) not in sessions_to_remove
        ]
    return final_plan, incorporated


def _build_executive_summary(
    macro: dict[str, Any], sessions: list[dict[str, Any]], constraints: dict[str, Any]
) -> str:
    ride = next(
        (session for session in sessions if session.get("discipline") == "Ride"), None
    )
    quality_run = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Run"
            and session.get("primary_metric") == "Pace"
        ),
        None,
    )
    fixed_swim_days = constraints.get("fixed_swim_days", [])
    race_sessions = [session for session in sessions if session.get("race_day")]
    if macro.get("macro_phase") == "transition":
        return "Use a post-race transition week: short, easy, optional sessions only, no quality and no tests, so body and head recover before the next block."
    if macro.get("week_classification") == "holiday":
        return "Use a light holiday week: keep moving with easy, optional sessions and enjoy the break; it doubles as the unload week of the block."
    if race_sessions:
        names = " and ".join(session["session_name"].replace("RACE: ", "") for session in race_sessions)
        return f"Race week for {names}: protect freshness, keep a short sharp touch early, an opener the day before, and an easy or off day after."
    if macro.get("macro_phase") == "taper":
        return "Use a taper week: volume comes down and the long sessions shorten, while short intensity touches keep the legs sharp for race day."
    if macro.get("week_classification") == "recovery":
        return "Use a true recovery week: keep the swim routine and longer aerobic weekend, but remove threshold density so freshness returns for the next block."
    if macro.get("week_classification") == "absorb":
        return (
            f"Use an absorb-style week: protect {ride['session_name'] if ride else 'the main bike'} and "
            f"{quality_run['session_name'] if quality_run else 'the main run'}, keep the fixed swim rhythm on {', '.join(fixed_swim_days) if fixed_swim_days else 'the usual days'}, and avoid unnecessary extra load."
        )
    return (
        f"Build the week around {ride['session_name'] if ride else 'the main bike'}, "
        f"{quality_run['session_name'] if quality_run else 'the key run'}, and the normal weekend durability rhythm while preserving swim continuity."
    )


def _build_last_week_tells_us(
    snapshot: dict[str, Any], performance: dict[str, Any]
) -> str:
    adherence = snapshot.get("prior_plan_adherence", {})
    parts: list[str] = []
    if adherence.get("status") == "available":
        reviews = adherence.get("session_reviews", [])
        key_reviews = [
            review
            for review in reviews
            if review.get("status") != "missed"
            and review.get("discipline") in {"Ride", "Run"}
        ]
        if key_reviews:
            parts.append(key_reviews[0]["interpretation"])
        parts.append(adherence["summary"])
    load_change_pct = snapshot.get("load_change_pct", 0)
    if load_change_pct > 8:
        parts.append(
            f"Load rose by {load_change_pct}% versus the prior week, so the next step should stay deliberate rather than opportunistic."
        )
    if performance.get("main_risk"):
        parts.append(f"The main current risk is {performance['main_risk']}.")
    return (
        " ".join(parts)
        or "Training kept moving, and the next step should preserve the gains without adding avoidable fatigue."
    )


def _build_coach_note(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    performance: dict[str, Any],
    final_plan: dict[str, Any],
    validator: dict[str, Any],
) -> str:
    constraints = snapshot.get("planning_constraints", {})
    fixed_swim_days = constraints.get("fixed_swim_days", [])
    protected = _protected_sessions(final_plan.get("sessions", []), limit=2)
    note_parts = [
        f"This is a {macro.get('week_classification', 'build')} week inside the {macro.get('macro_phase', 'training')} phase.",
    ]
    if fixed_swim_days:
        note_parts.append(
            f"Keep the swim routine anchored on {', '.join(fixed_swim_days)} rather than letting those sessions disappear."
        )
    adherence = snapshot.get("prior_plan_adherence", {})
    if adherence.get("status") == "available":
        note_parts.append(adherence["summary"])
    note_parts.append(
        f"The main current risk is {performance.get('main_risk', 'drifting away from the plan')}, so if life gets messy, protect {', '.join(protected)} first."
    )
    if validator.get("proposed_edits"):
        note_parts.append(
            "The validator trimmed extra clutter so the week stays more executable in real life."
        )
    note_parts.extend(validator.get("info", [])[:1])
    return " ".join(note_parts)


def _build_weekly_focus_notes(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    performance: dict[str, Any],
    race: dict[str, Any],
) -> list[dict[str, Any]]:
    monday = snapshot.get("week_start")
    constraints = snapshot.get("planning_constraints", {})
    athlete_name = snapshot.get("athlete_profile", {}).get("name", "Athlete")
    race_name = (race.get("priority_race") or {}).get("name", "the next race")
    phase = macro.get("macro_phase", "build")
    readiness = performance.get("readiness_status", "steady")
    fixed_swim_days = constraints.get("fixed_swim_days", [])
    day_offs = constraints.get("days_off", [])
    body = (
        f"Inspired by Steve Magness: the best Monday is the one that makes the rest of the week better. "
        f"For {athlete_name}, this {phase} phase is not about proving fitness on day one; it is about protecting the controllables so you are better for {race_name}. "
        f"Keep Monday as a real reset{', with the usual rest rhythm intact' if day_offs else ''}, respect the fixed swim rhythm on {', '.join(fixed_swim_days) if fixed_swim_days else 'the usual swim days'}, and let restraint create the momentum."
    )
    if readiness != "green":
        body += " Because readiness is not fully green, smooth execution matters more than squeezing out extra work."
    return [
        {
            "date": monday,
            "day": "Monday",
            "title": "Monday Coaching Note",
            "body": body,
            "push_to_intervals": True,
        }
    ]


def _constraint_override_notes(
    sessions: list[dict[str, Any]], constraints: dict[str, Any]
) -> list[str]:
    notes: list[str] = []
    long_ride = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Ride"
            and "long" in session.get("session_name", "").lower()
        ),
        None,
    )
    long_run = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Run"
            and "long" in session.get("session_name", "").lower()
        ),
        None,
    )
    preferred_long_ride_day = constraints.get("preferred_long_ride_day")
    preferred_long_run_day = constraints.get("preferred_long_run_day")
    if (
        long_ride
        and preferred_long_ride_day
        and long_ride["day"] != preferred_long_ride_day
    ):
        notes.append(
            f"The long ride moved from the preferred {preferred_long_ride_day} to {long_ride['day']} to fit the overall week better."
        )
    if (
        long_run
        and preferred_long_run_day
        and long_run["day"] != preferred_long_run_day
    ):
        notes.append(
            f"The long run moved from the preferred {preferred_long_run_day} to {long_run['day']} to protect the overall sequence."
        )
    return notes


def _build_athlete_brief(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    performance: dict[str, Any],
    final_plan: dict[str, Any],
    validator: dict[str, Any],
    *,
    executive_summary: str,
    what_last_week_tells_us: str,
) -> dict[str, Any]:
    constraints = snapshot.get("planning_constraints", {})
    athlete = snapshot.get("athlete_profile", {})
    protect_if_busy = _protected_sessions(final_plan.get("sessions", []), limit=3)
    watch_for = list(
        dict.fromkeys(
            [
                *performance.get("warnings", []),
                performance["main_risk"],
                *final_plan.get("planner_warnings", []),
            ]
        )
    )
    why_this_week = (
        what_last_week_tells_us
        if macro.get("week_classification") == "recovery"
        else f"{what_last_week_tells_us} The goal is to keep the week executable while still moving the phase forward."
    )
    tone = str(athlete.get("coaching_tone_preference", "Direct and practical")).lower()
    week_theme = (
        executive_summary
        if "direct" not in tone
        else executive_summary.replace("Use ", "")
    )
    return {
        "week_theme": week_theme,
        "why_this_week": why_this_week,
        "protect_if_busy": protect_if_busy,
        "watch_for": watch_for,
        "fallback_plan": validator.get(
            "simpler_alternative",
            "Keep the key sessions and strip everything else back to easy support work.",
        ),
        "constraint_overrides": list(
            dict.fromkeys(
                [
                    *_constraint_override_notes(final_plan.get("sessions", []), constraints),
                    *final_plan.get("planner_notes", []),
                ]
            )
        ),
        "tone": athlete.get("coaching_tone_preference", "Direct and practical"),
    }


def _dynamic_specificity_locations(
    sessions: list[dict[str, Any]], macro: dict[str, Any], fallback: list[str]
) -> list[str]:
    recovery_week = macro.get("week_classification") == "recovery"
    ride = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Ride"
            and "Long" not in session.get("session_name", "")
        ),
        None,
    )
    long_ride = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Ride"
            and "Long" in session.get("session_name", "")
        ),
        None,
    )
    run = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Run"
            and session.get("primary_metric") == "Pace"
        ),
        None,
    )
    if recovery_week:
        items = []
        if ride:
            items.append(
                f"{ride['day']} {ride['session_name']} keeps bike feel alive without meaningful fatigue cost."
            )
        if long_ride:
            items.append(
                f"{long_ride['day']} {long_ride['session_name']} preserves durability without race-rhythm density."
            )
        long_run = next(
            (
                session
                for session in sessions
                if session.get("discipline") == "Run"
                and "Long" in session.get("session_name", "")
            ),
            None,
        )
        if long_run:
            items.append(
                f"{long_run['day']} {long_run['session_name']} keeps the run durable while staying clearly aerobic."
            )
        return items or fallback

    items = []
    if ride:
        items.append(
            f"{ride['day']} {ride['session_name']} should touch race-relevant sustained power."
        )
    if long_ride:
        items.append(
            f"{long_ride['day']} {long_ride['session_name']} should reinforce controlled endurance and fueling habits."
        )
    if run:
        items.append(
            f"{run['day']} {run['session_name']} should reinforce threshold discipline rather than raw speed."
        )
    return items or fallback


def _build_nutrition_lookup(nutrition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["date"]: item for item in nutrition.get("daily_priorities", [])}


def _build_session_fueling_lookup(
    nutrition: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item["date"], item["session_name"]): item
        for item in nutrition.get("session_fueling", [])
    }


def _build_threshold_testing(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    testing = snapshot.get("threshold_testing", {})
    decision = macro.get("threshold_tests") or {}
    test_sessions = {
        session["threshold_test"]["discipline"]: session
        for session in sessions
        if session.get("threshold_test")
    }
    scheduled = []
    for entry in decision.get("scheduled", []):
        session = test_sessions.get(entry["discipline"])
        scheduled.append(
            {
                **entry,
                "date": session.get("date") if session else None,
                "session_name": session.get("session_name") if session else None,
            }
        )
    return {
        "status": {
            discipline: {
                key: (testing.get(discipline) or {}).get(key)
                for key in (
                    "status",
                    "reason",
                    "blockers",
                    "last_test_date",
                    "days_since_last_test",
                    "protocol",
                )
            }
            for discipline in ("bike", "run")
        },
        "scheduled": scheduled,
        "deferred": decision.get("deferred", []),
        "rationale": decision.get("rationale", ""),
        "completed_tests": testing.get("completed_tests", []),
        "threshold_updates": testing.get("proposed_updates", []),
    }


def run(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
    phase_alignment: dict[str, Any],
    training_philosophy: dict[str, Any],
    draft_plan: dict[str, Any],
    validator: dict[str, Any],
    nutrition: dict[str, Any],
) -> dict[str, Any]:
    final_plan, incorporated = _apply_validator_edits(draft_plan, validator)
    stop_message = None
    if performance.get("training_clearance") == "no_training_see_doctor":
        stop_message = next(
            (item for item in performance.get("constraints", []) if "see a doctor" in item),
            "Stop training and see a doctor before resuming.",
        )
        final_plan["sessions"] = _rest_only_sessions(
            snapshot, final_plan["sessions"], stop_message
        )
    constraints = snapshot.get("planning_constraints", {})
    final_phase_alignment = _refresh_phase_alignment(
        phase_alignment, macro, final_plan["sessions"], constraints
    )
    final_nutrition = nutritionist_agent.run(
        snapshot, final_plan, performance, macro, race
    )
    nutrition_lookup = _build_nutrition_lookup(final_nutrition)
    session_fueling_lookup = _build_session_fueling_lookup(final_nutrition)
    planned_minutes = 0
    planned_load = 0
    for session in final_plan["sessions"]:
        if session["discipline"] == "Recovery":
            session["estimated_load"] = 0
        else:
            session["estimated_load"] = _estimate_load(session)
        planned_minutes += int(session["duration_min"])
        planned_load += int(session["estimated_load"])
        session["nutrition"] = session_fueling_lookup.get(
            (session["date"], session["session_name"]),
            nutrition_lookup.get(session["date"], {}),
        )

    planned_workouts = [
        session
        for session in final_plan["sessions"]
        if session["discipline"] != "Recovery"
        and session.get("push_to_intervals") is not False
    ]
    note_dates = [
        note["date"]
        for note in final_nutrition.get("daily_notes", [])
        if note.get("push_to_intervals") and note.get("body", "").strip()
    ]

    prior_minutes = snapshot.get("recent_week_minutes", 0)
    minute_delta_pct = 0.0
    if prior_minutes:
        minute_delta_pct = ((planned_minutes - prior_minutes) / prior_minutes) * 100.0
    prior_load = snapshot.get("recent_week_load", 0)
    load_delta_pct = 0.0
    if prior_load:
        load_delta_pct = ((planned_load - prior_load) / prior_load) * 100.0

    race_notes = deepcopy(race)
    race_notes["specificity_locations"] = _dynamic_specificity_locations(
        final_plan["sessions"], macro, race.get("specificity_locations", [])
    )
    executive_summary = (
        f"No training this week. {stop_message}"
        if stop_message
        else _build_executive_summary(macro, final_plan["sessions"], constraints)
    )
    what_last_week_tells_us = _build_last_week_tells_us(snapshot, performance)
    athlete_brief = _build_athlete_brief(
        snapshot,
        macro,
        performance,
        final_plan,
        validator,
        executive_summary=executive_summary,
        what_last_week_tells_us=what_last_week_tells_us,
    )
    weekly_time_budget = draft_plan.get("weekly_time_budget", {})
    weekly_focus_notes = _build_weekly_focus_notes(
        snapshot, macro, performance, race_notes
    )
    note_dates = [
        *[
            note["date"]
            for note in final_nutrition.get("daily_notes", [])
            if note.get("push_to_intervals") and note.get("body", "").strip()
        ],
        *[
            note["date"]
            for note in weekly_focus_notes
            if note.get("push_to_intervals") and note.get("body", "").strip()
        ],
    ]

    threshold_testing = _build_threshold_testing(
        snapshot, macro, final_plan["sessions"]
    )
    applied_updates = [
        update
        for update in threshold_testing["threshold_updates"]
        if update.get("apply")
    ]
    approval_checkpoint = "Approve this narrative plan before the system compiles and pushes workouts plus nutrition notes to Intervals.icu."
    if applied_updates:
        approval_checkpoint = (
            f"{approval_checkpoint} Approval also updates {len(applied_updates)} threshold setting(s) in Intervals.icu sport settings: "
            + ", ".join(
                f"{update['sport']} {update['label']} {update['current_display']} -> {update['proposed_display']}"
                for update in applied_updates
            )
            + "."
        )
    open_decisions = build_open_decisions(snapshot)
    if open_decisions:
        approval_checkpoint = (
            f"{approval_checkpoint} Open decision(s) still need your answer: "
            + "; ".join(
                decision["question"]
                + (f" (by {decision['due_date']})" if decision.get("due_date") else "")
                for decision in open_decisions
            )
            + "."
        )
    if stop_message:
        approval_checkpoint = f"{stop_message} {approval_checkpoint}"

    return {
        "agent": "orchestrator",
        "executive_summary": executive_summary,
        "athlete_brief": athlete_brief,
        "coach_note": _build_coach_note(
            snapshot, macro, performance, final_plan, validator
        ),
        "what_last_week_tells_us": what_last_week_tells_us,
        "strategic_overview": draft_plan["strategic_theme"],
        "macro_cycle_decision": {
            "phase": macro["macro_phase"],
            "classification": macro["week_classification"],
            "rationale": macro["rationale"],
            "friel_block_week": macro.get("friel_block_week"),
            "friel_cycle_type": macro.get("friel_cycle_type"),
            "phase_focus": macro.get("phase_focus"),
        },
        "phase_alignment_check": final_phase_alignment,
        "training_philosophy_check": training_philosophy,
        "readiness_status": performance["readiness_status"],
        "readiness_score": performance["readiness_score"],
        "risk_status": performance["main_risk"],
        "sessions": final_plan["sessions"],
        "weekly_time_budget": weekly_time_budget,
        "weekly_focus_notes": weekly_focus_notes,
        "planned_total_minutes": planned_minutes,
        "planned_total_estimated_load": planned_load,
        "changes_vs_prior_week": {
            "minutes_delta_pct": round(minute_delta_pct, 1),
            "load_delta_pct": round(load_delta_pct, 1),
            "summary": (
                f"Planned time is {round(minute_delta_pct, 1)}% versus last week's completed time, but estimated load is {round(load_delta_pct, 1)}% "
                "versus last week's completed training load because this week trims density and protects recovery while re-establishing normal rhythm."
            ),
        },
        "validator_adjustments_incorporated": incorporated,
        "validator_summary": validator,
        "nutrition_summary": final_nutrition,
        "race_notes": race_notes,
        "approval_checkpoint": approval_checkpoint,
        "open_decisions": open_decisions,
        "threshold_testing": threshold_testing,
        "write_back_preview": {
            "planned_workouts_to_push": len(planned_workouts),
            "planned_notes_to_push": len(note_dates),
            "note_dates": note_dates,
            "workout_disciplines": [
                session["discipline"] for session in planned_workouts
            ],
        },
    }
