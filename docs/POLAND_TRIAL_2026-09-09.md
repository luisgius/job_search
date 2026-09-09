# Poland / Kraków trial — 9 September 2026

The live trial retrieved real Poland listings and exposed four fixable retrieval defects. Three Codex Astra workers handled source auditing, independent job assessment, and implementation across five Orca tasks. The coordinator reviewed the changes and replayed the captured feed through the actual pipeline.

## Where to spend application effort

These are selective stretches against the configured approximately 1.5 years of experience, not verified full matches. Polish proficiency remains unconfirmed; the shortlist below favors the supplied English-speaking profile. Work authorization and degree requirements also need candidate confirmation.

| Option | Why consider it | Main reservation |
|---|---|---|
| [Lufthansa — Data Scientist, AI & Machine Learning](https://apply.lufthansagroup.careers/index.php?ac=jobad&id=134587), Kraków | Applied ML, Python and business problems; practical RAG/agent work is relevant to the recorded thesis project. | Requires 2+ years, practical LLM experience and permission to work in Poland. Hybrid attendance details need checking. |
| [Meniga — Data Scientist](https://nofluffjobs.com/job/data-scientist-meniga-warszawa), Warsaw | Python/SQL, behavioral features and churn/propensity models; English required, Polish only optional. | Requires 2–3 years; two office days weekly. Advertised B2B rate: PLN 18,000–22,000/month plus VAT. Validity displayed through September 30. |
| [Allegro — Data Scientist, AI Hub](https://careers.allegro.eu/job/Warszawa-Data-Scientist-Allegro-AI-Hub-00-841/1365885255/), Warsaw | Commercial ML, boosting, causal inference and large datasets. | Requires at least two years; Warsaw-based hybrid role. Language and exact attendance interpretation remain unverified. |
| [Amway — Data Scientist](https://jobs.amway.com/job/Data-Scientist/43227-en_US), Kraków | Strong forecasting, experimentation, causal inference and SQL overlap; English stated. | Explicit 3–5-year minimum and three office days: a substantial seniority stretch. |

Direct HTTP checks found Lufthansa and Amway available as complete job pages with status 200. HERE's attractive Data Scientist II requisition returned 410 despite cached search text, so it was excluded from current options. A displayed application control supports apparent availability, not guaranteed acceptance.

Full-ad review also found fluent Polish required at Ework, Polish C1 and 3+ years at DCG, and Polish plus 4+ years at Scalo. These jobs count as retrieval successes, not candidate recommendations. See the [independent options review](POLAND_OPTIONS_REVIEW_2026-09-09.md) for requirements, rates, sources and availability evidence.

## What the app retrieved and what changed

No Fluff Jobs returned 18,630 regional listing rows, of which its adapter retained 624 data/AI junior-or-mid rows. Deduplication reduced those to 123 identities. All final retained candidates in this trial were in Poland; no Kraków-office role passed the existing DS/ML title filter. This is a bounded source trial, not the whole Polish market.

| View | Before fixes | After fixes |
|---|---:|---:|
| Daily 72-hour policy | 1 fresh listing | **3: Ework, DCG, Scalo** |
| Exploratory view | 4 listings | **8 listings** |

The coordinator's exploratory replay uses a finite 90-day limit in a copied configuration; all baseline exploratory survivors fall inside it. Original posting dates remain unchanged. Older listings are not described as fresh or automatically eligible. The normal configuration retains its 72-hour policy.

Implemented fixes:

- Normalize observed ISO3 country codes, including `POL → PL`, without changing display locations or historical job identifiers. Secondary country evidence is retained in raw metadata.
- Prevent synthetic snippet length from overriding publication recency during deduplication; retain full-description and source-quality precedence.
- Preserve available requirement tiles while still marking the result as an incomplete snippet.
- Display explicit hourly/monthly salary units and retain contract/period metadata.

The Allegro watchlist comment now identifies the confirmed migration to SAP Harmony. The existing adapter still does not retrieve that new career site. Just Join IT returned HTTP 503 in this trial; its coverage is unknown, not zero available jobs. Full-ad language/experience checking remains essential because listing snippets omit requirements.

## Validation artifacts

Final offline suite: **2,256 passed, one skipped, one expected failure, 69 network tests deselected**. Command: `.venv/bin/python -m pytest -o addopts='' -m 'not network' -q`. All 96 added regression cases passed; `git diff --check` passed and Graft was refreshed. The existing Python 3.9.6 environment remains below the declared Python >=3.10 requirement, with the existing urllib3/LibreSSL warning. These checks found no regressions in covered behavior; they do not prove live provider availability or candidate fit.

The [source audit](POLAND_SOURCE_AUDIT_2026-09-09.md) contains request counts, captured evidence, before/after reproductions and scoped limitations. The combined replay preserved keys, locations, original dates and ATS identity across all 624 retained records. It produced both digests with an in-memory tracker, zero scoring attempts, no application outcomes, no CV reads, no network access and no LLM/application calls.

- [Fresh trial digest](/tmp/job-hunter-poland-integration/production72h/digest_2026-09-09.html)
- [Exploratory trial digest](/tmp/job-hunter-poland-integration/snapshot/digest_2026-09-09.html)
- [Replay results](/tmp/job-hunter-poland-integration/results.json)

Temporary artifacts are local and may be removed by system cleanup. No application was submitted, employer contacted, dependency installed, production tracker opened, or commit created.
