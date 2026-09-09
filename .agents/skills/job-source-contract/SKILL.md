---
name: job-source-contract
description: Maintain or review Job Hunter source adapters, payload normalization, and description enrichment with focused fixtures and downstream contract checks.
---

# Job source contract

Use Graft before source reads: `graft map` for orientation, then `graft ask "<adapter or parser symbol>" --source`. Use `graft callers` for affected consumers and open exact returned spans when truncated. Scope work to the requested source and failure; this skill does not initiate employer discovery or a board-wide scan.

Reproduce with a small public or synthetic payload and an injected session. Preserve the adapter's actual missing-field policy: for example, Arbeitnow rejects missing company names while Adzuna accepts anonymous listings. Do not manufacture employer facts to make a fixture pass.

Preserve these contracts:

- Emit `Job`, not a vendor payload. `posted_at` becomes UTC; unknown dates stay unknown. Do not replace an absent publication date with fetch time. Distinguish an alert receipt timestamp from employer publication evidence.
- Keep `remote=None` when unstated; assert `False` only from explicit evidence. Remote does not imply eligibility everywhere. Respect configured country restrictions and explicit location evidence through the existing geo/filter functions.
- Preserve stable ATS identity. `Job.key` prefers vendor plus ATS ID; display-name overrides must not re-key the posting. `dedupe_key` represents normalized company/title/city, with country fallback. Dedupe first groups canonical URLs, then fuzzy identity, selecting by date presence, description length, source rank, and recency.
- Retain snippet provenance such as Adzuna's `raw["snippet_only"]`; an empty description or failed enrichment is not a full description. Report full/snippet/missing coverage when enrichment changes, without claiming a universal provenance field exists.
- Distinguish a valid empty feed from a failed request or malformed envelope. Keep errors attributable to the affected source/board and preserve bounded requests, pagination, and existing injected test seams.

Verify the changed parser/fetcher on relevant malformed, missing-date/location, duplicate, and request-failure fixtures. Exercise the affected dedupe/filter path to show which jobs survive and why; parser success alone does not establish useful source coverage. Report the exact fixture, changed funnel counts/reasons, focused test command, and any unverified live behavior. Live checks stay within the user's authorized source and request budget.

Evidence: [Job identity](../../../src/models.py), `Job:173–258`; [dedupe and filters](../../../src/filters.py), `_check_location:334–440`, `is_fresh:562–596`, `dedupe:735–807`; [Adzuna](../../../src/sources/adzuna.py), `parse_result:221–292`. Re-resolve spans through Graft after code moves.
