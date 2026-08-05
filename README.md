# Job Hunter

A daily job-search pipeline for one person. Every weekday morning it pulls
fresh postings from the company boards you name plus (optionally) Adzuna and
your LinkedIn job-alert emails, throws away everything outside your countries,
your title rules and your freshness window, asks Claude to score what is left
against your actual CV, writes a tailored CV and cover letter for each match,
optionally fills simple Greenhouse/Lever forms — and puts everything else in a
single HTML page with a link per job so your part of the work is one click,
not an hour of tab management. It never applies to the same job twice.

```text
sources ──┬─ Greenhouse boards ────┐
          ├─ Lever boards          │
          ├─ Workable boards       │
          ├─ Ashby boards          ├──▶ dedupe ──▶ hard filters ──▶ tracker gate
          ├─ SmartRecruiters       │     (title · location · freshness · keywords)
          ├─ Personio (XML)        │                               │
          ├─ Adzuna (optional)     │                               │
          └─ LinkedIn email        ┘                               ▼
   digest.html ◀── auto-apply ◀── PDF ◀── tailor CV + cover ◀── LLM fit score
   (needs your click · auto-applied · dry-run · below threshold · run stats)
```

**Greenhouse and Lever are American.** If you are searching in Spain, Germany
or Italy, Workable, Ashby, SmartRecruiters and Personio are where the mid-size
local employers actually post — Personio in particular is the default ATS for
German, Spanish and Italian SMBs. All six need no key and no scraping.

---

## Setup (~20 minutes)

```bash
cd job_search
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# only if you want auto-apply; skip it and everything still works
playwright install chromium
```

### 1. Put your CV in `cv/base_cv.md`

This one file drives both scoring and tailoring. The model may only reorder,
re-emphasise and rephrase what is written there — it is explicitly forbidden
from inventing employers, dates, degrees or numbers — so a vague CV produces
vague scores and a bland tailored version.

```bash
$EDITOR cv/base_cv.md
```

Concrete bullets with metrics beat adjectives:

```markdown
GOOD: Cut p99 checkout latency 840ms → 210ms by batching Redis reads
BAD:  Improved performance of critical systems
```

### 2. Fill in `config.yaml`

The minimum that will pass validation:

```yaml
applicant:
  name: "Ada Lovelace"
  email: "ada@example.com"
  phone: "+49 30 1234567"      # required before you turn dry_run off

keys:
  anthropic: ""                # or leave blank and export the env var
```

The API key resolves as **environment > config.yaml**, so this is the safer
option:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Then trim `filters.countries` to the countries you can actually work in, and
set `scoring.threshold` (65 is medium, 75 is strict). Every other key has a
working default in `src/config.py`.

#### Choosing a model provider

Two providers, one behaviour — `scoring` and `tailoring` never learn which is
in use, and the retry policy is identical either way.

```yaml
llm:
  provider: anthropic        # the SDK, direct
keys:
  anthropic: ""              # or export ANTHROPIC_API_KEY
```

```yaml
llm:
  provider: openrouter       # one key, every model
keys:
  openrouter: ""             # or export OPENROUTER_API_KEY
scoring:
  model: anthropic/claude-sonnet-5
tailoring:
  model: anthropic/claude-sonnet-5
```

On OpenRouter, **model ids are vendor-qualified**: `anthropic/claude-sonnet-5`,
`openai/gpt-5`, `google/gemini-2.5-pro`. A bare `claude-sonnet-5` is a 404 —
`--validate-only` catches that before you spend anything.

Only the key belonging to `llm.provider` is read. Leaving a stale
`keys.anthropic` in place while running on OpenRouter cannot silently keep
using it.

`llm.base_url` points the same transport at any OpenAI-compatible gateway —
LiteLLM, a local vLLM, a corporate proxy:

```yaml
llm:
  provider: openrouter
  base_url: "http://localhost:4000/v1"
```

A note on mixing: it is reasonable to score with something cheap and tailor
with something strong, since tailoring is what you actually send. `scoring.model`
and `tailoring.model` are independent.

---

### 3. Name the companies in `watchlist.yaml`

Slugs come out of the URL on a company's careers page. Open the company's
"Apply" link and read the host — that tells you which board they are on:

```text
boards.greenhouse.io/SLUG/jobs/123        ->  greenhouse: SLUG
jobs.lever.co/SLUG/uuid                   ->  lever: SLUG
apply.workable.com/SLUG/j/ABC123/         ->  workable: SLUG
jobs.ashbyhq.com/SLUG/uuid                ->  ashby: SLUG
jobs.smartrecruiters.com/SLUG/74399...    ->  smartrecruiters: SLUG
SLUG.jobs.personio.de/job/12345           ->  personio: SLUG
```

Pasting the whole URL works too — it is reduced to the slug for you.

```yaml
greenhouse:
  - spotify
  - datadog
lever:
  - plaid
workable:
  - some-valencia-company
personio:
  - some-madrid-company        # or the full host: acme.jobs.personio.com
```

Two vendor quirks worth knowing: **SmartRecruiters slugs are case-sensitive**
(copy the capitalisation out of the URL), and **Personio's slug is the
subdomain** rather than a path segment — a bare name tries
`{slug}.jobs.personio.de` and falls back to `.personio.com`.

The four European boards ship **switched off** in `config.yaml`, because an
enabled board with an empty watchlist quietly returns nothing every morning.
Put your companies in `watchlist.yaml` first, then turn them on:

```yaml
# config.yaml
sources:
  workable: true
  ashby: true
  smartrecruiters: true
  personio: true
```

Slugs change and a wrong one fails silently as "0 postings today", so check
them:

```bash
python -m src.sources.ats_boards --check greenhouse spotify
python -m src.sources.ats_boards --check personio acme
python -m src.sources.ats_boards --check-all       # everything in the watchlist
```

Then validate the whole configuration before spending anything:

```bash
python -m src.main --validate-only
```

### 4. Optional — Adzuna (job aggregator, free tier)

Register at [developer.adzuna.com](https://developer.adzuna.com) for an app ID
and key, then:

```bash
export ADZUNA_APP_ID=...
export ADZUNA_APP_KEY=...
```

```yaml
# config.yaml
sources:
  adzuna: true
```

```yaml
# watchlist.yaml
adzuna:
  countries: [de, nl, es]           # lowercase ISO codes
  queries:
    - "python engineer"
    - "backend engineer"
  max_days_old: 1
```

Adzuna's `created` is *its* ingest time, not the employer's publish time, so
its freshness is looser than an ATS board's.

### 5. Optional — LinkedIn job alerts via Gmail

The pipeline does not scrape LinkedIn. You create job alerts in LinkedIn, and
it reads the resulting alert emails out of your inbox with a **read-only**
Gmail scope.

1. Open the [Google Cloud Console](https://console.cloud.google.com) and
   create a project (any name).
2. **APIs & Services → Library →** search "Gmail API" → **Enable**.
3. **APIs & Services → OAuth consent screen →** User type **External** →
   fill in app name and your own email → **Save and continue**.
4. On the **Scopes** step add `https://www.googleapis.com/auth/gmail.readonly`
   and nothing else.
5. On the **Test users** step add your own Gmail address. (Leave the app in
   *Testing*; you do not need Google to verify anything for personal use.)
6. **Credentials → Create credentials → OAuth client ID →** application type
   **Desktop app** → **Create** → **Download JSON**.
7. Save that file as `gmail_credentials.json` in the project root.
8. Turn the source on and run once — a browser window opens for consent and
   writes `gmail_token.json` next to it:

```yaml
# config.yaml
sources:
  linkedin_email: true
```

```bash
python -m src.main --source linkedin_email --limit 3 --skip-apply
```

Both JSON files are in `.gitignore`. `gmail_token.json` is a plaintext
credential that can read your entire mailbox — treat it like a password, and
remember that backup and sync clients do not honour `.gitignore`.

---

## Run

**Start here — it costs nothing and needs no API key and no CV:**

```bash
python -m src.main --no-llm
```

Fetch, filter, digest. No scoring, no tailoring, no applying. Use it to prove
your slugs actually return jobs and to tune `filters.countries` and
`title_exclude` against real postings before you spend anything. Everything
that survives the filters shows up in the digest marked `—` rather than a
score, because "nobody looked" and "terrible fit" are opposite instructions to
the reader.

Once the digest looks like a list of jobs you would consider, add the key and
the CV:

```bash
python -m src.main --limit 3 --skip-apply   # cheap real run, ~3 model calls
python -m src.main                          # the daily run; opens the digest
python -m src.main --no-browser             # same, opens nothing — for cron
```

The digest lands at `output/digest_YYYY-MM-DD.html`, with a copy at
`output/digest_latest.html` you can bookmark.

| Flag | Meaning |
|---|---|
| `--config PATH` | path to `config.yaml` (default: `config.yaml`) |
| `--watchlist PATH` | path to `watchlist.yaml` (default: `watchlist.yaml`) |
| `--no-llm` | fetch + filter + digest only. No model calls, no key, no CV needed |
| `--no-browser` | never open the digest when the run finishes |
| `--dry-run` | force `apply.dry_run: true` — fill forms, submit nothing |
| `--no-dry-run` | force `apply.dry_run: false` — really submit (read the section below) |
| `--skip-apply` | skip the auto-apply stage entirely; every match goes to the digest |
| `--source NAME` | restrict the run to this source; repeatable. Narrows what the config enables, never enables anything new |
| `--limit N` | score at most N jobs — your cost ceiling for one run |
| `--validate-only` | check the config, print every problem, exit 0 (ok) or 1 (invalid) |
| `-v`, `--verbose` | DEBUG logging |

Exit codes: **0** ok · **1** config invalid · **2** unexpected error · **130**
interrupted.

---

## Cron

```cron
0 8 * * 1-5 cd /path/to/job_search && .venv/bin/python -m src.main --no-browser >> output/cron.log 2>&1
```

Weekdays at 08:00, into a log you can `tail`. Use the venv's Python by
absolute path — cron does not run your shell profile, so `python` alone will
be the wrong interpreter and `ANTHROPIC_API_KEY` will not be set (put it in
the crontab or a sourced file).

**macOS caveat:** cron does not run while the laptop is asleep and does not
catch up afterwards. If the lid is shut at 08:00 the run simply never happens.
`launchd` (or a systemd timer on Linux) at least fires on wake — and the next
run that *does* happen will tell you it missed one (see below).

---

## Failure notification

The failure worth worrying about is the quiet one. A morning where every board
returned 404, or where cron never fired, produces exactly what a genuinely
quiet Tuesday produces: an empty digest, or no digest at all. Read that for a
week and you conclude the market is dead rather than that your watchlist
rotted.

So every run is assessed against the history in the tracker, and these five
things raise an alert:

| Alert | Fires when |
|---|---|
| `no_digest` | the run finished without writing a page |
| `missed_run` | no run completed for ~36h (weekends excused for a weekday cron) |
| `no_jobs` | zero postings from every source at once |
| `source_zero` | a source that averaged 3+/run returned nothing — a renamed board looks exactly like an empty one |
| `all_sources_failed` | every source errored |

Deliberately **not** alerted on: a low job count (a real quiet day — crying
wolf is how an alert gets ignored), zero matches, a single scoring failure, or
the first ever run.

```yaml
notify:
  enabled: true
  on: [no_digest, missed_run, no_jobs, source_zero, all_sources_failed]
  channels:
    console: true          # stderr — lands in cron.log, and in cron's own mail
    file: true             # output/ALERT.txt, deleted again when a run recovers
    command: ""            # anything at all — see below
    email: {}              # to / from / smtp_host / smtp_port / username / starttls
  exit_nonzero: false      # true = a run with alerts exits 4
```

`ALERT.txt` is the channel that survives a closed laptop and a cleared
terminal: if the file is there, the last run had a problem. It is removed
automatically when a run comes back healthy.

`command` is the escape hatch, and it means this project needs no notifier
dependency. The message arrives three ways — as the final argument, on stdin,
and in `$JOBHUNTER_ALERT` — so most tools work with no wrapper:

```yaml
    command: "terminal-notifier -title 'Job Hunter' -message"   # macOS
    command: "notify-send 'Job Hunter'"                         # Linux
    command: "curl -d @- https://ntfy.sh/your-topic"            # phone push
```

It runs **without a shell**, so a job title containing `; rm -rf ~` is an
argument and never a command.

For email, the SMTP password comes from `$JOBHUNTER_SMTP_PASSWORD`, not from
`config.yaml` — same reason as the Anthropic key.

---

## Auto-apply — read this before flipping the switch

`apply.dry_run: true` is the shipped default. **Leave it there for a few
days.**

A dry run does everything a real application does except the last step: it
opens the form, refuses it if it asks you anything, types your name, email,
phone and links, attaches the tailored CV PDF, screenshots the filled page to

```text
output/applications/<company>-<title>-<id>/form_filled.png
```

and stops. Nothing is submitted. Open a few of those screenshots — that is
exactly what would have been sent. A dry run also leaves the job eligible for
a real application later; only a genuine submission blocks it forever.

The hard rules, all enforced in code and pinned by tests:

- **Greenhouse and Lever only.** Every other ATS — Workable, Ashby,
  SmartRecruiters, Personio, anything reached through Adzuna or LinkedIn —
  goes to the digest for a manual click. No exceptions, no "close enough" URL
  matching. Those sources deliberately do not even *claim* an ATS name the
  apply stage recognises, so the refusal does not depend on URL parsing alone.
- **Basic fields only:** first/last/full name, email, phone, résumé upload,
  LinkedIn/website URL, and a legally-required consent checkbox.
- **Any screener question and it bails to the digest.** Any `<textarea>`, any
  `<select>`, any radio group, any required field it does not recognise, and
  any label that reads like a question (`?`, "why", "describe",
  "sponsorship", "salary", "notice period", "how did you hear") ends the
  attempt. Ambiguity always resolves to bail — it will never answer a
  question on your behalf.
- **No tailored PDF, no application.** With `apply.require_pdf: true` (the
  default) a match without a rendered CV PDF goes to the digest rather than
  being submitted with nothing attached.
- Plus a score floor (`apply.min_score`, default 80 — deliberately stricter
  than `scoring.threshold`) and a per-run cap (`apply.max_per_run`).

When you do flip it, flip it narrowly:

```bash
python -m src.main --no-dry-run --limit 5
```

---

## PDF hook

Turning markdown into a PDF that looks like *your* CV is a taste problem, so
the pipeline refuses to guess. It looks for a file you write, `src/render_pdf.py`,
exposing exactly one function:

```python
def render(cv_markdown: str, out_path: str) -> None: ...
```

`src/render_pdf.example.py` is a complete, working ReportLab implementation —
it handles the markdown subset the tailoring stage emits (headings, bullets,
numbered lists, bold/italic/code, links, rules). Start there:

```bash
cp src/render_pdf.example.py src/render_pdf.py
pip install reportlab
$EDITOR src/render_pdf.py        # bend the styles until it looks like a CV
```

`src/render_pdf.py` is git-ignored: it is yours, and it will never be
overwritten by an update.

Until it exists — which is a supported, boring state, not an error — tailored
CVs stay as markdown in `output/applications/`, no PDF is produced, and every
match goes to the digest instead of being auto-applied. The run says so once,
not once per job.

---

## Honest limitations

**Freshness is the weakest claim here.** A stated window is not something
these sources can guarantee. Greenhouse's `updated_at` moves when anything
changes, so a typo fix on a three-month-old req can look brand new. LinkedIn
alert emails carry no per-posting date at all — every job in one email
inherits the email's arrival time. `freshness.skip_undated: true` is the
honest default and it *will* silently discard real jobs; watch the `undated`
count in the digest's filter breakdown for a week and relax it if a company
you care about always lands there.

The window itself ships at **72 hours, not 24**, and the reasoning is written
out next to the setting in `config.yaml`: `db.skip_seen_days` already shows you
each posting exactly once, so a wider window does not multiply your digest — it
only recovers what a 24-hour one loses in silence (a weekend, a board that
publishes late, an aggregator whose timestamp is its own ingest time).

**Ghost jobs are flagged, never filtered.** Between 18% and 27% of online
postings are never filled; Greenhouse's own study puts at least 1 in 5 US
postings in that bucket. There is **one** signal, it comes free from what the
tracker already stores, and it appears as a line on the card rather than as a
rejection: the same role re-listed under a new job id at least
`freshness.repost_min_gap_days` (14) before the current listing. Set it to 0
to turn the flag off.

Posting *age* is deliberately not a second signal. Everything you are shown
already came through the 72-hour window, so it is fresh by construction and a
"this posting is old" flag can never fire on it — an earlier version shipped
that knob anyway and it was dead code. The re-listing gap is reachable because
a re-posted role carries a brand new date, and it measures the better thing:
how long the *role* has been circulating, not how long this listing has been up.

Only sightings on the same board count, so an ATS migration or an aggregator
re-dating a live posting is not mistaken for a repost. Even so the flag hedges,
because a company that failed to fill a role and honestly re-advertised it
looks identical from outside. A wrong flag costs you a glance; a wrong deletion
costs you a job you never hear about, so nothing here ever removes a card.

**The ATS APIs are unofficial-but-public.** They have been stable for years,
but nothing obliges them to stay that way, and a format change degrades into
"that company returned nothing today" — which is quiet. Watch the per-source
counts in the digest, not just the total.

**The four European boards have never been run against a live API.** Workable,
Ashby, SmartRecruiters and Personio were written from published vendor
documentation on a machine with no outbound network. Their offline tests prove
the parsers agree with fixtures that were written the same way, which means a
wrong field name would be wrong in both places and every test would still pass.
Before you trust a posting from one of them, run the contract tests that
actually talk to the vendors:

```bash
python -m src.sources.ats_boards --check-all     # do the slugs exist?
pytest -m network -q                             # is the payload what we think?
```

**SmartRecruiters costs one extra HTTP request per posting.** Its listing
endpoint carries no description at all, so the ad is fetched job by job, capped
at `SMARTRECRUITERS_MAX_DESCRIPTIONS` (60). Past the cap, postings still reach
the digest but are scored on title, company and location alone — the run log
says how many. A company with hundreds of open roles is a company worth putting
on its own watchlist line and watching that count for.

**LinkedIn's guest description endpoint breaks and rate-limits.** When it
does, the scorer sees a title and a company and little else. Treat a high
score on a description-less LinkedIn item with suspicion.

**Scoring is a model's judgment, not a measurement.** It is uncalibrated on
day one against *your* CV and *your* market. Read the `score_reasons` on the
digest cards for a week and check them against your own opinion; if it is
generous, move `scoring.threshold` from 65 towards 75. That number is the
only real control you have over how much you read each morning.

**Tailoring cannot invent things about you** — the prompts forbid new
employers, dates, degrees and metrics, and a tailored CV that drifts too far
from the base is discarded in favour of the original. That is a guard, not a
guarantee. Skim what you send, every time.

**Cost is small but real.** On a normal weekday — roughly 15 jobs scored and
10 tailored, on Sonnet — it lands in the low tens of cents per day, a few
euros a month, dominated by tailoring. `scoring.max_jobs` is a
hard ceiling on the expensive stage; switching `scoring.model` to
`claude-haiku-4-5-20251001` cuts the scoring bill by roughly an order of
magnitude for a modest loss of judgment. Keep tailoring on Sonnet — it is the
output you actually send.

**You are responsible for every application sent under your name.** A form
this tool submitted is a form you submitted. If it attaches the wrong CV or
writes something you would not have written, that is yours to own with the
employer, and "the automation did it" is not a defence anyone accepts.

**Some employers' terms discourage automated submission.** Job boards and
career sites frequently prohibit automated access or filing. This tool is
deliberately conservative — public APIs, one request per board, no scraping
behind a login — but read the terms of any site that matters to you, and
apply by hand where automated submission is not welcome.

**The tracker only knows about applications *it* made.** Every job you apply
to yourself — from the digest link, from LinkedIn, from a referral — is
invisible to it, so it will keep showing you that job and would happily
auto-apply to it later. It is also a single SQLite file at
`output/tracker.sqlite3`, and `output/` is git-ignored: back it up, because
losing it resets the never-apply-twice guarantee to zero.

---

## The daily flow

1. Coffee. The 08:00 run has already finished.
2. Open `output/digest_latest.html` (or today's dated file).
3. Read **Needs your click** top-down — it is sorted by score. Each card has
   the score, why the model gave it, the gaps it found, and links to the
   tailored CV and cover letter. Click through to the posting and apply.
4. Glance at **Dry run — check these**: open the `form_filled.png` for each
   and confirm you would have been happy for that to go out.
5. Skim **Below threshold** once a week. If good jobs keep appearing there,
   your threshold is too high; if the top of "Needs your click" is junk, it is
   too low.
6. Check the **Run stats** funnel. A board that returned 0 today and 40
   yesterday is broken, not quiet.

---

## Project layout

```text
job_search/
├── config.yaml               settings
├── watchlist.yaml            what to search: board slugs, queries
├── cv/base_cv.md             your CV — drives scoring and tailoring
├── src/
│   ├── main.py               CLI + the pipeline that wires the stages
│   ├── config.py             defaults, merging, validation
│   ├── models.py             Job, Score, ScoredJob, Artifacts, RunStats
│   ├── db.py                 SQLite tracker — the never-apply-twice guarantee
│   ├── util.py               logging, HTTP with retries, HTML/text, dates
│   ├── geo.py                EU country / remote resolution
│   ├── filters.py            title, location, freshness, keyword filters
│   ├── llm.py                Anthropic client + JSON recovery
│   ├── scoring.py            fit score against your CV
│   ├── tailor.py             tailored CV + cover letter per match
│   ├── pdf.py                indirection to your PDF hook
│   ├── render_pdf.example.py copy to src/render_pdf.py and edit
│   ├── digest.py             the HTML page
│   ├── templates/            digest.html.j2
│   ├── sources/              ats_boards.py (6 vendors) · adzuna.py
│   │                         linkedin_email.py
│   └── apply/autoapply.py    Greenhouse/Lever form filling + the safety core
├── tests/                    offline suite — no network, no API key, no browser
├── docs/                     ARCHITECTURE.md · TESTING.md · EVALUATION.md
└── output/                   digests, tailored applications, tracker.sqlite3
```

## Testing

```bash
pytest -q                          # the whole suite, offline
pytest -q tests/test_autoapply.py  # one module
pytest -q -k "double_apply or inspect_form"
```

The suite runs with **PyYAML, Jinja2 and pytest** installed and nothing else —
`anthropic`, `playwright`, `requests`, `googleapiclient` and `reportlab` are
imported lazily inside the functions that need them, so a fresh checkout can be
verified before the heavy dependencies are. Every boundary (HTTP, the API,
Gmail, the browser, the clock) has exactly one injectable seam and the tests
use only that. See `docs/TESTING.md`.

---

## Troubleshooting

**A board returns nothing / 404** — the slug is wrong or the company moved
ATS. Confirm it, then fix `watchlist.yaml`:

```bash
python -m src.sources.ats_boards --check greenhouse spotify
python -m src.sources.ats_boards --check-all
```

Remember the slug is the path segment, not the company name:
`boards.greenhouse.io/reallyacme/jobs/1` is `reallyacme`, not `Really Acme`.
Two exceptions: **SmartRecruiters** slugs are case-sensitive, and **Personio**
slugs are the *subdomain* (`acme.jobs.personio.de` is `acme`).

**A company moved off Greenhouse and you cannot find them** — check the other
five. European companies migrate to Personio and Workable more often than the
other way round, and the give-away is where their "Apply" button points.

**"0 jobs found" on a run that fetched hundreds** — almost always freshness.
Look at the filter breakdown in the digest: a large `stale` or `undated` count
is the answer. The window already ships at 72 hours, so widen it further and,
if the `undated` count is the big one, keep the postings nobody dated:

```yaml
freshness:
  max_age_hours: 168      # a week
  skip_undated: false
```

`skip_undated: false` is the bigger change of the two: a board with no dates
hands you its entire back catalogue on the first run, not just its recent
postings. That is one noisy morning, not a permanent one — `db.skip_seen_days`
suppresses them all afterwards — but it can push genuinely fresh jobs past
`scoring.max_jobs`, so flip it deliberately rather than as a first guess.

Second most common: `filters.countries` is narrower than you think, or a
`title_exclude` term is catching more than you meant.

**Everything is "already seen"** — the tracker is doing its job. Jobs you have
been shown are suppressed for `db.skip_seen_days` (default 30). Lower it, or
delete `output/tracker.sqlite3` to start over — which also throws away the
record of what you applied to.

**Gmail auth fails** — delete `gmail_token.json` and run again to redo the
consent flow. `Error 403: access_denied` means your address is not on the
OAuth consent screen's **Test users** list. `credentials_file is missing` means
the downloaded JSON is not at the project root under the name
`gmail_credentials.json`.

**"playwright is not installed"** — expected until you run
`playwright install chromium`. Until then every match goes to the digest,
which is a fine way to run this tool.

**"No Anthropic API key"** — `export ANTHROPIC_API_KEY=sk-ant-...` or set
`keys.anthropic` in `config.yaml`. Environment always wins. Under cron, note
that your shell profile is not read.

**No PDFs and every match says "no tailored CV PDF"** — you have not created
`src/render_pdf.py`. See the PDF hook section; `cp src/render_pdf.example.py
src/render_pdf.py && pip install reportlab` is the whole fix.

**Exit code 1 with a list of problems** — config validation. Fix the lines it
prints; `python -m src.main --validate-only` re-checks without spending
anything. Exit code 2 is a bug: the traceback is in the log.
