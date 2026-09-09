# Job Hunter roadmap: user actions and AI delivery

Updated 9 September 2026. The goal is to find more distinct, relevant Poland opportunities, beginning with Kraków, while preserving the normal pipeline and application history. This roadmap follows the [Astra coverage review](COVERAGE_FUTURE_2026-09-09.md). It does not promise coverage of every company.

## Your actions

| Action | Why your input matters | When needed | Current state |
|---|---|---|---|
| Confirm Polish proficiency and permission to work in Poland | These are personal facts; the AI must not infer them from an English CV or current address. | Before declaring eligibility or preparing an application | Asked; unknown until answered |
| Set acceptable locations and attendance | Kraków is a candidate location. Warsaw hybrid, relocation and Poland-remote roles need your preference. | Before final shortlist decisions | Kraków included; other preferences pending |
| Set salary floor and contract preferences | Hourly B2B and monthly employment offers cannot be treated as equivalent. | Before ranking compensation | Pending |
| Mark a small sample “pursue”, “stretch”, or “dismiss”, with a reason | This supplies real relevance labels beyond synthetic parser tests. | First explorer review, then weekly | Pending first review |
| Connect Adzuna credentials or authorize alert-mail access if that experiment is selected | Account access belongs to you. Put secrets in the existing credential mechanism, never in this roadmap or chat. | Only for those later integrations | Optional; not required for current build |
| Decide which applications to send | An employer lead or unassessed explorer result is not application approval. | After full requirements and personal facts are checked | No new submissions authorized by this roadmap |

You do not need to implement parsers, edit the watchlist by hand, diagnose failures, run regression tests, or coordinate the agents. Those are AI tasks.

## AI actions: current implementation batch

Orca run: `run_4fbe8b4ab269`. All three implementation workers use Codex Astra and share this checkout with distinct file ownership. Each worker implements and tests, requests coordinator review, and remains responsible for any corrections. Worker completion alone is not acceptance.

| ID | Deliverable | Owner | Acceptance requirement | State |
|---|---|---|---|---|
| AI-01 | Versioned 20-employer Poland registry, at least ten with evidenced Kraków relevance; offline validation and summary CLI | Registry Astra | Official evidence and dated unknowns preserved; unique identifiers; truthful health/coverage summaries; schema and CLI tests | Accepted: 20 Poland / 14 Kraków; 64 focused tests |
| AI-02 | Bounded Allegro public-careers collector | Source Astra | Actual public listing/detail contract; stable identity; unknown dates remain unknown; pagination/request limits; failures and closure fixtures; downstream checks | Accepted after correction; 57 source tests |
| AI-03 | Separate 30-day opportunity explorer, with a maximum 90-day window and capped undated review | Explorer Astra | Original dates and daily configuration preserved; distinct HTML/JSON output; unassessed/unknown-availability labels; no scoring, CV, production tracker, mail or application side effects | Accepted after two correction rounds; 32 focused tests |
| AI-04 | Integrate the new source and preserve hard-filter rejection counts in saved run statistics | Coordinator | Source selection/configuration consistent; older configs still work; rejection evidence survives serialization; focused integration tests | Complete: source selection and saved counters checked |
| AI-05 | Review all changes, return defects to their original worker, run regression checks and refresh Graft | Coordinator + original owners | Review defects resolved; relevant tests and full offline suite pass; remaining live limitations recorded | Complete: 2,411 tests passed; Graft refreshed |

The registry is a reviewed discovery inventory. It must not silently enroll every listed employer into daily retrieval or equate a known careers URL with a healthy complete feed. The explorer is a separate research command and does not replace daily alerts.

## Review and correction procedure

1. Assign a bounded task, files, test requirements and request budget to one worker.
2. Worker implements, runs focused tests, and requests coordinator review before completion.
3. Coordinator inspects code and evidence, checks interactions with existing configuration/identity/history, and independently exercises important behavior.
4. If a defect is found, mark the review **changes required**, send the reproduction and expected behavior to the same worker, and repeat the review after correction.
5. Mark **accepted** only after the corrected result passes. Run the integrated offline regression suite after all accepted parts are present.
6. Record what was proven by fixtures, what was observed live, and what still needs user labels or longitudinal observation. Release workers when their accepted assignment is complete.

No stage grants permission to send an application, subscribe to a paid service, or change personal facts. Existing user-authorized work and reversible implementation do not need another confirmation.

## AI actions after the current batch

These are subsequent measured increments, not claims that they have already run.

| Order | AI work | Dependency / evidence required | Pass condition |
|---|---|---|---|
| 1 | Reconcile the pilot registry with actual monitored endpoints and collect per-employer complete/partial/failed states | Accepted registry and collectors | Every pilot employer has a truthful state; filtered absence never closes a job |
| 2 | Selectively retrieve full descriptions for plausible snippet-only roles | Source fixtures and request budget | Language, experience and location evidence improves without unbounded fetches; unknowns remain explicit |
| 3 | Create a dated 50–100-job benchmark including rejected titles, snippets and stale/closed cases | Your pursue/stretch/dismiss labels | Retrieval and shortlist quality reported separately, with sample sizes and failure cases |
| 4 | Test adjacent titles such as product/experimentation analytics and ML software engineering | Held-out benchmark and preference labels | Useful rejected roles recovered without flooding the shortlist; normal policy changes are explicit |
| 5 | Sample directories, official searches and existing aggregators for new employers; expand toward 50 monitored targets | First cohort produces incremental useful opportunities within the measured review budget | Verified new employers and net-new pursue-worthy jobs, not just more rows |
| 6 | Compare one additional platform or discovery channel: Workday, another public career surface, Adzuna, or authorized alerts | Repeated relevant missing employers, access evidence and bounded cost | Added source contributes unique reviewed opportunities beyond existing sources |
| 7 | Observe seven daily runs and two to four weekly reviews; retain productive integrations | Elapsed real-world observation and your decisions | Source health, backlog age, unique useful yield and review effort remain acceptable |

Start with a proposed ceiling of 60 minutes/week for source/employer maintenance and 30 extra minutes/week for explorer review. These are pilot targets to measure, not verified operating costs. A 20- or 50-employer benchmark measures that cohort only. Scheduled runs, account connections and paid services will not be invented to satisfy an acceptance gate.

## Completion record

Review corrections so far:

- Explorer: added an explicit country selector, readable count tables and a no-enabled-source diagnostic. Independent review then reproduced a sponsored UK role leaking into `--country PL`; the original worker fixed the exception intersection and added PL-only, GB-only and combined-country regression cases. Coordinator reran 32 focused tests and the original reproduction successfully.
- Explorer replay: captured NFJ data reproduced 624 source records → 123 deduplicated identities → eight dated exploratory results at a fixed clock, preserving configuration and original records with network and SQLite calls blocked. All eight remain snippets, unassessed and availability-unknown. Browser DOM inspection confirmed labelled sections and no horizontal overflow at 881 pixels; screenshot capture timed out in Orca, so pixel-level review is unverified.
- Registry: early generated YAML aliases violated its own strict loader; the original worker corrected the artifact and shipped-file checks passed. The same worker corrected compatibility with the new limited Allegro collector while leaving live collector health unknown. Final summary: three supported routes, four unsupported, thirteen unknown; these are compatibility findings, not twenty verified monitors or live vacancies.
- Allegro: the original worker corrected the employer-feed identity namespace, added an injectable expiry clock, and detected repeated/truncated pages against provider totals. Coordinator reviewed the code and independently passed 252 source/main/config/model tests.
- Cross-component replay: both normal source dispatch and the explorer executed the actual Allegro collector against two captured listing pages plus the captured SAP detail, with real network and SQLite calls blocked. Each used three fixture requests and returned 20 records with explicit partial-coverage errors. The 30-day explorer displayed one dated AI Hub role and one undated review item; eighteen records failed existing filters. Configuration remained unchanged. The strict 72-hour policy still yields zero from this captured set. This proves integration behavior against captures, not current availability.
- All three implementation assignments completed and their terminals were released. An additional reuse attempt for an independent integration review failed before delivery because the terminal displayed an interactive model/rate-limit reminder. That failed dispatch is recorded honestly in Orca; the coordinator completed integration review and replay directly.

Final offline suite: **2,411 passed, one skipped, one expected failure, 69 network tests deselected**, in 66.17 seconds. `git diff --check` passed and `graft build` refreshed 77 Python files. The existing environment is Python 3.9.6, below the project's declared Python 3.10 minimum, and emits the existing urllib3/LibreSSL warning; supported-runtime and live-network validation remain unverified. No production tracker, paid model, email or application submission was used for these validation runs.

## Use the delivered tools

Offline registry commands:

```sh
.venv/bin/python -m src.employers validate
.venv/bin/python -m src.employers summary --json
```

Public-source exploration (these commands make bounded source requests and create separate HTML/JSON artifacts):

```sh
.venv/bin/python -m src.explore --country PL --days 30 --source nofluffjobs --source allegro --undated-limit 10
```

Regression command:

```sh
.venv/bin/python -m pytest -o addopts='' -m 'not network' -q
```

Read the [registry contract](EMPLOYER_COVERAGE.md), [Allegro evidence and limits](ALLEGRO_SOURCE.md), and [explorer usage](OPPORTUNITY_EXPLORER.md) for interpretation. Reviewable captured-data examples from this session: [NFJ explorer](../exploration_output/explore_ohxgd23x/exploration.html) and [Allegro explorer](../exploration_output/explore_hz8irn8j/exploration.html). These local generated artifacts are ignored by git and are not live vacancy confirmations.

The current implementation batch is complete. The next AI increment is per-employer monitoring state and selective full-description enrichment; benchmark tuning then depends on your relevance labels. Coverage expansion and weekly observation remain planned roadmap work, not completed claims or configured automations.
