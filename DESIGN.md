# Design

## Goal

Implement a local weekly coaching workflow where an LLM coding agent (any agent; the workflow is agent-agnostic) is the real orchestrator and the Python layer handles data preparation, artifact validation, compilation, and push.

## Runtime model

The system is on-demand only.

The intended production sequence is:

1. the main agent runs `prepare-run`
2. the Python helper layer pulls Intervals profile, sport settings, wellness, activities, and calendar events
3. the Python helper layer normalizes the data into one coaching snapshot
4. the main agent verifies that the fetched activity and wellness data are fresh enough for planning
5. the main agent summarizes the prior week from Intervals data and asks only for subjective feedback plus next-week constraints
6. the main agent calls specialist prompts in order and writes JSON artifacts into the run folder
7. the main agent writes `final_plan.json`
8. the Python helper layer runs `finalize-run` to validate artifacts and render the report
9. the main agent presents the weekly report and waits for approval
10. after approval, the Python helper layer compiles workouts plus meaningful daily nutrition notes and pushes them through the adapter

The repository also keeps a deterministic stub workflow for tests and offline fixture-backed runs.

## Why there are two layers

The prompt files are the production reasoning layer, while the local Python implementation remains deterministic for execution so that:

- dry-run mode works without credentials
- tests are stable
- compilation and approval are verifiable
- Intervals write-back stays deterministic and auditable

## Data sources

- local files: athlete profile, weekly check-in, optional supplemental race notes
- Intervals live or mock data: profile, sport settings, wellness, activities, events

Races are sourced only from the Intervals calendar (RACE events). The local race calendar file is optional and can only fill missing notes on a race that is already on the Intervals calendar; it never adds a race or overrides its date, name, priority or distance. `athlete_state.json` keeps history and long-term memory (weekly actuals, race results, injury log, open decisions), never the priority race.

## Feedback loop

- `prepare-run` fetches and normalizes; `record-checkin` merges the athlete's answers and re-normalizes from the stored fetch
- `finalize-run` validates against `schemas/*.json` (nested required keys), runs the coaching audit, and binds approval to the plan's sha256
- `approve` skips events the athlete edited in Intervals since the last push and records weekly actuals in `athlete_state.json`
- `adjust-week` re-plans the remaining days of an approved week in a linked run

Wellness baselines for HRV, resting HR, and sleep are derived from Intervals history whenever enough data exists. Manual baseline fields are optional fallback overrides only.

## Run artifacts

Every run writes a folder containing:

- `inputs.json`
- `normalized_snapshot.json`
- `agent_outputs.json`
- `draft_plan.json`
- `final_plan.json`
- `weekly_report.md`
- `approval.json`
- `compiled_workouts.json` after approval
- `compiled_notes.json` after approval
- `compiled_push_payload.json` after approval
- `push_result.json` after approval
- `run_context.json`
- `agent_outputs/*.json`

## Key implementation choices

- standard library Python only
- JSON for local runtime data contracts
- schemas in `schemas/` and runtime validation in `triathlon_agent/contracts.py`
- conservative Intervals syntax generation using `description`
- one primary metric per workout for sync safety
- strength as structured `WeightTraining` notes, not guaranteed Garmin-native structure
- the main agent (any LLM coding agent) owns coaching reasoning in production
- `Joe Friel` governs periodization and 3:1 block logic
- `Jack Daniels` governs run taxonomy
- `Andrew Coggan` governs bike taxonomy and IF/TSS framing
- nutrition is personalized from body metrics, readiness, doctrine, and session type, and can be pushed as separate note events

## Deterministic workout targeting rules

- bike -> `%FTP`
- easy or aerobic-only run -> `HR`
- quality run -> `Pace` from Intervals threshold pace
- strength -> structured notes
- swims -> simple planned `Swim` events when structure does not need to be encoded

## Doctrine enforcement

- `macro_coach` emits Friel block week, cycle type, and phase focus
- `head_coach_planner` emits Daniels types for runs and Coggan levels plus IF/TSS for bikes
- `phase_alignment_auditor` checks whether the week matches the current phase and race timing
- `training_philosophy_auditor` checks Daniels, Coggan, and Friel compliance before the plan is treated as final

## Validator and syntax guard

- `validator_coach` critiques training quality, sequencing, and adherence
- the orchestrator incorporates validator edits into the final plan
- `intervals_syntax_guard` operates only after approval and protects parseability before push

## Production helper commands

- `prepare-run`: fetches data and initializes the run folder
- `finalize-run`: validates agent artifacts and renders the report
- `approve`: compiles and pushes after approval
- `plan-week`: deterministic stub workflow retained for tests and fixtures

## Known limitations

- the agent prompt-call loop is documented rather than embedded inside the Python CLI
- swim structure is lighter than bike or run structure
- strength parsing is intentionally conservative

## `plan my week` contract

The user-facing production behavior for `plan my week` is specified in `PLAN_MY_WEEK_SPEC.md`.
