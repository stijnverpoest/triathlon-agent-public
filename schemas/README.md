# Agent Output Schemas

These schemas define the file contracts the OpenCode main agent should write during a weekly planning run.

- `performance_doctor.json`
- `macro_coach.json`
- `race_director.json`
- `phase_alignment_auditor.json`
- `training_philosophy_auditor.json`
- `head_coach_planner.json`
- `validator_coach.json`
- `nutritionist.json`
- `final_plan.json`
- `intervals_syntax_guard.json`
- `session.json`

In production, the OpenCode main agent should read these schemas before writing artifacts into `runs/<run_id>/agent_outputs/` and `runs/<run_id>/final_plan.json`.

The local Python validator in `triathlon_agent/contracts.py` enforces the same required-field contracts without relying on external libraries.
