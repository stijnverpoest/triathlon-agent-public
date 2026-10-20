from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


REPLACE_ATTEMPTS = 20


def _replace_with_retry(source: Path, target: Path) -> None:
    """os.replace, retried briefly: on Windows a virus scanner or indexer can
    hold the just-written target open for a moment (PermissionError)."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def write_text(path: str | Path, content: str) -> None:
    """Write atomically: a crash mid-write never leaves a truncated file.

    The content goes to a temp file in the same directory, is flushed to disk
    and then swapped in with os.replace (atomic on POSIX and Windows).
    """
    target = Path(path)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def write_json(path: str | Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso_date(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def start_of_planned_week(planning_date: str) -> date:
    current = parse_date(planning_date)
    return current + timedelta(days=(7 - current.weekday()) % 7)


def data_as_of_date(planning_date: date, fetch_timestamp: str | None) -> date:
    """Newest day the fetched evidence can cover: the planning date, or the
    fetch day when the planning date lies ahead (planning next week mid-week)."""
    if not fetch_timestamp:
        return planning_date
    try:
        fetch_day = parse_date(str(fetch_timestamp)[:10])
    except ValueError:
        return planning_date
    return min(planning_date, fetch_day)


def daterange(start_day: date, count: int) -> list[date]:
    return [start_day + timedelta(days=index) for index in range(count)]


def slugify(value: str) -> str:
    clean = []
    for char in value.lower():
        if char.isalnum():
            clean.append(char)
        elif clean and clean[-1] != "-":
            clean.append("-")
    result = "".join(clean).strip("-")
    return result or "item"


def day_name(day_value: date) -> str:
    return day_value.strftime("%A")


def minutes_to_hhmm(minutes: int) -> str:
    hours, rem = divmod(int(minutes), 60)
    return f"{hours}h {rem}m" if hours else f"{rem}m"


def sum_minutes(items: list[dict[str, Any]], field: str = "moving_time") -> int:
    total = 0
    for item in items:
        value = item.get(field, 0)
        try:
            total += int(float(value or 0))
        except (TypeError, ValueError):
            continue
    return total


def week_monday(day_value: date) -> date:
    return day_value - timedelta(days=day_value.weekday())


def _planned_day_done(
    day_text: str,
    activities: list[dict[str, Any]],
    planned_sessions: list[dict[str, Any]],
) -> bool:
    """Every planned (non-recovery) session of `day_text` has a same-day activity."""
    planned = [
        session
        for session in planned_sessions
        if str(session.get("date") or "")[:10] == day_text
        and session.get("discipline") not in {"Recovery", None}
    ]
    if not planned:
        return False
    done_types: list[str] = [
        str(activity.get("type") or "")
        for activity in activities
        if str(activity.get("start_date_local") or "")[:10] == day_text
    ]
    for session in planned:
        discipline = str(session.get("discipline"))
        match = next(
            (kind for kind in done_types if discipline.lower() in kind.lower()),
            None,
        )
        if match is None:
            return False
        done_types.remove(match)
    return True


def is_partial_week(
    week_end: date,
    planning_date: date,
    activities: list[dict[str, Any]],
    *,
    planned_sessions: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> bool:
    """A Mon-Sun week is partial while it can still gain training.

    Weeks ending before the planning date are complete. A week ending on the
    planning date (planning on a Sunday) counts as complete only once that
    day is over (`today` is later than the planning date), or when every
    session planned for that day already has a matching activity. A single
    Sunday-morning swim no longer closes the week before the long run.
    """
    if week_end < planning_date:
        return False
    if week_end > planning_date:
        return True
    current_day = today or date.today()
    if current_day > planning_date:
        return False
    if planned_sessions and _planned_day_done(
        planning_date.isoformat(), activities, planned_sessions
    ):
        return False
    return True


def calendar_weeks(
    week_start: date,
    activities: list[dict[str, Any]],
    weeks: int = 8,
    *,
    planning_date: date | None = None,
    planned_sessions: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Mon-Sun windows ending the day before the planned week starts, oldest first.

    `planned_sessions` (the approved plan of the running week, optional) lets a
    Sunday check-in count the week as complete once Sunday's planned sessions
    are done; without it the week stays partial until the planning day is over.
    """
    windows: list[dict[str, Any]] = []
    first_monday = week_monday(week_start)
    for offset in range(weeks, 0, -1):
        start = first_monday - timedelta(days=7 * offset)
        end = start + timedelta(days=6)
        selected = []
        for activity in activities:
            raw = str(activity.get("start_date_local") or "")[:10]
            if not raw:
                continue
            if start <= parse_date(raw) <= end:
                selected.append(activity)
        windows.append(
            {
                "week_start": start.isoformat(),
                "week_end": end.isoformat(),
                "activities": selected,
                "partial": (
                    is_partial_week(
                        end,
                        planning_date,
                        activities,
                        planned_sessions=planned_sessions,
                        today=today,
                    )
                    if planning_date is not None
                    else False
                ),
            }
        )
    return windows


# Shared "no niggle" detection: the weekly check-in is free text, often
# "none", "geen", "ITB fine" or "no pain". Only genuine complaints count.
NO_NIGGLE_VALUES = {
    "",
    "none",
    "no",
    "nee",
    "geen",
    "niets",
    "nothing",
    "n/a",
    "na",
    "nvt",
    "-",
    "/",
    "ok",
    "okay",
    "fine",
    "all good",
    "alles goed",
    "nope",
    "none really",
    "nothing to report",
    "no niggles",
    "no pain",
    "geen pijn",
    "geen klachten",
}
_NO_NIGGLE_PREFIXES = (
    "no ",
    "geen ",
    "zero ",
    "without ",
    "nothing",
    "niets",
    "none",
    "all good",
    "alles goed",
    "not really",
    # Workflow placeholder before the athlete answered: unknown, not a niggle.
    "pending",
)
_CLEAR_SUFFIX_RE = re.compile(
    r"\b(fine|ok|okay|good|goed|great|better|resolved|gone|healed|weg|prima|"
    r"in orde|pain[- ]free|symptom[- ]free|no issues?|no pain)\s*$"
)
_NEGATION_RE = re.compile(r"\b(not|niet|isn't|isnt|never|nog niet)\b")
_CLAUSE_SPLIT_RE = re.compile(
    r"[,;.\n!?]|\bbut\b|\bmaar\b|\balthough\b|\bthough\b|\bexcept\b|\bhowever\b"
)
_BODY_PARTS: tuple[tuple[str, str], ...] = (
    (r"\bitb\b|\bit[- ]band\b|\biliotibial\b", "ITB"),
    (r"\bachilles\b", "Achilles"),
    (r"\bplantar\b", "plantar fascia"),
    (r"\bhamstrings?\b", "hamstring"),
    (r"\bhip flexors?\b", "hip flexor"),
    (r"\bcalf\b|\bcalves\b|\bkuit\b", "calf"),
    (r"\bknees?\b|\bknie\b", "knee"),
    (r"\bshins?\b|\bscheen\b", "shin"),
    (r"\bankles?\b|\benkel\b", "ankle"),
    (r"\bfoot\b|\bfeet\b|\bvoet\b", "foot"),
    (r"\bhips?\b|\bheup\b", "hip"),
    (r"\bglutes?\b", "glute"),
    (r"\bquads?\b|\bquadriceps\b", "quad"),
    (r"\bgroin\b|\badductors?\b|\blies\b", "groin"),
    (r"\blower back\b|\bonderrug\b", "lower back"),
    (r"\bback\b|\brug\b", "back"),
    (r"\bshoulders?\b|\bschouder\b", "shoulder"),
    (r"\bneck\b|\bnek\b", "neck"),
)


# Medical red flags (EN + NL). Any of these means: stop training and see a
# doctor before resuming. They are never scored as a musculoskeletal niggle.
RED_FLAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "chest_pain",
        re.compile(
            r"\bchest (pain|tightness|tight|pressure|discomfort)\b|\btight(ness)? (in|of) (the |my )?chest\b|"
            r"\bpain in (the |my )?chest\b|\bborstpijn\b|\bpijn (op|in) de borst\b|"
            r"\bdruk op de borst\b|\bbeklemd\w*\b|\bbeklemming\b"
        ),
    ),
    (
        "palpitations",
        re.compile(
            r"\bpalpitation\w*\b|\bheart (racing|fluttering|flutter|skipping|skips)\b|"
            r"\birregular (heart ?beat|heart ?rate|pulse)\b|\bhartklopping\w*\b|"
            r"\bhartritmestoornis\w*\b|\bonregelmatige (hartslag|pols)\b"
        ),
    ),
    (
        "disproportionate_breathlessness",
        re.compile(
            r"\bshort(ness)? of breath\b|\bbreathless\w* (at rest|on easy|walking|on stairs|for no reason)\b|"
            r"\bunusually breathless\b|\bcan'?t catch (my )?breath\b|\bkortademig\w*\b|\bademnood\b|"
            r"\bbenauwd\b(?! weer)"
        ),
    ),
    (
        "dizziness_or_fainting",
        re.compile(
            r"\bdizz(y|iness)\b|\bfainted\b|\bfainting\b|\b(felt|feel|feeling|nearly|almost) faint\b|\blight[- ]?headed\w*\b|\bpassed out\b|"
            r"\bblack(ed)? out\b|\bsyncope\b|\bduizel\w*\b|\bflauw\w*\b|\bbewusteloos\b|"
            r"\blicht in (mijn|het) hoofd\b"
        ),
    ),
    (
        "fever",
        re.compile(
            r"\bfever\w*\b|\bkoorts\w*\b|\bchills\b|\b(koude )?rillingen\b|"
            r"\btemperature (of )?3[89]\b|\b3[89][.,]\d ?(c|Â°c|graden)\b"
        ),
    ),
    (
        "below_neck_illness",
        re.compile(
            r"\bchest (cold|congestion|infection)\b|\bdeep cough\w*\b|\bcoughing up\b|\bbronchitis\b|"
            r"\bbody ?aches?\b|\bmuscle aches all over\b|\baching all over\b|\bvomit\w*\b|"
            r"\bdiarrh\w*\b|\bspierpijn overal\b|\bspierpijn over (heel )?mijn lichaam\b|\bovergeven\b|"
            r"\bbraken\b|\bdiarree\b|\bbuikgriep\b|\bgastro\w*\b"
        ),
    ),
)
RED_FLAG_LABELS = {
    "chest_pain": "chest pain or tightness",
    "palpitations": "palpitations / irregular heartbeat",
    "disproportionate_breathlessness": "breathlessness out of proportion to the effort",
    "dizziness_or_fainting": "dizziness or fainting",
    "fever": "fever",
    "below_neck_illness": "below-the-neck illness symptoms",
}
# Above-the-neck symptoms: easy aerobic work only, no intensity.
ABOVE_NECK_RE = re.compile(
    r"\brunny nose\b|\bsnot\w*\b|\bsneez\w*\b|\bstuffy nose\b|\bblocked nose\b|\bnasal\b|"
    r"\bsore throat\b|\bkeelpijn\b|\bcold\b|\bverkoud\w*\b|\bcongest\w*\b|\bsinus\w*\b|"
    r"\bneusverkoudheid\b|\bloopneus\b|\bhead cold\b|\bmild cough\b|\bkuch\w*\b"
)
_RED_FLAG_NEGATION_RE = re.compile(
    r"\b(no|not|geen|niet|without|zonder|never|nooit)\b[^,;.]{0,25}$"
)


def detect_red_flags(text: Any) -> list[str]:
    """Red-flag categories mentioned (and not negated) in free text."""
    found: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(str(text or "").lower()):
        for category, pattern in RED_FLAG_PATTERNS:
            match = pattern.search(clause)
            if not match:
                continue
            if _RED_FLAG_NEGATION_RE.search(clause[: match.start()]):
                continue
            if category not in found:
                found.append(category)
    return found


def mentions_above_neck_symptoms(text: Any) -> bool:
    lowered = str(text or "").lower()
    for clause in _CLAUSE_SPLIT_RE.split(lowered):
        match = ABOVE_NECK_RE.search(clause)
        if match and not _RED_FLAG_NEGATION_RE.search(clause[: match.start()]):
            return True
    return False


def _clause_is_clear(clause: str) -> bool:
    text = clause.strip().lower().strip("!?.:- ")
    if text in NO_NIGGLE_VALUES:
        return True
    negated = bool(_NEGATION_RE.search(text))
    if text.startswith(_NO_NIGGLE_PREFIXES) and not negated:
        return True
    if _CLEAR_SUFFIX_RE.search(text) and not negated:
        return True
    return False


def _complaint_clauses(text: Any) -> list[str]:
    # Red-flag clauses (chest pain, fever, ...) are medical, not niggles: they
    # are handled by detect_red_flags and never scored as a generic niggle.
    return [
        clause
        for clause in _CLAUSE_SPLIT_RE.split(str(text or "").lower())
        if clause.strip(" -")
        and not _clause_is_clear(clause)
        and not detect_red_flags(clause)
    ]


def has_niggle(text: Any) -> bool:
    """True when the free-text niggle field reports an actual complaint."""
    return bool(_complaint_clauses(text))


def niggle_body_parts(text: Any) -> list[str]:
    """Body parts named in the complaint clauses of a niggle/injury text.

    "ITB fine, calf tight" -> ["calf"]: areas reported as fine are skipped.
    """
    lowered = " ; ".join(_complaint_clauses(text))
    parts: list[str] = []
    for pattern, label in _BODY_PARTS:
        if re.search(pattern, lowered) and label not in parts:
            parts.append(label)
    if "lower back" in parts and "back" in parts:
        parts.remove("back")
    return parts


def chunk_weekly(
    start_day: date, activities: list[dict[str, Any]], weeks: int = 4
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for offset in range(weeks):
        week_end = start_day - timedelta(days=7 * offset)
        week_start = week_end - timedelta(days=6)
        selected = []
        for activity in activities:
            activity_day = parse_date(activity["start_date_local"])
            if week_start <= activity_day <= week_end:
                selected.append(activity)
        windows.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "activities": selected,
            }
        )
    windows.reverse()
    return windows
