# Allegro public careers source

`src.sources.allegro.fetch(config, *, session=None, errors=None, now=None)` reads Allegro's observed employer listing and bounded public details into `Job` objects. This is a single-employer collector, not a generic SAP integration. No credentials, administrative APIs, application actions, paid models, or new dependencies are required.

## Public evidence: 9 September 2026

Inspection used **12 total HTTP/page-open attempts**, counting a failed Python TLS connection and two web-tool page opens. Direct curl requests used no retries and no automatic redirects. No application link was followed.

| Attempt | Public resource | Observation |
|---|---|---|
| 1 | `https://careers.allegro.eu/`, Python requests | TLS timeout; no response body. |
| 2–3 | Careers root and [AI Hub detail](https://careers.allegro.eu/job/Warszawa-Data-Scientist-Allegro-AI-Hub-00-841/1365885255/), web tool | Root navigation/cookie surface; readable AI Hub description from a five-day-old crawl. Not original-date evidence. |
| 4–5 | Same root and AI Hub detail, curl | Both HTTP 200. Root contains a meta refresh to `https://jobs.allegro.eu/`. Detail contains original `datePosted` microdata `Fri Aug 28 00:00:00 UTC 2026`, `itemprop=description`, employer/location fields, and an Apply control. |
| 6 | [Employer home](https://jobs.allegro.eu/) | HTTP 200; observed `/offer/` listing link. |
| 7 | [Employer listing](https://jobs.allegro.eu/offer/) | HTTP 200; `v-offers-filter` publishes `:api-url` as `https://jobs.allegro.eu//wp-json/api/v1/offer`. |
| 8 | Published feed, without parameters | HTTP 200 with `offers: []`, `max_pages: 0`, `results_count: 0`; language is required for useful retrieval. |
| 9 | Listing's linked `main.min.js?ver=2026-08-11` | Browser code sends GET to that exact endpoint with `lang` and `page`, and uses `offers`, `results_count`, `max_pages`. This is observed frontend behavior, not a guessed API. |
| 10 | [Feed page 1](https://jobs.allegro.eu//wp-json/api/v1/offer?lang=en&page=1) | 10 offers, 4 total pages, 36 reported results. All ten `releaseDate` values null. Includes a literal `Test` record, excluded as a non-job. |
| 11 | [Linked analyst detail](https://jobs.allegro.eu/offer/data-analyst-expert-senior-cx-fulfillment/) | HTTP 500 with a WordPress error page. No full description recovered. |
| 12 | [Feed page 2](https://jobs.allegro.eu//wp-json/api/v1/offer?lang=en&page=2) | Ten more distinct original `uid` values, all undated; pagination still reports four pages. |

The raw SAP AI Hub page exposes 4,928 normalized description characters, Warsaw/PL, employer Allegro, employment contract, and a publication date of **2026-08-28 UTC**. Hybrid 4/1 plus occasional remote days does not become `remote=True`. A readable description/Apply control does not prove that an application would currently be accepted.

Only two listing pages were inspected. The AI Hub ID was not in those pages; its membership in the remaining feed pages is unknown. One public WordPress detail failed. Consequently, live complete-detail coverage is **unverified**, and the collector must preserve these limitations rather than synthesize descriptions or freshness.

## Public evidence: 5 October 2026

The daily log for 2026-10-05 (and for 09-25, 10-01, 10-02, the other runs that reached the network) shows `skipped 10 malformed/non-job records` on each of four pages and `page cap reached (4/16)`, leaving only the configured seed. A bounded check used **13 Allegro requests** out of a 20-request budget shared with Just Join IT: no retries, no automatic redirects, 30-second timeout, no application link followed. The ledger and raw bodies are outside the repository in `/tmp/job-followup-oct05/evidence/`.

| # | Public resource | Observation |
|---|---|---|
| 1 | Feed page 1, `lang=en&page=1` | HTTP 200. 10 offers, `max_pages: 16`, `results_count: 156` (36 on 9 September). Every record has the keys `id, url, name, team, location, brand, contract, defaultLang, releaseDate`: **no `uid`**, `id` is now a numeric string, `url` is `https://careers.allegro.eu/job-invite/<id>?locale=en_US`, `releaseDate` is an integer. |
| 2–3 | `job-invite/3609` | 302 to `careers.allegro.eu/job/Warszawa-Senior-Data-Analyst-(Risk-Management)-00-841/1363037255/`, which answers 200 with "Sorry, this position has been filled." while the feed still lists the record. |
| 4–5 | `job-invite/3509` | 302 to `/job/Warszawa-Product-Manager-AdTech-00-841/1366343655/`: open posting, same microdata as the September capture, 5,288 description characters, `datePosted` **Thu Oct 01 02:00:00 UTC 2026** against feed `releaseDate` 2026-09-02T11:50:14Z. The page carries `internalId: "3509-en_US"`. |
| 13 | [Employer listing](https://jobs.allegro.eu/offer/) | `v-offers-filter` still publishes the same `:api-url`; it now also publishes the `team`, `location`, `brand` and `contract` option lists and 131 position hints. Script is `main.min.js?ver=2026-09-10` (was `2026-08-11`). |
| 14 | That script | `getOffers` sends `lang`, `page` and, when chosen, `position`, `location`, `team`, `brand`, `areas`, `contract`; `team` is an array. |
| 15 | Feed with `team[]` = Data & AI, IT - Machine Learning, IT - Analytics & Consulting | HTTP 200, one page, 5 offers, all from those teams. |
| 16–20 | One run of the corrected collector: `teams: ["Data & AI"]`, the configured seed, `max_details: 3`, `max_requests: 5` | 2 listed records; seed page 200; `job-invite/3677` → its page 200; `job-invite/3784` → 302 to the seed page, not requested again. Two jobs, both with full descriptions, no source errors. |

What this establishes:

- **Cause of the 40 rejected rows: feed shape drift, not bad rows and not an outage.** The previous parser required `uid` and a `jobs.allegro.eu/offer/<slug>/` link; after the 2026-09-10 frontend build no record has either. Four pages of ten records is forty.
- **The new `id` is the old `uid`.** Records 3153 and 3609 appear in both the September fixture (as `uid`) and the October capture (as `id`) with the same title and location, and every invite link repeats the number. `allegro:uid:<n>` keys are therefore unchanged.
- **The invite link is the employer's own mapping to the SAP page.** Requests 2, 4, 18 and 20 each redirected once to `/job/<slug>/<number>/`; request 20 landed on the configured seed, so that seed and feed record 3784 are one posting.
- **The two employer date fields disagree, and the page's value changed between captures.** The seed page read `datePosted` `Fri Aug 28 00:00:00 UTC 2026` on 9 September and `Sat Sep 26 02:00:00 UTC 2026` on 5 October. Feed record 3784, the same posting, states `releaseDate` 2026-08-28T06:23:20Z. Records 3509 and 3677 show the same gap (feed 09-02 and 08-28, page 10-01 and 09-26). Why the page value changed, and what either field records, was not established.
- **Cause of the page cap: the feed grew from 4 pages to 16** while `max_pages` was 4 and the library ceiling 5. The published position hints include Junior Data Scientist, Data Scientist and Product Analyst roles; none of the 10 records on unfiltered page 1 is in the three data teams, and the one analyst record on that page is filed under Finance, so a team filter is not a safe default.

Not verified: pages 2–16 of the unfiltered feed; what an invite link returns for a requisition that no longer exists (only the "filled" page was seen); non-English listings; what `releaseDate` and `datePosted` each record, and whether either is the first publication (in three records the feed date is the earlier, and for one it equals the page date captured in September; nothing more); the `position`, `location`, `brand`, `areas` and `contract` parameters, which were read in the script but never sent.

## Configuration and bounds

The coordinator integrates source registration and repository config; the source itself accepts:

```yaml
sources:
  allegro: true
watchlist:
  allegro:
    max_pages: 16         # library default 2; hard maximum 20
    max_details: 8        # library default 8; 0 disables; hard maximum 20
    max_requests: 40      # library default 12; hard maximum 40
    timeout_seconds: 20  # library default 20; hard maximum 30
    detail_urls: []       # optional explicit public SAP career-detail seeds
    teams: []             # optional and off in the shipped watchlist; names as the listing page lists them; at most 10
```

The shipped `watchlist.yaml` walks the whole listing: 16 pages, 8 details, 40 requests, no team filter. On the 5 October feed that is 16 listing requests, one for the seed and two for each of 7 listing details, 31 in all. The page ceiling is 20 and the request ceiling 40; a feed with more pages than `max_pages` reports `page cap reached (n/total)`, and details that do not fit report `request cap reached`. Neither is ever read as complete. Only page 1 of the unfiltered feed was captured live; the 16-page walk and both ceilings are proven on a synthetic feed in the recorded shape.

`teams` remains available and is sent as the listing page sends it (`team[]`). It is not the default because data roles are filed under several teams: with the three data teams the 5 October feed returned 5 records, and the risk analyst in request 2 was in Finance. A record from a team that was not requested is kept and reported as `team filter not honoured`.

Each listing detail costs **two** requests, the invite link and the page it redirects to, against the same `max_requests`. Every further redirect hop is charged too, at most four requests per resource.

The interface reads `config.source_enabled("allegro")` and `config.watchlist["allegro"]`. Library defaults do not enable this source. `detail_urls` defaults to empty; the coordinator may explicitly configure the checked AI Hub URL. A seed is **requested again on each run**, never emitted from cached research. Seeds consume the same total/detail limits and come before listing details. Listings are collected first; the remaining detail slice prioritizes Data Scientist/Machine Learning titles, then Data Analyst/Data Engineer/analytics, then other roles. This is request prioritization, not candidate-fit scoring or title filtering.

Each GET has a bounded timeout and disables automatic redirects. At most three redirects are followed per resource, each charged to the global request budget, on the original HTTPS origin and allowed route only; this is tested against a real `requests.Session` over an offline transport, not only against call-counting fakes. Listing pagination is constructed from the observed fixed endpoint and integer page metadata; payload-supplied links cannot change its origin. A listing record's link must be `careers.allegro.eu/job-invite/<digits>` carrying the record's own number (query reduced to its `locale=xx_XX`, anything else dropped) or the earlier `jobs.allegro.eu/offer/<slug>/`; explicit seeds must be on `careers.allegro.eu/job/<slug>/<numeric-id>/`. An invite link may redirect once, to a `/job/<slug>/<numeric-id>/` page on the same host; from there the numeric ID cannot change, and an employer detail redirect cannot change its offer path. No Apply route, login route, search page, other host, or meta refresh is followed.

Each listing processes at most 100 records, configured seeds are bounded at 20, and HTML/JSON parsing rejects bodies above 2 million characters. The size check occurs after response retrieval; it is a parser/memory guard, not a streaming byte or total wall-clock deadline. Malformed config limits fall back or clamp to finite limits. Optional `now` propagates into detail expiry evaluation for deterministic frozen replays; omitting it uses current UTC time. A supplied session must honor `allow_redirects=False` and must not introduce hidden adapter retries; the internally created requests session uses its default zero-retry adapter and is closed after fetching.

## Identity, fields, and failure semantics

- The original requisition number is the identifier: `uid` in the captured 9 September feed, the string `id` in the captured 5 October feed (`raw.uid_field` records which; the frontend asset name contains 2026-09-10, but the exact migration/deployment date is not established). The earlier integer `id` was the WordPress post number, kept in `raw.wordpress_id` and never used as identity; a string `id` is accepted only when the invite link repeats it. `ats="allegro_public"` is a source-owned identity namespace (not a vendor claim), and `ats_job_id="allegro:uid:<uid>"` keeps identity stable across employer/title edits and repeated pages. The feed vendor and `raw.platform` remain unknown, including enrichment from a WordPress structured job page. Only the observed SAP host uses `ats="successfactors"` and `raw.platform="SAP SuccessFactors"`; its public URL number has an independently scoped `allegro:career:<number>` identity. The two are equated only where the employer's own redirect shows it. When a listing record's invite link lands on the page of a configured seed, one job is emitted under the seed's identity, the one earlier runs emitted, and it gains the listing's `uid`, team and release date. That holds whether or not the seed's own request succeeded in that run: a seed that answers 500 and is then reached through the listing still comes out as `allegro:career:<number>`, so a pending or exhausted evaluation recorded under that key is found again. The page is not requested twice when the seed did succeed. A listing job enriched from a career page that is not a seed keeps `allegro:uid:<n>`, takes the page URL as `url`, and records `raw.listing_url` and `raw.career_page_id`. One gap remains: if the seed's request fails and the matching listing record's invite is not followed in that run (detail or request cap), nothing proves which record is the seed, and the record is emitted under `allegro:uid:<n>` for that run with the seed failure reported.
- Employer comes from the public listing `brand`, or the SAP detail's published employer field. Missing required feed title, brand, numeric UID or safe URL rejects the record. Test/migration notice titles and explicit closed titles are not jobs. Location remains the published text; the existing geo/filter path resolves Poland and enforces configured countries. Unknown remote status stays `None`; only explicit structured `TELECOMMUTE` sets `True`.
- `releaseDate` (epoch seconds since the 2026-09-10 build, between 2000 and 2100; ISO text before) and explicit detail `datePosted` are parsed only when they contain a complete supported date; UTC is preserved. Missing, malformed, partial, relative, millisecond or numeric-string dates remain unknown. A fetched-at timestamp and WordPress/SEO page modification dates never become publication dates. Both original values stay in `raw.releaseDate` and `raw.datePosted`, and their parsed forms in `raw.source_dates`. `posted_at` is the earliest complete one by full UTC time, and `raw.posted_at_source` names the field it came from. When the two differ, `raw.date_conflict` is true. Neither value is written to `raw.updated_at`: no field read here is an update time, so the digest's source-update line stays unknown. The earliest date is a conservative choice for freshness, not a proven first publication. A seed that is not matched to a listing record has only the page's `datePosted`; that value changed on the one page captured twice, so its freshness can be overstated.
- A recognized structured job description sets `raw.description_status="full"` and `snippet_only=False`; empty or failed enrichment remains `description_status="missing"`, `snippet_only=True`. The feed contains no teaser, so this adapter emits no synthesized snippet text. `detail_status` distinguishes not fetched, failed, and verified. Full text retains requirements for downstream filtering; collection is not suitability assessment.
- HTTP 404/410, explicit job-closure wording, or a past explicit `validThrough` removes the posting, including the listing fallback. Other HTTP failures, malformed envelopes, non-job/migration HTML, and unsafe redirects are reported with an `allegro:` prefix. Failed enrichment preserves the original listing as missing description; a failed seed has no job fallback. A verified empty feed returns no jobs and no source error. Repeated listing pages, changing pagination metadata, or a last-page raw/unique-UID count mismatch against `results_count` report partial coverage while retaining valid jobs; counts include non-job records with original UIDs so excluding `Test` does not invent a feed gap. Page/detail/request/record caps report partial coverage, not a false complete or empty result.

## Fixtures and verification

`tests/fixtures/allegro/listing_page1.json` and `listing_page2.json` preserve only public job-listing fields. `ai_hub_detail.html` is a reduced capture of the public SAP job content and observed microdata, with navigation, cookies, tracking scripts and unrelated assets removed. It is not a screenshot or full original HTML. The closed, migration and WordPress-error HTML fixtures are synthetic negative controls. JSON-LD, expiry, missing/partial dates, unsafe links and redirects are synthetic contract scenarios. General WordPress full-detail parsing remains unverified.

The `live_2026-10-05_*` fixtures are recorded: `listing_page1.json` and `listing_team_filter.json` are the feed payloads as served (re-indented); `detail_open.html`, `detail_filled.html` and `detail_ai_hub.html` keep the page title and the `jobDisplayShell` block verbatim and drop navigation, scripts, styles, the cookie manager and the footer. `detail_filled.html` is the first closure page actually fetched; `detail_ai_hub.html` is the same page as `ai_hub_detail.html`, 26 days later.

Focused validation:

```sh
.venv/bin/python -m pytest tests/test_allegro.py tests/test_filters.py -q
```

On 5 October **109 Allegro tests pass** (57 earlier, 52 on the recorded shape, the review findings and the 16-page walk), without live HTTP, production DB access, or models. The 9 September fixture funnel is 20 listed records → 19 usable unique records after excluding `Test`; adding the separately fetched AI Hub seed gives 20 jobs. All 20 pass Poland location checks. Under the strict 72-hour/skip-undated freshness policy at 2026-09-09 12:00 UTC, 19 fail for unknown dates and the AI Hub job fails as older than 72 hours, leaving **zero** strict matches. Disabling the undated freshness rejection makes the listing records eligible for further exploration; it does not establish freshness, full descriptions, or candidate fit.

The 5 October recorded funnel, through the repository's `filters` and `freshness` settings at 2026-10-05 10:40 UTC:

| Slice | Feed rows | Jobs emitted | Dated | Full description | After dedupe | Filter survivors | First rejection |
|---|---:|---:|---:|---:|---:|---:|---|
| Unfiltered page 1 of 16, one detail | 10 | 9 (one removed: filled) | 9 | 0 | 9 | 0 | 5 excluded title, 4 title not included |
| Three data teams, listing only | 5 | 5 | 5 | 0 | 5 | 0 | 4 older than 72 h, 1 excluded title |
| Live run: Data & AI plus the seed | 2 | 2 | 2 | 2 | 2 | 0 | 2 older than 72 h |

The newest of the five data-team records is Junior Data Scientist, released 2026-09-15. The collector reads the feed again; on 5 October that produced no posting inside the 72-hour window. Before the correction the same records were 0 jobs and, for the seed, a `posted_at` of 26 September. The table predates the shipped 16-page configuration, which was not run live: no request budget remained.

Tests also assert unknown vendor provenance for employer-feed details, distinct verified SAP provenance, injected expiry time across fetch/detail, and incomplete/repeated last-page detection. Tests exercise original-ID and canonical-URL dedupe, display-name stability, two-page/total/detail limits, seed reservation and DS/ML detail priority, partial-page retention, HTTP 500 detail retention, 404/410/explicit closure exclusion, malformed records/envelopes, external and identity-changing redirects, disabled source, body-size guard, timezone conversion, and explicit remote evidence.

Coordinator accepted the correction, passed the full offline suite, replayed the actual collector through normal source dispatch and the explorer against captured evidence, and refreshed Graft; see the [roadmap completion record](ROADMAP.md#completion-record). The exhausted live budget is not extended by offline tests. More live collection, verifying all four pages, and recovering broken employer detail pages require a separate bounded check; this implementation does not promise fresh or suitable opportunities.
