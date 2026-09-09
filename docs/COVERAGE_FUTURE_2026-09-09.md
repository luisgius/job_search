# Company coverage and the future of Job Hunter

Assessment date: 9 September 2026. Three Codex Astra workers reviewed implementation, external employer coverage, and product strategy through Orca run `run_93d4c7bfd275`. The coordinator exchanged their findings and challenged the proposed scope. This is a research and design review; the proposals below have not been implemented.

## Direct answer

**No: the app does not cover all companies, and we cannot currently calculate a defensible percentage of the Polish market covered.** The employer universe is undefined and changes; some hiring never becomes a public advertisement. Even public vacancies are split across company sites, ATS tenants, aggregators, alerts and custom portals.

The checked watchlist contains **36 employer-board entries across eight supported ATS vendors**: Greenhouse 20, Lever 1, Workable 5, Ashby 4, SmartRecruiters 2, Personio 1, Recruitee 2 and Teamtailor 1. This counts configuration entries, not 36 freshly verified active employers, and is not Poland-specific. Aggregator sources add employers beyond that watchlist, so 36 is not a cap on all employers the app can encounter.

Supporting an ATS does not discover all its customers. The fetch loop visits configured entries (`src/sources/ats_boards.py:2368–2400`); discovery starts with company names supplied by the caller, probes plausible tenant names, and prints suggestions (`src/sources/ats_boards.py:3109–3221`). Actual defaults are four slug variants per board/company and **160 board probes per run** (`2630–2673`); the watchlist's 120-request comment is stale. Personio fallback can use two HTTP requests for one probe. An unfinished probe is not evidence that a company has no jobs. Greenhouse's official public job-list endpoint itself requires a board token, illustrating this tenant-specific model. [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)

The configuration enables four broad feeds in addition to the ATS vendors: Arbeitnow, Landing.jobs, Just Join IT and No Fluff Jobs. Adzuna and LinkedIn email are implemented but disabled. Some feeds stop after three pages, and Just Join IT's maximum 300 requested rows include broad junior/mid roles before local DS/ML filtering. Employer counts and jobs discarded by source-level caps are not uniformly persisted. The [implementation inventory](COVERAGE_INVENTORY_2026-09-09.md) documents exact limits and source spans.

The Poland trial already demonstrated several separate gaps: Allegro's configured tenant only advertised its move to another platform; Just Join IT failed with HTTP 503; the daily 72-hour cutoff hid older opportunities; snippets omitted candidate requirements; and narrow title rules excluded adjacent roles. After parser and deduplication fixes, the captured feed yielded three daily results and eight exploratory results. Those are retrieval counts, not eight suitable jobs or an estimate of market recall. [Trial and validation](POLAND_TRIAL_2026-09-09.md)

## What the Astra discussion changed

The following is a paraphrased record of actual worker messages, not a simulated conversation.

- **Strategy Astra:** First separate fresh alerts from an open-opportunity explorer and measure what is lost. Thousands of extra slugs or a longer freshness window can add substantial inventory with little candidate value. Judge marginal verified, pursue-worthy opportunities per review hour.
- **Market Astra:** Official searches expose additional Poland employers and platforms. Prioritize official-careers verification, then choose the next adapter by unique relevant jobs recovered. Conflicting versions of the same requisition show why cached search titles cannot establish current seniority or availability.
- **Inventory Astra:** Four configured endpoints are the ATS vendors' own demonstration employers. Discovery cannot expand the employer universe by itself, and source-level caps can discard roles before the central filters see them. Blindly raising every cap is a poor substitute for identifying the actual losses.
- **Coordinator challenge:** Would a 50-employer pilot, including 20 relevant to Kraków, justify a company registry and weekly reconciliation of all open jobs?
- **Strategy response:** Start with 20 employers, including ten relevant to Kraków, and grow to 50 after checking remains affordable. A versioned data file is sufficient initially. Complete feeds can support reconciliation; partial sources and outages cannot establish that omitted jobs are closed.
- **Coordinator decision:** Accept the smaller first cohort and the distinction between complete and partial retrieval. Keep a declared 50-employer expansion target, not a claim of nationwide completeness. Measure benefit before creating a registry service or building multiple new ATS adapters.
- **Market objection on ordering:** Do not wait for the whole explorer and measurement system before repairing Allegro. Its current AI Hub role provides a concrete, relevant missing opportunity, whereas merely counting Workday employers would not establish equivalent value.
- **Coordinator resolution:** Prepare a small Allegro retrieval repair alongside the coverage work as the first implementation candidate. Record the careers frontend, application destination and underlying ATS separately when evidence supports each; a branded careers page does not prove its backend.
- **Final peer agreement:** Strategy accepted the bounded Allegro exception while reserving judgment on a platform-wide SAP adapter. Inventory required raw identities and explicit complete-fetch evidence before closing jobs through absence; filtered outputs are insufficient. The proposed 60-minute weekly maintenance ceiling remains an experiment, not an established cost estimate.

The [market review](COVERAGE_MARKET_2026-09-09.md) records twelve employer leads with official links, observed platforms and dated limitations. It carries forward Allegro, Lufthansa, Amway, SmartRecruiters, Fetcherr and HERE, and adds Hitachi Energy, Motorola Solutions, AstraZeneca, ING, Roche and Cisco to the prior research. This is twelve employers to investigate, not twelve open or suitable vacancies: Hitachi and ING detail checks failed with closure/not-found responses, HERE remains closed, Motorola was unreadable, and some other evidence is historical or employer-level only. The [strategy review](COVERAGE_STRATEGY_2026-09-09.md) contains the detailed proposed evaluation gates and operating budgets.

## Recommended expansion sequence

### 1. Make coverage visible and start a measured employer cohort

Create a small versioned employer inventory with a stable employer ID, official domain, careers URL, location evidence, observed ATS/tenant, discovery provenance, last successful verification, and retrieval status. Maintain separate states for verified live feed, verified empty feed, migrated, unsupported, partial, failed and not checked. An HTTP 200 migration announcement must not count as healthy vacancy coverage.

Start with 20 named Poland employers, including ten with evidenced Kraków relevance, spanning product businesses, financial services, enterprise technology, consulting and smaller firms. Do not constrain the entire cohort to convenient ATS vendors. Audit that cohort before increasing it to 50. These numbers are proposed operating limits, not evidence-based estimates of the total market.

### 2. Add recurring company discovery

Use three complementary inputs: employers found in existing aggregator results; targeted searches of official careers sites for role families and cities; and local business/member directories to discover companies that have not appeared in feeds. ASPIRE is a Kraków-focused technology/business-services association with a member directory, making it a useful employer seed source. Its extracted member page did not expose a usable complete list during this review, so no membership total or complete import is claimed. [ASPIRE](https://www.aspire.org.pl/), [member directory](https://www.aspire.org.pl/aspire-members/)

Follow employer domain → careers page → actual ATS links before trying guessed tenants. Deduplicate aliases and subsidiaries with retained provenance. Each new employer enters a verification queue; a discovered name alone does not become a daily fetch commitment. Keep per-host and total request caps, and show unfinished work explicitly.

### 3. Recover jobs at employers already known

Repair migrated coverage such as Allegro and verify supported ATS routes for new pilot employers before building a universal scraper. Choose an unsupported platform pilot only after demonstrating incremental relevant roles at several named employers. Workday, SAP/SuccessFactors and custom portals are candidates for investigation, not interchangeable public APIs.

Allegro is the concrete first exception to waiting for a multi-employer platform case: its official AI Hub page exposes SAP/SuccessFactors assets and an application control, and describes commercial ML work requiring at least two years of Python/SQL experience. This is a plausible stretch lead for the recorded profile and evidence of a retrieval gap, not a verified complete candidate match. [Allegro official role](https://careers.allegro.eu/job/Warszawa-Data-Scientist-Allegro-AI-Hub-00-841/1365885255/)

Adzuna is another bounded experiment because an adapter already exists: its official API provides keyword/location searches and requires an application ID and key. Credentials, terms, quota and actual Poland yield must be checked before enabling it; no coverage gain is assumed. Authorized existing job-alert ingestion is another discovery channel, with completeness explicitly unknown. [Adzuna API overview](https://developer.adzuna.com/overview), [search documentation](https://developer.adzuna.com/docs/search)

### 4. Separate current opportunities from new alerts

Retain the daily fresh feed and introduce a separate finite 30/90-day explorer, with explicitly verified undated opportunities handled separately. Preserve the original posting date alongside first-seen and last-verified timestamps. A week-old posting can still be open; a new search result can already be closed.

Retrieve full descriptions for shortlisted snippets before drawing conclusions about language, seniority, location, remote eligibility or salary period. Use visible employer content and structured `JobPosting` data as evidence, then verify closure signals. Search caches and structured data can lag: Google's own documentation describes expired advertisements remaining visible. Its Indexing API is for site owners to notify Google about their pages, not a global job-search retrieval API. [JobPosting documentation](https://developers.google.com/search/docs/appearance/structured-data/job-posting), [Indexing API purpose](https://developers.google.com/search/apis/indexing-api/v3/quickstart)

### 5. Widen role discovery with measured relevance

Create an adjacent-role review lane for titles such as decision scientist, product analytics, forecasting, risk modelling and machine-learning software engineer when their actual duties match the profile. Sample currently rejected titles before changing rules. Keep strong seniority, language and location evidence visible; unknown Polish proficiency must remain unknown. Expand aliases from reviewed examples rather than letting broad keywords flood the primary recommendations.

## How to tell whether the expansion works

Record the funnel by source and employer: attempted → complete/partial/failed → raw rows → unique requisitions → geographically plausible → title/experience plausible → full ad checked → candidate-relevant → selected for pursuit. Record exclusion reasons. Raw regional rows, deduplicated vacancies and employers are different units.

Use a dated manual benchmark of the pilot employers' official current roles, including both relevant roles and near misses. Report retrieval recall on this declared sample and shortlist precision on reviewed results, with counts and unknown coverage. An 80% result on twenty employers would be a pilot result, not 80% of Poland.

Proposed expansion gates: every cohort employer has an explicit status and evidence; no partial fetch is labelled complete; added sources deliver unique reviewed opportunities beyond the baseline; and weekly human review stays within a proposed 60-minute budget. If the cohort has no suitable openings, report that result and inspect the benchmark instead of manufacturing a growth target.

For future implementation, use captured source fixtures, pagination and outage cases, migration notices, incomplete descriptions, multi-location records and closed-job examples. Replay against the existing pipeline with an isolated tracker; verify stable identifiers/dates, duplicate behavior, unchanged normal alerts and zero application side effects. Use the existing scoring backlog and evaluation harness rather than rebuilding them.

## My assessment

The project has useful foundations: direct source adapters, bounded probing, deterministic filters, duplicate protection, persistent scoring work and regression coverage. Its largest opportunity now is learning **which employers and vacancies it misses**, then adding the highest-value missing routes. More agents or a larger model cannot recover jobs the retrieval system never encounters.

I would develop it into a personal opportunity monitor that explains what it checked, what it could not check, why a role was excluded, and when an employer changed platforms. Start with observable coverage, a small Poland cohort, full-ad verification and an open-opportunity view. Expand adapters and geography when measured gains justify their maintenance.

This review changes documentation only. The previous implementation's 2,256 passing offline tests remain historical validation; they were not rerun or claimed as new validation for this research task.
