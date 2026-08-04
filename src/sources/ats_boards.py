"""Greenhouse + Lever public board APIs — the cheapest, highest-signal source.

Both vendors expose an unauthenticated JSON endpoint per company ("slug"), so
there are no keys, no scraping and no rate-limit games; better still, every
posting carries a stable ATS id, which is what keeps `Job.key` from drifting
when a company edits a title.

The price is that slugs rot silently: a company renames its board and the
pipeline just starts returning zero jobs for it forever. That is what the
`--check` CLI is for::

    python -m src.sources.ats_boards --check greenhouse spotify
    python -m src.sources.ats_boards --check-all

Nothing in here raises out of `fetch()` — one dead board must never take the
whole run down with it.
"""

from __future__ import annotations

import argparse
import html as html_module
import json as json_lib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..config import Config, ConfigError
from ..models import Job
from ..util import (
    get_logger,
    html_to_text,
    http_get_json,
    parse_datetime,
    setup_logging,
    truncate,
)

logger = get_logger(__name__)

GREENHOUSE_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/{slug}"

#: Boards this module knows how to talk to, in watchlist order.
BOARDS: tuple[str, ...] = ("greenhouse", "lever")

# Project root, so `--check` works from any working directory.
_ROOT = Path(__file__).resolve().parents[2]

_REMOTE_RE = re.compile(
    r"\b(remote(?:ly)?|work from home|wfh|home[- ]office|telecommut\w*|anywhere)\b",
    re.IGNORECASE,
)
# Greenhouse boards routinely carry placeholder offices; they are noise, not
# locations, and would confuse the geo filter downstream.
_PLACEHOLDER_LOCATIONS = {"", "n/a", "na", "none", "no office", "unknown", "-"}

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_STATUS_HINTS: dict[int, str] = {
    401: "board requires authentication",
    403: "board refused the request (blocked or private)",
    404: "slug not found",
    410: "board no longer exists",
    429: "rate limited — try again later",
}

# How long the joined location field may get when a posting lists many offices.
#
# A *count* cap used to live here, and it cost real jobs: US companies list
# their offices home-first — SF, NYC, Austin, Seattle, then Berlin — so cutting
# at the fourth left a string that reads unambiguously American, and the one
# posting genuinely open in Berlin was rejected as a US role. This field is not
# decoration; it is the entire input to the geo filter, so anything dropped
# here deletes a job invisibly.
#
# The remaining cap is a sanity bound on a pathological payload rather than a
# tidiness rule, and it is generous enough for every real multi-office posting
# (~20 offices). A posting with more than that can still lose its last office —
# the alternative is an unbounded string in the digest, which is the cheaper
# failure of the two.
_MAX_LOCATION_CHARS = 600


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------


def _clean_slug(value: Any) -> str:
    """Normalise a watchlist slug, tolerating a pasted board URL.

    People copy `https://boards.greenhouse.io/spotify` out of the browser far
    more often than they type `spotify`, and a wrong slug is indistinguishable
    from an empty board, so it is worth fixing here rather than debugging later.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text or text.startswith("www.") or ".io/" in text or ".co/" in text:
        path = re.sub(r"^[a-z]+://", "", text)
        parts = [p for p in path.split("/") if p]
        # Drop the host, keep the first path segment (the slug).
        if parts and ("." in parts[0]):
            parts = parts[1:]
        text = parts[0] if parts else ""
    return text.strip().strip("/").strip()


def company_from_slug(slug: str) -> str:
    """Guess a display company name from a board slug ("acme-corp" -> "Acme Corp").

    Neither the Greenhouse nor the Lever payload contains a company field, so
    this heuristic is the default. Callers override it by writing the watchlist
    entry as ``{slug: acme-corp, company: ACME Corporation}``.
    """
    cleaned = _clean_slug(slug).replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else str(slug or "").strip()


def _mentions_remote(*values: Any) -> bool:
    """True when any of `values` looks like a remote-work marker."""
    for value in values:
        if value and _REMOTE_RE.search(str(value)):
            return True
    return False


def _clean_location(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDER_LOCATIONS else text


def _join_locations(values: Iterable[Any]) -> str:
    """De-duplicate and join location strings, bounded by `_MAX_LOCATION_CHARS`.

    Every distinct office is kept: the geo filter reads this string, and a
    truncated office list is how a role open in Berlin gets rejected as
    American. Only an absurdly long list is cut, and always on a whole entry.
    """
    seen: list[str] = []
    for value in values:
        text = _clean_location(value)
        if text and text.lower() not in {s.lower() for s in seen}:
            seen.append(text)

    kept: list[str] = []
    length = 0
    for text in seen:
        extra = len(text) + (2 if kept else 0)
        if kept and length + extra > _MAX_LOCATION_CHARS:
            logger.debug(
                "location list truncated at %d of %d entries (over %d chars)",
                len(kept), len(seen), _MAX_LOCATION_CHARS,
            )
            break
        kept.append(text)
        length += extra
    return "; ".join(kept)


def _describe_error(exc: Exception) -> str:
    """Turn an exception into the short human message the CLI prints.

    `util.HttpError` embeds the URL and the status; users only need the status
    and what it means for them ("HTTP 404 (slug not found)").
    """
    text = str(exc).strip() or exc.__class__.__name__
    match = _HTTP_STATUS_RE.search(text)
    if match:
        status = int(match.group(1))
        hint = _STATUS_HINTS.get(status)
        return f"HTTP {status} ({hint})" if hint else f"HTTP {status}"
    return truncate(text, 200, suffix=" …")


def _as_list(payload: Any, *keys: str) -> list[Any]:
    """Coerce an API payload into a list of postings, whatever shape it took."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------


def _greenhouse_location(posting: Mapping[str, Any]) -> str:
    """Location for a Greenhouse posting.

    `location` is missing or null on plenty of real boards, in which case the
    offices list is the only hint we have.
    """
    location = posting.get("location")
    if isinstance(location, Mapping):
        name = _clean_location(location.get("name"))
        if name:
            return name
    elif isinstance(location, str):
        name = _clean_location(location)
        if name:
            return name

    offices = posting.get("offices")
    if isinstance(offices, list):
        candidates = []
        for office in offices:
            if isinstance(office, Mapping):
                candidates.append(office.get("location") or office.get("name"))
            else:
                candidates.append(office)
        return _join_locations(candidates)
    return ""


def _greenhouse_salary(posting: Mapping[str, Any]) -> str | None:
    """Pull a pay range out of the board's custom `metadata` list, if present.

    Greenhouse has no salary field; boards that publish one put it in metadata
    under a name of the company's choosing, so match loosely and give up quietly.
    """
    metadata = posting.get("metadata")
    if not isinstance(metadata, list):
        return None
    for entry in metadata:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").lower()
        if not any(word in name for word in ("salary", "compensation", "pay range")):
            continue
        value = entry.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _parse_greenhouse_posting(
    posting: Mapping[str, Any], slug: str, company: str
) -> Job | None:
    """Convert one Greenhouse posting into a `Job`, or None when unusable."""
    title = str(posting.get("title") or "").strip()
    if not title:
        return None

    job_id = posting.get("id")
    ats_job_id = str(job_id).strip() if job_id not in (None, "") else None
    url = str(posting.get("absolute_url") or "").strip()
    if not url and ats_job_id:
        url = f"https://boards.greenhouse.io/{slug}/jobs/{ats_job_id}"
    if not url:
        return None

    # `content` is HTML *entity-escaped* HTML ("&lt;p&gt;"). Unescape exactly
    # once to recover real markup, then flatten it to text.
    description = html_to_text(html_module.unescape(str(posting.get("content") or "")))

    # `updated_at` moves every time anyone touches the posting, so it overstates
    # freshness badly; `first_published` is the real publication date.
    posted_at = parse_datetime(posting.get("first_published")) or parse_datetime(
        posting.get("updated_at")
    )

    location = _greenhouse_location(posting)
    departments = [
        str(d.get("name")).strip()
        for d in posting.get("departments") or []
        if isinstance(d, Mapping) and d.get("name")
    ]

    return Job(
        source="greenhouse",
        company=company,
        title=title,
        url=url,
        location=location,
        description=description,
        posted_at=posted_at,
        # Only ever assert remote-ness positively: "no remote marker" is not
        # evidence of an onsite role, and False would wrongly narrow filters.
        remote=True if _mentions_remote(location, title) else None,
        salary=_greenhouse_salary(posting),
        ats="greenhouse",
        ats_job_id=ats_job_id,
        raw={
            "board": "greenhouse",
            "slug": slug,
            "id": job_id,
            "requisition_id": posting.get("requisition_id"),
            "first_published": posting.get("first_published"),
            "updated_at": posting.get("updated_at"),
            "departments": departments,
        },
    )


def fetch_greenhouse(
    slug: str, *, session: Any = None, content: bool = True
) -> list[Job]:
    """Fetch every open posting on a Greenhouse board.

    `content=False` skips the (large) description payload — used by `--check`,
    which only needs to know the board answers and how many roles it has.

    Raises on transport/HTTP failure; `fetch` and `check_slug` are the layers
    that turn that into a logged, non-fatal error.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty greenhouse slug")

    payload = http_get_json(
        GREENHOUSE_BOARD_URL.format(slug=clean),
        params={"content": "true"} if content else None,
        session=session,
    )
    company = company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "jobs"):
        if not isinstance(posting, Mapping):
            logger.debug("greenhouse/%s: skipping non-object posting %r", clean, posting)
            continue
        try:
            # Rare, but some boards do embed a proper display name.
            name = str(posting.get("company_name") or "").strip()
            job = _parse_greenhouse_posting(posting, clean, name or company)
        except Exception as exc:  # one bad posting must not kill the board
            logger.debug(
                "greenhouse/%s: skipping malformed posting %r: %s",
                clean, posting.get("id"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "greenhouse/%s: skipping posting without title/url (id=%r)",
                clean, posting.get("id"),
            )
            continue
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------


def _lever_location(posting: Mapping[str, Any], categories: Mapping[str, Any]) -> str:
    """Location for a Lever posting: the primary one first, extras appended.

    `allLocations` is where multi-city postings really live, and the geo filter
    only ever sees `Job.location`, so a location-less "Remote" posting still
    has to say "Remote" somewhere.
    """
    values: list[Any] = [categories.get("location")]
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        values.extend(all_locations)
    joined = _join_locations(values)
    if joined:
        return joined
    workplace = str(posting.get("workplaceType") or "").strip()
    return "Remote" if workplace.lower() == "remote" else ""


def _lever_description(posting: Mapping[str, Any]) -> str:
    """Assemble the full posting text.

    Lever splits a posting into an intro (`description`) plus `lists` blocks,
    and the lists are where the requirements live — dropping them would throw
    away the single most scoring-relevant part of the ad.
    """
    parts: list[str] = []
    intro = str(posting.get("descriptionPlain") or "").strip()
    if not intro:
        intro = html_to_text(posting.get("description"))
    if intro:
        parts.append(intro)

    for block in posting.get("lists") or []:
        if not isinstance(block, Mapping):
            continue
        heading = html_to_text(block.get("text")).strip()
        body = html_to_text(block.get("content")).strip()
        chunk = "\n".join(p for p in (heading, body) if p)
        if chunk:
            parts.append(chunk)

    # Closing boilerplate sometimes carries visa/relocation info worth keeping.
    closing = str(posting.get("additionalPlain") or "").strip()
    if not closing:
        closing = html_to_text(posting.get("additional"))
    if closing:
        parts.append(closing)

    return "\n\n".join(parts).strip()


def _lever_salary(posting: Mapping[str, Any]) -> str | None:
    """Format `salaryRange` when the board publishes one."""
    salary = posting.get("salaryRange")
    if not isinstance(salary, Mapping):
        return None
    low, high = salary.get("min"), salary.get("max")
    currency = str(salary.get("currency") or "").strip()
    interval = str(salary.get("interval") or "").strip()
    if low is None and high is None:
        return None
    amount = f"{low}-{high}" if (low is not None and high is not None) else str(
        low if low is not None else high
    )
    return " ".join(p for p in (currency, amount, interval) if p) or None


def _parse_lever_posting(posting: Mapping[str, Any], slug: str, company: str) -> Job | None:
    """Convert one Lever posting into a `Job`, or None when unusable."""
    title = str(posting.get("text") or posting.get("title") or "").strip()
    if not title:
        return None

    posting_id = posting.get("id")
    ats_job_id = str(posting_id).strip() if posting_id not in (None, "") else None
    url = str(posting.get("hostedUrl") or posting.get("applyUrl") or "").strip()
    if not url and ats_job_id:
        url = f"https://jobs.lever.co/{slug}/{ats_job_id}"
    if not url:
        return None

    categories = posting.get("categories")
    if not isinstance(categories, Mapping):
        categories = {}

    location = _lever_location(posting, categories)
    workplace = str(
        posting.get("workplaceType") or categories.get("workplaceType") or ""
    ).strip().lower()

    if workplace == "remote" or _mentions_remote(location, categories.get("allLocations")):
        remote: bool | None = True
    elif workplace in {"onsite", "on-site", "hybrid"}:
        remote = False  # Lever states this explicitly, so it is trustworthy.
    else:
        remote = None

    # `createdAt` is a millisecond epoch; util.parse_datetime detects that.
    posted_at = parse_datetime(posting.get("createdAt")) or parse_datetime(
        posting.get("publishedAt")
    )

    return Job(
        source="lever",
        company=company,
        title=title,
        url=url,
        location=location,
        description=_lever_description(posting),
        posted_at=posted_at,
        remote=remote,
        salary=_lever_salary(posting),
        ats="lever",
        ats_job_id=ats_job_id,
        raw={
            "board": "lever",
            "slug": slug,
            "id": posting_id,
            "commitment": categories.get("commitment"),
            "team": categories.get("team"),
            "department": categories.get("department"),
            "workplace_type": workplace or None,
            "apply_url": posting.get("applyUrl"),
            "created_at": posting.get("createdAt"),
        },
    )


def fetch_lever(slug: str, *, session: Any = None) -> list[Job]:
    """Fetch every published posting on a Lever board.

    Raises on transport/HTTP failure; callers (`fetch`, `check_slug`) contain it.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty lever slug")

    payload = http_get_json(
        LEVER_POSTINGS_URL.format(slug=clean),
        params={"mode": "json"},
        session=session,
    )
    company = company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "data", "postings", "results"):
        if not isinstance(posting, Mapping):
            logger.debug("lever/%s: skipping non-object posting %r", clean, posting)
            continue
        try:
            job = _parse_lever_posting(posting, clean, company)
        except Exception as exc:  # one bad posting must not kill the board
            logger.debug(
                "lever/%s: skipping malformed posting %r: %s",
                clean, posting.get("id"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "lever/%s: skipping posting without title/url (id=%r)",
                clean, posting.get("id"),
            )
            continue
        jobs.append(job)
    return jobs


def _fetch_board(board: str, slug: str, *, session: Any = None, **kwargs: Any) -> list[Job]:
    """Dispatch to the right fetcher.

    Deliberately resolves `fetch_greenhouse` / `fetch_lever` at call time via
    the module globals rather than a lookup table captured at import, so tests
    that monkeypatch the public fetchers really do intercept every caller.
    """
    if board == "greenhouse":
        return fetch_greenhouse(slug, session=session, **kwargs)
    if board == "lever":
        return fetch_lever(slug, session=session)
    raise ValueError(f"unknown board {board!r}")


# --------------------------------------------------------------------------
# watchlist -> jobs
# --------------------------------------------------------------------------


def _watchlist_entries(raw: Any) -> list[tuple[str, str | None]]:
    """Normalise a watchlist board section into `(slug, company_override)` pairs.

    Accepts the three shapes people actually write::

        greenhouse: [spotify, {slug: acme-corp, company: ACME Corporation}]
        greenhouse: {acme-corp: ACME Corporation}
        greenhouse: spotify
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, Mapping):
        raw = [{"slug": k, "company": v} for k, v in raw.items()]
    if not isinstance(raw, Sequence):
        logger.warning("watchlist entry ignored, expected a list: %r", raw)
        return []

    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for entry in raw:
        company: str | None = None
        if isinstance(entry, Mapping):
            slug = _clean_slug(
                entry.get("slug") or entry.get("board") or entry.get("id")
                or entry.get("name")
            )
            override = entry.get("company") or entry.get("display_name")
            company = str(override).strip() if override else None
        else:
            slug = _clean_slug(entry)
        if not slug:
            logger.warning("watchlist entry has no usable slug, skipping: %r", entry)
            continue
        if slug.lower() in seen:
            continue
        seen.add(slug.lower())
        pairs.append((slug, company or None))
    return pairs


def fetch(config: Config, *, session: Any = None, errors: list[str] | None = None) -> list[Job]:
    """Fetch every enabled Greenhouse/Lever board in the watchlist.

    Each slug is isolated: a renamed board, a 500 or a malformed payload costs
    that company's postings and nothing else. Failures are logged and appended
    to `errors`; this function never raises.
    """
    jobs: list[Job] = []
    for board in BOARDS:
        if not config.source_enabled(board):
            logger.debug("%s disabled in config.sources, skipping", board)
            continue

        entries = _watchlist_entries(config.watchlist.get(board))
        if not entries:
            logger.warning("%s is enabled but watchlist.%s is empty", board, board)
            continue

        for slug, company_override in entries:
            try:
                found = _fetch_board(board, slug, session=session)
            except Exception as exc:
                message = f"{board}/{slug}: {_describe_error(exc)}"
                logger.warning("%s", message)
                if errors is not None:
                    errors.append(message)
                continue
            if company_override:
                for job in found:
                    job.company = company_override
            logger.info("%s/%s: %d postings", board, slug, len(found))
            jobs.extend(found)
    return jobs


# --------------------------------------------------------------------------
# slug checking / CLI
# --------------------------------------------------------------------------


def _check(board: str, slug: str, *, session: Any = None) -> tuple[bool, str, int | None]:
    """`check_slug` plus the posting count, which `--json` reports separately."""
    board_name = str(board or "").strip().lower()
    clean = _clean_slug(slug)
    if board_name not in BOARDS:
        return False, f"unknown board {board!r} (expected one of {', '.join(BOARDS)})", None
    if not clean:
        return False, "empty slug", None

    try:
        # Greenhouse descriptions are not needed to prove a board exists, and
        # they dominate the payload — skip them.
        extra = {"content": False} if board_name == "greenhouse" else {}
        found = _fetch_board(board_name, clean, session=session, **extra)
    except Exception as exc:
        return False, _describe_error(exc), None

    count = len(found)
    if count == 0:
        return True, "0 postings (board reachable but empty)", 0
    return True, f"{count} posting{'s' if count != 1 else ''}", count


def check_slug(board: str, slug: str, *, session: Any = None) -> tuple[bool, str]:
    """Verify one board slug. Returns `(ok, human message)`, never raises.

    A reachable board with zero postings counts as OK: that is a real state
    (nobody is hiring today), not a broken slug.
    """
    ok, message, _count = _check(board, slug, session=session)
    return ok, message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.sources.ats_boards",
        description="Verify Greenhouse / Lever board slugs before trusting them.",
        epilog=(
            "examples:\n"
            "  python -m src.sources.ats_boards --check greenhouse spotify\n"
            "  python -m src.sources.ats_boards --check lever plaid\n"
            "  python -m src.sources.ats_boards --check-all --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        nargs=2,
        metavar=("BOARD", "SLUG"),
        help="check a single slug, e.g. --check greenhouse spotify",
    )
    parser.add_argument(
        "--check-all",
        action="store_true",
        help="check every greenhouse/lever slug in the watchlist",
    )
    parser.add_argument(
        "--config",
        default=str(_ROOT / "config.yaml"),
        help="path to config.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--watchlist",
        default=str(_ROOT / "watchlist.yaml"),
        help="path to watchlist.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of text"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `--check BOARD SLUG` / `--check-all`.

    Exit code 0 when every checked slug answered, 1 on any failure (including
    "you gave me nothing to check", so a typo in CI is never mistaken for a pass).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Keep stdout to the OK/FAIL lines; library logging goes to stderr only
    # when something is genuinely wrong.
    setup_logging("WARNING")

    targets: list[tuple[str, str]] = []
    if args.check:
        targets.append((str(args.check[0]).lower(), str(args.check[1])))
    if args.check_all:
        try:
            config = Config.load(args.config, args.watchlist)
        except ConfigError as exc:
            if args.json:
                print(json_lib.dumps({"ok": False, "error": str(exc), "results": []}))
            else:
                print(f"FAIL — {exc}")
            return 1
        for board in BOARDS:
            targets.extend(
                (board, slug) for slug, _company in _watchlist_entries(
                    config.watchlist.get(board)
                )
            )

    if not targets:
        if args.check_all:
            problem = f"no greenhouse/lever slugs found in {args.watchlist}"
            if args.json:
                print(json_lib.dumps({"ok": False, "error": problem, "results": []}))
            else:
                print(f"FAIL — {problem}")
        else:
            parser.print_help()
        return 1

    results: list[dict[str, Any]] = []
    failures = 0
    for board, slug in targets:
        ok, message, count = _check(board, slug)
        results.append(
            {"board": board, "slug": slug, "ok": ok, "count": count, "message": message}
        )
        if not ok:
            failures += 1
        if not args.json:
            print(f"{'OK' if ok else 'FAIL'} {board}/{slug} — {message}")

    if args.json:
        print(json_lib.dumps(
            {"ok": failures == 0, "checked": len(results), "failures": failures,
             "results": results},
            indent=2,
        ))
    elif len(results) > 1:
        print(f"\n{len(results) - failures}/{len(results)} slugs OK")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
