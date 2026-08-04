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

#: `<input>` types that are NOT a box you type an answer into. Anything else
#: (text, url, tel, email, number, date, ...) is free text, and `inspect_form`
#: holds free-text boxes to a higher standard than the rest.
NON_TEXT_INPUT_TYPES: frozenset[str] = frozenset({"file", "checkbox", "radio"})

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

#: Screener vocabulary in the languages an EU-focused search actually meets.
#: An English-only trigger list is not a smaller safety net, it is a hole: the
#: German Greenhouse form that asks "Gehaltsvorstellung" looked, to the
#: classifier, exactly like a form that asked nothing at all.
#:
#: Matched against `_field_text`, which is accent-folded, so "Kündigungsfrist"
#: arrives as "kuendigungsfrist"... except that folding gives "kundigungsfrist"
#: (ü -> u), so both spellings are listed.
_NON_ENGLISH_QUESTION_TERMS: tuple[str, ...] = (
    # German
    r"warum", r"weshalb", r"wieso", r"beschreiben", r"erzahlen", r"erzaehlen",
    # German puts the head noun LAST, so a \b-anchored "gehalt" catches
    # "Gehaltsvorstellung" and misses "Wunschgehalt", "Jahresgehalt",
    # "Zielgehalt", "Bruttojahresgehalt". Allow the compound prefix too.
    r"\w*gehalt\w*", r"\w*verguetung\w*", r"\w*vergutung\w*",
    r"\w*kundigungsfrist\w*", r"\w*kuendigungsfrist\w*",
    r"\w*eintritt\w*", r"\w*verfugbar\w*", r"\w*verfuegbar\w*",
    r"wie ?viel\w* jahre", r"\w*erfahrung",
    # Polish / Swedish / Danish salary fields, honestly outside the six
    # languages first claimed but just as common on EU boards.
    r"\w*wynagrodzeni\w*", r"oczekiwania", r"okres wypowiedzenia",
    r"\w*loneanspr\w*", r"\w*lonekrav\w*", r"\w*loneonske\w*",
    r"\w*opsigelsesvarsel\w*",
    r"kundigungsfrist_legacy",
    r"kuendigungsfrist", r"eintrittstermin", r"eintrittsdatum",
    r"verfugbar\w*", r"verfuegbar\w*", r"berufserfahrung", r"aufmerksam geworden",
    r"arbeitserlaubnis", r"visum", r"staatsangehorigkeit", r"geschlecht",
    r"schwerbehind\w*",
    # French
    r"pourquoi", r"decrivez", r"decrire", r"pretentions?", r"remuneration",
    r"salariales?", r"preavis", r"disponibilite", r"annees d experience",
    r"autorisation de travail", r"nationalite",
    # Spanish / Portuguese
    r"por que", r"porque", r"describe", r"descreva", r"expectativas?",
    r"salarial\w*", r"pretensao", r"preaviso", r"disponibilidad\w*",
    r"anos de experiencia", r"permiso de trabajo", r"nacionalidad",
    # Dutch
    r"waarom", r"beschrijf", r"salaris\w*", r"opzegtermijn", r"beschikbaar\w*",
    r"werkvergunning",
    # Italian
    r"perche", r"descrivi", r"retribuzione", r"preavviso", r"disponibilita",
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
    ) + tuple(rf"\b{term}\b" for term in _NON_ENGLISH_QUESTION_TERMS)
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


def _tracker_flag(tracker: Any, method: str, *args: Any) -> bool | None:
    """Ask an optional tracker method. `None` means "it could not answer".

    The tracker is duck-typed — the pipeline injects a real `db.Tracker`, but
    callers and tests pass partial stand-ins — so a gate the object simply
    does not implement degrades to False rather than crashing the stage. A
    method that exists and *raises* is a different thing entirely: the answer
    is unknown, and unknown in front of a submit click is not a yes.
    """
    func = getattr(tracker, method, None)
    if not callable(func):
        return False
    try:
        return bool(func(*args))
    except Exception as exc:
        logger.warning("tracker.%s failed: %s", method, exc)
        return None


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

    # A job the scorer could not judge is not a job that scored badly. When
    # the LLM call fails, scoring emits `Score(value=0, error=...)` and routes
    # the job to the digest *because a human has to look at it*. Comparing
    # only the number turns an API outage into a run that auto-applies to
    # everything it never assessed the moment `apply.min_score` is 0 — which
    # is exactly how someone writes "apply to everything you can".
    score = scored.score
    if score is None:
        return False, "no score — auto-apply never acts on an unjudged job"
    if score.error:
        return False, (
            f"the scorer could not judge this job ({score.error}) — "
            "a human decides this one"
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

        # A submit click that never came back (see `apply_one`). The outcome
        # was recorded `apply_failed`, which deliberately does not block — but
        # the POST may well have landed, so the *click* blocks even when the
        # outcome did not.
        attempted = _tracker_flag(tracker, "submit_attempted", job.key)
        if attempted is None:
            return False, "could not verify apply history — not applying"
        if attempted:
            return False, (
                "submit was already clicked for this job and the result could "
                f"not be confirmed — check it by hand: {job.url}"
            )

        # The same role re-posted under a new requisition id is a new
        # `Job.key` but the same `dedupe_key`.
        similar = _tracker_flag(tracker, "has_applied_similar", job.dedupe_key)
        if similar is None:
            return False, "could not verify apply history — not applying"
        if similar:
            return False, (
                "already applied to this role at this company (re-posted under "
                "a new id)"
            )

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

        # A free-text box with no readable label AND nothing recognisable in
        # its name is how Greenhouse renders every custom question: the
        # wording lives in a sibling `<label for=...>`, the input itself
        # carries no aria-label, no placeholder and no `required` (custom
        # questions are validated client-side). Reading only the element's own
        # attributes, "Why do you want to work at Acme?" arrives here as an
        # anonymous optional text box — and submitting leaves it blank.
        #
        # So an unreadable free-text box counts as a question we cannot see.
        # The cost is a bail on the rare genuinely anonymous optional input
        # (one click); the alternative is filing an application that visibly
        # ignored the one thing the employer asked.
        if (tag == "input" and ftype not in NON_TEXT_INPUT_TYPES
                and kind == "unknown"
                and not str(field.get("label") or "").strip()):
            return False, (
                f"free-text field {what} has no label this bot can read — its "
                "question is in markup we cannot see, so it would be submitted "
                "blank"
            )

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


def _unfillable_required(
    fields: list[dict[str, Any]],
    config: Any,
    scored: ScoredJob,
    cv_pdf: str | None,
) -> list[str]:
    """Required fields we recognise but have no value for.

    `inspect_form` bails on required fields it cannot *classify*; this is the
    other half. A phone field is recognised, so it sails through inspection —
    but plenty of people leave `applicant.phone` empty and plenty of German
    boards mark it required, and `_fill_fields` simply skips a kind with no
    value. Without this the run clicks submit on a form it knowingly left
    incomplete, which is a worse first impression than not applying at all.
    """
    values = _values_for(config, scored)
    missing: list[str] = []
    for field in fields:
        if not bool(field.get("required")):
            continue
        kind = classify_field(field)
        if kind == "resume":
            if not cv_pdf:
                missing.append(_describe(field))
            continue
        # consent is ticked, not typed; unknown already bailed in inspection;
        # a cover letter is optional by the definition of its category.
        if kind in ("consent", "unknown", "cover_letter_optional"):
            continue
        if not values.get(kind, "").strip():
            missing.append(f"{_describe(field)} — applicant.{kind} is empty")
    return missing


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


def _confirmation_selector(page: Any, timeout_ms: int,
                           before: frozenset[str] = frozenset()) -> str | None:
    """First confirmation selector that resolves — and was not already there.

    `before` is the set of these selectors that matched BEFORE the submit
    click. Without it this is the same hole the text check already closed, one
    door along: `.confirmation` is an ordinary class name, and Playwright's
    `text=` is a case-insensitive substring search over the whole page, so a
    posting whose own ad says "Thank you for your interest" was recorded as a
    submitted application that was never sent — and then blocked forever.
    """
    budget = max(1000, timeout_ms // max(1, len(CONFIRMATION_SELECTORS)))
    for selector in CONFIRMATION_SELECTORS:
        if selector in before:
            continue
        try:
            if page.wait_for_selector(selector, timeout=budget) is not None:
                return selector
        except Exception:
            continue
    return None


def _confirmation_selectors_present(page: Any) -> frozenset[str]:
    """Which confirmation selectors already match. Cheap, no waiting."""
    found = set()
    for selector in CONFIRMATION_SELECTORS:
        try:
            if page.query_selector_all(selector):
                found.add(selector)
        except Exception:
            continue
        try:
            if selector.startswith("text=") and selector[5:].lower() in _page_text(page).lower():
                found.add(selector)
        except Exception:
            continue
    return frozenset(found)


def _page_text(page: Any) -> str | None:
    """`page.content()`, or None when the page cannot be read at all.

    The None is load-bearing: "the page says nothing" and "there is no page
    left to ask" lead to opposite decisions after a submit click.
    """
    try:
        return str(page.content() or "")
    except Exception:
        return None


def _confirmation_text(content: str | None, before: str | None) -> str | None:
    """Confirmation phrase that appeared *because of* the submit, or None.

    `before` is the page text captured before the click, and it is what makes
    this fallback mean anything. A plain substring search over the whole page
    matches a posting whose own ad says "thanks for applying" — and a false
    confirmation is the worst outcome in this module: the run records APPLIED
    for an application that was never sent, and the tracker then blocks the
    real one forever.
    """
    if content is None:
        return None
    lowered = content.lower()
    prior = str(before or "").lower()
    for phrase in CONFIRMATION_TEXTS:
        if phrase in lowered and phrase not in prior:
            return phrase
    return None


def _note_submit_attempt(
    tracker: Any, scored: ScoredJob, *, method: str, now: datetime
) -> None:
    """Record the intent to click submit. Best effort, never fatal.

    A tracker that cannot take this note still gets the outcome row, and the
    orphan file in `_record` remains the last line of defence — refusing to
    apply because of a bookkeeping failure would cost the user the
    application every time their DB is momentarily locked.
    """
    record = getattr(tracker, "record_submit_attempt", None)
    if not callable(record):
        return
    try:
        record(scored.job.key, url=scored.job.url, method=method, now=now)
    except Exception as exc:
        logger.warning(
            "could not write the pre-submit record for %s (%s) — a crash from "
            "here on would be invisible to the next run",
            scored.job.key, exc,
        )


def _clear_submit_attempt(tracker: Any, scored: ScoredJob) -> None:
    """Undo `_note_submit_attempt` once we know nothing was sent."""
    clear = getattr(tracker, "clear_submit_attempt", None)
    if not callable(clear):
        return
    try:
        clear(scored.job.key)
    except Exception as exc:
        logger.debug("could not clear the pre-submit record for %s: %s",
                     scored.job.key, exc)


def _write_orphan_record(
    scored: ScoredJob,
    artifacts_dir: Path | None,
    method: str,
    now: datetime,
    error: Exception,
) -> None:
    """Last-resort durable note that an application was really sent.

    The tracker is the system of record; when writing to it fails, the fact
    that a human sent an application must not evaporate with the process.
    """
    try:
        target = Path(artifacts_dir) if artifacts_dir else Path.cwd()
        ensure_dir(target)
        (target / "APPLIED_BUT_UNRECORDED.txt").write_text(
            f"An application WAS submitted and the tracker write failed.\n\n"
            f"when:    {now.isoformat()}\n"
            f"job:     {scored.job.label}\n"
            f"url:     {scored.job.url}\n"
            f"key:     {scored.job.key}\n"
            f"method:  {method}\n"
            f"error:   {error}\n\n"
            "Record this by hand before the next run, or it will be sent again.\n",
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - nothing left to try
        logger.error("could not even write the orphan record: %s", exc)


def _form_still_present(page: Any) -> bool:
    """Are there still form fields on screen after clicking submit?

    Greenhouse and Lever both replace the form with a confirmation page that
    has no application fields, so fields still being there means we are still
    inside the flow — either validation rejected the submission, or that click
    was a "Next" on a multi-page form and the screener is on page two. Either
    way nothing was sent, and the job must stay eligible.

    If the page cannot be read at all the honest answer is "no idea", and this
    returns False so the caller takes the cautious branch and blocks a retry.
    """
    try:
        # A resume upload, or a submit control, is evidence of the application
        # form. Any-input-at-all was too broad: a search box or a cookie
        # checkbox on a localized confirmation page read as "still on the
        # form", so a real submission was recorded as never sent and applied
        # to again the next day.
        for field in collect_fields(page):
            if str(field.get("type", "")).lower() == "file":
                return True
            # A required field still on screen means the flow is not finished:
            # either validation rejected the submission, or this is page two
            # of a multi-page form. A confirmation page asks for nothing.
            if field.get("required"):
                return True
        for selector in SUBMIT_SELECTORS:
            try:
                if page.query_selector_all(selector):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


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
        if outcome.status in (ApplyStatus.APPLIED,
                              ApplyStatus.SUBMITTED_UNCONFIRMED):
            # An application really was sent and the outcome row just failed to
            # save. The pre-submit record written before the click is what
            # normally stops tomorrow's run repeating it, but that is a
            # different table and a different write, so it may be missing too.
            # Shout, and leave a file behind that survives the process.
            logger.error(
                "APPLIED to %s but could not record the outcome (%s) — the "
                "pre-submit record should still block a repeat; recording it "
                "on disk as well.",
                scored.job.url, exc,
            )
            _write_orphan_record(scored, artifacts_dir, method, now, exc)
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
        missing = _unfillable_required(fields, config, scored, cv_pdf)
        if missing:
            # Checked here rather than before the dry-run branch on purpose: a
            # dry run submits nothing, so an incomplete form is still a useful
            # screenshot. Only the real submit has to refuse.
            return finish(
                ApplyStatus.DIGEST,
                "the form requires " + "; ".join(missing)
                + " — refusing to submit it incomplete, apply by hand: "
                + job.url,
                shot,
            )

        for field in fields:
            if str(field.get("type", "")).lower() == "checkbox" \
                    and classify_field(field) == "consent":
                try:
                    page.click(field_selector(field))
                except Exception as exc:
                    logger.warning("could not tick consent box: %s", exc)

        try:
            before_submit = str(page.content() or "")
        except Exception:
            before_submit = ""
        # Both confirmation channels are compared against the pre-click page.
        # A signal that was already on screen is not evidence of anything.
        before_selectors = _confirmation_selectors_present(page)

        submit = _find_submit(page)
        if submit is None:
            return finish(
                ApplyStatus.APPLY_FAILED,
                "no submit button found on the form",
                shot,
            )
        # Write down that a submit is about to happen, BEFORE it happens.
        # Everything after the click can fail — the tab can die, the page can
        # become unreadable — and the outcome row is written afterwards, so
        # without this a crashed submit leaves only `apply_failed` behind and
        # tomorrow's run cheerfully sends a second application. Cleared again
        # below, but only on positive evidence that nothing was sent.
        _note_submit_attempt(tracker, scored, method=method, now=now)
        page.click(submit)

        selector_signal = _confirmation_selector(page, timeout_ms, before_selectors)
        after = _page_text(page)
        signal = selector_signal or _confirmation_text(after, before_submit)
        still_a_form = _form_still_present(page)

        if signal is not None and selector_signal is None and still_a_form:
            # A multi-step form acknowledges step 1 — "Thanks for applying!
            # Just a few more questions." — while the application is still
            # sitting unsent on page 2. The words alone cannot tell that from
            # a real confirmation, so the text fallback additionally requires
            # the form to be *gone*: Greenhouse and Lever both replace it on
            # success. Believing the text here records a terminal `applied`
            # for an application that was never sent, and the user can then
            # never file the real one through this tool.
            signal = None

        if signal is None:
            # Two very different situations look identical from here, and
            # conflating them is expensive in both directions.
            if still_a_form:
                # The form is still on screen: client-side validation rejected
                # the submission and nothing left the machine. The job must
                # stay eligible, or a fixable problem costs the application.
                if after is not None:
                    # ...but only erase the write-ahead record when the page
                    # actually answered us. A page that cannot be read at all
                    # is not evidence of anything, whatever a stale DOM query
                    # still says.
                    _clear_submit_attempt(tracker, scored)
                return finish(
                    ApplyStatus.APPLY_FAILED,
                    "submit was rejected by the form and nothing was sent — "
                    f"check manually: {job.url}",
                    shot,
                )
            # The page is gone or unreadable. The POST may well have landed.
            # Blocking a possible duplicate beats sending one.
            return finish(
                ApplyStatus.SUBMITTED_UNCONFIRMED,
                "clicked submit but could not read a confirmation — treated as "
                f"sent so it is never sent twice. CHECK MANUALLY: {job.url}",
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
