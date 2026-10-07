# Singapore data-science job search: research notes

Research date: **2026-10-07** (revised the same day after coordinator follow-up). Everything here was
fetched live from public, unauthenticated HTTPS endpoints with a descriptive User-Agent
(`JobHunterResearch/0.1 (personal job-search research; contact via repo owner; 1 req/s)`), at most one
request per second per host, with backoff on 429/503. robots.txt was checked on each host before any
job endpoint was called. No browser, no accounts, no logins, no applications. The monitor built on this
research must never use accounts either.

Files this research owns:

| File | What it holds |
|---|---|
| `config/sg_companies.yaml` | `companies:`: 25 verified endpoints plus 15 seed companies that need manual checks |
| `config/sg_sources.yaml` | `sources:`: MCF, three boards, three recruiters and LinkedIn, with access policy and manual-alert routine |
| `config/sg_monitor.yaml` | `ep_thresholds:` (MOM, age 25), `search_terms:`, `llm` / `scoring` model, schedule |
| `tests/fixtures/sg_research/companies/*.json` | Per-company fetch evidence: robots check, HTTP status, counts, SG DS matches |
| `tests/fixtures/sg_research/candidates/*.json` | Full fetched postings for the qualifying-candidate samples |
| `tests/fixtures/sg_research/boards/` | Raw robots.txt bodies and `board_policy_checks.json` |
| `tests/fixtures/sg_research/policy/mom_ep_qualifying_salary.json` | MOM's full EP age table (current and from 2027-01-01) |
| `tests/fixtures/sg_research/policy/ashby_public_job_posting_api.md`, `rfc9309_2.3.1.3-2.3.1.4.txt` | Fetched basis for the Ashby robots-401 exception |

## Two gates, kept separate

* **`status: verified`**: the endpoint answered HTTP 200 with a parseable job payload on the
  `evidence.checked_at` date. This only says the endpoint works.
* **`automation_allowed: true`**: the pipeline may poll it. This is set only when the provider
  documents the API for public use, robots.txt on the origin actually called permits it, and no
  `policy_hold` applies. Unknown or unclear means `false`, which means manual.

robots.txt is per-origin, and each case was judged on the origin actually called.

### Ashby: the robots-401 exception, and only that

`https://api.ashbyhq.com/robots.txt` answers **HTTP 401**. (`jobs.ashbyhq.com` disallows `/api/`, but it is
a different origin and is never called.) Fetched basis, 2026-10-07:

* **RFC 9309 §2.3.1.3 "Unavailable"** (https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3): for a
  4xx robots response (HTTP 400–499), "the crawler MAY access any resources on the server."
* **RFC 9309 §2.3.1.4 "Unreachable"**: for 5xx or network errors the crawler "MUST assume complete
  disallow". The brief cited §2.3.1.4; that section is the fail-closed rule, and the permissive clause
  is §2.3.1.3.
* **Ashby's official docs** (https://developers.ashbyhq.com/docs/public-job-posting-api, `updatedAt
  2026-05-26`; the docs host's robots.txt allows `/`) document an unauthenticated
  `GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}` that "allows you to get data for
  all currently published Job Postings". The docs frame it for an organization's own careers page and
  put no restriction on reading it.

Each Ashby row therefore carries:

```yaml
robots_unavailable:
  http_status: 401
  policy: documented_public_api
  path_prefix: /posting-api/job-board/
  documentation_url: https://developers.ashbyhq.com/docs/public-job-posting-api
  reference_url: https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3
  verified_at: "2026-10-07"
```

The runtime recheck recorded HTTP 401 with the plain text `Unauthorized` and no `WWW-Authenticate`
challenge in `policy/ashby_robots_runtime.json`. Only an empty body or that exact plain response is
accepted; HTML, robots directives and other bodies remain blocked.
The exception applies only when robots.txt returns exactly that status, the request is to
`api.ashbyhq.com` under that path prefix, and the row is enabled with no `policy_hold`. These cases still
**fail closed**: a robots 5xx or network error, a robots 200 with a matching Disallow, any other robots
status, and a 401/403/429/5xx on the job endpoint itself. The exact rule was sent to the adapter owner
(dispatch `ctx_b8c533a30f14`).

### Defence exclusion

* **Palantir was removed** as a defence-focused employer. Salesforce, a Workday tenant already fetched
  on 2026-10-07, replaced it.
* The user confirmed that ordinary civilian roles at general technology companies such as OpenAI
  should be included. OpenAI's public posting API is enabled. Defence-focused employers and
  defence-related roles remain excluded.

## Verified company endpoints (25)

`SG` is the exact count of postings with Singapore in their location (Greenhouse, Ashby). For Workday it
is the keyword-search total for "Singapore", which is approximate. `DS` is the number of SG postings
whose title matches data scientist / ML / AI engineer / applied or decision scientist. For Workday this
only covers the first page of a "data scientist Singapore" search.

| Company | ATS | Identifier | FS | Seed | HTTP | Total | SG | DS | automation_allowed |
|---|---|---|---|---|---|---|---|---|---|
| Micron Technology | workday | `micron.wd1.myworkdayjobs.com / micron / External` |  | seed | 200 | 3125 | 881 | 1 | false |
| Agilent Technologies | workday | `agilent.wd5.myworkdayjobs.com / agilent / Agilent_Careers` |  | seed | 200 | 345 | 21 | 2 | false |
| HP Inc. | workday | `hp.wd5.myworkdayjobs.com / hp / ExternalCareerSite` |  | seed | 200 | 900 | 74 | 1 | false |
| Applied Materials | workday | `amat.wd1.myworkdayjobs.com / amat / External` |  | seed | 200 | 2000 (API cap) | 354 | 1 | false |
| GlobalFoundries | workday | `globalfoundries.wd1.myworkdayjobs.com / globalfoundries / External` |  | seed | 200 | 735 | 243 | 1 | false |
| Expedia Group | workday | `expedia.wd108.myworkdayjobs.com / expedia / search` |  | seed | 200 | 172 | 3 | 0 | false |
| Mastercard | workday | `mastercard.wd1.myworkdayjobs.com / mastercard / CorporateCareers` | Y | seed | 200 | 1085 | 44 | 3 | false |
| Visa | workday | `visa.wd5.myworkdayjobs.com / visa / Visa` | Y | seed | 200 | 797 | 95 | 1 | false |
| UOB (United Overseas Bank) | workday | `uobgroup.wd3.myworkdayjobs.com / uobgroup / UOBExternal` | Y | seed | 200 | 1043 | 1043* | 0 | false |
| Unilever | workday | `unilever.wd3.myworkdayjobs.com / unilever / Unilever_Experienced_Professionals` |  | seed | 200 | 231 | 0 | 0 | false |
| Agoda | greenhouse | `agoda` |  | seed | 200 | 307 | 7 | 6 | **true** |
| Airwallex | ashby | `airwallex` | Y | seed | 200 | 540 | 153 | 13 | **true** |
| Stripe | greenhouse | `stripe` | Y | ext | 200 | 718 | 53 | 0 | **true** |
| Databricks | greenhouse | `databricks` |  | ext | 200 | 888 | 29 | 0 | **true** |
| Datadog | greenhouse | `datadog` |  | ext | 200 | 435 | 17 | 0 | **true** |
| Coinbase | greenhouse | `coinbase` | Y | ext | 200 | 223 | 7 | 1 | **true** |
| Okta | greenhouse | `okta` |  | ext | 200 | 372 | 5 | 0 | **true** |
| MongoDB | greenhouse | `mongodb` |  | ext | 200 | 392 | 9 | 0 | **true** |
| Pinterest | greenhouse | `pinterest` |  | ext | 200 | 178 | 13 | 0 | **true** |
| Elastic | greenhouse | `elastic` |  | ext | 200 | 419 | 6 | 0 | **true** |
| OpenAI | ashby | `openai` |  | ext | 200 | 823 | 38 | 2 | **true** |
| Salesforce | workday | `salesforce.wd12.myworkdayjobs.com / salesforce / External_Career_Site` |  | ext | 200 | 1523 | 31 | 0 | false |
| Point72 (incl. Cubist Systematic Strategies) | greenhouse | `point72` | Y | ext | 200 | 215 | 10 | 3 | **true** |
| Tower Research Capital | greenhouse | `towerresearchcapital` | Y | ext | 200 | 93 | 21 | 1 | **true** |
| Snowflake | ashby | `snowflake` |  | ext | 200 | 353 | 3 | 0 | **true** |

\* UOB's Workday site returns the same total for "Singapore" as for an empty search, so keyword
search doesn't filter location there.

Totals: **25 verified** (12 seeds, 13 extensions). **14 automation-enabled**: 11 Greenhouse (Agoda,
Stripe, Databricks, Datadog, Coinbase, Okta, MongoDB, Pinterest, Elastic, Point72, Tower Research) and
3 Ashby (Airwallex, OpenAI, Snowflake). **11 manual**: the Workday tenants.
`src.singapore.config.validate_entries` returns no errors. Greenhouse board names were
cross-checked through `/v1/boards/{token}`, and Workday tenants were reached from company careers pages.
Tower's official [roles page](https://tower-research.com/roles/) embeds the `towerresearchcapital`
Greenhouse board. Its public board and jobs payload were fetched and recorded in
`tests/fixtures/sg_research/companies/tower_research.json`.

Changes in this revision: Palantir removed (defence); Salesforce, Point72 and Tower Research added; Airbnb
and Ripple dropped to stay at 25 (each had 4 SG postings and no DS role; their fixtures stay as
evidence). Four more Workday tenants were verified but are not targets: NVIDIA (`nvidia.wd5 /
NVIDIAExternalCareerSite`), PayPal (`paypal.wd1 / jobs`), Citi (`citi.wd5 / 2`), Autodesk (`autodesk.wd1 / Ext`).

### Policy per ATS

| ATS | Origin called | robots.txt | Terms / docs | Gate |
|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io` | only `/embed/` disallowed | Job Board API documented as public GET (developers.greenhouse.io/job-board.html) | **true** |
| Ashby | `api.ashbyhq.com` | 401: "Unavailable", RFC 9309 §2.3.1.3 | Public job posting API documented | **true**, only via `robots_unavailable` (see above) |
| Workday | `{tenant}.wdN.myworkdayjobs.com/wday/cxs/...` | per-tenant; `/wday/cxs/` not disallowed on any tenant checked | CXS endpoint undocumented. Workday site terms (updated 2026-08-13) forbid "data mining, robots or similar data gathering or extraction methods designed to scrape or extract data from our Sites" | **false**: stays manual |
| SmartRecruiters | `api.smartrecruiters.com` | `User-agent: * Disallow: /` (LinkedInBot only) | n/a | **forbidden**, not fetched |

No Lever target remains after Palantir's removal. The Lever API (`api.lever.co`, `Allow: /`) was
verified and would still qualify.

## Live candidate research (2026-10-07)

Every SG data-science role found on the original boards was Senior/Staff/Lead/Principal, and those are
never relabelled. Point72 and Jump Trading surfaced roles without a seniority marker, but they fail
the requested degree or title filters below. Tower Research provides a qualifying secondary title.
Fetched Point72 and Jump postings remain in `tests/fixtures/sg_research/candidates/` as research evidence.

**1. Primary title: Point72 / Cubist, "Cubist Data Scientist"**
- URL: https://boards.greenhouse.io/point72/jobs/7001158002?gh_jid=7001158002 (Greenhouse job `7001158002`;
  first published 2023-11-20, updated 2026-09-15, which looks like an evergreen requisition)
- Location: Singapore. Employer: Point72 (Cubist Systematic Strategies), asset management, so the FS
  EP threshold applies (S$6,709 at age 25 before 2027; S$7,155 from 1 Jan 2027).
- Description: Cubist Data Services group. Onboard novel datasets, build and deploy data pipelines,
  feature creation, data-quality alerts, preliminary research for investment teams.
- Years: **no years-of-experience requirement stated**.
- Degree: "**Masters** in Financial Engineering, Statistics, Computer Science or other disciplines
  involving rigorous quantitative analysis". Python and SQL are required; AWS/Linux/Airflow and financial
  experience are "preferred but not required".
- Salary: not stated. **Excluded** by the user's mandatory-master's knockout rule.

**2. Outside the requested title list: Jump Trading, "Research Engineer – AI Agents"**
- URL: https://www.jumptrading.com/hr/job?gh_jid=6881808 (Greenhouse job `6881808`; first published
  2025-05-12, updated 2026-09-17)
- Location: Singapore. Employer: Jump Trading Group, quantitative trading (FS threshold).
- Description: build LLM-powered and agent-based tools for developers and quant researchers, from
  prototyping to deployment.
- Years: "**2+ years** of professional software engineering or applied AI experience".
- Degree: **none stated**. The posting talks about talent in Mathematics, Physics and Computer Science.
- Salary: not stated.

Jump Trading was replaced by Tower Research in the target list without widening the title filters.

**3. Eligible secondary title: Tower Research Capital, "Machine Learning Engineer"**
- [Direct company application](https://www.tower-research.com/open-positions/?gh_jid=7714667).
- Singapore is one of the listed locations; quantitative trading uses the financial-services threshold.
- Requires 2+ years of distributed-systems experience; no mandatory advanced degree is stated.
- Builds ML research infrastructure, distributed training pipelines and experiment tracking.
  It does not focus on pretraining foundation models or inventing deep-learning architectures.
- Salary is not stated. Flagged `secondary`; the existing scorer assesses actual profile fit.

Point72 "AI Data Scientist" is excluded (PhD plus 3+ years). Ordinary civilian OpenAI roles are
checked after the user's clarification; every posting still passes the same knockouts.

### Other live SG data-science postings (radar; all senior unless noted)

- GlobalFoundries, Data Scientist (no seniority in title; Workday, manual): https://globalfoundries.wd1.myworkdayjobs.com/External/job/Singapore/Data-Scientist_JR-2601445
- Agilent, Decision Support Data Scientist: https://agilent.wd5.myworkdayjobs.com/Agilent_Careers/job/Singapore-Yishun/Decision-Support-Data-Scientist--Global-Manufacturing-Analytics-_4039242-1
- Agilent, AI & Data Scientist – Smart Manufacturing: https://agilent.wd5.myworkdayjobs.com/Agilent_Careers/job/Singapore-Yishun/AI---Data-Scientist---Smart-Manufacturing_4039990
- Applied Materials, AI Engineer: https://amat.wd1.myworkdayjobs.com/External/job/SingaporeSGP/AI-Engineer_R2626141
- Mastercard, AP Lead Data Scientist – Financial Crime; Visa, Manager, Data Science & Analytics Consulting; HP, Lead Data Scientist; Micron, Sr Data Scientist
- Airwallex (13 SG DS/AI roles, all Senior/Staff/Lead); Agoda (6, all Lead/Staff/Principal); Coinbase, Senior Data Scientist

The Workday descriptions were not fetched (only search pages), so their years and degree requirements are unknown.

## Seed companies without a verified endpoint (15)

These are in `sg_companies.yaml` with `ats: custom`, `identifiers: {}`, `automation_allowed: false`.
No ATS IDs were invented for them.

| Seed | Status | Why |
|---|---|---|
| Booking.com | manual_check | jobs.booking.com is custom (iCIMS-backed); robots allows with crawl-delay 5, but there's no documented API |
| Grab | manual_check | grab.careers is a custom JS app (hireEZ); Greenhouse and Lever `grab` both 404 |
| Shopee / Sea | manual_check | career.sea.com is a custom Next.js site; its robots.txt path serves HTML, so policy is unclear |
| Traveloka | manual_check | Custom site; Greenhouse and Lever 404 |
| ByteDance / TikTok | manual_check | Custom recruiting platform; undocumented search API |
| Wise | manual_check | wise.jobs is Attrax (custom). Greenhouse `wise` exists (15 jobs) but wasn't confirmed to be Wise plc; `transferwise`, Ashby and Lever 404 |
| DBS | manual_check | robots.txt disallows `/careers/jobs.page`, so it was not fetched |
| OCBC | manual_check | Links to ocbc.taleo.net (Oracle Taleo); no documented public feed |
| Standard Chartered | unverified | Third parties say Taleo (`scb.taleo.net`), but that host didn't resolve |
| Sia Partners | manual_check | Custom opportunities page; no ATS API found |
| Frost & Sullivan | manual_check | Custom careers page; no ATS API found |
| McKinsey / QuantumBlack | manual_check | Custom search UI; no documented API |
| BCG X | manual_check | Phenom + Eightfold front end; no documented API |
| Procter & Gamble | manual_check | Phenom front end; no documented API |
| Dyson | unverified | careers.dyson.com returned 403; `dyson.wd3 / Careers` 404 |

Resolved through investigation: **Visa** (its careers page redirects to Workday `visa.wd5/Visa`; the old
SmartRecruiters API is robots-forbidden) and **Unilever** (Radancy front end that links to Workday).

## Job boards and recruiters

On 2026-10-07 no board qualified for automation. Every one is either prohibited or unclear, so all are
manual. The `manual_alert` steps are things the user does personally; the monitor never signs in to,
reads or polls any account or mailbox. Full text is in `config/sg_sources.yaml`; evidence is in
`tests/fixtures/sg_research/boards/`.

| Source | Frequency | robots.txt | Terms | Decision |
|---|---|---|---|---|
| MyCareersFuture | daily | allows all | SPA, text not readable statically; snippet bars storing content in a retrieval system without WSG permission | manual (unclear) |
| eFinancialCareers | MWF | `/search`, `/v1`, `/v3` disallowed; crawl-delay 10 | CAPTCHA wall | manual (anti-bot) |
| Glints | MWF | `*/opportunities/jobs/explore?*` disallowed | 403 | manual (prohibited) |
| Tech in Asia | MWF | robots.txt itself 403 | 403 | manual (unknown) |
| Michael Page | weekly | `Disallow: /` | n/a | manual (prohibited) |
| Robert Walters | weekly | every search-parameter URL disallowed | not found | manual (prohibited) |
| Hays | weekly | `/job-search/*` disallowed | "No framing, harvesting, "scraping"..." | manual (prohibited) |
| LinkedIn | daily | generic agents disallowed | User Agreement bans scraping bots | manual, user only; never fetched |

### MCF ads are radar, not proof of sponsorship

MOM's Fair Consideration Framework page (last updated 8 Aug 2025) says employers submitting EP
applications "must first advertise on MyCareersFuture". So an MCF listing only shows that the employer
is (or may be) recruiting. It doesn't show that they will sponsor a foreign candidate. Treat an MCF hit as a
lead. Shortlist only established, non-government, non-statutory, non-defence employers with explicit
evidence, then check eligibility against MOM's own criteria below.

## Employment Pass thresholds (MOM, verified)

Source: https://www.mom.gov.sg/passes-and-permits/employment-pass/eligibility, page "Last Updated: 28
April 2026", fetched 2026-10-07. Values are fixed monthly salary in SGD.

| Applies to | Non-FS, age 25 | FS, age 25 | Floor (≤23) non-FS / FS | Ceiling (≥45) non-FS / FS |
|---|---|---|---|---|
| New applications before 1 Jan 2027 (renewals expiring before 1 Jan 2028) | **6,064** | **6,709** | 5,600 / 6,200 | 10,700 / 11,800 |
| New applications from 1 Jan 2027 (renewals expiring from 1 Jan 2028) | **6,500** | **7,155** | 6,000 / 6,600 | 11,500 / 12,700 |

The user's figures (6,064 non-FS and 6,709 FS for 2026 at age 25) match MOM exactly. The January 2027
change is confirmed on the same MOM page. The salary threshold is necessary but not sufficient: COMPASS
needs 40 points unless the role is exempt. Check with MOM's Self-Assessment Tool:
https://www.mom.gov.sg/eservices/services/employment-s-pass-self-assessment-tool

## Scoring model

`config/sg_monitor.yaml` sets `llm: {provider: openrouter}` and `scoring: {model: google/gemma-4-31b-it:free}`,
the existing model the user chose.

## API schemas for the pipeline

* **Greenhouse**: `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs` (add `?content=true` for
  descriptions). SG match is on `location.name`. Enabled tokens: agoda, stripe, databricks, datadog,
  coinbase, okta, mongodb, pinterest, elastic, point72, jumptrading.
* **Ashby**: `GET https://api.ashbyhq.com/posting-api/job-board/{board}` (optional `?includeCompensation=true`).
  The response is `{apiVersion, jobs: [{title, location, secondaryLocations: [{location, address}],
  department, team, isListed, isRemote, workplaceType, descriptionHtml, descriptionPlain, publishedAt,
  employmentType, address: {postalAddress: {addressLocality, addressRegion, addressCountry}}, jobUrl,
  applyUrl, compensation?}]}`. SG match is on `location`, `secondaryLocations[].location`, or
  `address.postalAddress.addressCountry`. Enabled boards: airwallex, snowflake (openai on hold). Must
  apply the `robots_unavailable` rule above.
* **Workday** (reference only; manual): `POST https://{host}/wday/cxs/{tenant}/{site}/jobs` with body
  `{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}`. It returns `{total, jobPostings: [{title,
  externalPath, locationsText, postedOn, bulletFields}]}`, and `total` caps at 2000.

## How the research was done

1. Checked robots.txt on every ATS API origin (Greenhouse, Lever, Ashby, SmartRecruiters, each Workday
   tenant), and on developers.ashbyhq.com and rfc-editor.org before fetching the policy basis.
2. Found each seed's ATS by loading its public careers page once (after a robots check) and looking for
   ATS hostnames in the HTML, plus web search. Then probed candidate tokens on documented APIs.
3. Recorded per-company evidence in `tests/fixtures/sg_research/companies/` (trimmed summaries, not full payloads).
4. For candidate samples, fetched single Greenhouse job records (`/v1/boards/{token}/jobs/{id}`) and stored them in full.
5. For boards, fetched robots.txt and the terms page only. No board search endpoint was called.
6. Read the MOM EP eligibility table from the official page and stored it in full.
