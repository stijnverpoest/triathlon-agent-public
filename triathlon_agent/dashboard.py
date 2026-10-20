from __future__ import annotations

import html
from typing import Any

from .utils import minutes_to_hhmm


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _metric_delta(metric: dict[str, Any], positive_good: bool) -> tuple[str, str]:
    delta = metric.get("delta")
    if delta in (None, ""):
        return ("No baseline", "neutral")
    label = f"{delta:+}"
    if isinstance(delta, float):
        label = f"{delta:+.1f}"
    status = "good" if (delta >= 0) == positive_good else "warning"
    return (label, status)


def _load_bars(windows: list[dict[str, Any]]) -> str:
    visible = windows[-6:]
    peak = max((float(item.get("load", 0) or 0) for item in visible), default=1.0)
    if peak <= 0:
        peak = 1.0
    bars = []
    for item in visible:
        height = max(18, int((float(item.get("load", 0) or 0) / peak) * 120))
        bars.append(
            "".join(
                [
                    '<div class="load-bar-wrap">',
                    f'<div class="load-bar" style="height:{height}px"></div>',
                    f'<div class="load-label">{_esc(item.get("week_start"))}</div>',
                    f'<div class="load-value">{_esc(item.get("load"))}</div>',
                    "</div>",
                ]
            )
        )
    return "".join(bars)


def _session_cards(sessions: list[dict[str, Any]]) -> str:
    cards = []
    for session in sessions:
        badges = [session.get("discipline", "")]
        if session.get("priority") == "key":
            badges.append("Key")
        if session.get("variant_id"):
            badges.append(session["variant_id"])
        cards.append(
            "".join(
                [
                    '<article class="session-card">',
                    f'<div class="session-day">{_esc(session.get("day"))}</div>',
                    f"<h3>{_esc(session.get('session_name'))}</h3>",
                    f'<div class="badge-row">{"".join(f'<span class="badge">{_esc(badge)}</span>' for badge in badges)}</div>',
                    f'<p class="session-meta">{_esc(minutes_to_hhmm(int(session.get("duration_min", 0) or 0)))} | {_esc(session.get("intensity_guidance"))}</p>',
                    f"<p>{_esc(session.get('objective'))}</p>",
                    f'<p class="small"><strong>Success:</strong> {_esc(session.get("success_criteria"))}</p>',
                    "</article>",
                ]
            )
        )
    return "".join(cards)


def _review_cards(reviews: list[dict[str, Any]]) -> str:
    if not reviews:
        return '<p class="empty">No prior-week session reviews were available.</p>'
    cards = []
    for review in reviews:
        status = str(review.get("status", "neutral"))
        tone = (
            "good"
            if "completed" in status
            else ("warning" if status in {"shortened", "missed"} else "neutral")
        )
        gain = review.get("elevation_gain_m")
        terrain_bit = ""
        if gain not in (None, "") and float(gain) > 0:
            density = review.get("elevation_per_km")
            density_text = f" (~{density:g} m/km)" if density not in (None, "") else ""
            terrain_bit = f' | D+ {int(round(float(gain)))} m{density_text}'
        cards.append(
            "".join(
                [
                    f'<article class="review-card {tone}">',
                    f'<div class="review-top"><strong>{_esc(review.get("session_name"))}</strong><span>{_esc(status.replace("_", " "))}</span></div>',
                    f"<p>{_esc(review.get('interpretation'))}</p>",
                    f'<p class="small">Planned {int(review.get("planned_minutes", 0) or 0)} min | Actual {_esc(review.get("actual_minutes"))} min | Pragmatic ratio {_esc(review.get("pragmatic_ratio"))}{_esc(terrain_bit)}</p>',
                    "</article>",
                ]
            )
        )
    return "".join(cards)


def _rung_label(session: dict[str, Any]) -> str:
    progression = session.get("progression") or {}
    if not progression:
        return "-"
    return (
        f"{progression.get('family')} rung {int(progression.get('rung', 0)) + 1}: "
        f"{progression.get('time_in_zone_min')}' @ {progression.get('target')}"
    )


def _engine_rows(sessions: list[dict[str, Any]]) -> str:
    rows = []
    for session in sessions:
        if session.get("discipline") == "Recovery":
            continue
        rows.append(
            "".join(
                [
                    "<tr>",
                    f"<td>{_esc(session.get('session_name'))}</td>",
                    f"<td>{_esc(session.get('training_method', 'General'))}</td>",
                    f"<td>{_esc(_rung_label(session))}</td>",
                    f"<td>{_esc(session.get('primary_metric'))}</td>",
                    "</tr>",
                ]
            )
        )
    return "".join(rows)


def render_dashboard(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
    final_plan: dict[str, Any],
) -> str:
    budget = final_plan.get("weekly_time_budget", {})
    adherence = snapshot.get("prior_plan_adherence", {})
    derived = snapshot.get("derived_baselines", {})
    hrv_label, hrv_tone = _metric_delta(derived.get("hrv", {}), positive_good=True)
    rhr_label, rhr_tone = _metric_delta(
        derived.get("resting_hr", {}), positive_good=False
    )
    sleep_label, sleep_tone = _metric_delta(
        derived.get("sleep_hours", {}), positive_good=True
    )
    weekly_focus = final_plan.get("weekly_focus_notes", [])
    week_note = next(
        (note for note in weekly_focus if note.get("day") == "Monday"),
        weekly_focus[0] if weekly_focus else {},
    )
    note_label = (
        f"{week_note['day']} focus note" if week_note.get("day") else "Week focus note"
    )
    red_flags = performance.get("red_flags") or []
    alert_html = ""
    if red_flags or performance.get("training_clearance") == "no_training_see_doctor":
        labels = ", ".join(
            dict.fromkeys(str(flag.get("label") or flag.get("category")) for flag in red_flags)
        )
        alert_html = (
            '<section class="panel alert"><h2>Stop training and see a doctor</h2>'
            f"<p>Medical red flag reported ({_esc(labels)}). Rest or easy walking only until a doctor has cleared you.</p></section>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Weekly Training Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #08111f;
      --panel: rgba(13, 24, 40, 0.92);
      --line: rgba(148, 163, 184, 0.22);
      --text: #eef2ff;
      --muted: #94a3b8;
      --accent: #67e8f9;
      --accent-2: #c084fc;
      --good: #34d399;
      --warning: #f59e0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, system-ui, sans-serif; background: radial-gradient(circle at top, #12253f, var(--bg) 55%); color: var(--text); }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 28px 18px 56px; }}
    .hero, .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 24px; padding: 22px; backdrop-filter: blur(14px); box-shadow: 0 18px 50px rgba(0,0,0,0.22); }}
    .hero {{ display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; margin-bottom: 18px; }}
    .hero h1 {{ margin: 0 0 10px; font-size: clamp(2rem, 5vw, 3.4rem); line-height: 0.96; }}
    .hero p {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 18px; }}
    .span-12 {{ grid-column: span 12; }}
    .span-8 {{ grid-column: span 8; }}
    .span-6 {{ grid-column: span 6; }}
    .span-4 {{ grid-column: span 4; }}
    .metric-strip {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
    .metric {{ padding: 16px; border-radius: 18px; background: rgba(255,255,255,0.03); border: 1px solid var(--line); }}
    .metric .value {{ font-size: 1.7rem; font-weight: 700; margin-top: 8px; }}
    .badge-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 12px; }}
    .badge {{ display: inline-flex; padding: 4px 10px; border-radius: 999px; font-size: 0.8rem; background: rgba(103,232,249,0.12); color: var(--accent); border: 1px solid rgba(103,232,249,0.22); }}
    .small {{ color: var(--muted); font-size: 0.92rem; }}
    .session-grid, .review-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
    .session-card, .review-card {{ padding: 16px; border-radius: 18px; border: 1px solid var(--line); background: rgba(255,255,255,0.03); }}
    .session-day {{ color: var(--accent-2); font-size: 0.84rem; text-transform: uppercase; letter-spacing: 0.08em; }}
    .session-card h3 {{ margin: 8px 0; font-size: 1.05rem; }}
    .session-meta {{ color: var(--muted); }}
    .review-card.good {{ border-color: rgba(52,211,153,0.4); }}
    .review-card.warning {{ border-color: rgba(245,158,11,0.45); }}
    .alert {{ border-color: rgba(248,113,113,0.7); margin-bottom: 18px; }}
    .review-top {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; text-transform: capitalize; }}
    .health-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
    .tone-good {{ color: var(--good); }}
    .tone-warning {{ color: var(--warning); }}
    .tone-neutral {{ color: var(--muted); }}
    .load-chart {{ display: flex; align-items: end; gap: 12px; min-height: 160px; padding-top: 12px; }}
    .load-bar-wrap {{ flex: 1; text-align: center; }}
    .load-bar {{ background: linear-gradient(180deg, var(--accent), #2563eb); border-radius: 14px 14px 6px 6px; }}
    .load-label, .load-value {{ color: var(--muted); font-size: 0.78rem; margin-top: 6px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; }}
    @media (max-width: 900px) {{
      .hero, .metric-strip, .health-grid {{ grid-template-columns: 1fr; }}
      .span-8, .span-6, .span-4 {{ grid-column: span 12; }}
    }}
  </style>
</head>
<body>
  <main>
    {alert_html}
    <section class="hero">
      <div>
        <div class="badge-row">
          <span class="badge">{_esc(macro.get("macro_phase", "build"))}</span>
          <span class="badge">{_esc(macro.get("week_classification", "build"))}</span>
          <span class="badge">{_esc(race.get("priority_race_lens", "general build"))}</span>
        </div>
        <h1>{_esc(final_plan.get("athlete_brief", {}).get("week_theme", "Weekly plan"))}</h1>
        <p>{_esc(final_plan.get("what_last_week_tells_us"))}</p>
        <p><strong>Planned volume this week:</strong> {_esc(minutes_to_hhmm(int(budget.get("planned_min", final_plan.get("planned_total_minutes", 0)) or 0)))}. Typical week (reference, not a cap): {_esc(minutes_to_hhmm(int(budget.get("reference_min", 0) or 0)))}.</p>
        <p class="small">{_esc(budget.get("rationale"))}</p>
      </div>
      <div class="panel">
        <p class="small">{_esc(note_label)}</p>
        <h2>{_esc(week_note.get("title", "Weekly Process Cue"))}</h2>
        <p>{_esc(week_note.get("body", "Start calm, protect the important work, and let consistency carry the week."))}</p>
      </div>
    </section>

    <section class="grid">
      <div class="panel span-12">
        <div class="metric-strip">
          <div class="metric"><div class="small">Planned time</div><div class="value">{_esc(minutes_to_hhmm(int(final_plan.get("planned_total_minutes", 0) or 0)))}</div></div>
          <div class="metric"><div class="small">Estimated load</div><div class="value">{_esc(final_plan.get("planned_total_estimated_load"))}</div></div>
          <div class="metric"><div class="small">Readiness</div><div class="value">{_esc(performance.get("readiness_status"))} {_esc(performance.get("readiness_score"))}/100</div></div>
        </div>
      </div>

      <div class="panel span-8">
        <h2>Week Schedule</h2>
        <div class="session-grid">{_session_cards(final_plan.get("sessions", []))}</div>
      </div>

      <div class="panel span-4">
        <h2>Protection Order</h2>
        <p>{_esc(final_plan.get("athlete_brief", {}).get("fallback_plan"))}</p>
        <div class="badge-row">{"".join(f'<span class="badge">{_esc(item)}</span>' for item in final_plan.get("athlete_brief", {}).get("protect_if_busy", []))}</div>
        <p class="small">{_esc(final_plan.get("coach_note"))}</p>
      </div>

      <div class="panel span-6">
        <h2>Last Week Interpretation</h2>
        <p class="small">{_esc(adherence.get("completion_policy_note"))}</p>
        <div class="review-grid">{_review_cards(adherence.get("session_reviews", []))}</div>
      </div>

      <div class="panel span-6">
        <h2>Health Signals</h2>
        <div class="health-grid">
          <div class="metric"><div class="small">HRV vs baseline</div><div class="value tone-{_esc(hrv_tone)}">{_esc(hrv_label)}</div><div class="small">Acute {_esc(derived.get("hrv", {}).get("acute"))} | Baseline {_esc(derived.get("hrv", {}).get("baseline"))}</div></div>
          <div class="metric"><div class="small">Resting HR vs baseline</div><div class="value tone-{_esc(rhr_tone)}">{_esc(rhr_label)}</div><div class="small">Acute {_esc(derived.get("resting_hr", {}).get("acute"))} | Baseline {_esc(derived.get("resting_hr", {}).get("baseline"))}</div></div>
          <div class="metric"><div class="small">Sleep vs baseline</div><div class="value tone-{_esc(sleep_tone)}">{_esc(sleep_label)}</div><div class="small">Acute {_esc(derived.get("sleep_hours", {}).get("acute"))}h | Baseline {_esc(derived.get("sleep_hours", {}).get("baseline"))}h</div></div>
        </div>
        <p class="small">Main risk: {_esc(performance.get("main_risk"))}</p>
      </div>

      <div class="panel span-6">
        <h2>Load Trend</h2>
        <div class="load-chart">{_load_bars(snapshot.get("weekly_windows", []))}</div>
      </div>

      <div class="panel span-6">
        <h2>Workout Engine</h2>
        <p>The head coach designs each session itself; every quality session sits on a progression ladder and moves one rung up from the last completed dose.</p>
        <table>
          <thead><tr><th>Session</th><th>Method</th><th>Progression</th><th>Metric</th></tr></thead>
          <tbody>{_engine_rows(final_plan.get("sessions", []))}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>
"""
