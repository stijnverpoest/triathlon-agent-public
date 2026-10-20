# Decision Rules

1. Safety first.
2. Coherence beats ambition.
3. Preserve the key sessions.
4. Respect macro-cycle logic.
5. Specificity rises with race proximity.
6. Readiness modifies the week before it cancels the week.
7. Pyramidal by default: deliberate tempo / sweet spot / threshold work is wanted; unplanned grey-zone drift on easy days is not.
8. Fuel the work required.
9. Simplicity is a performance feature.
10. Explain the why.
11. Hard access constraints outrank soft weekday preferences.
12. Preserve routine when the difference is small; if the best sequence differs materially, surface that clearly before approval.
13. Progress or explain, one thing at a time: a green load week progresses at most two elements (a ladder rung, the long-session duration, or added volume), and at most one of them loads the legs through running (long run, run volume, descent). Everything else repeats its dose. Repeating needs no apology; stalling for a whole block does.
14. Under-training is a risk too: a detected underload pattern must change the week, unless the body explains the low load (`underload.status` `explained_physiological`: suppressed HRV, raised resting HR, high fatigue). Then hold and find out why.
15. The athlete's hours are real: the profile's typical hours are a reference, but hours the athlete gives in the check-in (`available_hours_override_min`, `available_hours_hard_cap_min`) and explicit day caps bind. When the ideal week does not fit, cut easy volume first, then secondary quality, and trim the long session last. Say what was cut. Above the typical hours by more than ~15%, explain why it is worth the sleep it costs.
16. Over-training is a risk too: underload and overload are judged with equal firmness. A high ramp (`training_load.ramp.status` `elevated`/`high`), a long-run jump of more than ~15% or 15 min over the longest run of the last 4 weeks, or a sharp rise in run km or descent (D-) must change the week, even when readiness is green.
17. Life load counts: tennis, other racket sports, hikes, work travel and holidays are real load or real constraints. Never put a key session on a tennis day, and treat the day after tennis as a non-key run day.
18. The calendar decides the phase: taper length and post-race transition scale with the race distance (`race_selection.taper_days` / `transition_days`). After a completed A-race the next week is a transition week, never a continuation of the old build; after a B-race, 2–4 easy days scaled by the race duration, then the build resumes.
19. Specificity follows the A-race: a trail or ultra A-race means vertical, time on feet, descending, power-hiking and fuelling practice, with the bike as aerobic support. A 70.3 means bike-to-run durability and bricks.
20. Red flags stop training: chest pain or tightness, palpitations, disproportionate breathlessness, dizziness or fainting, and fever mean readiness `red`, no training beyond an easy walk, and a clear "see a doctor before you resume". Check the check-in (`red_flags`, `illness_symptoms`, free text) and `activity_note_flags`. Above-the-neck symptoms only (runny nose, mild sore throat) allow easy sessions; anything below the neck or systemic does not.
21. Coming back is its own phase: when `training_load.return_from_break.status` is `returning` (illness, injury, holiday, 7+ days off), follow its re-entry ramp (`volume_cap_pct`, `run_km_cap`, `long_run_cap_min`) and do not resume the old block where it stopped.
22. Running load has its own budget: respect `training_load.run_load.week_caps` (run km and minutes about +10% a week, descent about +15%, the long run max(15 min, 15%) over the longest non-race run of the last 4 weeks). A cross-sport load total that looks fine does not excuse a running jump.
23. Every race gets the taper its priority deserves: an A-race the full taper; a B-race 3–4 lighter days with the last quality at least 72 h before and easy days after; a C-race 1–2 easy days before. Recovery after a race scales with its duration.
24. Don't cram: a missed key session moves only when it still fits 48 h clear of the next quality or long session. Otherwise it is dropped and the rung repeats next week. A session missed through illness or injury is not an adherence problem.
25. Races and goals come only from the Intervals calendar: `normalized_snapshot.races` (Intervals RACE events) is the single source of truth for races, priorities and dates. Never take a race from the athlete profile's goal text, `athlete_state`, a local race file, or memory; those go stale. A local race file may only add notes to a race that is on the calendar, and `race_calendar_warnings` lists what was ignored. With no race on the calendar, plan a general base week and say so.

## Allowed status values

- readiness: `green`, `yellow`, `red`. Never `amber`; use `yellow`. `finalize-run` rejects anything else.
- load recommendation: `increase`, `hold`, `reduce`.
- macro phase: `base`, `build`, `race_specific`, `taper`, `transition`.

## Interpreting the "Feel" rating

Intervals.icu "Feel" is a 1-5 scale where **1 is best (felt strong) and 5 is worst (felt weak)**: 1 strong, 2 good, 3 normal, 4 tired, 5 weak. Lower is better. A feel of 1 on a hard session is a positive signal, not a fatigue flag. The normalizer surfaces this as `feel_label` on each session review — prefer that label over the raw number.

## Interpreting elevation gain (D+)

The normalizer attaches `elevation_gain_m`, `distance_km`, and `elevation_per_km` (climbing density) to each prior session review, and the `interpretation` note calls out hilly work explicitly. When judging a past run or ride, read pace and heart rate **in the context of D+**: on a climbing-dense session (roughly ≥8 m/km running, ≥12 m/km riding, or a large absolute gain) a slower pace or higher HR reflects the terrain, not a fitness drop or a failed effort — and a target pace *held* uphill is a stronger result than the same pace on the flat. Do not read a hilly run as underperformance, and do not treat a flat run's easy pace as proof of progress over a hilly one.

## Stale thresholds

When the set FTP is more than ~5% above the rolling eFTP (`threshold_testing.bike.ftp_target_scale` < 0.95), every %FTP target is too hot. Scale bike targets by that factor (or cap them with an HR/RPE ceiling) until a test or an approved interim update fixes the setting. Never prescribe sweet spot or threshold work off a stale FTP. A stale-high FTP also makes Intervals under-count ride load (`underload.ftp_stale_load_undercounted`): do not read low bike load as underload while it is true. Above ~10% drift the interim eFTP update is proposed with `apply: true`; name it in the approval checkpoint. When eFTP sits more than ~5% *above* the set FTP, the targets are too easy: scale them up or propose the update.

## Missing is not fine

A blank check-in answer, stale HRV (`hrv_gate.status` `stale`) or a missing wellness signal is unknown, never "fine". Say what is missing and lower the confidence.

## Doctrine anchors

- `Jack Daniels` governs run-session taxonomy and quality distribution
- `Andrew Coggan` governs bike-session level labeling and stress framing
- `Joe Friel` governs periodization, race-phase timing, and 3:1 rhythm unless a justified override is needed
- `threshold_testing_doctrine.md` governs when FTP / threshold pace / LTHR are retested and how results update Intervals sport settings
- `training_intensity_doctrine.md` governs intensity distribution, quality-session count, HRV gating, underload, and progression ladders

## Traffic light implications

### Green

- progression is expected within the progression budget (rule 13): 2 quality sessions by default, 3 only when the HRV gate is open, underload is `detected`, and no niggle or return-from-break applies
- keep ambition specific, not reckless

### Yellow

- preserve key intent but reduce complexity or stress
- add recovery precision

### Red

- prioritize health, recovery, and damage control
- minimum effective structure only

## Conflict rules

- performance doctor wins on safety (illness, injury, red readiness); it does not win by default on mere caution
- performance doctor's `intensity_allowance` is the ceiling on quality sessions; the head coach picks the number within it
- macro coach wins on cycle rhythm, including which threshold test belongs in the week
- performance doctor can always veto a threshold test on readiness, illness, or niggles
- race director wins on specificity timing
- phase alignment auditor wins on build / taper / race-phase coherence
- planner wins on session structure
- validator improves clarity and risk control
- syntax guard wins on parseability after approval

## Write-back rule

No workout is pushed until the narrative plan is approved.
Threshold updates to Intervals sport settings follow the same approval gate.
