# Evaluation

An honest read of this design before you point it at your actual job search.
Written against the implementation in this repo, not against the pitch.

The short version: **the fetch → filter → score → tailor → digest path is
sound and worth running daily. The auto-apply stage is the one part whose
expected value is genuinely negative for most people**, and the design's own
default (`dry_run: true`) is the correct one. Below is the reasoning, plus
the failure modes that are structural rather than fixable with a patch.

---

## 1. What this is actually good at

| Stage | Verdict | Why |
|---|---|---|
| ATS pulls (Greenhouse / Lever) | **Strong** | Public, stable, cheap JSON. The highest-signal source per unit of effort by a wide margin. |
| Hard filters | **Strong** | Deterministic, free, testable. Does the bulk of the real work. |
| LLM fit-scoring | **Good, with calibration debt** | Genuinely saves reading 40 postings. Its absolute numbers mean nothing until you tune them (§4). |
| Tailoring | **Good** | The largest real time-saver. Also the largest accountability surface. |
| Digest | **Strong** | The actual product. Everything else is plumbing feeding this file. |
| SQLite tracker | **Strong** | Simple, durable, and the only reason the loop is safe to run daily. |
| Auto-apply | **Weak / negative EV for most users** | §3. |
| LinkedIn-via-Gmail | **Clever, but fragile** | §5. |

If you deleted the auto-apply stage entirely, you would keep roughly 90% of
the value at roughly 10% of the risk. That is not a criticism of the
implementation — it is what the numbers say.

---

## 2. Freshness is the weakest claim in the pitch

"Fresh EU postings (last 24h)" is not something any of these sources can
actually guarantee.

- **Greenhouse** exposes `updated_at` on every job and `first_published` on
  most. `updated_at` moves when *anything* changes — a typo fix on a
  three-month-old req makes it look brand new. The implementation prefers
  `first_published` and falls back to `updated_at`, which is the right call,
  but the fallback is a known source of false freshness.
- **Lever** gives `createdAt`, which is trustworthy.
- **Adzuna** gives `created`, which is the aggregator's ingest time, not the
  employer's publish time. It lags, and it re-lists.
- **LinkedIn alert emails** carry no per-posting date at all. Every job in
  one email inherits the email's `internalDate`. That is a ceiling on
  precision, not a bug — the pipeline is honest about it, but you should
  know that "posted 3h ago" on a LinkedIn item means "the alert arrived 3h
  ago".

`freshness.skip_undated: true` is the honest default and it will silently
discard real jobs. Expect the funnel line in the digest to show a large
`undated` rejection count on some boards. **Check that number in week one**;
if one company you care about always lands there, set `skip_undated: false`
and accept the staleness for that board rather than never seeing it.

## 3. Auto-apply: read this twice

The safety design is the good part. `inspect_form` bails on any `<select>`,
any `<textarea>` that is not an obviously optional cover letter, any radio
group, any unrecognised required field, and any label that reads like a
question. The tests pin this behaviour hard, including "bail" as the default
for anything ambiguous. Combined with `require_pdf` and the score floor, the
set of jobs that actually reach a real submission is small.

That is exactly the problem: **the forms simple enough for a bot to submit
are disproportionately the forms nobody reads.** Any employer who cares
enough to ask "why us?" has a textarea, and the bot correctly refuses. So
auto-apply's yield concentrates on the postings where a generic submission
was always going to be ignored.

Meanwhile the downside is asymmetric and non-recoverable:

- A submitted application cannot be unsent. There is no undo.
- If the tailored CV contains an error, it went out under your name. You are
  accountable for it, not the model — this is stated in the README and it is
  not boilerplate.
- Some ATS deployments and job boards have terms that discourage or prohibit
  automated submission. Enforcement is rare; a permanent blocklist entry at
  a company you wanted to work for is not worth the twenty saved seconds.
- Duplicate applications are the classic failure, and the tracker only knows
  about applications *it* made. Anything you sent by hand, on your phone, or
  through a recruiter is invisible to it.

**Recommendation:** run with `dry_run: true` indefinitely. Treat the
screenshots as a "this form is trivial, go click it" signal rather than as a
rehearsal for turning the switch off. If you do flip it, set
`apply.min_score: 85+` and `apply.max_per_run: 2`, and read every screenshot
for a fortnight first. The one strong argument for flipping it is volume: if
you are sending 20+ applications a week and the marginal application has
genuinely low value to you, the arithmetic changes.

## 4. Scoring is a judgment, and it is uncalibrated on day one

The score is a model's opinion, conditioned on a prompt that tells it to be
strict. It is *consistent* — the same job and CV produce nearly the same
number at `temperature: 0` — but consistency is not calibration. On a fresh
setup the threshold of 65 is a guess.

What actually goes wrong:

- **The CV is the bottleneck, not the model.** A vague CV produces mushy
  scores for everything. The single highest-leverage thing you can do is put
  metrics in `cv/base_cv.md`.
- **Seniority mismatch is the most common scoring error**, in both
  directions: "Senior" in the title with junior responsibilities in the
  body, and the reverse.
- **Hard requirements get under-weighted.** The prompt tells the model to
  penalise a missing work authorisation or a required language hard, but it
  will still sometimes score a German-required role at 78 because the
  technical fit is excellent. If you do not speak the language, that is a 0.
- **Snippet-only sources score worse.** Adzuna descriptions are truncated;
  the pipeline flags this in `raw["snippet_only"]`, but the model is working
  from less text and its confidence is unwarranted.

**Do this in week one:** open the "Below threshold" section every day and
look for jobs you would have applied to. If you find them, the threshold is
too high or the CV is too thin. If "Needs your click" is full of jobs you
immediately dismiss, raise the threshold to 75. This is a ten-minute-a-day
habit for five days, and it is the difference between a useful tool and an
expensive RSS feed.

## 5. Source-specific fragility

- **ATS APIs are unofficial-but-public.** They are stable in practice —
  these endpoints have looked the same for years — but nothing obliges them
  to stay that way. Failures are logged and skipped, never fatal, so a
  format change degrades into "that company returned nothing today". Which
  is quiet. **Watch the per-source counts in the digest**, not just the total.
- **LinkedIn alert-email parsing is the most brittle code in the repo**, by
  construction: it parses marketing HTML that LinkedIn redesigns on its own
  schedule with no notice and no versioning. The parser is deliberately
  lenient (a job with a title and a URL but no company is still emitted), but
  a big enough redesign yields zero jobs, silently. The compensating control
  is the same one: watch the source count.
- **The LinkedIn guest description endpoint breaks and rate-limits
  periodically.** The pipeline degrades to email-only info, which means the
  scorer sees a title and a company and little else. Those scores are worth
  less; treat a high score on a description-less LinkedIn item with
  suspicion.
- **Reading Gmail is a real permission.** The scope is `gmail.readonly` —
  narrow in kind, broad in reach, since it can read *everything*, not just
  LinkedIn mail. The token sits in `gmail_token.json` in the project root, in
  plaintext, protected by nothing but filesystem permissions. `.gitignore`
  covers it; back-ups and sync clients do not care about `.gitignore`.

## 6. Cost

Roughly, at the shipped defaults (40 scored, 10 tailored per day, Sonnet):

- Scoring: ~40 calls × (CV + posting ≈ 3–5k input, ~300 output)
- Tailoring: ~10 × 2 calls × (CV + posting ≈ 4–6k input, ~1.5k output)

That lands in the **low tens of cents per weekday** — call it a few euros a
month, dominated by tailoring. Two controls matter: `scoring.max_jobs` is a
hard ceiling on the expensive stage and should stay set, and switching
`scoring.model` to `claude-haiku-4-5-20251001` cuts the scoring bill by
roughly an order of magnitude for a modest loss of judgment. Tailoring is
worth keeping on Sonnet — it is the output you actually send.

There is no runaway failure mode here: every LLM stage is bounded by an
explicit per-run cap, and the tracker prevents re-scoring yesterday's jobs.

## 7. Operational reality

- **Cron on a laptop is not a scheduler.** If the lid is shut at 08:00, the
  run does not happen and nothing tells you. `launchd` on macOS or a systemd
  timer on Linux at least catches up on wake. Better still: notice when the
  digest is missing.
- **Silent success is indistinguishable from silent failure.** A run that
  fetches 0 jobs because every board 404'd produces a digest that looks a lot
  like a quiet Tuesday. The digest's funnel section exists specifically to
  make this visible — read the numbers, not just the cards.
- **The tracker is a single SQLite file** at `output/tracker.sqlite3`, and
  `.gitignore` excludes `output/`. It is the only record that you applied to
  anything. Back it up. Losing it means the double-apply guarantee resets to
  zero, and the next run will happily re-apply to everything.
- **Concurrency:** the tracker is not thread-safe and the pipeline does not
  share it across threads (only the scoring stage is parallel, and it touches
  no DB). Do not run two instances at once.

## 8. Data protection, briefly

You are storing third-party job postings and your own CV locally, and sending
both to an LLM API. For personal use this is unremarkable. Two things worth
stating: the tailored CVs under `output/applications/` contain your full
personal data in plaintext and accumulate forever (there is no retention
policy — prune the directory yourself), and if you ever share this repo, the
`.gitignore` is what stands between you and committing your CV, your Gmail
token, and your application history.

## 9. What I would change first

In rough order of value:

1. **Notify on failure.** A run that produces no digest, or a source that
   returns zero when it returned 40 yesterday, should reach you. Today it
   does not. This is the single biggest gap.
2. **Persist the scores.** The tracker records a score per application but
   not the reasons; a month of `score_reasons` compared against the jobs you
   actually got interviews for is real calibration data, and it is being
   thrown away.
3. **Retention policy for `output/applications/`.** It grows without bound.
4. **A `--since` / catch-up flag.** Missing Monday's run currently means
   Monday's jobs are gone, because Tuesday's run only looks back 24h.
5. **Company-level dedupe.** Applying to three roles at the same company in
   one week is worse than applying to one. The tracker sees jobs, not
   employers.

## 10. Where the tests are pointed

The suite is written adversarially against the three claims that, if false,
make the tool actively harmful rather than merely unhelpful:

1. **"It never applies twice."** — `tests/test_db.py` pins that only
   `applied` blocks, that an `applied` row can never be downgraded by a later
   run, and that a dry run deliberately does *not* block a future real
   application.
2. **"It will never answer a question for you."** — `tests/test_autoapply.py`
   throws every screener shape at `inspect_form` (selects, textareas, radio
   groups, sponsorship questions, salary expectations, EEO fields) and
   asserts a bail, plus asserts that the dry-run path never clicks submit.
3. **"It can't invent things about you."** — `tests/test_tailor.py` pins the
   anti-fabrication clauses in the prompts and the post-hoc
   `validate_tailored_cv` guard, since prompt-level enforcement alone is not
   a guarantee.

Everything else — sources, filters, scoring, digest — is tested for
correctness rather than safety. The full breakdown is in `docs/TESTING.md`.
