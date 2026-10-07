# Singapore public career monitor

The monitor reuses `src.models.Job`, the existing Greenhouse/Lever parsers,
and `src.scoring.score_jobs`. It does not invoke tailoring, applications,
LinkedIn scraping, Gmail, browser automation, or authenticated job endpoints.
The EU pipeline remains a separate command.

```bash
python -m src.singapore.main --validate-only
python -m src.singapore.main --mode weekly --cv /path/to/real-cv.md --limit 5
python -m src.singapore.main --mode weekly --fetch-only
python -m src.singapore.main --mode auto --cv /path/to/real-cv.md
python -m pytest
```

`--config` selects the existing scorer's configuration and credentials; monitor-local
`llm` / `scoring` values in `sg_monitor.yaml` override its provider and model.
`--output-dir` sets an independent state and digest directory. `--limit` limits
jobs sent to scoring; jobs beyond the scorer's configured cap remain pending
for a later run. `--fetch-only` labels every unscored job explicitly. A missing
or example CV is never used to invent a fit score. Scoring failures remain
retryable, while discovery still distinguishes `new` from `seen`.

`config/sg_companies.yaml` lists verified endpoints and companies needing manual
review. `config/sg_sources.yaml` records every board/recruiter and its access
policy or manual alert. `config/sg_monitor.yaml` contains date-effective EP
salary thresholds. The salary check assumes the explicitly specified age 25;
change the values when the applicable age changes. The check is a review flag,
not a determination of EP eligibility. Salary without a known currency and
pay period is not silently converted.

The public HTTP transport checks robots on each origin, sends a descriptive
User-Agent, spaces every request to a host by at least one second, and backs
off bounded retries. Unknown policy approval, authentication, forbidden
robots, and blocked endpoints stop that source. An endpoint can be verified
but still require a manual check: reachability is separate from permission.
See [SINGAPORE_RESEARCH.md](SINGAPORE_RESEARCH.md) for dated evidence.

Outputs are `output/digest_singapore_YYYY-MM-DD.md`,
`output/singapore.sqlite3`, `output/singapore_runs.jsonl`, and
`output/sg_candidates.yaml`. The digest prefers company application links and
keeps all source provenance. Small/unknown companies are below established
companies. A Monday weekly digest includes discoveries from the preceding
seven days; other digests contain only new roles and are omitted on quiet days.
Every run logs checks, counts, exclusion reasons and errors, including quiet
days. Source or scoring errors cause exit code 2 with reviewable output.

MCF advertisements are radar observations, not proof of sponsorship or of
openness to foreign candidates. Established new firms are exported as
`candidate` entries for human review; the monitor never promotes them into the
verified company list. It never applies through MCF.

## GitHub scheduling

The monitor workflow runs at 07:00 `Europe/Warsaw` using GitHub's native IANA
timezone support. Its `auto` mode checks daily sources every day, M/W/F boards
on those weekdays, and companies/recruiters on Mondays. A single scheduled job
avoids duplicate Monday fetches. GitHub schedules can be delayed, and scheduled
workflows run only from the repository's default branch.

Configure repository secrets `SINGAPORE_CV_MARKDOWN` (your real CV) and
`OPENROUTER_API_KEY` for the configured OpenRouter scorer. If you switch the
monitor's `llm.provider` to Anthropic, set `ANTHROPIC_API_KEY` instead. The
monitor's `llm` and `scoring` overrides in `sg_monitor.yaml` apply only to this
command and use the existing LLM factory. The private CV is written
only to the ephemeral runner, never uploaded or cached. Digests and public run
logs become workflow artifacts. SQLite history is restored and saved under a
unique cache key per run; concurrency prevents simultaneous state writes.
GitHub cache eviction can remove old state, so retain a backup of SQLite if
permanent history matters. Changes on a feature branch need merging into the
default branch before the recurring schedule becomes active.

The separate `Tests` workflow runs offline pytest on every push and PR, without
job-site calls or scoring keys. All adapter tests use saved fixtures. Network
tests from the existing pipeline remain deselected by its pytest configuration.

## Adding a company

1. Find the official company careers page and follow its actual ATS link.
   Do not guess a tenant or slug. Check that it is established, hires in
   Singapore, and is outside government/statutory boards and defence.
2. Read the relevant robots files and terms. Fetch the permitted public
   endpoint with the monitor's transport, and save dated evidence of its
   actual JSON/HTML shape, company identity and Singapore hiring.
3. Add one company mapping with `name`, supported `ats`, `identifiers`,
   `verified_url`, `sector`, `fs`, `status`, `automation_allowed`, `robots_url`,
   `terms_url`, `evidence`, and `notes`. Workday identifiers are `tenant`, full
   `host`, and `site`; Greenhouse uses `token`, Lever `slug`, SmartRecruiters
   its company identifier, and Ashby its board name.
4. Enable only an actually verified endpoint with approved automation.
   Otherwise use `unverified` or `manual_check`; it appears in the digest's
   manual checklist and receives no automated requests.
5. Run `--validate-only` and the offline tests. For a new schema or ATS, add a
   representative saved fixture and test before a small live verification.

For a new board, document its official access route and terms first. Prohibited
or uncertain sources stay manual; do not add a scraper merely because their
frontend exposes JSON. Recruiter jobs require a named or demonstrably
established end client. Never automate LinkedIn.
