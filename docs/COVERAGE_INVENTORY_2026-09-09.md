# Coverage inventory — 9 September 2026

**We do not cover all companies or all jobs.** The current configuration enables 12 of 14 source names: eight ATS vendors serving **36 explicitly configured employer endpoints**, plus four broad feeds. Vendor support is a parser capability; it does not enumerate that vendor's employers. Broad feeds add employers outside the watchlist, but their unique employer coverage and market recall are unmeasured.

Scope: read-only code/configuration audit against the current working tree, including existing uncommitted fixes; Graft orientation and source-contract skill followed. No network requests, source edits, production DB access, scoring or applications. Only this report was written. Historical availability below comes from prior evidence, not new verification.

## Configured coverage versus observed activity

| ATS vendor | Enabled endpoints | Literal configured slugs / URL | Evidence |
|---|---:|---|---|
| Greenhouse | 20 | datadog, gitlab, elastic, canonical, grafanalabs, remotecom, adyen, cabify, typeform, intercom, stripe, hellofresh, n26, getyourguide, celonis, traderepublic, contentful, trustpilot, smartlyio, proton | `watchlist.yaml:68–98` |
| Lever | 1 | swordhealth | `watchlist.yaml:108–112` |
| Workable | 5 | blueground, netdata, hotjar, toggl, marfeel | `watchlist.yaml:130–137` |
| Ashby | 4 | ashby, posthog, linear, supabase | `watchlist.yaml:140–144` |
| SmartRecruiters | 2 | smartrecruiters, allegro | `watchlist.yaml:150–160` |
| Personio | 1 | personio | `watchlist.yaml:167–186` |
| Recruitee | 2 | channable, bunq | `watchlist.yaml:188–194` |
| Teamtailor | 1 | https://career.teamtailor.com/jobs | `watchlist.yaml:196–207` |
| **Total** | **36** | All entries are distinct within each vendor; all eight lists are nonempty. | YAML count independently checked |

Enabled broad feeds: **Arbeitnow, Landing.jobs, Just Join IT, No Fluff Jobs**. Disabled integrations: **Adzuna and LinkedIn email** (`config.yaml:81–114`). Four endpoints are the ATS vendors' own employers: Ashby, SmartRecruiters, Personio and Teamtailor. Greenhouse accounts for 20/36 endpoints; this is a small, uneven employer list, not eight complete vendor networks.

The code dispatches only those eight ATS vendors (`src/sources/ats_boards.py:2246–2269`), normalizes/deduplicates watchlist entries (`2313–2365`), then loops enabled lists and their slugs (`2368–2400`). Runtime CLI selection may narrow enabled sources further (`src/main.py:253–262`). No automatic employer enrollment occurs in this fetch path.

**Configured does not mean currently productive.** Four Workable endpoints were valid but empty on September 1: netdata, hotjar, toggl and marfeel (`watchlist.yaml:135–137`). Allegro's endpoint returns a migration notice, while its current SAP Harmony career site is not retrieved by this adapter (`watchlist.yaml:158–160`; `docs/POLAND_TRIAL_2026-09-09.md:38`). Just Join IT failed with HTTP 503 in the September 9 trial; its available-job count is unknown. That trial checked only two ATS tenants, not all 36 (`docs/POLAND_SOURCE_AUDIT_2026-09-09.md:7–9,28–40`). Therefore neither a current healthy-board total nor a current hiring-employer total is established. The watchlist comment saying four European vendors are switched off is stale (`watchlist.yaml:114–125` versus `config.yaml:94–97`).

## Discovery and retrieval boundaries

Discovery accepts supplied company names; it guesses slugs across eight vendors. Defaults are **four spellings per vendor per company**, at most **32 probes per company**, sharing **160 probes per invocation**. It finishes the current vendor round after a populated hit and skips later spellings; skipped/dropped candidates are recorded. It is not company discovery from a market directory or a careers-site crawler (`src/sources/ats_boards.py:2630–2673,3109–3221`). The counter measures probes: a bare Personio miss can make two HTTP requests, so 160 is not a strict wire-request ceiling (`2639–2655,2850–2883`). Hosted-slug guesses also cannot exhaust custom Teamtailor domains; known full careers URLs are accepted (`2212–2238`).

| Source | Implemented retrieval / enrichment bound | Exact source spans |
|---|---|---|
| Greenhouse / Lever / Workable / Ashby | One listing response per configured tenant; no client page walk or numeric job cap. Greenhouse/Workable request inline details; this does not prove upstream completeness. | `src/sources/ats_boards.py:527–576,712–756,978–1037,1177–1217` |
| Personio / Recruitee / Teamtailor | Single XML / JSON / RSS feed respectively; no client pagination. Personio can fall back from `.de` to `.com`. | `src/sources/ats_boards.py:1769–1838,1997–2028,2212–2238` |
| SmartRecruiters | Up to **20 × 100 = 2,000 listing rows per tenant**; early termination on short page/reported total. Only first **60 jobs per tenant** get detail attempts; detail failures preserve title-only jobs. Caps are logged. A later listing-page exception escapes the tenant fetch, so the outer handler discards that tenant's partial accumulated result. | Constants `src/sources/ats_boards.py:1225,1241,1249`; behavior `1382–1562,2386–2394` |
| Arbeitnow | **3 pages**, stopping if no next link; no requested page size in code, so a 300-job ceiling is not established here. | `src/sources/arbeitnow.py:39,106–144` |
| Landing.jobs | **3 × 100 = 300 requested listing rows**; DS/ML title gate precedes employer resolution. Maximum **60 job-detail employer resolutions**; overflow jobs needing those details are dropped. Legacy `company_id` lookup follows a separate cached branch, so 60 is not a universal HTTP ceiling. | `src/sources/landing_jobs.py:53–59,309–389` |
| Just Join IT | **3 × 100 = 300 requested rows**, junior/mid query, then local seniority and DS/ML title-or-skills gates. The 300 rows are not 300 DS/ML matches. Outputs are snippets. | `src/sources/justjoin_it.py:69–70,184–285` |
| No Fluff Jobs | One listing request, no page walk; local data/AI category and junior/mid gates. Outputs remain synthetic snippets after requirement-tile repairs. Source totals are not checked for completeness by this fetcher. | `src/sources/nofluffjobs.py:183–293` |
| Adzuna — disabled | Configured **10 countries × 3 queries = 30 first-page requests**, at most **1,500 raw results before duplicate removal**, 50 per query; provider freshness is only **1 day**, tighter than normal 72 hours. Requires keys. Counts above fetched page are logged, not paginated. | `watchlist.yaml:209–235`; `src/sources/adzuna.py:341–458` |
| LinkedIn email — disabled | Only existing alert emails matching `newer_than:2d`; **25 messages**, one Gmail listing call without next-page iteration; **60 description attempts**. Requires Gmail authorization. Email receipt time is used as `posted_at`, not proven employer publication time. | `watchlist.yaml:237–243`; `src/sources/linkedin_email.py:302–365,565–683` |

Page quantities describe requested rows under the expected provider contract, not verified current totals. Retries, details and fallback hosts make endpoint counts different from HTTP counts. Increasing `scoring.max_jobs` does not expand retrieval.

## Filter losses and observed yield

Normal configuration: **72 hours, undated rejected; 18 allowed countries plus GB only with explicit sponsorship; remote requires European evidence; English descriptions; 10 included title phrases and 52 excluded phrases** (`config.yaml:137–143,177–218,225–314`). No minimum description length or required-keyword gate is configured (`345–346`). These are candidate-policy choices, not evidence that rejected employers have no jobs.

Global processing deduplicates before filters. Filters stop on the first failure: title → employment type → location → freshness → language → keywords → length (`src/filters.py:659–727,763–811`). Hence reason counts are mutually exclusive first failures, not independent estimates of what each relaxed rule would recover. Fuzzy dedupe may combine distinct requisitions with the same employer/title/city; richer stale bodies can still beat fresh snippets (`735–760`; prior audit `docs/POLAND_SOURCE_AUDIT_2026-09-09.md:121–123`).

The captured No Fluff Jobs trial gives a concrete funnel:

| Stage | Count |
|---|---:|
| Raw regional rows | 18,630 |
| Dropped by adapter category | 16,479 |
| Dropped next by adapter seniority | 1,527 |
| Emitted normalized jobs | 624 |
| Deduplicated identities | 123 |
| After fixes: title not included / title excluded | 113 / 2 |
| After fixes: stale | 5 |
| After fixes: normal 72-hour survivors | **3** |
| After fixes: finite 90-day exploration survivors | **8** |

Raw/adapter evidence: `docs/POLAND_SOURCE_AUDIT_2026-09-09.md:28–40`; corrected result: `docs/POLAND_TRIAL_2026-09-09.md:20–38`. Exact corrected reason counts were read from `/tmp/job-hunter-poland-integration/results.json`, `cases.production72h.filter_counts` and `cases.snapshot.filter_counts`; this temporary artifact may later disappear. The provider reported **2,513 unique listings**, so 18,630 must not be described as unique vacancies. The earlier source-audit table is explicitly pre-fix: its one fresh survivor and four location drops are superseded by the combined replay.

All 624 NFJ descriptions were snippets, not full ads. Short descriptions can bypass language detection (`src/filters.py:517–559`); full-ad review found Polish requirements at all three fresh survivors, with additional experience requirements at DCG and Scalo (`docs/POLAND_TRIAL_2026-09-09.md:18`). Retrieval success is not candidate fit. No Kraków-office role passed the configured DS/ML title gate in that bounded trial (`22`).

Downstream scoring is capped at **40 per run**, tailoring at **10**, applications at **5** (`config.yaml:385–386,432,441`). The current durable backlog admits eligible work before scoring caps, so overflow should be described as pending work rather than assumed lost (`src/main.py:788–839`); its production backlog size was not inspected.

## What is measured, and what is missing

Present: source-level emitted-job counts, source-level survivors, aggregate dedupe/filter/scoring totals and errors (`src/main.py:303–311,765–786`; `src/models.py:377–418`). The digest distinguishes source error/degraded/ok and shows recent source health (`src/digest.py:652–717`). ATS failures carry vendor/slug, and some truncation/gate counts appear in logs.

Missing from the durable run-stat schema: per-employer configured/attempted/healthy/empty/migrated status and last verified careers URL; provider raw totals versus fetched rows/pages/cap hits; uniform malformed/category/seniority losses; full/snippet/missing-description counts; per-employer and per-source first-rejection reasons; newly covered employers and incremental deduplicated candidate yield per request. Aggregate `filter_counts` is dynamically attached but omitted by `RunStats.to_dict()`, which is what the CLI persists (`src/main.py:769–771,1066`; `src/models.py:401–418`). Existing per-source totals therefore cannot explain whether one employer silently disappeared while others on its vendor still produce jobs. No target-employer denominator exists, so an “all companies” percentage cannot be calculated honestly.

## Two strongest recommendations and peer critique

1. **Expand and maintain an employer registry from verified careers URLs.** Start with observed gaps such as Allegro's migrated career site and the employers surfaced in the Poland review; add supported tenant URLs in small batches and record employer, canonical careers URL, vendor, verification date and board state. Add a new ATS adapter only when a concrete set of desired employers justifies it. Measure newly covered employers and unique suitable jobs, not vendor count. Peer review favors a plain versioned file and 20 employers including 10 Kraków-relevant employers initially, expanding to 50 only after measured yield and maintenance cost. These are proposed pilot sizes, not measured market totals; a 60-minute weekly maintenance budget is an acceptance criterion, not an established estimate.

2. **Recover useful older open jobs and their missing evidence before maximizing volume.** Provide a bounded exploration/backfill view with original dates, selective full-ad retrieval after cheap role/location checks, and the employer-level funnel above. Keep daily freshness policy explicit. The replay demonstrates five additional exploratory survivors, but full-ad requirements must still establish fit and current availability. Use rejected-title examples to evaluate narrow synonyms; do not infer that all 113 title rejects were valuable jobs.

Additional low-implementation expansion: after credential setup, trial the already implemented Adzuna and LinkedIn email integrations with a small country/query or alert set. Their coverage remains bounded and overlapping; reconcile Adzuna's one-day source filter and distinguish LinkedIn receipt dates. Recheck Just Join IT health in a separately budgeted live trial before concluding its adapter needs replacement.

**Expansion idea I think is a mistake:** indiscriminately raise discovery/page/scoring caps and add every ATS vendor to claim universal coverage. This cannot discover employers whose names or careers URLs were never supplied, does not recover already-excluded roles, and can primarily increase duplicates, snippet-only candidates and scoring backlog. A targeted page-cap increase is defensible only after measured truncation and incremental suitable-job yield. These two recommendations and this objection were sent to the coordinator for peer critique.

**Challenge to weekly “all open jobs” reconciliation:** it needs per-employer complete-fetch evidence and stable raw identities before parser/category/title/freshness gates. The current emitted-job counts cannot establish completeness. Never mark a previously observed vacancy closed solely because it disappears from filtered output; failed, capped, malformed or alert-only sources leave its state unknown. Even single-response ATS feeds require completeness verification. A manual benchmark of current employer vacancies is necessary to measure pilot recall.

Validation: YAML counts computed independently with the existing Python/PyYAML environment; code spans inspected through Graft and exact-range reads; prior combined-trial artifact checked. No tests were rerun because this task changes documentation only and makes no new parser or live-availability claim.
