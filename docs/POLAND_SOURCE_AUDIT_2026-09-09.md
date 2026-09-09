# Poland / Kraków source audit — 2026-09-09

Before fixes, the adapters retrieved one fresh DS/ML candidate after the real dedupe-before-filter sequence: Ework Group's Data Scientist/Analyst in Warszawa. Two concrete losses explained additional coverage: No Fluff Jobs discarded the source's `POL` country code, hiding a fresh DCG remote-Poland role; deduplication selected a stale Scalo snippet over a fresh same-company/title/city posting. Both fixes and the requirement/salary evidence fixes are now implemented; the [final combined trial](POLAND_TRIAL_2026-09-09.md) recovered three fresh listings and passed 2,256 tests. The audit below preserves the original observations and counterfactuals. Allegro's configured SmartRecruiters board is a migration notice, not evidence that Allegro has only one vacancy.

## Scope and evidence

Public retrieval occurred on 2026-09-09, requests starting 09:38:24–09:40:35 UTC. Freshness reference: `2026-09-09T09:38:24.696303+00:00` (the exact timestamp is in `settings.json`). Six actual HTTP attempts of the allowed 20 were used: four listings and two individually selected ATS details. No retries or redirects; each `requests.get` used `timeout=15`. This is the Requests connect/read timeout, not a strict total wall-clock deadline; the NFJ request occupied roughly 31 seconds including connection/body processing. No sandbox denial occurred. Just Join IT's 503 was a server response, not a sandbox error.

Only the configured `allegro` and `smartrecruiters` ATS tenants were examined. The Allegro detail used canonical `Allegro` returned by its listing. No alternate tenants, employer discovery, third-party search, or detail fanout was performed. Production code, config/watchlist, production DB, submissions, email, paid models, installs and commits were untouched. All job processing was in memory; no tracker or DB was opened. This reproduces admission with empty prior tracker state, not the user's historical eligibility or backlog.

Local evidence root: `/tmp/job-hunter-poland-2026-09-09/`.

- `requests.json`: durable attempt ledger, URL/params/status/timeouts, redacted non-JSON 503 preview.
- `raw-02.json`: public NFJ listing JSON, with contact/token/cookie/recruiter/tracking fields and email addresses removed; `raw-03.json` / `raw-04.json`: ATS listings; `raw-05.json` / `raw-06.json`: ATS details. Unneeded logos and custom/internal field metadata were removed too. Response headers and credentials were not saved.
- `normalized-listings.json`: real adapter `Job` output before dedupe/filter, with original date and snippet provenance.
- **`pipeline-funnel.json`: authoritative production-order counts**, including every retained option and rejection reason, plus country-only counterfactual.
- `funnel.json` and `counterfactual.json`: diagnostic filtering of raw normalized rows before dedupe. These are explicitly **not** production-order survivor counts.
- `selected-evidence.json`, `poland-options.json`: compact public listing evidence, including actual requirement tiles and raw salary units.
- `smartrecruiters-allegro-detail.json`, `smartrecruiters-smartrecruiters-detail.json`: normalized detail text.
- `audit.py`, `details.py`: bounded retrieval scripts. `analyze.py`, `counterfactual.py`, `pipeline_order.py`, `verify.py`: offline reproducible analysis. Do not rerun retrieval without a fresh authorized budget; the ledger refuses repeated requests.

## Source health and funnel

The actual source settings, title lists and filter values are captured in `settings.json`. Existing configuration uses 72 hours, skips undated jobs, requires an allowed-country/European hint for remote roles, allows English, and excludes senior and internship titles. The allowed-country list includes Poland and 17 other European countries; every surviving option here is Poland. This audit did not silently narrow or widen those filters.

“Snapshot” below changes **only** freshness in a copied in-memory config: unlimited age, undated permitted. It does not claim jobs are new or bypass titles, location, language or employment checks. All 626 normalized jobs had source dates: 41 NFJ rows were within 72 hours, 583 NFJ rows were stale, and both ATS rows were stale. There were no undated or future-dated normalized records.

| Source | HTTP result | Raw listing rows | Adapter jobs | After real dedupe | 72h kept | Snapshot kept |
|---|---|---:|---:|---:|---:|---:|
| Just Join IT | **503 failure**, HTML nginx error | Unavailable | 0 | 0 | 0 | 0 |
| No Fluff Jobs | 200, valid populated envelope | 18,630 | 624 | 123 | **1** | **4** |
| SmartRecruiters / allegro | 200, migration-notice listing | 1 | 1 | 1 | 0 | 0 |
| SmartRecruiters / smartrecruiters | 200, real populated listing | 1 | 1 | 1 | 0 | 0 |
| Total | One failed source | 18,632 observed | 626 | 125 | **1** | **4** |

No valid empty feed was observed. Both ATS feeds were populated but yielded valid **filtered empties**. Allegro is a semantically obsolete board; Just Join IT coverage is unknown because it failed. NFJ reports `totalCount=18630`, `pageUniqueCount=2513`, `totalUniqueCount=2513`; raw rows include regional variants, so 18,630 is not a unique-vacancy count.

NFJ adapter gates: 16,479 rows rejected by category, 1,527 by seniority, 624 normalized, zero remaining malformed rows. Its 624 snippets reduce to 123 fuzzy/canonical identities. Real production-order first rejection counts for those 123: title not included 113; title excluded 2; location 4; stale 3; kept 1. Snapshot: identical title/location counts, no stale drops, kept 4. Each ATS row fails title inclusion in both views.

All 624 NFJ descriptions are synthesized **snippets**, none full or missing. Their short descriptions bypass the 150-character language detector threshold, so “passed filters” is **not evidence that Polish is unnecessary or the advertisement is in English**. The two ATS descriptions were initially missing; two selected detail calls obtained both bodies (one migration notice, one vacancy). These details do not change the title-based rejections.

Raw-row diagnostics explain scale but must not be confused with the table: applying filters before dedupe gives 2 fresh NFJ rows, 68 location drops, 4 stale drops and 550 title drops; snapshot gives 6 kept rows. The production sequence is `src/main.py:743–771`: dedupe first, filters second.

## Public options and actual available requirements

These are options to inspect, not validated application matches. NFJ requirement evidence is limited to its public `tiles.values[type=requirement]`, technology and seniority fields; full years-of-experience, language, education and detailed responsibilities were not retrieved. All listed NFJ DS roles are marked **Mid**, which needs checking against approximately 1.5 years of experience.

### Source-dated within 72 hours

| Option | Location / source date (UTC) | Requirements actually available | Current result |
|---|---|---|---|
| [Ework Group — Data Scientist/Analyst](https://nofluffjobs.com/job/data-scientist-analyst-ework-group-warszawa) | Warszawa; Sep 7 10:39 | SQL, Python, ML; Mid; B2B 100–130 PLN/hour | **Only current production survivor**; strongest visible overlap with Python/SQL/tabular ML |
| [Scalo — Data Scientist](https://nofluffjobs.com/job/data-scientist-scalo-warszawa-7) | Warszawa; Sep 8 13:32 | Machine learning, SQL, Python; Mid; B2B 90–100 PLN/hour | Fresh row exists, but stale Scalo variant wins dedupe and then fails freshness |
| [DCG — Data Scientist](https://nofluffjobs.com/job/data-scientist-dcg-remote-2) | Remote; explicit Poland in location places; Sep 8 12:18 | Python, Power BI, Tableau; Mid; B2B 120–150 PLN/hour | Country parsing defect drops it; BI tools need verification against candidate facts |

DCG's Poland eligibility is inferred from explicit `country.code=POL`, `country.name=Poland` in the posting's location places, not from the website domain. Its raw nested `location.fullyRemote=true` conflicts with top-level `fullyRemote=false`; the existing adapter uses the nested location field. Remote working terms still warrant checking on the actual ad.

### Exploratory older listings — not fresh

- [Meniga — Data Scientist, Warszawa](https://nofluffjobs.com/job/data-scientist-meniga-warszawa), **Aug 31**, Mid: Python, SQL, Data science. Useful visible overlap; survives current snapshot only.
- [Connectis_ — Data Scientist, Warszawa](https://nofluffjobs.com/job/data-scientist-connectis--warszawa-6), **Aug 28**, Mid: Python, Azure, Azure Machine Learning. Cloud tooling is a visible gap to check; survives current snapshot only.
- [Tesco Technology — Software Engineer (Machine Learning - MLOps), Kraków](https://nofluffjobs.com/job/software-engineer-machine-learning-mlops-tesco-technology-krakow-5), **Aug 20**, Mid: Python, MLOps, Machine learning. Relevant Kraków ML-adjacent option, but older and excluded by the current exact title phrases even in snapshot view; MLOps depth is unverified.
- [Verita HR — Junior Data Engineer, Kraków](https://nofluffjobs.com/job/junior-data-engineer-verita-hr-krakow), **Aug 25**, Junior: Python, Linux, AI/ML. Adjacent exploratory option, older and outside the current DS/ML title policy; do not count as a production survivor.
- [SmartRecruiters — Data Operations Consultant](https://jobs.smartrecruiters.com/smartrecruiters/744000137413079), **Jul 13**, remote Poland; published Associate level and **12-month fixed-term contract**. Full detail requires at least **2 years ETL** (Talend preferred), **1 year professional Python** (pandas, requests, FastAPI, Flask), SQL/NoSQL, Git, HTTP/web security and REST integrations. Python overlaps, but ETL experience is an explicit unverified gap and the role is integration/data operations rather than DS/ML; excluded by title and older than 72h.

NFJ adapter-retained rows with a Kraków city include 116 regional/multi-location rows representing 30 unique company/title pairs. No Kraków DS/ML title survived the existing configured title gate in this retrieval. This does not establish that Kraków has no relevant vacancies: Just Join IT failed, Allegro migrated, and the title gate excludes the Tesco wording above.

## Concrete defects and minimal proposed changes

1. **NFJ loses explicitly stated country (highest-priority source fix).** `src/sources/nofluffjobs.py:126–148` accepts only two-letter codes; public payloads frequently use `POL` and `country.name=Poland`. DCG normalizes to `location=Remote, country=None`, then `src/filters.py:334–440` correctly rejects the missing location evidence. An offline payload-only `POL→PL` conversion changes 586 of 624 normalized country fields, and recovers all four location-rejected deduped identities. Real production-order results change **1→2 fresh survivors** and **4→8 exploratory survivors**; the fresh addition is DCG. Minimal fix: normalize supported alpha-3 codes or use the explicit country name through existing geo normalization, retaining unknown for unrecognized values and preserving stated non-Poland countries. Never infer Poland merely from the NFJ host. Tests should cover POL/Poland, alpha-2 input, a non-EU country, unknown/absent codes, remote-only locations, unchanged identity/date and the actual location filter.

2. **Snippet richness hides a fresh Scalo role during dedupe.** `src/filters.py:735–756` prioritizes description length before recency, while `src/main.py:743–771` dedupes before freshness. Scalo `-5` (Aug 25, 31 chars), `-6` (Aug 25, 56 chars) and `-7` (Sep 8, 31 chars) share a fuzzy identity. `-6` wins because it alone has the synthesized “Main technology: Python” text, and is then dropped as stale. This is a reproduced contract interaction, not proof the three distinct vendor IDs represent the same requisition. Minimal scoped decision: stop treating differences in synthesized teaser length as stronger evidence than publication recency for same-source snippet-only fuzzy duplicates, or preserve distinct requisitions where vendor identity supports that choice. Do not globally invert source/date/full-description ranking. Test unequal-length stale/fresh snippets, preserve dated/full ATS preference, and check distinct vendor requisitions. `pipeline_order.py` asserts the current failure.

3. **Available skill requirements are omitted.** `src/sources/nofluffjobs.py:151–201` builds prose only from `technology`, seniority and category, ignoring `tiles.values`. Scalo's fresh normalized description has no Python/SQL/ML; DCG loses Power BI/Tableau; Ework loses Python/ML. Minimal fix: append deduplicated nonempty `type=requirement` tile values, retaining `snippet_only=True`; exclude category/promotional tiles and tolerate malformed collections. This improves evidence without pretending to retrieve a full ad. Add live-shaped fixtures and verify downstream snippet provenance.

4. **Salary loses pay period.** `src/sources/nofluffjobs.py:113–123` serializes DCG's `120–150 PLN`, omitting raw `period=Hour` and `type=b2b`. This makes hourly and monthly figures ambiguous. Minimal fix: preserve recognized period in display and contract/period metadata without conversion; leave unknown period unknown. Test Hour, Month and absent period. The table above uses actual raw hourly units.

5. **Allegro watchlist guidance is obsolete.** `watchlist.yaml:158–160` suggests trying capitalization because one posting seems suspicious. The API actually returns company identifier `Allegro` and a 2025-08-04 migration announcement; the detail explicitly points to [Allegro's corporate site](https://home.allegrogroup.com/). Minimal follow-up, separately authorized/scoped: correct the watchlist note and discover the current public career source. No configuration or watchlist change was made. This was not an adapter parsing failure, nor evidence that capitalization fixes coverage.

6. **Just Join IT availability is unresolved, not a proven parser defect.** `src/sources/justjoin_it.py:233–285` correctly reports its failed first page. One HTTP 503 establishes failure for this run only. No endpoint replacement, additional headers, bypass, or repeat probes are justified by this snapshot. Preserve the health error and retry only in a later bounded run.

The Tesco title mismatch is an explicit policy scope choice, not automatically a parser bug. If desired, a narrowly tested ML/MLOps title variant can be considered separately; widening to all software/data engineering would materially change candidate scope.

## Validation and limits

`verify.py` passed offline assertions using the saved DCG record: original loss, source-country-only recovery, stable key/date, actual freshness, omitted tile skills/pay unit, and request cap. `pipeline_order.py` passed assertions for the stale Scalo dedupe winner and production-order baseline/counterfactual counts. These scripts invoke real parser/filter/dedupe functions; they do not substitute a mock scoring model or fabricate candidate skills.

All **157 focused existing tests passed** (exit 0): `.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/job-hunter-poland-2026-09-09/pytest tests/test_justjoin_it.py tests/test_nofluffjobs.py tests/test_smartrecruiters.py tests/test_filters.py`. Results are recorded in `focused-tests.txt`; the only warning was urllib3 reporting the local LibreSSL version. No production code was changed. The supplied 2,160-test baseline was coordinator context, not a full-suite result rerun by this audit. Raw fixtures remain local under `/tmp`; the only repository artifact written by this worker is this report.

## Implemented dedupe correction — 2026-09-09

The preceding audit records the original behavior. A subsequent scoped implementation changes only dedupe ranking in `src/filters.py` and adds regressions in `tests/test_filters.py`; the NFJ country adapter change belongs to another worker.

`_richness` retains its single lexicographic tuple: `(date present, non-snippet description length, source rank, original posting timestamp)`. A record marked `raw["snippet_only"]` contributes **zero** to description richness regardless of metadata/teaser length. Thus two snippet records are compared by source rank and then publication recency; a dated record with a real body still outranks a dated snippet, and a dated record still outranks an undated one. Empty bodies have zero richness as before. No pairwise comparator was introduced, so the ordering is transitive. Exact ties still select the first input, and canonical-URL grouping, fuzzy identity, first-seen group order, source-rank fallback for ATS records, and original dates are unchanged.

Nine new regression cases cover unequal-length stale/fresh snippets through both distinct URLs and a common canonical URL, dedupe followed by the real 72-hour filter, full descriptions versus newer longer snippets at the same/different source rank, mixed-source snippet ranking across all six input permutations, dated snippet versus undated full body, stable ties for dated and undated snippets, and an old ATS body versus a newer empty LinkedIn receipt. Before the code change these cases produced **7 failures and 2 passes**, establishing that the new behavior is tested rather than merely restated.

Validation command (no paid calls or external network):

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -o addopts='' -q -p no:cacheprovider tests/test_filters.py tests/test_edge_fetch.py tests/test_main.py tests/test_scoring_backlog.py
```

Result: **293 passed, 1 warning in 1.92 seconds**. The warning is the existing urllib3/LibreSSL environment warning. Existing edge-fetch tests cover cross-country identity and distinct cities; main/backlog tests exercise downstream admission. `git diff --check` passed for the owned code/test files. Only test-created temporary databases were used by the existing suite; the replay did not open a DB, and no production database was opened.

### Captured Scalo replay

Offline replay instantiated the actual `Job` records from `/tmp/job-hunter-poland-2026-09-09/normalized-listings.json`, using captured filters and `retrieved_at` from `settings.json`. It compared the old key to the implemented key and called real `dedupe` before `apply_filters`. This deliberately uses the **immutable pre-country-fix normalization**, isolating this change from concurrent adapter work.

| Measurement | Old key | Implemented key |
|---|---:|---:|
| NFJ normalized rows | 624 | 624 |
| After dedupe | 123 | 123 |
| Fresh survivors | 1 | **2** |
| Stale rejections | 3 | **2** |
| Location rejections | 4 | 4 |
| Title not included / title excluded | 113 / 2 | 113 / 2 |

Old Scalo winner: `data-scientist-scalo-warszawa-6`, original timestamp `2026-08-25T10:11:31.337000+00:00`. Implemented winner: [Scalo's current captured role](https://nofluffjobs.com/job/data-scientist-scalo-warszawa-7), original timestamp **`2026-09-08T13:32:38.144000+00:00`**. Assertions confirmed the winner's date was not manufactured or changed. Ework remains the other fresh survivor; DCG is still location-rejected in this isolated replay because its country correction is a separate change. No additional HTTP attempt was made; the retrieval ledger remains at six.

### Residual limitations

This fix selects a better representative; it does **not** prove that Scalo's three different vendor IDs represent one requisition. Existing fuzzy identity can still merge distinct requisitions sharing company, title and city. This scope intentionally leaves that identity policy unchanged. Also, a stale dated full description can still win over a fresh snippet, and an unmarked snippet still looks like a body because quality depends on truthful `snippet_only` provenance. Source rank continues to outrank recency when both candidates have zero body richness; this protects source authority but can retain an older authoritative title-only record. These are explicit remaining ranking/identity tradeoffs, not claimed resolved by this patch.

The coordinator will independently review the diff, replay combined country/dedupe changes and run the combined full suite. Graft is refreshed deterministically after the final implementation.
