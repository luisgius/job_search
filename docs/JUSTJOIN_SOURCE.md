# Just Join IT source

`src.sources.justjoin_it.fetch(config, *, session=None, errors=None)` reads junior/mid data and AI offers from Just Join IT into `Job` objects. It asks the site's internal offers API first and, when that fails on its first page, reads a bounded number of the site's public listing pages. No credentials, no application actions, no new dependencies.

## Public evidence: 5 October 2026

The daily logs show `HTTP 503` from `api.justjoin.it/v2/user-panel/offers` on each of the four runs that reached the network (09-25, 10-01, 10-02, 10-05); `tests/test_live_contract.py` notes the same on 09-01. No retained run shows an offer from this source. A bounded check used **7 Just Join IT requests** out of a 20-request budget shared with Allegro: one attempt each, no automatic redirects, 30-second timeout. The ledger and raw bodies are outside the repository in `/tmp/job-followup-oct05/evidence/`.

| # | Request | Identity | Observation |
|---|---|---|---|
| 6 | API, `page=1&perPage=100&experienceLevels[]=junior&experienceLevels[]=mid` | the adapter's browser-shaped headers | HTTP 503, 592 bytes, `text/html`: nginx's stock "503 Service Temporarily Unavailable" page, relayed by Cloudflare (`cf-cache-status: BYPASS`). No challenge page, no `cf-mitigated`, no `Retry-After`. |
| 7 | `https://justjoin.it/robots.txt` | the pipeline's plain `job-hunter/1.0` identity | HTTP 200. Disallows `/oferty-pracy/*,*`, `/api/` and asset paths on that host; `/job-offers/…` is not disallowed. |
| 8 | `https://api.justjoin.it/` | plain | HTTP 503, the same nginx page. |
| 9 | `https://justjoin.it/` | plain | HTTP 200, 1.65 MB. The Next.js flight payload embeds an `offers` array of 50 objects with the API's field names; the page config names `https://api.justjoin.it` as its API base. Category links include `/job-offers/all-locations/data`, `/ai`, `/analytics`. |
| 10 | `/job-offers/all-locations/data?experience-level=junior,mid` | plain | HTTP 200, 1.55 MB, 50 offers: 48 mid, 2 junior. The page's own pagination links read `?experience-levels=junior,mid&page=2` … `page=7`. |
| 11 | `/job-offers/all-locations/ai?experience-levels=junior,mid` | plain | HTTP 200, 50 offers, all mid; pagination links to page 4. |
| 12 | `/job-offers/all-locations/data?experience-levels=junior,mid&page=2` | plain | HTTP 200, 50 further offers, no slug shared with page 1. |

What this establishes:

- **The 503 is persistent, not a transient outage.** It was returned on five separate days over five weeks, on the offers path and on the bare host, to browser-shaped and plain headers alike, while the site itself was up.
- **Why the API host refuses this client is not established.** An earlier comment called it a TLS-fingerprint block; nothing captured shows that. The body is an origin error page rather than a CDN challenge, which is all that can be said. Telling a retired route from a network or client rule would need a request from a different client or network, which was out of bounds. The adapter does not try to get around the refusal.
- **The listing pages are public and carry the same offer objects.** They answered the pipeline's plain identity, robots.txt leaves them open, and the 150 embedded objects all had the keys the adapter parses plus `lastPublishedAt`, `expiredAt`, `guid`, `applyMethod`, `body`, `isPromoted` and converted salary rows.
- **Each offer carries two publication fields that usually disagree.** In 135 of the 150 objects `publishedAt` is later than `lastPublishedAt` (median 35 days, 2 to 88), in 15 the two are equal, in none is it earlier; 132 of the 135 later values fall in the first minute of an hour. One offer reads `publishedAt` 2026-10-05T08:00:05Z and `lastPublishedAt` 2026-08-31T07:26:08Z, and Allegro's own feed dates that posting 2026-08-28. Those are the observations. What either field records was not established, and neither is an update time.
- **Salary rows include the site's currency conversions.** Each advertised range is repeated in USD, EUR, CHF and GBP with `currencySource: "conversion"`; 63 of the 150 offers state no amount at all.
- **One job can appear under two categories with different slugs and `guid`s** (`…-warszawa-data` and `…-warszawa-ai`), so neither field unifies them.

Not verified: the API's current response shape, since it never answered; whether the API carries `lastPublishedAt`; the `analytics` category page and any page beyond those listed; how far back three pages reach on a given day (two `data` pages spanned 24 hours of `publishedAt`); the meaning of `publishedAt` and `lastPublishedAt`, and whether either is the first publication.

## Behaviour

- **One bounded transport for every request.** Each GET is made without automatic redirects, with a 20-second timeout, and its body is read under a 4,000,000-byte cap that is enforced while reading (and from `Content-Length` before reading when that is declared). Nothing is retried.
- **API first.** One request a page, at most 3. A redirect from the API is not followed. A 200 whose body lacks the `data` list is reported as shape drift and does not trigger the listing pages. The API walk never claims the whole board: a full third page is reported as `page cap reached … coverage partial`, a failure on page 2 or 3 as `earlier pages kept, coverage partial`, and every job carries `raw["coverage"] = "bounded_api"`. How much of the board three pages hold was never measured, because the API has never answered.
- **Listing pages only when page 1 of the API fails.** `LISTING_CATEGORIES = ("data", "ai")`, `LISTING_MAX_PAGES = 3` each, requested as `?experience-levels=junior,mid` and `&page=N` with the pipeline's plain identity. The whole walk shares `LISTING_MAX_REQUESTS = 8`: six pages and two spare. A redirect is followed only to the same HTTPS host and path and costs one request from that budget; a redirect anywhere else is refused, named in `errors`, and ends that category. A run that falls back therefore makes at most 9 requests, one to the API and eight to `justjoin.it`, and none to any other host. This is tested against a real `requests.Session` over an offline transport.
- **Only the observed envelope is read.** Offers come from the single non-empty `"offers":[…]` array inside the `self.__next_f.push([1,"…"])` scripts. Recognising that envelope and validating its rows are separate steps:
  - a page with no flight payload, an offers array that cannot be decoded, a non-empty array with no row carrying a `slug`, or more than one non-empty array is reported as drift;
  - an empty auxiliary array cannot hide any of those;
  - a page whose every offers array is empty is a valid empty result;
  - inside a recognised array a malformed row is skipped and counted (`skipped 1 malformed row(s) of 50; valid rows kept`) and its neighbours are kept. At most 100 rows a page are processed.
- **The same gates as the API path.** Junior/mid re-check, the unchanged title/skills gate, `parse_offer`, one job per slug. `raw["surface"]` is `"api"` or `"listing_page"`, and `raw["coverage"]` is `"partial"` for every job read from a listing page.
- **Dates.** A publication field is read only when it is a complete calendar date, optionally with time and offset; `"Oct 5"`, `"2026-10"`, `"10:30"`, relative wording and numbers stay unknown instead of being completed from today's date. `posted_at` is the earlier complete value of `publishedAt` and `lastPublishedAt`, and `raw["posted_at_source"]` names the field. Both original strings stay in `raw`, their parsed forms in `raw["source_dates"]`, and `raw["date_conflict"]` is true when they differ. Nothing is written to `raw["updated_at"]`, so the digest's source-update line stays unknown. The earlier date is a conservative choice for freshness, not a proven first publication.
- **Salary.** Rows marked `currencySource: "conversion"` are ignored; the widest advertised range is reported as before.
- **Unknowns stay unknown.** `country` is never set; `remote` is `True` only for `workplaceType: remote` and otherwise `None`. The description is still the synthesized skills teaser with `raw["snippet_only"] = True`: no surface observed here carries the ad body.
- **Every fallback run says so in `errors`**: the API failure, each listing page that failed, any malformed rows, and one line giving the number of offers read, the categories, and any page cap reached. Coverage through the listing pages is the first pages of two categories. It is not the board.

## Fixtures and verification

`tests/fixtures/justjoin_offers.json` remains spec-derived. The `tests/fixtures/justjoin/live_2026-10-05_*` files are recorded: `api_503.html` is the 503 body as served; `listing_data.html` and `listing_ai.html` keep the flight row carrying the offers and the pagination row verbatim, with 9 and 4 of the 50 embedded offer objects (every field as served) and the rest of the page removed. Tests that need full or further pages build them from those recorded objects in the same envelope and are labelled synthetic.

```sh
.venv/bin/python -m pytest tests/test_justjoin_it.py tests/test_filters.py -q
```

**90 Just Join IT tests pass** (15 earlier, 75 on the recorded evidence and the review findings), without live HTTP, production DB access, or models.

Replaying the captured responses (API 503, `data` pages 1–2, `ai` page 1) through the adapter and the repository's `filters` and `freshness` settings at 2026-10-05 10:40 UTC:

| Stage | Count |
|---|---:|
| Offer objects embedded in the three pages | 150 |
| Jobs emitted after the junior/mid and title/skills gate | 106 |
| … with a known date | 106 |
| … with a full description | 0 (all snippet) |
| … `remote=True` / unknown | 39 / 67 |
| After dedupe | 100 |
| Rejected first on title not included / excluded title / older than 72 h | 65 / 14 / 18 |
| Filter survivors | 3 |

The three survivors are two Surveily "Mid Machine Learning Engineer · AI / Computer Vision" listings and Jit Team's "Mid ML Engineer", all with both publication fields on 5 October. They are snippet-only and unscored: surviving the filters is not a fit assessment. Using the earlier publication field puts 9 of the 106 inside the 72-hour window where `publishedAt` alone put all 106; that is the effect of a conservative policy, not evidence of when each offer was first published. The replay stands in a 404 for the two pages that were never captured (`data` 3, `ai` 2); the corrected adapter itself was not run against the live site, because the request budget was spent.

The recorded pages also show that the existing title/skills gate drops Polish-language titles such as "Analityk Danych (SQL, BI)". That separate coverage gap remains for a profile-grounded filter audit; the language of a title alone does not establish the required working language. The coordinator removed the unsupported TLS-fingerprint explanation from `tests/test_live_contract.py`.
