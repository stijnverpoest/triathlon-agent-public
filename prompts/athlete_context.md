# Athlete Context Template

Maintain three local sources of truth:

1. athlete profile
2. weekly check-in
3. race calendar

## Athlete profile should contain

- training background
- time constraints
- hard constraints
- soft preferences
- decision policy for when coaching logic should override routine
- injury history
- sleep and recovery context
- coaching tone preference

Baselines for HRV, resting HR, and sleep should be derived from Intervals wellness history whenever possible. Manual profile baseline fields are optional fallback overrides only.

## Weekly check-in should contain

- planning date
- last week summary
- missed sessions
- unusual fatigue or life stress
- `open_water_mode` so the system knows whether pool-season swim constraints still apply
- this week time constraints
- double-session capacity by day if relevant
- time caps by day if relevant
- motivation
- readiness
- pain or niggles, plus `pain_scores` (0-10 per body part)
- illness symptoms and a red-flag screen (chest pain, palpitations, disproportionate breathlessness, dizziness, fever)
- subjective sleep quality and work stress
- reasons for missed sessions (illness and travel are not adherence problems)
- upcoming life events in the next 2-4 weeks
- a race debrief when a race happened
- available hours this week, which bind as a cap
- athlete notes

A blank answer is unknown, never "fine".

## Races come from Intervals

The Intervals.icu calendar (RACE_A / RACE_B / RACE_C events) is the only source of races, priorities and dates. The optional local race calendar can only add notes to a race that is already on the Intervals calendar. It never adds a race or changes its date, name or priority. The profile's goal text is background, not a race list.

## Optional local race notes may contain

- race name
- date
- priority
- distance
- event-demand notes

The system must explicitly surface assumptions when any of these are incomplete.

## Constraint handling

- hard constraints include access constraints such as club sessions, pool opening hours, travel, or real life scheduling blocks
- soft constraints include athlete preferences such as preferred rest day or preferred long-session day
- optimize the weekly plan around training quality and phase alignment first, then fit soft preferences where possible
- when `open_water_mode` is `false`, pool-season swim constraints can remain hard
- when `open_water_mode` is `true`, the planner may relax pool-season-only constraints and place swims more freely
