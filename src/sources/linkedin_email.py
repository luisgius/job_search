"""LinkedIn jobs, read out of LinkedIn's own alert emails via Gmail.

LinkedIn has no public jobs API and actively blocks scraping: automating
`linkedin.com` risks the account you actually need for the job hunt. So this
source never touches LinkedIn's website with your identity. Instead:

  1. you create job alerts in LinkedIn ("daily", by keyword + location);
  2. LinkedIn emails you the results, in HTML, every morning;
  3. this module reads *your own inbox* with the **read-only** Gmail scope
     (`gmail.readonly`) and parses the alert markup into `Job`s.

The only request that ever leaves for linkedin.com is an optional, best-effort
GET of the anonymous `jobs-guest` posting endpoint to fill in a description —
unauthenticated, no cookies, and treated as failure-by-default (see
`fetch_description`). Turn it off with `watchlist.linkedin_email
.fetch_descriptions: false` and the pipeline still works off the email alone.

Two consequences worth knowing downstream:

  * **Freshness is the alert's, not the posting's.** Alert HTML says "2 hours
    ago" in prose that changes format constantly, so every job in a message
    inherits the message's `internalDate`. A daily alert read the same morning
    is accurate to within a day; that is what `freshness.max_age_hours` sees.
  * **The markup changes without warning.** Parsing is deliberately
    forgiving: a job with a title and a URL is emitted even when the company
    and location blocks cannot be found, because a half-known match in the
    digest beats a silently dropped one.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

from ..config import Config
from ..models import Job, ensure_utc
from ..util import get_logger, html_to_text, http_get, parse_datetime

logger = get_logger(__name__)

#: Read-only Gmail. The pipeline must never be able to send, label or delete.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

#: Anonymous posting endpoint — no login, no cookies, breaks periodically.
GUEST_JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
CANONICAL_JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}"

#: Upper bound on guest-endpoint requests per run, so a fat inbox cannot turn
#: into several hundred sequential HTTP calls.
MAX_DESCRIPTION_FETCHES = 60

# /comm/ is the tracking-wrapped variant LinkedIn puts in emails. `&` and `=`
# are excluded from the slug because some alerts wrap the whole posting URL in
# a `click.linkedin.com/?url=…` redirector, where the tracking parameters that
# follow run straight into the id.
_JOB_HREF_RE = re.compile(
    r"linkedin\.com/(?:comm/)?jobs/view/(?P<slug>[^/?#&=\s\"'<>]+)", re.IGNORECASE
)
# Slugs are either a bare id or "senior-backend-engineer-at-acme-3987654321".
_TRAILING_ID_RE = re.compile(r"(\d{6,})\s*$")

_REMOTE_RE = re.compile(r"\b(remote|hybrid|work from home|wfh|anywhere)\b", re.IGNORECASE)

# LinkedIn localises the alert but not its structure, so every filter below
# has to be multilingual or it is not a filter at all: an English-only noise
# list files "Anzeige" as the employer of the job above it. That is worse than
# cosmetic — `Job.key` hashes the company for any posting with no ATS id, so a
# badge in the company slot re-keys the job every day and the tracker offers it
# again each morning.
#
# "2 hours ago", "Just now", "Yesterday", "vor 5 Stunden", "il y a 3 heures".
_AGE_RE = re.compile(
    r"^(?:just now|today|yesterday|gerade eben|heute|gestern|"
    r"aujourd hui|aujourd'hui|hier|hoy|ayer|vandaag|gisteren|"
    r"vor\s+\d+\s*\+?\s*(?:sekunde|minute|stunde|tag|woche|monat|jahr)\w*|"
    r"il y a\s+\d+\s*\+?\s*(?:seconde|minute|heure|jour|semaine|mois|an)\w*|"
    r"hace\s+\d+\s*\+?\s*(?:segundo|minuto|hora|d[ií]a|semana|mes|año|ano)\w*|"
    r"\d+\s*\+?\s*(?:second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago|"
    r"\d+\s*\+?\s*(?:uur|dag|dagen|week|weken|maand|maanden|jaar)\s+geleden)$",
    re.IGNORECASE,
)
# "12 applicants", "48 people clicked apply", "12 Bewerber"
_COUNT_RE = re.compile(
    r"^\d[\d,.]*\s+(?:applicants?|viewers?|people\b|connections?|alumni|"
    r"bewerber\w*|personen|kandidat\w*|candidat\w*|solicitantes?)",
    re.IGNORECASE,
)
# Footer / call-to-action anchors that must never be mistaken for a company.
_CTA_RE = re.compile(
    r"^(?:see all|see more|view (?:all|job|company)|apply|easy apply|unsubscribe|"
    r"help|manage|settings|sign in|download|about|privacy|terms|update your|"
    r"stop receiving|jobs? you may|linkedin|this email|you are receiving"
    # German
    r"|alle (?:jobs|stellen|anzeigen)|jobs? anzeigen|abmelden|abbestellen"
    r"|einstellungen|hilfe|datenschutz|impressum|nutzungsbedingungen"
    r"|jetzt bewerben|diese e[- ]?mail|sie erhalten"
    # French
    r"|voir (?:tout|tous|toutes)|se d[ée]sabonner|postuler|aide|confidentialit[ée]"
    # Spanish / Portuguese
    r"|ver (?:todos|todas)|darse de baja|cancelar|solicitar empleo|ayuda"
    r"|ver todas as|cancelar subscri[çc][ãa]o"
    # Dutch
    r"|alle vacatures|bekijk alle|afmelden|solliciteer|instellingen)",
    re.IGNORECASE,
)
_NOISE_WORDS = {
    "promoted", "new", "actively recruiting", "be an early applicant",
    "easy apply", "your job alert", "job alert",
    # German
    "anzeige", "gesponsert", "beworben", "neu", "einfache bewerbung",
    "wird aktiv rekrutiert", "ihr job-alert", "dein job-alert", "jobbenachrichtigung",
    # French
    "sponsorise", "sponsorisé", "nouveau", "candidature simplifiee",
    "candidature simplifiée", "recrute activement",
    # Spanish / Portuguese
    "promocionado", "patrocinado", "nuevo", "novo", "solicitud sencilla",
    # Dutch
    "gepromoot", "gesponsord", "nieuw", "eenvoudig solliciteren",
}
# Separators LinkedIn sprinkles between fields.
_SEPARATORS = " \t\r\n·•|-–—:,"
# A company or a location is short. Anything long is footer prose.
_MAX_FIELD_CHARS = 80
# How far past a job link to look for its company/location before giving up.
_CONTEXT_LOOKAHEAD = 6


# --------------------------------------------------------------------------
# urls
# --------------------------------------------------------------------------


def _job_id_from_url(url: str) -> str | None:
    """Pull the numeric LinkedIn job id out of any of its URL shapes.

    Some alerts route every link through `click.linkedin.com/r/?url=<posting>`,
    and the wrapped URL is percent-encoded about half the time. Un-escaping
    first is what makes both shapes yield the same id — and because one email
    uses one link style throughout, getting this wrong drops *every* card in
    that alert, silently.
    """
    text = str(url or "")
    match = _JOB_HREF_RE.search(text) or _JOB_HREF_RE.search(unquote(text))
    if not match:
        return None
    slug = match.group("slug")
    if slug.isdigit():
        return slug
    trailing = _TRAILING_ID_RE.search(slug)
    return trailing.group(1) if trailing else None


def canonical_linkedin_url(url: str) -> str:
    """Strip LinkedIn's tracking cruft down to a stable, shareable job URL.

    `.../comm/jobs/view/123/?refId=x&trackingId=y` -> `.../jobs/view/123`.
    The tracking params encode *which email* the click came from, so leaving
    them in would make the same posting look different every day and break
    de-duplication against the same job seen on an ATS board.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    job_id = _job_id_from_url(text)
    if job_id:
        return CANONICAL_JOB_URL.format(job_id=job_id)
    # Not a job link (a company page, a search) — still drop query/fragment
    # and the /comm/ tracking prefix so the URL is at least stable.
    cleaned = text.split("#", 1)[0].split("?", 1)[0]
    return cleaned.replace("/comm/", "/").rstrip("/")


# --------------------------------------------------------------------------
# HTML extraction
# --------------------------------------------------------------------------


class _AlertParser(HTMLParser):
    """Flatten alert HTML into an ordered token stream.

    stdlib only, on purpose: `bs4` is not a dependency and a regex-only pass
    cannot tell "the text right after this anchor" from "any text anywhere".
    Tokens are `("job", (id, href, title))`, `("link", text)` or
    `("text", text)`, which is exactly the ordering information needed to
    attach a company and a location to the job link above them.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, Any]] = []
        self._in_anchor = False
        self._href = ""
        self._chunks: list[str] = []
        self._skip_depth = 0

    # -- stdlib hooks ---------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip_depth += 1
            return
        if tag == "a":
            if self._in_anchor:  # unclosed <a>: emit what we have, start over
                self._close_anchor()
            self._in_anchor = True
            self._href = dict(attrs).get("href") or ""
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._in_anchor:
            self._close_anchor()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_anchor:
            self._chunks.append(data)
            return
        text = _clean(data)
        if text:
            self.tokens.append(("text", text))

    def close(self) -> None:
        super().close()
        if self._in_anchor:
            self._close_anchor()

    # -- internals ------------------------------------------------------

    def _close_anchor(self) -> None:
        text = _clean(" ".join(self._chunks))
        href = self._href
        self._in_anchor = False
        self._href = ""
        self._chunks = []
        job_id = _job_id_from_url(href)
        if job_id:
            self.tokens.append(("job", (job_id, href, text)))
        elif text:
            self.tokens.append(("link", text))


def _clean(value: str) -> str:
    """Collapse whitespace (including nbsp) in a text node."""
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _is_noise(text: str) -> bool:
    """True for separators, timestamps, badges and footer prose."""
    stripped = text.strip(_SEPARATORS)
    if not stripped or len(stripped) > _MAX_FIELD_CHARS:
        return True
    low = stripped.lower()
    if low in _NOISE_WORDS or _AGE_RE.match(low) or _COUNT_RE.match(low):
        return True
    if _CTA_RE.match(low):
        return True
    # Addresses and links in the footer are never a company name.
    return "@" in stripped or "http" in low


def _context_fields(tokens: list[tuple[str, Any]], index: int) -> tuple[str, str]:
    """Company + location for the job link at `tokens[index]`.

    LinkedIn renders each card as *title link, company, location, age*, but
    which of those are text nodes, table cells or their own anchors changes
    between template revisions — so this walks forward over whatever comes
    next, drops the noise, and takes the first two survivors. It stops at the
    next job link (that card's fields belong to it) or after a few tokens, so
    the last card in an email cannot adopt the footer as its employer.
    """
    pieces: list[str] = []
    looked = 0
    for kind, value in tokens[index + 1:]:
        if kind == "job":
            break
        looked += 1
        if looked > _CONTEXT_LOOKAHEAD:
            break
        # A company name is sometimes its own anchor to the company page, so
        # link text counts — the CTA/footer filter in `_is_noise` is what
        # keeps "See all jobs" out.
        for piece in str(value).split("·"):
            candidate = piece.strip(_SEPARATORS)
            if candidate and not _is_noise(candidate):
                pieces.append(candidate)
        if len(pieces) >= 2:
            break
    company = pieces[0] if pieces else ""
    location = pieces[1] if len(pieces) > 1 else ""
    return company, location


def extract_jobs_from_html(
    html: str, received_at: datetime | None = None
) -> list[Job]:
    """Parse one alert email body into `Job`s.

    Every job inherits `received_at` as its `posted_at` — the alert's own
    freshness (see the module docstring). Jobs are de-duplicated by LinkedIn
    id, since LinkedIn repeats the same posting further down the same email,
    and a later, richer copy backfills a company/location the first one lacked.
    """
    parser = _AlertParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception as exc:  # malformed markup must not kill the message
        logger.debug("linkedin_email: HTML parse aborted: %s", exc)

    posted_at = ensure_utc(received_at)
    tokens = parser.tokens
    jobs: dict[str, Job] = {}

    for index, (kind, value) in enumerate(tokens):
        if kind != "job":
            continue
        job_id, href, title = value
        if not title:
            # An icon/logo anchor pointing at the same job. The title lives on
            # the text anchor, so drop this one rather than invent a heading.
            logger.debug("linkedin_email: job %s link has no anchor text", job_id)
            continue

        existing = jobs.get(job_id)
        company, location = _context_fields(tokens, index)
        if existing is not None:
            if not existing.company and company:
                existing.company = company
            if not existing.location and location:
                existing.location = location
            continue

        if not company:
            # Emitting it anyway is deliberate: filters and scoring can still
            # judge a title, and a dropped job is invisible forever.
            logger.debug(
                "linkedin_email: job %s (%r) has no company block", job_id, title
            )

        jobs[job_id] = Job(
            source="linkedin_email",
            company=company,
            title=title,
            url=canonical_linkedin_url(href),
            location=location,
            description="",
            posted_at=posted_at,
            remote=True if _REMOTE_RE.search(f"{location} {title}") else None,
            # Aggregated alert: the posting lives behind LinkedIn, and which
            # ATS it redirects to is a later stage's problem.
            ats=None,
            ats_job_id=None,
            raw={"linkedin_job_id": job_id},
        )

    return list(jobs.values())


# --------------------------------------------------------------------------
# Gmail message -> jobs
# --------------------------------------------------------------------------


def _decode_body(data: Any) -> str:
    """base64url-decode a Gmail `body.data` blob.

    Gmail uses the URL-safe alphabet ('-' and '_') and strips the '=' padding,
    which `urlsafe_b64decode` refuses — hence the manual re-pad.
    """
    text = str(data or "")
    if not text:
        return ""
    text += "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text.encode("ascii", "ignore")).decode(
            "utf-8", "replace"
        )
    except Exception as exc:
        logger.debug("linkedin_email: undecodable body part: %s", exc)
        return ""


def _part_body(node: Any, mime: str) -> str:
    """Depth-first search of the MIME tree for the first `mime` part's text."""
    if not isinstance(node, Mapping):
        return ""
    if str(node.get("mimeType") or "").strip().lower().startswith(mime):
        text = _decode_body((node.get("body") or {}).get("data"))
        if text:
            return text
    for part in node.get("parts") or []:
        found = _part_body(part, mime)
        if found:
            return found
    return ""


def parse_message(payload: Mapping[str, Any]) -> list[Job]:
    """Turn one full Gmail message dict into `Job`s.

    Prefers the `text/html` part (the alert's real content) anywhere in the
    MIME tree and falls back to `text/plain`. `internalDate` — Gmail's own
    millisecond-epoch receipt time — becomes every job's `posted_at`.
    """
    if not isinstance(payload, Mapping):
        return []

    received_at = parse_datetime(payload.get("internalDate"))
    body = payload.get("payload")
    html = _part_body(body, "text/html")
    if not html:
        html = _part_body(body, "text/plain")
        if html:
            logger.debug(
                "linkedin_email: message %s has no HTML part, using text/plain",
                payload.get("id"),
            )
    if not html:
        logger.debug("linkedin_email: message %s has no readable body", payload.get("id"))
        return []

    return extract_jobs_from_html(html, received_at=received_at)


# --------------------------------------------------------------------------
# descriptions (best effort)
# --------------------------------------------------------------------------


def fetch_description(url: str, *, session: Any = None) -> str:
    """Fetch a posting's text from LinkedIn's anonymous guest endpoint.

    Best effort by design: the endpoint is undocumented, rate-limits hard and
    404s for expired postings, so **every** failure returns `""` and logs at
    debug. The job still reaches the digest with its email-derived fields.
    """
    job_id = _job_id_from_url(url)
    if not job_id:
        logger.debug("linkedin_email: no job id in %r, skipping description", url)
        return ""
    try:
        # One attempt only: this endpoint answers or it does not, and retries
        # against a rate-limiter are how you get blocked.
        response = http_get(
            GUEST_JOB_URL.format(job_id=job_id),
            session=session,
            retries=1,
            timeout=15,
        )
        return html_to_text(getattr(response, "text", "") or "")
    except Exception as exc:
        logger.debug("linkedin_email: guest description for %s failed: %s", job_id, exc)
        return ""


# --------------------------------------------------------------------------
# Gmail service
# --------------------------------------------------------------------------


def _settings(config: Config) -> dict[str, Any]:
    section = config.watchlist.get("linkedin_email")
    return dict(section) if isinstance(section, Mapping) else {}


def _resolve(config: Config, value: Any, default: str) -> Path:
    path = Path(str(value or default))
    return path if path.is_absolute() else (config.root / path)


def _missing_credentials(creds_path: Path) -> RuntimeError:
    return RuntimeError(
        f"Gmail OAuth credentials not found at {creds_path}. Create an OAuth "
        "client of type 'Desktop app' in the Google Cloud Console, download the "
        "JSON, and save it there (or point "
        "watchlist.linkedin_email.credentials_file at it)."
    )


def build_service(config: Config) -> Any:
    """Build an authorised, read-only Gmail client.

    Reuses `token_file` when it is still valid, refreshes it when it has
    merely expired, and only falls back to the browser consent flow when
    there is nothing usable left. The token is written back so the daily run
    is non-interactive after the first time.

    Raises `RuntimeError` (naming the file) when OAuth credentials are needed
    and `credentials_file` is not there — the one failure a user must fix by
    hand, so it must not be swallowed into a log line.
    """
    settings = _settings(config)
    token_path = _resolve(config, settings.get("token_file"), "gmail_token.json")
    creds_path = _resolve(config, settings.get("credentials_file"), "gmail_credentials.json")

    # Checked before the google imports on purpose: with no token *and* no
    # client secrets there is nothing any library could do, and "install
    # google-api-python-client" would be the wrong thing to tell the user.
    if not token_path.exists() and not creds_path.exists():
        raise _missing_credentials(creds_path)

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Any = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            # A corrupt//revoked token is recoverable: re-run the consent flow.
            logger.warning("Gmail token at %s is unusable (%s), re-authorising", token_path, exc)
            creds = None

    if creds is not None and not getattr(creds, "valid", False):
        if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            try:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
            except Exception as exc:
                logger.warning("Gmail token refresh failed (%s), re-authorising", exc)
                creds = None
        else:
            creds = None

    if creds is None or not getattr(creds, "valid", False):
        if not creds_path.exists():
            raise _missing_credentials(creds_path)
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as exc:
            # Losing the token only costs another consent prompt tomorrow.
            logger.warning("could not save Gmail token to %s: %s", token_path, exc)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------
# source entry point
# --------------------------------------------------------------------------


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def fetch(
    config: Config,
    *,
    service: Any = None,
    errors: list[str] | None = None,
    session: Any = None,
) -> list[Job]:
    """Read LinkedIn job-alert emails and return the jobs they advertise.

    `service=` is the seam: pass a Gmail-shaped object and nothing is built,
    authorised or sent. `session=` is the same seam for the optional
    description fetch (`watchlist.linkedin_email.fetch_descriptions`), so a
    caller can run this source with no network at all.

    Never raises: a missing token, a Gmail outage or one unreadable message
    costs those jobs and is reported through `errors`.
    """
    if not config.source_enabled("linkedin_email"):
        logger.debug("linkedin_email disabled in config.sources, skipping")
        return []

    settings = _settings(config)
    query = str(
        settings.get("gmail_query")
        or "from:jobalerts-noreply@linkedin.com newer_than:2d"
    )
    try:
        max_messages = int(settings.get("max_messages") or 25)
    except (TypeError, ValueError):
        max_messages = 25
    max_messages = max(1, max_messages)

    if service is None:
        try:
            service = build_service(config)
        except Exception as exc:
            _report(f"linkedin_email: Gmail auth failed: {exc}", errors)
            return []

    try:
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_messages)
            .execute()
        )
    except Exception as exc:
        _report(f"linkedin_email: Gmail search failed: {exc}", errors)
        return []

    messages = (listing or {}).get("messages") or []
    if not messages:
        logger.info("linkedin_email: no messages match %r", query)
        return []

    jobs: list[Job] = []
    seen: set[str] = set()
    for meta in messages[:max_messages]:
        message_id = (meta or {}).get("id") if isinstance(meta, Mapping) else None
        if not message_id:
            continue
        try:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except Exception as exc:
            logger.warning("linkedin_email: could not read message %s: %s", message_id, exc)
            continue
        try:
            found = parse_message(full or {})
        except Exception as exc:  # a template change must not abort the source
            logger.warning("linkedin_email: could not parse message %s: %s", message_id, exc)
            continue

        for job in found:
            # LinkedIn repeats the same posting across consecutive daily
            # alerts; the first (oldest-listed) copy wins.
            ident = str(job.raw.get("linkedin_job_id") or "") or job.dedupe_key
            if ident in seen:
                continue
            seen.add(ident)
            jobs.append(job)

    logger.info(
        "linkedin_email: %d job(s) from %d message(s)", len(jobs), len(messages[:max_messages])
    )

    if settings.get("fetch_descriptions", True):
        _attach_descriptions(jobs, session=session)
    return jobs


def _attach_descriptions(jobs: list[Job], *, session: Any = None) -> None:
    """Fill in descriptions from the guest endpoint, quietly and in-place.

    Capped at `MAX_DESCRIPTION_FETCHES`: alert emails can easily add up to
    hundreds of jobs, and each miss costs a request against an endpoint that
    rate-limits.
    """
    attempted = 0
    filled = 0
    for job in jobs:
        if attempted >= MAX_DESCRIPTION_FETCHES:
            logger.info(
                "linkedin_email: description fetch capped at %d, %d job(s) left "
                "with email-only detail",
                MAX_DESCRIPTION_FETCHES, len(jobs) - attempted,
            )
            break
        attempted += 1
        text = fetch_description(job.url, session=session)
        if text:
            job.description = text
            filled += 1
    if attempted:
        logger.info("linkedin_email: fetched %d/%d descriptions", filled, attempted)
