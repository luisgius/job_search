# freehire.me source

`src.sources.freehire.fetch(config, *, session=None, errors=None)` reads freehire.me's public search API into `Job` objects. freehire.me is an aggregator: it indexes postings from about fifty ATS platforms and republishes them in one schema. The source is **off by default** (`sources.freehire: false`). It needs no credentials, application actions, paid models or new dependencies.

## Why it is here

Our ATS adapters only reach employers someone has named in `watchlist.yaml`, and only on eight vendors. freehire covers vendors we have no adapter for. On 2026-10-22 its Poland facet listed about **25,000** open postings (about 4,000 in Kraków). The biggest upstreams were whatjobs-pl 6,405, Workday 4,089, nofluffjobs 1,923, SmartRecruiters 1,705, Oracle 1,166, Greenhouse 1,042, energyjobline 654, Traffit 637, SuccessFactors 478 and Phenom 428. Its data/AI categories are `data_engineering`, `ai_engineering`, `data_analytics`, `data_science` and `ml_ai`. Remote postings in those categories number about 12,500 worldwide (2,276 tagged `eu`, 426 `global`).

## Evidence: 22 October 2026

The analyst made four GET requests and saved the responses outside the repository, in `/tmp/fh-probe-54684/`. Implementing the adapter took no further requests.

| # | Resource | Observation |
|---|---|---|
| 1 | `robots.txt` | `User-agent: *` may fetch `/api/`. Only Googlebot and Bingbot are disallowed from it. |
| 2 | `GET /api/v1/jobs/facets`, Poland | `{"data": {"total": 25077, "facets": {…}}}`. Counts per `source`, `category`, `work_mode`, `regions` and `countries`. Rate-limit headers: `x-ratelimit-limit: 600`. |
| 3 | `GET /api/v1/agent/jobs/search`, a Poland search, `limit=8` | `{"data": [8 hits], "meta": {"limit": 8, "offset": 0, "total": 233}}`. Each hit has a full description. Rate-limit headers: `x-ratelimit-limit: 300`. |
| 4 | Facets, remote data/AI | Total 12,511, `work_mode: remote` only. |

What the hits show:

- **`posted_at` is re-dated.** Six Capco hits had the same `posted_at` (`2026-10-22T14:42:15Z`), but their `created_at` values ran from 2026-08-13 to 2026-10-12. A Telnyx hit had that afternoon's `posted_at` with a `created_at` of 2026-06-12, and freehire's own `reality.fake_freshness` flag was `true` for it. Only the emagine hit, created that day, had matching values. So the adapter uses `created_at` as `posted_at`. The re-dated value is kept as `raw.source_posted_at`. The server's `posted_within_days` filter works on the re-dated field, so it is never sent, and `config.validate` rejects a query that sets it.
- **`url` is the upstream posting**, for example `https://job-boards.greenhouse.io/capco/jobs/8111831?utm_source=freehire.me`. `util.canonical_url` strips `utm_source`, so the copy lands in the same dedupe group as the Greenhouse adapter's record of that posting.
- **Freehire's own guesses sit alongside the posting's facts**: `enrichment` (seniority, employment type, salary, summary, requirements), `skills`, `regions`, `cities`, `collections` and `reality`. `work_mode` is missing from most hits (two of eight).
- `whatjobs-pl` and `energyjobline` upstreams are themselves aggregators: reposts, not employers.

## Mapping

| Job field | From | Rule |
|---|---|---|
| `title`, `company` | `title`, `company` | HTML entities decoded and whitespace collapsed. A hit with no company is skipped, never filled in. |
| `url` | `url` | Upstream URL as served. Anything other than plain http(s), or a URL with credentials, skips the hit. |
| `posted_at` | `created_at` | Never `posted_at`. A hit without `created_at` stays undated. |
| `remote` | `work_mode` | `True` only for `remote`, `False` only for `onsite`. Hybrid or missing gives `None`. |
| `country` | `countries` | Set only when exactly one country is listed. All are kept in `raw.location_countries`. |
| `description` | `description` | `description_format=text`: entities decoded and spacing tidied, so `<2 years` survives. Only a body that still carries markup (closing tags, `<br>`) goes through `html_to_text`. |
| `ats` | — | Always `None`. `ats_job_id` holds the `public_slug`. |

`raw` keeps `public_slug`, `external_id`, `upstream_source`, `source_posted_at`, `posted_at_source` and `work_mode`. Freehire's guesses go under `raw.freehire_enrichment`, which no hard filter reads. That matters for employment type: if freehire guessed `employment_type: contract` and that value sat where the employer's own field goes, `filters.employment_type_exclude` would reject the posting on a guess. Salary comes only from those guesses, so `Job.salary` stays `None`.

## Safety downstream

- **Dedupe.** `SOURCE_RANK["freehire"]` is 1, the lowest rank. On top of that, `_richness` lets any other source's full description beat a freehire copy whatever their lengths. For every other source this new step equals "description length > 0", so their order does not change. Two cases still go to the freehire copy, by the rules that already apply to every board. If the direct record is snippet-only, the copy's full text wins. If the direct record is undated and the copy is dated, the copy wins, because date presence comes first so that `skip_undated` does not drop a posting that some source dates.
- **Auto-apply.** `autoapply.eligible` refuses `source == "freehire"` (`NEVER_AUTO_APPLY_SOURCES`) before it checks the URL. This matters because the URL may be a real Greenhouse or Lever form, while the title, description and date are a third party's restatement. A refused job goes to the digest with an "apply by hand" link.

## Limits

- **Coverage is capped.** By default the run makes at most 6 requests of 50 hits each, split between the queries. A query that runs out of results passes its unused share to the next. Hits come back sorted newest-`posted_at`-first, which is the re-dated field. A posting that was created recently but not re-dated can therefore sort below older re-dated ones and fall outside the cap. Hitting the cap is logged, not reported as an error, because on a broad facet it happens every day.
- **`created_at` is when freehire first saw the posting**, not when the employer published it. When freehire adds a new upstream, that upstream's old postings look fresh for a few days.
- **Descriptions are full in practice, not by guarantee.** The API keeps the index preview for a hit whose database row has vanished. Nothing in the hit shows when that happened, so descriptions are not marked `snippet_only`.
- **Not verified live:** the `description_format=text` rendering for every upstream, the remote query with `regions=eu&regions=global` (only the facet counts were captured), pages after the first, and hits with `closed_at` set (the adapter drops them).

## Configuration

```yaml
# config.yaml
sources:
  freehire: true          # default false

# watchlist.yaml (defaults shown; the shipped file repeats them)
freehire:
  queries:                # facets sent as written; a list becomes a repeated param
    - countries: [pl]
      category: [data_engineering, ai_engineering, data_analytics, data_science, ml_ai]
    - work_mode: remote
      regions: [eu, global]
      category: [data_engineering, ai_engineering, data_analytics, data_science, ml_ai]
  limit: 50               # hits per request, 1–100
  max_requests: 6         # per run, shared by the queries, 1–20
  exclude_upstream: [whatjobs-pl, energyjobline]
```

A query may not set `limit`, `offset`, `description_format` or `posted_within_days`. `config.validate` reports malformed settings when the source is on, and the adapter drops them again if it is called directly. Requests go through `util.http_get` with the project's `job-hunter/1.0` User-Agent and its bounded retry on 429/5xx. Before raising `max_requests`, remember that each retry is another request against freehire's 300/600-per-window limits.

Errors start with `freehire:`, so the digest's health table shows them on this source's row. A non-mapping body or a missing `data` list is reported as shape drift and never counted as an empty market. A page where every hit is unusable is reported too. HTTP failures, 404 included, are errors.

## Fixtures and verification

`tests/fixtures/freehire_search.json` contains four hits reduced from the 2026-10-22 capture: two re-dated Capco hits, the emagine hit and the multi-country Telnyx hit. Descriptions, skills and requirement lists are trimmed, and the vote and view counters are dropped. Five more hits, with slugs ending in `synthNN`, are synthetic contract cases in the same shape: an entity-escaped title, a missing company, a `whatjobs-pl` upstream, an onsite posting with guessed salary and contract type, and a hit without `created_at`.

```sh
.venv/bin/python -m pytest tests/test_freehire.py -q
.venv/bin/python -m pytest -m network -k freehire tests/test_live_contract.py   # live, 2 requests
```

The offline tests cover every row of the mapping table, envelope drift, 404 and 403, the shared request cap, the upstream exclusion, dedupe against a Greenhouse copy of the same URL, the auto-apply refusal, the default-off switch and config validation.
