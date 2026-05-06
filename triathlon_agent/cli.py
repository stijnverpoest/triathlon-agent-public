from __future__ import annotations

import argparse
import sys

from .config import load_config
from .workflow import approve_run, finalize_run, plan_week, prepare_run


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

    approve_parser = subparsers.add_parser(
        "approve", help="Approve a draft and push workouts"
    )
    approve_parser.add_argument("run_dir")
    approve_parser.add_argument("--mode", choices=["mock", "live"], default=None)
    approve_parser.add_argument("--dry-run", action="store_true")

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
        return 0

    if args.command == "finalize-run":
        result = finalize_run(args.run_dir, auto_approve=args.auto_approve)
        print(f"Run finalized: {result['run_dir']}")
        print(f"Report: {result['report_path']}")
        print(f"Dashboard: {result['dashboard_path']}")
        print(f"Approval status: {result['approval_status']}")
        return 0

    if args.command == "approve":
        result = approve_run(config, args.run_dir, dry_run=args.dry_run)
        print(f"Compiled workouts: {result['compiled_workouts']}")
        print(f"Compiled notes: {result['compiled_notes']}")
        print(f"Push result: {result['push_result']}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
