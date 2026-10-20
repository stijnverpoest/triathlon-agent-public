# Jack Daniels Run Doctrine

Use Daniels-style run logic when building triathlon weeks.

## Allowed run types

- `E`: easy / recovery / long aerobic
- `T`: threshold / tempo
- `I`: VO2max intervals
- `R`: repetition / economy / short speed

## Rules

- most weekly run volume should remain `E`, genuinely easy
- `T` is the default primary quality run for middle-distance triathlon and half-marathon development
- `I` is a real tool: run it in 3-4 week blocks from build onward, and whenever threshold pace stagnates (VO2max raises the ceiling T works under)
- `R` (strides, short reps) is a weekly economy touch, not a quality session
- quality volume caps per session: T <= 10%, I <= 8% (max 10 km), R <= 5% of weekly run volume
- progress quality runs along the ladders in `training_intensity_doctrine.md`; record the rung on the session
- yellow-readiness weeks usually contain one real quality run
- long runs are `E` at the core, with a steady or race-pace finish in build / race-specific phases for HM, marathon, and 70.3
- run-focused and ultra-trail blocks can carry 2-3 quality runs per green week (T, I or uphill threshold, race-pace long run)
