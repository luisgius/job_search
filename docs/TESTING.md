# Testing

**1684 tests, ~43s, fully offline** (plus 44 network-only contract tests,
deselected by default).

```bash
pytest -q                      # the whole suite, offline, no API key, no browser
pytest -q tests/test_db.py     # one module
pytest -q -k "double_apply or inspect_form"
pytest --cov=src --cov-report=term-missing
```

| File | Tests | Defends |
|---|---:|---|
| `test_geo.py` | 178 | US/EU city collisions — the expensive mistake |
| `test_autoapply.py` | 134 | **the screener-bail guarantee** |
| `test_edge_apply.py` | 111 | the apply leg against real 2026 form markup |
| `test_ats_boards.py` | 108 | Greenhouse/Lever payload reality; slug + `--check` for all six |
| `test_edge_fetch.py` | 86 | the shapes a real European job week produces |
| `test_edge_match.py` | 76 | prompt injection; a model reply is untrusted input |
| `test_main.py` | 60 | the pipeline end to end, offline |
| `test_util.py` | 59 | retries, HTML flattening, every ATS date shape |
| `test_filters.py` | 58 | whole-word matching, dedupe richness |
| `test_models.py` | 53 | job identity — the tracker's primary key |
| `test_linkedin_email.py` | 52 | leniency under LinkedIn template change |
| `test_scoring.py` | 52 | a job is never silently lost |
| `test_llm_openrouter.py` | 50 | provider equivalence — same behaviour either way |
| `test_config.py` | 48 | env-beats-file, validate-everything-at-once |
| `test_health.py` | 44 | a quiet day vs a broken pipeline |
| `test_llm.py` | 43 | JSON recovery, retry policy |
| `test_tailor.py` | 42 | **the anti-fabrication guarantee** |
| `test_db.py` | 41 | **the double-apply guarantee** |
| `test_workable.py` | 69 | split description/requirements/benefits; assembled locations |
| `test_live_contract.py` | 44 | **the live APIs still emit what we parse** (network-only) |
| `test_live_contract_policy.py` | 22 | **that file skips only when it should** — offline |
| `test_ashby.py` | 40 | `secondaryLocations`; unlisted drafts stay hidden |
| `test_smartrecruiters.py` | 48 | the two-call shape — and its cap |
| `test_digest.py` | 36 | escaping, and legibility of failure |
| `test_adzuna.py` | 32 | snippets, duplicates, key redaction |
| `test_personio.py` | 52 | XML, subdomain slugs, no external entities |
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
- **`test_workable.py` / `test_ashby.py` / `test_smartrecruiters.py` /
  `test_personio.py`** — the four European boards, one file each. Every file
  covers the same seven shapes (happy path, null location, null date, titleless
  posting, junk list entries, a 404, and one dead board not costing the others)
  plus what is peculiar to that vendor:
  - **Workable** assembles `Job.location` from structured parts, so a dropped
    country is an unresolvable job; and `description` / `requirements` /
    `benefits` are three fields, of which the second is the one that decides
    scores. Every field is read under all of its known spellings — country,
    region and the remote flag alike — because the widget and v3 APIs disagree
    and reading one spelling empties the field on the payloads that use the
    other. Extra offices are merged in the way Lever's `allLocations` are: a
    posting open in San Francisco *and* Valencia reads as American from
    `location` alone, and the US veto deletes it. And a posting is dated by
    `published_on`, never `created_at` — the latter is when the *draft* was
    opened, and drafting weeks ahead makes a new req look a month stale.
  - **Ashby** hides drafts behind `isListed: false` — and a *missing*
    `isListed` must not be read as `false`, or the board empties the day they
    stop sending it. `secondaryLocations` is merged in, for the same reason
    Lever's `allLocations` is: a Valencia+Berlin role must not be pinned to one.
    The description is asserted across every block and against the
    `descriptionSocial` teaser, because scoring a one-line OG blurb produces a
    perfectly reasonable-looking number and no error at all.
  - **SmartRecruiters** is the two-call vendor: the listing has no description
    at all. The cap on per-posting detail calls is asserted, as is the property
    that a failed detail call costs the description and *not* the job. The
    listing is *paged*, and the offsets are followed to the end — one page is
    100 postings, and a company with 250 roles used to contribute exactly 100
    and lose the rest with nothing above DEBUG to say so.
  - **Personio** is XML and is addressed by subdomain, so the generic
    "drop the host, keep the path segment" slug rule is exactly backwards. An
    HTML error page is well-formed XML, so the root tag is checked — otherwise
    a login wall parses into zero jobs and reads as a quiet company. A
    namespaced feed is read all the way down and not merely admitted at the
    root: a gate that lets a document in and then cannot read its children
    turns a loud "this is not a job feed" into a quiet zero. External
    entities are asserted *not* to resolve, which is why the stdlib parser is
    enough and no `defusedxml` dependency exists.
  - All four are asserted to date a posting by its *publication* field and
    never by a modified one: `updated_at` moves on any edit, so one typo fix
    would make a three-month-old req today's news, invisibly.
  - All four are asserted never to claim an `ats` value in
    `autoapply.SUPPORTED_ATS`, and `detect_ats` is asserted to reject every URL
    they produce. Only Greenhouse and Lever have been through the screener-bail
    work; anything else must reach a human.
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

## Markers — and the limit of an offline suite

```bash
pytest -q                  # the default; excludes network (see pyproject addopts)
pytest -m network -q       # the live contract tests, run deliberately
```

**Be clear about what the offline suite proves.** It proves our parsers handle
the payloads in `tests/fixtures/`. It does not prove the live APIs still emit
those payloads. If Greenhouse renamed a field tomorrow, the fixture and the
parser would agree with each other and both be wrong, and every test would
stay green.

`tests/test_live_contract.py` closes that gap and is the one place that talks
to the real internet. It asserts the specific things the parsers bet on:

- the fields each parser reads are still present, for all six boards;
- Greenhouse `content` is still *double* entity-escaped (we unescape exactly
  once — if they stop, that unescape starts corrupting real text);
- `first_published` still exists, so freshness does not silently fall back to
  the inflated `updated_at`;
- Lever `createdAt` is still a **millisecond** epoch (seconds would date every
  posting to 1970 and drop them all as stale, silently);
- Lever still splits requirements into `lists`;
- Workable's `?details=true` is still what produces a description at all,
  `requirements` is still a separate block, `published_on` still exists (without
  it, freshness falls back to the draft date and ages every posting), and
  *something* still names the extra offices a posting is open in — the key name
  is a hypothesis, so the test names every spelling the parser accepts and
  reports which is real;
- Ashby's `secondaryLocations` entries still carry a readable name, and
  `publishedAt` still exists (without it, `skip_undated` drops the whole board);
- the SmartRecruiters *listing* still carries no description — i.e. the
  expensive per-posting detail call is still necessary — the detail payload
  still exposes `jobAd.sections.*.text`, and `?offset=` still advances the
  window (if it stopped, every page would be page one and a large employer
  would be read as its first hundred roles forever);
- Personio still serves `<workzag-jobs>` XML with `<jobDescription>` sections,
  its colon-less `+0200` offsets are still parseable, and `?language=` is still
  accepted;
- no live board produces a URL `autoapply.detect_ats` would accept;
- and the offline fixtures do not claim fields the live API no longer returns —
  excepting a short, named list of fields carried *on purpose* to prove the
  parser ignores them (`updated_at`, `updatedOn`, `updatedAt`,
  `descriptionSocial`), since "we never date a posting from `updated_at`" is
  unprovable against a payload that has no `updated_at` in it.

> **Read this before trusting the four European boards.** Their parsers and
> their fixtures were written from vendor documentation on a machine with **no
> outbound network** — not one byte of a real response was ever seen. The
> offline suite proves the parsers agree with those fixtures. If a field name
> is wrong, the fixture and the parser are wrong *together* and every test
> stays green. `pytest -m network -q` is the only thing that can settle it, and
> the fixture-vs-reality tests in that file are the ones to read first.

It is excluded from the default run on purpose — a suite that goes red because
someone else's API had a bad afternoon trains you to ignore failures — and each
test **skips** rather than fails when the network is simply unreachable. A few
cheap probes decide that for the whole file, so an offline `pytest -m network`
finishes in about a second instead of spending every test's retry budget
rediscovering it. Run it on setup, and again whenever a source mysteriously
returns nothing.

### A skip in this file is the dangerous outcome

Everything above is gated twice — once by the session probe, once by the helper
that makes the request — and both gates can skip. **A skip prints green**, so a
bug in either gate does not look like a bug; it looks like a pass, and it
disarms every test in the file at once. Two rules, and
`tests/test_live_contract_policy.py` proves them offline, in the default run,
on the machine where nobody is looking:

- **An API that answered and rejected us fails; a connection that never
  happened skips.** A 404, a 403 or a 500 means the endpoint exists and
  refused — a finding. Only DNS and connection failures are a train tunnel.
- **The probe does not rest on one company.** It used to be a single third
  party's Greenhouse board, skipping the file on any exception including a 404,
  so the day that company changed ATS all four European contracts would skip
  and read green. It is now several independent hosts, and "every probe was
  answered and rejected" — which is not an offline machine — fails loudly
  instead of skipping.

## Adding a test

1. If it needs a new seam, add the seam to `src` — do not reach past it.
2. Put the fake in `conftest.py` if more than one module will want it.
3. Use `now=NOW`, never the real clock.
4. Say *why* in the docstring when the case is non-obvious. "This is the
   substring bug that deletes every International Sales role" is worth more
   to the next reader than the assertion itself.
