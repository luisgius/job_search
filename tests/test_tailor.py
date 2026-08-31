"""Tests for src/tailor.py — generating the CV and cover letter that go out
under the user's name.

This is one of the three suites that exist for safety rather than
correctness. The claim being defended is **"it can't invent things about
you"**, and prompt-level enforcement is not a guarantee — a model can ignore
any instruction. So both layers are tested:

  1. the anti-fabrication clauses are present, specific, and enumerate the
     categories models actually invent (employers, dates, degrees, tools,
     metrics) — a vague "be accurate" is exactly what gets rounded off;
  2. `validate_tailored_cv` catches the mechanically detectable failures
     afterwards, and a rejected CV falls back to the base CV, which is the
     document the user wrote about themselves and is therefore always safe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models import ApplyStatus, Artifacts
from src.tailor import (
    ANTI_FABRICATION,
    COVER_LETTER_MAX_WORDS,
    COVER_SYSTEM_PROMPT,
    CV_SYSTEM_PROMPT,
    MAX_LENGTH_RATIO,
    artifact_dir,
    build_cover_prompt,
    build_cv_prompt,
    tailor_job,
    tailor_jobs,
    validate_tailored_cv,
)
from tests.conftest import (
    BASE_CV,
    FakeAnthropic,
    TransientAPIError,
    llm_client,
    make_job,
    make_scored,
    write_config,
)

TAILORED_CV = """# Ada Lovelace

## Summary
Senior backend engineer, 8 years, Python and distributed data systems.

## Experience
### Senior Backend Engineer — Northwind
- Cut p99 checkout latency 840ms to 210ms by batching Redis reads.
"""

COVER = """Dear Acme hiring team,

I cut p99 checkout latency from 840ms to 210ms at Northwind by batching Redis
reads, and led a 40-service migration from ECS to EKS with zero downtime.

Ada Lovelace
"""


def tailor_config(tmp_path: Path, **overrides):
    base = {"output": {"dir": str(tmp_path / "output")},
            "tailoring": {"enabled": True, "max_per_run": 10},
            "scoring": {"threshold": 65}}
    for key, value in (overrides or {}).items():
        base.setdefault(key, {})
        base[key].update(value) if isinstance(value, dict) else base.update({key: value})
    return write_config(tmp_path, base)


# ==========================================================================
# the anti-fabrication clause
# ==========================================================================


def test_the_clause_enumerates_what_models_actually_invent():
    """A generic "be accurate" instruction is the kind a model rounds off.
    The specific categories are what make it stick."""
    lowered = ANTI_FABRICATION.lower()
    for category in ("employers", "job titles", "dates", "degrees",
                     "certifications", "technologies", "metrics"):
        assert category in lowered, category


def test_the_clause_says_omit_rather_than_approximate():
    # Whitespace-normalised: the clause is hard-wrapped, so a phrase can
    # straddle a line break.
    flat = " ".join(ANTI_FABRICATION.lower().split())
    assert "omit" in flat
    assert "do not approximate" in flat
    assert "imply" in flat


def test_the_clause_states_the_asymmetry_of_the_two_mistakes():
    """Models weigh instructions by stated consequence; "an omission costs one
    application, a fabrication costs the offer" is doing real work here."""
    assert "reputation" in ANTI_FABRICATION.lower()


@pytest.mark.parametrize("builder", [build_cv_prompt, build_cover_prompt])
def test_both_prompts_carry_the_clause(builder):
    prompt = builder(make_job(), BASE_CV, {"name": "Ada Lovelace"})
    assert ANTI_FABRICATION in prompt


@pytest.mark.parametrize("system", [CV_SYSTEM_PROMPT, COVER_SYSTEM_PROMPT])
def test_the_system_prompts_deny_outside_knowledge(system):
    """The failure mode this guards: the model filling gaps from what it
    "knows" about a well-known company or a common career path."""
    lowered = system.lower()
    assert "markdown only" in lowered
    assert ("no information about this person beyond" in lowered
            or "strictly from the candidate" in lowered)


# ==========================================================================
# prompts
# ==========================================================================


def test_cv_prompt_contains_the_base_cv_and_the_posting():
    prompt = build_cv_prompt(make_job(company="Northwind"), BASE_CV, {})
    assert BASE_CV in prompt
    assert "Northwind" in prompt


def test_cv_prompt_forbids_code_fences_and_commentary():
    prompt = build_cv_prompt(make_job(), BASE_CV, {})
    assert "no commentary" in prompt.lower()
    assert "code fences" in prompt.lower()


def test_cover_prompt_sets_a_word_limit_and_a_shape():
    prompt = build_cover_prompt(make_job(), BASE_CV, {})
    assert str(COVER_LETTER_MAX_WORDS) in prompt
    assert "paragraph" in prompt.lower()


def test_cover_prompt_bans_the_boilerplate_opener():
    """"I am writing to apply for the position of..." is the single clearest
    signal to a reader that nobody spent any time on this."""
    assert "i am writing to apply" in build_cover_prompt(make_job(), BASE_CV, {}).lower()


def test_cover_prompt_requires_achievements_from_the_cv():
    lowered = build_cover_prompt(make_job(), BASE_CV, {}).lower()
    assert "achievement" in lowered
    assert "from the cv" in lowered or "in the cv" in lowered


def test_cover_prompt_gates_work_authorisation_on_the_cv_saying_so():
    lowered = build_cover_prompt(make_job(), BASE_CV, {}).lower()
    assert "authorisation" in lowered or "authorization" in lowered


# ==========================================================================
# validate_tailored_cv
# ==========================================================================


def test_a_good_tailored_cv_passes():
    ok, reason = validate_tailored_cv(BASE_CV, TAILORED_CV, {"name": "Ada Lovelace"})
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize("tailored", ["", "   ", "\n\n", None])
def test_an_empty_tailored_cv_is_rejected(tailored):
    ok, reason = validate_tailored_cv(BASE_CV, tailored, {"name": "Ada Lovelace"})
    assert ok is False
    assert "empty" in reason


def test_a_cv_that_lost_the_applicants_name_is_rejected():
    """Submitting a nameless CV is worse than submitting the base one."""
    stripped = TAILORED_CV.replace("Ada Lovelace", "Candidate")
    ok, reason = validate_tailored_cv(BASE_CV, stripped, {"name": "Ada Lovelace"})
    assert ok is False
    assert "name" in reason


def test_the_name_check_only_enforces_what_the_base_cv_established():
    """If the user's own CV never spells their name, its absence downstream is
    not the model's doing and must not block the run."""
    anonymous_base = BASE_CV.replace("Ada Lovelace", "")
    ok, _ = validate_tailored_cv(anonymous_base, "# CV\nSome content",
                                 {"name": "Ada Lovelace"})
    assert ok is True


def test_a_cv_that_grew_far_beyond_the_base_is_rejected():
    """Growth means content was *written*, not re-emphasised — which is the
    mechanical signature of fabrication."""
    bloated = TAILORED_CV + ("\n- Invented an entirely new job here." * 400)
    ok, reason = validate_tailored_cv(BASE_CV, bloated, {"name": "Ada Lovelace"})
    assert ok is False
    assert "invented content" in reason
    assert str(MAX_LENGTH_RATIO) in reason or f"{MAX_LENGTH_RATIO:g}" in reason


def test_modest_growth_is_allowed():
    grown = BASE_CV + "\n" + BASE_CV[: int(len(BASE_CV) * 0.5)]
    assert validate_tailored_cv(BASE_CV, grown, {"name": "Ada Lovelace"})[0] is True


def test_validation_tolerates_a_missing_applicant_block():
    assert validate_tailored_cv(BASE_CV, TAILORED_CV, None)[0] is True
    assert validate_tailored_cv(BASE_CV, TAILORED_CV, {})[0] is True


# ==========================================================================
# artifact_dir
# ==========================================================================


def test_artifact_dir_is_readable_and_unique(tmp_path: Path):
    job = make_job(company="Northwind GmbH", title="Senior Backend Engineer")
    directory = artifact_dir(job, tmp_path)
    assert directory.is_dir()
    assert "northwind" in directory.name
    assert job.key[:8] in directory.name
    assert directory.parent.name == "applications"


def test_artifact_dirs_differ_between_jobs(tmp_path: Path):
    a = artifact_dir(make_job(ats_job_id="1"), tmp_path)
    b = artifact_dir(make_job(ats_job_id="2"), tmp_path)
    assert a != b


def test_artifact_dir_is_stable_for_the_same_job(tmp_path: Path):
    """Re-running must overwrite yesterday's documents, not accumulate a new
    directory per run."""
    assert artifact_dir(make_job(), tmp_path) == artifact_dir(make_job(), tmp_path)


def test_artifact_dir_survives_a_hostile_title(tmp_path: Path):
    job = make_job(company="../../etc", title="pass/../../wd Engineer")
    directory = artifact_dir(job, tmp_path)
    assert tmp_path.resolve() in directory.resolve().parents
    assert ".." not in directory.name


# ==========================================================================
# tailor_job
# ==========================================================================


def test_tailor_job_writes_both_documents(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    scored = make_scored()
    tailor_job(scored, BASE_CV, cfg, client=llm_client([TAILORED_CV, COVER]))

    # `_strip_fences` also trims surrounding whitespace, hence the .strip().
    assert Path(scored.artifacts.cv_md).read_text(encoding="utf-8") == TAILORED_CV.strip()
    assert Path(scored.artifacts.cover_md).read_text(encoding="utf-8") == COVER.strip()
    assert scored.tailored_cv_md == TAILORED_CV.strip()
    assert scored.cover_letter_md == COVER.strip()


def test_tailor_job_writes_a_self_describing_job_json(tmp_path: Path):
    """Opening an artifact directory three weeks later should tell you what
    the job was and why it scored what it did."""
    cfg = tailor_config(tmp_path)
    scored = make_scored(score=88)
    tailor_job(scored, BASE_CV, cfg, client=llm_client([TAILORED_CV, COVER]))

    payload = json.loads((Path(scored.artifacts.dir) / "job.json").read_text())
    assert payload["title"] == scored.job.title
    assert payload["url"] == scored.job.url
    assert payload["score"]["value"] == 88


def test_tailor_job_uses_the_configured_model_and_two_calls(tmp_path: Path):
    from src.llm import LLMClient

    cfg = tailor_config(tmp_path, tailoring={"model": "test-model", "max_tokens": 321,
                                             "temperature": 0.5})
    fake = FakeAnthropic([TAILORED_CV, COVER])
    tailor_job(make_scored(), BASE_CV, cfg, client=LLMClient("k", client=fake))
    assert len(fake.calls) == 2
    assert {c["model"] for c in fake.calls} == {"test-model"}
    assert {c["max_tokens"] for c in fake.calls} == {321}


def test_tailor_job_strips_stray_code_fences(tmp_path: Path):
    """Models wrap markdown in ``` far too often; a fenced CV renders as a
    code block in the PDF and looks broken."""
    cfg = tailor_config(tmp_path)
    scored = make_scored()
    tailor_job(scored, BASE_CV, cfg,
               client=llm_client([f"```markdown\n{TAILORED_CV}\n```", COVER]))
    assert not scored.tailored_cv_md.startswith("```")
    assert "Ada Lovelace" in scored.tailored_cv_md


def test_a_rejected_tailored_cv_falls_back_to_the_base_cv(tmp_path: Path):
    """The safe fallback: the base CV is the document the user wrote about
    themselves, so it can never be a fabrication."""
    cfg = tailor_config(tmp_path)
    scored = make_scored()
    bloated = TAILORED_CV + ("\n- fabricated line" * 500)
    tailor_job(scored, BASE_CV, cfg, client=llm_client([bloated, COVER]))

    assert scored.tailored_cv_md == BASE_CV
    assert "rejected" in scored.status_detail
    assert Path(scored.artifacts.cv_md).read_text(encoding="utf-8") == BASE_CV


def test_a_model_failure_leaves_the_job_without_documents_but_in_the_digest(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    scored = make_scored()
    tailor_job(scored, BASE_CV, cfg, client=llm_client([TransientAPIError()] * 5))

    assert scored.artifacts.cv_md is None
    assert scored.tailored_cv_md is None
    assert "tailoring failed" in scored.status_detail
    assert scored.status is ApplyStatus.DIGEST


def test_tailor_job_survives_a_broken_client(tmp_path: Path):
    class Broken:
        def complete(self, **kwargs):
            raise RuntimeError("unexpected")

    scored = make_scored()
    tailor_job(scored, BASE_CV, tailor_config(tmp_path), client=Broken())
    assert "tailoring failed" in scored.status_detail


def test_an_empty_cover_letter_is_reported_not_written_as_garbage(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    scored = make_scored()
    tailor_job(scored, BASE_CV, cfg, client=llm_client([TAILORED_CV, "   "]))
    assert scored.cover_letter_md == ""
    assert "cover letter" in scored.status_detail


def test_tailor_job_reports_an_unwritable_output_directory(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    scored = make_scored()
    tailor_job(scored, BASE_CV, cfg, client=llm_client([TAILORED_CV, COVER]),
               out_dir=blocker)
    assert "tailoring failed" in scored.status_detail


# ==========================================================================
# tailor_jobs
# ==========================================================================


def test_tailor_jobs_only_tailors_matches(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    match = make_scored(score=88, ats_job_id="1")
    below = make_scored(score=40, ats_job_id="2")
    below.status = ApplyStatus.SCORED_BELOW

    out = tailor_jobs([match, below], BASE_CV, cfg,
                      client=llm_client([TAILORED_CV, COVER]))
    assert len(out) == 2                       # the full list comes back
    assert match.artifacts.cv_md is not None
    assert below.artifacts.cv_md is None


def test_tailor_jobs_preserves_input_order(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    items = [make_scored(score=90 - i, ats_job_id=str(i)) for i in range(4)]
    out = tailor_jobs(items, BASE_CV, cfg, client=llm_client([TAILORED_CV, COVER]))
    assert [s.key for s in out] == [s.key for s in items]


def test_max_per_run_caps_the_expensive_stage_and_says_why(tmp_path: Path):
    cfg = tailor_config(tmp_path, tailoring={"max_per_run": 2})
    items = [make_scored(score=90, ats_job_id=str(i)) for i in range(5)]
    out = tailor_jobs(items, BASE_CV, cfg, client=llm_client([TAILORED_CV, COVER]))

    tailored = [s for s in out if s.artifacts.cv_md]
    untailored = [s for s in out if not s.artifacts.cv_md]
    assert len(tailored) == 2
    assert len(untailored) == 3
    # Still in the digest, and the digest explains the omission.
    assert all(s.status is ApplyStatus.DIGEST for s in untailored)
    assert all("max_per_run" in s.status_detail for s in untailored)


def test_tailoring_can_be_switched_off_entirely(tmp_path: Path):
    cfg = tailor_config(tmp_path, tailoring={"enabled": False})
    items = [make_scored(score=90)]
    out = tailor_jobs(items, BASE_CV, cfg, client=llm_client([TAILORED_CV, COVER]))
    assert out == items
    assert items[0].artifacts.cv_md is None


def test_tailor_jobs_on_an_empty_list(tmp_path: Path):
    assert tailor_jobs([], BASE_CV, tailor_config(tmp_path), client=llm_client()) == []


def test_tailor_jobs_never_raises(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    items = [make_scored(score=90, ats_job_id=str(i)) for i in range(3)]
    errors: list[str] = []
    out = tailor_jobs(items, BASE_CV, cfg,
                      client=llm_client([TransientAPIError()] * 20), errors=errors)
    assert len(out) == 3


# ==========================================================================
# per-role CV variants — same facts, per-job presentation
# ==========================================================================

from src.tailor import load_cv_variants, select_cv  # noqa: E402


ML_MARKER = "prototype-to-production presentation marker"
PRODUCT_MARKER = "analytics-meets-product presentation marker"


def _variant_text(marker: str) -> str:
    """A distinct, plausible variant: long enough to clear the stub floor."""
    return f"# Ada Lovelace\n\n## Summary\n{marker}\n\n" + ("real content. " * 40)


def variants_config(tmp_path: Path, entries=None, **overrides):
    (tmp_path / "cv").mkdir(exist_ok=True)
    (tmp_path / "cv" / "ml.md").write_text(_variant_text(ML_MARKER), encoding="utf-8")
    (tmp_path / "cv" / "product.md").write_text(
        _variant_text(PRODUCT_MARKER), encoding="utf-8"
    )
    cv = {"path": "cv/base_cv.md", "variants": entries if entries is not None else [
        {"path": "cv/ml.md",
         "title_terms": ["ml", "machine learning engineer", "ai"]},
        {"path": "cv/product.md",
         "title_terms": ["product", "analytics"]},
    ]}
    return tailor_config(tmp_path, cv=cv, **overrides)


class CapturingClient:
    """Duck-typed LLM client, via the public `client=` seam: records every
    prompt so a test can see which CV each job was tailored from."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, *, model, system, prompt, max_tokens, temperature=0.0):
        self.calls.append({"system": system, "prompt": prompt})
        return TAILORED_CV if system == CV_SYSTEM_PROMPT else COVER


def test_select_cv_picks_the_first_matching_variant():
    variants = [(["ml", "ai"], "ml.md", "ML-TEXT"),
                (["product", "analytics"], "product.md", "PRODUCT-TEXT")]
    md, why = select_cv(make_job(title="ML Engineer"), "BASE-TEXT", variants)
    assert md == "ML-TEXT" and "ml.md" in why
    md, why = select_cv(make_job(title="Product Analytics Lead"), "BASE-TEXT", variants)
    assert md == "PRODUCT-TEXT"
    # Both match -> config order is the priority order, like the watchlist.
    md, _ = select_cv(make_job(title="ML Product Engineer"), "BASE-TEXT", variants)
    assert md == "ML-TEXT"


def test_select_cv_defaults_to_the_base_cv():
    variants = [(["ml"], "ml.md", "ML-TEXT")]
    md, why = select_cv(make_job(title="Data Scientist"), "BASE-TEXT", variants)
    assert md == "BASE-TEXT" and why == ""


def test_select_cv_matches_whole_words_only():
    """"ml" must hit "ML Engineer" and never "HTML Developer" — the same
    contract every filters.title_* term has."""
    variants = [(["ml"], "ml.md", "ML-TEXT")]
    assert select_cv(make_job(title="HTML Developer"), "B", variants)[0] == "B"
    assert select_cv(make_job(title="ML Engineer"), "B", variants)[0] == "ML-TEXT"


def test_load_cv_variants_resolves_against_the_config_root(tmp_path: Path):
    cfg = variants_config(tmp_path)
    loaded = load_cv_variants(cfg)
    assert [label for _, label, _ in loaded] == ["ml.md", "product.md"]
    assert ML_MARKER in loaded[0][2]


def test_load_cv_variants_degrades_broken_entries(tmp_path: Path):
    """A missing file, a stub, or an entry without terms costs that variant
    only — the job tailors from the base CV, which is a worse emphasis, not a
    wrong document."""
    (tmp_path / "cv").mkdir(exist_ok=True)
    (tmp_path / "cv" / "stub.md").write_text("too short", encoding="utf-8")
    cfg = variants_config(tmp_path, entries=[
        {"path": "cv/missing.md", "title_terms": ["ml"]},
        {"path": "cv/stub.md", "title_terms": ["ml"]},
        {"path": "cv/ml.md", "title_terms": []},
        {"path": "", "title_terms": ["ml"]},
        {"path": "cv/product.md", "title_terms": ["product"]},
    ])
    loaded = load_cv_variants(cfg)
    assert [label for _, label, _ in loaded] == ["product.md"]


def test_tailor_jobs_hands_each_job_its_variant(tmp_path: Path):
    """The integration claim: the ML job's prompt carries the ML variant, the
    unmatched job's prompt carries the base CV — and the variant text is what
    the anti-fabrication ground truth becomes for that job."""
    cfg = variants_config(tmp_path)
    client = CapturingClient()
    items = [
        make_scored(score=90, title="Machine Learning Engineer", ats_job_id="a"),
        make_scored(score=88, title="Data Scientist", ats_job_id="b"),
    ]
    tailor_jobs(items, BASE_CV, cfg, client=client)

    cv_prompts = [c["prompt"] for c in client.calls if c["system"] == CV_SYSTEM_PROMPT]
    assert len(cv_prompts) == 2
    assert ML_MARKER in cv_prompts[0]
    assert BASE_CV.strip().splitlines()[0] in cv_prompts[1]
    assert ML_MARKER not in cv_prompts[1]


# ==========================================================================
# hard-number grounding — the mechanical half of "do not fabricate"
# ==========================================================================

from src.tailor import unanchored_numbers, validate_cover_letter  # noqa: E402


def test_reformatted_numbers_are_the_same_fact():
    """"10,000", "10.000" and "10k" are one number spelled three ways — the
    model may reformat, never invent, so comparison happens post-formatting."""
    base = "Handled 10,000 requests and 99.9 uptime."
    assert unanchored_numbers(base, "Handled 10k requests (99.9%).") == []
    assert unanchored_numbers("Mapped 10.000 SKUs", "Mapped 10,000 SKUs") == []


def test_word_durations_only_count_with_a_unit():
    """"three years" is a fact; a bare "one" ("one of the largest teams") is
    prose, and counting it would make this a false-positive machine."""
    assert unanchored_numbers("Worked 3 years.", "Worked three years.") == []
    assert unanchored_numbers("No numbers here.", "One of the largest teams.") == []


def test_a_duration_derivable_from_anchored_years_is_arithmetic_not_invention():
    base = "### Engineer — Northwind\n*2020 – 2023*"
    assert unanchored_numbers(base, "Three years at Northwind (2020-2023).") == []


def test_an_invented_percent_rejects_the_cv():
    ok, reason = validate_tailored_cv(BASE_CV, TAILORED_CV + "\n- Improved accuracy by 23%.")
    assert not ok
    assert "23%" in reason


def test_an_invented_year_rejects_the_cv():
    ok, reason = validate_tailored_cv(BASE_CV, TAILORED_CV.replace(
        "## Experience", "Shipping software since 2015.\n\n## Experience"))
    assert not ok
    assert "2015" in reason


def test_loose_small_numbers_are_left_to_the_prompt():
    """Only percents and years hard-stop; "8 stakeholders" (8 is anchored
    here anyway) or a rounded count is the prompt's job — a validator that
    cries wolf ends up ignored."""
    ok, reason = validate_tailored_cv(BASE_CV, TAILORED_CV + "\n- Worked with 6 teams.")
    assert ok, reason


def test_the_real_fixture_pair_still_validates():
    ok, reason = validate_tailored_cv(BASE_CV, TAILORED_CV)
    assert ok, reason


# ==========================================================================
# cover letter QA
# ==========================================================================


def test_a_cover_letter_may_quote_the_posting_but_not_invent():
    job = make_job(description="We need 2+ years of Python and 99% SLAs.")
    ok, reason, flags = validate_cover_letter(
        "Dear Acme team,\nYour 2+ years requirement fits me.\nAda",
        base_md=BASE_CV, job=job)
    assert ok, reason

    ok, reason, _ = validate_cover_letter(
        "Dear Acme team,\nI raised conversion 31% last quarter.\nAda",
        base_md=BASE_CV, job=job)
    assert not ok
    assert "31%" in reason


def test_a_placeholder_rejects_the_cover_letter():
    ok, reason, _ = validate_cover_letter(
        "Dear [Company Name] team,\nAda", base_md=BASE_CV, job=make_job())
    assert not ok
    assert "placeholder" in reason


def test_the_wrong_company_surfaces_as_a_flag_not_a_block():
    """The worst letter error is the wrong company; the detectable half is a
    letter that never names the right one. Judgement stays with the human, so
    it flags instead of blocking."""
    ok, reason, flags = validate_cover_letter(
        "Dear team,\nMy Northwind work speaks for itself.\nAda",
        base_md=BASE_CV, job=make_job(company="Acme"))
    assert ok and not reason
    assert any("never names Acme" in flag for flag in flags)


def test_an_overlong_letter_is_flagged():
    long_letter = "Dear Acme team,\n" + ("word " * 400) + "\nAda"
    ok, _, flags = validate_cover_letter(long_letter, base_md=BASE_CV, job=make_job())
    assert ok
    assert any("words" in flag for flag in flags)


def test_tailor_job_discards_a_fabricating_cover_and_says_why(tmp_path: Path):
    cfg = tailor_config(tmp_path)
    bad_cover = "Dear Acme team,\nI grew revenue 45% at Northwind.\nAda"
    item = make_scored(score=90)
    out = tailor_job(item, BASE_CV, cfg,
                     client=llm_client([TAILORED_CV, bad_cover]),
                     out_dir=tmp_path / "apps")
    assert out.cover_letter_md == ""
    assert "cover letter rejected" in out.status_detail
    assert "45%" in out.status_detail
    # The CV survives: a bad letter must not cost the good document.
    assert out.tailored_cv_md == TAILORED_CV.strip() or out.tailored_cv_md
