# AGENTS Guide

This file is for coding agents operating in this repository.

## Project Summary

- Python 3.11+, standard library only
- Main package: `triathlon_agent/`
- Tests: `unittest`
- Runtime: local CLI + JSON artifacts + prompt/spec files
- Intervals.icu integration in live mode, fixtures in mock mode

## Important Files

- `README.md`, `AGENT_WORKFLOW.md`, `DESIGN.md`
- `prompts/`, `schemas/`, `triathlon_agent/`, `tests/`
- `fixtures/`, `data/`, `runs/`

## Cursor / Copilot Rules

- No `.cursorrules` file was found.
- No `.cursor/rules/` directory was found.
- No `.github/copilot-instructions.md` file was found.
- Do not assume any hidden editor-specific instructions beyond this file and the repository docs.

## Setup Notes

- No `pyproject.toml`, `requirements.txt`, or third-party dependencies.
- Prefer `python3` everywhere.
- Keep the standard-library-only approach unless explicitly asked otherwise.

## Primary Commands

### Run tests

- Full suite:
  - `python3 -m unittest discover -s tests -v`
- Single test module:
  - `python3 -m unittest tests.test_workflow -v`
- Single test class:
  - `python3 -m unittest tests.test_workflow.WorkflowTests -v`
- Single test method:
  - `python3 -m unittest tests.test_workflow.WorkflowTests.test_prepare_and_finalize_support_agent_orchestration -v`

### Compile / syntax-check Python

- Compile package and tests:
  - `python3 -m compileall triathlon_agent tests`

### Validate JSON manually

- `python3 -m json.tool data/weekly_checkin.json > /tmp/weekly_checkin.validated.json`

### Run CLI in mock mode

- Prepare a run:
  - `python3 -m triathlon_agent.cli prepare-run --mode mock --athlete-profile fixtures/athlete_profile.json --weekly-checkin fixtures/weekly_checkin.json`
- Full stub workflow:
  - `python3 -m triathlon_agent.cli plan-week --mode mock --athlete-profile fixtures/athlete_profile.json --weekly-checkin fixtures/weekly_checkin.json`
- Approve mock run:
  - `python3 -m triathlon_agent.cli approve runs/<run_id> --mode mock --dry-run`

### Run CLI in live mode

- Prepare live run:
  - `python3 -m triathlon_agent.cli prepare-run --mode live --athlete-profile data/athlete_profile.json --weekly-checkin data/weekly_checkin.json`
- Finalize a prepared run:
  - `python3 -m triathlon_agent.cli finalize-run runs/<run_id>`
- Push approved run:
  - `python3 -m triathlon_agent.cli approve runs/<run_id> --mode live`
- Safer live push preview:
  - `python3 -m triathlon_agent.cli approve runs/<run_id> --mode live --dry-run`

## No Dedicated Lint / Formatter

- There is currently no configured `ruff`, `black`, `flake8`, `mypy`, or similar tool.
- Do not invent lint commands in commits or docs.
- Use `compileall` + `unittest` as the minimum verification path.

## Code Style

### General

- Follow the existing style in `triathlon_agent/` and `tests/`.
- Prefer small, deterministic helper functions over clever abstractions.
- Keep runtime behavior inspectable via JSON artifacts.
- Preserve the separation between `prompts/`, `schemas/`, and deterministic execution in `triathlon_agent/`.

### Imports

- Group imports in this order:
  1. `from __future__ import annotations`
  2. standard library imports
  3. local package imports
- Prefer explicit imports over wildcard imports.
- Keep import lists short and readable; use multiline formatting when needed.

### Formatting

- Match the repo’s PEP 8-ish style.
- Use 4 spaces, not tabs.
- Prefer double quotes consistently.
- Keep line length reasonable; existing files wrap long calls over multiple lines.
- Use trailing commas in multiline literals/calls when it improves diffs.

### Types

- Type hints are expected on new or changed functions.
- Use built-in generics like `list[str]`, `dict[str, Any]`, `tuple[...]`.
- `Any` is acceptable at JSON/API boundaries.
- Avoid overengineered type hierarchies; this codebase is JSON-centric.

### Naming

- Functions/variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- JSON keys should stay stable and descriptive; changing them is a contract change.
- Follow existing agent names exactly: `performance_doctor`, `macro_coach`, `race_director`, `phase_alignment_auditor`, `training_philosophy_auditor`, `head_coach_planner`, `validator_coach`, `nutritionist`, `orchestrator`.

### Error Handling

- Prefer explicit, actionable errors over silent failure.
- Preserve current behavior of raising `ValueError` / custom runtime errors when contracts are broken.
- At external boundaries, tolerate missing wellness values, missing optional fields, and `None` numeric API values.
- For Intervals API calls, keep failures descriptive and non-destructive.

### Data / Contract Safety

- If you change an agent output shape, update all of:
  - `schemas/*.json`
  - `triathlon_agent/contracts.py`
  - any writers/readers in `triathlon_agent/workflow.py`
  - tests
- If you change session fields, also inspect:
  - `triathlon_agent/compiler.py`
  - `triathlon_agent/report.py`
  - `schemas/session.json`

### Tests

- Add or update tests for any behavior change.
- Prefer targeted `unittest` coverage over broad manual claims.
- Keep fixture-driven tests deterministic.
- Typical test homes: `tests/test_workflow.py`, `tests/test_cli.py`, `tests/test_normalizer.py`, `tests/test_doctrine.py`.

## Repository-Specific Guidance

- Do not edit `.env` unless the user explicitly asks.
- Do not commit or expose secrets.
- Be careful with `data/*.json`; these are user-owned local inputs.
- `fixtures/*.json` should remain stable unless tests intentionally need new coverage.
- `sample_output/` should only be regenerated when behavior intentionally changes.
- `runs/` is ephemeral; avoid using it for test expectations.

## Verification Checklist Before Finishing

- Run `python3 -m compileall triathlon_agent tests`
- Run `python3 -m unittest discover -s tests -v`
- If JSON templates or fixtures changed, validate them with `python3 -m json.tool`
- If CLI behavior changed, verify at least one representative command manually
- If contracts changed, ensure schemas, validators, and report rendering all agree

## Agent Workflow Expectations

- Prefer the documented agent flow in `AGENT_WORKFLOW.md`.
- Use `prepare-run` + artifact writing + `finalize-run` for production-style orchestration.
- Use `plan-week` mainly for deterministic stub testing and fixture-backed dry runs.
- Never push live workouts without an approval step unless the user explicitly instructs it.
