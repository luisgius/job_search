---
name: application-state-audit
description: Audit Job Hunter application eligibility, dry-run behavior, duplicate protection, and submission persistence using offline scenarios; use for state changes or suspected unsafe retries.
---

# Application state audit

Begin with `graft ask "eligible apply_one record_submit_attempt" --source`, then `graft callers` for the changed symbol. Open exact spans when truncated. Default to synthetic jobs, a temporary or in-memory `Tracker`, fake pages, and a fixed clock. An audit does not authorize live form filling, submission, or production-tracker access.

Check the relevant transitions across both outcome rows and submit-attempt records:

- `run` calls `eligible`; `apply_one` assumes eligibility was already checked. Calling `apply_one` directly does not test the complete application gate.
- Eligibility requires enabled application, a supported Greenhouse/Lever URL, an actual non-error score meeting the threshold, appropriate cover flags/PDF state, and successful duplicate/history checks when a tracker is supplied. A failed tracker read must not authorize another submission. Live `apply_one` additionally refuses a missing CV PDF even when the configurable PDF gate was disabled.
- `inspect_form` rejects unsupported questions/controls; recognized required fields must have values before live submission. Use its current classifications, not guessed answers for sponsorship, salary, or custom questions.
- Dry run fills and screenshots but never clicks submit. It may expose incomplete fields for inspection; this is not a successful application.
- Persist the write-ahead attempt before the submit click. A click exception can leave `APPLY_FAILED` plus an unresolved attempt, which still blocks a retry. Clear the attempt only on positive evidence that nothing was sent; timeout or unreadable content is insufficient.
- Confirmation must be new relative to the pre-click page. Text-only confirmation also requires the form to be gone. A vanished/unreadable page without confirmation becomes `SUBMITTED_UNCONFIRMED`, not confirmed success or permission to retry.
- `APPLIED` and `SUBMITTED_UNCONFIRMED` protect against resubmission and cannot be downgraded to nonterminal states. Check exact job identity and the same role under a new requisition ID. Reminder-window visibility is separate from application eligibility.
- Preserve the last-resort `APPLIED_BUT_UNRECORDED.txt` recovery evidence when a sent outcome cannot be persisted. Scoring retries and recruiting outcomes must not erase submission history.

Select offline scenarios proportionate to the change: dry-run click count, unknown/required fields, duplicate/repost gate, tracker-read/write failure, crash after click, stale confirmation, readable rejected form, or unconfirmed submission followed by rerun. Verify resulting durable state and whether another click is allowed, not only returned status text.

Report scenario → evidence → stored state → retry consequence, with exact test commands and source spans. An audit finding is not permission to clear an attempt or repair production history. If reconciliation is requested, use evidence about the actual submission and existing authorization; never test uncertainty by submitting again.

Evidence: [application flow](../../../src/apply/autoapply.py), `eligible:340–438`, `inspect_form:665–747`, `apply_one:1122–1310`; [tracker](../../../src/db.py), `record_status:435–483`, identity/attempt/reminder gates `497–598`; [status schema](../../../src/models.py), `ApplyStatus:329–348`. Re-resolve spans through Graft after code moves.
