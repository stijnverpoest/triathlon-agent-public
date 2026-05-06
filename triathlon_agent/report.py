from __future__ import annotations

from typing import Any

from .utils import minutes_to_hhmm


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
    lines.append("## Executive Summary")
    lines.append(final_plan["executive_summary"])
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
        lines.append("## Time Budget")
        lines.append(
            f"- Required time this week: {minutes_to_hhmm(weekly_time_budget['target_min'])}"
        )
        lines.append(
            f"- Effective range: {minutes_to_hhmm(weekly_time_budget['minimum_effective_min'])} to {minutes_to_hhmm(weekly_time_budget['maximum_effective_min'])}"
        )
        lines.append(f"- Mode: {weekly_time_budget['mode']}")
        lines.append(f"- Why: {weekly_time_budget['rationale']}")
        lines.append("")
    lines.append("## What Last Week Tells Us")
    lines.append(final_plan["what_last_week_tells_us"])
    lines.append("")
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
            lines.append(f"- {edit['summary']}")
    else:
        lines.append("- No validator edits were required.")
    lines.append("")
    lines.append("## Seven-Day Plan")
    for session in final_plan["sessions"]:
        lines.append(
            f"### {session['day']} {session['date']} - {session['session_name']}"
        )
        lines.append(f"- Discipline: {session['discipline']}")
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
        lines.append(f"- Success: {session['success_criteria']}")
        lines.append(f"- Cut first: {session['cut_first']}")
        lines.append("- Structure:")
        for section in session["blocks"]:
            repeat = f" {section['repeat']}x" if section.get("repeat") else ""
            lines.append(f"  - {section['section']}{repeat}")
            for step in section["steps"]:
                cadence = f" {step['cadence']}" if step.get("cadence") else ""
                lines.append(f"    - {step['duration']} {step['target']}{cadence}")
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
