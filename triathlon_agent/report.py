from __future__ import annotations

from typing import Any

from .race_selection import select_priority_race
from .utils import minutes_to_hhmm


MEMORY_WEEKS_SHOWN = 4
MEMORY_ITEMS_SHOWN = 5


def _render_red_flags(performance: dict[str, Any]) -> list[str]:
    flags = performance.get("red_flags") or []
    clearance = performance.get("training_clearance")
    if not flags and clearance != "no_training_see_doctor":
        if clearance == "easy_only":
            return [
                "## Health Check",
                "- Above-the-neck symptoms: easy aerobic sessions only, no intensity. "
                "If symptoms move below the neck or a fever appears, stop training and see a doctor.",
                "",
            ]
        return []
    lines = ["## STOP: Medical Red Flag"]
    labels = ", ".join(dict.fromkeys(str(flag.get("label") or flag.get("category")) for flag in flags))
    lines.append(
        f"- Stop training and see a doctor before resuming ({labels or 'red-flag symptom reported'}). "
        "Rest or easy walking only until a doctor has cleared you."
    )
    for flag in flags:
        excerpt = f": \"{flag['excerpt']}\"" if flag.get("excerpt") else ""
        lines.append(f"- {flag.get('label') or flag.get('category')} ({flag.get('source')}){excerpt}")
    lines.append("")
    return lines


def _render_open_decisions(final_plan: dict[str, Any]) -> list[str]:
    decisions = final_plan.get("open_decisions") or []
    if not decisions:
        return []
    lines = ["## Open Decisions (your answer needed)"]
    for decision in decisions:
        due = f" - decide by {decision['due_date']}" if decision.get("due_date") else ""
        lines.append(f"- **{decision.get('question')}**{due}")
        if decision.get("options"):
            lines.append(f"  - Options: {', '.join(str(item) for item in decision['options'])}")
        if decision.get("default_if_no_answer"):
            lines.append(f"  - If no answer: {decision['default_if_no_answer']}")
    lines.append("")
    return lines


def _render_long_term_memory(snapshot: dict[str, Any]) -> list[str]:
    state = snapshot.get("athlete_state") or {}
    actuals = (state.get("weekly_actuals") or [])[-MEMORY_WEEKS_SHOWN:]
    races = (state.get("race_results") or [])[-MEMORY_ITEMS_SHOWN:]
    injuries = (state.get("injury_log") or [])[-MEMORY_ITEMS_SHOWN:]
    if not (actuals or races or injuries):
        return []
    lines = ["## Long-Term Memory"]
    for week in actuals:
        extras = []
        if week.get("niggles"):
            extras.append(f"niggles: {', '.join(week['niggles'])}")
        if week.get("illness"):
            extras.append("illness week")
        lines.append(
            f"- Week of {week.get('week_start')}: {week.get('completed_min')}/{week.get('planned_min')} min completed, "
            f"{len(week.get('key_sessions_done') or [])}/{week.get('key_sessions_planned', 0)} key sessions, "
            f"adherence {week.get('adherence_pct')}%"
            + (f" ({'; '.join(extras)})" if extras else "")
        )
    for race in races:
        time_text = f" in {race['time']}" if race.get("time") else ""
        debrief = f" - debrief: {race['debrief']}" if race.get("debrief") else ""
        lines.append(
            f"- Race result: {race.get('name')} ({race.get('date')}, {race.get('distance') or 'distance n/a'}){time_text}{debrief}"
        )
    for injury in injuries:
        lines.append(
            f"- Injury log: {injury.get('date')} {injury.get('body_part')} "
            f"(severity {injury.get('severity')}, {injury.get('source')})"
        )
    lines.append("")
    return lines


def _render_race_source(snapshot: dict[str, Any]) -> list[str]:
    """Races always come from the Intervals calendar."""
    lines: list[str] = []
    race = select_priority_race(snapshot)
    if race:
        lines.append(
            f"- Intervals calendar priority race: {race.get('name')} on {race.get('date')} "
            f"({race.get('priority', '?')}-race, {race.get('distance') or 'distance n/a'})"
        )
    else:
        lines.append("- No race on your Intervals calendar: add your races there as RACE events.")
    for warning in snapshot.get("race_calendar_warnings") or []:
        lines.append(f"- Race calendar warning: {warning}")
    return lines


def _session_coaching_lines(session: dict[str, Any]) -> list[str]:
    lines = []
    if session.get("rpe_target"):
        lines.append(f"- RPE target: {session['rpe_target']}")
    if session.get("feel_cue"):
        lines.append(f"- Feel: {session['feel_cue']}")
    if session.get("bail_out_rule"):
        lines.append(f"- Bail out: {session['bail_out_rule']}")
    terrain_bits = []
    if session.get("terrain"):
        terrain_bits.append(str(session["terrain"]).replace("_", " "))
    if session.get("elevation_gain_m") is not None:
        terrain_bits.append(f"D+ {int(round(float(session['elevation_gain_m'])))} m")
    if session.get("elevation_loss_m") is not None:
        terrain_bits.append(f"D- {int(round(float(session['elevation_loss_m'])))} m")
    if terrain_bits:
        lines.append(f"- Terrain: {', '.join(terrain_bits)}")
    return lines


def _render_threshold_testing(threshold_testing: dict[str, Any]) -> list[str]:
    if not threshold_testing:
        return []
    lines = ["## Threshold Testing"]
    for discipline in ("bike", "run"):
        status = threshold_testing.get("status", {}).get(discipline) or {}
        last = status.get("last_test_date") or "none on record"
        lines.append(
            f"- {discipline.capitalize()}: {status.get('status', 'unknown')} "
            f"(protocol {status.get('protocol', 'n/a')}, last test {last}) - {status.get('reason', '')}"
        )
        for blocker in status.get("blockers") or []:
            lines.append(f"  - Blocker: {blocker}")
    for entry in threshold_testing.get("scheduled", []):
        lines.append(
            f"- Scheduled this week: {entry.get('session_name') or entry['discipline']} on {entry.get('date') or 'TBD'} ({entry['protocol']})"
        )
    for entry in threshold_testing.get("deferred", []):
        lines.append(f"- Deferred {entry['discipline']}: {entry['reason']}")
    if threshold_testing.get("rationale"):
        lines.append(f"- Rationale: {threshold_testing['rationale']}")
    for test in threshold_testing.get("completed_tests", []):
        lines.append(
            f"- Completed: {test.get('name')} on {test['date']} ({test.get('protocol_label', test['protocol'])}) - {test.get('status')}"
        )
        for note in test.get("notes", []):
            lines.append(f"  - {note}")
    updates = threshold_testing.get("threshold_updates", [])
    if updates:
        lines.append("")
        lines.append("### Proposed Threshold Updates")
        lines.append("| Sport | Setting | Current | Proposed | Change | Confidence | Pushed on approval |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for update in updates:
            delta = (
                f"{update['delta_pct']:+.1f}%" if update.get("delta_pct") is not None else "n/a"
            )
            pushed = "yes" if update.get("apply") else f"no - {update.get('apply_reason', '')}"
            lines.append(
                f"| {update['sport']} | {update.get('label', update['field'])} | {update.get('current_display')} | "
                f"{update.get('proposed_display')} | {delta} | {update.get('confidence')} | {pushed} |"
            )
    lines.append("")
    return lines


def render_weekly_report(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
    final_plan: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# Weekly Coaching Report")
    lines.append("")
    adjustment = final_plan.get("adjustment") or {}
    if adjustment:
        lines.append(
            f"> Mid-week adjustment from {adjustment.get('from_date')}: {adjustment.get('reason')} "
            f"(replaces the remaining days of {adjustment.get('replaces_run')})."
        )
        lines.append("")
    lines.extend(_render_red_flags(performance))
    lines.append("## Executive Summary")
    lines.append(final_plan["executive_summary"])
    lines.append("")
    lines.extend(_render_open_decisions(final_plan))
    audit = final_plan.get("coaching_audit")
    if audit:
        lines.append("## Coaching Audit")
        lines.append(
            f"- Status: {audit['status']} ({audit['quality_session_count']} quality session(s), "
            f"allowance {audit.get('max_quality_sessions')}, {audit['planned_intensity_pct']}% of bike/run time above easy"
            + (
                f": {audit['planned_moderate_pct']}% moderate, {audit['planned_high_pct']}% high)"
                if audit.get("planned_moderate_pct") is not None
                else ")"
            )
        )
        for finding in audit["findings"]:
            lines.append(f"- WARNING [{finding['code']}]: {finding['message']}")
        if not audit["findings"]:
            lines.append("- The plan follows the intensity and progression doctrine.")
        lines.append("")
    athlete_brief = final_plan.get("athlete_brief")
    if athlete_brief:
        lines.append("## Athlete Brief")
        lines.append(f"- Week theme: {athlete_brief['week_theme']}")
        lines.append(f"- Why: {athlete_brief['why_this_week']}")
        lines.append(
            f"- Protect if busy: {', '.join(athlete_brief['protect_if_busy'])}"
        )
        lines.append(f"- Watch for: {', '.join(athlete_brief['watch_for'])}")
        lines.append(f"- Busy fallback: {athlete_brief['fallback_plan']}")
        if athlete_brief.get("constraint_overrides"):
            lines.append(
                f"- Constraint overrides: {'; '.join(athlete_brief['constraint_overrides'])}"
            )
        lines.append("")
    coach_note = final_plan.get("coach_note")
    if coach_note:
        lines.append("## Coach Note")
        lines.append(coach_note)
        lines.append("")
    weekly_time_budget = final_plan.get("weekly_time_budget")
    if weekly_time_budget:
        lines.append("## Weekly Volume")
        lines.append(
            f"- Planned this week: {minutes_to_hhmm(int(weekly_time_budget['planned_min'] or 0))} (head coach decision)"
        )
        if weekly_time_budget.get("reference_min"):
            lines.append(
                f"- Your typical week (reference, not a cap): {minutes_to_hhmm(int(weekly_time_budget['reference_min']))}"
            )
        if weekly_time_budget.get("hard_cap_min"):
            lines.append(
                f"- Hours you have this week (cap): {minutes_to_hhmm(int(weekly_time_budget['hard_cap_min']))}"
            )
        for cut in weekly_time_budget.get("cuts") or []:
            lines.append(f"- Cut to fit: {cut}")
        lines.append(f"- Why: {weekly_time_budget['rationale']}")
        lines.append("")
    lines.append("## What Last Week Tells Us")
    lines.append(final_plan["what_last_week_tells_us"])
    lines.append("")
    lines.extend(_render_long_term_memory(snapshot))
    adherence = snapshot.get("prior_plan_adherence", {})
    if adherence.get("status") == "available":
        lines.append("## Recent Adherence Context")
        lines.append(f"- {adherence['summary']}")
        if adherence.get("missed_sessions"):
            lines.append(
                f"- Missed sessions: {', '.join(adherence['missed_sessions'])}"
            )
        if adherence.get("shortened_sessions"):
            lines.append(
                f"- Shortened sessions: {', '.join(adherence['shortened_sessions'])}"
            )
        if adherence.get("pragmatic_completions_count"):
            lines.append(
                f"- Pragmatic completions: {adherence['pragmatic_completions_count']}"
            )
        if adherence.get("completion_policy_note"):
            lines.append(f"- Policy: {adherence['completion_policy_note']}")
        lines.append("")
    if adherence.get("session_reviews"):
        lines.append("## Session Interpretation")
        for review in adherence["session_reviews"]:
            lines.append(
                f"- {review['session_name']} ({review['status']}): {review['interpretation']}"
            )
        lines.append("")
    lines.append("## Strategic Overview")
    lines.append(final_plan["strategic_overview"])
    lines.append("")
    lines.append("## Macro-Cycle Decision")
    lines.append(f"- Phase: {final_plan['macro_cycle_decision']['phase']}")
    lines.append(
        f"- Classification: {final_plan['macro_cycle_decision']['classification']}"
    )
    lines.append(f"- Rationale: {final_plan['macro_cycle_decision']['rationale']}")
    lines.append(
        f"- Friel block week: {final_plan['macro_cycle_decision'].get('friel_block_week', 'n/a')}"
    )
    lines.append(
        f"- Friel cycle type: {final_plan['macro_cycle_decision'].get('friel_cycle_type', 'n/a')}"
    )
    lines.append(
        f"- Phase focus: {final_plan['macro_cycle_decision'].get('phase_focus', 'n/a')}"
    )
    lines.append("")
    lines.extend(_render_threshold_testing(final_plan.get("threshold_testing") or {}))
    lines.append("## Phase Alignment Check")
    phase_alignment = final_plan["phase_alignment_check"]
    lines.append(f"- Status: {phase_alignment['alignment_status']}")
    lines.append(f"- Summary: {phase_alignment['week_fit_summary']}")
    lines.append(
        f"- Supporting signals: {'; '.join(phase_alignment['supporting_signals'])}"
    )
    if phase_alignment.get("required_adjustments"):
        lines.append(
            f"- Required adjustments: {'; '.join(phase_alignment['required_adjustments'])}"
        )
    if phase_alignment.get("forbidden_elements"):
        lines.append(
            f"- Forbidden elements: {'; '.join(phase_alignment['forbidden_elements'])}"
        )
    lines.append("")
    lines.append("## Training Philosophy Check")
    philosophy = final_plan["training_philosophy_check"]
    lines.append(f"- Overall status: {philosophy['overall_status']}")
    lines.append(f"- Daniels: {philosophy['daniels_alignment']}")
    lines.append(f"- Coggan: {philosophy['coggan_alignment']}")
    lines.append(f"- Friel: {philosophy['friel_alignment']}")
    if philosophy.get("findings"):
        lines.append(f"- Findings: {'; '.join(philosophy['findings'])}")
    if philosophy.get("required_edits"):
        lines.append(f"- Required edits: {'; '.join(philosophy['required_edits'])}")
    lines.append("")
    lines.append("## Readiness / Risk Status")
    lines.append(
        f"- Status: {performance['readiness_status']} ({performance['readiness_score']}/100)"
    )
    if performance.get("readiness_confidence"):
        lines.append(f"- Confidence: {performance['readiness_confidence']}")
    lines.append(f"- Main risk: {performance['main_risk']}")
    health_events = performance.get("health_events") or {}
    if health_events.get("status", "none") != "none":
        lines.append(
            f"- Health notes (Intervals): {health_events['status']}, "
            f"{health_events.get('days_since_end')} day(s) since last sick/injured note"
        )
    lines.append(
        f"- Objective findings: {'; '.join(performance['objective_findings'])}"
    )
    lines.append(
        f"- Subjective findings: {'; '.join(performance['subjective_findings'])}"
    )
    if performance.get("baseline_sources"):
        baseline_sources = performance["baseline_sources"]
        lines.append(
            "- Baseline sources: "
            f"HRV={baseline_sources.get('hrv')}, "
            f"RHR={baseline_sources.get('resting_hr')}, "
            f"sleep={baseline_sources.get('sleep_hours')}"
        )
    if performance.get("missing_data"):
        lines.append(f"- Missing data: {'; '.join(performance['missing_data'])}")
    if performance.get("override_reasons"):
        lines.append(
            f"- Override reasons: {'; '.join(performance['override_reasons'])}"
        )
    lines.append("")
    lines.append("## Strategic Race Lens")
    lines.extend(_render_race_source(snapshot))
    lines.append(f"- Priority race: {race['priority_race_lens']}")
    lines.append(f"- Needs: {', '.join(race['race_specific_needs'])}")
    lines.append(f"- Mistake to avoid: {race['mistake_to_avoid']}")
    lines.append("")
    recommendations = race.get("race_recommendations", [])
    if recommendations:
        lines.append("## Race Recommendations")
        for rec in recommendations:
            covered = rec.get("covered_by")
            rec_type = rec.get("type", "recommendation")
            for_race = rec.get("for_race", "")
            if rec_type == "tune_up" and covered:
                lines.append(
                    f"- **Tune-up for {for_race}**: {covered} "
                    f"({rec.get('covered_by_days_to_race')} days away) already fills this role — "
                    f"{rec['enhancement_rationale']}"
                )
            elif rec_type == "tune_up":
                lines.append(
                    f"- **Tune-up for {for_race}** — add a {rec['suggested_distance']} race "
                    f"in the window {rec.get('ideal_window', 'TBD')}: "
                    f"{rec['enhancement_rationale']}"
                )
            elif rec_type == "fitness_test":
                lines.append(
                    f"- **Build-block fitness test** ({rec.get('ideal_window', 'TBD')}) — "
                    f"add a {rec['suggested_distance']} race: {rec['enhancement_rationale']}"
                )
        lines.append("")
    lines.append("## Validator Adjustments Incorporated")
    if final_plan["validator_adjustments_incorporated"]:
        for edit in final_plan["validator_adjustments_incorporated"]:
            lines.append(f"- {edit.get('summary') or edit.get('action') or edit}")
    else:
        lines.append("- No validator edits were required.")
    lines.append("")
    lines.append("## Seven-Day Plan")
    for session in final_plan["sessions"]:
        lines.append(
            f"### {session['day']} {session['date']} - {session['session_name']}"
        )
        lines.append(f"- Discipline: {session['discipline']}")
        if session.get("race_day"):
            lines.append("- Race day: yes")
        if session.get("priority") == "optional":
            lines.append("- Optional: skip freely if tired or busy")
        if session.get("push_to_intervals") is False:
            lines.append("- Not pushed to Intervals.icu (already on the calendar or logged as played)")
        lines.append(f"- Objective: {session['objective']}")
        lines.append(f"- Duration: {minutes_to_hhmm(session['duration_min'])}")
        lines.append(f"- Intensity: {session['intensity_guidance']}")
        lines.append(f"- Metric: {session['primary_metric']}")
        if session.get("daniels_type"):
            lines.append(f"- Daniels type: {session['daniels_type']}")
        if session.get("coggan_level"):
            lines.append(f"- Coggan level: {session['coggan_level']}")
        if session.get("expected_if") is not None:
            lines.append(f"- Expected IF: {session['expected_if']}")
        if session.get("estimated_tss") is not None:
            lines.append(f"- Estimated TSS: {session['estimated_tss']}")
        if session.get("threshold_test"):
            lines.append(
                f"- Threshold test: {session['threshold_test'].get('protocol_label')} - {session['threshold_test'].get('formula_note', '')}"
            )
        lines.extend(_session_coaching_lines(session))
        lines.append(f"- Success: {session['success_criteria']}")
        lines.append(f"- Cut first: {session['cut_first']}")
        lines.append("- Structure:")
        for section in session["blocks"]:
            repeat = f" {section['repeat']}x" if section.get("repeat") else ""
            lines.append(f"  - {section.get('section', '')}{repeat}")
            for step in section.get("steps") or []:
                cadence = f" {step['cadence']}" if step.get("cadence") else ""
                lines.append(f"    - {step.get('duration', '')} {step.get('target', '')}{cadence}")
        lines.append(f"- Execution notes: {'; '.join(session['execution_notes'])}")
        nutrition = session.get("nutrition", {})
        if nutrition:
            lines.append(
                f"- Nutrition priority: {nutrition.get('priority', nutrition.get('day_priority', 'Normal fueling'))}"
            )
            guidance = nutrition.get("guidance", {})
            if guidance:
                lines.append(
                    f"- Fueling: pre={guidance.get('pre', '')} during={guidance.get('during', '')} post={guidance.get('post', '')}"
                )
        lines.append("")
    lines.append("## Nutrition Guidance")
    lines.append(f"- Week strategy: {final_plan['nutrition_summary']['week_strategy']}")
    lines.append(
        f"- Hydration: {final_plan['nutrition_summary']['hydration_guidance']}"
    )
    lines.append(
        f"- Recovery: {final_plan['nutrition_summary']['recovery_priorities']}"
    )
    if final_plan["nutrition_summary"]["major_concerns"]:
        lines.append(
            f"- Concerns: {'; '.join(final_plan['nutrition_summary']['major_concerns'])}"
        )
    lines.append("")
    lines.append("## Nutrition Note Previews")
    daily_notes = final_plan["nutrition_summary"].get("daily_notes", [])
    if daily_notes:
        for note in daily_notes:
            lines.append(f"- {note['day']} {note['date']}: {note['title']}")
            lines.append(f"  {note['body']}")
    else:
        lines.append("- No separate nutrition notes will be pushed for this week.")
    lines.append("")
    lines.append("## Weekly Focus Note Previews")
    focus_notes = final_plan.get("weekly_focus_notes", [])
    if focus_notes:
        for note in focus_notes:
            lines.append(f"- {note['day']} {note['date']}: {note['title']}")
            lines.append(f"  {note['body']}")
    else:
        lines.append("- No separate weekly focus notes will be pushed for this week.")
    lines.append("")
    lines.append("## Changes vs Prior Week")
    lines.append(f"- {final_plan['changes_vs_prior_week']['summary']}")
    lines.append("")
    lines.append("## Write-Back Preview")
    lines.append(
        f"- Planned pushes after approval: {final_plan['write_back_preview']['planned_workouts_to_push']}"
    )
    lines.append(
        f"- Planned nutrition notes after approval: {final_plan['write_back_preview'].get('planned_notes_to_push', 0)}"
    )
    lines.append(
        f"- Disciplines: {', '.join(final_plan['write_back_preview']['workout_disciplines'])}"
    )
    if final_plan["write_back_preview"].get("note_dates"):
        lines.append(
            f"- Nutrition note dates: {', '.join(final_plan['write_back_preview']['note_dates'])}"
        )
    lines.append("")
    lines.append("## Approval Checkpoint")
    lines.append(final_plan["approval_checkpoint"])
    for decision in final_plan.get("open_decisions") or []:
        due = f" (by {decision['due_date']})" if decision.get("due_date") else ""
        lines.append(f"- Open decision: {decision.get('question')}{due}")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_athlete_brief(final_plan: dict[str, Any]) -> str:
    athlete_brief = final_plan.get("athlete_brief", {})
    lines = ["# Athlete Brief", ""]
    if athlete_brief:
        lines.append(f"Theme: {athlete_brief.get('week_theme', 'Weekly plan')}")
        lines.append("")
        lines.append(f"Why this week: {athlete_brief.get('why_this_week', '')}")
        lines.append("")
        lines.append("Protect if busy:")
        for item in athlete_brief.get("protect_if_busy", []):
            lines.append(f"- {item}")
        lines.append("")
        lines.append("Watch for:")
        for item in athlete_brief.get("watch_for", []):
            lines.append(f"- {item}")
        lines.append("")
        lines.append("Busy fallback:")
        lines.append(athlete_brief.get("fallback_plan", ""))
        if athlete_brief.get("constraint_overrides"):
            lines.append("")
            lines.append("Why the routine changed:")
            for item in athlete_brief["constraint_overrides"]:
                lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"
