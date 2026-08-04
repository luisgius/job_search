"""Tests for src/apply/autoapply.py — the only stage that can act on the
user's behalf.

This suite exists for safety, not correctness. The claim being defended is:

    **"Any screener question, dropdown, or textarea → the bot bails and the
    job goes to your digest instead. It will never answer a substantive
    question for you."**

The bias is one-directional and every test below is written from that side: a
wrong bail costs the user one click, a wrong submit costs them their standing
with an employer. So ambiguity must resolve to bail, and anything the module
does not positively recognise is a bail.

Three properties get the most attention:

  1. `inspect_form` refuses every screener shape (§ the bail matrix);
  2. the dry-run path never clicks submit, under any circumstances;
  3. `eligible` refuses anything outside Greenhouse/Lever, under the score
     floor, without its tailored PDF, or already applied to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.apply.autoapply import (
    MAX_FORM_FIELDS,
    SUPPORTED_ATS,
    ApplyOutcome,
    apply_one,
    classify_field,
    collect_fields,
    detect_ats,
    eligible,
    inspect_form,
    question_trigger,
    run,
)
from src.models import ApplyStatus
from tests.conftest import (
    NOW,
    FakeBrowser,
    FakeElement,
    FakePage,
    form_with,
    make_scored,
    simple_form,
    write_config,
)


def apply_config(tmp_path: Path, **apply_overrides):
    settings = {"enabled": True, "dry_run": True, "min_score": 80,
                "require_pdf": True, "max_per_run": 5, "headless": True}
    settings.update(apply_overrides)
    return write_config(tmp_path, {"apply": settings,
                                   "output": {"dir": str(tmp_path / "output")}})


def with_pdf(tmp_path: Path, scored=None, *, score: int = 90, **kwargs):
    """A scored job whose tailored CV PDF actually exists on disk."""
    scored = scored if scored is not None else make_scored(score=score, **kwargs)
    directory = tmp_path / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    scored.artifacts.dir = str(directory)
    scored.artifacts.cv_pdf = str(pdf)
    return scored


# ==========================================================================
# detect_ats
# ==========================================================================


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/4012345", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/4012345", "greenhouse"),
        ("https://boards.greenhouse.io/embed/job_app?token=1", "greenhouse"),
        ("https://acme.greenhouse.io/jobs/4012345", "greenhouse"),
        ("https://jobs.lever.co/globex/9f2b1c4e-1111", "lever"),
        ("https://acme.jobs.lever.co/globex/9f2b1c4e", "lever"),
        ("boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ],
)
def test_detect_ats_recognises_the_supported_forms(url, expected):
    assert detect_ats(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/123",
        "https://acme.workday.com/en-US/careers/job/1",
        "https://apply.workable.com/acme/j/ABC/",
        "https://jobs.ashbyhq.com/acme/uuid",
        "https://careers.acme.com/apply/1",
        "https://www.adzuna.de/land/ad/5012345678",
        "", None, "not a url", "ftp://boards.greenhouse.io/acme",
    ],
)
def test_detect_ats_refuses_everything_else(url):
    assert detect_ats(url) is None


def test_a_hostname_lookalike_cannot_smuggle_the_bot_somewhere_else():
    """A substring test would drive the bot into a stranger's site. Matching
    is on the hostname, never on the URL as text."""
    assert detect_ats("https://evil.example.com/?next=https://jobs.lever.co/a/1") is None
    assert detect_ats("https://jobs.lever.co.evil.com/acme/1") is None
    assert detect_ats("https://greenhouse.io.phish.net/acme/jobs/1") is None


def test_non_applicant_facing_lever_hosts_are_refused():
    assert detect_ats("https://hire.lever.co/acme") is None
    assert detect_ats("https://www.lever.co/pricing") is None


def test_supported_ats_is_exactly_two():
    assert SUPPORTED_ATS == ("greenhouse", "lever")


# ==========================================================================
# classify_field — pure, no browser
# ==========================================================================


@pytest.mark.parametrize(
    "field,expected",
    [
        ({"tag": "input", "type": "text", "label": "First Name"}, "first_name"),
        ({"tag": "input", "type": "text", "label": "Last Name"}, "last_name"),
        ({"tag": "input", "type": "text", "label": "Full Name"}, "name"),
        ({"tag": "input", "type": "email", "label": "Email"}, "email"),
        ({"tag": "input", "type": "text", "name": "email"}, "email"),
        ({"tag": "input", "type": "tel", "label": "Phone"}, "phone"),
        ({"tag": "input", "type": "text", "label": "Mobile number"}, "phone"),
        ({"tag": "input", "type": "file", "label": "Resume/CV"}, "resume"),
        ({"tag": "input", "type": "url", "label": "LinkedIn Profile"}, "linkedin"),
        ({"tag": "input", "type": "url", "label": "GitHub"}, "github"),
        ({"tag": "input", "type": "url", "label": "Personal Website"}, "website"),
        ({"tag": "input", "type": "checkbox", "label": "I agree to the privacy policy"},
         "consent"),
        ({"tag": "textarea", "label": "Cover Letter", "required": False},
         "cover_letter_optional"),
    ],
)
def test_classify_field_recognises_the_boring_fields(field, expected):
    assert classify_field(field) == expected


@pytest.mark.parametrize(
    "field",
    [
        {"tag": "select", "label": "Country"},
        {"tag": "input", "type": "radio", "label": "Are you eligible"},
        {"tag": "textarea", "label": "Cover Letter", "required": True},
        {"tag": "textarea", "label": "Why do you want to work here"},
        {"tag": "input", "type": "checkbox", "label": "Send me marketing updates"},
        {"tag": "input", "type": "checkbox", "label": "Join our talent community"},
        {"tag": "input", "type": "file", "label": "Upload a writing sample"},
        {"tag": "input", "type": "file", "label": "Portfolio"},
        {"tag": "input", "type": "text", "label": "Desired salary"},
        {"tag": "input", "type": "text", "label": "Something we have never seen"},
    ],
)
def test_classify_field_answers_unknown_when_it_is_not_certain(field):
    """"unknown" is the honest answer and the safe one — `inspect_form` bails
    on any required field that lands here."""
    assert classify_field(field) == "unknown"


def test_a_required_cover_letter_is_not_an_optional_cover_letter():
    optional = {"tag": "textarea", "label": "Cover Letter", "required": False}
    required = {"tag": "textarea", "label": "Cover Letter", "required": True}
    assert classify_field(optional) == "cover_letter_optional"
    assert classify_field(required) == "unknown"


def test_a_marketing_opt_in_is_not_consent():
    """Ticking a privacy acknowledgement is a legal necessity; opting the user
    into a mailing list is a decision that is not the bot's to make."""
    assert classify_field(
        {"tag": "input", "type": "checkbox",
         "label": "I agree to receive marketing updates"}
    ) == "unknown"


# ==========================================================================
# question_trigger
# ==========================================================================


@pytest.mark.parametrize(
    "label",
    [
        "Why do you want to work at Acme?",
        "Why us",
        "Describe your proudest project",
        "Tell us about yourself",
        "Explain your career gap",
        "Do you require visa sponsorship",
        "Will you now or in the future require sponsorship",
        "Are you authorized to work in the EU",
        "Do you have the right to work in Germany",
        "Do you hold a valid work permit",
        "Salary expectations",
        "Expected compensation",
        "What is your notice period",
        "Earliest start date",
        "When are you available",
        "How did you hear about us",
        "Employee referral",
        "Gender",
        "Race / Ethnicity",
        "Veteran status",
        "Disability status",
        "Preferred pronouns",
        "How many years of experience do you have with Python",
    ],
)
def test_question_trigger_fires_on_every_screener_phrasing(label):
    assert question_trigger({"label": label, "tag": "input", "type": "text"})


def test_question_trigger_reads_the_field_name_too():
    """Plenty of forms label a screener only in `name="why_us"`."""
    assert question_trigger({"label": "", "name": "why_us", "tag": "textarea"})
    assert question_trigger({"label": "", "id": "sponsorship_required", "tag": "input"})


@pytest.mark.parametrize(
    "label",
    ["First Name", "Last Name", "Email", "Phone", "Resume/CV", "LinkedIn Profile",
     "Website", "Full Name"],
)
def test_question_trigger_stays_quiet_on_ordinary_fields(label):
    assert question_trigger({"label": label, "tag": "input", "type": "text"}) is None


# ==========================================================================
# inspect_form — THE BAIL MATRIX
# ==========================================================================


def test_the_only_shape_that_passes():
    ok, reason = inspect_form(FakePage(simple_form()))
    assert ok is True
    assert reason == ""


def test_a_form_with_optional_extras_still_passes():
    page = FakePage(form_with(
        FakeElement("input", type="url", name="linkedin", label="LinkedIn Profile"),
        FakeElement("input", type="url", name="website", label="Website"),
        FakeElement("textarea", name="cover_letter", label="Cover Letter"),
        FakeElement("input", type="checkbox", name="privacy",
                    label="I agree to the privacy policy"),
    ))
    assert inspect_form(page)[0] is True


@pytest.mark.parametrize(
    "offender,expected_in_reason",
    [
        (FakeElement("select", name="country", label="Country"), "dropdown"),
        (FakeElement("select", name="source", label="How did you hear about us",
                     options=["LinkedIn", "Referral"]), "dropdown"),
        (FakeElement("input", type="radio", name="visa", label="Do you need a visa"),
         "radio"),
        # Bails as a textarea before the question check runs — the control
        # type is the more fundamental reason, and either way it bails.
        (FakeElement("textarea", name="why", label="Why do you want to work here"),
         "textarea"),
        (FakeElement("textarea", name="cover_letter", label="Cover Letter",
                     required=True), "required textarea"),
        (FakeElement("textarea", name="notes", label="Additional notes"),
         "not clearly an optional cover letter"),
        (FakeElement("input", type="text", name="salary",
                     label="Salary expectations"), "question"),
        (FakeElement("input", type="text", name="notice",
                     label="Notice period"), "question"),
        (FakeElement("input", type="text", name="sponsorship",
                     label="Do you require sponsorship?"), "question"),
        (FakeElement("input", type="text", name="gender", label="Gender"), "question"),
        (FakeElement("input", type="checkbox", name="marketing",
                     label="Send me marketing emails"), "consent"),
        (FakeElement("input", type="text", name="mystery", label="Mystery field",
                     required=True), "not one this bot can fill"),
        (FakeElement("input", type="text", name="yoe",
                     label="How many years of experience"), "question"),
    ],
)
def test_inspect_form_bails_on_every_screener_shape(offender, expected_in_reason):
    """The heart of the safety claim. Each of these must bail, and the reason
    must name the field so the user learns what the rules are catching."""
    ok, reason = inspect_form(FakePage(form_with(offender)))
    assert ok is False, f"{offender!r} should have bailed"
    assert expected_in_reason in reason.lower()
    assert reason.strip()


def test_bail_reasons_name_the_offending_field():
    page = FakePage(form_with(FakeElement("select", name="country", label="Country")))
    _, reason = inspect_form(page)
    assert "Country" in reason or "country" in reason


def test_a_long_form_is_a_screener():
    extras = [FakeElement("input", type="text", name=f"f{i}", label=f"Field {i}")
              for i in range(MAX_FORM_FIELDS)]
    ok, reason = inspect_form(FakePage(form_with(*extras)))
    assert ok is False
    assert "screener" in reason


def test_a_form_with_no_resume_upload_bails():
    """A simple form with nowhere to attach a CV is not an application form —
    it is probably a newsletter signup or a redirect stub."""
    no_resume = [el for el in simple_form()
                 if el.attrs.get("type") != "file"]
    ok, reason = inspect_form(FakePage(no_resume))
    assert ok is False
    assert "resume" in reason.lower()


def test_an_empty_page_bails():
    ok, reason = inspect_form(FakePage([]))
    assert ok is False


def test_a_page_that_cannot_be_queried_bails():
    """Failing to read the form must never be read as "the form is simple"."""
    class Hostile:
        def query_selector_all(self, selector):
            raise RuntimeError("detached frame")

    assert inspect_form(Hostile())[0] is False


def test_required_is_detected_from_a_bare_attribute():
    """`<input required>` yields "" from get_attribute in a real DOM, so
    presence is what counts — a truthiness test would read it as optional."""
    field = FakeElement("textarea", name="essay", label="Additional information")
    field.attrs["required"] = ""
    fields = collect_fields(FakePage([field]))
    assert fields[0]["required"] is True


def test_required_is_detected_from_an_asterisk_label():
    fields = collect_fields(FakePage([
        FakeElement("input", type="text", name="x", label="Portfolio URL *")
    ]))
    assert fields[0]["required"] is True


def test_hidden_and_submit_inputs_are_not_counted_as_fields():
    page = FakePage(simple_form() + [
        FakeElement("input", type="hidden", name="csrf"),
        FakeElement("input", type="hidden", name="utm"),
    ])
    assert inspect_form(page)[0] is True


# ==========================================================================
# eligible
# ==========================================================================


def test_eligible_on_the_happy_path(tmp_path: Path, memory_tracker):
    scored = with_pdf(tmp_path)
    ok, reason = eligible(scored, apply_config(tmp_path), memory_tracker)
    assert ok is True
    assert reason == ""


def test_apply_disabled_blocks_everything(tmp_path: Path):
    scored = with_pdf(tmp_path)
    ok, reason = eligible(scored, apply_config(tmp_path, enabled=False))
    assert ok is False
    assert "apply.enabled" in reason


def test_an_unsupported_ats_is_refused(tmp_path: Path):
    scored = with_pdf(tmp_path)
    scored.job.url = "https://acme.workday.com/apply/1"
    ok, reason = eligible(scored, apply_config(tmp_path))
    assert ok is False
    assert "Greenhouse or Lever" in reason
    assert scored.job.url in reason      # the digest shows this verbatim


def test_a_score_below_the_apply_floor_is_refused(tmp_path: Path):
    scored = with_pdf(tmp_path, score=79)
    ok, reason = eligible(scored, apply_config(tmp_path, min_score=80))
    assert ok is False
    assert "79" in reason and "80" in reason


def test_the_apply_floor_is_independent_of_the_scoring_threshold(tmp_path: Path):
    """apply.min_score is deliberately stricter than scoring.threshold: being
    worth an application is a lower bar than being worth an *automatic* one."""
    cfg = write_config(tmp_path, {"scoring": {"threshold": 65},
                                  "apply": {"min_score": 90, "require_pdf": False}})
    assert eligible(make_scored(score=70), cfg)[0] is False


def test_a_missing_pdf_sends_the_job_to_the_digest(tmp_path: Path):
    scored = make_scored(score=95)          # no artifacts.cv_pdf
    ok, reason = eligible(scored, apply_config(tmp_path, require_pdf=True))
    assert ok is False
    assert "render_pdf.py" in reason


def test_a_pdf_path_that_is_not_on_disk_is_refused(tmp_path: Path):
    scored = make_scored(score=95)
    scored.artifacts.cv_pdf = str(tmp_path / "nope.pdf")
    ok, reason = eligible(scored, apply_config(tmp_path))
    assert ok is False
    assert "missing on disk" in reason


def test_require_pdf_can_be_switched_off(tmp_path: Path):
    scored = make_scored(score=95)
    assert eligible(scored, apply_config(tmp_path, require_pdf=False))[0] is True


def test_an_already_applied_job_is_refused(tmp_path: Path, memory_tracker):
    scored = with_pdf(tmp_path)
    memory_tracker.record_job(scored.job, now=NOW)
    memory_tracker.record_status(scored.job.key, ApplyStatus.APPLIED, now=NOW)
    ok, reason = eligible(scored, apply_config(tmp_path), memory_tracker)
    assert ok is False
    assert "already applied" in reason


def test_a_dry_run_does_not_block_a_later_real_application(tmp_path: Path,
                                                           memory_tracker):
    scored = with_pdf(tmp_path)
    memory_tracker.record_job(scored.job, now=NOW)
    memory_tracker.record_status(scored.job.key, ApplyStatus.DRY_RUN, now=NOW)
    assert eligible(scored, apply_config(tmp_path), memory_tracker)[0] is True


def test_a_broken_tracker_refuses_rather_than_licensing_a_reapply(tmp_path: Path):
    """If we cannot verify the history, the safe answer is "do not apply"."""
    class Broken:
        def has_applied(self, key):
            raise RuntimeError("database is locked")

    ok, reason = eligible(with_pdf(tmp_path), apply_config(tmp_path), Broken())
    assert ok is False
    assert "not applying" in reason


def test_gates_are_reported_in_order(tmp_path: Path):
    """The first failure is the one the user can act on."""
    scored = make_scored(score=10)
    scored.job.url = "https://acme.workday.com/apply/1"
    _, reason = eligible(scored, apply_config(tmp_path))
    assert "Greenhouse or Lever" in reason     # not the score, not the PDF


# ==========================================================================
# apply_one — dry run
# ==========================================================================


def test_dry_run_fills_screenshots_and_does_not_submit(tmp_path: Path):
    """The single most important branch in the codebase."""
    scored = with_pdf(tmp_path)
    page = FakePage(simple_form())
    outcome = apply_one(scored, apply_config(tmp_path, dry_run=True), page=page)

    assert outcome.status is ApplyStatus.DRY_RUN
    assert page.submitted is False
    assert page.clicks == []
    assert outcome.screenshot and Path(outcome.screenshot).exists()
    assert "not submitted" in outcome.detail


def test_dry_run_saves_the_screenshot_next_to_the_tailored_documents(tmp_path: Path):
    scored = with_pdf(tmp_path)
    outcome = apply_one(scored, apply_config(tmp_path, dry_run=True),
                        page=FakePage(simple_form()))
    assert Path(outcome.screenshot).name == "form_filled.png"
    assert Path(outcome.screenshot).parent == Path(scored.artifacts.dir)


def test_dry_run_fills_the_applicant_details(tmp_path: Path):
    scored = with_pdf(tmp_path)
    page = FakePage(simple_form())
    apply_one(scored, apply_config(tmp_path, dry_run=True), page=page)
    filled = " ".join(page.filled.values())
    assert "Ada" in filled
    assert "ada@example.com" in filled


def test_dry_run_uploads_the_tailored_pdf(tmp_path: Path):
    scored = with_pdf(tmp_path)
    page = FakePage(simple_form())
    apply_one(scored, apply_config(tmp_path, dry_run=True), page=page)
    assert scored.artifacts.cv_pdf in page.uploaded.values()


def test_dry_run_never_submits_even_on_a_form_with_a_tempting_button(tmp_path: Path):
    for extra in (FakeElement("button", type="submit", id="submit_app",
                              label="Submit Application"),
                  FakeElement("input", type="submit", name="go", label="Apply Now")):
        page = FakePage(simple_form() + [extra])
        apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=True), page=page)
        assert page.submitted is False


# ==========================================================================
# apply_one — bail paths
# ==========================================================================


def test_a_screener_form_goes_to_the_digest_not_the_bin(tmp_path: Path):
    page = FakePage(form_with(FakeElement("select", name="country", label="Country")))
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path), page=page)
    assert outcome.status is ApplyStatus.DIGEST
    assert "dropdown" in outcome.detail
    assert page.filled == {}          # nothing was typed before bailing


def test_a_navigation_failure_is_reported_not_raised(tmp_path: Path):
    page = FakePage(simple_form(), goto_error=TimeoutError("navigation timeout"))
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path), page=page)
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert "timeout" in outcome.detail.lower()


def test_no_page_is_a_handoff_not_a_failure(tmp_path: Path):
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path), page=None)
    assert outcome.status is ApplyStatus.DIGEST
    assert "by hand" in outcome.detail


def test_apply_one_never_raises(tmp_path: Path):
    class Exploding:
        url = "https://boards.greenhouse.io/acme/jobs/1"

        def goto(self, *a, **k):
            raise RuntimeError("chromium crashed")

    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path), page=Exploding())
    assert isinstance(outcome, ApplyOutcome)
    assert outcome.status is ApplyStatus.APPLY_FAILED


# ==========================================================================
# apply_one — live submission
# ==========================================================================


def test_a_live_run_submits_and_confirms(tmp_path: Path):
    scored = with_pdf(tmp_path)
    page = FakePage(simple_form(), html="Thank you for applying")
    outcome = apply_one(scored, apply_config(tmp_path, dry_run=False), page=page)
    assert outcome.status is ApplyStatus.APPLIED
    assert page.submitted is True


def test_a_live_run_without_a_confirmation_is_a_failure_not_a_success(tmp_path: Path):
    """Reporting success on an unconfirmed submit would corrupt the tracker
    and permanently block a real application."""
    scored = with_pdf(tmp_path)
    page = FakePage(simple_form(), html="", confirmation=None)
    outcome = apply_one(scored, apply_config(tmp_path, dry_run=False), page=page)
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert "check manually" in outcome.detail


def test_a_live_run_refuses_to_submit_without_the_cv_it_promised(tmp_path: Path):
    scored = make_scored(score=95)      # no PDF on disk
    page = FakePage(simple_form(), html="Thank you for applying")
    outcome = apply_one(scored, apply_config(tmp_path, dry_run=False), page=page)
    assert outcome.status is ApplyStatus.DIGEST
    assert page.submitted is False
    assert "refusing to submit" in outcome.detail


def test_a_successful_application_is_written_to_the_tracker(tmp_path: Path,
                                                            memory_tracker):
    scored = with_pdf(tmp_path)
    memory_tracker.record_job(scored.job, now=NOW)
    page = FakePage(simple_form(), html="Thank you for applying")
    apply_one(scored, apply_config(tmp_path, dry_run=False), page=page,
              tracker=memory_tracker, now=NOW)
    assert memory_tracker.has_applied(scored.job.key) is True


def test_a_dry_run_is_recorded_without_blocking_a_future_application(tmp_path: Path,
                                                                     memory_tracker):
    scored = with_pdf(tmp_path)
    memory_tracker.record_job(scored.job, now=NOW)
    apply_one(scored, apply_config(tmp_path, dry_run=True),
              page=FakePage(simple_form()), tracker=memory_tracker, now=NOW)
    assert memory_tracker.get_status(scored.job.key) == ApplyStatus.DRY_RUN.value
    assert memory_tracker.has_applied(scored.job.key) is False


# ==========================================================================
# run
# ==========================================================================


def test_run_applies_to_eligible_jobs_only(tmp_path: Path, memory_tracker):
    good = with_pdf(tmp_path, make_scored(score=95, ats_job_id="1"))
    low = with_pdf(tmp_path, make_scored(score=50, ats_job_id="2"))
    browser = FakeBrowser()
    out = run([good, low], apply_config(tmp_path), tracker=memory_tracker,
              browser=browser)

    assert good.status is ApplyStatus.DRY_RUN
    assert low.status is ApplyStatus.DIGEST
    assert "min_score" in low.status_detail
    assert len(browser.created) == 1      # no page opened for the ineligible job
    assert len(out) == 2


def test_run_honours_max_per_run(tmp_path: Path, memory_tracker):
    jobs = [with_pdf(tmp_path, make_scored(score=95, ats_job_id=str(i)))
            for i in range(5)]
    browser = FakeBrowser()
    run(jobs, apply_config(tmp_path, max_per_run=2), tracker=memory_tracker,
        browser=browser)
    assert len(browser.created) == 2
    assert sum(1 for j in jobs if j.status is ApplyStatus.DRY_RUN) == 2


def test_run_closes_every_page_it_opens(tmp_path: Path, memory_tracker):
    jobs = [with_pdf(tmp_path, make_scored(score=95, ats_job_id=str(i)))
            for i in range(3)]
    browser = FakeBrowser()
    run(jobs, apply_config(tmp_path), tracker=memory_tracker, browser=browser)
    assert all(page.closed for page in browser.created)


def test_run_with_apply_disabled_opens_no_browser(tmp_path: Path, memory_tracker):
    jobs = [with_pdf(tmp_path)]
    browser = FakeBrowser()
    run(jobs, apply_config(tmp_path, enabled=False), tracker=memory_tracker,
        browser=browser)
    assert browser.created == []


def test_run_leaves_already_settled_jobs_alone(tmp_path: Path, memory_tracker):
    settled = with_pdf(tmp_path, make_scored(score=95))
    settled.status = ApplyStatus.SCORED_BELOW
    browser = FakeBrowser()
    run([settled], apply_config(tmp_path), tracker=memory_tracker, browser=browser)
    assert settled.status is ApplyStatus.SCORED_BELOW
    assert browser.created == []


def test_run_never_raises(tmp_path: Path, memory_tracker):
    class Broken:
        def new_page(self, **kwargs):
            raise RuntimeError("browser died")

        def close(self):
            pass

    jobs = [with_pdf(tmp_path)]
    out = run(jobs, apply_config(tmp_path), tracker=memory_tracker, browser=Broken())
    assert len(out) == 1
    assert jobs[0].status in (ApplyStatus.DIGEST, ApplyStatus.APPLY_FAILED)


def test_run_on_an_empty_list(tmp_path: Path, memory_tracker):
    assert run([], apply_config(tmp_path), tracker=memory_tracker,
               browser=FakeBrowser()) == []
