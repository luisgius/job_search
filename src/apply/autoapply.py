"""Auto-apply — the one stage that can act on the user's behalf.

WHAT THIS MODULE DOES
  * Opens a Greenhouse or Lever posting in a browser page.
  * Fills only fields it can answer from `config.applicant` — name, email,
    phone, LinkedIn/GitHub/website — uploads the tailored CV PDF, and
    screenshots the filled form.
  * With `apply.dry_run: true` (the default) it stops there: the screenshot
    is the deliverable and nothing is submitted.
  * With `apply.dry_run: false` it clicks submit on that one boring form and
    waits for a confirmation.

WHAT THIS MODULE NEVER DOES
  * Answer a question. Any dropdown, any radio group, any textarea that is
    not an optional cover letter, any unrecognised required field, any label
    with a "?" or with "why", "sponsorship", "salary", "notice period",
    "how did you hear", or a diversity question, sends the job to the digest
    for a human instead.
  * Guess at a field it does not recognise, or submit without the CV it
    promised to attach.
  * Apply twice — `tracker.has_applied` is checked before a page is opened.
  * Touch any ATS other than Greenhouse and Lever.
  * Raise. Every failure becomes an `ApplyOutcome` and lands in the digest.

The bias is one-directional on purpose: a wrong bail costs the user one
click, a wrong submit costs them their standing with an employer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..models import ApplyStatus, ScoredJob, ensure_utc, normalize_text, utcnow
from ..util import ensure_dir, get_logger, slugify

logger = get_logger(__name__)

#: The only two application forms this bot is allowed to touch.
SUPPORTED_ATS: tuple[str, ...] = ("greenhouse", "lever")

#: More inputs than this and the form is a screener, not an application.
MAX_FORM_FIELDS = 12

#: Selectors tried, in order, to find the submit control.
SUBMIT_SELECTORS: tuple[str, ...] = (
    "#submit_app",                    # Greenhouse
    "button[type=submit]",
    "input[type=submit]",
    "button#btn-submit",              # Lever
    ".application--submit button",
)

#: Selectors that mean "the application went through". Each is waited for in
#: turn; the first one that resolves wins.
CONFIRMATION_SELECTORS: tuple[str, ...] = (
    "#application_confirmation",
    ".application-confirmation",
    ".confirmation",
    "text=Thank you",
    "text=Application submitted successfully",
)

#: Fallback confirmation signal, checked against `page.content()`.
CONFIRMATION_TEXTS: tuple[str, ...] = (
    "thank you", "application submitted", "successfully submitted",
    "application received", "we have received", "thanks for applying",
)

#: Input types that are not user-answerable form fields.
IGNORED_INPUT_TYPES: frozenset[str] = frozenset(
    {"hidden", "submit", "button", "reset", "image"}
)

#: Where a field's human label may hide, best first.
LABEL_ATTRIBUTES: tuple[str, ...] = (
    "aria-label", "data-label", "placeholder", "title", "alt",
)

_CSS_ID_RE = re.compile(r"^[A-Za-z][\w-]*$")
_GREENHOUSE_JOB_PATH_RE = re.compile(
    r"/(jobs?|embed|job_app|application|applications|apply)(/|$)", re.IGNORECASE
)


# --------------------------------------------------------------------------
# field vocabulary
# --------------------------------------------------------------------------

#: Phrase -> field kind, checked in this order. Phrases are matched as whole
#: words against the normalised label+name+id of a field, so "name" cannot
#: fire on "username" and "first name" is decided before plain "name".
ALLOWED_FIELD_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("email", ("email", "e mail", "email address")),
    ("phone", ("phone", "telephone", "mobile", "phone number", "cell")),
    ("linkedin", ("linkedin", "linked in", "linkedin profile", "linkedin url")),
    ("github", ("github", "git hub", "github url", "github profile")),
    ("website", (
        "website", "web site", "personal website", "portfolio", "blog",
        "homepage", "personal site", "url", "other website",
    )),
    ("first_name", ("first name", "firstname", "given name", "forename")),
    ("last_name", ("last name", "lastname", "surname", "family name")),
    ("name", ("name", "full name", "fullname", "your name", "applicant name")),
)

RESUME_HINTS: tuple[str, ...] = (
    "resume", "resumé", "cv", "curriculum vitae", "curriculum", "upload resume",
)
COVER_LETTER_HINTS: tuple[str, ...] = (
    "cover letter", "coverletter", "covering letter", "letter",
)
CONSENT_HINTS: tuple[str, ...] = (
    "consent", "privacy", "privacy policy", "terms", "terms and conditions",
    "gdpr", "data protection", "i agree", "agree", "acknowledge",
    "i have read", "policy",
)
#: Consent-shaped checkboxes we still refuse to tick: opting the user into
#: marketing is not a legal necessity, it is a decision.
MARKETING_HINTS: tuple[str, ...] = (
    "marketing", "newsletter", "subscribe", "updates", "promotions",
    "future opportunities", "talent community", "mailing list",
)

#: Labels that mean the form is asking a question. Matched against the
#: normalised label+name+id; a literal "?" is checked separately on the raw
#: text because normalisation strips punctuation.
QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in (
        r"\bwhy\b",
        r"\bdescribe\b",
        r"\btell us\b",
        r"\bexplain\b",
        r"\bsponsor(ship|ed|ing|s)?\b",
        r"\bvisas?\b",
        r"\bauthori[sz](ed|ation|e)\b",
        r"\bright to work\b",
        r"\bwork permit\b",
        r"\bsalar(y|ies)\b",
        r"\bcompensation\b",
        r"\bexpected\b",
        r"\bexpectations?\b",
        r"\bnotice period\b",
        r"\bavailab(le|ility)\b",
        r"\bstart date\b",
        r"\bhow did you hear\b",
        r"\breferr(al|ed|er)\b",
        r"\bgender\b",
        r"\brace\b",
        r"\bethnic(ity|ities)?\b",
        r"\bveterans?\b",
        r"\bdisabilit(y|ies)\b",
        r"\bpronouns?\b",
        r"\byears of experience\b",
        r"\bhow many years\b",
        r"\bportfolio required\b",
    )
)


@dataclass
class ApplyOutcome:
    """What happened on one application attempt."""

    status: ApplyStatus
    detail: str
    screenshot: str | None = None


# --------------------------------------------------------------------------
# config access
# --------------------------------------------------------------------------


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    """Read a dotted key from a `Config` *or* a plain nested dict."""
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(dotted, default)
    return default


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _applicant(config: Any) -> dict[str, Any]:
    section = getattr(config, "applicant", None)
    if isinstance(section, Mapping):
        return dict(section)
    section = _cfg(config, "applicant", {})
    return dict(section) if isinstance(section, Mapping) else {}


def _output_dir(config: Any) -> Path:
    out = getattr(config, "output_dir", None)
    if out:
        return Path(str(out))
    return Path(str(_cfg(config, "output.dir", "output") or "output"))


def _timeout_ms(config: Any) -> int:
    seconds = _int(_cfg(config, "apply.timeout_seconds", 60), 60)
    return max(1, seconds) * 1000


# --------------------------------------------------------------------------
# ATS detection
# --------------------------------------------------------------------------


def detect_ats(url: str | None) -> str | None:
    """Which supported ATS hosts this URL, or None.

    Matching is on the *hostname* only. A tracking URL such as
    `https://jobs.example.com/go?to=https://jobs.lever.co/acme/1` contains
    "lever.co" but is not a Lever form, and a substring test would happily
    drive the bot into a stranger's website.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    # Tolerate scheme-less input ("boards.greenhouse.io/acme/jobs/1").
    parts = urlsplit(raw if "//" in raw else f"//{raw}", scheme="https")
    # An application form is served over HTTP(S) and nothing else. Without
    # this, "ftp://boards.greenhouse.io/..." reaches the browser on the
    # strength of its hostname alone.
    if (parts.scheme or "").lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        return None
    path = parts.path or "/"

    # Lever only ever serves applications from jobs.lever.co (or a company
    # sub-host of it). hire.lever.co / www.lever.co are not applicant-facing.
    if host == "jobs.lever.co" or host.endswith(".jobs.lever.co"):
        return "lever"

    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        # boards. / job-boards. only ever serve postings; on any other
        # greenhouse.io host (incl. the marketing site) require a job path.
        first_label = host.split(".")[0]
        if "boards" in first_label or _GREENHOUSE_JOB_PATH_RE.search(path):
            return "greenhouse"
    return None


# --------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------


def eligible(
    scored: ScoredJob,
    config: Any,
    tracker: Any = None,
) -> tuple[bool, str]:
    """May this job be auto-applied to? Returns `(ok, reason)`.

    Gates in the contract's order, returning the *first* failure as text the
    digest can show the user verbatim. `reason` is empty when eligible.
    """
    job = scored.job

    if not bool(_cfg(config, "apply.enabled", True)):
        return False, "auto-apply is off (apply.enabled: false)"

    ats = detect_ats(job.url)
    if ats not in SUPPORTED_ATS:
        return False, (
            "not a Greenhouse or Lever application form — apply by hand: "
            f"{job.url}"
        )

    min_score = _int(_cfg(config, "apply.min_score", 80), 80)
    if scored.score_value < min_score:
        return False, (
            f"score {scored.score_value} is below apply.min_score ({min_score})"
        )

    if bool(_cfg(config, "apply.require_pdf", True)):
        pdf = str(getattr(scored.artifacts, "cv_pdf", "") or "")
        if not pdf:
            return False, (
                "no tailored CV PDF (apply.require_pdf is true) — add "
                "src/render_pdf.py to enable auto-apply"
            )
        if not Path(pdf).is_file():
            return False, f"tailored CV PDF is missing on disk: {pdf}"

    if tracker is not None:
        try:
            applied = bool(tracker.has_applied(job.key))
        except Exception as exc:  # a broken tracker must not license a re-apply
            logger.warning("tracker.has_applied failed for %s: %s", job.key, exc)
            return False, f"could not verify apply history ({exc}) — not applying"
        if applied:
            return False, "already applied to this job"

    return True, ""


# --------------------------------------------------------------------------
# field classification (pure — no browser required)
# --------------------------------------------------------------------------


def _field_text(field: Mapping[str, Any]) -> str:
    """Normalised label + name + id, for whole-word phrase matching."""
    parts = [
        str(field.get("label") or ""),
        str(field.get("name") or ""),
        str(field.get("id") or ""),
    ]
    # Underscores survive `normalize_text` (they are word characters), so
    # "first_name" would never match the phrase "first name" without this.
    return normalize_text(" ".join(p for p in parts if p).replace("_", " "))


def _matched(text: str, phrases: tuple[str, ...]) -> str | None:
    """First phrase present in `text` as a whole word/phrase, else None."""
    padded = f" {text} "
    for phrase in phrases:
        if f" {phrase} " in padded:
            return phrase
    return None


def classify_field(field: Mapping[str, Any]) -> str:
    """Name the role of one form field from its plain-dict description.

    `field` is `{tag, type, name, id, label, required, options}` — the shape
    `collect_fields` produces — so every hard decision in this module stays
    unit-testable without a browser.

    Returns one of: name, first_name, last_name, email, phone, resume,
    linkedin, website, github, cover_letter_optional, consent, unknown.
    "unknown" is the honest answer and the safe one: `inspect_form` bails on
    any *required* field that lands here.
    """
    tag = str(field.get("tag") or "input").lower()
    ftype = str(field.get("type") or "").lower()
    text = _field_text(field)
    required = bool(field.get("required"))

    if tag == "select" or ftype in ("select", "select-one", "radio"):
        return "unknown"

    if ftype == "checkbox":
        if _matched(text, MARKETING_HINTS):
            return "unknown"      # opting into marketing is a decision, not consent
        return "consent" if _matched(text, CONSENT_HINTS) else "unknown"

    if tag == "textarea":
        # Only ever tolerated when it is an *optional* cover letter; the name
        # of the category carries that condition on purpose.
        if not required and _matched(text, COVER_LETTER_HINTS):
            return "cover_letter_optional"
        return "unknown"

    if ftype == "file":
        # A file input that is not clearly the CV could be anything (writing
        # sample, portfolio, ID scan) — we do not upload into a guess.
        return "resume" if _matched(text, RESUME_HINTS) else "unknown"

    if ftype == "email":
        return "email"
    if ftype == "tel":
        return "phone"

    for kind, phrases in ALLOWED_FIELD_PATTERNS:
        if _matched(text, phrases):
            return kind

    if _matched(text, RESUME_HINTS):
        return "resume"
    return "unknown"


def field_selector(field: Mapping[str, Any]) -> str:
    """A CSS selector addressing this field, preferring its id."""
    tag = str(field.get("tag") or "input").lower()
    ident = str(field.get("id") or "").strip()
    if ident and _CSS_ID_RE.match(ident):
        return f"#{ident}"
    name = str(field.get("name") or "").strip()
    if name:
        return f'{tag}[name="{name}"]'
    ftype = str(field.get("type") or "").strip()
    if ftype:
        return f'{tag}[type="{ftype}"]'
    return tag


def _describe(field: Mapping[str, Any]) -> str:
    """Human handle for a field, for bail reasons the user can act on."""
    label = str(field.get("label") or "").strip()
    name = str(field.get("name") or field.get("id") or "").strip()
    if label and name:
        return f"{label!r} (name={name})"
    if label:
        return repr(label)
    if name:
        return f"(name={name})"
    return f"<{str(field.get('tag') or 'field')}>"


def question_trigger(field: Mapping[str, Any]) -> str | None:
    """The reason this field reads as a question, or None.

    Checked against the label *and* the name/id: plenty of forms label a
    screener only in `name="why_us"`.
    """
    raw = " ".join(
        str(field.get(key) or "") for key in ("label", "name", "id")
    )
    if "?" in raw:
        return "contains a question mark"
    text = _field_text(field)
    for pattern in QUESTION_PATTERNS:
        found = pattern.search(text)
        if found:
            return f"mentions {found.group(0)!r}"
    return None


# --------------------------------------------------------------------------
# DOM -> plain dicts
# --------------------------------------------------------------------------


def _attr(element: Any, name: str) -> str:
    try:
        return str(element.get_attribute(name) or "").strip()
    except Exception:
        return ""


def _label_of(element: Any) -> str:
    for attribute in LABEL_ATTRIBUTES:
        value = _attr(element, attribute)
        if value:
            return value
    try:
        return str(element.inner_text() or "").strip()
    except Exception:
        return ""


def _is_required(element: Any, label: str) -> bool:
    """True when the field is required.

    `<input required>` yields `""` from `get_attribute` in a real DOM and
    `"true"` in the test fake, so *presence* is what counts — only an
    explicit falsey value means "not required".
    """
    raw = None
    try:
        raw = element.get_attribute("required")
    except Exception:
        raw = None
    if raw is not None and str(raw).strip().lower() not in ("false", "0", "no"):
        return True
    if _attr(element, "aria-required").lower() in ("true", "1", "yes", "required"):
        return True
    stripped = label.strip().lower()
    return stripped.endswith("*") or "(required)" in stripped


def _options_of(element: Any) -> list[str]:
    """Option labels when the element exposes them; `[]` otherwise.

    Deliberately does not walk the DOM: the element protocol is only
    `get_attribute` + `inner_text`, and every `<select>` bails anyway, so
    options are informational.
    """
    raw = getattr(element, "options", None)
    if isinstance(raw, (list, tuple)):
        return [str(option) for option in raw]
    return []


def collect_fields(page: Any) -> list[dict[str, Any]]:
    """Turn the page's form controls into plain dicts.

    One query per tag, because the element protocol has no way to ask an
    element what tag it is (`evaluate` is off-limits by design). The result is
    therefore grouped inputs → textareas → selects rather than document order,
    which no caller depends on.

    Returns `[]` on any query failure — which makes `inspect_form` bail, the
    safe direction.
    """
    fields: list[dict[str, Any]] = []
    for tag in ("input", "textarea", "select"):
        try:
            elements = page.query_selector_all(tag)
        except Exception as exc:
            logger.warning("could not query %s elements: %s", tag, exc)
            continue
        for element in elements or []:
            ftype = _attr(element, "type").lower()
            if tag == "input":
                ftype = ftype or "text"
                if ftype in IGNORED_INPUT_TYPES:
                    continue
            else:
                ftype = ftype or tag
            label = _label_of(element)
            fields.append({
                "tag": tag,
                "type": ftype,
                "name": _attr(element, "name"),
                "id": _attr(element, "id"),
                "label": label,
                "required": _is_required(element, label),
                "options": _options_of(element),
            })
    return fields


# --------------------------------------------------------------------------
# the safety core
# --------------------------------------------------------------------------


def inspect_form(page: Any) -> tuple[bool, str]:
    """Is this form simple enough to fill without answering anything?

    Returns `(True, "")` only for the boring case: names, email, phone, a
    resume upload, optional profile URLs, an optional cover-letter textarea
    and a plain consent checkbox. Everything else returns `(False, reason)`
    naming the offending field, and that reason is shown to the user in the
    digest.

    `page` follows the protocol documented on `apply_one`.
    """
    fields = collect_fields(page)

    if len(fields) > MAX_FORM_FIELDS:
        return False, (
            f"form has {len(fields)} fields (limit {MAX_FORM_FIELDS}) — "
            "long forms are screeners, not applications"
        )

    for field in fields:
        what = _describe(field)
        tag = str(field.get("tag") or "").lower()
        ftype = str(field.get("type") or "").lower()
        kind = classify_field(field)

        if tag == "select" or ftype.startswith("select"):
            return False, (
                f"dropdown/select {what} — this bot never picks an option "
                "for you"
            )
        if ftype == "radio":
            return False, f"radio group {what} — this bot never picks an answer"
        if tag == "textarea":
            if bool(field.get("required")):
                return False, (
                    f"required textarea {what} — free-text answers are yours "
                    "to write"
                )
            if kind != "cover_letter_optional":
                return False, (
                    f"textarea {what} is not clearly an optional cover letter"
                )
        if ftype == "checkbox" and kind != "consent":
            return False, (
                f"checkbox {what} is not a plain consent/privacy "
                "acknowledgement"
            )

        trigger = question_trigger(field)
        if trigger:
            return False, f"field {what} {trigger} — that is a question for you"

        if bool(field.get("required")) and kind == "unknown":
            return False, f"required field {what} is not one this bot can fill"

    if not any(classify_field(field) == "resume" for field in fields):
        return False, (
            "no resume upload field found — this does not look like a simple "
            "application form"
        )

    return True, ""


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


def artifact_dir_for(scored: ScoredJob, config: Any) -> Path:
    """Where this application's screenshot belongs.

    Prefers the directory the tailoring stage already created, so the CV,
    cover letter and screenshot stay together.
    """
    existing = str(getattr(scored.artifacts, "dir", "") or "")
    if existing:
        return Path(existing)
    job = scored.job
    return (
        _output_dir(config) / "applications"
        / f"{slugify(f'{job.company}-{job.title}')}-{job.key}"
    )


def _values_for(config: Any, scored: ScoredJob) -> dict[str, str]:
    """Field kind -> the value to type. Only ever from `config.applicant`."""
    applicant = _applicant(config)
    full = str(applicant.get("name") or "").strip()
    first, _, rest = full.partition(" ")
    return {
        "name": full,
        # First token / remainder: "Ada King Lovelace" -> "Ada" + "King
        # Lovelace". Never synthesised when the user gave one token only.
        "first_name": first,
        "last_name": rest.strip(),
        "email": str(applicant.get("email") or "").strip(),
        "phone": str(applicant.get("phone") or "").strip(),
        "linkedin": str(applicant.get("linkedin") or "").strip(),
        "github": str(applicant.get("github") or "").strip(),
        "website": str(applicant.get("website") or "").strip(),
    }


def _cv_pdf(scored: ScoredJob) -> str | None:
    """The tailored CV PDF, only if it really exists on disk."""
    path = str(getattr(scored.artifacts, "cv_pdf", "") or "")
    if path and Path(path).is_file():
        return path
    return None


def _fill_fields(
    page: Any,
    fields: list[dict[str, Any]],
    config: Any,
    scored: ScoredJob,
    cv_pdf: str | None,
) -> list[str]:
    """Fill what we can identify. Returns the kinds actually filled.

    Errors are *not* swallowed: a page that cannot be filled must fail the
    attempt rather than get submitted half-empty.
    """
    values = _values_for(config, scored)
    cover = str(scored.cover_letter_md or "").strip()
    filled: list[str] = []

    for field in fields:
        kind = classify_field(field)
        selector = field_selector(field)

        if kind == "resume":
            if cv_pdf:
                page.set_input_files(selector, cv_pdf)
                filled.append("resume")
            continue
        if kind == "cover_letter_optional":
            if cover:
                page.fill(selector, cover)
                filled.append("cover_letter")
            continue

        value = values.get(kind, "")
        if not value:
            continue     # unknown/consent fields, and anything unset in config
        page.fill(selector, value)
        filled.append(kind)

    return filled


def _screenshot(page: Any, directory: Path) -> str | None:
    """Save `<artifact_dir>/form_filled.png`. Diagnostic, never fatal."""
    try:
        target = ensure_dir(directory) / "form_filled.png"
        page.screenshot(path=str(target))
        return str(target)
    except Exception as exc:
        logger.warning("could not screenshot the filled form: %s", exc)
        return None


def _find_submit(page: Any) -> str | None:
    for selector in SUBMIT_SELECTORS:
        try:
            found = page.query_selector_all(selector)
        except Exception:
            continue
        if found:
            return selector
    return None


def _confirmed(page: Any, timeout_ms: int) -> str | None:
    """Wait for any confirmation signal. Returns the signal, or None.

    The per-selector budget is split so a form with no confirmation cannot
    stall the run for `len(CONFIRMATION_SELECTORS)` × the full timeout.
    """
    budget = max(1000, timeout_ms // max(1, len(CONFIRMATION_SELECTORS)))
    for selector in CONFIRMATION_SELECTORS:
        try:
            if page.wait_for_selector(selector, timeout=budget) is not None:
                return selector
        except Exception:
            continue
    try:
        content = str(page.content() or "").lower()
    except Exception:
        return None
    for phrase in CONFIRMATION_TEXTS:
        if phrase in content:
            return phrase
    return None


def _record(
    scored: ScoredJob,
    outcome: ApplyOutcome,
    tracker: Any,
    *,
    method: str,
    artifacts_dir: Path | None,
    now: datetime,
) -> ApplyOutcome:
    """Persist the outcome and hang the screenshot off the ScoredJob."""
    if outcome.screenshot:
        scored.artifacts.screenshot = outcome.screenshot
    if tracker is None:
        return outcome
    # `applications.key` is a foreign key onto `jobs.key`, so the posting has
    # to exist before an outcome can be written. `record_job` is an upsert, so
    # doing it here costs nothing when the pipeline already recorded it.
    record_job = getattr(tracker, "record_job", None)
    if callable(record_job):
        try:
            record_job(scored.job, now=now)
        except Exception as exc:
            logger.debug("could not upsert %s before its status: %s",
                         scored.job.key, exc)
    try:
        tracker.record_status(
            scored.job.key,
            outcome.status,
            detail=outcome.detail,
            score=scored.score_value,
            method=method,
            artifacts_dir=str(artifacts_dir) if artifacts_dir else None,
            now=now,
        )
    except Exception as exc:
        logger.warning("could not record %s for %s: %s",
                       outcome.status, scored.job.key, exc)
    return outcome


def apply_one(
    scored: ScoredJob,
    config: Any,
    *,
    page: Any = None,
    tracker: Any = None,
    now: datetime | None = None,
) -> ApplyOutcome:
    """Attempt one application on an already-eligible job.

    Eligibility (`eligible`) is `run`'s job; this function is about the form
    in front of it: open it, refuse it if it asks anything, fill what it can,
    screenshot it, and submit *only* when `apply.dry_run` is false.

    THE PAGE PROTOCOL — everything this module is allowed to call, and hence
    everything a fake page in the tests must implement:

        page.goto(url, timeout=ms)            -> None
        page.query_selector_all(selector)     -> list[Element]
        page.fill(selector, value)            -> None
        page.set_input_files(selector, path)  -> None
        page.click(selector)                  -> None
        page.screenshot(path=...)             -> None
        page.wait_for_selector(sel, timeout=ms)  -> Element  (raises on timeout)
        page.content()                        -> str
        page.url                              -> str

        Element.get_attribute(name)           -> str | None
        Element.inner_text()                  -> str

    `Element.evaluate` is deliberately never used, so the surface stays small
    enough to fake exactly.

    Never raises: every failure is an `ApplyOutcome`, and the job reaches the
    digest either way.
    """
    now = ensure_utc(now) or utcnow()
    job = scored.job
    method = detect_ats(job.url) or ""
    art_dir = artifact_dir_for(scored, config)
    dry_run = bool(_cfg(config, "apply.dry_run", True))
    timeout_ms = _timeout_ms(config)

    def finish(status: ApplyStatus, detail: str,
               screenshot: str | None = None) -> ApplyOutcome:
        return _record(
            scored, ApplyOutcome(status, detail, screenshot), tracker,
            method=method, artifacts_dir=art_dir, now=now,
        )

    if page is None:
        # Nothing was attempted, so this is not a failure — it is a hand-off.
        return finish(ApplyStatus.DIGEST, "no browser page available — apply by hand")

    try:
        page.goto(job.url, timeout=timeout_ms)

        simple, reason = inspect_form(page)
        if not simple:
            logger.info("bailing on %s: %s", job.label, reason)
            return finish(ApplyStatus.DIGEST, reason)

        cv_pdf = _cv_pdf(scored)
        if cv_pdf is None and not dry_run:
            # Submitting an application without the CV it promised is worse
            # than not submitting at all.
            return finish(
                ApplyStatus.DIGEST,
                "no tailored CV PDF on disk — refusing to submit without one",
            )

        fields = collect_fields(page)
        filled = _fill_fields(page, fields, config, scored, cv_pdf)
        summary = ", ".join(filled) if filled else "nothing"
        if cv_pdf is None:
            summary += " (no CV PDF to upload)"
        shot = _screenshot(page, art_dir)

        # ------------------------------------------------------------------
        # THE branch. In dry-run mode the function stops here, unconditionally
        # and before any click: the screenshot is the whole deliverable and
        # nothing is submitted. Do not "just also click submit to test it".
        # ------------------------------------------------------------------
        if dry_run:
            assert dry_run, "dry_run must never fall through to submit"
            return finish(
                ApplyStatus.DRY_RUN,
                f"dry run — filled {summary}, not submitted",
                shot,
            )

        # From here on the run is really applying on the user's behalf.
        for field in fields:
            if str(field.get("type", "")).lower() == "checkbox" \
                    and classify_field(field) == "consent":
                try:
                    page.click(field_selector(field))
                except Exception as exc:
                    logger.warning("could not tick consent box: %s", exc)

        submit = _find_submit(page)
        if submit is None:
            return finish(
                ApplyStatus.APPLY_FAILED,
                "no submit button found on the form",
                shot,
            )
        page.click(submit)

        signal = _confirmed(page, timeout_ms)
        if signal is None:
            return finish(
                ApplyStatus.APPLY_FAILED,
                f"clicked submit but saw no confirmation — check manually: {job.url}",
                shot,
            )
        logger.info("applied to %s via %s", job.label, method or "form")
        return finish(
            ApplyStatus.APPLIED,
            f"submitted via {method or 'form'} ({signal})",
            shot,
        )
    except Exception as exc:
        logger.warning("apply failed for %s: %s", job.label, exc)
        return finish(ApplyStatus.APPLY_FAILED, str(exc) or repr(exc))


# --------------------------------------------------------------------------
# stage entry point
# --------------------------------------------------------------------------

#: Statuses an earlier stage already settled — auto-apply leaves them alone.
_SKIP_STATUSES: frozenset[ApplyStatus] = frozenset({
    ApplyStatus.FILTERED,
    ApplyStatus.SCORED_BELOW,
    ApplyStatus.SKIPPED_DUPLICATE,
    ApplyStatus.APPLIED,
})


def _mark(scored: ScoredJob, status: ApplyStatus, detail: str) -> None:
    scored.status = status
    scored.status_detail = detail


def _close(page: Any) -> None:
    try:
        page.close()
    except Exception as exc:
        logger.debug("closing the page failed: %s", exc)


def run(
    scored_jobs: list[ScoredJob],
    config: Any,
    *,
    tracker: Any = None,
    browser: Any = None,
) -> list[ScoredJob]:
    """Auto-apply stage. Returns the same list, with statuses updated.

    Jobs are processed in the order given, up to `apply.max_per_run`;
    everything else — ineligible, over the cap, bailed on, failed — is left
    with `ApplyStatus.DIGEST` and a reason the digest shows verbatim.

    `browser=` is the test seam and only needs `new_page()`. Without it
    Playwright is imported and launched here (and only here); if it is not
    installed the stage degrades to "everything goes to the digest" instead
    of failing the run. Never raises.
    """
    jobs = list(scored_jobs or [])
    if not jobs:
        return jobs

    candidates: list[ScoredJob] = []
    for scored in jobs:
        if scored.status in _SKIP_STATUSES:
            continue
        ok, reason = eligible(scored, config, tracker)
        if not ok:
            _mark(scored, ApplyStatus.DIGEST, reason)
            continue
        candidates.append(scored)

    cap = max(0, _int(_cfg(config, "apply.max_per_run", 5), 5))
    for scored in candidates[cap:]:
        _mark(scored, ApplyStatus.DIGEST,
              f"apply.max_per_run ({cap}) reached this run — apply by hand")
    todo = candidates[:cap]
    if not todo:
        return jobs

    owned_browser: Any = None
    playwright: Any = None
    if browser is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "playwright is not installed — no auto-apply this run "
                "(pip install playwright && playwright install chromium)"
            )
            for scored in todo:
                _mark(scored, ApplyStatus.DIGEST,
                      "playwright is not installed — apply by hand")
            return jobs
        try:
            playwright = sync_playwright().start()
            owned_browser = playwright.chromium.launch(
                headless=bool(_cfg(config, "apply.headless", True))
            )
        except Exception as exc:
            logger.warning("could not start a browser: %s", exc)
            for scored in todo:
                _mark(scored, ApplyStatus.DIGEST,
                      f"could not start a browser ({exc}) — apply by hand")
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            return jobs
        browser = owned_browser

    try:
        for scored in todo:
            try:
                page = browser.new_page()
            except Exception as exc:
                logger.warning("could not open a page for %s: %s",
                               scored.job.label, exc)
                _mark(scored, ApplyStatus.DIGEST,
                      f"could not open a browser page ({exc}) — apply by hand")
                continue
            try:
                outcome = apply_one(scored, config, page=page, tracker=tracker)
                _mark(scored, outcome.status, outcome.detail)
                if outcome.screenshot:
                    scored.artifacts.screenshot = outcome.screenshot
            finally:
                _close(page)   # one page per job, closed whatever happened
    finally:
        # Only tear down what we created; an injected browser belongs to the
        # caller (and to their other tests).
        if owned_browser is not None:
            try:
                owned_browser.close()
            except Exception as exc:
                logger.debug("closing the browser failed: %s", exc)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                logger.debug("stopping playwright failed: %s", exc)

    return jobs
