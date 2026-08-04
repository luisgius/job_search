"""Core data structures shared by every stage of the pipeline.

The whole pipeline is a sequence of pure-ish transforms over `Job` objects:

    sources -> [Job]  -> filters -> [Job] -> scoring -> [ScoredJob]
            -> tailoring -> [ScoredJob(+artifacts)] -> apply -> digest

Everything here is dependency-free (stdlib only) so it can be imported from
tests without pulling in requests/anthropic/playwright.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------
# normalisation helpers
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
# Letters NFKD refuses to decompose because they are distinct letters rather
# than accented ones. Without these, "København" and "Copenhagen"-adjacent
# spellings never match, and Polish/Croatian company names drift.
_TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH", "ı": "i", "İ": "I", "ħ": "h", "Ħ": "H",
    "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
})
# "B.V." / "N.V." / "S.A." are one token, not two. Collapse dotted initialisms
# before punctuation becomes whitespace, so the legal-suffix strip can see
# them -- while leaving "Booking.com" alone.
_INITIALISM_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")
# Legal-entity suffixes that differ between sources for the same company
# ("Spotify" vs "Spotify AB", "Zalando SE" vs "Zalando").
_COMPANY_SUFFIXES = {
    "inc", "inc.", "llc", "ltd", "limited", "gmbh", "ag", "ab", "as", "a/s",
    "bv", "b.v.", "nv", "n.v.", "sa", "s.a.", "sas", "sarl", "srl", "spa",
    "se", "oy", "oyj", "plc", "corp", "corporation", "co", "company", "aps",
    "kft", "sp", "zoo", "z.o.o", "sl", "s.l.", "ug", "kg", "eg", "holding",
    "group", "the",
}


def normalize_text(value: str | None) -> str:
    """Lowercase, fold accents, strip punctuation and collapse whitespace."""
    if not value:
        return ""
    transliterated = str(value).translate(_TRANSLITERATE)
    decomposed = unicodedata.normalize("NFKD", transliterated)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    depunct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", depunct).strip()


def normalize_company(value: str | None) -> str:
    """Normalise a company name and drop trailing legal-entity noise."""
    collapsed = _INITIALISM_RE.sub(
        lambda m: m.group(0).replace(".", ""), str(value or "")
    )
    tokens = normalize_text(collapsed).split()
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    # Never normalise a company down to nothing (e.g. a company literally
    # called "The Group") - fall back to the un-stripped form.
    return " ".join(tokens) if tokens else normalize_text(value)


#: Parenthetical content that is decoration rather than identity. Stripping
#: these lets "Backend Engineer" and "Backend Engineer (m/f/d)" collapse.
#: Anything NOT matching stays, because "(Payments)" and "(Machine Learning)"
#: are what distinguish two genuinely different requisitions — dropping them
#: silently deletes one of the two jobs before it is ever scored.
_NOISE_PARENTHETICAL_RE = re.compile(
    r"""^\s*(?:
        [mwfdhxu](?:\s*[/|]\s*[mwfdhxu])+          # m/f/d, w/m/d, h/f, m/f/x
      | all\s+genders? | any\s+gender | gn | d\s*/\s*f\s*/\s*m
      | remote\b.* | hybrid | on[\s-]?site | in[\s-]?office
      | full[\s-]?time | part[\s-]?time | permanent | temporary
      | contract | freelance | fixed[\s-]?term | interim
      | [a-z]{2,3}\s*[/|]\s*[a-z]{2,3}             # de/en, en/fr
      | \d+\s*%                                     # (80%)
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _strip_noise_parentheticals(raw: str) -> str:
    """Drop only the decorative brackets, keep the meaningful ones."""
    def _replace(match: "re.Match[str]") -> str:
        inner = match.group(1)
        return " " if _NOISE_PARENTHETICAL_RE.match(inner) else match.group(0)

    raw = re.sub(r"\(([^()]*)\)", _replace, raw)
    raw = re.sub(r"\[([^\[\]]*)\]", _replace, raw)
    return raw


def normalize_title(value: str | None) -> str:
    """Normalise a job title, dropping only decorative parentheticals.

    "(m/f/d)", "(Remote)" and "(Full-time)" are noise on the same posting;
    "(Payments)" and "(Machine Learning)" are two different jobs.
    """
    return normalize_text(_strip_noise_parentheticals(str(value or "")))


def _short_hash(*parts: str, length: int = 16) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def utcnow() -> datetime:
    """Timezone-aware 'now'. Injectable everywhere a clock is needed."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC. Naive input is assumed UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------


@dataclass
class Job:
    """A single normalised job posting.

    Sources are responsible for producing these; every downstream stage only
    ever sees `Job`, never a source-specific payload.
    """

    source: str                       # "greenhouse" | "lever" | "adzuna" | "linkedin_email"
    company: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    posted_at: datetime | None = None     # tz-aware UTC, None when unknown
    remote: bool | None = None
    salary: str | None = None
    country: str | None = None            # ISO-3166 alpha-2, filled by filters.geo
    ats: str | None = None                # "greenhouse" | "lever" | None
    ats_job_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.company = (self.company or "").strip()
        self.title = (self.title or "").strip()
        self.url = (self.url or "").strip()
        self.location = (self.location or "").strip()
        self.description = self.description or ""
        self.posted_at = ensure_utc(self.posted_at)
        if self.country:
            self.country = self.country.upper()

    # -- identity ---------------------------------------------------------

    @property
    def key(self) -> str:
        """Stable identity for the tracker DB.

        Prefers the ATS-assigned id, which is globally unique within its
        vendor and survives title edits, relocations and company renames.

        The display name is deliberately NOT part of it: Greenhouse and Lever
        payloads carry no company field, so it is derived from the board slug
        or overridden in the watchlist. Mixing it in meant that adding the
        documented `{slug: acme, company: ACME Technologies}` override re-keyed
        every open requisition on that board — and re-keying is how an
        already-applied job becomes eligible again.
        """
        if self.ats and self.ats_job_id:
            return _short_hash(self.ats, str(self.ats_job_id))
        return _short_hash(
            normalize_company(self.company),
            normalize_title(self.title),
            normalize_text(self.location),
        )

    @property
    def dedupe_key(self) -> str:
        """Fuzzy identity used to collapse the same role seen via several
        sources in a single run (e.g. Greenhouse *and* a LinkedIn alert).

        Deliberately ignores the ATS id and the free-text location tail so
        "Berlin, Germany" and "Berlin" collapse together.
        """
        return _short_hash(
            normalize_company(self.company),
            normalize_title(self.title),
            self.city or self.country or "",
        )

    @property
    def city(self) -> str:
        """The city part of the location, normalised.

        The first comma-segment, so "Berlin, Germany" and "Berlin" agree while
        "Berlin" and "Munich" stay apart. Keying on the country instead merged
        two genuinely different requisitions a company had open in two cities.
        """
        head = str(self.location or "").split(",")[0]
        head = re.sub(r"\b(remote|hybrid|on[\s-]?site)\b", " ", head, flags=re.I)
        return normalize_text(head)

    # -- convenience ------------------------------------------------------

    @property
    def age_hours(self) -> float | None:
        if self.posted_at is None:
            return None
        return (utcnow() - self.posted_at).total_seconds() / 3600.0

    def age_hours_at(self, now: datetime) -> float | None:
        if self.posted_at is None:
            return None
        return (ensure_utc(now) - self.posted_at).total_seconds() / 3600.0

    @property
    def label(self) -> str:
        return f"{self.company} — {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "location": self.location,
            "description": self.description,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "remote": self.remote,
            "salary": self.salary,
            "country": self.country,
            "ats": self.ats,
            "ats_job_id": self.ats_job_id,
        }


# --------------------------------------------------------------------------
# Scoring / tailoring
# --------------------------------------------------------------------------


@dataclass
class Score:
    """Result of the LLM fit-scoring call."""

    value: int                       # 0-100
    reasons: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    verdict: str = ""                # one-line summary from the model
    model: str = ""
    error: str | None = None         # set when the call failed / was unparseable

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Artifacts:
    """Files produced for a single match."""

    dir: str | None = None
    cv_md: str | None = None
    cover_md: str | None = None
    cv_pdf: str | None = None
    cover_pdf: str | None = None
    screenshot: str | None = None


class ApplyStatus(str, Enum):
    """Terminal state of a job for one run.

    Persisted verbatim in the tracker DB, so values are part of the schema.
    """

    NEW = "new"                          # seen, nothing decided yet
    FILTERED = "filtered"                # dropped by hard filters
    SCORED_BELOW = "scored_below"        # scored under threshold
    DIGEST = "digest"                    # needs a human click
    DRY_RUN = "dry_run"                  # form filled + screenshotted, not submitted
    APPLIED = "applied"                  # auto-submitted
    APPLY_FAILED = "apply_failed"        # attempted, blew up -> also lands in digest
    SKIPPED_DUPLICATE = "skipped_duplicate"


@dataclass
class ScoredJob:
    """A `Job` carried through scoring / tailoring / applying."""

    job: Job
    score: Score | None = None
    artifacts: Artifacts = field(default_factory=Artifacts)
    status: ApplyStatus = ApplyStatus.NEW
    status_detail: str = ""
    cover_letter_md: str | None = None
    tailored_cv_md: str | None = None

    @property
    def score_value(self) -> int:
        return self.score.value if self.score else 0

    @property
    def key(self) -> str:
        return self.job.key


@dataclass
class RunStats:
    """Counters surfaced in the digest and the run log."""

    fetched: int = 0
    after_dedupe: int = 0
    after_filters: int = 0
    already_seen: int = 0
    scored: int = 0
    matches: int = 0
    tailored: int = 0
    auto_applied: int = 0
    dry_run: int = 0
    apply_failed: int = 0
    digest_items: int = 0
    errors: list[str] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "after_dedupe": self.after_dedupe,
            "after_filters": self.after_filters,
            "already_seen": self.already_seen,
            "scored": self.scored,
            "matches": self.matches,
            "tailored": self.tailored,
            "auto_applied": self.auto_applied,
            "dry_run": self.dry_run,
            "apply_failed": self.apply_failed,
            "digest_items": self.digest_items,
            "errors": list(self.errors),
            "source_counts": dict(self.source_counts),
        }
