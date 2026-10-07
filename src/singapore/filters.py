"""Hard knockouts for the public Singapore monitor.

Deterministic and free: no network, no model calls. Surviving jobs may be
scored later. Rejection reasons are stable short slugs; soft findings go on
`job.flags` instead of dropping the posting.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..models import Job, ensure_utc, normalize_text, utcnow

# --------------------------------------------------------------------------
# public result
# --------------------------------------------------------------------------


@dataclass
class FilterResult:
    """Outcome of one Singapore filtering pass."""

    kept: list[Job] = field(default_factory=list)
    rejected: list[tuple[Job, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.kept)

    @property
    def total(self) -> int:
        return len(self.kept) + len(self.rejected)

    def summary(self) -> str:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"{len(self.kept)} kept, {len(self.rejected)} dropped" + (
            f" ({detail})" if detail else ""
        )


# --------------------------------------------------------------------------
# title catalogues
# --------------------------------------------------------------------------

_DEFAULT_SEARCH_TERMS: tuple[str, ...] = (
    "Data Scientist",
    "Junior Data Scientist",
    "Associate Data Scientist",
    "Data Scientist I",
    "Data Scientist II",
    "Applied Data Scientist",
    "Decision Data Scientist",
    "Decision Scientist",
    "Product Data Scientist",
    "Pricing Data Scientist",
    "Marketing Data Scientist",
    "Risk Data Scientist",
    "Machine Learning Scientist",
    "ML Scientist",
    "Machine Learning Engineer",
    "ML Engineer",
    "AI Engineer",
)

_DEFAULT_SECONDARY: tuple[str, ...] = (
    "Machine Learning Engineer",
    "ML Engineer",
    "AI Engineer",
)

# Seniority / leadership knockouts — whole-word, case-insensitive.
_SENIORITY_RE = re.compile(
    r"(?i)\b(?:"
    r"senior|sr|staff|principal|principle|lead|manager|head|director|"
    r"avp|vp|vice[\s-]?president|associate[\s-]?vice[\s-]?president"
    r")\b"
)

_INTERNSHIP_RE = re.compile(
    r"(?i)\b(?:"
    r"intern|internship|internships|working[\s-]?student"
    r")\b"
)

# Experience: mandatory floor of 3+ years (and higher).
_YEARS_NUM = r"(?P<n>\d{1,2})"
_YEARS_WORD = (
    r"(?P<w>three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen)"
)
_WORD_TO_INT = {
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15,
}

_MANDATORY_YEARS_RES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?i)\b(?:{_YEARS_NUM}|{_YEARS_WORD})\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|professional\s+|work\s+)?experience\b"),
    re.compile(
        rf"(?i)\b(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+"
        rf"(?:{_YEARS_NUM}|{_YEARS_WORD})\s*\+?\s*years?\b"
    ),
    re.compile(rf"(?i)\b{_YEARS_NUM}\s*\+\s*years?\b"),
    re.compile(
        rf"(?i)\b(?:{_YEARS_WORD}|{_YEARS_NUM})\s+or\s+more\s+years?\b"
    ),
    re.compile(
        rf"(?i)\b(?:{_YEARS_NUM}|{_YEARS_WORD})\s*[-–—to]+\s*"
        rf"(?:\d{{1,2}}|more)\s*years?\b"
    ),
    re.compile(
        rf"(?i)\b(?:requires?|must\s+have|seeking)\s+"
        rf"(?:{_YEARS_NUM}|{_YEARS_WORD})\s*\+?\s*years?\b"
    ),
)

# Soft band that passes with a review flag (en-dash in the flag text).
_TWO_TO_THREE_RE = re.compile(
    r"(?i)\b(?:2|two)\s*[-–—to]+\s*(?:3|three)\s*years?\b"
)

# Local experience/achievement equivalence — degree equivalence does NOT count.
_LOCAL_EQUIV_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:local\s+)?equivalent\s+(?:experience|achievement)s?"
    r"|equivalent\s+(?:local\s+)?(?:experience|achievement)s?"
    r"|or\s+equivalent\s+(?:experience|achievement)s?"
    r")\b"
)
_DEGREE_EQUIV_ONLY_RE = re.compile(
    r"(?i)\b(?:or\s+)?(?:equivalent\s+)?(?:degree|qualification|education)"
    r"(?:\s+equivalent)?\b"
)

# Masters / PhD mandatory vs preferred.
_DEGREE_NAME_RE = (
    r"(?:master'?s?(?:\s+degree)?|masters|msc|m\.sc|ms\b|mba|"
    r"ph\.?\s*d\.?|doctorate|doctoral)"
)
_DEGREE_REQUIRED_RE = re.compile(
    rf"(?i)\b(?:{_DEGREE_NAME_RE})\s+"
    rf"(?:is\s+)?(?:required|mandatory|must|essential)\b"
    rf"|\b(?:required|mandatory|must\s+have|essential)\b"
    rf"[^.]{{0,40}}\b(?:{_DEGREE_NAME_RE})\b"
    rf"|\b(?:{_DEGREE_NAME_RE})\s*(?:or\s+higher)?\s*"
    rf"(?:required|mandatory|essential)\b"
)
_DEGREE_PREFERRED_RE = re.compile(
    rf"(?i)\b(?:{_DEGREE_NAME_RE})\s+"
    rf"(?:is\s+)?(?:preferred|desired|nice[\s-]?to[\s-]?have|a\s+plus)\b"
    rf"|\b(?:preferred|desired|nice[\s-]?to[\s-]?have|advantageous)\b"
    rf"[^.]{{0,40}}\b(?:{_DEGREE_NAME_RE})\b"
)

# Citizens / PR / clearance — with common negations.
_CITIZEN_PR_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:singapore(?:an)?\s+)?(?:citizens?|nationals?)\s+only"
    r"|only\s+(?:for\s+)?(?:singapore(?:an)?\s+)?(?:citizens?|nationals?|prs?|permanent\s+residents?)"
    r"|(?:open\s+to|restricted\s+to|limited\s+to)\s+"
    r"(?:singapore(?:an)?\s+)?(?:citizens?|prs?|permanent\s+residents?)"
    r"|must\s+be\s+(?:a\s+)?(?:singapore(?:an)?\s+)?"
    r"(?:citizen|pr|permanent\s+resident)"
    r"|singapore(?:an)?\s+(?:citizens?|prs?|permanent\s+residents?)\s+only"
    r")\b"
)
_CLEARANCE_RE = re.compile(
    r"(?i)\b(?:"
    r"security\s+clearance\s+(?:is\s+)?(?:required|mandatory|needed|essential)"
    r"|(?:requires?|must\s+have|need(?:s)?)\s+(?:a\s+)?security\s+clearance"
    r"|holding\s+(?:a\s+)?(?:valid\s+)?security\s+clearance"
    r")\b"
)
_NEGATION_WINDOW = 40
_NEGATION_RE = re.compile(
    r"(?i)\b(?:"
    r"no|not|without|does\s+not|do\s+not|don't|doesn't|"
    r"need\s+not|no\s+need(?:\s+for)?|not\s+required|not\s+necessary|"
    r"unrestricted|not\s+limited\s+to|not\s+restricted\s+to"
    r")\b"
)

# Foundation-model / new-architecture research focus (vs ordinary GenAI use).
_FOUNDATION_FOCUS_RE = re.compile(
    r"(?i)\b(?:"
    r"train(?:ing|s)?\s+(?:large\s+)?(?:language\s+)?models?"
    r"|pre-?train(?:ing|s)?\s+(?:large\s+)?(?:language\s+)?models?"
    r"|foundation\s+models?"
    r"|large\s+language\s+models?"
    r"|\bllms?\b"
    r"|new\s+(?:deep\s+learning|dl|neural)\s+architectures?"
    r"|novel\s+(?:deep\s+learning|dl|neural|model)\s+architectures?"
    r"|research(?:ing)?\s+(?:new\s+)?(?:model\s+)?architectures?"
    r"|develop(?:ing|s)?\s+new\s+(?:dl|deep\s+learning)\s+architectures?"
    r")\b"
)
# Soft GenAI product work that should NOT be knocked out alone.
_NORMAL_GENAI_RE = re.compile(
    r"(?i)\b(?:"
    r"gen(?:erative)?\s*ai|llm\s+application|prompt\s+engineering|"
    r"rag\b|retrieval[\s-]augmented|copilot|chatbot"
    r")\b"
)

# Singapore location — strict.
_SG_POSITIVE_RE = re.compile(
    r"(?i)\b(?:"
    r"singapore|singaporean|\bsg\b|\bsgp\b|"
    r"marina\s+bay|one-?north|jurong|tampines|changi|"
    r"raffles\s+place"
    r")\b"
)
_SG_NEGATIVE_RE = re.compile(
    r"(?i)\b(?:"
    r"remote\s+(?:worldwide|global|anywhere)|anywhere\s+in\s+the\s+world|"
    r"united\s+states|\busa\b|\bus\s+only\b|london|berlin|amsterdam|"
    r"hong\s+kong|tokyo|sydney|melbourne|kuala\s+lumpur|\bkl\b|"
    r"indonesia|jakarta|philippines|manila|vietnam|bangkok|thailand|"
    r"india\b|bangalore|bengaluru|hyderabad|mumbai|delhi"
    r")\b"
)

# Salary parsing (SGD fixed monthly / annual / simple ranges).
_MONEY_RE = re.compile(
    r"(?i)(?:(?P<cur>s\$|sgd|singapore\s+dollars?)\s*)?"
    r"(?P<a>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(?:\s*[-–—to]+\s*"
    r"(?P<b>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?))?"
    r"(?:\s*(?P<cur2>s\$|sgd))?"
    r"(?:\s*(?P<period>(?:/\s*|per\s+)?(?:monthly|months?|mo|mth|years?|yr|annual(?:ly)?|annum|p\.?a\.?)))?"
)
_UP_TO_RE = re.compile(r"(?i)\bup\s+to\b")
_TOTAL_COMP_RE = re.compile(r"(?i)\b(?:total\s+comp(?:ensation)?|ote|tc)\b")

FLAG_YEARS = "2–3 years"
FLAG_SALARY = "salary below EP"
FLAG_SECONDARY = "secondary"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _settings_map(settings: Any) -> Mapping[str, Any]:
    if settings is None:
        return {}
    if isinstance(settings, Mapping):
        return settings
    data = getattr(settings, "data", None)
    if isinstance(data, Mapping):
        return data
    get = getattr(settings, "get", None)
    if callable(get):
        # Config-like: expose a shallow view via known keys.
        out: dict[str, Any] = {}
        for key in (
            "search_terms", "secondary_search_terms", "ep_thresholds",
            "salary_rules", "applicant_age", "exclusions",
        ):
            value = get(key)
            if value is not None:
                out[key] = value
        return out
    return {}


def _blob(job: Job) -> str:
    text = f"{job.title}\n{job.location}\n{job.description or ''}".replace("’", "'")
    text = re.sub(r"(?i)\bph\s*\.\s*d\.?", "PhD", text)
    return re.sub(r"(?i)\byrs?\b", "years", text)


def _clause(text: str, start: int, end: int) -> str:
    left = max([text.rfind(separator, 0, start) for separator in '.!?;\n'] + [-1]) + 1
    right = min([position for separator in '.!?;\n'
                 if (position := text.find(separator, end)) >= 0] + [len(text)])
    return text[left:right]


def _negated_near(text: str, match: re.Match[str]) -> bool:
    start = max(0, match.start() - _NEGATION_WINDOW)
    window = text[start:match.start()]
    return bool(_NEGATION_RE.search(window))


def _parse_year_value(match: re.Match[str]) -> int | None:
    if match.groupdict().get("n"):
        try:
            return int(match.group("n"))
        except (TypeError, ValueError):
            return None
    word = match.groupdict().get("w")
    if word:
        return _WORD_TO_INT.get(word.lower())
    return None


def _has_local_experience_equivalence(text: str) -> bool:
    if not _LOCAL_EQUIV_RE.search(text):
        return False
    # Degree-only equivalence nearby does not cancel a years floor.
    return True


def _title_haystack(title: str) -> str:
    return normalize_text(title)


def _term_patterns(terms: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for term in terms:
        norm = normalize_text(term)
        if not norm:
            continue
        parts = norm.split()
        body = r"\s+".join(re.escape(p) for p in parts)
        patterns.append((term, re.compile(rf"(?<!\w){body}(?!\w)", re.I)))
    # Prefer longer phrases first so "data scientist ii" wins over "data scientist".
    patterns.sort(key=lambda item: len(item[0]), reverse=True)
    return patterns


def _match_search_title(
    title: str, settings: Mapping[str, Any]
) -> tuple[bool, bool]:
    """Return (allowed, is_secondary_only)."""
    primary = list(settings.get("search_terms") or _DEFAULT_SEARCH_TERMS)
    secondary = list(settings.get("secondary_search_terms") or _DEFAULT_SECONDARY)
    hay = _title_haystack(title)
    if not hay:
        return False, False
    primary_hit = any(p.search(hay) for _, p in _term_patterns(primary))
    secondary_hit = any(p.search(hay) for _, p in _term_patterns(secondary))
    if primary_hit:
        # Secondary-only when the only hits are secondary engineering titles
        # and there is no scientist/DS phrase.
        ds_hint = re.search(
            r"(?i)\b(?:data\s+scientist|decision\s+scientist|"
            r"machine\s+learning\s+scientist|ml\s+scientist)\b",
            hay,
        )
        is_secondary = secondary_hit and not ds_hint
        return True, is_secondary
    if secondary_hit:
        return True, True
    return False, False


def _singapore_location(job: Job) -> tuple[bool, str]:
    loc = job.location or ""
    country = (job.country or "").upper()
    if _SG_POSITIVE_RE.search(loc):
        return True, ""
    if country == "SG" and not _SG_NEGATIVE_RE.search(loc):
        return True, ""
    return False, "location_not_singapore"


def located_in_singapore(job: Job) -> bool:
    """Apply the location boundary before cross-source identity merging."""
    return _singapore_location(job)[0]


def _check_years(text: str) -> tuple[bool, str, bool]:
    """Return (ok, reason, flag_2_3)."""
    flag_23 = bool(_TWO_TO_THREE_RE.search(text))
    passing_ranges = [(match.start(), match.end()) for match in _TWO_TO_THREE_RE.finditer(text)]
    for pattern in _MANDATORY_YEARS_RES:
        for match in pattern.finditer(text):
            if any(start < match.start() < end for start, end in passing_ranges):
                continue
            if re.match(r"(?i)\s*(?:track\s+record|history|in\s+business|since\s+our\s+founding)\b", text[match.end():]):
                continue
            years = _parse_year_value(match)
            if years is None:
                continue
            if years >= 3:
                tail = re.split(r"[.!?;\n]", text[match.end():], maxsplit=1)[0][:180]
                equivalent = _LOCAL_EQUIV_RE.search(tail)
                if equivalent and not re.search(r"(?i)\b(?:bachelor|master|phd|degree|qualification)\b", tail[:equivalent.start()]):
                    continue
                return False, "years_experience_too_high", flag_23
    # Explicit high word forms already covered; also catch bare "5+ years".
    bare = re.finditer(r"(?i)\b([3-9]|1[0-5])\s*\+\s*years?\b", text)
    for match in bare:
        if re.match(r"(?i)\s*(?:track\s+record|history|in\s+business)\b", text[match.end():]):
            continue
        tail = re.split(r"[.!?;\n]", text[match.end():], maxsplit=1)[0][:180]
        equivalent = _LOCAL_EQUIV_RE.search(tail)
        if equivalent and not re.search(r"(?i)\b(?:bachelor|master|phd|degree|qualification)\b", tail[:equivalent.start()]):
            continue
        return False, "years_experience_too_high", flag_23
    return True, "", flag_23


def _check_degree(text: str) -> tuple[bool, str]:
    for match in re.finditer(rf"(?i)\b{_DEGREE_NAME_RE}\b", text):
        clause = _clause(text, match.start(), match.end())
        tail = re.split(r"[.!?;\n]", text[match.end():], maxsplit=1)[0][:180]
        equivalent = re.search(r"(?i)\b(?:or\s+equivalent|equivalent\s+(?:experience|qualification|education|achievement))\b", tail)
        if equivalent and not re.search(r"(?i)\b\d+\s*\+?\s*years?\b", tail[:equivalent.start()]):
            continue
        if re.search(r"(?i)\bbachelor[^.!?;\n]{0,100}\bor\b", clause):
            continue
        if _DEGREE_PREFERRED_RE.search(clause) and not _DEGREE_REQUIRED_RE.search(clause):
            continue
        required = bool(_DEGREE_REQUIRED_RE.search(clause))
        implicit = bool(re.search(r"(?i)^\s*(?:a\s+)?(?:degree\s+)?in\b", text[match.end():match.end() + 30]))
        qualification_context = bool(re.search(r"(?i)\b(?:qualifications|requirements|must\s+have|you\s+(?:have|hold)|requires?)\b", text[max(0, match.start() - 100):match.start()]))
        if required or implicit or qualification_context:
            return False, "masters_phd_required"
    return True, ""


def _check_citizenship_clearance(text: str) -> tuple[bool, str]:
    for match in _CITIZEN_PR_RE.finditer(text):
        if not _negated_near(text, match):
            return False, "citizens_pr_only"
    for match in _CLEARANCE_RE.finditer(text):
        if not _negated_near(text, match):
            return False, "security_clearance_required"
    return True, ""


def _check_foundation_focus(text: str) -> tuple[bool, str]:
    strong = re.compile(r"(?i)\b(?:pre-?train(?:ing)?\s+(?:(?:large|language|foundation)\s+)*models?|train(?:ing)?\s+(?:large\s+(?:language\s+)?|foundation\s+)models?|(?:new|novel)\s+(?:deep[ -]learning|dl|neural)\s+architectures?)\b")
    for match in strong.finditer(text):
        clause = _clause(text, match.start(), match.end())
        if not _negated_near(text, match) and not re.search(r"(?i)\b(?:preferred|nice.to.have|familiarity|exposure)\b", clause):
            return False, "foundation_model_research"
    return True, ""


def _money_to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _extract_monthly_sgd(text: str) -> float | None:
    """Best-effort fixed monthly SGD. Unknown / up-to / TC → None."""
    best: float | None = None
    for match in _MONEY_RE.finditer(text):
        span_start = max(0, match.start() - 12)
        prefix = text[span_start:match.start()]
        clause = _clause(text, match.start(), match.end())
        if _UP_TO_RE.search(prefix) or _TOTAL_COMP_RE.search(clause):
            continue
        cur = (match.group("cur") or match.group("cur2") or "").lower()
        period = (match.group("period") or "").lower()
        # Require an SGD marker somewhere nearby unless period implies SG job
        # salary context with S$ already in the match.
        has_sgd = bool(cur)
        if not has_sgd:
            continue
        a = _money_to_float(match.group("a"))
        if a is None:
            continue
        b = _money_to_float(match.group("b")) if match.group("b") else None
        # For ranges use the upper bound: flag only when even the top is below EP.
        value = max(a, b) if b is not None else a
        annual = any(token in period for token in ("year", "yr", "annual", "annum", "p.a", "pa"))
        monthly = any(token in period for token in ("month", "mo", "mth"))
        monthly = monthly or bool(re.search(r"(?i)\b(?:fixed\s+)?monthly\s+(?:base\s+)?salary\b", clause))
        if annual:
            value = value / 12.0
        elif not monthly:
            continue
        if best is None or value > best:
            best = value
    return best


def _threshold_for(settings: Mapping[str, Any], now: datetime, *, fs: bool) -> float | None:
    rows = list(settings.get("ep_thresholds") or [])
    if not rows:
        return None
    moment = ensure_utc(now) or utcnow()
    today = moment.date()

    def _parse_date(raw: Any) -> date | None:
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None

    dated: list[tuple[date, Mapping[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        effective = _parse_date(row.get("effective_date"))
        if effective is None:
            continue
        dated.append((effective, row))
    if not dated:
        return None
    dated.sort(key=lambda item: item[0])
    chosen = dated[0][1]
    for effective, row in dated:
        if effective <= today:
            chosen = row
        else:
            break
    key = "fs" if fs else "non_fs"
    try:
        return float(chosen.get(key))
    except (TypeError, ValueError):
        return None


def _add_flag(job: Job, flag: str) -> None:
    flags = list(job.flags or [])
    if flag not in flags:
        flags.append(flag)
    job.flags = flags


def _check_seniority_title(title: str) -> tuple[bool, str]:
    if _INTERNSHIP_RE.search(title or ""):
        return False, "internship_title"
    if _SENIORITY_RE.search(title or ""):
        return False, "seniority_title"
    return True, ""


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def filter_job(
    job: Job,
    settings: Any,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return ``(keep, reason_slug)`` and attach soft ``job.flags``.

    Soft findings (2–3 year band, salary below the date-effective EP
    threshold, secondary ML/AI engineering titles) never reject on their own.
    """
    cfg = _settings_map(settings)
    moment = ensure_utc(now) or utcnow()
    text = _blob(job)

    employer_type = str(job.raw.get("employer_type", "")).casefold()
    if employer_type in {"government", "statutory_board", "defence", "defense"}:
        return False, "excluded_employer_type"
    if re.search(r"(?i)\b(?:defen[cs]e|military|weapons|combat)\b", job.title) or re.search(
        r"(?i)\b(?:work(?:ing)?\s+on|develop(?:ing)?|build(?:ing)?|support(?:ing)?|focus(?:ed)?\s+on)\s+(?:\w+\s+){0,3}(?:defen[cs]e|military|weapons|combat)\b", job.description):
        return False, "defence_related"
    if str(job.raw.get("employmentType", "")).casefold() in {"intern", "internship"} or re.search(
        r"(?i)\b(?:this\s+(?:role|position)\s+is\s+(?:an?\s+)?internship|internship\s+(?:role|position))\b", job.description):
        return False, "internship_title"

    ok, reason = _singapore_location(job)
    if not ok:
        return False, reason

    ok, reason = _check_seniority_title(job.title or "")
    if not ok:
        return False, reason

    allowed, secondary = _match_search_title(job.title or "", cfg)
    if not allowed:
        return False, "title_not_relevant"
    if secondary:
        _add_flag(job, FLAG_SECONDARY)

    ok, reason, flag_23 = _check_years(text)
    if not ok:
        return False, reason
    if flag_23:
        _add_flag(job, FLAG_YEARS)

    ok, reason = _check_degree(text)
    if not ok:
        return False, reason

    ok, reason = _check_citizenship_clearance(text)
    if not ok:
        return False, reason

    ok, reason = _check_foundation_focus(text)
    if not ok:
        return False, reason

    monthly = _extract_monthly_sgd(
        " ".join(p for p in (job.salary or "", job.description or "") if p)
    )
    if monthly is not None:
        fs = bool((job.raw or {}).get("fs"))
        threshold = _threshold_for(cfg, moment, fs=fs)
        if threshold is not None and monthly < threshold:
            _add_flag(job, FLAG_SALARY)

    if job.country is None and _SG_POSITIVE_RE.search(job.location or ""):
        job.country = "SG"

    return True, ""


def apply_filters(
    jobs: Iterable[Job],
    settings: Any,
    now: datetime | None = None,
) -> FilterResult:
    """Run every Singapore knockout over ``jobs``; stamp soft flags on keepers."""
    moment = ensure_utc(now) or utcnow()
    result = FilterResult()
    for job in jobs or []:
        try:
            ok, reason = filter_job(job, settings, now=moment)
        except Exception as exc:  # malformed posting must not end the run
            ok, reason = False, f"filter_error:{type(exc).__name__}"
        if ok:
            result.kept.append(job)
            continue
        result.rejected.append((job, reason))
        result.counts[reason] = result.counts.get(reason, 0) + 1
    return result
