from __future__ import annotations

import argparse
import sys

from .config import load_config
from .utils import read_json
from .workflow import (
    adjust_week,
    approve_run,
    finalize_run,
    plan_week,
    prepare_run,
    record_checkin,
)


def _rewrite_phrase_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    joined = " ".join(argv[:3]).strip().lower()
    if argv[0].strip().lower() == "plan my week" or joined == "plan my week":
        remainder = argv[1:] if argv[0].strip().lower() == "plan my week" else argv[3:]
        return ["prepare-run", *remainder]
    return argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local multi-agent triathlon coaching workflow"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan-week", help="Create a weekly coaching draft"
    )
    plan_parser.add_argument("--mode", choices=["mock", "live"], default=None)
    plan_parser.add_argument("--athlete-profile", required=True)
    plan_parser.add_argument("--weekly-checkin", required=True)
    plan_parser.add_argument(
        "--race-calendar",
        help="Optional supplemental race calendar JSON. If omitted, race events are pulled from Intervals.icu calendar data.",
    )
    plan_parser.add_argument("--output-dir")
    plan_parser.add_argument("--auto-approve", action="store_true")
    plan_parser.add_argument("--dry-run", action="store_true")

    prepare_parser = subparsers.add_parser(
        "prepare-run",
        help="Prepare a run folder for OpenCode-driven agent orchestration",
    )
    prepare_parser.add_argument("--mode", choices=["mock", "live"], default=None)
    prepare_parser.add_argument("--athlete-profile", required=True)
    prepare_parser.add_argument("--weekly-checkin", required=True)
    prepare_parser.add_argument("--race-calendar")
    prepare_parser.add_argument("--output-dir")

    finalize_parser = subparsers.add_parser(
        "finalize-run",
        help="Validate agent outputs and render the weekly report",
    )
    finalize_parser.add_argument("run_dir")
    finalize_parser.add_argument("--auto-approve", action="store_true")
    finalize_parser.add_argument(
        "--allow-missing-checkin",
        action="store_true",
        help="Finalize a live run even though record-checkin was never run.",
    )

    checkin_parser = subparsers.add_parser(
        "record-checkin",
        help="Merge the athlete's post-fetch check-in answers into a prepared run and re-normalize it",
    )
    checkin_parser.add_argument("run_dir")
    checkin_parser.add_argument("answers", help="JSON file with check-in fields to merge")

    adjust_parser = subparsers.add_parser(
        "adjust-week",
        help="Re-plan the remaining days of an approved week in a new linked run",
    )
    adjust_parser.add_argument("run_dir", help="The approved run to adjust")
    adjust_parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    adjust_parser.add_argument("--reason", required=True)
    adjust_parser.add_argument("--mode", choices=["mock", "live"], default=None)
    adjust_parser.add_argument("--output-dir")

    approve_parser = subparsers.add_parser(
        "approve", help="Approve a draft and push workouts"
    )
    approve_parser.add_argument("run_dir")
    approve_parser.add_argument("--mode", choices=["mock", "live"], default=None)
    approve_parser.add_argument("--dry-run", action="store_true")
    approve_parser.add_argument(
        "--overwrite-edited",
        action="store_true",
        help="Also overwrite calendar events the athlete edited or moved since the last push.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    resolved_argv = _rewrite_phrase_args(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(resolved_argv)
    config = load_config(mode=getattr(args, "mode", None))

    if args.command == "plan-week":
        if config.mode == "live":
            print(
                "`plan-week` is a deterministic stub workflow and cannot be used in live mode. "
                "Start with `prepare-run`, summarize the fetched data, ask the weekly check-in questions, then draft the plan.",
                file=sys.stderr,
            )
            return 2
        result = plan_week(
            config,
            athlete_profile_path=args.athlete_profile,
            weekly_checkin_path=args.weekly_checkin,
            race_calendar_path=args.race_calendar,
            output_dir=args.output_dir,
            auto_approve=args.auto_approve,
            dry_run=args.dry_run,
        )
        print(f"Run created: {result['run_dir']}")
        print(f"Report: {result['report_path']}")
        print(f"Dashboard: {result['dashboard_path']}")
        print(f"Approval status: {result['approval_status']}")
        if result.get("approval_result"):
            print(
                f"Compiled workouts: {result['approval_result']['compiled_workouts']}"
            )
            print(f"Compiled notes: {result['approval_result']['compiled_notes']}")
            print(f"Push result: {result['approval_result']['push_result']}")
        return 0

    if args.command == "prepare-run":
        result = prepare_run(
            config,
            athlete_profile_path=args.athlete_profile,
            weekly_checkin_path=args.weekly_checkin,
            race_calendar_path=args.race_calendar,
            output_dir=args.output_dir,
        )
        print(f"Run created: {result['run_dir']}")
        print(f"Normalized snapshot: {result['normalized_snapshot_path']}")
        print(f"Run context: {result['run_context_path']}")
        print(f"Approval file: {result['approval_path']}")
        print(
            "Next: ask the post-fetch check-in, then "
            f"`python -m triathlon_agent.cli record-checkin {result['run_dir']} <answers.json>`"
        )
        return 0

    if args.command == "record-checkin":
        try:
            result = record_checkin(args.run_dir, read_json(args.answers))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Check-in recorded: {result['weekly_checkin_path']}")
        print(f"Answered fields: {', '.join(result['answered_fields'])}")
        print(f"Normalized snapshot rebuilt: {result['normalized_snapshot_path']}")
        return 0

    if args.command == "adjust-week":
        try:
            result = adjust_week(
                config,
                args.run_dir,
                from_date=args.from_date,
                reason=args.reason,
                output_dir=args.output_dir,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Adjustment run created: {result['run_dir']}")
        for row in result["done_so_far"]:
            print(
                f"  {row['date']} {row['session_name']}: {row['status']} "
                f"({row['actual_min']}/{row['planned_min']} min)"
            )
        print(f"Remaining sessions to revise: {result['remaining_sessions']}")
        print(f"Adjustment context: {result['adjustment_context_path']}")
        return 0

    if args.command == "finalize-run":
        try:
            result = finalize_run(
                args.run_dir,
                auto_approve=args.auto_approve,
                allow_missing_checkin=args.allow_missing_checkin,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Run finalized: {result['run_dir']}")
        audit = result.get("coaching_audit") or {}
        print(f"Coaching audit: {audit.get('status', 'n/a')}")
        for finding in audit.get("findings", []):
            print(f"  WARNING [{finding['code']}]: {finding['message']}")
        print(f"Report: {result['report_path']}")
        print(f"Dashboard: {result['dashboard_path']}")
        print(f"Approval status: {result['approval_status']}")
        return 0

    if args.command == "approve":
        try:
            result = approve_run(
                config,
                args.run_dir,
                dry_run=args.dry_run,
                overwrite_edited=args.overwrite_edited,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        for event in result.get("edited_events", []):
            verb = "OVERWRITTEN" if result.get("edited_events_overwritten") else "SKIPPED"
            print(
                f"  {verb} event you edited since the last push: {event['date']} {event['name']} "
                f"({', '.join(event['changes'])})"
            )
        print(f"Compiled workouts: {result['compiled_workouts']}")
        print(f"Compiled notes: {result['compiled_notes']}")
        print(f"Push result: {result['push_result']}")
        for orphan in result.get("orphan_events", []):
            print(
                f"  NOTE superseded coach event still on the calendar: "
                f"{orphan['date']} {orphan['name']} (id {orphan['id']})"
            )
        print(f"Threshold update result: {result['threshold_update_result']}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
