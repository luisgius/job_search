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

## Configuration and bounds

The coordinator integrates source registration and repository config; the source itself accepts:

```yaml
sources:
  allegro: true
watchlist:
  allegro:
    max_pages: 4          # library default 2; hard maximum 5
    max_details: 8        # library default 8; 0 disables; hard maximum 20
    max_requests: 12      # library default 12; hard maximum 30
    timeout_seconds: 20  # library default 20; hard maximum 30
    detail_urls: []       # optional explicit public SAP career-detail seeds
```

The interface reads `config.source_enabled("allegro")` and `config.watchlist["allegro"]`. Library defaults do not enable this source. `detail_urls` defaults to empty; the coordinator may explicitly configure the checked AI Hub URL. A seed is **requested again on each run**, never emitted from cached research. Seeds consume the same total/detail limits and come before listing details. Listings are collected first; the remaining detail slice prioritizes Data Scientist/Machine Learning titles, then Data Analyst/Data Engineer/analytics, then other roles. This is request prioritization, not candidate-fit scoring or title filtering.

Each GET has a bounded timeout and disables automatic redirects. At most three redirects are followed, each charged to the global request budget, on the original HTTPS origin and allowed route only. Listing pagination is constructed from the observed fixed endpoint and integer page metadata; payload-supplied links cannot change its origin. Listing detail URLs must stay on `jobs.allegro.eu/offer/<slug>/`; explicit seeds must be on `careers.allegro.eu/job/<slug>/<numeric-id>/`. No automatic cross-origin detail hop, Apply route, login route, or meta refresh is followed. A SAP redirect cannot change the numeric ID; an employer detail redirect cannot change its offer path.

Each listing processes at most 100 records, configured seeds are bounded at 20, and HTML/JSON parsing rejects bodies above 2 million characters. The size check occurs after response retrieval; it is a parser/memory guard, not a streaming byte or total wall-clock deadline. Malformed config limits fall back or clamp to finite limits. Optional `now` propagates into detail expiry evaluation for deterministic frozen replays; omitting it uses current UTC time. A supplied session must honor `allow_redirects=False` and must not introduce hidden adapter retries; the internally created requests session uses its default zero-retry adapter and is closed after fetching.

## Identity, fields, and failure semantics

- Feed `uid` is the original identifier; WordPress `id` is retained separately in `raw.wordpress_id`. `ats="allegro_public"` is a source-owned identity namespace (not a vendor claim), and `ats_job_id="allegro:uid:<uid>"` keeps identity stable across employer/title edits and repeated pages. The feed vendor and `raw.platform` remain unknown, including enrichment from a WordPress structured job page. Only the observed SAP host uses `ats="successfactors"` and `raw.platform="SAP SuccessFactors"`; its public URL number has an independently scoped `allegro:career:<number>` identity. Their mapping is unknown: do not silently equate them or replace the original feed UID during enrichment. Existing canonical-URL/fuzzy dedupe may collapse corroborated overlap; different surfaces can still overlap when their URLs/titles differ.
- Employer comes from the public listing `brand`, or the SAP detail's published employer field. Missing required feed title, brand, numeric UID or safe URL rejects the record. Test/migration notice titles and explicit closed titles are not jobs. Location remains the published text; the existing geo/filter path resolves Poland and enforces configured countries. Unknown remote status stays `None`; only explicit structured `TELECOMMUTE` sets `True`.
- `releaseDate` and explicit detail `datePosted` are parsed only when they contain a complete supported date; UTC is preserved. Missing, malformed, partial, or relative dates remain unknown. A fetched-at timestamp and WordPress/SEO page modification dates never become publication dates. The live feed's non-null `releaseDate` semantics remain unverified; only nulls were observed.
- A recognized structured job description sets `raw.description_status="full"` and `snippet_only=False`; empty or failed enrichment remains `description_status="missing"`, `snippet_only=True`. The feed contains no teaser, so this adapter emits no synthesized snippet text. `detail_status` distinguishes not fetched, failed, and verified. Full text retains requirements for downstream filtering; collection is not suitability assessment.
- HTTP 404/410, explicit job-closure wording, or a past explicit `validThrough` removes the posting, including the listing fallback. Other HTTP failures, malformed envelopes, non-job/migration HTML, and unsafe redirects are reported with an `allegro:` prefix. Failed enrichment preserves the original listing as missing description; a failed seed has no job fallback. A verified empty feed returns no jobs and no source error. Repeated listing pages, changing pagination metadata, or a last-page raw/unique-UID count mismatch against `results_count` report partial coverage while retaining valid jobs; counts include non-job records with original UIDs so excluding `Test` does not invent a feed gap. Page/detail/request/record caps report partial coverage, not a false complete or empty result.

## Fixtures and verification

`tests/fixtures/allegro/listing_page1.json` and `listing_page2.json` preserve only public job-listing fields. `ai_hub_detail.html` is a reduced capture of the public SAP job content and observed microdata, with navigation, cookies, tracking scripts and unrelated assets removed. It is not a screenshot or full original HTML. The closed, migration and WordPress-error HTML fixtures are synthetic negative controls; no live closed page was fetched. JSON-LD, expiry, missing/partial dates, unsafe links and redirects are synthetic contract scenarios. General WordPress full-detail parsing and live closure templates remain unverified.

Focused validation:

```sh
.venv/bin/python -m pytest tests/test_allegro.py tests/test_filters.py -q
```

**148 tests pass** (57 Allegro and 91 existing filter tests), without live HTTP, production DB access, or models. The fixture funnel is 20 listed records → 19 usable unique records after excluding `Test`; adding the separately fetched AI Hub seed gives 20 jobs. All 20 pass Poland location checks. Under the strict 72-hour/skip-undated freshness policy at 2026-09-09 12:00 UTC, 19 fail for unknown dates and the AI Hub job fails as older than 72 hours, leaving **zero** strict matches. Disabling the undated freshness rejection makes the listing records eligible for further exploration; it does not establish freshness, full descriptions, or candidate fit.

Tests also assert unknown vendor provenance for employer-feed details, distinct verified SAP provenance, injected expiry time across fetch/detail, and incomplete/repeated last-page detection. Tests exercise original-ID and canonical-URL dedupe, display-name stability, two-page/total/detail limits, seed reservation and DS/ML detail priority, partial-page retention, HTTP 500 detail retention, 404/410/explicit closure exclusion, malformed records/envelopes, external and identity-changing redirects, disabled source, body-size guard, timezone conversion, and explicit remote evidence.

Coordinator accepted the correction, passed the full offline suite, replayed the actual collector through normal source dispatch and the explorer against captured evidence, and refreshed Graft; see the [roadmap completion record](ROADMAP.md#completion-record). The exhausted live budget is not extended by offline tests. More live collection, verifying all four pages, and recovering broken employer detail pages require a separate bounded check; this implementation does not promise fresh or suitable opportunities.
