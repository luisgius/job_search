"""Tailored CV + cover letter generation.

This is the stage with the largest accountability surface in the repo: what it
writes goes out under the user's name, and a hallucinated employer or degree is
CV fraud whether or not a model typed it. So the defence is layered:

  1. **The prompts forbid invention explicitly** (`ANTI_FABRICATION`), in terms
     specific enough to be checkable: employers, titles, dates, degrees,
     certifications, tools, metrics.
  2. **`validate_tailored_cv` checks the output afterwards**, because prompt
     instructions are a request, not a guarantee. A CV that came back empty,
     lost the applicant's name, or doubled in length is discarded and the base
     CV is kept.
  3. **Everything is written to disk** — `cv.md`, `cover_letter.md` and a
     `job.json` describing what it was tailored for — so three weeks later the
     artifact directory still explains itself.

A tailoring failure is never fatal: the job keeps its score and reaches the
digest with the raw link, just without generated documents.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .llm import LLMError, client_from_config
from .models import ApplyStatus, Artifacts, Job, ScoredJob, normalize_text
from .util import ensure_dir, get_logger, slugify, truncate

logger = get_logger(__name__)

DESCRIPTION_LIMIT = 6000
COVER_LETTER_MAX_WORDS = 300

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_PER_RUN = 10
DEFAULT_THRESHOLD = 65

#: A tailored CV longer than this multiple of the base is padding, not
#: emphasis — the model started writing new experience.
MAX_LENGTH_RATIO = 2.0

CV_SYSTEM_PROMPT = """\
You are an expert technical CV editor. You re-present an existing CV for one \
specific job. You are not a copywriter and you are not the candidate's \
biographer: you have no information about this person beyond the CV you are \
given, and you never act as if you do. You output markdown only."""

COVER_SYSTEM_PROMPT = """\
You write short, specific cover letters that a hiring manager finishes. You \
work strictly from the candidate's CV — every claim you make must already be \
in it. You output markdown only."""

#: Pinned by the test-suite. Keep the specifics: a vague "be accurate" clause
#: is exactly the kind of instruction models round off.
ANTI_FABRICATION = """\
ABSOLUTE RULE — DO NOT FABRICATE
You may reorder, re-emphasise, re-word, summarise and select from the base CV,
and you may drop items irrelevant to this posting. You must NOT invent, imply
or embellish anything that is not already in the base CV. Specifically, never
introduce:
  - employers, clients or projects that do not appear in the base CV;
  - job titles, seniorities, team sizes or reporting lines it does not state;
  - dates, durations or years of experience it does not state;
  - degrees, universities, certifications or licences it does not list;
  - technologies, tools, languages or frameworks it does not name;
  - metrics, percentages, revenue or scale figures it does not contain.
If this job asks for something the base CV does not show, OMIT it. Do not
approximate it, do not phrase it as familiarity, do not imply it by adjacency.
An omission costs the candidate one application; a fabrication costs them the
offer and their reputation."""

OUTPUT_RULES = """\
OUTPUT
Markdown only. No preamble, no commentary, no explanation of your choices, no
code fences around the document."""


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


def _float(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _applicant(config: Any) -> dict[str, Any]:
    value = _cfg(config, "applicant", {}) or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _output_dir(config: Any) -> Path:
    """Where artifacts go. Honours `Config.path` so relative dirs resolve."""
    resolver = getattr(config, "path", None)
    if callable(resolver):
        return Path(resolver("output.dir", "output"))
    return Path(str(_cfg(config, "output.dir", "output") or "output"))


# --------------------------------------------------------------------------
# artifact locations
# --------------------------------------------------------------------------


def artifact_dir(job: Job, base_dir: str | Path) -> Path:
    """`<base_dir>/applications/<slug>-<key8>`, created.

    The key suffix keeps two roles with the same title at the same company
    (different teams, different reqs) in separate directories, while the slug
    keeps the path readable when you open it by hand.
    """
    slug = slugify(f"{job.company} {job.title}")
    return ensure_dir(Path(base_dir) / "applications" / f"{slug}-{job.key[:8]}")


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------


def _job_block(job: Job) -> str:
    facts = [
        f"Company: {job.company or 'unknown'}",
        f"Title: {job.title or 'unknown'}",
        f"Location: {job.location or 'unstated'}",
        f"URL: {job.url}",
    ]
    description = truncate(job.description or "", DESCRIPTION_LIMIT)
    if not description.strip():
        description = "(no description available)"
    return "\n".join(facts) + f"\n\nDescription:\n{description}"


def _contact_block(applicant: Mapping[str, Any] | None) -> str:
    """Contact details the model may safely put in the header.

    These come from config, not from the model, so they are the one class of
    fact it is allowed to add — and it must use exactly these spellings.
    """
    data = applicant or {}
    fields = (
        ("Name", "name"), ("Email", "email"), ("Phone", "phone"),
        ("Location", "location"), ("LinkedIn", "linkedin"),
        ("GitHub", "github"), ("Website", "website"),
    )
    lines = [f"{label}: {str(data.get(key)).strip()}"
             for label, key in fields if str(data.get(key) or "").strip()]
    return "\n".join(lines)


def build_cv_prompt(job: Job, cv_markdown: str, applicant: Mapping[str, Any] | None) -> str:
    """Prompt for the tailored CV: base CV + posting + anti-fabrication rules."""
    contact = _contact_block(applicant)
    contact_block = (
        f"\nCONTACT DETAILS (use exactly these, verbatim, in the header)\n{contact}\n"
        if contact else ""
    )
    return f"""\
Re-present the base CV below for this specific job posting.

BASE CV (the only source of truth about this candidate)
--------------------------------------------------------------------
{cv_markdown}
--------------------------------------------------------------------

TARGET POSTING
{_job_block(job)}
{contact_block}
WHAT TO DO
- Keep the same overall structure and section headings as the base CV.
- Lead with the experience, skills and bullets this posting actually asks for;
  move the rest down or drop it.
- Re-word bullets to use the posting's vocabulary ONLY where the base CV
  already describes that same work.
- Keep every metric exactly as written in the base CV — same numbers, same
  units, same direction.
- Aim for the same length as the base CV or shorter. Never longer.

{ANTI_FABRICATION}

{OUTPUT_RULES}"""


def build_cover_prompt(job: Job, cv_markdown: str, applicant: Mapping[str, Any] | None) -> str:
    """Prompt for the cover letter: short, concrete, sourced from the CV."""
    name = str((applicant or {}).get("name") or "").strip()
    signature = f"\n- Sign off with the candidate's name: {name}." if name else ""
    company = job.company or "the company"
    return f"""\
Write a cover letter for this candidate for this specific job.

BASE CV (the only source of truth about this candidate)
--------------------------------------------------------------------
{cv_markdown}
--------------------------------------------------------------------

TARGET POSTING
{_job_block(job)}

REQUIREMENTS
- At most {COVER_LETTER_MAX_WORDS} words. Shorter is better.
- Addressed to {company} (use "Dear {company} team" if no named contact
  appears in the posting).
- Exactly three short paragraphs:
  1. why this role, tied to something concrete in the posting;
  2. two specific achievements TAKEN FROM THE CV, with the CV's own numbers,
     and why they matter for this role;
  3. a short close.
- State availability, notice period or work authorisation ONLY if the CV
  states it. If the CV is silent, say nothing about it.
- Do NOT open with "I am writing to apply for" or any variant of it. Start
  with something that could only have been written for this posting.
- Do NOT profess enthusiasm for products, missions or markets the CV gives no
  evidence the candidate has worked with or cares about. No flattery about the
  company's "innovative culture".
- Plain, direct sentences. No superlatives about the candidate.{signature}

{ANTI_FABRICATION}

{OUTPUT_RULES}"""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Remove a code fence wrapped around the whole document.

    Models add ```markdown fences despite being told not to, and a leading
    fence would end up rendered literally in the PDF.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip()


def validate_tailored_cv(
    base_md: str,
    tailored_md: str | None,
    applicant: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Sanity-check a tailored CV before it is allowed to represent someone.

    Not a fabrication detector — that cannot be done cheaply. It catches the
    failure modes that are *mechanically* checkable and would be embarrassing:
    an empty document, a document that lost the candidate's name, and one that
    grew far beyond the base CV (which means new content was written, not
    re-emphasised).

    Returns `(ok, reason)`; `reason` is empty when ok.
    """
    text = (tailored_md or "").strip()
    if not text:
        return False, "tailored CV is empty"

    base = (base_md or "").strip()
    name = str((applicant or {}).get("name") or "").strip()
    if name:
        needle = normalize_text(name)
        # Only enforce what the base CV itself establishes: if the user's own
        # CV never spells their name, its absence is not the model's doing.
        if needle and needle in normalize_text(base) and needle not in normalize_text(text):
            return False, f"tailored CV no longer contains the applicant's name ({name})"

    if base and len(text) > MAX_LENGTH_RATIO * len(base):
        ratio = len(text) / len(base)
        return False, (
            f"tailored CV is {ratio:.1f}x the length of the base CV "
            f"(limit {MAX_LENGTH_RATIO:g}x) — it invented content"
        )
    return True, ""


# --------------------------------------------------------------------------
# tailoring
# --------------------------------------------------------------------------


def _resolve_client(config: Any, client: Any) -> Any:
    """Return the injected client, or build the one `llm.provider` asks for."""
    if client is not None:
        return client
    return client_from_config(config)


def _write_job_json(path: Path, scored: ScoredJob) -> None:
    """Dump the posting + its score next to the generated documents."""
    payload: dict[str, Any] = scored.job.to_dict()
    score = scored.score
    payload["score"] = None if score is None else {
        "value": score.value,
        "verdict": score.verdict,
        "reasons": list(score.reasons),
        "strengths": list(score.strengths),
        "gaps": list(score.gaps),
        "model": score.model,
        "error": score.error,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def tailor_job(
    scored: ScoredJob,
    cv_markdown: str,
    config: Any,
    *,
    client: Any = None,
    out_dir: str | Path | None = None,
) -> ScoredJob:
    """Generate, validate and write a tailored CV + cover letter for one job.

    Mutates and returns `scored`. On any model failure the artifacts are left
    empty and `status_detail` explains why — the job still reaches the digest,
    it just arrives without documents.
    """
    job = scored.job
    model = str(_cfg(config, "tailoring.model", DEFAULT_MODEL) or DEFAULT_MODEL)
    max_tokens = _int(_cfg(config, "tailoring.max_tokens", DEFAULT_MAX_TOKENS), DEFAULT_MAX_TOKENS)
    temperature = _float(
        _cfg(config, "tailoring.temperature", DEFAULT_TEMPERATURE), DEFAULT_TEMPERATURE
    )
    applicant = _applicant(config)

    try:
        llm = _resolve_client(config, client)
        cv_md = _strip_fences(llm.complete(
            model=model,
            system=CV_SYSTEM_PROMPT,
            prompt=build_cv_prompt(job, cv_markdown, applicant),
            max_tokens=max_tokens,
            temperature=temperature,
        ))
        cover_md = _strip_fences(llm.complete(
            model=model,
            system=COVER_SYSTEM_PROMPT,
            prompt=build_cover_prompt(job, cv_markdown, applicant),
            max_tokens=max_tokens,
            temperature=temperature,
        ))
    except LLMError as exc:
        logger.warning("tailoring %s failed: %s", job.label, exc)
        scored.status_detail = f"tailoring failed: {exc}"
        return scored
    except Exception as exc:  # an odd injected client must not end the run
        logger.warning("tailoring %s failed unexpectedly: %s", job.label, exc)
        scored.status_detail = f"tailoring failed: {exc}"
        return scored

    detail = ""
    ok, reason = validate_tailored_cv(cv_markdown, cv_md, applicant)
    if not ok:
        # Falling back to the base CV is always safe: it is the document the
        # user wrote about themselves.
        logger.warning("discarding tailored CV for %s: %s", job.label, reason)
        cv_md = cv_markdown
        detail = f"tailored CV rejected ({reason}); using the base CV"

    if not (cover_md or "").strip():
        cover_md = ""
        detail = (detail + "; " if detail else "") + "cover letter came back empty"

    base = Path(out_dir) if out_dir is not None else _output_dir(config)
    try:
        directory = artifact_dir(job, base)
        cv_path = directory / "cv.md"
        cover_path = directory / "cover_letter.md"
        cv_path.write_text(cv_md, encoding="utf-8")
        cover_path.write_text(cover_md, encoding="utf-8")
        _write_job_json(directory / "job.json", scored)
    except OSError as exc:
        logger.warning("could not write artifacts for %s: %s", job.label, exc)
        scored.status_detail = f"tailoring failed: could not write artifacts: {exc}"
        return scored

    scored.tailored_cv_md = cv_md
    scored.cover_letter_md = cover_md
    if scored.artifacts is None:  # defensive: the dataclass default is never None
        scored.artifacts = Artifacts()
    scored.artifacts.dir = str(directory)
    scored.artifacts.cv_md = str(cv_path)
    scored.artifacts.cover_md = str(cover_path)
    if detail:
        scored.status_detail = detail
    logger.info("tailored %s -> %s", job.label, directory)
    return scored


def tailor_jobs(
    scored_jobs: Iterable[ScoredJob],
    cv_markdown: str,
    config: Any,
    *,
    client: Any = None,
    errors: list[str] | None = None,
) -> list[ScoredJob]:
    """Tailor the jobs headed for the digest, capped by `tailoring.max_per_run`.

    Returns the full input list in its original order — tailored and not — so
    the caller can hand it straight to the apply stage and the digest.
    """
    items = list(scored_jobs or [])
    if not items:
        return []

    if not bool(_cfg(config, "tailoring.enabled", True)):
        logger.info("tailoring.enabled is false — skipping %d jobs", len(items))
        return items

    threshold = _int(_cfg(config, "scoring.threshold", DEFAULT_THRESHOLD), DEFAULT_THRESHOLD)
    max_per_run = _int(
        _cfg(config, "tailoring.max_per_run", DEFAULT_MAX_PER_RUN), DEFAULT_MAX_PER_RUN
    )

    eligible = [
        item for item in items
        if item.status is ApplyStatus.DIGEST and item.score_value >= threshold
    ]
    if 0 < max_per_run < len(eligible):
        skipped = len(eligible) - max_per_run
        logger.warning(
            "tailoring.max_per_run=%d reached: tailoring %d of %d eligible jobs, "
            "%d left untailored (they still reach the digest)",
            max_per_run, max_per_run, len(eligible), skipped,
        )
        for item in eligible[max_per_run:]:
            item.status_detail = item.status_detail or (
                f"not tailored: tailoring.max_per_run ({max_per_run}) reached"
            )
        eligible = eligible[:max_per_run]

    if not eligible:
        logger.info("nothing to tailor (%d jobs, threshold %d)", len(items), threshold)
        return items

    try:
        llm = _resolve_client(config, client)
    except LLMError as exc:
        message = f"tailoring skipped: {exc}"
        logger.warning(message)
        if errors is not None:
            errors.append(message)
        for item in eligible:
            item.status_detail = message
        return items

    out_dir = _output_dir(config)
    tailored = 0
    for item in eligible:
        before = item.status_detail
        tailor_job(item, cv_markdown, config, client=llm, out_dir=out_dir)
        if item.artifacts and item.artifacts.cv_md:
            tailored += 1
        elif item.status_detail and item.status_detail != before and errors is not None:
            errors.append(f"tailoring {item.job.label}: {item.status_detail}")

    logger.info("tailored %d of %d eligible jobs", tailored, len(eligible))
    return items
