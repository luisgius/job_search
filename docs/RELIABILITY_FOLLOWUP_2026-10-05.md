# Provider limits, source coverage and real-job evaluation — 5 October 2026

This follow-up starts from commit `9909485`. Work was isolated in `/tmp/job-followup-oct05/work`; the production tracker, application attempts and uncertain confirmation states were not modified. No applications or external messages were sent.

## Coordination and review

Orca run `run_934888988b23` used two Codex Astra workers (provider handling and ranking evaluation) and one Claude worker (source adapters). Cursor/Grok was not available because its CLI required authentication. Provider identity is evidenced by Orca launch receipts; the Claude model name was not reported by the runtime. All nine dispatched tasks completed. Independent review findings were returned to their original owner before acceptance. All three worker terminals were released after their final delivery; no reclaimable worker remains.

## Confirmed provider defect and correction

An offline reproduction generated six HTTP POST attempts across two jobs after HTTP 429 responses. The transport discarded status and retry/reset headers, preventing the chain from respecting provider cooldowns. The correction retains structured errors and rate headers, scopes cooldowns to the model/endpoint/credential evidence, and permits one recovery probe after expiry. Explicit shared free-quota evidence suppresses sibling free models; ambiguous upstream failures do not suppress unrelated models. Local fallback remains independent. Model IDs and generation settings are unchanged.

A single authenticated read-only quota request at **2026-10-05 10:32:49 UTC** returned HTTP 200: **3 used / 50 allowed / 47 remaining** free requests for that UTC day. These are a dated snapshot, not a future guarantee. Null key spending caps and a deprecated rate value of -1 do not demonstrate unlimited credits or throughput. Sanitized evidence: [quota snapshot](evidence/provider-quota-2026-10-05.json). No completion was requested from OpenRouter.

The historical 429 response bodies were not retained. Their exact scope remains unknown. A free-model alternative does not increase an exhausted shared allowance; it may help when the limit belongs to an upstream model/provider. See [OpenRouter limits](https://openrouter.ai/docs/api_reference/limits) and [error handling](https://openrouter.ai/docs/api_reference/errors-and-debugging).

Cooldowns are in memory per chain/client. They do not recall requests already in flight, persist across processes, or change external account limits. Anthropic SDK retry behavior is outside the confirmed HTTP defect.

## Ranking evidence policy

The frozen cohort contains 12 real European vacancies: seven retained backlog records and five public employer details retrieved in six bounded HTTP requests (one additional selected detail returned 404 and was excluded). Full descriptions and raw source evidence are also preserved locally under gitignored `output/audits/2026-10-05-followup/` with restricted file permissions; the repository dataset contains short factual excerpts, provenance and hashes. Publication, update and retrieval timestamps remain separate.

Before any model output, ten cases were selected: `real-01`, `real-03`, `real-04`, `real-05`, `real-06`, `real-07`, `real-08`, `real-09`, `real-10`, `real-12`. These include both proposed positive examples, the two historical scoring failures, experience gaps, senior product analytics and the explicit research-score cap. Wheely and Fin were omitted from the live subset; the complete cohort remains intact.

Labels are AI proposals until Luis explicitly validates them. No human-grounded ranking quality or improvement claim is supported by unreviewed labels. Employer/dedup/time groups are separated between tuning and held-out partitions; all records come from the same collection window, so this is not an out-of-time generalization test. The convenience sample cannot estimate market-wide recall or coverage of all companies. Unknown labels and failed evaluations are excluded from measured ranking denominators and reported separately.

## Action ownership

| Owner | Next action | Evidence required |
|---|---|---|
| AI | Maintain provider cooldown and source-contract regressions | Offline tests and bounded dated observations |
| Luis | Validate or correct pursue/decline/unknown labels | Explicit judgments with reasons; silence is not approval |
| AI, after labels | Recompute human-grounded metrics before changing prompts/models | Frozen labels, stable split, actual outputs and denominators |
| AI | Monitor source schema changes and partial collection | Fetch failures distinct from zero listings; publication dates not invented |
| Luis, only if desired later | Decide any paid-provider budget or account change | Actual quota evidence and explicit spending authorization |

Asia expansion is outside this follow-up. Increasing title breadth or model changes without evidence is not part of the implementation.

## Executed local benchmark

The unchanged local model `qwen3.8:27b` returned valid evaluations for **10/10 jobs, zero errors**, in **412.17 seconds**, with **10 actual HTTP requests**, no retries and no remote fallback. All responses reported that served model. Provider-reported usage was **35,624 input tokens and 4,731 output tokens**; monetary cost is unknown. Maximum requested output was 15,000 tokens; the run stayed below the 600-second deadline.

| Employer | Case | Proposed label (AI) | Score |
|---|---|---|---:|
| gravity9 | real-01 | decline | 45 |
| Monzo | real-03 | pursue | 72 |
| Predium | real-04 | decline | 45 |
| F H Bertling | real-05 | pursue | 72 |
| Nucs AI | real-06 | decline | 35 |
| Lemrock | real-07 | unknown | 45 |
| Cabify | real-08 | decline | 45 |
| HelloFresh | real-09 | decline | 25 |
| Stripe | real-10 | decline | 45 |
| Datadog | real-12 | decline | 15 |

At the unchanged threshold of 65, Monzo and F H Bertling were selected. Against the **AI proposals only**, both positives were above threshold, all seven negatives below it, and Lemrock remains unknown. Pairwise agreement was 14/14 (held-out 10/10), with no false-negative positive cases or checked cap/range/exclusion-high-score violations. This small convenience sample supports a preliminary diagnostic, not a claim of improved general ranking quality. Precision at k is 2/9 for k=9 (held-out 2/7): when k includes the entire assessed labeled sample, that number is prevalence and says little about ordering. Human-grounded metrics remain unavailable (0 reviewed labels).

The production scorer, prompts, CV and labels were unchanged during generation. The harness file received derived-metric changes during the running process, so its legacy report hash is an end-of-run on-disk hash and does not prove the exact harness source imported at launch. The original result is preserved; derived threshold metrics are computed separately from its retained scores. Subsequent harness runs snapshot code at startup and flag source changes. No extra model run was used to hide this limitation.

Historical backlog availability was not rechecked. The benchmark intentionally scores some jobs outside daily filters to expose ranking behavior; it does not select them for application. Predium and Lemrock now both produced usable scores (45), rather than being presented as matches after an evaluation error.

## Source evidence and coverage limits

[The dated HTTP ledger](evidence/source-http-2026-10-05.json) records 20 public read-only requests: 13 Allegro and 7 JustJoin, including redirect responses. Separate ranking collection used six public detail requests and the provider quota check used one read-only authenticated request. No applications, employer messages or remote scoring completions were sent.

The recorded Allegro page contains ten rows with string `id`, numeric invite routes and epoch-second `releaseDate`; the old parser rejects this observed shape. Cross-checks with September fixtures establish continuity of the legacy requisition IDs for matched records. The new parser preserves that identity namespace and resolves employer invite links. The listing advertises 156 records on 16 pages, so the former four-page setting cannot cover its current listing. The final default walks a bounded 16 pages and retains prioritized detail limits rather than select only three teams; the captured Finance analyst demonstrates why an exclusive data-team slice would lose coverage. The full sixteen-page production configuration is verified offline, not claimed as a fresh live full crawl.

JustJoin's API returned 503 again, as it did on several retained runs, while its public listing pages returned embedded offers. The underlying API failure remains unknown; a TLS-fingerprint explanation is not established. Fallback covers the first three pages of the public data and AI categories and always reports partial coverage. Description snippets, unknown country/remote evidence and collection failures remain visible.

The captured JustJoin slice contained 150 records, emitted 106 jobs and retained 100 after existing deduplication. It had **zero full descriptions**. Under the current title/location/freshness filters, three records survived: two Surveily listings in Wrocław and one Jit Team ML Engineer listing in Kraków. These are unscored snippet records, not validated matches or necessarily three distinct underlying requisitions. First-rejection counts were 65 title-not-included, 14 excluded-title and 18 freshness; these order-dependent counts do not audit all rejection reasons independently.

The small live Allegro check returned two fully described jobs, both outside 72 hours, hence zero filter survivors. JustJoin's corrected adapter was replayed against captured responses; the original twenty-request live source budget was already spent. Missing replay pages used synthetic 404 stand-ins and must not be interpreted as real failures of those pages.

Conflicting publication-labelled dates are preserved as conflicting evidence. The conservative earlier complete timestamp is used for freshness; a later date is not automatically an update, and neither source has established universal first-publication semantics. The absence of an explicit update remains unknown. This policy avoids making older postings look fresh through list promotion or date conflict, while preserving evidence for review.

## Review decisions

The independent Astra source review reproduced eleven failing assertions across six defects despite 225 original focused tests passing. The original Claude owner received fixes for deterministic seeded identity (including a failed first seed fetch), actual request/byte bounds, date provenance, partial dates, malformed neighboring rows, and same-day freshness boundaries. The review reproduced duplicate scoring-pending admission under changed Allegro identity using an in-memory tracker; it did not claim an automatic Allegro application occurred.

Claude independently checked the provider implementation against the baseline and reproduced its 583 focused tests. Accepted follow-ups covered whitespace-only retry hints, non-mapping response headers and recovery probes failing for a non-limit reason. Embedded HTTP-200 error codes now enter the existing bounded status-based retry/native-schema compatibility logic; this behavior is explicitly tested and documented. A recommendation to shorten arbitrarily large but valid server retry hints was declined: the client must honor server reset instructions. Scope remains finite to the chain/process, and local fallback is independent.

Astra independently reconstructed full-input prompt hashes and verified the retained ten-job benchmark. A later preflight failure could inherit preceding request telemetry; the original ranking owner fixed that association using per-case request deltas. Parent verification passed 37 checks including the independent reproduction. This defect did not occur in the completed all-valid benchmark.

No model or threshold was changed to obtain favorable scores. No label was revised after seeing the result. The earlier capacity failure of one Astra worker was observed explicitly; the same task and terminal were resumed, preserving its files and authority.

## Limits that remain external or require new evidence

- Provider account allowances and upstream availability cannot be increased by this patch. The dated 47-request balance is not a reservation or a guarantee for the next run; no paid-account change was made.
- If an Allegro seed fails and its matching listing invite cannot be followed within the detail/request cap, the adapter cannot prove their identity and emits the listing under its requisition key. This documented gap can create a separate scoring-pending identity; existing submission protections were not changed.
- Allegro's listing may grow beyond the configured window, and description enrichment remains capped. Undated or conflicting source evidence is not proof of a newly published job. Only the small documented live slice was checked.
- JustJoin API availability remains unresolved; public fallback covers data/AI pages only, not analytics or the entire board. Snippets do not establish language, degree, sponsorship or experience requirements absent from the evidence. The two Surveily records may represent the same underlying vacancy; current records do not prove otherwise.
- Human labels remain pending. [The review packet](../evals/real_job_fit/dataset/HUMAN_REVIEW.md) separates the AI proposals and uncertainties. The [result evidence](../evals/real_job_fit/results/README.md) is reusable without new model calls, but neither this sample nor its two positive labels measures all-company recall.
- Tests used the existing Python 3.9.6 environment, while project metadata declares Python >=3.10; supported-interpreter CI is still advisable. The existing urllib3/LibreSSL warning is unrelated to the changes.

The original database, scoring backlog, application-attempt ledger and uncertain-confirmation states were preserved. No automatic applications or user communications were triggered by this audit.

## Final validation

The coordinator ran the complete offline suite in the isolated tree:

```sh
/Users/luisgimenez/job_search/.venv/bin/python -m pytest -o addopts='' -m 'not network' -q --tb=short
```

**2,734 passed, 2 skipped, 69 network tests deselected, 1 expected failure; 63.67 seconds.** The only warning is the existing urllib3/LibreSSL warning. No Python source/test files changed during that full run. The coordinator separately reran the independent source reviewer’s eleven previously failing assertions: **11/11 passed**. The ranking owner’s regressions plus independent preflight-attribution reproduction passed **37/37**. Provider owner focused checks passed **613/613**; the complete suite includes them.

After the subsequent JustJoin coverage-label correction and its additional regression, the source worker reran the complete suite: **2,735 passed, 2 skipped, 69 deselected, 1 expected failure**, exit 0 in 63.05 seconds. The coordinator then independently reran all 90 JustJoin tests plus the 11 source-review controls: **101/101 passed**. Configuration validation also passed.

The external live checks were deliberately bounded; the offline suite is not represented as a full live source crawl or an application submission test. No production pipeline was launched to obtain these results. Existing duplicate protection, attempt ledgers and uncertain-confirmation code were preserved and covered by existing regressions.

Example of an accurate result summary for this audit:

> Local evaluation benchmark: 10 attempted, 10 completed, 0 pending/error evaluations, 2 above threshold. Human-validated matches: not measured (labels pending). Public-source replay: JustJoin recovered 106 snippet records, 100 after dedupe and 3 filter survivors; full descriptions 0, fit not evaluated. Allegro’s checked small live slice returned 2 fully described older jobs and 0 recent survivors. Coverage remains bounded. Applications and outbound communications: 0.

This is an audit example, not a newly generated production daily digest.
