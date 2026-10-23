# Ideas adopted from ai-job-search — 22 October 2026

Source: [ai-job-search](https://github.com/ai-job-search-dev/ai-job-search) at
`e16acee` (21 Oct 2026), MIT License, © 2026 Mads Lorentzen. It is an
interactive Claude Code framework (`/scrape`, `/rank`, `/apply`, `/outcome`);
Job Hunter is an unattended daily pipeline, so only ideas with a deterministic,
offline-testable core were adopted. Files that copy its regex lists, test
strings or phrase lists say so in a header comment.

Seven read-only comparisons (posting integrity, fit rubric, identity/dedupe,
tailoring/ATS, application lifecycle, sources, CI/hygiene) proposed changes;
nine implementers built them in separate worktrees; three independent reviews
of the merged result found 21 defects, all fixed with regression tests before
this landed.

## What changed, and the switch for each

| Area | Change | Default |
|---|---|---|
| Posting integrity | `src/posting_guard.py`: invisible/bidi/tag characters removed from everything that reaches a prompt or the digest; forged prompt fences defanged; single-line fields stay single-line; instruction-shaped text ("ignore previous instructions", "if you are an AI…", planted `"score": 100`) reported as an amber advisory on the card. "As an AI Engineer…" and similar job-ad phrasing do not flag. | on (safety fix) |
| | Live auto-apply refuses a flagged posting. | `apply.block_on_posting_guard: true` |
| Scoring | Replies with `72.0`, `"72"`, `"82/100"` or `64.5` were scoring errors because schema validation ran before `parse_score`; they are now read, and 64.5 rounds half-up to 65. Other scales (`"8/10"`, `0.85`, `"850"`) remain errors so the fallback model answers. | on (bug fix) |
| | Configured `score_caps` enforced in code from the model's declared `caps_fired`; can only lower a score. | `scoring.enforce_caps: false` |
| Dedupe | A copy from another board of a job already handled (digest, applied, …) inside `db.skip_seen_days` is not scored again. A below-threshold verdict on a snippet does not hide a richer copy. | on (bug fix) |
| | The same posting listed per city (No Fluff Jobs rotations, N26 Barcelona/Berlin) is scored once, preferring the ATS's own record, then Kraków, then remote; a copy already applied to keeps the whole group closed. | `filters.collapse_city_variants: true` |
| | A queued job keeps its full description when today's sighting is only a teaser. | on (bug fix) |
| Tracking | `python -m src.track mark/add/list/report/export` records outcomes (applied, followed_up, interview, offer, hired, rejected, no_response, offer_declined, withdrawn, dismissed) and relevance labels (pursue/stretch/dismiss) in a new `application_events` table (schema v5; v6 adds `applications.full_text`). A hand-made application now blocks auto-apply, including the board's own copy when the URL is a Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Personio or Teamtailor one, and any job at an already-applied URL. See [RUNNING.md](RUNNING.md#recording-outcomes). | on; nothing recorded until you run it |
| | Digest: "Follow-ups due" (quiet ≥ 10 days, ≤ 2 nudges, stale at 60) and "Applications to date" funnel, each shown only when non-empty; needs-click cards show the `mark … applied` command. CSV export guards against spreadsheet formulas. | `tracking.followup_days: 10`, `stale_days: 60`, `max_followups: 2` |
| Tailoring | Non-breaking hyphens/narrow spaces (black boxes in 7 of the 8 PDFs from 21 Oct) mapped to plain characters; cover-letter greeting/paragraphs/signature separated. | on (bug fix) |
| | CV header pinned to the base CV's (3 of 8 CVs had lost the name heading). | `tailoring.pin_header: true` |
| | Invented employer/degree/title headings, new certifications/languages, and links or emails not in the CV or contact details reject the draft (repair, then base-CV fallback). 0 false rejections on the 8 real CVs. | `tailoring.entity_check: true`, `cv.verbatim_sections` |
| | Cover-letter style lint (missing greeting, banned clichés, em dashes). | `tailoring.style.mode: off` (`flag` / `repair`) |
| PDF | Example renderer keeps headings, including a section heading followed by an entry heading, with their content (3 of 8 CVs stranded a heading at a page foot; 0 after). Your git-ignored `src/render_pdf.py` was identical to the example and has been updated to match. | — |
| | Text-layer check of rendered PDFs (■ glyphs, missing name/email, page count, stranded headings). | `pdf.verify: off` (`warn` / `block`) |
| Sources | freehire.me feed: Poland and EU/global-remote data/AI roles, incl. Workday/SuccessFactors/Oracle employers no watchlist board reaches. Freshness from `created_at` (its `posted_at` is re-dated). Never auto-applied. See [FREEHIRE_SOURCE.md](FREEHIRE_SOURCE.md). | `sources.freehire: false` |
| | `http_get` honours Retry-After within a 30 s budget and fills `HttpError.status_code`. Shared JSON-LD `JobPosting` extractor; Allegro detail pages with several JSON-LD blocks or a `@type` list no longer fail. | on |
| Repo | `.github/workflows/tests.yml` (Python 3.10 + 3.12, SHA-pinned actions, read-only token) and Dependabot; `.gitignore` covers `.env.*`, stray SQLite files, root-level PDFs, `.claude/projects/`; tests pin the workflow hardening, the ignore rules and the agent-skill files. | — |

## Considered and not adopted

- LinkedIn guest-search scraping: against LinkedIn's terms and robots.txt; alert emails remain the route.
- EXT's weighted five-dimension score: conflicts with the "different job family is a ceiling" rule and has no labels to set weights.
- LLM reviewer on every tailored document, drafted follow-up emails, interview prep packs, company research, Notion/Gmail sync, salary lookup: interactive or paid per job; the deterministic parts (outcome log, follow-up list) were taken instead.
- Hidden-HTML stripping in `html_to_text`: hidden instructions are already caught by the advisory scan; stripping could drop real accordion content. Revisit with a week of advisory data.
- Re-keying company names in `dedupe_key` ("Grafanalabs" vs "Grafana Labs"): needs an audited migration of existing rows.

## Decisions for you

1. Enable freehire (`sources.freehire: true`) once you're happy with [its limits](FREEHIRE_SOURCE.md); it adds jobs to the same `scoring.max_jobs` budget.
2. Record outcomes and a few pursue/stretch/dismiss labels with `python -m src.track`; the ranking evaluation in [JOB_FIT_EVALUATION.md](JOB_FIT_EVALUATION.md) needs them.
3. The CI workflow runs only once this branch is pushed — GitHub's default branch is an older, divergent history.

Not verified here: live runs of any source, a real model's behaviour with `enforce_caps`, and the workflow on GitHub's runners (the suite passed locally under 3.12 and, with borrowed packages, 3.10).

## Second pass — 23 October 2026

Upstream unchanged (`e16acee`). Two reasons for not adopting changed: Luis
accepted LinkedIn's Terms-of-Service risk for personal use, and a local
Ollama model makes per-job LLM work free. Added:

| Area | Change | Default |
|---|---|---|
| Sources | `src/sources/linkedin_search.py`: LinkedIn's anonymous `jobs-guest` search, parsing ported from their `linkedin-search` CLI. Detail pages only for cards that pass the title and location filters; the first 403/429/999 stops LinkedIn requests for the run; a promising card left undescribed is held back rather than scored on its title. Live test: 127 cards, 41 past the filters. | `sources.linkedin_search: false` (on in this config) |
| Tracking | Tracker schema v7 stores the scorer's `gaps`. `python -m src.track gaps` counts them against the CV, near misses first (their `/upskill` heatmap, without the model call or web search). | — |
| Interview | `python -m src.track prep HANDLE` writes `interview_prep.md` from the posting and the CV and cover letter that were sent (their `/interview` and interview-prep guide). Local model only. | — |

Same day, at Luis's request, the rest of what was left:

| Area | Change | Default |
|---|---|---|
| Dashboard | `python -m src.track dashboard`: one offline HTML page (their `/html-report`) with the funnel, follow-ups, score distribution, per-source yield and skill gaps, built from the tracker. | — |
| Profile | `python -m src.expand` (their `/expand`): reads `documents/` (git-ignored) and recent public GitHub repos; the local model proposes skills, tools, projects and achievements with a verbatim evidence quote; quotes not found in the named source, and items the CV already shows, are dropped. Never edits the CV. | — |
| Review | Company research (Wikipedia/Wikidata and the company's own site, no search-engine scraping) and a hiring-manager review of each tailored CV and letter (their `application-reviewer`), local model only, advisory `review.md`. | `review.enabled: false` |
| Language | `filters.work_languages`: a posting that demands any other language (English or native phrasing: "maîtrise du français", "biegła znajomość języka polskiego"), or is written only in one, is dropped before scoring; a score cap backs it up. | `[]` (here `[en, es]`) |
| Location | Singapore and Taiwan resolve as countries (`geo.TARGETS_ABROAD`); LinkedIn and freehire search them. | in this config |

Still not adopted: the four Danish portals (Danish-language market, now
filtered out by `work_languages` anyway) and salary lookup (Danish union data).
