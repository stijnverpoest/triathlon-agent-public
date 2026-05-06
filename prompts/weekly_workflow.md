# Weekly Workflow Definition

## Mandatory order

1. run `python3 -m triathlon_agent.cli prepare-run ...`
2. read `runs/<run_id>/normalized_snapshot.json` and `runs/<run_id>/run_context.json`
3. verify the activity and wellness snapshot is fresh enough for the requested planning week
4. summarize last week from Intervals before asking follow-up questions
5. ask only for subjective feedback and upcoming constraints that Intervals cannot know
6. call `performance_doctor` and write `agent_outputs/performance_doctor.json`
7. call `macro_coach` and write `agent_outputs/macro_coach.json`
8. call `race_director` and write `agent_outputs/race_director.json`
9. call `head_coach_planner` and write `agent_outputs/head_coach_planner.json`
10. call `phase_alignment_auditor` and write `agent_outputs/phase_alignment_auditor.json`
11. call `training_philosophy_auditor` and write `agent_outputs/training_philosophy_auditor.json`
12. call `validator_coach` and write `agent_outputs/validator_coach.json`
13. revise the plan if needed, then call `nutritionist` and write `agent_outputs/nutritionist.json`
14. synthesize `final_plan.json` as the main agent
15. run `python3 -m triathlon_agent.cli finalize-run runs/<run_id>`
16. present the narrative weekly report for approval
17. after approval only: compile workouts and meaningful nutrition notes
18. run `intervals_syntax_guard` on workouts
19. push workouts plus nutrition notes to Intervals.icu with `python3 -m triathlon_agent.cli approve runs/<run_id> --mode <mock|live>`

## Required final report content

- strategic overview
- readiness and risk
- macro-cycle decision
- seven-day plan
- session details
- nutrition guidance
- changes versus prior week
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
