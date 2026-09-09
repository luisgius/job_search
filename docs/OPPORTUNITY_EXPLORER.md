# Opportunity explorer

`python -m src.explore` creates a separate, unassessed view of postings returned by configured public sources. It does not assess candidate fit or verify that a posting is still available. A recent source date is not a recommendation or proof that an employer is hiring.

```sh
.venv/bin/python -m src.explore --source greenhouse --country PL --days 30 --limit 100
.venv/bin/python -m src.explore --source greenhouse --source nofluffjobs --days 60 --undated-limit 10
.venv/bin/python -m src.explore --config config.yaml --watchlist watchlist.yaml --output-dir exploration_output
```

Only enable sources and configure employer boards/queries through the existing `sources` settings in `config.yaml` and source settings in `watchlist.yaml`. Repeated `--source` arguments narrow enabled sources; they never enable disabled ones. With no `--source`, all configured public sources are selected. `linkedin_email` is always excluded, including when enabled in normal configuration; explicitly requesting it is an error. This module does not read or send mail.

`--country PL` changes only the copied run's country filter, making a Poland view possible without editing a normal Europe configuration. Repeat `--country` for multiple countries; supported values are the European ISO2 codes recognized by the existing geography module. Omitting it preserves configured countries. Existing location semantics remain: a multi-country posting can pass if any listed country is allowed, remote roles can pass through European hints, and sponsorship exceptions are restricted to the explicitly selected countries. Explicit selection replaces the normal country allowlist; selecting `GB` alone leaves that allowlist nonempty and cannot disable the location gate. Sponsorship exceptions outside the selection cannot admit other countries. This selector does not independently establish permission to work remotely from Poland, and unknown locations still follow the normal location filter.

## Bounds and interpretation

- The dated view defaults to the last 30 days. `--days` accepts integers from 1 through 90. The cutoff is inclusive; future dates are excluded. Dates and stable posting keys are preserved rather than replaced with fetch time.
- `--limit` defaults to 100 and accepts 1 through 500. It independently caps dated opportunities and rejected examples. Undated postings are excluded by default; `--undated-limit 1..50` explicitly enables a separate capped review list. Undated records keep a null date and never enter the dated list.
- Existing deduplication chooses the richest representative, and existing title, employment, location, language, keyword and description filters still apply. Only the exploration copy of freshness settings changes. Neither the caller's normal configuration nor its input `Job` objects are mutated.
- Ordering is deterministic for the same source results and evaluation time: newest source date first, then company/title, stable key, source, URL and evidence tie breakers. Undated records follow the same remaining tie breakers. The view is not ranked by fit.
- The lookback is a post-fetch eligibility window, not a historical archive query. Source adapters retain their existing watchlist, page, query, request and enrichment limits, including any narrower source-side age limit. A source can return only a subset of the window. This report makes no claim about market completeness.
- Counts distinguish fetched, deduplicated, eligible and displayed records. `first_rejection_counts` records one first failing hard-filter category per rejected posting; for a survivor with a future source date, the explorer adds `future_date`. Rejected examples are capped but their counts cover the whole deduplicated batch. Source errors and per-source fetched counts remain visible in labeled HTML tables/lists. An empty enabled selection has an explicit diagnostic in HTML, JSON and CLI output, separate from valid empty source results and collection errors. Adapter-level parser/title drops before returning `Job` are outside these counts.

## Evidence and artifacts

Each displayed record is labeled `unassessed` and `availability: unknown`. The JSON preserves the selected posting's original key, date, complete available description and source metadata; HTML shows a short preview with expandable description and metadata. Source snippet flags remain evidence: `snippet_only` produces `evidence_kind: snippet`, an empty description produces `missing`, and other text is conservatively labeled `description_completeness_unknown`. Absence of a snippet flag does not prove that a complete employer description was fetched. Source dates can describe publication or updates; the explorer does not independently verify their semantics.

Each run creates a fresh `explore_*` subdirectory under `exploration_output` (relative to the configuration root), containing `exploration.html` and `exploration.json`. An explicit output directory must be separate from the production output tree, including resolved symlink aliases. Existing artifacts are never overwritten: run directories are unique and output files use exclusive creation. HTML escapes posting content and links only HTTP(S) URLs.

The independent module imports neither the normal pipeline nor the tracker, scoring, LLM, tailoring, application, mail or digest modules. It does not open a tracker connection, inspect production history, read a CV, generate application documents, submit applications, send mail, write a digest, or open a browser. Public-source network requests and the two exploration artifacts are its intended external effects. A source failure produces a report containing errors and CLI exit status 1; invalid arguments/configuration/output paths return status 2. Successful collection, including a valid empty selection, returns 0.

## Offline use and checks

`explore(config, days=30, limit=100, undated_limit=0, sources=None, countries=None, now=None, fetchers=None)` returns the report in memory. An injected `fetchers` mapping uses existing `fetch(config, errors=...)` signatures, keyed by adapter module name (`ats_boards` for all ATS vendors; otherwise the public source name). An incomplete injected mapping reports the missing adapter and never falls back to live network access. The copied configuration narrows enabled vendors before the shared ATS fetch runs. `write_report(report, config, output_dir=...)` writes only exploration artifacts.

```sh
.venv/bin/python -m pytest tests/test_explore.py -q
.venv/bin/python -m src.explore --help
```

The offline tests cover date boundaries, undated separation, country override isolation, source narrowing (including registered Allegro), empty-selection diagnostics, first rejections, dedupe, deterministic caps, evidence, HTML escaping, source errors, isolated output and CLI behavior. Failing sentinels guard forbidden imports, SQLite connections, production file access and CV access while the CLI creates real temporary exploration artifacts. No live source collection is needed for these checks. Source adapter changes and normal CLI integration remain outside this module.
