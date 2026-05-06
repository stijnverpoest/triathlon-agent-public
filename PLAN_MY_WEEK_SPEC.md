# `plan my week` Behavior Spec

## Goal

When the athlete says `plan my week` or `plan my next week`, the system should behave like a live coaching intake, not like a generic chat prompt.

The workflow must:

- fetch fresh Intervals.icu data first
- summarize what actually happened last week from that data
- ask only for information Intervals cannot know
- draft the next week
- require approval before anything is pushed back to Intervals.icu

## Scope resolution

The trigger covers requests such as:

- `plan my week`
- `plan my next week`
- `plan the week of YYYY-MM-DD`

Default behavior:

- if no week is specified, plan the next training week relative to today
- if a week is specified, use that requested week explicitly
- the system must resolve one concrete planning date before fetching data

## Mandatory live-first sequence

For production planning in live mode, the system must follow this order:

1. resolve the requested planning week
2. run `prepare-run` in live mode
3. fetch profile, sport settings, wellness, activities, and calendar events from Intervals.icu
4. normalize the snapshot
5. verify snapshot freshness
6. summarize last week from the fetched data
7. ask the athlete a targeted post-fetch check-in
8. draft the weekly plan
9. render the report
10. wait for approval
11. only after approval, compile and push workouts plus meaningful nutrition notes

The system must not draft a live week from checked-in `runs/` artifacts, stale local snapshots, or memory when a fresh Intervals fetch is available.

## Freshness gate

Fresh data is mandatory before drafting a live week.

The prepared snapshot should expose at least:

- requested planning date
- fetch timestamp
- latest activity local date
- latest wellness date

The planner must block live drafting if either of these is true:

- the latest activity date is more than 1 day behind the planning date
- the latest wellness date is more than 1 day behind the planning date

If the snapshot is stale, the system must say so clearly and stop before planning, unless the athlete explicitly asks to proceed with stale data.

## What the system must infer automatically

After the live fetch, the system should infer as much as possible without asking the athlete to repeat it.

It should summarize:

- completed sessions from the prior week
- missed, shortened, or swapped sessions when a prior planned week exists
- recent load, frequency, and long-session execution
- the most relevant readiness signals from wellness and training history
- race-calendar context and current phase implications

If no prior planned week exists, the system should still summarize the actual completed training week.

## Mandatory post-fetch athlete check-in

After the data summary and before drafting, the system must ask a targeted check-in that covers only information Intervals does not reliably know.

It should ask for:

- pain, niggles, soreness, or unusual tightness
- how key sessions felt during and after
- work stress and life stress
- motivation and subjective readiness
- coming-week constraints such as travel, time caps, pool access, and days that cannot be doubled

It should not ask the athlete to manually list completed workouts if live activity data has already been fetched.

Recommended interaction pattern:

- show a short evidence-based summary of last week first
- ask the athlete to confirm or correct that summary only where needed
- ask one compact block of targeted questions for the missing subjective data and next-week constraints

## Drafting rules

Once the live summary and athlete check-in are complete, the planner should:

- preserve the phase logic and race priorities
- use last week as the starting context for progression or restraint
- keep the week executable within the athlete's real constraints
- preserve key routine anchors unless a better sequence is materially better
- make all major deviations from routine explicit

The plan should clearly state:

- weekly theme
- key sessions
- why the week exists
- cut-first rules when fatigue or constraints rise
- fueling priorities for meaningful sessions

## Approval and push

After drafting, the system must:

1. write all run artifacts
2. run `finalize-run`
3. present `weekly_report.md`
4. wait for explicit athlete approval

Before approval:

- no workouts are pushed
- no nutrition notes are pushed

After approval:

- run `approve`
- compile workouts and meaningful nutrition notes
- run the syntax guard on workouts
- push the approved payload to Intervals.icu

## Offline and mock behavior

If the workflow is intentionally run in mock or offline mode, the system must say so explicitly.

In that mode it may use fixtures instead of live data, but it should not pretend the fetched snapshot is current live athlete data.

## Non-goals

`plan my week` is not:

- a push command
- a request to reuse the last checked-in plan without re-fetching live data
- permission to skip the athlete check-in

## Acceptance criteria

The behavior is correct when all of the following are true:

- `plan my week` starts with a live fetch in production mode
- stale activity or wellness data blocks drafting by default
- the system summarizes last week from Intervals before asking questions
- the athlete is not asked to manually reconstruct completed training if the data is available
- the athlete is asked for subjective and constraint data before the week is drafted
- the final plan is reviewable before any Intervals write-back happens
- Intervals push happens only after explicit approval
