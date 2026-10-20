from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .agents import (
    head_coach_planner,
    macro_coach,
    nutritionist,
    orchestrator,
    phase_alignment_auditor,
    performance_doctor,
    race_director,
    syntax_guard,
    training_philosophy_auditor,
    validator_coach,
)
from .athlete_state import (
    athlete_state_path,
    bootstrap_athlete_state,
    capture_race_results,
    load_athlete_state,
    log_injuries,
    pending_open_decisions,
    plan_week_start,
    record_week_actuals,
    resolve_open_decisions,
    store_open_decisions,
    update_athlete_state_from_plan,
    write_athlete_state,
)
from .compiler import compile_note_events, compile_nutrition_notes, compile_plan
from .checkin import build_post_fetch_checkin, merge_checkin_answers
from .config import AppConfig
from .contracts import validate_agent_output, validate_final_plan
from .dashboard import render_dashboard
from .intervals import (
    BaseIntervalsAdapter,
    IntervalsAdapterError,
    build_intervals_adapter,
    find_orphan_coach_events,
)
from .memory import build_athlete_memory
from .normalizer import normalize_snapshot
from .plan_audit import audit_plan
from .planning_state import (
    finalize_planning_state,
    initialize_planning_state,
    load_planning_state,
    mark_step_complete,
    validate_planning_state_for_finalize,
)
from .prompt_loader import load_prompts
from .report import render_athlete_brief, render_weekly_report
from .run_manifest import AGENT_OUTPUT_FILES, agent_output_path, agent_outputs_dir
from .threshold_testing import detect_test_activities, record_threshold_tests
from .utils import (
    ensure_dir,
    parse_date,
    read_json,
    sha256_file,
    start_of_planned_week,
    write_json,
    write_text,
)


class StalePlanningDateError(RuntimeError):
    pass


class MissingCheckinError(ValueError):
    pass


class ApprovalMismatchError(ValueError):
    pass


RUN_CHECKIN_FILE = "weekly_checkin.json"
# Edited-event comparison window: also catch events moved a few days away.
EDIT_SEARCH_PADDING_DAYS = 7


def _assert_planning_date_is_current(planning_date: str, mode: str) -> None:
    """Guard against a forgotten planning_date in weekly_checkin.json.

    Live fetches use planning_date as the upper bound for Intervals wellness/
    activity queries (see intervals.py), so a stale value silently truncates
    the pull instead of raising an obvious error. This only applies to live
    mode: mock-mode tests/fixtures intentionally pin planning_date to a fixed
    date, and future planning dates ("plan the week of ...") are legitimate.
    """
    if mode != "live":
        return
    today = datetime.now().date()
    requested = parse_date(planning_date)
    gap_days = (today - requested).days
    if gap_days > 1:
        raise StalePlanningDateError(
            f"weekly_checkin.json planning_date ({planning_date}) is {gap_days} "
            f"day(s) behind today ({today.isoformat()}). Live Intervals fetches use "
            "planning_date as the newest-data cutoff, so this would silently "
            "truncate the wellness/activity pull. Update planning_date to today's "
            "date before running prepare-run in live mode."
        )


def _create_run_dir(base_dir: Path, output_dir: str | None = None) -> Path:
    if output_dir:
        return ensure_dir(output_dir)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    return ensure_dir(base_dir / run_id)


def _load_local_inputs(
    athlete_profile_path: str,
    weekly_checkin_path: str,
    race_calendar_path: str | None,
) -> dict[str, Any]:
    race_calendar: dict[str, Any] = {"races": []}
    if race_calendar_path and Path(race_calendar_path).exists():
        race_calendar = read_json(race_calendar_path)
    return {
        "athlete_profile": read_json(athlete_profile_path),
        "weekly_checkin": read_json(weekly_checkin_path),
        "race_calendar": race_calendar,
    }


def _load_athlete_state() -> dict[str, Any]:
    return load_athlete_state(athlete_state_path())


def _same_athlete(
    athlete_profile: dict[str, Any], candidate_inputs: dict[str, Any]
) -> bool:
    candidate_profile = candidate_inputs.get("local_inputs", {}).get(
        "athlete_profile", {}
    )
    athlete_name = str(athlete_profile.get("name", "")).strip().lower()
    candidate_name = str(candidate_profile.get("name", "")).strip().lower()
    if athlete_name and candidate_name:
        return athlete_name == candidate_name
    return True


def _latest_planned_session_date(final_plan: dict[str, Any]) -> str | None:
    dates = [
        session.get("date")
        for session in final_plan.get("sessions", [])
        if session.get("date")
    ]
    if not dates:
        return None
    return max(dates)


def _load_prior_plan_contexts(
    search_root: Path,
    athlete_profile: dict[str, Any],
    planning_date: str,
    current_run: Path,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Pushed plans of earlier weeks, newest first, one (the latest) per week.

    Only runs whose push really happened count (planning_state push_status
    `pushed`, or `pushed_at` on the approval); dry runs and approved-but-never
    pushed runs would otherwise double count adherence. When several pushed
    runs cover the same week (a re-plan or a mid-week adjustment) only the
    latest one is used; an adjustment run is merged with the days of the run
    it replaced that came before the adjustment.
    """
    current_week_start = start_of_planned_week(planning_date)
    by_week: dict[str, tuple[str, dict[str, Any]]] = {}
    for candidate in search_root.iterdir():
        if not candidate.is_dir() or candidate == current_run:
            continue
        final_plan_path = candidate / "final_plan.json"
        inputs_path = candidate / "inputs.json"
        if not final_plan_path.exists() or not inputs_path.exists():
            continue
        approval = _read_json_or_empty(candidate / "approval.json")
        if approval.get("status") != "approved" or not _push_happened(candidate):
            continue
        inputs = read_json(inputs_path)
        if not _same_athlete(athlete_profile, inputs):
            continue
        final_plan = _effective_final_plan(candidate, read_json(final_plan_path))
        latest_session = _latest_planned_session_date(final_plan)
        week_start = plan_week_start(final_plan)
        if not latest_session or not week_start:
            continue
        if parse_date(latest_session) >= current_week_start:
            continue
        order_key = str(approval.get("pushed_at") or approval.get("approved_at") or "") + candidate.name
        existing = by_week.get(week_start)
        if existing and existing[0] >= order_key:
            continue
        by_week[week_start] = (
            order_key,
            {
                "source_run": str(candidate),
                "final_plan": final_plan,
                # The merged (answered) check-in, not the pre-answer template.
                "weekly_checkin": _run_checkin(candidate, inputs),
                "approval": approval,
            },
        )
    ordered = sorted(by_week.items(), key=lambda item: item[0], reverse=True)
    return [payload for _, (_, payload) in ordered[:limit]]


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = read_json(path)
    return payload if isinstance(payload, dict) else {}


def _push_happened(run_dir: Path) -> bool:
    """True only when the run's workouts were really pushed (not a dry run)."""
    approval = _read_json_or_empty(run_dir / "approval.json")
    if approval.get("pushed_at"):
        return True
    state = _read_json_or_empty(run_dir / "planning_state.json")
    if state:
        return state.get("push_status") == "pushed"
    push_result = _read_json_or_empty(run_dir / "push_result.json")
    return bool(push_result) and push_result.get("dry_run") is False


def _run_checkin(run_dir: Path, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    path = run_dir / RUN_CHECKIN_FILE
    if path.exists():
        return read_json(path)
    inputs = inputs if inputs is not None else _read_json_or_empty(run_dir / "inputs.json")
    return inputs.get("local_inputs", {}).get("weekly_checkin", {})


def _effective_final_plan(run_dir: Path, final_plan: dict[str, Any]) -> dict[str, Any]:
    """An adjustment run plus the earlier days of the plan it replaced."""
    adjustment = final_plan.get("adjustment") or {}
    replaced = adjustment.get("replaces_run")
    if not replaced:
        return final_plan
    replaced_plan_path = Path(replaced) / "final_plan.json"
    if not replaced_plan_path.exists():
        replaced_plan_path = run_dir.parent / Path(replaced).name / "final_plan.json"
    if not replaced_plan_path.exists():
        return final_plan
    earlier = [
        session
        for session in read_json(replaced_plan_path).get("sessions", [])
        if str(session.get("date")) < str(adjustment.get("from_date"))
    ]
    return {**final_plan, "sessions": [*earlier, *final_plan.get("sessions", [])]}


def _fetch_threshold_test_intervals(
    adapter: BaseIntervalsAdapter,
    activities: list[dict[str, Any]],
    athlete_state: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Pull lap data only for threshold tests that are not yet recorded."""
    recorded = athlete_state.get("threshold_testing") or {}
    intervals: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for test in detect_test_activities(activities):
        last_date = (recorded.get(test["discipline"]) or {}).get("last_test_date")
        if last_date and test["date"] <= last_date:
            continue
        try:
            intervals[str(test["activity_id"])] = adapter.fetch_activity_intervals(
                test["activity_id"]
            )
        except IntervalsAdapterError as exc:
            errors.append(
                f"Could not fetch intervals for threshold test {test['name']} ({test['date']}): {exc}"
            )
    return intervals, errors


def _recent_races(snapshot: dict[str, Any], days: int = 14) -> list[dict[str, Any]]:
    """Intervals races of the last `days` days (for the post-race debrief question)."""
    return [
        race
        for race in snapshot.get("races") or []
        if -days <= int(race.get("days_to_race", 9999) or 9999) < 0
    ]


def _run_context_manifest(
    config: AppConfig,
    run_dir: Path,
    planning_date: str,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or {}
    return {
        "workflow": "opencode-main-agent",
        "run_dir": str(run_dir),
        "prompt_dir": str(config.prompt_dir),
        "schema_dir": "schemas",
        "normalized_snapshot": str(run_dir / "normalized_snapshot.json"),
        "athlete_state_path": str(athlete_state_path()),
        "planning_state_path": str(run_dir / "planning_state.json"),
        "agent_outputs_dir": str(agent_outputs_dir(run_dir)),
        "expected_agent_outputs": {
            name: str(agent_output_path(run_dir, name)) for name in AGENT_OUTPUT_FILES
        },
        "final_plan_path": str(run_dir / "final_plan.json"),
        "weekly_report_path": str(run_dir / "weekly_report.md"),
        "athlete_brief_path": str(run_dir / "athlete_brief.md"),
        "dashboard_path": str(run_dir / "weekly_dashboard.html"),
        "approval_path": str(run_dir / "approval.json"),
        "weekly_checkin_path": str(run_dir / RUN_CHECKIN_FILE),
        "suggested_commands": {
            "record_checkin": f"python3 -m triathlon_agent.cli record-checkin {run_dir} <answers.json>",
            "finalize_run": f"python3 -m triathlon_agent.cli finalize-run {run_dir}",
            "approve_dry_run": f"python3 -m triathlon_agent.cli approve {run_dir} --mode {config.mode} --dry-run",
            "approve_live": f"python3 -m triathlon_agent.cli approve {run_dir} --mode {config.mode}",
        },
        "workflow_summary": [
            "Read normalized_snapshot.json and the specialist prompt files.",
            "Summarize the fetched week and ask the post-fetch athlete check-in before drafting.",
            "Record the answers with record-checkin; this re-normalizes the snapshot with them.",
            "Write each specialist JSON artifact into agent_outputs/.",
            "Write the orchestrated final plan to final_plan.json.",
            "Run finalize-run to validate artifacts and render weekly_report.md.",
            "Wait for approval before running approve.",
        ],
        "open_decisions": snapshot.get("open_decisions") or [],
        "post_fetch_checkin": build_post_fetch_checkin(
            planning_date,
            pending_decisions=snapshot.get("open_decisions") or [],
            recent_races=_recent_races(snapshot),
        ),
    }


def _write_agent_outputs(run_dir: Path, outputs: dict[str, Any]) -> None:
    directory = ensure_dir(agent_outputs_dir(run_dir))
    aggregate: dict[str, Any] = {}
    for agent_name, payload in outputs.items():
        path = directory / AGENT_OUTPUT_FILES[agent_name]
        write_json(path, payload)
        aggregate[agent_name] = payload
    write_json(run_dir / "agent_outputs.json", aggregate)


def _load_agent_outputs(run_dir: Path) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for agent_name in AGENT_OUTPUT_FILES:
        path = agent_output_path(run_dir, agent_name)
        if path.exists():
            outputs[agent_name] = read_json(path)
    return outputs


def _final_plan_sha(run_dir: Path) -> str | None:
    path = run_dir / "final_plan.json"
    return sha256_file(path) if path.exists() else None


def _ensure_approval_file(run_dir: Path, approved: bool = False) -> dict[str, Any]:
    """Write approval.json; an approval is bound to the exact final_plan.json.

    An existing approval is kept on re-finalize only when it recorded the
    plan's sha256 and the plan is byte-identical. Any change (or an approval
    without a recorded hash) resets it to pending, so an old approval can
    never carry over to a revised plan.
    """
    approval_path = run_dir / "approval.json"
    current_sha = _final_plan_sha(run_dir)
    reset_reason = None
    if approval_path.exists() and not approved:
        existing = read_json(approval_path)
        if existing.get("status") == "approved":
            recorded = existing.get("final_plan_sha256")
            if recorded and recorded == current_sha:
                return existing
            reset_reason = (
                "final_plan.json changed since it was approved"
                if recorded
                else "re-finalized; the earlier approval did not record which plan it approved"
            )
    approval: dict[str, Any] = {
        "status": "approved" if approved else "pending",
        "approved_at": datetime.now().isoformat(timespec="seconds")
        if approved
        else None,
        "requires_approval_before_push": True,
        "final_plan_sha256": current_sha if approved else None,
    }
    if reset_reason:
        approval["reset_reason"] = reset_reason
    write_json(approval_path, approval)
    return approval


def _check_approval_matches_plan(run_path: Path, approval: dict[str, Any]) -> dict[str, Any]:
    """Refuse a push when final_plan.json changed after approval."""
    current_sha = _final_plan_sha(run_path)
    recorded = approval.get("final_plan_sha256")
    if recorded and recorded != current_sha:
        raise ApprovalMismatchError(
            "final_plan.json changed after it was approved. Re-run finalize-run "
            f"{run_path} and approve the revised plan before pushing."
        )
    if not recorded:
        # Approved by hand-editing approval.json: bind the approval to the plan
        # as it is now, so later edits are caught.
        approval = {**approval, "final_plan_sha256": current_sha}
        write_json(run_path / "approval.json", approval)
    return approval


def _sync_focus_notes_to_sessions(final_plan: dict[str, Any]) -> dict[str, Any]:
    """
    Rebuild weekly_focus_notes from the finalized sessions list.

    Focus notes generated during initial orchestration become stale when
    sessions are revised after the fact (e.g. session moved from Friday to
    Thursday, interval count changed). This regenerates them deterministically
    so pushed calendar notes always reflect the current plan.
    """
    sessions = final_plan.get("sessions", [])
    if not sessions:
        return final_plan

    ordered_dates: list[str] = []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        date = session.get("date")
        if not date:
            continue
        if date not in by_date:
            ordered_dates.append(date)
            by_date[date] = []
        by_date[date].append(session)

    notes: list[dict[str, Any]] = []
    for date in ordered_dates:
        day_sessions = by_date[date]
        day_name = day_sessions[0].get("day", "")
        active = [s for s in day_sessions if s.get("discipline") != "Recovery"]

        if active:
            title = " + ".join(s["session_name"] for s in active)
            body_parts: list[str] = []
            for s in active:
                objective = s.get("objective", "")
                key_block = next(
                    (b for b in s.get("blocks", []) if (b.get("repeat") or 0) > 1),
                    None,
                )
                if key_block and key_block.get("steps"):
                    step = key_block["steps"][0]
                    interval_desc = (
                        f"{key_block['repeat']}×{step.get('duration', '')} "
                        f"at {step.get('target', '')}"
                    ).strip()
                    body_parts.append(
                        f"{objective} — {interval_desc}" if objective else interval_desc
                    )
                elif objective:
                    body_parts.append(objective)
            body = " | ".join(body_parts) if body_parts else title
            push = True
        else:
            title = " + ".join(s["session_name"] for s in day_sessions)
            body = " | ".join(
                s.get("objective", "Rest and recover.") for s in day_sessions
            )
            push = False

        notes.append(
            {
                "day": day_name,
                "date": date,
                "title": title,
                "body": body,
                "push_to_intervals": push,
            }
        )

    return {**final_plan, "weekly_focus_notes": notes}


def _clear_run_artifacts(run_dir: Path) -> None:
    for relative_path in [
        "agent_outputs.json",
        "draft_plan.json",
        "final_plan.json",
        "weekly_report.md",
        "athlete_brief.md",
        "weekly_dashboard.html",
        "compiled_workouts.json",
        "compiled_notes.json",
        "compiled_push_payload.json",
        "push_result.json",
        "threshold_update_result.json",
        "run_context.json",
        "planning_state.json",
        "approval.json",
        "inputs.json",
        "normalized_snapshot.json",
        RUN_CHECKIN_FILE,
        "adjustment_context.json",
        "edited_events.json",
        "orphan_events.json",
    ]:
        path = run_dir / relative_path
        if path.exists():
            path.unlink()
    directory = ensure_dir(agent_outputs_dir(run_dir))
    for path in directory.glob("*.json"):
        path.unlink()


def prepare_run(
    config: AppConfig,
    athlete_profile_path: str,
    weekly_checkin_path: str,
    race_calendar_path: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    run_dir = _create_run_dir(config.runs_dir, output_dir=output_dir)
    _clear_run_artifacts(run_dir)
    local_inputs = _load_local_inputs(
        athlete_profile_path, weekly_checkin_path, race_calendar_path
    )
    _assert_planning_date_is_current(
        local_inputs["weekly_checkin"]["planning_date"], config.mode
    )
    return _prepare_run_dir(config, run_dir, local_inputs)


def _load_history_inputs(
    local_inputs: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    local_inputs["athlete_state"] = _load_athlete_state()
    prior_plan_contexts = _load_prior_plan_contexts(
        run_dir.parent,
        local_inputs["athlete_profile"],
        local_inputs["weekly_checkin"]["planning_date"],
        run_dir,
    )
    local_inputs["prior_plan_contexts"] = prior_plan_contexts
    local_inputs["prior_plan_context"] = (
        prior_plan_contexts[0] if prior_plan_contexts else None
    )
    return local_inputs


def _build_snapshot(
    local_inputs: dict[str, Any],
    intervals_data: dict[str, Any],
    assumption_notes: list[str],
) -> dict[str, Any]:
    """Normalize the stored raw inputs; re-runnable without a new fetch.

    `local_inputs["athlete_state"]` is the state as loaded from disk; the
    bootstrapped copy (phase/block re-derived when stale, races from the
    Intervals calendar only) goes into the snapshot.
    """
    snapshot = normalize_snapshot(local_inputs, intervals_data)
    snapshot["assumptions"].extend(assumption_notes)
    weekly_checkin = local_inputs["weekly_checkin"]
    bootstrapped_state = bootstrap_athlete_state(
        snapshot, local_inputs.get("athlete_state") or {}
    )
    bootstrapped_state = resolve_open_decisions(
        bootstrapped_state, weekly_checkin, today=weekly_checkin.get("planning_date")
    )
    # Races only come from the Intervals calendar: the state embedded in the
    # snapshot never carries a stored priority race (informational
    # last_planned_priority_race only).
    bootstrapped_state.pop("priority_race", None)
    (bootstrapped_state.get("active_macro_cycle") or {}).pop("priority_race", None)
    snapshot["athlete_state"] = bootstrapped_state
    if bootstrapped_state.get("stale_reset"):
        snapshot["assumptions"].append(
            "Stored macro cycle was re-derived: "
            + str(bootstrapped_state["stale_reset"].get("reason"))
            + "."
        )
    # Reliability memory without illness/injury weeks (needs the run check-ins).
    prior_contexts = local_inputs.get("prior_plan_contexts") or []
    snapshot["athlete_memory"] = build_athlete_memory(
        snapshot.get("prior_plan_history") or [],
        snapshot.get("planning_constraints") or {},
        local_inputs["athlete_profile"],
        weekly_checkin,
        health_events=snapshot.get("recent_health_events") or [],
        checkins_by_run={
            str(context.get("source_run")): context.get("weekly_checkin") or {}
            for context in prior_contexts
        },
        weekly_actuals=bootstrapped_state.get("weekly_actuals") or [],
    )
    snapshot["open_decisions"] = pending_open_decisions(bootstrapped_state)
    return snapshot


def _write_prepared_run(
    config: AppConfig,
    run_dir: Path,
    local_inputs: dict[str, Any],
    intervals_data: dict[str, Any],
    assumption_notes: list[str],
) -> dict[str, Any]:
    snapshot = _build_snapshot(local_inputs, intervals_data, assumption_notes)
    prompts = load_prompts(config.prompt_dir)
    ensure_dir(agent_outputs_dir(run_dir))
    write_json(
        run_dir / "inputs.json",
        {
            "local_inputs": local_inputs,
            "intervals_snapshot": intervals_data,
            "assumption_notes": assumption_notes,
            "prompt_files": sorted(prompts),
        },
    )
    write_json(run_dir / RUN_CHECKIN_FILE, local_inputs["weekly_checkin"])
    write_json(run_dir / "normalized_snapshot.json", snapshot)
    _ensure_approval_file(run_dir, approved=False)
    context = _run_context_manifest(
        config,
        run_dir,
        local_inputs["weekly_checkin"]["planning_date"],
        snapshot,
    )
    write_json(run_dir / "run_context.json", context)
    initialize_planning_state(
        run_dir,
        workflow_mode=config.mode,
        freshness=snapshot.get("freshness", {}),
        planning_date=local_inputs["weekly_checkin"]["planning_date"],
        post_fetch_checkin=context["post_fetch_checkin"],
    )
    mark_step_complete(
        run_dir,
        "freshness_check",
        artifact_path=str(run_dir / "normalized_snapshot.json"),
    )
    mark_step_complete(
        run_dir,
        "last_week_summary",
        artifact_path=str(run_dir / "run_context.json"),
    )
    # post_fetch_checkin is NOT complete yet: the athlete has not answered.
    # record-checkin marks it once the answers are merged and re-normalized.
    return snapshot


def _adapter(config: AppConfig) -> BaseIntervalsAdapter:
    return build_intervals_adapter(
        mode=config.mode,
        fixture_dir=config.fixture_dir,
        base_url=config.intervals_base_url,
        api_key=config.intervals_api_key,
        athlete_id=config.intervals_athlete_id,
    )


def _prepare_run_dir(
    config: AppConfig, run_dir: Path, local_inputs: dict[str, Any]
) -> dict[str, Any]:
    local_inputs = _load_history_inputs(local_inputs, run_dir)
    adapter = _adapter(config)
    intervals_data = adapter.fetch_snapshot(
        local_inputs["weekly_checkin"]["planning_date"]
    )
    test_intervals, test_interval_errors = _fetch_threshold_test_intervals(
        adapter, intervals_data.get("activities", []), local_inputs["athlete_state"]
    )
    intervals_data["threshold_test_intervals"] = test_intervals
    _write_prepared_run(config, run_dir, local_inputs, intervals_data, test_interval_errors)
    return {
        "run_dir": str(run_dir),
        "normalized_snapshot_path": str(run_dir / "normalized_snapshot.json"),
        "run_context_path": str(run_dir / "run_context.json"),
        "approval_path": str(run_dir / "approval.json"),
        "weekly_checkin_path": str(run_dir / RUN_CHECKIN_FILE),
    }


def record_checkin(run_dir: str, answers: dict[str, Any]) -> dict[str, Any]:
    """Merge the athlete's answers into the run and re-normalize without a new fetch.

    The merged check-in is persisted in the run folder (weekly_checkin.json and
    inputs.json), the snapshot is rebuilt from the stored raw Intervals data,
    and only then is post_fetch_checkin marked complete. A race debrief or
    decision answers are also written to the long-term athlete state.
    """
    run_path = Path(run_dir)
    inputs_path = run_path / "inputs.json"
    if not inputs_path.exists():
        raise ValueError(f"{run_path} has no inputs.json; run prepare-run first.")
    inputs = read_json(inputs_path)
    local_inputs = inputs["local_inputs"]
    merged = merge_checkin_answers(_run_checkin(run_path, inputs), answers)
    local_inputs["weekly_checkin"] = merged
    snapshot = _build_snapshot(
        local_inputs,
        inputs["intervals_snapshot"],
        list(inputs.get("assumption_notes") or []),
    )
    inputs["local_inputs"] = local_inputs
    write_json(inputs_path, inputs)
    write_json(run_path / RUN_CHECKIN_FILE, merged)
    write_json(run_path / "normalized_snapshot.json", snapshot)
    state_written = False
    if merged.get("race_debrief") or merged.get("decision_answers"):
        state_path = athlete_state_path()
        state = load_athlete_state(state_path)
        if merged.get("race_debrief"):
            state = capture_race_results(state, snapshot, merged)
        if merged.get("decision_answers"):
            state = resolve_open_decisions(state, merged, today=merged.get("planning_date"))
        write_athlete_state(state_path, state)
        state_written = True
    if (run_path / "planning_state.json").exists():
        mark_step_complete(
            run_path,
            "post_fetch_checkin",
            artifact_path=str(run_path / RUN_CHECKIN_FILE),
            extra={"checkin_recorded_at": datetime.now().isoformat(timespec="seconds")},
        )
    payload = answers.get("answers") if isinstance(answers.get("answers"), dict) else answers
    return {
        "run_dir": str(run_path),
        "weekly_checkin_path": str(run_path / RUN_CHECKIN_FILE),
        "normalized_snapshot_path": str(run_path / "normalized_snapshot.json"),
        "answered_fields": sorted(payload.keys()),
        "athlete_state_updated": state_written,
    }


def _require_recorded_checkin(run_path: Path, allow_missing_checkin: bool) -> None:
    """Live finalize refuses a plan drafted before the athlete answered the check-in."""
    if not (run_path / "planning_state.json").exists():
        return
    state = load_planning_state(run_path)
    if "post_fetch_checkin" in state.get("completed_steps", []):
        return
    if allow_missing_checkin:
        mark_step_complete(
            run_path,
            "post_fetch_checkin",
            extra={
                "override_flags": [
                    *state.get("override_flags", []),
                    "finalized_without_recorded_checkin",
                ]
            },
        )
        return
    if state.get("workflow_mode") == "live":
        raise MissingCheckinError(
            "The post-fetch check-in was never recorded for this run, so the plan would ignore "
            "the athlete's answers. Run `python -m triathlon_agent.cli record-checkin "
            f"{run_path} <answers.json>` first, or pass --allow-missing-checkin to finalize anyway."
        )


def _hard_cap_errors(final_plan: dict[str, Any], weekly_checkin: dict[str, Any]) -> list[str]:
    """The athlete's available hours are a binding weekly cap."""
    caps = [
        int(value)
        for value in (
            weekly_checkin.get("available_hours_hard_cap_min"),
            weekly_checkin.get("available_hours_override_min"),
        )
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    ]
    if not caps or final_plan.get("adjustment"):
        return []
    cap = min(caps)
    planned = sum(
        int(session.get("duration_min") or 0)
        for session in final_plan.get("sessions", [])
        if session.get("discipline") != "Recovery"
    )
    if planned > cap:
        return [
            f"The plan has {planned} min of training but the athlete's available time this week "
            f"is {cap} min (binding cap from the check-in). Trim the plan to fit."
        ]
    return []


def finalize_run(
    run_dir: str,
    final_plan: dict[str, Any] | None = None,
    auto_approve: bool = False,
    allow_missing_checkin: bool = False,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    snapshot = read_json(run_path / "normalized_snapshot.json")
    _require_recorded_checkin(run_path, allow_missing_checkin)
    outputs = _load_agent_outputs(run_path)
    required_agents = [
        "performance_doctor",
        "macro_coach",
        "race_director",
        "phase_alignment_auditor",
        "training_philosophy_auditor",
        "head_coach_planner",
        "validator_coach",
        "nutritionist",
    ]
    validation_errors: list[str] = []
    for agent_name in required_agents:
        payload = outputs.get(agent_name)
        if payload is None:
            validation_errors.append(
                f"Missing required agent output: {agent_output_path(run_path, agent_name)}"
            )
            continue
        validation_errors.extend(validate_agent_output(agent_name, payload))

    if (run_path / "planning_state.json").exists():
        for step_name, agent_name in [
            ("performance_doctor", "performance_doctor"),
            ("macro_coach", "macro_coach"),
            ("race_director", "race_director"),
            ("phase_alignment_auditor", "phase_alignment_auditor"),
            ("training_philosophy_auditor", "training_philosophy_auditor"),
            ("head_coach_planner", "head_coach_planner"),
            ("validator_coach", "validator_coach"),
            ("nutritionist", "nutritionist"),
            ("final_plan", "orchestrator"),
        ]:
            if agent_name in outputs:
                mark_step_complete(
                    run_path,
                    step_name,
                    artifact_path=str(agent_output_path(run_path, agent_name))
                    if agent_name != "orchestrator"
                    else str(run_path / "final_plan.json"),
                )
        mark_step_complete(run_path, "revision_loop")
        planning_state_errors = validate_planning_state_for_finalize(run_path)
        validation_errors.extend(planning_state_errors)

    resolved_final_plan = final_plan or read_json(run_path / "final_plan.json")
    resolved_final_plan = _sync_focus_notes_to_sessions(resolved_final_plan)
    validation_errors.extend(validate_final_plan(resolved_final_plan))
    validation_errors.extend(
        _hard_cap_errors(resolved_final_plan, snapshot.get("weekly_checkin") or {})
    )
    if validation_errors:
        raise ValueError("; ".join(validation_errors))

    resolved_final_plan["coaching_audit"] = audit_plan(
        snapshot,
        outputs["performance_doctor"],
        outputs["macro_coach"],
        resolved_final_plan,
    )
    outputs["orchestrator"] = resolved_final_plan
    _write_agent_outputs(run_path, outputs)
    write_json(run_path / "final_plan.json", resolved_final_plan)
    report = render_weekly_report(
        snapshot,
        outputs["performance_doctor"],
        outputs["macro_coach"],
        outputs["race_director"],
        resolved_final_plan,
    )
    athlete_brief = render_athlete_brief(resolved_final_plan)
    dashboard = render_dashboard(
        snapshot,
        outputs["performance_doctor"],
        outputs["macro_coach"],
        outputs["race_director"],
        resolved_final_plan,
    )
    write_text(run_path / "weekly_report.md", report)
    write_text(run_path / "athlete_brief.md", athlete_brief)
    write_text(run_path / "weekly_dashboard.html", dashboard)
    if (run_path / "planning_state.json").exists():
        mark_step_complete(run_path, "finalize_run")
    approval = _ensure_approval_file(run_path, approved=auto_approve)
    finalize_planning_state(
        run_path,
        approval_status=approval["status"],
        push_status="not_started",
    )
    return {
        "run_dir": str(run_path),
        "report_path": str(run_path / "weekly_report.md"),
        "athlete_brief_path": str(run_path / "athlete_brief.md"),
        "dashboard_path": str(run_path / "weekly_dashboard.html"),
        "approval_status": approval["status"],
        "coaching_audit": resolved_final_plan["coaching_audit"],
    }


def _run_stub_plan(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    perf = performance_doctor.run(snapshot)
    macro = macro_coach.run(snapshot, perf)
    race = race_director.run(snapshot, macro)
    draft = head_coach_planner.run(snapshot, perf, macro, race)
    phase_alignment = phase_alignment_auditor.run(snapshot, macro, race, draft)
    philosophy = training_philosophy_auditor.run(
        snapshot, macro, race, phase_alignment, draft
    )
    validator = validator_coach.run(snapshot, draft, perf, macro, race)
    nutrition = nutritionist.run(snapshot, draft, perf, macro, race)
    final_plan = orchestrator.run(
        snapshot,
        perf,
        macro,
        race,
        phase_alignment,
        philosophy,
        draft,
        validator,
        nutrition,
    )
    outputs = {
        "performance_doctor": perf,
        "macro_coach": macro,
        "race_director": race,
        "phase_alignment_auditor": phase_alignment,
        "training_philosophy_auditor": philosophy,
        "head_coach_planner": draft,
        "validator_coach": validator,
        "nutritionist": nutrition,
        "orchestrator": final_plan,
    }
    return outputs, final_plan


def plan_week(
    config: AppConfig,
    athlete_profile_path: str,
    weekly_checkin_path: str,
    race_calendar_path: str | None = None,
    output_dir: str | None = None,
    auto_approve: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    prepared = prepare_run(
        config,
        athlete_profile_path,
        weekly_checkin_path,
        race_calendar_path,
        output_dir=output_dir,
    )
    run_dir = Path(prepared["run_dir"])
    # The stub workflow treats the supplied check-in file as the answers.
    mark_step_complete(
        run_dir,
        "post_fetch_checkin",
        artifact_path=str(run_dir / RUN_CHECKIN_FILE),
        extra={"checkin_source": "stub: check-in file used as the answers"},
    )
    snapshot = read_json(run_dir / "normalized_snapshot.json")
    outputs, final_plan = _run_stub_plan(snapshot)
    _write_agent_outputs(run_dir, outputs)
    write_json(run_dir / "draft_plan.json", outputs["head_coach_planner"])
    write_json(run_dir / "final_plan.json", final_plan)
    result = finalize_run(
        str(run_dir), final_plan=final_plan, auto_approve=auto_approve
    )
    if auto_approve:
        result["approval_result"] = approve_run(config, str(run_dir), dry_run=dry_run)
    return result


def _orphan_event_report(
    adapter: BaseIntervalsAdapter,
    push_payload: list[dict[str, Any]],
    today: str,
) -> dict[str, Any]:
    """Read-only: list coach events in the pushed window the new plan no longer has."""
    days = sorted(
        str(item.get("start_date_local") or "")[:10]
        for item in push_payload
        if item.get("start_date_local")
    )
    if not days or max(days[0], today) > days[-1]:
        return {"status": "skipped", "orphans": []}
    oldest, newest = max(days[0], today), days[-1]
    try:
        existing = adapter.list_events(oldest, newest)
    except (IntervalsAdapterError, NotImplementedError) as exc:
        return {"status": "unavailable", "error": str(exc), "orphans": []}
    orphans = find_orphan_coach_events(existing, push_payload, oldest, newest)
    return {
        "status": "orphans_found" if orphans else "clean",
        "window": [oldest, newest],
        "orphans": orphans,
        "note": "Nothing was deleted. Remove these superseded events in Intervals.icu if the revised plan replaces them.",
    }


def approve_run(
    config: AppConfig,
    run_dir: str,
    dry_run: bool = False,
    today: str | None = None,
    overwrite_edited: bool = False,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    approval_path = run_path / "approval.json"
    approval = read_json(approval_path)
    if approval.get("status") != "approved":
        raise ValueError(
            "Approval is required before pushing. Run finalize-run with --auto-approve or update approval.json to approved first."
        )
    approval = _check_approval_matches_plan(run_path, approval)

    final_plan = read_json(run_path / "final_plan.json")
    validation_errors = validate_final_plan(final_plan)
    if validation_errors:
        raise ValueError("; ".join(validation_errors))
    adapter = _adapter(config)
    if (run_path / "planning_state.json").exists():
        finalize_planning_state(run_path, approval_status="approved")
    run_id = run_path.name
    push_plan = _remaining_days_only(final_plan)
    compiled = compile_plan(push_plan, run_id=run_id)
    compiled_notes = compile_nutrition_notes(
        push_plan.get("nutrition_summary", {}), run_id=run_id
    )
    compiled_notes.extend(
        compile_note_events(push_plan.get("weekly_focus_notes", []), run_id=run_id)
    )
    reviewed = []
    for workout in compiled:
        syntax_result = syntax_guard.review(workout)
        workout["description"] = syntax_result["final_description"]
        workout["syntax_review"] = syntax_result
        reviewed.append(workout)
    if (run_path / "planning_state.json").exists():
        mark_step_complete(run_path, "approval")
        mark_step_complete(run_path, "syntax_guard")
    write_json(run_path / "compiled_workouts.json", reviewed)
    write_json(run_path / "compiled_notes.json", compiled_notes)
    push_payload = [*compiled_notes, *reviewed]
    edited_report = _edited_event_report(
        adapter, run_path, push_payload, overwrite_edited=overwrite_edited
    )
    write_json(run_path / "edited_events.json", edited_report)
    skipped_ids = {
        item["external_id"] for item in edited_report["edited"] if not overwrite_edited
    }
    push_payload = [
        item for item in push_payload if item.get("external_id") not in skipped_ids
    ]
    # The payload actually sent: the next push compares the calendar with it.
    write_json(run_path / "compiled_push_payload.json", push_payload)
    push_result = adapter.push_workouts(push_payload, dry_run=dry_run)
    push_result["skipped_edited_events"] = sorted(skipped_ids)
    write_json(run_path / "push_result.json", push_result)
    orphan_report = _orphan_event_report(
        adapter, push_payload, today or datetime.now().date().isoformat()
    )
    write_json(run_path / "orphan_events.json", orphan_report)
    threshold_testing = final_plan.get("threshold_testing") or {}
    threshold_updates = [
        update
        for update in threshold_testing.get("threshold_updates", [])
        if update.get("apply")
    ]
    threshold_result = adapter.update_sport_settings(
        threshold_updates, dry_run=dry_run
    )
    threshold_result["updates"] = threshold_updates
    write_json(run_path / "threshold_update_result.json", threshold_result)
    applied_updates = (
        []
        if dry_run
        else [
            update
            for update in threshold_updates
            if any(
                result.get("status") != "error"
                and result.get("sport_settings_id") == update["sport_settings_id"]
                for result in threshold_result["results"]
            )
        ]
    )
    if (run_path / "planning_state.json").exists():
        finalize_planning_state(
            run_path, push_status="dry_run" if dry_run else "pushed"
        )
    if not dry_run:
        approval["pushed_at"] = datetime.now().isoformat(timespec="seconds")
        write_json(approval_path, approval)
        _update_state_after_push(
            run_path, final_plan, threshold_testing, applied_updates
        )
    return {
        "compiled_workouts": str(run_path / "compiled_workouts.json"),
        "compiled_notes": str(run_path / "compiled_notes.json"),
        "push_payload": str(run_path / "compiled_push_payload.json"),
        "push_result": str(run_path / "push_result.json"),
        "orphan_events": orphan_report.get("orphans", []),
        "edited_events": edited_report["edited"],
        "edited_events_overwritten": overwrite_edited,
        "threshold_update_result": str(run_path / "threshold_update_result.json"),
    }


def _update_state_after_push(
    run_path: Path,
    final_plan: dict[str, Any],
    threshold_testing: dict[str, Any],
    applied_updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist the macro cycle plus long-term memory after a real push."""
    snapshot = _read_json_or_empty(run_path / "normalized_snapshot.json")
    checkin = _run_checkin(run_path)
    state_path = athlete_state_path()
    athlete_state = update_athlete_state_from_plan(
        load_athlete_state(state_path), final_plan, run_path
    )
    athlete_state = record_threshold_tests(
        athlete_state,
        threshold_testing.get("completed_tests", []),
        applied_updates,
    )
    athlete_state = record_week_actuals(
        athlete_state,
        snapshot.get("prior_plan_adherence") or {},
        checkin,
        health_events=snapshot.get("recent_health_events") or [],
    )
    athlete_state = capture_race_results(athlete_state, snapshot, checkin)
    athlete_state = log_injuries(athlete_state, snapshot, checkin)
    athlete_state = resolve_open_decisions(
        athlete_state, checkin, today=checkin.get("planning_date")
    )
    athlete_state = store_open_decisions(athlete_state, final_plan, run_path)
    write_athlete_state(state_path, athlete_state)
    return athlete_state


def _remaining_days_only(final_plan: dict[str, Any]) -> dict[str, Any]:
    """An adjust-week plan never pushes days before its from_date."""
    adjustment = final_plan.get("adjustment") or {}
    from_date = adjustment.get("from_date")
    if not from_date:
        return final_plan

    def keep(item: dict[str, Any]) -> bool:
        return str(item.get("date") or "") >= str(from_date)

    nutrition = dict(final_plan.get("nutrition_summary") or {})
    nutrition["daily_notes"] = [
        note for note in nutrition.get("daily_notes", []) if keep(note)
    ]
    return {
        **final_plan,
        "sessions": [session for session in final_plan.get("sessions", []) if keep(session)],
        "weekly_focus_notes": [
            note for note in final_plan.get("weekly_focus_notes", []) if keep(note)
        ],
        "nutrition_summary": nutrition,
    }


def _last_pushed_payload(run_path: Path) -> dict[str, dict[str, Any]]:
    """external_id -> the event as this agent last pushed it (earlier runs)."""
    pushed_runs: list[tuple[str, Path]] = []
    for candidate in run_path.parent.iterdir():
        if not candidate.is_dir() or candidate == run_path:
            continue
        if not (candidate / "compiled_push_payload.json").exists():
            continue
        if not _push_happened(candidate):
            continue
        approval = _read_json_or_empty(candidate / "approval.json")
        pushed_runs.append((str(approval.get("pushed_at") or "") + candidate.name, candidate))
    latest: dict[str, dict[str, Any]] = {}
    for _, candidate in sorted(pushed_runs, key=lambda item: item[0]):
        payload = read_json(candidate / "compiled_push_payload.json")
        for item in payload if isinstance(payload, list) else []:
            if item.get("external_id"):
                latest[str(item["external_id"])] = item
    return latest


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _event_changes(previous: dict[str, Any], live: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    previous_day = str(previous.get("start_date_local") or "")[:10]
    live_day = str(live.get("start_date_local") or "")[:10]
    if previous_day and live_day and previous_day != live_day:
        changes.append(f"moved from {previous_day} to {live_day}")
    if live.get("name") is not None and _normalize_text(live.get("name")) != _normalize_text(
        previous.get("name")
    ):
        changes.append("renamed")
    if live.get("description") is not None and _normalize_text(
        live.get("description")
    ) != _normalize_text(previous.get("description")):
        changes.append("description edited")
    return changes


def _edited_event_report(
    adapter: BaseIntervalsAdapter,
    run_path: Path,
    push_payload: list[dict[str, Any]],
    *,
    overwrite_edited: bool,
) -> dict[str, Any]:
    """Events the athlete edited or moved since our last push (never deleted).

    The upsert by external_id would silently overwrite them, so they are
    skipped unless --overwrite-edited is given. The live calendar is read
    with list_events; when that is unavailable the events fetched in this
    run's snapshot are used.
    """
    last_pushed = _last_pushed_payload(run_path)
    relevant = [
        item for item in push_payload if str(item.get("external_id")) in last_pushed
    ]
    if not relevant:
        return {"status": "no_prior_push", "edited": [], "source": None}
    days = sorted(str(item.get("start_date_local") or "")[:10] for item in relevant)
    oldest = (parse_date(days[0]) - timedelta(days=EDIT_SEARCH_PADDING_DAYS)).isoformat()
    newest = (parse_date(days[-1]) + timedelta(days=EDIT_SEARCH_PADDING_DAYS)).isoformat()
    source = "live_calendar"
    try:
        live_events = adapter.list_events(oldest, newest)
    except (IntervalsAdapterError, NotImplementedError):
        source = "run_snapshot"
        inputs = _read_json_or_empty(run_path / "inputs.json")
        live_events = (inputs.get("intervals_snapshot") or {}).get("events") or []
    live_by_id = {
        str(event.get("external_id")): event
        for event in live_events or []
        if event.get("external_id")
    }
    edited = []
    for item in relevant:
        external_id = str(item.get("external_id"))
        live = live_by_id.get(external_id)
        if not live:
            continue
        changes = _event_changes(last_pushed[external_id], live)
        if changes:
            edited.append(
                {
                    "external_id": external_id,
                    "name": live.get("name"),
                    "date": str(live.get("start_date_local") or "")[:10],
                    "event_id": live.get("id"),
                    "changes": changes,
                    "action": "overwritten" if overwrite_edited else "skipped",
                }
            )
    return {
        "status": "edited_found" if edited else "clean",
        "source": source,
        "edited": edited,
        "note": "Nothing is deleted. Skipped events keep the athlete's version; "
        "pass --overwrite-edited to replace them with the new plan.",
    }


def _done_so_far(
    sessions: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    from_date: str,
) -> list[dict[str, Any]]:
    """Planned sessions before from_date with what was actually done that day."""
    unmatched = list(activities)
    rows = []
    for session in sessions:
        day = str(session.get("date") or "")
        if not day or day >= from_date or session.get("discipline") == "Recovery":
            continue
        discipline = str(session.get("discipline") or "").lower()
        match = next(
            (
                activity
                for activity in unmatched
                if str(activity.get("start_date_local") or "")[:10] == day
                and discipline
                and discipline in str(activity.get("type") or "").lower()
            ),
            None,
        )
        if match:
            unmatched.remove(match)
        rows.append(
            {
                "date": day,
                "session_name": session.get("session_name"),
                "discipline": session.get("discipline"),
                "planned_min": int(session.get("duration_min") or 0),
                "actual_min": round(float(match.get("moving_time") or 0) / 60.0, 1)
                if match
                else 0.0,
                "status": "done" if match else "missed",
            }
        )
    return rows


def adjust_week(
    config: AppConfig,
    run_dir: str,
    from_date: str,
    reason: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Re-plan the rest of an approved week from `from_date` on.

    Creates a new run linked to the original (final_plan.adjustment), fetches
    fresh data, records done-so-far vs planned, and seeds the remaining days of
    the approved plan as the draft to revise. It needs its own finalize-run
    and approval, and approve pushes only the remaining days.
    """
    original = Path(run_dir)
    parse_date(from_date)
    approval = _read_json_or_empty(original / "approval.json")
    if approval.get("status") != "approved":
        raise ValueError(
            f"{original} is not approved; adjust-week revises an approved week. "
            "Finalize and approve it first, or simply revise and re-finalize that run."
        )
    original_plan = read_json(original / "final_plan.json")
    remaining = [
        session
        for session in original_plan.get("sessions", [])
        if str(session.get("date") or "") >= from_date
    ]
    if not remaining:
        raise ValueError(
            f"The approved plan in {original} has no sessions on or after {from_date}."
        )
    if not reason.strip():
        raise ValueError("adjust-week needs a --reason so the change is explained.")
    inputs = read_json(original / "inputs.json")
    local_inputs = {
        "athlete_profile": inputs["local_inputs"]["athlete_profile"],
        "weekly_checkin": dict(_run_checkin(original, inputs)),
        "race_calendar": inputs["local_inputs"].get("race_calendar") or {"races": []},
    }
    if config.mode == "live":
        local_inputs["weekly_checkin"]["planning_date"] = datetime.now().date().isoformat()
    previous_notes = str(local_inputs["weekly_checkin"].get("athlete_notes") or "").strip()
    local_inputs["weekly_checkin"]["athlete_notes"] = " ".join(
        part
        for part in (previous_notes, f"Mid-week adjustment from {from_date}: {reason}")
        if part
    )

    new_dir = _create_run_dir(config.runs_dir, output_dir=output_dir)
    if new_dir.resolve() == original.resolve():
        raise ValueError("adjust-week must write to a new run folder, not the original.")
    _clear_run_artifacts(new_dir)
    prepared = _prepare_run_dir(config, new_dir, local_inputs)
    snapshot = read_json(new_dir / "normalized_snapshot.json")
    activities = (snapshot.get("intervals") or {}).get("activities") or []
    done = _done_so_far(original_plan.get("sessions", []), activities, from_date)
    adjustment = {
        "from_date": from_date,
        "reason": reason,
        "replaces_run": str(original),
    }
    context = {
        **adjustment,
        "done_so_far": done,
        "done_minutes": round(sum(row["actual_min"] for row in done), 1),
        "planned_minutes_so_far": sum(row["planned_min"] for row in done),
        "remaining_sessions": remaining,
        "instructions": (
            "Revise the remaining days in final_plan.json (and agent_outputs/) for the reason given, "
            "then run finalize-run and approve this run. Only days from from_date on are pushed."
        ),
    }
    write_json(new_dir / "adjustment_context.json", context)

    outputs = _load_agent_outputs(original)
    outputs.pop("orchestrator", None)
    if "head_coach_planner" in outputs:
        outputs["head_coach_planner"] = {**outputs["head_coach_planner"], "sessions": remaining}
    if outputs:
        _write_agent_outputs(new_dir, outputs)
    write_json(new_dir / "draft_plan.json", {"sessions": remaining, "adjustment": adjustment})
    seeded = _remaining_days_only({**original_plan, "adjustment": adjustment})
    seeded.pop("coaching_audit", None)
    write_json(new_dir / "final_plan.json", seeded)
    mark_step_complete(
        new_dir,
        "post_fetch_checkin",
        artifact_path=str(new_dir / "adjustment_context.json"),
        extra={"adjustment": adjustment},
    )
    return {
        **prepared,
        "adjustment_context_path": str(new_dir / "adjustment_context.json"),
        "done_so_far": done,
        "remaining_sessions": len(remaining),
    }
