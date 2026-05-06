from __future__ import annotations

from datetime import datetime
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
    load_athlete_state,
    update_athlete_state_from_plan,
    write_athlete_state,
)
from .compiler import compile_note_events, compile_nutrition_notes, compile_plan
from .checkin import build_post_fetch_checkin
from .config import AppConfig
from .contracts import validate_agent_output, validate_final_plan
from .dashboard import render_dashboard
from .intervals import build_intervals_adapter
from .normalizer import normalize_snapshot
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
from .utils import (
    ensure_dir,
    parse_date,
    read_json,
    start_of_planned_week,
    write_json,
    write_text,
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
    current_week_start = start_of_planned_week(planning_date)
    candidates: list[tuple[object, dict[str, Any]]] = []
    for candidate in search_root.iterdir():
        if not candidate.is_dir() or candidate == current_run:
            continue
        approval_path = candidate / "approval.json"
        final_plan_path = candidate / "final_plan.json"
        inputs_path = candidate / "inputs.json"
        if (
            not approval_path.exists()
            or not final_plan_path.exists()
            or not inputs_path.exists()
        ):
            continue
        approval = read_json(approval_path)
        if approval.get("status") != "approved":
            continue
        inputs = read_json(inputs_path)
        if not _same_athlete(athlete_profile, inputs):
            continue
        final_plan = read_json(final_plan_path)
        approval = read_json(approval_path)
        latest_session = _latest_planned_session_date(final_plan)
        if not latest_session:
            continue
        latest_day = parse_date(latest_session)
        if latest_day >= current_week_start:
            continue
        candidates.append(
            (
                latest_day,
                {
                    "source_run": str(candidate),
                    "final_plan": final_plan,
                    "weekly_checkin": inputs.get("local_inputs", {}).get(
                        "weekly_checkin", {}
                    ),
                    "approval": approval,
                },
            )
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [payload for _, payload in candidates[:limit]]


def _run_context_manifest(
    config: AppConfig, run_dir: Path, planning_date: str
) -> dict[str, Any]:
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
        "suggested_commands": {
            "finalize_run": f"python3 -m triathlon_agent.cli finalize-run {run_dir}",
            "approve_dry_run": f"python3 -m triathlon_agent.cli approve {run_dir} --mode {config.mode} --dry-run",
            "approve_live": f"python3 -m triathlon_agent.cli approve {run_dir} --mode {config.mode}",
        },
        "workflow_summary": [
            "Read normalized_snapshot.json and the specialist prompt files.",
            "Summarize the fetched week and ask the post-fetch athlete check-in before drafting.",
            "Write each specialist JSON artifact into agent_outputs/.",
            "Write the orchestrated final plan to final_plan.json.",
            "Run finalize-run to validate artifacts and render weekly_report.md.",
            "Wait for approval before running approve.",
        ],
        "post_fetch_checkin": build_post_fetch_checkin(planning_date),
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


def _ensure_approval_file(run_dir: Path, approved: bool = False) -> dict[str, Any]:
    approval_path = run_dir / "approval.json"
    if approval_path.exists() and not approved:
        existing = read_json(approval_path)
        if existing.get("status") == "approved":
            return existing
    approval = {
        "status": "approved" if approved else "pending",
        "approved_at": datetime.now().isoformat(timespec="seconds")
        if approved
        else None,
        "requires_approval_before_push": True,
    }
    write_json(approval_path, approval)
    return approval


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
        "run_context.json",
        "planning_state.json",
        "approval.json",
        "inputs.json",
        "normalized_snapshot.json",
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
    adapter = build_intervals_adapter(
        mode=config.mode,
        fixture_dir=config.fixture_dir,
        base_url=config.intervals_base_url,
        api_key=config.intervals_api_key,
        athlete_id=config.intervals_athlete_id,
    )
    intervals_data = adapter.fetch_snapshot(
        local_inputs["weekly_checkin"]["planning_date"]
    )
    snapshot = normalize_snapshot(local_inputs, intervals_data)
    bootstrapped_state = bootstrap_athlete_state(
        snapshot, local_inputs["athlete_state"]
    )
    local_inputs["athlete_state"] = bootstrapped_state
    snapshot["athlete_state"] = bootstrapped_state
    prompts = load_prompts(config.prompt_dir)

    ensure_dir(agent_outputs_dir(run_dir))
    write_json(
        run_dir / "inputs.json",
        {
            "local_inputs": local_inputs,
            "intervals_snapshot": intervals_data,
            "prompt_files": sorted(prompts),
        },
    )
    write_json(run_dir / "normalized_snapshot.json", snapshot)
    _ensure_approval_file(run_dir, approved=False)
    context = _run_context_manifest(
        config,
        run_dir,
        local_inputs["weekly_checkin"]["planning_date"],
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
    mark_step_complete(
        run_dir,
        "post_fetch_checkin",
        artifact_path=str(run_dir / "run_context.json"),
    )
    return {
        "run_dir": str(run_dir),
        "normalized_snapshot_path": str(run_dir / "normalized_snapshot.json"),
        "run_context_path": str(run_dir / "run_context.json"),
        "approval_path": str(run_dir / "approval.json"),
    }


def finalize_run(
    run_dir: str,
    final_plan: dict[str, Any] | None = None,
    auto_approve: bool = False,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    snapshot = read_json(run_path / "normalized_snapshot.json")
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
    validation_errors.extend(validate_final_plan(resolved_final_plan))
    if validation_errors:
        raise ValueError("; ".join(validation_errors))

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


def approve_run(
    config: AppConfig, run_dir: str, dry_run: bool = False
) -> dict[str, Any]:
    run_path = Path(run_dir)
    approval_path = run_path / "approval.json"
    approval = read_json(approval_path)
    if approval.get("status") != "approved":
        raise ValueError(
            "Approval is required before pushing. Run finalize-run with --auto-approve or update approval.json to approved first."
        )

    final_plan = read_json(run_path / "final_plan.json")
    validation_errors = validate_final_plan(final_plan)
    if validation_errors:
        raise ValueError("; ".join(validation_errors))
    adapter = build_intervals_adapter(
        mode=config.mode,
        fixture_dir=config.fixture_dir,
        base_url=config.intervals_base_url,
        api_key=config.intervals_api_key,
        athlete_id=config.intervals_athlete_id,
    )
    if (run_path / "planning_state.json").exists():
        finalize_planning_state(run_path, approval_status="approved")
    run_id = run_path.name
    compiled = compile_plan(final_plan, run_id=run_id)
    compiled_notes = compile_nutrition_notes(
        final_plan.get("nutrition_summary", {}), run_id=run_id
    )
    compiled_notes.extend(
        compile_note_events(final_plan.get("weekly_focus_notes", []), run_id=run_id)
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
    write_json(run_path / "compiled_push_payload.json", push_payload)
    push_result = adapter.push_workouts(push_payload, dry_run=dry_run)
    write_json(run_path / "push_result.json", push_result)
    if (run_path / "planning_state.json").exists():
        finalize_planning_state(
            run_path, push_status="dry_run" if dry_run else "pushed"
        )
        if not dry_run:
            athlete_state = update_athlete_state_from_plan(
                load_athlete_state(athlete_state_path()), final_plan, run_path
            )
            write_athlete_state(athlete_state_path(), athlete_state)
    return {
        "compiled_workouts": str(run_path / "compiled_workouts.json"),
        "compiled_notes": str(run_path / "compiled_notes.json"),
        "push_payload": str(run_path / "compiled_push_payload.json"),
        "push_result": str(run_path / "push_result.json"),
    }
