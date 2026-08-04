# Testing

**958 tests, ~37s, fully offline.**

```bash
pytest -q                      # the whole suite, offline, no API key, no browser
pytest -q tests/test_db.py     # one module
pytest -q -k "double_apply or inspect_form"
pytest --cov=src --cov-report=term-missing
```

| File | Tests | Defends |
|---|---:|---|
| `test_geo.py` | 134 | US/EU city collisions — the expensive mistake |
| `test_autoapply.py` | 133 | **the screener-bail guarantee** |
| `test_ats_boards.py` | 71 | Greenhouse/Lever payload reality |
| `test_util.py` | 59 | retries, HTML flattening, every ATS date shape |
| `test_filters.py` | 58 | whole-word matching, dedupe richness |
| `test_linkedin_email.py` | 52 | leniency under LinkedIn template change |
| `test_scoring.py` | 49 | a job is never silently lost |
| `test_models.py` | 45 | job identity — the tracker's primary key |
| `test_llm.py` | 43 | JSON recovery, retry policy |
| `test_tailor.py` | 42 | **the anti-fabrication guarantee** |
| `test_main.py` | 42 | the pipeline end to end, offline |
| `test_db.py` | 40 | **the double-apply guarantee** |
| `test_health.py` | 39 | a quiet day vs a broken pipeline |
| `test_digest.py` | 36 | escaping, and legibility of failure |
| `test_config.py` | 36 | env-beats-file, validate-everything-at-once |
| `test_adzuna.py` | 32 | snippets, duplicates, key redaction |
| `test_notify.py` | 31 | no shell injection; one channel's death is contained |
| `test_pdf.py` | 16 | every half-failure of the user's hook |

The suite runs with **PyYAML, Jinja2 and pytest** installed and nothing else.
`anthropic`, `playwright`, `googleapiclient`, `requests` and `reportlab` are
imported lazily inside the functions that need them, so a bare checkout can be
verified before any of the heavy dependencies are installed.

## The rule that shapes everything

**Every boundary has exactly one injectable seam, and tests use only that.**

| Boundary | Seam | Fake |
|---|---|---|
| HTTP | `session=` | `FakeSession` — route by substring or regex, returns responses, raises exceptions, records every call |
| Anthropic | `client=` | `FakeAnthropic` — canned strings, `FakeMessage`s, callables or exceptions; records model/system/prompt |
| Gmail | `service=` | `FakeGmail` — the four-deep `users().messages().list().execute()` chain |
| Browser | `page=` / `browser=` | `FakePage` / `FakeBrowser` — implements exactly the documented page protocol, with a mini CSS matcher |
| Clock | `now=` | a fixed `NOW = 2026-08-04T09:00Z`; no `freezegun`, no monkeypatched `datetime` |
| Filesystem | `tmp_path` | `write_config()` builds a config, watchlist and CV rooted in the tmpdir |

There is deliberately **no monkeypatching of private functions** to make a
test pass. If a test would need one, that is a design bug in `src`, not a
missing fixture — the one exception is `stub_boards` in
`tests/test_ats_boards.py`, which intercepts `_fetch_board` for the CLI tests
because `main()` takes no `session=` argument.

Everything lives in `tests/conftest.py`. Start there.

## Where the effort is concentrated

The suite is not uniform, on purpose. Three claims, if false, make this tool
actively harmful rather than merely unhelpful, and they get the adversarial
treatment:

### 1. "It never applies twice" — `tests/test_db.py`

- only `applied` blocks a future application; `dry_run`, `digest`,
  `apply_failed` and `scored_below` deliberately do not (a dry run submits
  nothing, so blocking on it would prevent the real application);
- an `applied` row can never be downgraded by a later run recording anything
  else — this is the exact regression that would produce a duplicate;
- `HANDLED_STATUSES` is asserted to cover every `ApplyStatus`, so adding a
  status without classifying it fails a test rather than silently re-showing
  jobs forever;
- a repeat sighting never overwrites a known `posted_at` with `NULL`, which
  would make a dated job look undated and get it dropped from then on.

### 2. "It will never answer a question for you" — `tests/test_autoapply.py`

Every screener shape is thrown at `inspect_form` — selects, textareas, radio
groups, sponsorship and visa questions, salary expectations, notice period,
"how did you hear about us", EEO/demographic fields, unrecognised required
inputs — and each must **bail**. Ambiguity resolves to bail. Separately: the
dry-run path is asserted never to click submit, and `eligible()` is asserted
to refuse anything that is not Greenhouse or Lever, under the score floor, or
missing its tailored PDF.

### 3. "It can't invent things about you" — `tests/test_tailor.py`

Prompt-level enforcement is not a guarantee, so both layers are tested: the
anti-fabrication clauses are asserted present in the prompts, *and*
`validate_tailored_cv` is tested against a tailored CV that grew a job the
base CV never had.

## Everything else

Correctness rather than safety, but still adversarial about the cases that
actually occur in production payloads:

- **`test_models.py`** — job identity. `Job.key` is the tracker's primary
  key, so instability breaks the double-apply guarantee and collisions merge
  two jobs into one record. Covers legal-suffix drift (`Spotify AB` vs
  `spotify`), retitled requisitions, and cross-source `dedupe_key` collapse.
- **`test_geo.py`** — the US/EU city collision, which is the expensive
  mistake: Berlin CT, Birmingham AL, Dublin OH, Athens GA, Naples FL, Vienna
  VA, Paris TX, Cambridge MA. Plus `DE` meaning both Germany and Delaware,
  and the invariant that matching is never a naive substring test (`IN` is
  inside `Berlin`).
- **`test_filters.py`** — that `intern` rejects "Backend Intern" but not
  "International Sales Manager"; that an empty `title_include` means "allow
  everything" and not "match nothing"; that rejection categories are stable
  slugs, since the digest groups on them.
- **`test_ats_boards.py`** — Greenhouse's entity-escaped `content` unescaped
  exactly once, `first_published` preferred over the freshness-inflating
  `updated_at`, Lever's `lists` blocks kept (that is where requirements
  live), and one dead board never costing more than that company.
- **`test_adzuna.py`** — snippet-only descriptions flagged, duplicate results
  collapsed, a rejected key abandoning its country instead of collecting the
  same 401 once per query, and API keys redacted out of error strings before
  they reach the digest or `run.log`.
- **`test_linkedin_email.py`** — leniency under template change. A redesign
  that strips the company/location markup must still yield titles and links.
  Unpadded base64url is tested at all four padding offsets.
- **`test_llm.py`** — `extract_json` against fenced blocks, prose padding,
  nested braces, braces inside string literals and trailing commas; retry on
  transient errors and *no* retry on auth failures.
- **`test_scoring.py`** — score clamping and coercion (`"82/100"`), and that
  a failed scoring call sends the job to the digest with a warning rather
  than dropping it silently.
- **`test_digest.py`** — autoescaping. Job titles and descriptions are
  attacker-controllable text from the open internet, so
  `<img src=x onerror=alert(1)>` must render escaped. Also: every section
  renders with zero items, and the whole page renders with zero jobs, so
  "quiet day" is distinguishable from "pipeline broken".
- **`test_main.py`** — the pipeline end to end with a fake LLM, an in-memory
  tracker and no network, including the funnel counts and the CLI's exit
  codes.
- **`test_health.py`** — every alert comes in a pair: a case that must fire it
  and a neighbouring case that must stay silent. Zero jobs from every board is
  an alert; two jobs instead of forty is a quiet Tuesday and is not. A
  Friday→Monday gap is excused; a Saturday→Tuesday gap of the same length is
  not, because it really did miss Monday.
- **`test_notify.py`** — the `command` channel runs a subprocess with text
  built from job titles, so a posting called `; rm -rf ~` is asserted to stay
  an argument. And one channel's failure never stops the others: a notifier
  that breaks the run it was meant to warn about is worse than none.

## Markers

```bash
pytest -m "not network"    # the default; nothing in the suite hits the network
```

`@pytest.mark.network` exists for tests you may want to add against the real
Greenhouse/Lever endpoints. None are shipped: a suite that fails because
someone else's API had a bad afternoon trains you to ignore failures.

## Adding a test

1. If it needs a new seam, add the seam to `src` — do not reach past it.
2. Put the fake in `conftest.py` if more than one module will want it.
3. Use `now=NOW`, never the real clock.
4. Say *why* in the docstring when the case is non-obvious. "This is the
   substring bug that deletes every International Sales role" is worth more
   to the next reader than the assertion itself.
