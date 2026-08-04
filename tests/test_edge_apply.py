"""Edge cases for LEG 3 — APPLY: `src/apply/autoapply.py`, `src/pdf.py`, and
the tracker's role as the double-apply gate.

`tests/test_autoapply.py` throws abstract screener shapes at `inspect_form`.
This file throws the *actual markup and the actual wording* a European job
hunt produces in one real week:

  * Greenhouse's 2026 form shapes — the EEO block, the work-authorisation
    pair, `urls[LinkedIn Profile]`, a drag-and-drop resume dropzone, a custom
    question whose text lives in a sibling `<label for=...>`, and a form split
    across two pages where page 1 is trivial and the screener is on page 2;
  * Lever's "additional information" textarea and its five-URL row;
  * German, French, Dutch and Spanish screeners — an EU-focused tool that only
    recognises English questions will happily submit a form that asked for
    salary expectations;
  * day-over-day identity drift of the same posting, against `has_applied`;
  * the half-failures of a real submission: a crash between the click and the
    confirmation, a page that navigates away mid-fill, a tracker write that
    fails after the application has already gone out;
  * the PDF hook's remaining half-failures, since "no PDF" is what stands
    between the user and an application submitted without a CV.

The bias is the same one the module states: a wrong bail costs one click, a
wrong submit costs the user their standing with an employer. Where the code
currently gets that wrong, the test is `xfail(strict=True)` so it turns into a
failure the moment somebody fixes it.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.apply.autoapply import (
    MAX_FORM_FIELDS,
    apply_one,
    detect_ats,
    eligible,
    inspect_form,
    run,
)
from src.db import Tracker
from src.models import ApplyStatus
from src.pdf import render_if_available
from tests.conftest import (
    NOW,
    FakeBrowser,
    FakeElement,
    FakePage,
    form_with,
    make_job,
    make_scored,
    simple_form,
    write_config,
)


# ==========================================================================
# local helpers and fakes
# ==========================================================================


def apply_config(tmp_path: Path, *, applicant: dict[str, Any] | None = None,
                 **apply_overrides: Any):
    """A config rooted in `tmp_path` with the apply section spelled out."""
    settings: dict[str, Any] = {
        "enabled": True, "dry_run": True, "min_score": 80,
        "require_pdf": True, "max_per_run": 5, "headless": True,
        "timeout_seconds": 5,
    }
    settings.update(apply_overrides)
    overrides: dict[str, Any] = {
        "apply": settings,
        "output": {"dir": str(tmp_path / "output")},
    }
    if applicant is not None:
        overrides["applicant"] = applicant
    return write_config(tmp_path, overrides)


def with_pdf(tmp_path: Path, scored=None, *, score: int = 90, slug: str = "a",
             **kwargs: Any):
    """A scored job whose tailored CV PDF really exists on disk."""
    scored = scored if scored is not None else make_scored(score=score, **kwargs)
    directory = tmp_path / "artifacts" / slug
    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake tailored cv")
    scored.artifacts.dir = str(directory)
    scored.artifacts.cv_pdf = str(pdf)
    return scored


def greenhouse_job(**kwargs: Any):
    """A posting on the shape of URL Greenhouse actually serves in 2026."""
    defaults: dict[str, Any] = {
        "source": "greenhouse", "company": "Acme", "title": "Backend Engineer",
        "url": "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "location": "Berlin, Germany", "ats": "greenhouse",
        "ats_job_id": "4012345",
    }
    defaults.update(kwargs)
    return make_job(**defaults)


class TwoStepPage(FakePage):
    """A Greenhouse form split across two pages.

    Step 1 is name/email/resume and a "Continue" button; the screener only
    exists on step 2. Clicking the step-1 button swaps the DOM, exactly as a
    single-page app would.
    """

    def __init__(self, step_one, step_two, *, step_two_html: str = "", **kwargs: Any):
        kwargs.setdefault("confirmation", None)
        super().__init__(step_one, **kwargs)
        self._step_two = list(step_two)
        self._step_two_html = step_two_html
        self.advanced = False

    @property
    def input_queries(self) -> int:
        """How many times the form's controls were read — i.e. inspections."""
        return sum(1 for name, arg in self.actions
                   if name == "query_selector_all" and arg == "input")

    def click(self, selector: str, **kwargs: Any) -> None:
        super().click(selector, **kwargs)
        if not self.advanced and selector == "button[type=submit]":
            self.advanced = True
            self.elements = list(self._step_two)
            self.html = self._step_two_html


class DyingPage(FakePage):
    """The browser dies right after the submit click.

    A laptop lid, an OOM kill or a Chromium crash between "the POST left the
    machine" and "we read the response" is an ordinary Tuesday for a scraper.
    """

    def wait_for_selector(self, selector: str, **kwargs: Any):
        if self.submitted:
            raise RuntimeError("Target page, context or browser has been closed")
        return super().wait_for_selector(selector, **kwargs)

    def content(self) -> str:
        if self.submitted:
            raise RuntimeError("Target page, context or browser has been closed")
        return super().content()


class DiskFullTracker:
    """A tracker whose reads work and whose writes fail.

    SQLite raising `database or disk is full` (or `database is locked`, if the
    user opened the tracker in a DB browser) is the realistic way the status
    write fails *after* the application has already been submitted.
    """

    def __init__(self, inner: Tracker) -> None:
        self.inner = inner
        self.attempted: list[Any] = []

    def has_applied(self, key: str) -> bool:
        return self.inner.has_applied(key)

    def get_status(self, key: str):
        return self.inner.get_status(key)

    def record_job(self, job, *, now=None):
        return self.inner.record_job(job, now=now)

    def record_status(self, key: str, status, **kwargs: Any) -> None:
        self.attempted.append(status)
        raise RuntimeError("database or disk is full")


class Hook:
    """Stand-in for the user's `src/render_pdf.py`, with the failure modes a
    half-finished ReportLab script actually produces."""

    def __init__(self, behaviour: str = "pdf", *, payload: bytes = b"",
                 delay: float = 0.0) -> None:
        self.behaviour = behaviour
        self.payload = payload
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    def render(self, cv_markdown: str, out_path: str) -> None:
        self.calls.append((cv_markdown, str(out_path)))
        if self.delay:
            time.sleep(self.delay)
        target = Path(out_path)
        if self.behaviour == "directory":
            target.mkdir(parents=True, exist_ok=True)
            return
        if self.behaviour == "elsewhere":
            (target.parent / "actually-here.pdf").write_bytes(b"%PDF-1.4\nreal")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload or b"%PDF-1.4\nfake pdf content")


# ==========================================================================
# 1. REAL GREENHOUSE FORM SHAPES
# ==========================================================================


def test_the_greenhouse_eeo_block_at_the_bottom_of_every_us_owned_board_bails():
    """Greenhouse appends a voluntary self-identification block — gender,
    ethnicity, veteran and disability selects — to most boards, and marks none
    of them required. Answering any of them for the user is not the bot's
    call."""
    eeo = [
        FakeElement("select", name="gender", id="gender", label="Gender",
                    options=["Male", "Female", "Decline to self identify"]),
        FakeElement("select", name="hispanic_ethnicity", id="hispanic_ethnicity",
                    label="Are you Hispanic/Latino?"),
        FakeElement("select", name="veteran_status", id="veteran_status",
                    label="Veteran Status"),
        FakeElement("select", name="disability_status", id="disability_status",
                    label="Disability Status"),
    ]
    ok, reason = inspect_form(FakePage(form_with(*eeo)))
    assert ok is False
    assert "dropdown" in reason.lower()


def test_the_work_authorisation_pair_that_is_on_almost_every_eu_posting_bails():
    """"Are you legally authorized to work in Germany?" plus "Will you now or
    in the future require sponsorship?" is the single most common screener in
    Europe. Getting either wrong is a lie on an application."""
    pair = [
        FakeElement("select", name="authorized", id="authorized",
                    label="Are you legally authorized to work in Germany?"),
        FakeElement("select", name="sponsorship", id="sponsorship",
                    label="Will you now or in the future require sponsorship?"),
    ]
    ok, reason = inspect_form(FakePage(form_with(*pair)))
    assert ok is False
    assert reason.strip()


def test_the_linkedin_and_website_pair_is_recognised_through_greenhouse_bracket_names(
        tmp_path: Path):
    """Greenhouse names its profile inputs `urls[LinkedIn Profile]` and
    `urls[Website]` and puts the visible text in a sibling label. The bot has
    to recognise them from the bracketed name alone, or it leaves the user's
    LinkedIn off every application it files."""
    urls = [
        FakeElement("input", type="text", name="urls[LinkedIn Profile]",
                    id="job_application_urls_linkedin_profile", label=""),
        FakeElement("input", type="text", name="urls[Website]",
                    id="job_application_urls_website", label=""),
    ]
    page = FakePage(form_with(*urls))
    assert inspect_form(page)[0] is True

    apply_one(with_pdf(tmp_path), apply_config(tmp_path), page=page)
    assert "https://linkedin.com/in/ada" in " ".join(page.filled.values())


def test_a_drag_and_drop_dropzone_hiding_a_file_input_still_receives_the_cv(
        tmp_path: Path):
    """The modern Greenhouse resume field is a drag-and-drop div wrapping an
    `<input type=file>` that is never visible. If the bot only looked at
    visible controls it would report "no resume field" on every current board
    and auto-apply would quietly stop working."""
    dropzone = [el for el in simple_form() if el.attrs.get("type") != "file"]
    hidden_file = FakeElement("input", type="file", name="resume", id="resume",
                              label="Resume", classes=["dropzone__input"],
                              style="display:none", required=True)
    page = FakePage(dropzone[:-1] + [hidden_file] + dropzone[-1:])

    assert inspect_form(page)[0] is True
    scored = with_pdf(tmp_path)
    apply_one(scored, apply_config(tmp_path), page=page)
    assert scored.artifacts.cv_pdf in page.uploaded.values()


def test_a_dropzone_file_input_with_no_resume_hint_at_all_bails():
    """Some boards render the dropzone input as a bare `<input type=file
    class=dz-hidden-input>`. With nothing identifying it, uploading the user's
    CV into it is a guess — it could be an ID scan slot."""
    fields = [el for el in simple_form() if el.attrs.get("type") != "file"]
    anonymous = FakeElement("input", type="file", classes=["dz-hidden-input"])
    ok, reason = inspect_form(FakePage(fields[:-1] + [anonymous] + fields[-1:]))
    assert ok is False
    assert "resume" in reason.lower()


def test_greenhouse_csrf_and_analytics_inputs_do_not_eat_the_field_budget():
    """A real Greenhouse page carries a dozen `<input type=hidden>` controls
    (authenticity_token, utm_*, gh_src). Counting them would trip the
    "long forms are screeners" limit on every genuinely simple form."""
    noise = [FakeElement("input", type="hidden", name=f"utm_{i}")
             for i in range(MAX_FORM_FIELDS)]
    ok, reason = inspect_form(FakePage(simple_form() + noise))
    assert ok is True, reason


@pytest.mark.xfail(
    strict=True,
    reason="a Greenhouse custom question whose text sits in a sibling "
           "<label for=...> is invisible to the bot, so an unanswered screener "
           "is submitted",
)
def test_a_greenhouse_custom_question_labelled_by_a_sibling_element_is_caught():
    """This is exactly how Greenhouse renders every custom question: the text
    lives in `<label for="job_application_answers_attributes_0_text_value">`
    and the input carries no aria-label, no placeholder and no required
    attribute (validation is client-side). The bot reads only attributes on
    the element itself, so "Why do you want to work at Acme?" arrives as an
    anonymous optional text box — and the form is submitted with it blank."""
    question = FakeElement(
        "input", type="text",
        name="job_application[answers_attributes][0][text_value]",
        id="job_application_answers_attributes_0_text_value", label="",
    )
    ok, _ = inspect_form(FakePage(form_with(question)))
    assert ok is False


def test_the_same_custom_question_is_caught_once_it_is_marked_required():
    """The pair to the case above, and the reason it is a gap rather than a
    total absence of a guard: the moment Greenhouse emits `required`, the
    unrecognised-required-field rule catches the identical field."""
    question = FakeElement(
        "input", type="text",
        name="job_application[answers_attributes][0][text_value]",
        id="job_application_answers_attributes_0_text_value", label="",
        required=True,
    )
    ok, reason = inspect_form(FakePage(form_with(question)))
    assert ok is False
    assert "not one this bot can fill" in reason


def test_a_two_page_form_is_clicked_through_and_page_two_is_never_inspected(
        tmp_path: Path):
    """The interesting shape: page 1 is name/email/resume and a "Continue"
    button, and the salary/notice-period screener only exists on page 2.

    `inspect_form` runs once, before any click, so a form can pass inspection
    and *then* reveal its screener. The bot clicks Continue, finds no
    confirmation and reports a failure — nothing is submitted and no question
    is answered, which is the safe direction — but it never looks at page 2,
    so it repeats the click on every subsequent run."""
    step_two = [
        FakeElement("input", type="text", name="salary",
                    label="Salary expectations", required=True),
        FakeElement("select", name="notice", label="Notice period"),
    ]
    page = TwoStepPage(
        [el for el in simple_form() if el.attrs.get("id") != "submit_app"]
        + [FakeElement("button", type="submit", name="continue", id="continue",
                       label="Continue", text="Continue")],
        step_two,
        step_two_html="<h2>A few more questions</h2>",
    )

    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=page)

    assert page.advanced is True, "the Continue button was clicked"
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert "no confirmation" in outcome.detail
    # Two reads of the form (inspect_form, then collect_fields for filling),
    # both against page 1. Page 2's screener is never examined.
    assert page.input_queries == 2


@pytest.mark.xfail(
    strict=True,
    reason="step 2 of a multi-page form saying 'Thanks for applying' is read "
           "as a confirmation, so a never-submitted application is recorded "
           "APPLIED and the real one is blocked forever",
)
def test_a_multi_page_form_that_thanks_you_on_step_two_is_not_a_confirmation(
        tmp_path: Path, memory_tracker):
    """Multi-step forms routinely acknowledge step 1 — "Thanks for applying!
    Just a few more questions." — and the confirmation check is a substring
    search over the whole page. So the bot records `applied` for an
    application that is still sitting on page 2, and `applied` is terminal:
    the user can never file the real one through this tool again."""
    page = TwoStepPage(
        [el for el in simple_form() if el.attrs.get("id") != "submit_app"]
        + [FakeElement("button", type="submit", name="continue", id="continue",
                       label="Continue", text="Continue")],
        [FakeElement("input", type="text", name="salary",
                     label="Salary expectations", required=True)],
        step_two_html="<h2>Thanks for applying! Just a few more questions.</h2>",
    )
    scored = with_pdf(tmp_path)
    memory_tracker.record_job(scored.job, now=NOW)

    apply_one(scored, apply_config(tmp_path, dry_run=False), page=page,
              tracker=memory_tracker, now=NOW)
    assert memory_tracker.has_applied(scored.job.key) is False


def test_the_greenhouse_submit_button_is_preferred_over_a_save_draft_button(
        tmp_path: Path):
    """Boards that offer "Save draft" render it as another
    `button[type=submit]`. Greenhouse's real control is `#submit_app`, and it
    is tried first for exactly this reason."""
    page = FakePage(
        simple_form()[:-1]
        + [FakeElement("button", type="submit", name="draft", id="save_draft",
                       label="Save draft", text="Save draft"),
           FakeElement("button", type="submit", name="submit", id="submit_app",
                       label="Submit Application", text="Submit Application")],
        html="Thank you for applying",
    )
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=page)
    assert outcome.status is ApplyStatus.APPLIED
    assert page.clicks == ["#submit_app"]


def test_a_javascript_only_form_with_no_submit_control_fails_loudly(tmp_path: Path):
    """Some boards bind the submit to a plain `<div role=button>`. Silently
    doing nothing and calling it a success would put a job into the digest's
    "auto-applied" section that was never sent."""
    page = FakePage([el for el in simple_form()
                     if el.attrs.get("id") != "submit_app"])
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=page)
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert "submit" in outcome.detail.lower()


@pytest.mark.parametrize(
    "label",
    [
        "I consent to Acme storing my data for 12 months",
        "I consent to Acme GmbH retaining my application data for 24 months",
        "I have read and accept the privacy policy",
        "I agree to the terms and conditions",
    ],
)
def test_gdpr_retention_consent_variants_are_ticked_rather_than_bailed_on(
        tmp_path: Path, label):
    """Every EU board carries one of these, and it is legally required to file
    the application at all. Bailing on it would mean auto-apply never fires in
    Europe; the line is drawn at consent, not at marketing opt-ins."""
    box = FakeElement("input", type="checkbox", name="gdpr", id="gdpr_consent",
                      label=label)
    page = FakePage(form_with(box), html="Thank you for applying")
    assert inspect_form(page)[0] is True

    apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False), page=page)
    assert "#gdpr_consent" in page.clicks


def test_an_attestation_checkbox_is_not_a_consent_checkbox():
    """"I certify that the information provided is true and complete" is a
    statement the user makes under their own name, not a privacy notice. The
    bot must not sign it."""
    box = FakeElement("input", type="checkbox", name="certify",
                      label="I certify that the information provided is true "
                            "and complete")
    ok, reason = inspect_form(FakePage(form_with(box)))
    assert ok is False
    assert "consent" in reason.lower()


@pytest.mark.xfail(
    strict=True,
    reason="a required field the bot recognises but has no config value for "
           "(phone left blank) is submitted empty instead of bailing",
)
def test_a_required_phone_the_config_cannot_fill_is_not_submitted_blank(
        tmp_path: Path):
    """Plenty of people leave `applicant.phone` empty, and plenty of German
    boards mark the phone field required. `inspect_form` only bails on fields
    it cannot *classify*; a phone it recognises but cannot fill sails through,
    the field is skipped, and submit is clicked on an incomplete form."""
    form = simple_form()
    for el in form:
        if el.attrs.get("name") == "phone":
            el.attrs["required"] = "true"
            el.required = True
    page = FakePage(form, html="Thank you for applying")
    config = apply_config(
        tmp_path, dry_run=False,
        applicant={"name": "Ada Lovelace", "email": "ada@example.com",
                   "phone": "", "linkedin": "https://linkedin.com/in/ada"},
    )
    apply_one(with_pdf(tmp_path), config, page=page)
    assert page.submitted is False


# ==========================================================================
# 2. REAL LEVER FORM SHAPES
# ==========================================================================


def test_levers_additional_information_box_bails_on_practically_every_lever_form():
    """Lever puts an optional "Additional information" textarea on the default
    posting template, so this bail fires on most Lever jobs the user will ever
    see. That is deliberate and documented (docs/EVALUATION.md §3): the forms
    a bot may submit are a small, deliberately boring subset, and a free-text
    box is the user's to fill even when it is optional."""
    comments = FakeElement("textarea", name="comments",
                           placeholder="Additional information")
    ok, reason = inspect_form(FakePage(form_with(comments)))
    assert ok is False
    assert "cover letter" in reason.lower()


def test_levers_five_url_row_passes_and_only_the_urls_we_know_are_filled(
        tmp_path: Path):
    """Lever asks for LinkedIn, Twitter, GitHub, Portfolio and Other in one
    row. None are required, so the form stays simple — but the bot must fill
    only the ones the user actually configured and leave the rest alone rather
    than pasting the LinkedIn URL into all five."""
    urls = [FakeElement("input", type="text", name=f"urls[{name}]", label=name)
            for name in ("LinkedIn", "Twitter", "GitHub", "Portfolio", "Other")]
    page = FakePage(form_with(*urls))
    assert inspect_form(page)[0] is True

    config = apply_config(
        tmp_path,
        applicant={"name": "Ada Lovelace", "email": "ada@example.com",
                   "phone": "+49 30 123456",
                   "linkedin": "https://linkedin.com/in/ada",
                   "github": "https://github.com/ada"},
    )
    apply_one(with_pdf(tmp_path), config, page=page)
    filled = page.filled
    assert 'input[name="urls[LinkedIn]"]' in filled
    assert 'input[name="urls[GitHub]"]' in filled
    assert 'input[name="urls[Twitter]"]' not in filled
    assert 'input[name="urls[Other]"]' not in filled


def test_a_lever_card_question_rendered_as_a_select_bails():
    """Lever's custom "cards" carry opaque names like
    `cards[6f2a][field0]`. As a select there is nothing to interpret and
    nothing to guess at — bail."""
    card = FakeElement("select", name="cards[6f2a][field0]",
                       options=["0-2 years", "3-5 years", "5+ years"])
    ok, reason = inspect_form(FakePage(form_with(card)))
    assert ok is False
    assert "dropdown" in reason.lower()


# ==========================================================================
# 3. NON-ENGLISH FORMS — the EU tool's blind spot
# ==========================================================================


GERMAN_AND_FRENCH_SCREENERS = [
    ("Gehaltsvorstellung", "gehaltsvorstellung"),
    ("Gehaltsvorstellungen (brutto p.a.)", "gehalt"),
    ("Kündigungsfrist", "kuendigungsfrist"),
    ("Frühestmöglicher Eintrittstermin", "eintrittstermin"),
    ("Verfügbarkeit ab", "verfuegbarkeit"),
    ("Prétentions salariales", "pretentions"),
    ("Rémunération souhaitée", "remuneration"),
    ("Opzegtermijn", "opzegtermijn"),
    ("Salarisindicatie", "salarisindicatie"),
    ("Expectativas salariales", "expectativas"),
]


@pytest.mark.xfail(
    strict=True,
    reason="question_trigger only knows English, so a German/French/Dutch "
           "salary-expectation or notice-period screener is submitted rather "
           "than bailed",
)
@pytest.mark.parametrize("label,name", GERMAN_AND_FRENCH_SCREENERS)
def test_a_non_english_short_answer_screener_bails(label, name):
    """A Berlin or Paris startup runs an English-language Greenhouse form with
    one locally-worded custom question: "Gehaltsvorstellung",
    "Kündigungsfrist", "Prétentions salariales". These are short-answer text
    inputs, not textareas, and Greenhouse does not mark custom questions
    required, so nothing in the bail matrix fires. The tool is EU-focused and
    it will submit a form that asked the user for their salary expectations."""
    field = FakeElement("input", type="text", name=name, label=label)
    ok, _ = inspect_form(FakePage(form_with(field)))
    assert ok is False


@pytest.mark.parametrize(
    "label",
    ["Warum möchten Sie bei uns arbeiten?",
     "Pourquoi nous rejoindre ?",
     "¿Por qué quieres trabajar con nosotros?",
     "Waarom wil je bij ons werken?"],
)
def test_a_non_english_question_that_kept_its_question_mark_is_still_caught(label):
    """The one non-English screener shape that does bail, and the reason the
    gap above is narrow rather than total: punctuation is language-neutral, so
    any question phrased as a question is caught whatever the language."""
    field = FakeElement("input", type="text", name="q1", label=label)
    ok, reason = inspect_form(FakePage(form_with(field)))
    assert ok is False
    assert "question mark" in reason


def test_a_required_german_salary_field_is_caught_by_the_required_unknown_rule():
    """The pair to the xfail above. When the board does mark the German field
    required, the unrecognised-required-field rule catches it — so the gap is
    specifically "optional-looking non-English screener", not "non-English
    anything"."""
    field = FakeElement("input", type="text", name="gehaltsvorstellung",
                        label="Gehaltsvorstellung", required=True)
    ok, reason = inspect_form(FakePage(form_with(field)))
    assert ok is False
    assert "not one this bot can fill" in reason


def test_a_german_consent_checkbox_bails_instead_of_being_ticked():
    """"Ich stimme der Speicherung meiner Daten zu" is the German twin of the
    GDPR box the bot happily ticks in English. It is not recognised, so the
    whole form bails. That costs the user a click and nothing else — the safe
    direction — but it means a fully German-language board is effectively
    outside auto-apply."""
    box = FakeElement("input", type="checkbox", name="einwilligung",
                      label="Ich stimme der Speicherung meiner Daten zu")
    ok, reason = inspect_form(FakePage(form_with(box)))
    assert ok is False
    assert "consent" in reason.lower()


def test_a_fully_german_form_bails_on_its_very_first_field():
    """A German-only Greenhouse board labels its fields Vorname / Nachname /
    E-Mail / Telefon / Lebenslauf. None of the German words are in the field
    vocabulary, so the *required* ones bail immediately — the form never even
    reaches the "is there somewhere to attach a CV" check, because "Vorname"
    is already a required field the bot cannot fill.

    Which is the safe direction, and worth stating plainly: in practice
    auto-apply only ever fires on English-labelled boards. The residual risk
    is the mixed form — English labels with one German screener — and that is
    the xfail above."""
    german = [
        FakeElement("input", type="text", name="vorname", label="Vorname",
                    required=True),
        FakeElement("input", type="text", name="nachname", label="Nachname",
                    required=True),
        FakeElement("input", type="email", name="email", label="E-Mail",
                    required=True),
        FakeElement("input", type="tel", name="telefon", label="Telefon"),
        FakeElement("input", type="file", name="lebenslauf", label="Lebenslauf",
                    required=True),
    ]
    ok, reason = inspect_form(FakePage(german))
    assert ok is False
    assert "not one this bot can fill" in reason
    assert "Vorname" in reason

    # And with every German field optional, it is the missing CV slot that
    # bails: "Lebenslauf" is not recognised as a resume upload either.
    for element in german:
        element.required = False
        element.attrs.pop("required", None)
    ok, reason = inspect_form(FakePage(german))
    assert ok is False
    assert "resume" in reason.lower()


# ==========================================================================
# 4. IDENTITY DRIFT AND THE DOUBLE-APPLY GUARANTEE
# ==========================================================================


def test_a_retitled_requisition_is_still_the_job_we_already_applied_to(
        tmp_path: Path, memory_tracker):
    """Companies edit titles after publishing — "Backend Engineer" becomes
    "Backend Engineer (m/w/d)" the day HR notices. The tracker key is built
    from the ATS id, so the edit must not reopen the job for a second
    application."""
    monday = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(monday.job, now=NOW)
    memory_tracker.record_status(monday.job.key, ApplyStatus.APPLIED, now=NOW)

    tuesday = with_pdf(tmp_path, make_scored(
        job=greenhouse_job(title="Backend Engineer (m/w/d)")))
    ok, reason = eligible(tuesday, apply_config(tmp_path), memory_tracker)
    assert ok is False
    assert "already applied" in reason


def test_a_board_migration_does_not_reset_the_double_apply_guarantee(
        tmp_path: Path, memory_tracker):
    """Greenhouse moved every board from `boards.greenhouse.io` to
    `job-boards.greenhouse.io`. The URL is not part of the identity, so a
    migrated posting is the same posting."""
    old = with_pdf(tmp_path, make_scored(
        job=greenhouse_job(url="https://boards.greenhouse.io/acme/jobs/4012345")))
    memory_tracker.record_job(old.job, now=NOW)
    memory_tracker.record_status(old.job.key, ApplyStatus.APPLIED, now=NOW)

    new = with_pdf(tmp_path, make_scored(job=greenhouse_job(
        url="https://job-boards.greenhouse.io/acme/jobs/4012345?gh_src=abcd")))
    assert new.job.key == old.job.key
    assert eligible(new, apply_config(tmp_path), memory_tracker)[0] is False


def test_a_legal_suffix_appearing_in_the_company_name_does_not_reset_it(
        tmp_path: Path, memory_tracker):
    """One day the board says "Acme", the next it says "Acme GmbH" because
    somebody filled in the legal-entity field. Legal-suffix stripping keeps
    that from looking like a different employer."""
    plain = with_pdf(tmp_path, make_scored(job=greenhouse_job(company="Acme")))
    memory_tracker.record_job(plain.job, now=NOW)
    memory_tracker.record_status(plain.job.key, ApplyStatus.APPLIED, now=NOW)

    legal = with_pdf(tmp_path, make_scored(job=greenhouse_job(company="Acme GmbH")))
    assert eligible(legal, apply_config(tmp_path), memory_tracker)[0] is False


@pytest.mark.xfail(
    strict=True,
    reason="a company rename re-keys every open requisition, so an "
           "already-applied job becomes eligible again and is applied to twice",
)
def test_a_company_rename_does_not_reset_the_double_apply_guarantee(
        tmp_path: Path, memory_tracker):
    """`fetch_greenhouse` derives the company from the board slug unless the
    payload carries a better name, so a rebrand — or the user editing the
    watchlist slug — changes the company string on every posting overnight.
    The ATS id is unchanged and the URL is unchanged, but the tracker key is
    company-derived, so yesterday's applications stop counting."""
    before = with_pdf(tmp_path, make_scored(job=greenhouse_job(company="Acme")))
    memory_tracker.record_job(before.job, now=NOW)
    memory_tracker.record_status(before.job.key, ApplyStatus.APPLIED, now=NOW)

    after = with_pdf(tmp_path, make_scored(
        job=greenhouse_job(company="Acme Technologies")))
    assert eligible(after, apply_config(tmp_path), memory_tracker)[0] is False


@pytest.mark.xfail(
    strict=True,
    reason="the same role re-posted under a new requisition id is a new "
           "tracker key, so the bot files a second application for a job it "
           "already applied to",
)
def test_the_same_role_reposted_next_week_is_not_applied_to_twice(
        tmp_path: Path, memory_tracker):
    """Recruiters close and re-open requisitions constantly — to refresh the
    posting date, to move it to a new hiring manager, to reset the applicant
    list. Same company, same title, same city, new id. `Job.dedupe_key`
    already collapses these (and the tracker even stores and indexes it), but
    `has_applied` only ever looks at `Job.key`, so the user sends the same
    company two applications a week apart."""
    first = with_pdf(tmp_path, make_scored(job=greenhouse_job(ats_job_id="4012345")))
    memory_tracker.record_job(first.job, now=NOW)
    memory_tracker.record_status(first.job.key, ApplyStatus.APPLIED, now=NOW)

    repost = with_pdf(tmp_path, make_scored(job=greenhouse_job(ats_job_id="4099999")))
    assert repost.job.dedupe_key == first.job.dedupe_key
    ok, _ = eligible(repost, apply_config(tmp_path), memory_tracker,)
    assert ok is False


def test_a_linkedin_sighting_of_an_applied_job_is_a_second_row_but_never_a_second_apply(
        tmp_path: Path, memory_tracker):
    """Monday the job arrives via a LinkedIn alert with no ATS id; Tuesday the
    same role arrives from Greenhouse with one. Those are two rows to the
    tracker — but the LinkedIn copy links to linkedin.com, which is not a
    supported application form, so it can only ever reach the digest. The
    duplicate is one extra digest card, not one extra application."""
    applied = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(applied.job, now=NOW)
    memory_tracker.record_status(applied.job.key, ApplyStatus.APPLIED, now=NOW)

    via_linkedin = with_pdf(tmp_path, make_scored(job=make_job(
        source="linkedin_email", company="Acme", title="Backend Engineer",
        location="Berlin, Germany", ats=None, ats_job_id=None,
        url="https://www.linkedin.com/jobs/view/3987654321")))

    assert via_linkedin.job.key != applied.job.key
    assert memory_tracker.has_applied(via_linkedin.job.key) is False
    ok, reason = eligible(via_linkedin, apply_config(tmp_path), memory_tracker)
    assert ok is False
    assert "Greenhouse or Lever" in reason


def test_the_applied_row_is_visible_to_a_second_tracker_on_the_same_file(
        tmp_path: Path):
    """The tracker is one SQLite file and the guarantee has to survive the
    process that wrote it. If a cron run and a manual run overlap, the second
    one must see the first one's application the moment it is committed."""
    path = tmp_path / "output" / "tracker.sqlite3"
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    writer = Tracker(path)
    reader = Tracker(path)
    try:
        writer.record_job(scored.job, now=NOW)
        page = FakePage(simple_form(), html="Thank you for applying")
        apply_one(scored, apply_config(tmp_path, dry_run=False), page=page,
                  tracker=writer, now=NOW)
        assert reader.has_applied(scored.job.key) is True
        assert eligible(scored, apply_config(tmp_path), reader)[0] is False
    finally:
        writer.close()
        reader.close()


def test_three_roles_at_the_same_company_all_get_applied_to(tmp_path: Path,
                                                            memory_tracker):
    """A deliberate, documented limitation (docs/EVALUATION.md §9.5: "the
    tracker sees jobs, not employers"). Acme opening three backend
    requisitions on the same Monday produces three separate applications from
    the same person on the same day, which reads worse to a recruiter than one
    — but nothing in this leg is supposed to prevent it, so this pins the
    behaviour rather than complaining about it."""
    jobs = [with_pdf(tmp_path, make_scored(job=greenhouse_job(
                ats_job_id=str(4012345 + i), title=title)), slug=str(i))
            for i, title in enumerate(["Backend Engineer", "Senior Backend Engineer",
                                       "Platform Engineer"])]
    run(jobs, apply_config(tmp_path, dry_run=False), tracker=memory_tracker,
        browser=FakeBrowser())
    assert [j.status for j in jobs] == [ApplyStatus.APPLIED] * 3


def test_an_applied_row_cannot_be_downgraded_by_a_later_failed_attempt(
        tmp_path: Path, memory_tracker):
    """A second run that reaches `apply_one` for an already-applied job — a
    stale ScoredJob list, a `--limit` rerun — must not overwrite `applied`
    with `apply_failed`, because `apply_failed` does not block and the next
    run would then submit a duplicate."""
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(scored.job, now=NOW)
    memory_tracker.record_status(scored.job.key, ApplyStatus.APPLIED, now=NOW)

    dead = FakePage(simple_form(), goto_error=TimeoutError("navigation timeout"))
    apply_one(scored, apply_config(tmp_path, dry_run=False), page=dead,
              tracker=memory_tracker, now=NOW + timedelta(days=1))
    assert memory_tracker.get_status(scored.job.key) == ApplyStatus.APPLIED.value


# ==========================================================================
# 5. PARTIAL FAILURE DURING A REAL SUBMISSION
# ==========================================================================


def test_a_crash_between_the_submit_click_and_the_confirmation_is_a_failure(
        tmp_path: Path, memory_tracker):
    """The submit click landed — the POST left the machine — and then Chromium
    died before the confirmation could be read. The code deliberately calls
    this `apply_failed` rather than `applied`, because recording a false
    `applied` is terminal and would permanently block the real application
    (see `test_a_live_run_without_a_confirmation_is_a_failure_not_a_success`).
    This pins the deliberate half."""
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(scored.job, now=NOW)
    page = DyingPage(simple_form(), confirmation=None)

    outcome = apply_one(scored, apply_config(tmp_path, dry_run=False), page=page,
                        tracker=memory_tracker, now=NOW)
    assert page.submitted is True
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert memory_tracker.get_status(scored.job.key) == ApplyStatus.APPLY_FAILED.value


@pytest.mark.xfail(
    strict=True,
    reason="a submit click that could not be confirmed leaves nothing to stop "
           "tomorrow's run clicking submit again, so the employer receives two "
           "applications",
)
def test_an_unconfirmed_submission_does_not_license_a_second_one_tomorrow(
        tmp_path: Path, memory_tracker):
    """The other half of the case above, and the part that is not deliberate.
    Nothing is written *before* the click, so after a crash the tracker's only
    record is `apply_failed` — which by design does not block. Tomorrow's run
    finds the job eligible and submits again, and the employer sees two
    applications from the same person."""
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(scored.job, now=NOW)
    apply_one(scored, apply_config(tmp_path, dry_run=False),
              page=DyingPage(simple_form(), confirmation=None),
              tracker=memory_tracker, now=NOW)

    tomorrow = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    ok, _ = eligible(tomorrow, apply_config(tmp_path), memory_tracker)
    assert ok is False


@pytest.mark.xfail(
    strict=True,
    reason="the confirmation check is a substring search over the whole page, "
           "so a posting whose own text says 'thank you for your interest' is "
           "recorded APPLIED after a submission that failed validation",
)
def test_the_postings_own_text_cannot_fake_a_confirmation(tmp_path: Path,
                                                          memory_tracker):
    """"Thank you for your interest in Acme" is boilerplate at the bottom of
    thousands of job descriptions, and the description is still on the page
    after a failed submit. The fallback confirmation check searches
    `page.content()` for "thank you", finds it in the posting, and records a
    terminal `applied` for an application the ATS rejected — after which the
    user can never file the real one through this tool."""
    page = FakePage(
        simple_form(),
        html="<h1>Backend Engineer</h1>"
             "<p>Thank you for your interest in Acme.</p>"
             "<div class='error'>Please correct the errors below.</div>",
        confirmation=None,
    )
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(scored.job, now=NOW)
    outcome = apply_one(scored, apply_config(tmp_path, dry_run=False), page=page,
                        tracker=memory_tracker, now=NOW)
    assert outcome.status is not ApplyStatus.APPLIED


def test_an_error_banner_alone_is_never_read_as_a_confirmation(tmp_path: Path):
    """The neighbouring case that must stay quiet: a rejected submission whose
    page says only "Please correct the errors below" is a failure, and the
    user is told to check it by hand."""
    page = FakePage(simple_form(),
                    html="<div class='error'>Please correct the errors below.</div>",
                    confirmation=None)
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=page)
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert "check manually" in outcome.detail


def test_a_page_that_navigates_away_mid_fill_never_reaches_submit(tmp_path: Path):
    """Greenhouse re-renders the form when the resume upload finishes, and
    Playwright answers with "Execution context was destroyed". Half a form
    must never be submitted, so the fill error has to abort the attempt."""
    page = FakePage(simple_form(), html="Thank you for applying",
                    fill_error=RuntimeError(
                        "Execution context was destroyed, most likely because "
                        "of a navigation"))
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=page)
    assert outcome.status is ApplyStatus.APPLY_FAILED
    assert page.submitted is False
    assert page.uploaded == {}


@pytest.mark.xfail(
    strict=True,
    reason="a tracker write that fails after a successful submit is swallowed, "
           "so the application is invisible to the next run and is sent twice",
)
def test_a_tracker_write_failure_after_a_real_submission_is_not_lost(
        tmp_path: Path, memory_tracker):
    """The application really went out; the status write then hit
    `database or disk is full`. The failure is logged and dropped, the outcome
    still says APPLIED, and nothing durable records it — so the next run
    submits a second one. A write-ahead record before the click, or a retry
    here, is what would make this recoverable."""
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    memory_tracker.record_job(scored.job, now=NOW)
    tracker = DiskFullTracker(memory_tracker)

    outcome = apply_one(scored, apply_config(tmp_path, dry_run=False),
                        page=FakePage(simple_form(), html="Thank you for applying"),
                        tracker=tracker, now=NOW)
    assert outcome.status is ApplyStatus.APPLIED
    assert tracker.attempted, "the write was attempted"
    assert memory_tracker.has_applied(scored.job.key) is True


def test_a_failed_screenshot_does_not_downgrade_a_real_application(tmp_path: Path):
    """Headless Chromium fails screenshots on a page taller than the surface
    limit. The screenshot is diagnostic; the application already went out, and
    calling it `apply_failed` would invite a duplicate."""
    class NoScreenshots(FakePage):
        def screenshot(self, path: Any = None, **kwargs: Any) -> bytes:
            raise RuntimeError("Unable to capture screenshot")

    page = NoScreenshots(simple_form(), html="Thank you for applying")
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=page)
    assert outcome.status is ApplyStatus.APPLIED
    assert outcome.screenshot is None


def test_a_dry_run_whose_screenshot_fails_still_reports_a_dry_run(tmp_path: Path):
    """In a dry run the screenshot *is* the deliverable, so this is the case
    where losing it costs something: the digest shows a dry-run card with no
    evidence to click through to. It is still not a failure — the user can
    open the form themselves."""
    class NoScreenshots(FakePage):
        def screenshot(self, path: Any = None, **kwargs: Any) -> bytes:
            raise RuntimeError("Unable to capture screenshot")

    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=True),
                        page=NoScreenshots(simple_form()))
    assert outcome.status is ApplyStatus.DRY_RUN
    assert outcome.screenshot is None


def test_a_posting_closed_between_the_fetch_and_the_apply_goes_to_the_digest(
        tmp_path: Path):
    """Eight hours pass between the morning fetch and the apply stage. By then
    the req can be filled, and Greenhouse serves a "no longer accepting
    applications" page with no form on it. That is a hand-off, not a crash."""
    outcome = apply_one(with_pdf(tmp_path), apply_config(tmp_path, dry_run=False),
                        page=FakePage([], html="This job is no longer accepting "
                                               "applications."))
    assert outcome.status is ApplyStatus.DIGEST
    assert "resume" in outcome.detail.lower()


def test_a_tracker_that_has_never_seen_the_job_still_records_the_outcome(
        tmp_path: Path, memory_tracker):
    """`applications.key` is a foreign key onto `jobs.key`, so a job the
    pipeline never recorded (a `--limit` rerun, a hand-assembled list) would
    fail the status write. `_record` upserts the posting first, and if it did
    not, a real application would leave no trace at all."""
    scored = with_pdf(tmp_path, make_scored(job=greenhouse_job()))
    assert memory_tracker.has_job(scored.job.key) is False

    apply_one(scored, apply_config(tmp_path, dry_run=False),
              page=FakePage(simple_form(), html="Thank you for applying"),
              tracker=memory_tracker, now=NOW)
    assert memory_tracker.has_applied(scored.job.key) is True


def test_a_page_that_explodes_on_close_does_not_cost_the_rest_of_the_run(
        tmp_path: Path, memory_tracker):
    """One crashed tab must not take the other applications with it."""
    class UncloseablePage(FakePage):
        def close(self) -> None:
            raise RuntimeError("Target closed")

    browser = FakeBrowser([UncloseablePage(simple_form()),
                           FakePage(simple_form())])
    jobs = [with_pdf(tmp_path, make_scored(job=greenhouse_job(ats_job_id=str(i))),
                     slug=str(i)) for i in range(2)]
    run(jobs, apply_config(tmp_path), tracker=memory_tracker, browser=browser)
    assert [j.status for j in jobs] == [ApplyStatus.DRY_RUN, ApplyStatus.DRY_RUN]


# ==========================================================================
# 6. THE PDF HOOK
# ==========================================================================


@pytest.mark.xfail(
    strict=True,
    reason="render_if_available only checks the file is non-empty, so an HTML "
           "error page written to cv.pdf is uploaded to an employer as the "
           "user's CV",
)
def test_a_hook_that_writes_an_html_error_page_is_not_accepted_as_a_pdf(
        tmp_path: Path):
    """A half-configured hook that shells out to a converter, or a wkhtmltopdf
    wrapper that fails, leaves an HTML error page at `cv.pdf`. It is non-empty,
    so it passes, and auto-apply attaches it. Checking the four magic bytes
    `%PDF` would cost nothing and would catch it."""
    out = tmp_path / "cv.pdf"
    hook = Hook(payload=b"<html><body>Conversion failed: no such binary</body></html>")
    assert render_if_available("# Ada Lovelace\n\nSenior engineer.", out,
                               module=hook) is None


def test_a_truncated_but_genuinely_pdf_headed_file_is_accepted(tmp_path: Path):
    """The neighbouring case, pinned rather than complained about: a PDF that
    ReportLab started and never finished still begins with `%PDF-1.4`, so no
    cheap check at this layer could reject it. Structural validation is a
    different (and much heavier) job than the one this function claims."""
    out = tmp_path / "cv.pdf"
    hook = Hook(payload=b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog")
    assert render_if_available("# Ada Lovelace\n", out, module=hook) == str(out)


def test_a_hook_that_writes_to_a_path_of_its_own_choosing_returns_none(
        tmp_path: Path):
    """People copy the example hook and hard-code their own output path. The
    result is a PDF on disk somewhere and nothing at `out_path` — and
    returning `out_path` anyway would make `eligible()` believe a CV is
    attached to every application."""
    out = tmp_path / "cv.pdf"
    assert render_if_available("# Ada\n", out, module=Hook("elsewhere")) is None
    assert (tmp_path / "actually-here.pdf").exists()


def test_a_hook_that_creates_a_directory_is_caught_by_the_second_guard(
        tmp_path: Path):
    """`mkdir(out_path)` instead of `write(out_path)` is a one-character bug in
    a user's hook. `render_if_available` is fooled — a directory has a non-zero
    size — but `eligible()` asks for `is_file()`, so the application is refused
    rather than submitted with a directory attached. Defence in depth, and the
    reason the PDF is checked twice."""
    out = tmp_path / "cv.pdf"
    assert render_if_available("# Ada\n", out, module=Hook("directory")) == str(out)
    assert out.is_file() is False

    scored = make_scored(score=95, job=greenhouse_job())
    scored.artifacts.cv_pdf = str(out)
    ok, reason = eligible(scored, apply_config(tmp_path))
    assert ok is False
    assert "missing on disk" in reason


def test_an_oversized_pdf_is_returned_without_complaint(tmp_path: Path):
    """Greenhouse and Lever both cap resume uploads at a few megabytes. A hook
    that embeds fonts and a photo can blow past that, and nothing here checks:
    the upload then fails inside the browser and the run reports
    `apply_failed`. Pinned so the absence of a size guard is a known fact
    rather than a surprise."""
    out = tmp_path / "cv.pdf"
    hook = Hook(payload=b"%PDF-1.4\n" + b"\0" * (6 * 1024 * 1024))
    assert render_if_available("# Ada\n", out, module=hook) == str(out)
    assert out.stat().st_size > 5 * 1024 * 1024


def test_a_slow_hook_blocks_the_run_for_as_long_as_it_takes(tmp_path: Path):
    """There is no timeout around the user's hook. A LaTeX round-trip that
    takes a minute per CV turns a ten-match day into a ten-minute stall, and
    a hook that hangs hangs the whole run. Pinned with a small delay so the
    property is stated rather than assumed."""
    started = time.perf_counter()
    render_if_available("# Ada\n", tmp_path / "cv.pdf", module=Hook(delay=0.05))
    assert time.perf_counter() - started >= 0.05


def test_the_hook_receives_non_ascii_cv_text_unmangled(tmp_path: Path):
    """A European CV is full of ü, ß, é, ø and —. If anything normalised the
    markdown on the way to the hook, the user would post a CV with their own
    name misspelt to every employer."""
    markdown = ("# Ada Lovelace\n\n## Erfahrung\n"
                "Zürich · Malmö · Kraków — Straße 1, Düsseldorf\n"
                "Ingénieure logicielle, préparé für Großprojekte.\n")
    hook = Hook()
    render_if_available(markdown, tmp_path / "cv.pdf", module=hook)
    assert hook.calls[0][0] == markdown


def test_no_pdf_hook_means_every_match_goes_to_the_digest(tmp_path: Path,
                                                          memory_tracker):
    """The whole documented chain in one test: no `src/render_pdf.py` means
    `render_if_available` returns None, which means `artifacts.cv_pdf` is
    never set, which means `eligible()` refuses, which means the browser is
    never even opened. This is the default state of a fresh checkout and it
    has to stay boring."""
    scored = make_scored(score=95, job=greenhouse_job())
    scored.artifacts.cv_pdf = render_if_available(
        "# Ada Lovelace\n", tmp_path / "out" / "cv.pdf", module=None)
    assert scored.artifacts.cv_pdf is None

    browser = FakeBrowser()
    run([scored], apply_config(tmp_path), tracker=memory_tracker, browser=browser)
    assert scored.status is ApplyStatus.DIGEST
    assert "render_pdf.py" in scored.status_detail
    assert browser.created == []


# ==========================================================================
# 7. eligible() ORDERING AND CONFIG
# ==========================================================================


def test_an_apply_floor_under_the_scoring_threshold_is_caught_by_status_not_by_gate(
        tmp_path: Path, memory_tracker):
    """The footgun: `apply.min_score: 50` under `scoring.threshold: 65` reads
    like "auto-apply to jobs you were never shown". `eligible()` on its own
    would indeed say yes — the two numbers are unrelated to it. What actually
    saves the user is the status: scoring stamps those jobs `scored_below` and
    `run` skips settled statuses without ever consulting `eligible`."""
    config = write_config(tmp_path, {
        "scoring": {"threshold": 65},
        "apply": {"enabled": True, "dry_run": True, "min_score": 50,
                  "require_pdf": False, "max_per_run": 5},
        "output": {"dir": str(tmp_path / "output")},
    })
    below = make_scored(score=55, job=greenhouse_job(), status=ApplyStatus.SCORED_BELOW)
    assert eligible(below, config, memory_tracker)[0] is True

    browser = FakeBrowser()
    run([below], config, tracker=memory_tracker, browser=browser)
    assert below.status is ApplyStatus.SCORED_BELOW
    assert browser.created == []


@pytest.mark.parametrize("cap", [0, -1])
def test_max_per_run_of_zero_or_less_applies_to_nothing(tmp_path: Path,
                                                        memory_tracker, cap):
    """"Apply to at most zero jobs" has to mean zero, not "unlimited". Someone
    turning auto-apply off for a week by zeroing the cap must not discover
    they turned it up instead."""
    jobs = [with_pdf(tmp_path, make_scored(job=greenhouse_job(ats_job_id=str(i))),
                     slug=str(i)) for i in range(3)]
    browser = FakeBrowser()
    run(jobs, apply_config(tmp_path, max_per_run=cap), tracker=memory_tracker,
        browser=browser)
    assert browser.created == []
    assert all(j.status is ApplyStatus.DIGEST for j in jobs)
    assert all("max_per_run" in j.status_detail for j in jobs)


def test_a_form_that_bails_still_consumes_a_max_per_run_slot(tmp_path: Path,
                                                             memory_tracker):
    """`max_per_run` counts pages opened, not applications sent, so a day
    where the first two forms turn out to be screeners uses up a cap of two
    and the third job waits for tomorrow. That is the conservative direction
    for a safety cap, and worth knowing when tuning the number."""
    screener = FakePage(form_with(FakeElement("select", name="country",
                                              label="Country")))
    browser = FakeBrowser([screener, FakePage(simple_form())])
    jobs = [with_pdf(tmp_path, make_scored(job=greenhouse_job(ats_job_id=str(i))),
                     slug=str(i)) for i in range(3)]

    run(jobs, apply_config(tmp_path, max_per_run=2), tracker=memory_tracker,
        browser=browser)
    assert jobs[0].status is ApplyStatus.DIGEST and "dropdown" in jobs[0].status_detail
    assert jobs[1].status is ApplyStatus.DRY_RUN
    assert jobs[2].status is ApplyStatus.DIGEST
    assert "max_per_run" in jobs[2].status_detail
    assert len(browser.created) == 2


def test_enabled_but_universally_ineligible_never_starts_a_browser(tmp_path: Path,
                                                                   memory_tracker):
    """Launching Chromium costs a second and leaves a process behind. On the
    common day where every match is on Workday or under the score floor, the
    stage must be a no-op."""
    jobs = [
        with_pdf(tmp_path, make_scored(score=95, job=make_job(
            url="https://acme.wd3.myworkdayjobs.com/en-US/careers/job/1")), slug="w"),
        with_pdf(tmp_path, make_scored(score=40, job=greenhouse_job()), slug="l"),
    ]
    browser = FakeBrowser()
    run(jobs, apply_config(tmp_path), tracker=memory_tracker, browser=browser)
    assert browser.created == []
    assert all(j.status is ApplyStatus.DIGEST for j in jobs)


def test_dry_run_is_the_default_when_the_config_forgets_to_say(tmp_path: Path):
    """Someone hand-writing an `apply:` block and omitting `dry_run` must get
    the safe half of the switch. The default has to be dry, and it has to come
    from the config defaults rather than from the caller remembering."""
    config = write_config(tmp_path, {
        "apply": {"enabled": True, "min_score": 80, "require_pdf": True},
        "output": {"dir": str(tmp_path / "output")},
    })
    page = FakePage(simple_form(), html="Thank you for applying")
    outcome = apply_one(with_pdf(tmp_path), config, page=page)
    assert outcome.status is ApplyStatus.DRY_RUN
    assert page.submitted is False


def test_a_config_with_no_apply_section_at_all_is_still_a_dry_run(tmp_path: Path):
    """The shipped `config.example.yaml` can be trimmed to nothing by a user
    who only wants the digest. Missing keys must not read as "false"."""
    config = write_config(tmp_path, {"output": {"dir": str(tmp_path / "output")}})
    page = FakePage(simple_form(), html="Thank you for applying")
    assert apply_one(with_pdf(tmp_path), config, page=page).status is ApplyStatus.DRY_RUN
    assert page.submitted is False


@pytest.mark.xfail(
    strict=True,
    reason="eligible() reads score.value and never score.error, so a job the "
           "scorer failed on is auto-applied whenever apply.min_score is 0",
)
def test_a_job_the_scorer_could_not_judge_is_never_auto_applied(tmp_path: Path):
    """When the LLM call fails, scoring emits `Score(value=0, error=...)` and
    routes the job to the digest precisely because *a human* has to judge it.
    Auto-apply only ever compares the number, so `apply.min_score: 0` — the
    obvious way to say "apply to everything you can" — turns an API outage
    into applications the model never assessed."""
    scored = with_pdf(tmp_path, make_scored(
        score=0, error="anthropic overloaded", job=greenhouse_job()))
    ok, _ = eligible(scored, apply_config(tmp_path, min_score=0))
    assert ok is False


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.jobs.personio.de/job/1234567",
        "https://jobs.smartrecruiters.com/Acme/743999",
        "https://acme.recruitee.com/o/backend-engineer",
        "https://acme.softgarden.io/job/12345",
        "https://join.com/companies/acme/1234-backend-engineer",
        "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Berlin/Engineer_R-1",
    ],
)
def test_the_other_ats_platforms_a_european_search_hits_are_refused(url):
    """Personio, SmartRecruiters, Recruitee, Softgarden, join.com and Workday
    are most of the German and Dutch market. None of them is Greenhouse or
    Lever, so all of them are hand-offs — and the reason they are refused is
    the hostname, not a guess at the page."""
    assert detect_ats(url) is None


@pytest.mark.parametrize(
    "url",
    ["https://jobs.eu.lever.co/acme/9f2b1c4e",
     "https://hire.lever.co/acme/9f2b1c4e",
     "https://www.lever.co/customers"],
)
def test_lever_hosts_outside_jobs_lever_co_are_refused(url):
    """Only `jobs.lever.co` (and company sub-hosts of it) serve applicant-facing
    forms, so anything else is a hand-off. That is the safe direction, and it
    is worth pinning what it costs: a board served from a regional Lever host
    is never auto-applied to, it only ever reaches the digest."""
    assert detect_ats(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.eu.greenhouse.io/acme/jobs/4012345",
        "https://job-boards.eu.greenhouse.io/acme/jobs/4012345",
        "https://BOARDS.Greenhouse.IO/acme/jobs/4012345",
        "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=linkedin&utm=x",
        "https://boards.greenhouse.io/acme/jobs/4012345#app",
        "https://job-boards.greenhouse.io/acme/jobs/4012345/",
    ],
)
def test_greenhouse_survives_eu_residency_hosts_and_url_noise(url):
    """The same posting reaches the pipeline with tracking parameters from a
    LinkedIn alert, with a fragment from a share link, and from Greenhouse's
    EU data-residency hosts. Losing any of these to a fussy matcher would
    silently move real jobs into the digest."""
    assert detect_ats(url) == "greenhouse"


def test_the_tracker_is_consulted_last_so_the_actionable_reason_wins(
        tmp_path: Path, memory_tracker):
    """A job that is both already applied to *and* on an unsupported ATS shows
    the ATS reason, because the digest prints this string verbatim and "apply
    by hand at <url>" is the sentence the user can act on."""
    scored = with_pdf(tmp_path, make_scored(job=make_job(
        company="Acme", title="Backend Engineer", ats="workday",
        ats_job_id="R-1", url="https://acme.wd3.myworkdayjobs.com/job/1")))
    memory_tracker.record_job(scored.job, now=NOW)
    memory_tracker.record_status(scored.job.key, ApplyStatus.APPLIED, now=NOW)

    ok, reason = eligible(scored, apply_config(tmp_path), memory_tracker)
    assert ok is False
    assert "Greenhouse or Lever" in reason
    assert "already applied" not in reason
