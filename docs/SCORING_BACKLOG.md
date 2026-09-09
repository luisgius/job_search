# Durable scoring backlog

The pipeline keeps scoring work in SQLite independently of `applications` and
`submit_attempts`. This addresses cap overflow aging out before scoring and scorer
failures being mistaken for handled application outcomes. It uses the existing
explicit pipeline and tracker; there is no scheduler, new dependency, or model call
outside a normal pipeline run.

## Admission and processing

Freshly fetched jobs pass normal deduplication, all hard filters, and the tracker
reminder gate before admission. The queue stores a normalized job snapshot,
including description and raw requirement/provenance metadata, before either
`--limit` or `scoring.max_jobs` truncates the scoring batch. Application and
submit-attempt protections also apply at admission. If eligibility cannot be
established because the filter stage fails, durable admission stops for that run.

Each later run processes prior queued work before new admissions. Prior batches
are FIFO by admission time; new jobs within a batch retain newest-first order,
with undated jobs last. This deliberately gives overflow a chance despite a
continuous stream of new arrivals. Backoff or unavailable evidence does not
consume the run's scoring budget. Both new and queued jobs share the same
`scoring.max_jobs` and `--limit` ceilings.

A queued job must appear in the current deduplicated source result before it can
be scored. Its current description, location and requirement metadata replace the
saved evidence, while its original posting date, admission time and expiry remain
unchanged. All current hard filters are rerun, with only the admission-age ceiling
relaxed. Undated-posting policy still applies. Passing the original freshness gate
once does not exempt a queued job from changed language, title, employment-type,
location, keyword or description requirements.

A source returning no item is **not evidence of closure**: some feeds are partial,
some adapters absorb failures, and a source may be disabled. That job stays pending
with `availability unknown` and `current source observation needed`, without
spending an attempt or moving its observation timestamp. Saved-only evidence never
enters tailoring or auto-apply. A later current observation can recover the job
within its queue lifetime. This is conservative for alert-only or one-shot feeds:
without a new observation they eventually expire, and require manual review.
Current source presence is the availability evidence used here; this change does
not add a separate live endpoint probe or claim definitive ATS closure detection.

## Bounds and retries

Defaults are in `src/config.py`; existing configurations inherit them. Optional
settings under `scoring` are:

| Setting | Default | Accepted range | Meaning |
| --- | ---: | ---: | --- |
| `backlog_max_jobs` | 1000 | 0–10000 | Maximum active queue admissions; zero pauses admission |
| `backlog_max_age_days` | 7 | 1–30 | Fixed lifetime from admission, not from the posting date |
| `retry_max_attempts` | 3 | 1–10 | Total scoring attempts, including the initial attempt |
| `retry_base_hours` | 1 | 1–168 | Initial retry delay, doubled after each failed attempt |

At the defaults, attempts can occur at admission, after one hour, then after two
more hours, provided the normal pipeline runs and the job is observed again.
Attempts are recorded **before** the scorer runs. An interrupted process consumes
an attempt and leaves the next retry time durable. A provider outage, invalid
model response, missing scorer result, or unexpected batch exception therefore
cannot restart an unbounded retry loop. Existing provider fallback chains remain
inside each attempt; this is a bound on scoring attempts, not on individual HTTP
requests or fallback model calls.

`scoring.max_jobs: 0` and `--limit 0` retain admitted jobs without scoring them.
`--no-llm` still shows available eligible jobs unscored (subject to `--limit`),
retains their queue records, and spends no attempts. It does not mark scoring as
handled. No-LLM runs continue normal queue expiry/revalidation. These modes do not
freeze TTL. Changing lifetime configuration does not extend previously admitted
jobs; reducing the attempt bound is honored on the next run. Lowering capacity
stops new admission until active work drains; it does not evict admitted work.

An individual evidence snapshot is limited to 256 KiB of UTF-8 JSON. Oversized
snapshots are refused visibly rather than silently truncated. Queue capacity
limits retained evidence; expired, exhausted, ineligible and protected work keeps
only a small metadata/reason tombstone and releases its snapshot. Tombstones are
not automatically readmitted; their metadata follows the existing tracker history
retention model rather than deleting the history of why processing stopped.

When capacity is full, new jobs are **not queued** and a digest error names them.
They can be reconsidered on a later normal fresh fetch, but have no durability
guarantee until admitted. This is an explicit bounded-load limit, distinct from
scoring-cap overflow, which is retained. Expiry means the processing window ended,
not that the employer closed the job.

## Application safety and visibility

Pending and failed scoring cards do not write a `DIGEST`, zero score, or other
application outcome. Successful scored outcomes use the normal persistence path;
the queue is cleared only after that persistence succeeds. A persistence failure
leaves work queued. A crash between outcome persistence and queue deletion is
resolved on the next run by respecting the already-recorded application outcome.

An application outcome recorded after admission takes precedence over retry work.
Applied/unconfirmed submissions, recorded submit attempts and already-applied
similar roles prevent backlog processing. The normal auto-apply eligibility,
score, document, duplicate and write-ahead-submit checks still execute for current,
revalidated scored jobs. Scoring retry never clears an application or submit
attempt, and it never invokes a separate application retry path.

The existing digest error area shows the pending count, the count needing current
source evidence, and up to five pending job labels and reasons. Queue-full,
expired and exhausted transitions are visible as errors; individual scoring
failures explain the next retry time or exhaustion. Recovered cards say when they
were queued and that the source was observed this run. Original publication dates
remain intact. Detailed state is available read-only through the tracker:

```sql
SELECT j.company, j.title, j.posted_at,
       q.state, q.queued_at, q.last_observed_at, q.expires_at,
       q.attempts, q.next_attempt_at, q.detail
FROM scoring_backlog AS q JOIN jobs AS j ON j.key = q.key
ORDER BY q.queued_at;
```

The runtime migration adds schema v4 when a tracker is next opened. Existing
application and submit-attempt records are preserved. Historical scorer failures
already persisted as `DIGEST` have no retained scoring snapshot/error field to
reliably distinguish them from genuine human-review outcomes, so this migration
does not reinterpret or automatically requeue those old rows. Calls without a
`Tracker` retain their existing non-durable behavior.

## Offline verification

`tests/test_scoring_backlog.py` exercises real scoring/pipeline functions with an
injected fake client and temporary SQLite databases. It covers overflow beyond
freshness, restart, current evidence replacement with original timestamps,
outage recovery and backoff, unexpected batch errors, interrupted attempts,
exhaustion, TTL expiry, capacity and evidence size, zero budgets, no-LLM,
application persistence failure, changed eligibility, duplicate/submit attempts,
application-state preservation, replay-only exclusion from auto-apply, schema
migration and configuration validation.

Existing tracker schema assertions now follow the migration count. The existing
successful-score reminder test supplies valid score responses for both jobs;
previously its fake accidentally returned CV Markdown for the second score and
asserted that this malformed score would be permanently treated as handled.

Validation uses the existing `.venv` (Python 3.9.6). It remains below the project's
declared Python >=3.10 requirement; the pre-existing urllib3/LibreSSL warning also
remains. No environment rebuild, dependency install, paid model call, production
database access, email, application submission, or commit was performed.

Tracker API added by this change: `enqueue_scoring` admits bounded evidence;
`pending_scoring` and `get_scoring` read work; `observe_scoring` refreshes observed
inputs while preserving publication date; `begin_scoring` records an attempt;
`scoring_detail` updates evidence/retry needs; `stop_scoring` retains a reason and
releases the snapshot; `complete_scoring` removes successfully persisted work.
Admission captures the prior application status/update timestamp so an unchanged
old outcome beyond the reminder window does not suppress retries of a rescore.

Worker validation on 9 September 2026:

- Final focused command: `.venv/bin/python -m pytest -ra tests/test_scoring_backlog.py tests/test_db.py tests/test_main.py tests/test_scoring.py tests/test_config.py tests/test_models.py tests/test_edge_match.py tests/test_autoapply.py` — **558 passed**, one existing LibreSSL warning, 2.89 seconds. The new backlog module contributes **32 cases**.
- Interim complete offline command: `.venv/bin/python -m pytest -ra` — **2,131 passed, 1 skipped, 69 network tests deselected, 1 expected failure**, one existing warning, 62.79 seconds. This run preceded the final reminder-window baseline refinement and explicit missing-result exhaustion visibility regression; both are included in the final focused result above. Coordinator integration validation should run against the settled combined diff.
- `git diff --check` passed. `graft build` refreshed the ignored local graph after the final code changes.

Worker-owned diff: `src/config.py` adds policy defaults and validation; `src/db.py`
adds schema v4 and queue operations; `src/main.py` admits, revalidates, budgets,
retries and reports work independently of application outcomes; `tests/test_db.py`
and `tests/test_main.py` contain the compatibility corrections described above;
`tests/test_scoring_backlog.py` and this document are new. No changes were needed
to `src/models.py`, `src/scoring.py` or `config.yaml`. Other workers' evaluation and
skills/tooling files were not edited. Graft query outputs retained in this
worker's context report at least 886,049 estimated tokens saved; this is an
overlapping whole-file-baseline estimate, not unique measured context reduction.

## Storage-failure and transient-filter recovery follow-up

Queue storage errors are contained within the pipeline and included in
`stats.errors` and the rendered digest:

- A queue-membership read failure stops admission for that batch. A preparation
  failure holds that batch from scoring instead of falling back to treating the
  postings as fresh. Already committed queue evidence remains in SQLite.
- A `begin_scoring` failure skips that specific job. Other jobs with confirmed
  attempt writes can proceed within the original cap. If the write committed
  before the error was raised, its attempt/backoff remains in force; no model call
  is made on the strength of an unconfirmed write.
- A result-bookkeeping or post-score evidence-read failure holds the scoring
  batch from tailoring, automatic application and application-outcome persistence.
  Completed in-memory scores can still be displayed, with a message that queue
  storage is uncertain and automatic application was withheld. Successful work
  may therefore need scoring again on a later run, within the persisted bounds.
- Final diagnostics cannot abort digest rendering. If labels cannot be read, the
  queue key is shown with the reason; if the pending list cannot be read, the
  digest says diagnostics are unavailable rather than claiming an empty queue.
- Outcome-persistence and queue-cleanup errors are also digest-visible. When an
  application outcome was already persisted but queue cleanup fails, that outcome
  continues to protect the job on the next run. A late persistence/diagnostics
  error cannot undo actions already completed through the normal application
  stage; its existing submit-attempt protections remain intact.

These guards do not add in-process SQLite retries or reset attempt counts, TTL,
application state or submit attempts. A later normal run retries retained work
when storage and current evidence are available. Work that never successfully
entered the queue cannot be guaranteed durable; admission errors name that loss
of persistence explicitly. SQLite initialization/migration failures before
`run_pipeline`, an unavailable output filesystem, and invalid/missing configured
CV inputs remain outside this queue-storage recovery boundary.

A structured `filter_error` during queued revalidation now means **pending
revalidation**, rather than permanent ineligibility. Evidence, admission time and
expiry are retained and no scoring attempt is spent. A later successful filter
pass can recover the job after a restart. A genuine hard-filter rejection still
stops it as ineligible.

Follow-up validation on 9 September 2026:

- `.venv/bin/python -m pytest -ra tests/test_scoring_backlog.py tests/test_main.py tests/test_db.py tests/test_autoapply.py tests/test_filters.py` — **396 passed**, one pre-existing LibreSSL warning, 1.93 seconds.
- Added **20 fault/recovery cases**, covering admission reads and writes, preparation and duplicate/history reads, before/after-commit attempt failures with restart, failed result bookkeeping, pre-application evidence reads, final diagnostics/cleanup, and a transient language detector followed by recovery across restart.
- `git diff --check` passed; `graft build` refreshed the ignored local graph. Follow-up Graft calls reported **311,483 estimated tokens saved**, using their overlapping whole-file baselines.
- Only `src/main.py`, `tests/test_scoring_backlog.py` and this document were changed in this follow-up. The coordinator owns final combined-suite validation. Python 3.9.6 remains below the declared >=3.10 requirement; no environment rebuild, dependency install, production DB operation, external call, submission or commit was performed.
