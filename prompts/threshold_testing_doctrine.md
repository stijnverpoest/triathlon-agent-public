# Threshold Testing Doctrine

Keep FTP, threshold pace, and LTHR honest so every %FTP / %Pace / HR-zone target trains the intended system. Test often enough that zones stay correct, never so often that testing becomes training load or injury risk.

The deterministic assessment lives in `normalized_snapshot.json` under `threshold_testing` (computed by `triathlon_agent/threshold_testing.py`). Use it as the source of truth; do not re-derive dates from memory.

## Protocols

- **Bike, default (athlete preference): `coggan_20`**, the Coggan/Allen 20-minute FTP test
  - 20' warmup, 3x 1' high cadence, 5' all-out blowout, 10' easy, **20' all-out TT**, cooldown
  - FTP = 20' average power x 0.95
  - LTHR from this protocol is a low-confidence estimate only and is never auto-applied
- **Bike, option B: `friel_30`**, Friel's 30-minute solo time trial
  - 15' warmup, 3x 1' openers, **30' all-out TT split into 10' + 20' laps**, cooldown
  - FTP = 30' average power; bike LTHR = average HR of the final 20'
  - offer it when bike LTHR is stale or the athlete sets `bike_test_protocol: friel_30` in the weekly check-in
- **Run: `friel_30_run`**, Friel's 30-minute solo run time trial
  - 15' warmup, 4 strides, **30' all-out TT split into 10' + 20' laps** on flat ground, cooldown
  - threshold pace = 30' average pace; run LTHR = average HR of the final 20'
- bike tests are indoors on the trainer with ERG off (`freeride` steps)
- a recent flat road race of 10k or longer substitutes for the run test (threshold pace from race pace)

## Frequency (Friel "rest & test")

- **a recovery week ends with a test only when a discipline is due**, late in the week, after easy days: test what the A-race needs (a trail block tests the run; the bike only when the FTP is stale or overdue and bike targets are prescribed). Do not turn every recovery week into a maximal effort
- never test in a return-from-break week (`training_load.return_from_break.status` `returning`)
- the first week of a new block (block week 1, right after a recovery week) is also a valid test slot when readiness is green and no race falls inside its taper window (`placement: "block_week_1"`)
- **alternate bike and run** across recovery weeks, so each discipline is tested about every 8 weeks
- a discipline last tested 28+ days ago is eligible for this routine rest & test even before it is formally `due`
- in the first recovery week after an A-race, test **both** disciplines (48h apart); a race result can substitute for the run test
- skip a recovery-week test only for a blocker or when both disciplines were tested in the last 28 days, and state the reason in `threshold_tests.rationale`
- minimum 42 days between tests of the same discipline
- 28 days minimum when an early trigger fires: set FTP more than 5% away from eFTP, or a return from 7+ days of illness or injury (after 10+ days back)
- 84+ days since the last test is `overdue` and gets priority
- if both are overdue, schedule both in one recovery week, bike first, at least 48h apart

## Never test when

- inside the final 21 days before an A-race, or 7 days before a B-race
- readiness is red; with yellow readiness only the bike test is allowed
- a SICK/INJURED note ended less than 10 days ago
- a niggle is reported (run test), or an injury note exists (run test)
- the week is a load (other than block week 1), sharpen, taper or transition week

## Placement and load

- the test replaces the weekend long session of that discipline; it is not stacked on top
- no strength work the day before, and no quality session the day before or after
- the test is a key session with a fixed duration; do not trim it, move it or skip it instead

## Stale FTP between tests

- `threshold_testing.bike` compares the set FTP with Intervals' rolling eFTP (`icu_rolling_ftp`, never a single ride's `icu_pm_ftp`)
- more than 5% drift marks the bike threshold `stale` and exposes `ftp_target_scale` (eFTP / FTP)
- while stale, scale every %FTP target by `ftp_target_scale` or cap it with an HR / RPE ceiling
- `proposed_updates` then also carries an interim FTP (`source: "eftp_interim"`); above ~10% drift it is proposed with `apply: true` because a stale FTP mis-targets every ride and makes Intervals under-count ride load. It is still pushed only after the athlete approves the week, and the approval checkpoint names it
- when eFTP sits more than ~5% above the set FTP, targets are too easy: scale them up or propose the update
- swim threshold (CSS) is tracked for staleness only and never scheduled automatically

## After a test

- `prepare-run` pulls the test's laps from Intervals and computes proposed updates (`threshold_testing.proposed_updates`)
- updates with `apply: true` are pushed to Intervals sport settings **only after the athlete approves the weekly plan**
- low-confidence values, changes under 1%, or changes over 25% are shown but not auto-applied
