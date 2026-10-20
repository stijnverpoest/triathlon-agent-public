# Weekly Workflow Definition

## Mandatory order

1. run `python3 -m triathlon_agent.cli prepare-run ...`
2. read `runs/<run_id>/normalized_snapshot.json` and `runs/<run_id>/run_context.json`
3. verify the activity and wellness snapshot is fresh enough for the requested planning week
4. summarize last week from Intervals before asking follow-up questions
5. ask only for subjective feedback and upcoming constraints that Intervals cannot know (the questions are in `run_context.json`, including the red-flag screen, pain scores, missed-session reasons and open decisions from earlier weeks); write the answers to a JSON file and run `python3 -m triathlon_agent.cli record-checkin runs/<run_id> <answers.json>`, then re-read `normalized_snapshot.json`
6. call `performance_doctor` and write `agent_outputs/performance_doctor.json`
7. call `macro_coach` and write `agent_outputs/macro_coach.json`
8. call `race_director` and write `agent_outputs/race_director.json`
9. call `head_coach_planner` and write `agent_outputs/head_coach_planner.json`
10. call `phase_alignment_auditor` and write `agent_outputs/phase_alignment_auditor.json`
11. call `training_philosophy_auditor` and write `agent_outputs/training_philosophy_auditor.json`
12. call `validator_coach` and write `agent_outputs/validator_coach.json`
13. revise the plan if needed, then call `nutritionist` and write `agent_outputs/nutritionist.json`
14. synthesize `final_plan.json` as the main agent
15. run `python3 -m triathlon_agent.cli finalize-run runs/<run_id>`; if the printed coaching audit shows warnings, either revise the plan and finalize again, or explain each warning to the athlete in the report presentation
16. present the narrative weekly report for approval
17. after approval only: compile workouts and meaningful nutrition notes
18. run `intervals_syntax_guard` on workouts
19. push workouts plus nutrition notes to Intervals.icu with `python3 -m triathlon_agent.cli approve runs/<run_id> --mode <mock|live>`; the same command pushes approved threshold updates (`threshold_testing.threshold_updates` with `apply: true`) to Intervals sport settings and records the test in `data/athlete_state.json`

Approval is bound to the exact `final_plan.json`: after any revision, run `finalize-run` again and get a fresh approval. Events the athlete edited or moved in Intervals since the last push are skipped and listed in `edited_events.json`; show them and only use `--overwrite-edited` when the athlete agrees.

## Mid-week adjustment

When the athlete reports illness, a bad night, travel or a missed key session mid-week, do not wait for next week: run `python3 -m triathlon_agent.cli adjust-week runs/<run_id> --from <first day to re-plan> --reason "..." --mode <mock|live>`, revise only the remaining days (don't cram missed work), finalize, get approval, and approve.

After the push, `approve` writes `orphan_events.json` and prints any coach-created workouts or notes in the pushed date range that the new plan no longer contains (for example after renaming a session in a revised plan). Nothing is deleted automatically: show the list to the athlete so they can remove the superseded events in Intervals.icu.

## Required final report content

- strategic overview
- readiness and risk
- macro-cycle decision
- seven-day plan
- session details
- nutrition guidance
- changes versus prior week
- threshold testing status and proposed threshold updates
- approval checkpoint

## Failure handling

If data is weak:

- state what is missing
- do not ask the athlete to restate workouts that should have come from Intervals unless live data is unavailable
- simplify the week
- reduce progression ambition
- keep the approval gate

## Artifact rule

Do not keep reasoning only in chat. Write every agent output and the final plan into the run folder so the full run is inspectable and resumable.

## Scheduling rule

Optimize the best weekly combination first. Treat weekday preferences as soft unless they are based on access constraints such as club sessions or pool opening hours.
