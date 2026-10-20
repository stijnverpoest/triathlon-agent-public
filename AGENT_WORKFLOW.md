# Agent Workflow

This repository is designed so an LLM coding agent can orchestrate the whole weekly coaching run. It is agent-agnostic: any agent that can run shell commands and read and write files in the repository can be the main agent (for example Claude Code, OpenCode, Codex or Cursor).

## Using it with your agent

- Run the agent from the repository root.
- Give it `prompts/master_prompt.md` as its instructions for a planning run, for example by asking it to read and follow that file when you say `plan my next week`.
- `AGENTS.md` holds the repository conventions. Agents that read `AGENTS.md` automatically pick it up; for others, point their own instruction file (such as `CLAUDE.md`) at it.
- Nothing in the Python layer depends on a specific agent: it only reads and writes the run artifacts listed below.

## User experience

You should be able to say:

```text
plan my next week
```

The main agent then does the following:

1. runs `prepare-run`
2. verifies the snapshot is fresh enough for planning
3. summarizes last week from Intervals activities and wellness
4. asks only for targeted subjective feedback and next-week constraints, then records the answers with `record-checkin` (this re-normalizes the snapshot)
5. reads the normalized snapshot and prompt files
6. calls every specialist prompt in order
7. writes each JSON artifact into the run folder
8. writes `final_plan.json`
9. runs `finalize-run`
10. shows you `weekly_report.md`
11. waits for approval
12. after approval, runs `approve`, which pushes workouts and meaningful nutrition notes, plus any approved threshold updates to Intervals sport settings

The exact behavior contract is documented in `PLAN_MY_WEEK_SPEC.md`.

The freeform phrase `plan my week` should not draft directly. It should only prepare the run, expose the required post-fetch check-in block in `run_context.json`, and wait for the athlete answers before drafting.

## Helper commands

Prepare a run:

```bash
python3 -m triathlon_agent.cli prepare-run \
  --mode live \
  --athlete-profile data/athlete_profile.json \
  --weekly-checkin data/weekly_checkin.json
```

Record the athlete's check-in answers (required before `finalize-run` in live mode):

```bash
python3 -m triathlon_agent.cli record-checkin runs/<run_id> answers.json
```

Re-plan the remaining days of an approved week:

```bash
python3 -m triathlon_agent.cli adjust-week runs/<run_id> --from YYYY-MM-DD --reason "..." --mode live
```

Validate agent artifacts and render the report:

```bash
python3 -m triathlon_agent.cli finalize-run runs/<run_id>
```

Compile and push after approval:

```bash
python3 -m triathlon_agent.cli approve runs/<run_id> --mode live
```

Dry-run the push against live data:

```bash
python3 -m triathlon_agent.cli approve runs/<run_id> --mode live --dry-run
```

`approve` refuses when `final_plan.json` changed since approval, and skips events the athlete edited in Intervals since the last push (`--overwrite-edited` forces them).

## Required artifact files

- `runs/<run_id>/normalized_snapshot.json`
- `runs/<run_id>/agent_outputs/performance_doctor.json`
- `runs/<run_id>/agent_outputs/macro_coach.json`
- `runs/<run_id>/agent_outputs/race_director.json`
- `runs/<run_id>/agent_outputs/phase_alignment_auditor.json`
- `runs/<run_id>/agent_outputs/training_philosophy_auditor.json`
- `runs/<run_id>/agent_outputs/head_coach_planner.json`
- `runs/<run_id>/agent_outputs/validator_coach.json`
- `runs/<run_id>/agent_outputs/nutritionist.json`
- `runs/<run_id>/final_plan.json`

The final report is rendered to:

- `runs/<run_id>/weekly_report.md`

## Contracts

Use the prompt files in `prompts/` together with the schema files in `schemas/`.

The local validator enforces the same required-field contracts via `triathlon_agent/contracts.py`.
