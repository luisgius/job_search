# Europe plus worldwide remote

Luis added worldwide remote opportunities to the current search on 5 October 2026.
The existing European country list remains in place for office-based and regional
roles. Worldwide remote is an additional route through the location filter, not
an expansion to office-based jobs in every country.

The shipped configuration enables `filters.allow_remote_worldwide: true` alongside
`filters.allow_remote: true` and `filters.remote_requires_eu_hint: true`. The
library default remains false for other configurations. Explicit worldwide remote
work evidence can satisfy the location gate without a European hint. A bare
“Remote” label does not establish worldwide eligibility. The added worldwide route checks stated country limits, residency requirements
and onsite/hybrid wording before admitting a posting; generic company marketing
does not establish worldwide availability. This bounded check is not exhaustive
and does not repair every limitation of the existing Europe route (see below).

The scoring candidate context records this preference and asks the evaluator to
report hiring-country, residence and time-zone requirements. Unknown employment
eligibility remains unknown: “worldwide” does not prove work authorization,
contractor eligibility, payroll support or schedule compatibility. Location
admission still requires the remaining title, experience, language, freshness
and evaluation checks; it is not a validated match or permission to apply.

## Evidence and coverage

The prior configuration reproducibly rejected `Remote - Worldwide` with
“remote posting with no European hint”. Existing ATS parsing can retain that
location. Tests use synthetic postings through the production Lever parser,
shipped configuration and actual filters; no live vacancy availability or
market-wide coverage is inferred from them.

This change does not add a global job-board crawler or enumerate all employers.
Coverage is limited to configured sources, employer watchlists and their request
caps. Europe-oriented feeds remain Europe-oriented. Worldwide posts that never
reach these sources cannot be recovered by a filter change. No source request
budget, model, score threshold, application setting or duplicate protection was
changed.

## Deliberate limitations

The recognizer is conservative and primarily English. A concrete office/country
label is not reinterpreted as a headquarters address on the strength of worldwide
prose. Mixed worldwide/US location lists still hit the existing US veto; these
ambiguous records can be false negatives. Existing Europe-hint behavior is
unchanged, including its known inability to interpret every exclusion (for example
“Worldwide, excluding Europe”). Later requirement assessment remains necessary.

The tracker is not reset: already handled records retain the existing 30-day
suppression. The reviewer found no recent worldwide record requiring a targeted
requeue; historical worldwide-labelled records were predominantly rejected on
title. No title expansion or database migration accompanies this preference.

## Validation

Implementation and independent review are coordinated through Orca run
`run_ffd4a0246359` using Codex Astra and Claude. Changes are developed in an
isolated copy before integration. The first review reproduced eight in-scope failures (missing labels, overly broad
description vetoes and a US-based-candidate false positive), plus a separate
mixed Global/US list acceptance expectation. The eight defects were corrected;
the mixed-list expectation is deliberately not adopted because the existing US
veto remains in force. The coordinator reran the unchanged independent controls:
45 passed, two expected failures for the pre-existing excluded-Europe behavior,
and one failure for that documented mixed-list expectation. These limitations
are reported rather than hidden by editing the reviewer tests.

The original implementation and the Claude audit completed. Reusing the earlier
Astra terminal later failed before prompt delivery after the Orca runtime changed;
the same correction task was retried with a fresh Astra worker. No concurrent
implementation attempt was started.

The correction task completed successfully with 422 focused checks passing and
two explicit expected failures for the inherited Europe-exclusion behavior.
All supervised worker terminals were released; the failed pre-delivery attempt
had no owned resource. No source fetch, scoring request or application was made
for this change. The seven shipped-configuration/Lever integration checks and
configuration validation passed.

The first complete run found the newly intentional config/default divergence
missing from the configuration contract test, plus executable bits omitted by
the scratch-copy operation. The config test now records the reason for this
user-specific opt-in; the dedicated integration check pins its exact true/false
values. Script permissions were restored from the original files in scratch
only; production scripts and their permissions were unchanged.

Final complete offline suite (7 October 2026): **2,809 passed, 2 skipped,
69 network tests deselected, 3 expected failures**, exit 0 in 63.29 seconds.
The only warning is the existing urllib3/LibreSSL environment warning.
No live ranking-quality claim is made from these deterministic regressions.
