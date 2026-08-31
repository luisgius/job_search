# Architecture & module contract

This file is the **binding interface contract**. Every module implements
exactly the signatures below so the stages compose without surprises.

## Pipeline

```
sources.*      -> list[Job]          (never raises; logs + skips on failure)
dedupe                               -> collapse canonical apply URL, then Job.dedupe_key
filters.apply_filters                -> (kept: list[Job], rejected: list[tuple[Job, str]])
db.Tracker                           -> drop already-handled keys
scoring.score_jobs   -> list[ScoredJob]  (LLM; Score.error set on failure)
tailor.tailor        -> writes tailored CV + cover letter markdown
apply.autoapply.run  -> fills Greenhouse/Lever form OR bails to digest
digest.render        -> output/digest_YYYY-MM-DD.html
```

## Ground rules

1. **stdlib-first imports.** `requests`, `anthropic`, `playwright`,
   `googleapiclient`, `reportlab`, `lingua` and `jinja2` are imported *inside*
   functions, never at module top level (except `jinja2` in `digest.py` and
   `yaml` in `config.py`, which are hard core deps). This keeps most of the
   test suite runnable with only PyYAML + Jinja2 + pytest installed — the
   language-gate tests additionally want `lingua-language-detector`, whose
   absence in `src` degrades to keep-every-language.
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
  `utcnow`, `ensure_utc`. `normalize_text(value, *, casefold=True)` — the fold
  is off for exactly one caller, `ats_boards`' slug discovery, because
  SmartRecruiters' slug is case-sensitive; a second normaliser for that would
  be free to drift away from the one job identity depends on.

## Config (`src/config.py`) — already implemented

`Config.load(config_path, watchlist_path, root=None, env=None)`;
`cfg.get("apply.dry_run", default)`, `cfg.path("db.path")`, `cfg.watchlist`,
`cfg.source_enabled(name)`, `cfg.validate(require_llm=)`. Defaults live in
`DEFAULTS` / `WATCHLIST_DEFAULTS` — read them before inventing a key.

**A duplicated mapping key is a `ConfigError`, not a last-wins.** PyYAML keeps
the last copy of a duplicated key and raises nothing, so a watchlist that ends
in a second `greenhouse:` block — a pasted `--discover` report is the obvious
source — used to silently delete every company under the first block from
every future run: quiet job loss that reads as a quiet market, with exit code
0. `_read_yaml` loads through a `SafeLoader` subclass that refuses the file
and names the key, at every nesting level. YAML anchors, aliases and `<<:`
merge keys still work — overriding a merged key explicitly is the point of
that feature, not a paste accident.

Named constants, because a number written in two places is a number free to
drift: `DEFAULT_MAX_AGE_HOURS` (72, the freshness window) and
`DEFAULT_REPOST_MIN_GAP_DAYS` (14, the ghost-job flag). Both are imported by
`filters` and `digest` rather than re-typed.

**The shipped `config.yaml` is a fifth site and it wins at runtime**, because
it deep-merges *over* `DEFAULTS`. `test_config` compares the two files key by
key and fails on any divergence that is not listed as deliberate — without
that, editing the shipped file could silently revert a change to `DEFAULTS`
with every test still green. Two things it caught: `notify.on` had become the
boolean key `True` (YAML 1.1 reads a bare `on` as true), so every edit to the
shipped alert list did nothing; and `filters.description_exclude` had lost
`ts/sci`. Quote any key YAML would read as a boolean — `"on"`, `"NO"`.

## Tracker (`src/db.py`) — already implemented

`Tracker(path)`, `record_job(job, now=)`, `has_job(key)`,
`record_status(key, status, detail=, score=, method=, artifacts_dir=, now=)`,
`get_status(key)`, `has_applied(key)`, `has_applied_similar(dedupe_key)`,
`repost_gap_days(dedupe_key, key=, source=, posted_at=, now=)`,
`record_submit_attempt(key, url=, method=, now=)` /
`clear_submit_attempt(key)` / `submit_attempted(key)`,
`should_surface(key, within_days=, now=)`, `start_run` / `finish_run`,
`counts_by_status()`.

`has_applied` is True only for `applied`. A dry run must leave the job
eligible for a real application later.

`has_applied_similar` is the same gate one notch blunter, on `dedupe_key`: a
recruiter closing and re-opening a requisition produces a new ATS id for the
same role, and `has_applied` cannot see that.

`repost_gap_days(dedupe_key, key=, source=, posted_at=, now=)` reads the same
`dedupe_key` index to *measure* that re-opening — how many days before this
listing the same role was already on the market — and returns `None` when it
never was. It decides nothing: the threshold lives in
`freshness.repost_min_gap_days` (**0 means off**) and the only consumer is an
advisory line on a digest card.

Two restrictions carry the accuracy, and both exist because this flag accuses
a named employer:

- **Only same-`source` sightings count.** A different board with the same
  `dedupe_key` has three innocent readings and one guilty one: an ATS
  migration re-lists a company's whole board under new ids in a day (four
  reqs, four accusations, one relocation); an aggregator re-dates a live
  posting, because Adzuna's `created` is its own ingest time; or it is a plain
  cross-source duplicate. The gap alone was supposed to separate the last of
  those and only can when both sources agree about *when* — which is exactly
  what an aggregator does not do. Pass `source=`; the stored row is only a
  fallback for a caller that has not got one.
- **An undated *current* listing returns `None`.** Substituting `first_seen_at`
  for a prior row can only shrink the gap, which is the safe direction.
  Substituting it for the listing being judged moves the reference later and
  *inflates*: a role open and undated for 200 days produced the same `gap=200`
  as a role genuinely re-listed today. Reachable whenever
  `freshness.skip_undated` is false.

What is left is irreducible — an honest re-advertisement after a failed search
looks exactly like a ghost job from outside — so the card's wording reports the
measurement and names the innocent explanation rather than asserting a
mechanism.

**Posting age is deliberately not a second signal.** It cannot be one here:
every card comes from `scored_jobs ⊆ fresh ⊆ apply_filters(...).kept`, so it is
younger than `freshness.max_age_hours` by construction. A `stale_after_days: 30`
knob shipped once beside `max_age_hours: 72` and could not fire on anything the
pipeline was able to produce; it is gone from `config.yaml`, `DEFAULTS` and
`validate()`. The gap survives that argument because a re-listing carries a
brand new date and walks straight through the freshness window.

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
def dedupe(jobs: list[Job]) -> list[Job]   # URL-first: collapse on util.canonical_url,
                                           # then on dedupe_key; richest record wins
def is_fresh(job, max_age_hours, *, skip_undated=True, now=None) -> tuple[bool, str]
def passes_location(job, config) -> tuple[bool, str]
def passes_title(job, config) -> tuple[bool, str]
def passes_keywords(job, config) -> tuple[bool, str]
def first_title_match(title, terms) -> str | None   # the public whole-word matcher
```
Filter order (cheapest first): title → employment type → location → freshness
→ language → keywords → min_description_chars. `apply_filters` also stamps
`job.country`.
Title include/exclude match **whole words, case-insensitively** — `"intern"`
must not reject `"International Sales"`, but must reject `"Intern - Backend"`.
The title stage also stamps `raw["level"] = "junior"` when an *included* title
carries a `filters.title_junior_markers` term — metadata, never a gate, and
never on a plain title. The employment-type stage reads only what a source
states as structured data (Lever `categories.commitment`, Adzuna
`contract_type`), never the title. The location stage honours
`filters.countries_if_sponsorship`: those countries pass only when the posting
itself offers visa sponsorship (offer-shaped phrasings; "must have a valid
work visa" is the opposite sentence). The language stage
(`filters.languages`, ISO-639-1; empty = off) judges the *description only*
with lingua, keeps anything under `language_min_chars` or under 0.70
confidence, logs its drops per source, and degrades to keep-everything when
lingua is not installed.

### `src/sources/ats_boards.py`
```python
GREENHOUSE_BOARD_URL         = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_POSTINGS_URL           = "https://api.lever.co/v0/postings/{slug}"
WORKABLE_ACCOUNT_URL         = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
ASHBY_JOB_BOARD_URL          = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
SMARTRECRUITERS_POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SMARTRECRUITERS_POSTING_URL  = ".../postings/{posting_id}"
PERSONIO_XML_URL             = "https://{host}/xml"   # host = {slug}.jobs.personio.de
RECRUITEE_OFFERS_URL         = "https://{slug}.recruitee.com/api/offers/"
TEAMTAILOR_FEED_URL          = "https://{slug}.teamtailor.com/jobs.rss"  # or a full careers URL

BOARDS: tuple[str, ...] = ("greenhouse", "lever", "workable", "ashby",
                           "smartrecruiters", "personio", "recruitee",
                           "teamtailor")

def fetch_greenhouse(slug, *, session=None, content=True, retries=3) -> list[Job]
def fetch_lever(slug, *, session=None, retries=3) -> list[Job]
def fetch_workable(slug, *, session=None, details=True, retries=3,
                   envelope=None) -> list[Job]   # envelope: dict out-param —
                                    # account-level facts (the tenant's own
                                    # name) a zero-job payload still carries
def fetch_ashby(slug, *, session=None, retries=3) -> list[Job]
def fetch_smartrecruiters(slug, *, session=None, details=True,
                          max_descriptions=SMARTRECRUITERS_MAX_DESCRIPTIONS,
                          max_pages=SMARTRECRUITERS_MAX_PAGES, retries=3) -> list[Job]
def fetch_personio(slug, *, session=None, language=None, retries=3) -> list[Job]
def fetch_recruitee(slug, *, session=None, retries=3) -> list[Job]
def fetch_teamtailor(slug, *, session=None, retries=3) -> list[Job]  # slug or careers URL
def fetch(config, *, session=None, errors=None) -> list[Job]
def check_slug(board: str, slug: str, *, session=None) -> tuple[bool, str]
def main(argv=None) -> int          # supports: --check greenhouse spotify

# discovery: company name -> the board and slug to paste into watchlist.yaml
DISCOVER_MAX_SLUGS_PER_COMPANY = 4
DISCOVER_MAX_REQUESTS = 120
PROBE_FOUND / PROBE_EMPTY / PROBE_ABSENT / PROBE_ERROR / PROBE_UNREACHABLE
CONFIDENCE_HIGH / CONFIDENCE_MEDIUM / CONFIDENCE_LOW / CONFIDENCE_NONE

def slug_candidates(name, *, cased=False) -> list[str]   # uncapped, pure derivation;
                                    # the per-company cap has one owner:
                                    # discover_company's max_slugs
def probe_board(board, slug, *, session=None, expect="") -> BoardProbe
def discover_company(name, *, session=None, boards=BOARDS, max_slugs=…, budget=None) -> DiscoveryResult
def discover(names, *, session=None, …, max_requests=…, budget=None) -> tuple[list[DiscoveryResult], RequestBudget]
def format_discovery(results, budget) -> str
```

Every JSON fetcher checks the vendor's *envelope shape* before reading
postings out of it (Greenhouse/Workable/Ashby need their `jobs` list, Lever a
bare array or a wrapped one, SmartRecruiters a `content` list or a numeric
`totalFound`) and raises when a 200 lacks it — the JSON twin of Personio's
root-tag check. Without it, `{"error": "not found"}` with status 200 parsed as
zero postings: a broken slug that reads as a quiet company forever in the
daily run, and fabricated "exists, nothing open" evidence in `--discover`.
Every `fetch_<board>` raises on transport/HTTP failure; `fetch` isolates each
slug so one dead board costs that company and nothing else, and never raises.
`session=` is the only network seam, always via `util.http_get` /
`util.http_get_json`.

- **Greenhouse**: `?content=true`, description in `content` (HTML-escaped),
  date in `updated_at` / `first_published`, location in `location.name`,
  id in `id`, url in `absolute_url`. Company = the slug (title-cased) unless
  the payload carries a better name.
- **Lever**: list of postings; `text`, `hostedUrl`, `categories.location`,
  `categories.commitment`, `createdAt` (ms epoch), `descriptionPlain` or
  `description`, `id`.
- **Workable**: `?details=true`; `{name, jobs: [...]}`. `shortcode` is the
  stable id, `code` is the customer's own and is not. `description`,
  `requirements` and `benefits` are three separate HTML blocks and all three
  are concatenated — same reasoning as Lever's `lists`. `location` is
  structured parts (`city`, `region`, `country`, `countryCode` *or*
  `country_code`, `telecommuting`) and `Job.location` is assembled from them.
  `state` is checked against an allow-list of *closures* only.
- **Ashby**: `?includeCompensation=true`; `{apiVersion, jobs: [...]}`.
  `isListed: false` is skipped (a missing field is not). `secondaryLocations`
  is merged into `Job.location` the way Lever's `allLocations` is, because
  `filters.passes_location` gates on `geo.countries_of` of that one string.
  Date from `publishedAt` only — never `updatedAt`.
- **SmartRecruiters**: `?limit=100`; `{totalFound, content: [...]}`. **The
  listing carries no description**; it lives behind one request *per posting*
  at `.../postings/{id}` under
  `jobAd.sections.{companyDescription,jobDescription,qualifications,additionalInformation}.text`.
  Capped by `SMARTRECRUITERS_MAX_DESCRIPTIONS` with an INFO log line when the
  cap bites; a posting past it still reaches the digest with an empty
  description. Apply URL is built as
  `https://jobs.smartrecruiters.com/{company}/{id}` — the payload's `ref` is
  the API URL and is useless to a human.
- **Personio**: **XML, not JSON**, via `util.http_get` + stdlib
  `xml.etree.ElementTree` (which does not resolve external entities — that is
  what makes it acceptable on a third-party feed, and why no `defusedxml`
  dependency is added). Root must be `<workzag-jobs>`, because an HTML error
  page is well-formed XML and would otherwise parse into zero jobs silently.
  Each `<position>` carries `id`, `office`, `department`, `name`,
  `employmentType`, `createdAt` and titled `<jobDescription>` sections that are
  concatenated. The slug is the **subdomain**, not a path segment; a bare slug
  tries `.jobs.personio.de` then falls back to `.jobs.personio.com` on 404/410.
- **Recruitee**: JSON at `{slug}.recruitee.com/api/offers/`, addressed by
  **subdomain** like Personio. `id` is the stable id, `published_at` dates a
  posting (`created_at` only as fallback), `description` + `requirements` are
  concatenated, `country_code` is already ISO alpha-2 and is passed through,
  the one *public* structured salary in this file is kept, and
  `employment_type` rides `raw` in exactly the vocabulary
  `filters.employment_type_exclude` matches on.
- **Teamtailor**: **RSS, not JSON** — `{slug}.teamtailor.com/jobs.rss`, and a
  watchlist entry may be a full careers URL, because custom domains are the
  norm for its larger tenants (there a "slug" does not exist at all). The
  numeric id in `/jobs/<id>-…` is the stable posting id, `pubDate` the date,
  and `<remote-status>` asserts `remote` only for the fully-remote values —
  "hybrid" stays anchored to its office city.

**No new board may claim an `ats` value in `autoapply.SUPPORTED_ATS`.** Each
sets `ats=` to its own vendor name (`"workable"`, `"ashby"`,
`"smartrecruiters"`, `"personio"`, `"recruitee"`, `"teamtailor"`), which keeps
`Job.key` stable and unique per
vendor while guaranteeing `eligible()` sends the job to the digest — only
Greenhouse and Lever have been through the screener-bail work.
`posted_at` is `None` rather than a guess when no publication date exists, so
`freshness.skip_undated` stays meaningful, and `remote` is only ever asserted
positively.

- `--check` prints `OK <board>/<slug> — N postings` or a clear failure and
  returns exit code 0/1. Runnable as `python -m src.sources.ats_boards`.
  `_CHEAP_CHECK_KWARGS` skips the expensive half of a fetch per vendor.

#### `--discover` — company name to watchlist entry

`--check` answers "is this slug real?". `--discover` answers the question that
comes first and used to be a manual trawl through careers pages: **which board
is this company on, and under what slug?**

    python -m src.sources.ats_boards --discover "Glovo" "Factorial HR"

This is **the only feature that makes deliberate unsolicited requests**.
Everything else fetches boards the user chose; discovery guesses, and eight
vendors times several spellings times N companies of 404s from one IP is what a
scanner looks like. Losing access to the boards the daily run needs costs far
more than the convenience is worth, so the design is shaped by that:

- **Slug candidates come from `models.normalize_company`** — the same normaliser
  the tracker uses to decide "Spotify AB" and "spotify" are one company, which
  is the same question. Legal suffixes go (`Adyen N.V.` -> `adyen`), dotted
  initialisms collapse before the dots become spaces, `_TRANSLITERATE` handles
  the letters NFKD will not decompose (`Æther` -> `aether`). On top of that:
  `"".join` / `"-".join` / first-token for multi-word names, plus `&` -> `and`
  and the German umlaut expansion (`Bücher` -> `buecher`), which NFKD alone
  cannot produce. `normalize_text(…, casefold=False)` supplies the same
  tokenisation with the capitals kept, because **SmartRecruiters slugs are
  case-sensitive** and folding them away loses the one board a European company
  may actually be on. Only that vendor pays for both spellings.
- **The sweep is candidate-major.** Round one asks all eight boards about the
  best spelling, round two about the second-best. Consecutive requests
  therefore go to eight different hosts rather than eight in a row to one, and
  the ordinary case (the company is where you would expect) costs one round of
  probes and stops.
- **A board answering with real postings ends the sweep**; later candidates are
  not tried, and the `(board, slug)` pairs that were skipped are reported —
  "we stopped looking" and "there was nothing to find" are different claims. An
  *empty* board does not end it: zero postings is consistent both with the right
  slug on a quiet week and with a wrong slug that happens to exist.
- **The round is not cut short**, and this is the one place the economy is
  deliberately not taken. Stopping at the first hit mid-round would make the
  answer depend on the arbitrary order of `BOARDS`, and would hide the finding
  the whole module exists to surface: two vendors answering for one name, where
  one of them is a different company sharing a slug.
- **Two bounds, both audible.** `DISCOVER_MAX_SLUGS_PER_COMPANY` (4) caps the
  spellings and `DISCOVER_MAX_REQUESTS` (120) caps the run; each logs and prints
  exactly what it dropped, for the reason `SMARTRECRUITERS_MAX_PAGES` does. A
  cap that bites silently turns "we stopped guessing" into "this company is on
  no board", which is the one reading that must never be given.
- **One probe is one request — enforced, not hoped.** `_DISCOVER_PROBE_KWARGS`
  passes `retries=1` to every fetcher: `util.http_get`'s three-attempt policy
  is the right insurance for the daily run, where a transient failure costs
  real jobs, but a probe that misses costs one commented-out suggestion — and
  the default would re-ask a host that just said 429, twice, under a cap whose
  whole point is not looking like a scanner. A 404 was never retried anyway.
  The same kwargs turn off descriptions and hold SmartRecruiters to one
  listing page — the offset walk is right for the daily run and would
  otherwise turn one probe into twenty against a budget that thinks it spent
  one. The single residual multiplier is Personio's documented two-host
  fallback: a `.de` miss is retried once on `.com`, so a Personio miss is two
  requests against the budget's one.
- **Five answers, never collapsed**: `found` (postings), `empty` (reachable,
  nothing open — which demands the vendor's envelope shape, so a 200 error
  object cannot fake it), `absent` (404/410 — the answer that lets a board be
  ruled *out*), `error` (403/429/5xx, or a body that is not a board),
  `unreachable` (no answer at all). A confidence, not a verdict, is derived
  from them, and **only `high` prints uncommented**: `high` means every
  question that was asked got a real answer (found/empty/absent) and exactly
  one board had postings — spellings a hit left unasked are printed as
  untried, never held against it. `medium` is one clear answer with a named
  hole in the evidence (a probe nobody answered, or an empty twin of the
  winning slug); it, and everything below it, is printed **commented out** so
  that pasting the block can never install a guess — one board of eight timing
  out must never produce an installed slug.
- **Where the vendor publishes its own name** (Workable per account,
  SmartRecruiters per posting) it is compared against the name asked for,
  leniently — containment either way is agreement, spaces ignored, so
  "FactorialHR" and "Factorial HR" are one company. Workable's name lives on
  the account envelope and is read even off an *empty* board (the one case
  with no other company evidence); a mismatch there is evidence against the
  slug, said in the note. The four boards that publish no name never mint a
  mismatch — comparing `company_from_slug` back to the name it was derived
  from would be a constant compared to a constant — and a hit on the *bare
  first token* of a multi-word name on those boards is capped at `medium`,
  because a generic one-word slug with no name to check is exactly what a
  squatter looks like.
- **The paste block groups by board, never by company.** One `greenhouse:` key
  with every found company under it: per-company keys would emit the same
  top-level key twice the moment two companies land on one board, and YAML
  keeps only the last — a block that installs one company and silently
  vanishes the other. The block also says, in a comment, to merge into any key
  the watchlist already has; `Config.load`'s duplicate-key refusal backstops
  whoever pastes without reading. Every physical line of the commented half is
  comment-prefixed structurally, so a line break inside quoted text (an error
  message in a note, a company name) cannot smuggle an uncommented line in.
- **It never writes `watchlist.yaml`.** It prints. The file is the one the user
  curates by hand, and a tool that rewrites it will eventually eat something
  they wrote — while a wrong guess that stays on the terminal costs nothing.
- Exit code 0 only when every name graded `high`; medium, ambiguous,
  low-confidence, not-found and cap-truncated all exit 1. `--discover` given
  alongside `--check` runs alone and says on stderr that `--check` was
  ignored.

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
def build_prompt(job: Job, cv_markdown: str, applicant: dict, *, rules="") -> str
def parse_score(payload: dict) -> Score
def score_job(job, cv_markdown, config, *, client=None) -> Score
def score_jobs(jobs, cv_markdown, config, *, client=None, errors=None) -> list[ScoredJob]
```
Model returns `{"score": 0-100, "verdict": str, "reasons": [str],
"strengths": [str], "gaps": [str]}`. `parse_score` clamps to 0-100, coerces
numeric strings, tolerates missing lists. A failed call yields
`Score(value=0, error=...)` and the job goes to the digest with a warning —
never silently dropped. Respects `scoring.max_jobs`.

`rules` is `_candidate_rules(config)` rendered into the prompt's CANDIDATE
block: `scoring.candidate_context` (positioning the CV cannot state),
`scoring.positive_signals` (themes that weigh a score up) and
`scoring.score_caps` (`{when, cap}` ceilings the model must respect, naming
the cap that fired in `reasons`). All three are **prompt-only** — they never
move `scoring.threshold` or `scoring.max_jobs`.

### `src/tailor.py`
```python
def build_cv_prompt(job, cv_markdown, applicant) -> str
def build_cover_prompt(job, cv_markdown, applicant) -> str
def tailor_job(scored, cv_markdown, config, *, client=None, out_dir=None) -> ScoredJob
def tailor_jobs(scored_jobs, cv_markdown, config, *, client=None, errors=None) -> list[ScoredJob]
def artifact_dir(job, base_dir) -> Path      # <base>/applications/<slug>-<key>
def validate_tailored_cv(base_md, tailored_md, applicant=None) -> tuple[bool, str]
def validate_cover_letter(cover_md, *, base_md, job=None, applicant=None)
                                       -> tuple[bool, str, list[str]]  # ok, reason, flags
def unanchored_numbers(base_md, candidate_md, extra_md="") -> list[str]
```
The prompts must forbid inventing employers, dates, degrees, or metrics not
present in the base CV — reordering/rephrasing/emphasis only. Prompt rules are
a request, not a guarantee, so the output is validated afterwards:
`validate_tailored_cv` rejects an empty document, a lost applicant name, an
unfilled placeholder, a document past twice the base CV's length, and —
hard-number grounding — any percent or year the base CV never stated
(formatting is normalised first, so "10,000" and "10k" are one fact, and a
duration derivable from anchored year pairs is arithmetic, not invention); a
rejected CV falls back to the base. `validate_cover_letter` gates on the same
mechanically-certain failures — the posting is allowed as an extra number
anchor there, since a letter may quote the ad — and everything judgement-y
(overlength, never naming the company) is a flag in `status_detail`, never a
block. Writes `cv.md` + `cover_letter.md` into the artifact dir and fills
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
def build_context(scored_jobs, stats, config, *, now=None, tracker=None) -> dict
def render_html(context) -> str
def write_digest(scored_jobs, stats, config, *, now=None, tracker=None) -> Path
def posting_age_days(posted_at, now=None) -> float | None   # None, never 0
def relative_time(dt, now=None) -> str                       # "3h ago" / a date
RELATIVE_DAYS_LIMIT = 60      # past this, relative_time prints a calendar date
```
`tracker=` is optional and read-only — it supplies the sighting history behind
the repost flag. The ghost-job signal is a **flag, never a filter**: it never
drops, hides, reorders or downweights a card. A card renders with or without a
tracker, differing by one advisory line — and a tracker that raises, is
missing the method, or answers with something that is not a number costs that
same one line and nothing else. (It did not always: the `try` used to wrap
only the call, so a stub returning `"40"` raised `TypeError` all the way into
`build_context`'s "skipping unrenderable digest item" and *deleted the card*.)

That guarantee is enforced by comparing the funnel against the cards on the
page, not by comparing `stats` to itself — `build_context` copies `stats`
through untouched, so a "the funnel is unchanged" assertion over a hardcoded
`RunStats` is a constant compared to a constant and catches nothing.

`posting_age_days` returns `None` for an undated posting — a third state,
neither 0 nor "fresh" — and never falls back to `first_seen_at`, which is a
fact about our cron schedule rather than about the employer. Day counts on the
card are rounded half up, never truncated: `int()` printed a 23-hour-old
posting as `0` days and 30.5 days as `30`, next to an untruncated threshold.

Advisory notes render `p.advisory` (amber, `--warn`) and real failures render
`p.alert` (red, `--bad`). They were the same class, which made "this posting
may be old" indistinguishable from "your scorer is down".

Jinja2 template at `src/templates/digest.html.j2`, self-contained (inline
CSS, no CDN). The page opens with a per-source **health table** — source ·
fetched · kept · new today · ok/degraded/error · last OK — because the
morning a Tier 2 endpoint silently dies, `fetched 0 · degraded` on a source
that averaged forty is the story, and without it the digest just looks quiet
(`degraded` = errored-but-delivered, or an unexplained zero against the
source's own recent average; "last OK" comes from the tracker's run history).
Then the outcome sections: **Needs your click** (digest status, sorted by
score desc), **Submitted — unconfirmed**, **Auto-applied**, **Dry run — check
these**, **Below threshold**, **Run stats & errors**. Every item shows score,
`score_reasons`, gaps, company/title/location/posted, apply link, and links
to the tailored files. Must escape untrusted text (job titles/descriptions
come from the internet).

### `src/main.py`
```python
def build_parser() -> argparse.ArgumentParser
def run_pipeline(config, *, tracker=None, now=None, llm_client=None) -> tuple[list[ScoredJob], RunStats]
def main(argv=None) -> int
```
Flags: `--no-browser`, `--dry-run/--no-dry-run`, `--config`, `--watchlist`,
`--limit N`, `--source NAME` (repeatable), `--skip-apply`, `--no-llm`,
`--verbose`, `--validate-only`. Exit codes: 0 ok, 1 config invalid, 2
unexpected error, 4 ran-but-alerted (only with `notify.exit_nonzero`), 130
interrupted. Prints a compact summary and the digest path.

**Jobs are sorted newest-first before anything truncates them.** Both
`--limit` and `scoring.max_jobs` slice from the front of the list, and until
this sort they sliced in *fetch* order — `_fetch_all` extends in source order,
`apply_filters` and `_gate_on_tracker` append in input order — so board order
decided who got scored. With the window at 72 hours, 40 postings two days old
ahead of 5 posted two hours ago spent the entire ceiling on the older ones and
scored none of the freshest. Undated postings sort **last**, in fetch order:
`skip_undated` ships true, so they only get here when the user has turned it
off and accepted some staleness, and a board endpoint returning every open
requisition would otherwise evict every provably-fresh posting from the cap.
