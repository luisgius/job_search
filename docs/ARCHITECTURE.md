# Architecture & module contract

This file is the **binding interface contract**. Every module implements
exactly the signatures below so the stages compose without surprises.

## Pipeline

```
sources.*      -> list[Job]          (never raises; logs + skips on failure)
filters.apply_filters                -> (kept: list[Job], rejected: list[tuple[Job, str]])
dedupe                               -> collapse Job.dedupe_key across sources
db.Tracker                           -> drop already-handled keys
scoring.score_jobs   -> list[ScoredJob]  (LLM; Score.error set on failure)
tailor.tailor        -> writes tailored CV + cover letter markdown
apply.autoapply.run  -> fills Greenhouse/Lever form OR bails to digest
digest.render        -> output/digest_YYYY-MM-DD.html
```

## Ground rules

1. **stdlib-first imports.** `requests`, `anthropic`, `playwright`,
   `googleapiclient`, `reportlab` and `jinja2` are imported *inside*
   functions, never at module top level (except `jinja2` in `digest.py` and
   `yaml` in `config.py`, which are hard core deps). This keeps the test
   suite runnable with only PyYAML + Jinja2 + pytest installed.
2. **Nothing network-facing may raise out of a stage.** Wrap with
   `util.safe_call` or an explicit try/except, log a warning, append to
   `RunStats.errors`, continue.
3. **All datetimes are timezone-aware UTC.** Use `models.ensure_utc` /
   `util.parse_datetime`. Never call `datetime.now()` without a tz.
4. **Clocks are injectable.** Any function whose behaviour depends on the
   current time takes `now: datetime | None = None` and defaults to
   `models.utcnow()`. Tests pass a fixed `now`; no `freezegun` needed.
5. **HTTP goes through `util.http_get` / `util.http_get_json`** so retries and
   the User-Agent are uniform, and so tests can inject a fake `session`.
6. **No `print` in library code.** Use `util.get_logger(__name__)`.
   `main.py` and the `--check` CLIs are the only places that print.

## Shared types (`src/models.py`) — already implemented

- `Job` — `source, company, title, url, location, description, posted_at,
  remote, salary, country, ats, ats_job_id, raw`; properties `key`,
  `dedupe_key`, `age_hours_at(now)`, `label`, `to_dict()`.
- `Score` — `value:int, reasons, strengths, gaps, verdict, model, error`, `.ok`.
- `Artifacts` — `dir, cv_md, cover_md, cv_pdf, cover_pdf, screenshot`.
- `ApplyStatus` — `new / filtered / scored_below / digest / dry_run /
  applied / apply_failed / skipped_duplicate`.
- `ScoredJob` — `job, score, artifacts, status, status_detail,
  cover_letter_md, tailored_cv_md`; `.score_value`, `.key`.
- `RunStats` — counters + `errors: list[str]` + `source_counts`.
- Helpers: `normalize_text`, `normalize_company`, `normalize_title`,
  `utcnow`, `ensure_utc`.

## Config (`src/config.py`) — already implemented

`Config.load(config_path, watchlist_path, root=None, env=None)`;
`cfg.get("apply.dry_run", default)`, `cfg.path("db.path")`, `cfg.watchlist`,
`cfg.source_enabled(name)`, `cfg.validate(require_llm=)`. Defaults live in
`DEFAULTS` / `WATCHLIST_DEFAULTS` — read them before inventing a key.

## Tracker (`src/db.py`) — already implemented

`Tracker(path)`, `record_job(job, now=)`, `has_job(key)`,
`record_status(key, status, detail=, score=, method=, artifacts_dir=, now=)`,
`get_status(key)`, `has_applied(key)`, `has_applied_similar(dedupe_key)`,
`record_submit_attempt(key, url=, method=, now=)` /
`clear_submit_attempt(key)` / `submit_attempted(key)`,
`should_surface(key, within_days=, now=)`, `start_run` / `finish_run`,
`counts_by_status()`.

`has_applied` is True only for `applied`. A dry run must leave the job
eligible for a real application later.

`has_applied_similar` is the same gate one notch blunter, on `dedupe_key`: a
recruiter closing and re-opening a requisition produces a new ATS id for the
same role, and `has_applied` cannot see that.

The `submit_attempt*` trio is a write-ahead record of a submit *click*, taken
before the click and cleared only on positive evidence that nothing was sent.
It is what stops a browser that dies mid-submit from turning into a second
application tomorrow; `apply.eligible` consults it.

---

## Modules to implement

### `src/geo.py`
```python
EU_COUNTRIES: dict[str, str]          # ISO alpha-2 -> English name
def country_of(location: str) -> str | None      # ISO alpha-2 or None
def countries_of(location: str) -> list[str]     # all of them, best first
def is_remote(location: str, title: str = "", description: str = "") -> bool
def mentions_eu(text: str) -> bool     # "Remote (EU)", "EMEA", "Europe" ...
```
`countries_of` exists because "Remote (Portugal, Spain, Poland)" is one job
you may take from any of three countries; `country_of` can only ever name one
of them, and the location gate must not pin the role to it. Its first element
is always exactly what `country_of` returns. A country named only to be ruled
out ("EMEA (excluding UK)") is not a country either function reports.
Must handle: `"Berlin, Germany"`, `"Berlin"`, `"Munich, DE"`, `"Amsterdam,
Netherlands"`, `"London, UK"`, `"Remote - Europe"`, `"EMEA"`, `"Paris, France;
Remote"`, `"Zürich"`, `"Kraków, Poland"`. Must NOT match `"Berlin, CT"` →
US, `"Birmingham, AL"`, `"San Francisco"`, `"New York"`, `"Remote (US)"`.
City→country table for the ~80 largest EU tech hubs. Word-boundary matching
only — never a naive `in` substring test (`"IN"` must not match "Berlin").

### `src/filters.py`
```python
@dataclass
class FilterResult:
    kept: list[Job]
    rejected: list[tuple[Job, str]]   # (job, human-readable reason)
    counts: dict[str, int]            # reason -> n

def apply_filters(jobs, config, *, now=None) -> FilterResult
def dedupe(jobs: list[Job]) -> list[Job]   # keeps the richest record per dedupe_key
def is_fresh(job, max_age_hours, *, skip_undated=True, now=None) -> tuple[bool, str]
def passes_location(job, config) -> tuple[bool, str]
def passes_title(job, config) -> tuple[bool, str]
def passes_keywords(job, config) -> tuple[bool, str]
```
Filter order (cheapest first): title → employment type → location → freshness
→ keywords → min_description_chars. `apply_filters` also stamps
`job.country`.
Title include/exclude match **whole words, case-insensitively** — `"intern"`
must not reject `"International Sales"`, but must reject `"Intern - Backend"`.
The employment-type stage reads only what a source states as structured data
(Lever `categories.commitment`, Adzuna `contract_type`), never the title.

### `src/sources/ats_boards.py`
```python
GREENHOUSE_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_POSTINGS_URL   = "https://api.lever.co/v0/postings/{slug}"

def fetch_greenhouse(slug, *, session=None, content=True) -> list[Job]
def fetch_lever(slug, *, session=None) -> list[Job]
def fetch(config, *, session=None, errors=None) -> list[Job]
def check_slug(board: str, slug: str, *, session=None) -> tuple[bool, str]
def main(argv=None) -> int          # supports: --check greenhouse spotify
```
- Greenhouse: `?content=true`, description in `content` (HTML-escaped),
  date in `updated_at` / `first_published`, location in `location.name`,
  id in `id`, url in `absolute_url`. Company = the slug (title-cased) unless
  the payload carries a better name.
- Lever: list of postings; `text`, `hostedUrl`, `categories.location`,
  `categories.commitment`, `createdAt` (ms epoch), `descriptionPlain` or
  `description`, `id`.
- `--check` prints `OK <board>/<slug> — N postings` or a clear failure and
  returns exit code 0/1. Runnable as `python -m src.sources.ats_boards`.

### `src/sources/adzuna.py`
```python
BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
def fetch(config, *, session=None, errors=None) -> list[Job]
def parse_result(payload: dict, country: str) -> Job | None
```
Params: `app_id`, `app_key`, `results_per_page`, `what`, `max_days_old`,
`content-type=application/json`. `created` is ISO. Company from
`company.display_name`, location from `location.display_name`. Skips a
country cleanly when the key is rejected.

### `src/sources/linkedin_email.py`
```python
def fetch(config, *, service=None, errors=None) -> list[Job]
def build_service(config)                      # lazy google-api import
def parse_message(payload: dict) -> list[Job]  # one Gmail message -> jobs
def extract_jobs_from_html(html: str, received_at=None) -> list[Job]
def canonical_linkedin_url(url: str) -> str    # strip tracking params
def fetch_description(url, *, session=None) -> str    # guest endpoint, best-effort
```
Parse `https://www.linkedin.com/comm/jobs/view/<id>` links out of the alert
HTML; company/title/location come from the surrounding markup. Read-only
Gmail scope (`gmail.readonly`). Description fetch failures degrade to
email-only info — never fatal.

### `src/llm.py`
```python
class LLMError(RuntimeError): ...
class LLMClient:
    def __init__(self, api_key: str, *, client=None): ...
    def complete(self, *, model, system, prompt, max_tokens, temperature=0.0) -> str
    def complete_json(self, *, ..., schema_hint: str = "") -> dict
```
Lazy `import anthropic`. `complete_json` must survive fenced ```json blocks
and prose around the object; raise `LLMError` when no object can be
recovered. Retry twice on transient API errors. `client=` is the test seam.

### `src/scoring.py`
```python
def build_prompt(job: Job, cv_markdown: str, applicant: dict) -> str
def parse_score(payload: dict) -> Score
def score_job(job, cv_markdown, config, *, client=None) -> Score
def score_jobs(jobs, cv_markdown, config, *, client=None, errors=None) -> list[ScoredJob]
```
Model returns `{"score": 0-100, "verdict": str, "reasons": [str],
"strengths": [str], "gaps": [str]}`. `parse_score` clamps to 0-100, coerces
numeric strings, tolerates missing lists. A failed call yields
`Score(value=0, error=...)` and the job goes to the digest with a warning —
never silently dropped. Respects `scoring.max_jobs`.

### `src/tailor.py`
```python
def build_cv_prompt(job, cv_markdown, applicant) -> str
def build_cover_prompt(job, cv_markdown, applicant) -> str
def tailor_job(scored, cv_markdown, config, *, client=None, out_dir=None) -> ScoredJob
def tailor_jobs(scored_jobs, cv_markdown, config, *, client=None, errors=None) -> list[ScoredJob]
def artifact_dir(job, base_dir) -> Path      # <base>/applications/<slug>-<key>
```
The prompts must forbid inventing employers, dates, degrees, or metrics not
present in the base CV — reordering/rephrasing/emphasis only. Writes
`cv.md` + `cover_letter.md` into the artifact dir and fills
`ScoredJob.artifacts`. Respects `tailoring.max_per_run`.

### `src/render_pdf.py` — **user-supplied hook, do not implement**
```python
def render(cv_markdown: str, out_path: str) -> None: ...
```
Provide `src/render_pdf.example.py` as a working ReportLab starting point,
plus `pdf.render_if_available(md, out_path) -> str | None` in
`src/pdf.py` that imports `src.render_pdf` if present and returns the path,
or None. Missing hook ⇒ no PDF ⇒ **auto-apply is skipped, job to digest**.

### `src/apply/autoapply.py`
```python
SUPPORTED_ATS = ("greenhouse", "lever")

@dataclass
class ApplyOutcome:
    status: ApplyStatus
    detail: str
    screenshot: str | None = None

def detect_ats(url: str) -> str | None
def eligible(scored, config, tracker=None) -> tuple[bool, str]
def inspect_form(page) -> tuple[bool, str]      # (simple enough?, reason)
def apply_one(scored, config, *, page=None, tracker=None, now=None) -> ApplyOutcome
def run(scored_jobs, config, *, tracker=None, browser=None) -> list[ScoredJob]
```
`eligible` gates on, in order: `apply.enabled`, supported ATS, a score the
model actually produced (`Score.error` unset — a job the scorer failed on is
not a job that scored badly), `score >= apply.min_score`, a tailored CV PDF
exists (when `apply.require_pdf`), and then the tracker: not already applied,
no unresolved submit click, and not the same role under a new requisition id.

`inspect_form` is the safety core and must **bail** (return False) whenever
the form contains anything beyond first/last/full name, email, phone,
resume upload, LinkedIn/website URL, and a legally-required consent
checkbox. Any `<textarea>`, any `<select>`, any radio group, any required
field it does not recognise, any question-like label (`?`, "why", "describe",
"sponsorship", "salary", "notice period", "how did you hear"), and any
free-text box with no readable label at all (that is how Greenhouse renders
every custom question) ⇒ bail.

`apply_one` in `dry_run` mode fills the form, saves
`<artifact_dir>/form_filled.png`, and returns `DRY_RUN` **without clicking
submit**. Only with `dry_run: false` does it click — and then only if every
required field it recognises has a value in `config.applicant`; a form it
would have to submit incomplete goes to the digest instead. A confirmation
found only as *text* is believed only when the form is also gone, so a
multi-step form thanking you on step 1 is not read as a submission.
`page=` is the test seam — the whole module must be exercisable with a fake
page object; Playwright is imported only inside `run`.

### `src/digest.py`
```python
def build_context(scored_jobs, stats, config, *, now=None) -> dict
def render_html(context) -> str
def write_digest(scored_jobs, stats, config, *, now=None) -> Path
```
Jinja2 template at `src/templates/digest.html.j2`, self-contained (inline
CSS, no CDN). Sections: **Needs your click** (digest status, sorted by score
desc), **Auto-applied**, **Dry run — check these**, **Below threshold**,
**Run stats & errors**. Every item shows score, `score_reasons`, gaps,
company/title/location/posted, apply link, and links to the tailored files.
Must escape untrusted text (job titles/descriptions come from the internet).

### `src/main.py`
```python
def build_parser() -> argparse.ArgumentParser
def run_pipeline(config, *, tracker=None, now=None, llm_client=None) -> tuple[list[ScoredJob], RunStats]
def main(argv=None) -> int
```
Flags: `--no-browser`, `--dry-run/--no-dry-run`, `--config`, `--watchlist`,
`--limit N`, `--source NAME` (repeatable), `--skip-apply`, `--verbose`,
`--validate-only`. Exit codes: 0 ok, 1 config invalid, 2 unexpected error.
Prints a compact summary and the digest path.
