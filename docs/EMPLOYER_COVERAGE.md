# Employer coverage registry

`coverage/employers.yaml` is a versioned, manually reviewed evidence registry. It contains
20 Poland-relevant employers, including 14 with explicit Kraków evidence as of
2026-09-09. Geography includes indexed and historical job locations: it is neither a
vacancy count nor proof of current hiring, work eligibility, or candidate fit.

The standalone `src.employers` module reads this file without loading application
configuration, source adapters, the watchlist, candidate information, or the database.
It performs no network calls or writes and requires only the existing PyYAML dependency.
There is no service, migration, crawling, subscription, enrollment, application action,
or automatic tenant discovery. Existing dirty working-tree changes are independent.

## Use it offline

From the repository root, using the existing virtual environment:

```sh
.venv/bin/python -m src.employers validate
.venv/bin/python -m src.employers summary
.venv/bin/python -m src.employers summary --json
.venv/bin/python -m src.employers validate --registry coverage/employers.yaml --json
.venv/bin/python -m pytest tests/test_employers.py -q
```

The default registry path is resolved from the module, independent of the caller's
working directory. Python must still be able to import `src` when invoking `-m`.
`--registry` allows reviewing another file without changing the shipped cohort.
Exit codes: **0** valid/successful, **1** unreadable or invalid registry, **2** invalid
CLI usage. `--json` emits validation failures as `{valid: false, error: ...}` on stdout;
ordinary failures go to stderr. Argument-parser usage errors remain text on stderr.
Validation fails at the first error and reports its field path where available.

Current summary:

| Measurement | Recorded value | Interpretation |
|---|---:|---|
| Employers / Poland evidenced | 20 / 20 | Research cohort; not market coverage percentage |
| Kraków evidenced | 14 | Includes historical/indexed locations; remote Poland alone does not count |
| Supported / unsupported / unknown route compatibility | 3 / 4 / 13 | Adapter capability for observed routes, not activation or successful retrieval |
| Reachable / unavailable job-detail endpoints | 2 / 4 | Prior report's explicit HTTP observations; not a fresh live check |
| Verified reachable collector API endpoints | 0 | No collector API health asserted by this registry |
| Employers without a verified reachable collector in this registry | 20 | Missing registry evidence, not a claim that existing ingestion is broken |
| Employer live hiring status unknown | 20 | Readable pages and closed individual jobs cannot settle company-wide status |

Summary JSON keeps geography, research status, compatibility, live hiring status,
evidence methods and endpoint observations in separate fields. Endpoint counts are
split into `careers_index`, `job_detail`, and `collector_api`, with all health states
present even when zero. Counts of endpoint observations are not counts of unique jobs:
Hitachi's two unavailable URLs share R0057558. `asOf` is the registry revision date,
not an assertion that all URLs were rechecked then. Inspect each evidence item for its
method, observed date and provenance.

## Version 1 contract

The root requires `schemaVersion: 1`, `updatedOn: YYYY-MM-DD`, and a nonempty
`employers` list. Unknown/missing fields and unsupported versions are errors, so schema
changes require an explicit version decision. The registry accepts quoted or plain ISO
date text but rejects impossible calendar dates and observations/checks newer than
`updatedOn`. YAML duplicate mapping keys and aliases are rejected instead of silently
overwriting or sharing evidence. These loader rules do not change global PyYAML behavior.

Every employer requires these fields:

| Field | Meaning / allowed values |
|---|---|
| `employerId` | Stable lowercase hyphenated identifier. Preserve across display-name and career-platform changes. Never generate from an ATS tenant at runtime. |
| `name`, `aliases` | Display name and optional same-company names. Aliases are labels only; they do not trigger deduplication, merges, or activation. |
| `officialDomain`, `careersUrl` | Evidenced official employer/careers domain and absolute HTTP(S) route. The route may be an exact historical requisition. A branded hosted career domain may be recorded when no corporate root was verified (Tesco); this does not assert ownership of the service domain. |
| `locations` | Nonempty list of `{country, city, kind, evidence}`. Country uses two uppercase letters; city may be null for country-only evidence. Kinds: `office`, `job_location`, `historical_job`, `remote_country`, `country_presence`. Each requires evidence references. |
| `evidence` | Nonempty list of `{id, url, observedOn, method, provenance, note}`. IDs are local to one employer. Methods: `primary_page`, `search_index`, `prior_report`, `direct_http`. Provenance identifies the earlier report or bounded check; notes retain limitations. |
| `surfaces` | Exactly `frontend`, `apply`, `ats`. Each is `{status, label, url, evidence}` with status `observed`, `unknown`, or `not_checked`. Observed layers require label, URL and evidence; unobserved layers require null label/URL. |
| `sourceCompatibility` | `{status, adapter, evidence}`. Status: `supported`, `unsupported`, `unknown`. Supported requires one of the eight board adapter names or `allegro`, plus evidence; unsupported requires evidence and null adapter; unknown has null adapter. |
| `endpoints` | List of `{url, scope, health, checkedOn, evidence}`; an empty list means no recorded endpoint observations. Scope: `careers_index`, `job_detail`, `collector_api`. Health: `not_checked`, `unknown`, `reachable`, `unavailable`. |
| `status` | Research workflow: `lead`, `researched`, `needs_review`. Never an employer operating or vacancy status. |
| `liveHiringStatus` | `unknown` or `not_checked`. Version 1 deliberately does not offer company-wide open/closed states derived from individual jobs. |
| `nextAction` | Concrete, nonblank follow-up for a later bounded task. It is documentation, not an executable instruction. |

`not_checked` endpoint health requires a null `checkedOn`; every other endpoint state
requires a date with matching evidence. `unknown` means the attempted/reviewed route
did not establish transport health (including unreadable or potentially cached web
content). `reachable` and `unavailable` additionally require a same-date, exact-URL
`direct_http` or `prior_report` reference. Rendered content and search results alone
cannot become verified HTTP health. A validator verifies structure and these evidence
relationships; human review must verify that each note supports the actual assertion.
Historical health never becomes a current live guarantee simply by loading the file.

IDs and canonical careers/endpoint URLs cannot belong to two employers. Endpoint URLs
also cannot repeat within an employer; reuse the existing observation or add evidence.
Canonicalization ignores hostname case, default ports, fragments and trailing slashes;
it preserves path case and query strings because they can identify different routes.
Evidence URLs may legitimately repeat across observations or employers, and a careers
URL may also appear as its own endpoint/evidence URL. No network canonicalization or
redirect resolution runs. URLs reject non-HTTP schemes, malformed domains/ports,
credentials, whitespace, backslashes and malformed percent escapes.

Compatibility reflects the eight board adapters in `src/sources/ats_boards.py:115–118`:
Greenhouse, Lever, Workable, Ashby, SmartRecruiters, Personio, Recruitee, Teamtailor.
It also includes the limited employer-specific `allegro` collector implemented in
`src/sources/allegro.py:225–346` and wired into `src/config.py:318–321`. The standalone
module keeps this actual collector subset explicit to avoid importing networking or
application configuration; a focused test verifies the subset against `SOURCE_NAMES`
and `BOARD_SOURCE_NAMES`. A future relevant collector addition should update it.
`unsupported` refers to an evidenced route outside that set, not proof that every route
for an employer is unsupported. A frontend asset, an application destination and the
underlying ATS are distinct observations: AstraZeneca's TalentBrew assets and Eightfold
route leave its ATS unknown. Allegro's SAP career surface does not prove a public
SuccessFactors enterprise API. Its `supported` record means the bounded employer
collector exists; live complete-feed health stays unknown. The collector's feed `uid`
and separately seeded career-path identity are different namespaces, neither assumed
equivalent to a native ATS requisition ID. No tenant guessed from a name can activate
a source.

## Cohort evidence and research boundary

The first 12 employers carry forward the dated observations in
[the market report](COVERAGE_MARKET_2026-09-09.md), including its links to earlier same-day
checks. Their evidence method is `prior_report`; those requests were not repeated.
They are Allegro, Lufthansa Group Business Services, Amway, SmartRecruiters, Fetcherr,
HERE Technologies, Hitachi Energy, Motorola Solutions, AstraZeneca, ING Hubs Poland,
Roche and Cisco. Lufthansa, Amway, HERE, Hitachi, Motorola, AstraZeneca and Cisco supply
seven of the Kraków location records. Cisco remains historical indexed evidence.
HERE 81790 and the two Hitachi URLs remain unavailable; the prior report also excludes
HERE 81009, whose full canonical URL is not invented here. ING's 404 stays a missing job,
not an employer closure. Search crawl dates are never used as job publication dates.

Eight additions used **10 search queries, 12 open attempts and 3 in-page text finds**,
all bounded and completed on 2026-09-09. Opens include two focused rereads; no pagination
sweep, HTTP retries, application clicks, or account access occurred. Only employer
primary sources or employer-branded hosted careers pages support the new records.

| Addition | Primary evidence / limitation |
|---|---|
| Google | [Office directory](https://about.google/company-info/locations/) explicitly lists Kraków. The [careers page](https://www.google.com/about/careers/applications/locations/krakow/) is indexed, with no live listing established. |
| IBM | [Careers locations](https://www.ibm.com/careers/locations?source=WEB_Search_NA) establishes Poland. No Kraków attribution is made from this page. |
| HSBC | [Official programmes page](https://www.hsbc.com/careers/students-and-graduates/find-a-programme?page=2&take=10) is indexed with Kraków technology programmes and past closing dates; page-open content was differently aged. Historical geography only. |
| UBS | [Poland careers page](https://www.ubs.com/global/en/careers/about-us/locations/poland.html) names Kraków, Wrocław and Warsaw. Job board links do not identify the underlying ATS. |
| Sabre | [Employer Workday detail](https://sabre.wd1.myworkdayjobs.com/en-US/SabreJobs/job/Software-Engineering-Intern_JR107575) is indexed with Kraków; open yielded zero readable lines. The Poland careers overview open failed internally. |
| State Street | [Official Poland results](https://careers.statestreet.com/global/en/poland?from=50&rk=l-poland&s=1) are indexed with Kraków. Cached counts are not copied into the registry as live jobs. |
| Aptiv | [Poland company page](https://www.aptiv.com/pl/aptiv-w-polsce) explicitly describes the Kraków technical center. The registry separately retains an indexed Workday role, unverified live. |
| Tesco Technology | [Employer SmartRecruiters board](https://careers.smartrecruiters.com/TescoTechnologyCE/poland) describes its Kraków hub but renders no postings. This is an observed compatible surface needing review, not a verified healthy collector or evidence the employer stopped hiring. |

These eight add seven explicit Kraków records; IBM contributes Poland-only evidence.
There are no claims that the app now retrieves all 20 employers. The existing
SmartRecruiters adapter may already ingest its namesake employer: absence of a collector
observation in this registry does not negate operational evidence elsewhere.

## Updating and checking

Preserve employer IDs and separate location, platform and endpoint observations. Add
the exact primary URL, date, evidence method and caveat before asserting a new fact.
Set `updatedOn` to the revision date while retaining older observation dates; do not
re-date inherited evidence. Preserve unknowns and negative job evidence. If changing
an endpoint observation, retain relevant historical details in evidence notes.

Run validation and the focused tests before requesting review. Tests cover the shipped
cohort, malformed structures/enums/dates/URLs, duplicate IDs/routes/YAML keys, evidence
references, frontend/ATS separation, endpoint provenance gates, offline read-only
execution, working-directory independence, JSON output and CLI error codes. Summary
counts derive from the records, with no hand-maintained runtime totals. Operational
metrics, roadmap integration, collector implementation and watchlist decisions belong
to separate work.
