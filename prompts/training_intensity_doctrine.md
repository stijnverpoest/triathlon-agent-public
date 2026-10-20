# Training Intensity & Progression Doctrine

This doctrine sets how hard the athlete trains, how many quality sessions a week carries, and how each one progresses. It applies to every agent that shapes training load. Performance comes first: under-training is a performance risk just as real as over-training, and the agents must guard against both with equal firmness.

There is no session library. The head coach designs every session itself, using the principles below. The deterministic inputs live in `normalized_snapshot.json`:

- `training_load`:
  - `fitness`: CTL, ATL, TSB and the weekly CTL ramp
  - `intensity_distribution_28d`
  - `quality_frequency`
  - `volume`
  - `hrv_gate`
  - `eftp_trend`
  - `underload`
  - `ramp`: CTL ramp, ACWR (uncoupled, acute window ending yesterday) and week-on-week load change, with an overload `status` (`ok` / `elevated` / `high`); ramp limits scale with CTL
  - `run_load`: weekly run km, minutes, D+ and D- per calendar week, the longest non-race run of the last 28 / 56 days, run-specific `acwr` (km, minutes, descent), suggested `week_caps` for the planned week, and jump `flags`
  - `ride_load`: the longest ride of the last 28 / 56 days
  - `other_load`: tennis and other non-endurance sessions with an estimated load
  - `return_from_break`: a re-entry ramp after 7+ days off (illness, injury, holiday)
  - `daily_signals`: a 1–3 day early warning from resting HR and HRV
  - `overreaching`: functional vs non-functional overreaching markers
  - `efficiency_trend`: pace/power at HR, decoupling, RPE vs load and Feel over 4–6 weeks
- `holidays`: Intervals HOLIDAY events and their overlap with the planned week
- `progression`: the last completed dose per ladder family, plus the next rung if readiness is green
- `performance_doctor.intensity_allowance`: the readiness ceiling on quality sessions this week

## 1. Default distribution: pyramidal

Use the three-zone model:

- **low** = below LT1, Intervals HR Z1–Z2
- **moderate** = LT1–LT2: tempo, sweet spot, threshold, Intervals HR Z3–Z4
- **high** = above LT2: VO2max and anaerobic, Intervals HR Z5+

The weekly time-in-zone target is about **75–80% low, 12–20% moderate, 3–8% high**.

Why pyramidal and not strict polarized (80/0/20):

- For recreational and age-group athletes on limited hours, the evidence favours pyramidal (Rosenblat 2025 individual-participant network meta-analysis; Sellés-Pérez 2019, recreational half-Ironman triathletes; Festa 2020).
- Polarized only shows a small advantage in highly trained athletes (Oliveira 2024).
- Long-course racing happens mostly in the moderate zone (Muñoz 2014), so specificity demands real moderate-zone work.

What this means in practice:

- **Easy volume never replaces intensity.** Faster athletes add easy volume *on top of* a stable dose of quality (Muniz-Pumares 2024). Mostly-easy high volume gave the smallest gains (Stöggl & Sperlich 2014).
- **Deliberate moderate work is wanted.** Sweet spot, tempo, threshold and race pace are the backbone of a pyramidal week. The thing to avoid is *unplanned* drift: easy days ridden or run in Z3 because the athlete felt good. Keep easy days genuinely easy, so the hard days can be genuinely hard (Foster).
- **Phase order** (Filipas 2022; Casado 2022):
  - Base and early build: pyramidal, sub-threshold and threshold heavy.
  - Build: VO2max/I joins as a block of 3–4 weeks.
  - Final 4–6 weeks: more race-specific and slightly more polarized sharpening.
- **Use the 28-day distribution as feedback.** When `intensity_distribution_28d.model` is `almost_all_easy`, or moderate+high is under 12%, the next weeks need more quality. When it is `threshold_heavy`, add easy volume and move one session to the high zone.

## 2. How many quality sessions

A *quality session* is a session whose main purpose is moderate or high intensity: sweet spot, threshold, VO2max/I, race pace, or uphill threshold. A long ride or long run with a substantial race-intensity block counts as one. Threshold tests are not quality sessions.

The head coach chooses the number. `performance_doctor.intensity_allowance.max_quality_sessions` is the readiness ceiling:

| Situation | Quality sessions |
|---|---|
| Green readiness, load week | **2** default; **3** only when the HRV gate is open (`normal`/`above_normal`), underload is `detected`, and there is no niggle, return from break or overreaching marker |
| Yellow readiness | 1 (2 only with HRV clearly above normal and no niggle or illness) |
| Red readiness | 0 |
| Recovery / unload week | 0, plus a threshold test only when one is due |
| Return from break (`return_from_break.status` `returning`) | 0 in re-entry week 1, then 1, then normal |
| Base phase, or an ultra build where the long runs carry the load | 1–2; 1 is not under-dosing here |
| Taper | 1–2 short, race-specific sessions at full intensity, reduced volume |

Rules:

- **In a green build-phase load week, fewer than 2 quality sessions is under-dosing.** The validator flags it. In base, or in an ultra block where back-to-back long runs are the key work, 1 is enough.
- **Three quality sessions is the exception**, not the default, for an athlete on ~12 h with a 9-5 job and ~7 h of sleep. Spread them across the week: never on consecutive days, never the day before a long session that has its own race block.
- **Back-to-back long weekend:** at most one *running* quality session that week; a second quality session goes on the bike.
- **Triathlon:** with 2+ quality sessions, include at least one bike and one run session.
- **Run-only and ultra-trail blocks:** quality is mostly running (threshold, uphill threshold, I, race pace). Bike sessions are aerobic or sweet-spot support.

## 3. HRV gate (weekly level)

`training_load.hrv_gate` compares the 7-day HRV mean with a normal band: the 42-day baseline mean ± 0.5 SD of ln(rMSSD), the smallest worthwhile change (Plews 2012).

- **`above_normal`:** the athlete is coping well. Extra quality is supported.
- **`normal`:** planned quality is supported. Add a session when underload markers are present (Javaloyes 2019: HRV-guided training beat fixed plans).
- **`suppressed`:** hold intensity. Do not add quality, and do not progress the rung.
- **High day-to-day swing** (`acute_cv_pct` > 15) inside the band: keep the planned quality but do not add more.
- **`insufficient_data` or `stale`:** use readiness alone and lower the confidence; silence is not green.
- **Read HRV against load.** Above-normal HRV during a high ramp or deep negative TSB can be the parasympathetic overreaching pattern, not freshness: no extra quality then (`possible_overreaching_hrv_pattern`). Suppressed or swinging HRV while training load is low points to non-training stress (illness, work, sleep): cap quality at one session (`non_training_stress`).

## 4. Underload detection

`training_load.underload` combines these markers:

- `tsb_persistently_positive`: TSB above +5 on 10 or more of the last 14 days
- `ctl_flat_or_falling`: CTL ramp under +1/week over 3 weeks
- `low_quality_frequency`: fewer than 2 quality sessions per week
- `volume_below_profile`: under 70% of the athlete's own 12-week endurance average (the profile's typical hours only as a fallback)
- `quality_sessions_feel_easy`: average quality-session RPE ≤ 4
- `eftp_not_rising`: only counted when the priority race is a triathlon

Normal HRV is context, not a marker: it says the athlete copes, not that the dose is too small.

Status `detected` requires 3 or more markers, including at least one load marker. It turns into `explained` when there is a recent illness or injury note, a planned taper, the race week or taper window of the priority race, a post-race transition, a holiday, or a requested recovery week. It is capped at `watch` (`vetoes`) when CTL already rises 3+/week, the ramp is `elevated`/`high`, or run km is already climbing toward a run race.

It becomes `explained_physiological` when the body, not the plan, explains the low load: suppressed HRV, resting HR 5+ bpm above baseline, a `daily_signals` warning, or high fatigue / soreness. That is a hold-and-investigate week, not a push week. While `ftp_stale_load_undercounted` is true, Intervals under-counts ride load, so bike load cannot prove underload. A falling eFTP under a high ramp is an overreaching marker, not an underload marker.

When underload is `detected` and readiness is green:

- the load direction is **increase**;
- progress key sessions by up to two rungs, or add a quality session within the allowance;
- target a CTL ramp of about **+3 to +6 per week** (Friel's 5–8/week is sustainable for most athletes; brief 10+ weeks must be followed by recovery);
- say so explicitly in the plan.

Repeating last week's dose is not neutral: it is a decision to stop improving.

## 4b. Overload guardrails

Over-reaching without a plan is as much a coaching failure as under-dosing. Read `training_load.ramp` and `training_load.run_load` every week:

- **Ramp `elevated`** (CTL ramp above its CTL-scaled limit, ACWR > 1.3, or week load +30%): hold the load for a week. Progress quality one rung at most, do not add volume.
- **Ramp `high`** (CTL ramp well above the limit or ACWR > 1.5): reduce. No added quality; consider pulling the recovery week forward.
- **Return from break:** a jump after a light or empty week is never "a normal return to load". Follow `return_from_break` (about 50 → 60 → 75 → 90% of the pre-break week, run km and long run capped) until it clears.
- **Overreaching:** `overreaching.status` `non_functional_risk` (performance and HRV / resting HR off for more than ~2 weeks) means a recovery week now; `functional` is fine inside a load block if a recovery week follows.
- **Planned week vs recent weeks:** more than ~15–20% above the recent weekly average needs an explicit reason (return after a recovery or illness week, holiday catch-up).
- **Long run / long ride:** never more than max(15 min, 15%) above the longest session of the last 4 weeks.
- **Run km and descent (D-):** respect `run_load.week_caps` (run km and minutes about +10%, descent about +15%). A sharp jump is the classic ITB / tendon trigger. With an ITB history in the profile, descent progresses gradually and is paired with eccentric strength. Tag trail sessions with `elevation_gain_m`, `elevation_loss_m` and `terrain` so the audit can check it.
- **Strength and sequencing:** no heavy leg strength the day before a quality or long session; check across the week boundary too (last Sunday's long run against this Monday).
- **Intensity ceiling:** moderate + high above ~30% of the week's time is as much a warning as falling below the floor.
- **Tennis and other sports** carry real load (`other_load`): count them in the week and never put a key session on a tennis day.

`finalize-run` audits the final plan for these and prints warnings; resolve or explain every one.

## 5. Progression ladders

Every quality session belongs to a ladder family. In a load week it *may* progress one **rung** on the last *completed* dose of that family (`progression.last_dose_by_family`), within the weekly progression budget: at most two progressing elements per week (rungs, long-session duration, volume), at most one of them run-impact related. A rung adds about 10–25% time-in-zone, or lengthens the reps at the same total. Record the rung *index* so equal-total rungs (2×30 after 3×20) still advance.

| Family | Target | Rungs (reps × min) |
|---|---|---|
| `bike_sweet_spot` (70.3 race power) | 88–93% FTP | 3×10 → 3×12 → 2×20 → 3×15 → 2×25 → 3×20 → 2×30 → 2×40 |
| `bike_threshold` | 95–100% FTP | 3×8 → 4×8 → 3×12 → 4×10 → 2×20 → 3×15 → 3×20 |
| `bike_vo2` | 106–120% FTP | 5×3 → 6×3 → 5×4 → 6×4 → 5×5 (recovery = work time) |
| `run_threshold` (Daniels T) | 98–102% threshold pace | 4×5 → 5×5 → 3×10 → 3×12 → 4×10 → 3×15 → 2×25 |
| `run_interval` (Daniels I) | 106–110% threshold pace | 5×3 → 6×3 → 5×4 → 4×5 → 5×5 (jog recovery ≈ 80% of work) |
| `run_race_pace` (HM / 70.3 run / marathon) | 90–96% threshold pace | 2×10 → 3×10 → 2×15 → 3×12 → 2×20 → 3×15 → 2×25 |
| `run_uphill_threshold` (trail / ultra) | Z4 HR on a sustained climb | 4×6 → 5×6 → 4×8 → 3×12 → 5×8 → 4×12 |

Rules:

- **Completed last time** → one rung up. Two rungs when underload is `detected` and readiness is green.
- **Missed** (a prescription of similar size after the last completed one) → repeat the rung.
- **Partial** (cut short) → repeat the rung; the executed dose, not the planned one, is what was completed.
- **After a break** → step back proportionally: 14 days one rung, 28 days two, 42 days three, 56+ days restart at the phase entry rung.
- **Yellow week or suppressed HRV** → repeat the rung, never progress.
- **Recovery week** → no ladder work. Next load week resumes one rung above the last completed dose.
- **No history** → start at the phase entry rung (base 1st, build 3rd, race-specific 4th), plus block week − 1.
- **RPE feedback:**
  - RPE well below target (threshold ≤ 6, VO2 ≤ 7) or feel 1–2 → the next session may move up two rungs.
  - RPE 10 or a failed last rep → repeat the rung.
- **The top rung is not the end.** Progress density instead: shorter recoveries, a third session of the family, or the work placed later inside a long session.
- **Daniels volume caps still apply:**
  - T ≤ 10% of weekly run volume per session;
  - I ≤ 8% of weekly run volume, and at most 10 km at I pace;
  - R ≤ 5% of weekly run volume.

  For a low-mileage runner these caps, not the ladder, are the limit. Then grow run volume first.
- **Record the rung** on every quality session in the `progression` object (`family`, `rung`, `repeats`, `rep_min`, `recovery_min`, `time_in_zone_min`, `target`). Next week's progression reads it back.

## 6. Long sessions: durability is trained, not assumed

Durability means power and pace that hold late in a long effort (Maunder 2021; Spragg 2023/2024).

- **70.3, build and race-specific phases:**
  - The long ride carries a race-power block late in the ride: 20 → 60 min at 78–84% FTP (or race HR). It progresses by 10 min per load week.
  - Brick runs of 15–30 min at race effort follow it.
- **HM, marathon and 70.3 run:** the long run carries a steady or race-pace finish of 10 → 30 min in the build and race-specific phases.
- **Trail and ultra trail:**
  - long runs on trails with climbing; weekly D+ grows by at most ~15% a week from `run_load.avg_weekly_elevation_gain_m_4w`;
  - back-to-back long days on weekends in the build and race-specific phases (Saturday medium-long, Sunday long);
  - power-hiking the steep grades, practised at race grade;
  - progressive downhill running for eccentric strength, paired with eccentric strength work (step-downs, eccentric single-leg squats), never a sudden jump in descent;
  - technical terrain and night / headlamp sessions before races that need them;
  - fuelling rehearsal every long session (see the nutritionist carb ladder);
  - in flat terrain: treadmill at 8–12% incline, stairs or bridge repeats replace the sustained climb;
  - the bike is aerobic support, not a second long-ride priority.

  Long-run duration progresses from the longest run of the last 4 weeks (`run_load.longest_run_min_28d`), at most max(15 min, 15%) per load week, up to about 3–4 h for a 50 km race. It carries over between blocks; it does not reset to a template each block.
- **Long-session duration progresses through the block and carries over between blocks** (from the last completed dose, not the block-week number). Descent minutes, brick length, race blocks inside long rides and steady finishes progress the same way. Races do not count as the longest run. Without a recent long run, start from `run_load.week_caps.max_long_run_min`, never a template.
- The long session is trimmed last when the athlete's hours bind, never first.
- **Taper and race week:** taper length depends on the race distance (`race_selection.taper_days`: ~7 days for a sprint or 5k, ~10 for a 10k, half marathon or short trail, ~14 for a 70.3, ~18 for a marathon or ultra trail). Volume drops progressively (taper ~60–70% of build volume, race week ~40–50%), short intensity touches stay, and nothing hard or long lands on race day or the day before.
- **B-race mini-taper:** 3–4 lighter days, last quality at least 72 h before, 2–4 easy days after (scaled by race duration). **C-race:** 1–2 easy days before. A B/C trail race is the long run of its week.
- **Transition after an A-race** (a B-race gets 2–4 easy days, not a transition): easy, unstructured, optional sessions for `transition_days` (about 1 week after a half marathon, 2 weeks after an ultra or marathon). No quality, no tests.
- **Holidays** (e.g. a multi-week summer holiday) are maintenance, not recovery: 2–3 short easy sessions plus one optional long or hilly session a week, quality optional. A hilly destination is a trail-camp opportunity when the athlete wants it. The week after a long holiday follows `return_from_break`.

## 7. Volume is a coaching decision inside the athlete's real hours

- The athlete's `typical_training_hours_per_week` and `weekly_time_budget_policy` are **references**: plan above them when the week clearly needs it and explain why (more than ~15% above costs sleep for a 9-5 athlete).
- Hours the athlete gives for *this* week (`available_hours_override_min`, `available_hours_hard_cap_min`), explicit day caps (`time_caps_by_day`) and hard access constraints (pool hours, club sessions, days off) **bind**.
- When the ideal week does not fit: cut easy volume first, then the secondary quality session, and trim the long session last. Say what was cut.
- No phase multiplier trims volume automatically. The head coach sets the volume from:
  - what the key and long sessions need;
  - the CTL ramp target;
  - readiness.
- Report the decision in `weekly_time_budget`: `planned_min`, `reference_min`, `mode: "coach_decided"`, `rationale`.

## 8. Language

- Write intensity honestly. A threshold session should feel hard and is meant to.
- Avoid framing that tells the athlete to hold back on quality days ("not heroics", "never a grind"). Use precise targets and a clear success criterion instead.
- Reserve "stay controlled" for easy days and for the first half of progression runs.
