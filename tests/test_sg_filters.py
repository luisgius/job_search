"""Offline tests for Singapore hard filters — both keep and reject paths."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import Job
from src.singapore.filters import (
    FLAG_SALARY,
    FLAG_SECONDARY,
    FLAG_YEARS,
    apply_filters,
    filter_job,
)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

SETTINGS = {
    "applicant_age": 25,
    "ep_thresholds": [
        {
            "effective_date": "2025-01-01",
            "non_fs": 6064,
            "fs": 6709,
        },
        {
            "effective_date": "2027-01-01",
            "non_fs": 6500,
            "fs": 7155,
        },
    ],
    "search_terms": [
        "Data Scientist",
        "Junior Data Scientist",
        "Machine Learning Scientist",
        "ML Scientist",
        "Machine Learning Engineer",
        "ML Engineer",
        "AI Engineer",
        "Decision Scientist",
    ],
    "secondary_search_terms": [
        "Machine Learning Engineer",
        "ML Engineer",
        "AI Engineer",
    ],
}


def job(**kwargs) -> Job:
    base = dict(
        source="greenhouse",
        company="Acme Pte Ltd",
        title="Data Scientist",
        url="https://example.com/jobs/1",
        location="Singapore",
        description="Build models for product analytics using Python.",
        country="SG",
        source_type="company_site",
    )
    base.update(kwargs)
    return Job(**base)


# --------------------------------------------------------------------------
# location
# --------------------------------------------------------------------------


def test_singapore_location_passes():
    ok, reason = filter_job(job(location="Singapore"), SETTINGS, now=NOW)
    assert ok and reason == ""


def test_non_singapore_location_rejected():
    ok, reason = filter_job(job(location="Berlin, Germany", country="DE"), SETTINGS, now=NOW)
    assert ok is False
    assert reason == "location_not_singapore"


def test_remote_singapore_passes():
    ok, _ = filter_job(
        job(location="Remote - Singapore", country=None, remote=True),
        SETTINGS,
        now=NOW,
    )
    assert ok is True


# --------------------------------------------------------------------------
# titles
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Data Scientist",
        "Junior Data Scientist",
        "Applied Data Scientist",
        "Decision Scientist",
        "ML Scientist",
        "Machine Learning Scientist",
    ],
)
def test_ds_title_variants_pass(title):
    ok, _ = filter_job(job(title=title), SETTINGS, now=NOW)
    assert ok is True


@pytest.mark.parametrize("title", ["ML Engineer", "Machine Learning Engineer", "AI Engineer"])
def test_secondary_ml_ai_engineer_passes_with_flag(title):
    j = job(title=title)
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SECONDARY in j.flags


@pytest.mark.parametrize(
    "title",
    [
        "Senior Data Scientist",
        "Staff ML Engineer",
        "Principal Data Scientist",
        "Lead Data Scientist",
        "Data Science Manager",
        "Head of Data Science",
        "Director of AI",
        "VP Data Science",
        "AVP, Machine Learning",
        "Data Scientist Intern",
        "Internship - Data Scientist",
    ],
)
def test_seniority_and_internship_titles_rejected(title):
    ok, reason = filter_job(job(title=title), SETTINGS, now=NOW)
    assert ok is False
    assert reason in {"seniority_title", "internship_title"}


def test_unrelated_title_rejected():
    ok, reason = filter_job(job(title="Backend Engineer"), SETTINGS, now=NOW)
    assert ok is False
    assert reason == "title_not_relevant"


# --------------------------------------------------------------------------
# years of experience
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "at least 3 years of experience",
        "minimum of 5 years experience",
        "3+ years in data science",
        "three or more years of ML experience",
        "requires 4 years of experience",
        "7+ years building models",
    ],
)
def test_three_plus_years_mandatory_rejected(phrase):
    ok, reason = filter_job(
        job(description=f"We are hiring. {phrase}. Python required."),
        SETTINGS,
        now=NOW,
    )
    assert ok is False
    assert reason == "years_experience_too_high"


def test_local_equivalent_experience_exception_passes():
    text = (
        "at least 5 years of experience or equivalent local experience "
        "delivering production ML systems."
    )
    ok, reason = filter_job(job(description=text), SETTINGS, now=NOW)
    assert ok is True
    assert reason == ""


def test_degree_equivalence_cannot_cancel_years():
    text = "minimum of 4 years of experience or equivalent degree in Computer Science."
    ok, reason = filter_job(job(description=text), SETTINGS, now=NOW)
    assert ok is False
    assert reason == "years_experience_too_high"


def test_two_to_three_years_passes_with_flag():
    j = job(description="Looking for 2-3 years of experience in analytics.")
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_YEARS in j.flags


def test_no_years_mentioned_passes_without_years_flag():
    j = job(description="Python, SQL, and experimentation culture.")
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_YEARS not in j.flags


@pytest.mark.parametrize("text", [
    "Bachelor's or equivalent experience required. At least 5 years of experience required.",
    "3+ years of experience. A Master's or equivalent experience is accepted.",
    "Minimum of 4 years experience or equivalent degree.",
    "3 years of professional experience with Python.",
    "At least 12 years of experience.",
    "4 yrs of relevant experience.",
])
def test_unrelated_degree_equivalence_never_cancels_years(text):
    assert filter_job(job(description=text), SETTINGS, now=NOW) == (False, "years_experience_too_high")


@pytest.mark.parametrize("text", [
    "At least 3 years of experience or equivalent achievement.",
    "Minimum of 4 years of experience or equivalent experience.",
    "3+ years in analytics or equivalent local experience.",
    "2 years of professional experience.",
    "2+ years in analytics.",
])
def test_equivalence_and_two_years_pass_without_range_flag(text):
    item = job(description=text)
    assert filter_job(item, SETTINGS, now=NOW)[0]
    assert FLAG_YEARS not in item.flags


@pytest.mark.parametrize("text", [
    "Master's degree required or equivalent experience.",
    "Ph.D. or equivalent achievements required.",
    "Bachelor's or Master's degree required.",
    "Master's degree preferred. Python required.",
])
def test_degree_local_exceptions_pass(text):
    assert filter_job(job(description=text), SETTINGS, now=NOW)[0]


@pytest.mark.parametrize("text", [
    "Master's degree required. PhD preferred.",
    "Masters in Financial Engineering, Statistics or Computer Science.",
    "Qualifications: Master's degree in a technical field.",
    "PhD mandatory. Equivalent software engineering experience is useful.",
    "Master's degree required and 5+ years or equivalent experience.",
])
def test_mandatory_degree_implicit_and_unrelated_preference_rejected(text):
    assert filter_job(job(description=text), SETTINGS, now=NOW) == (False, "masters_phd_required")


@pytest.mark.parametrize("location,country,remote", [
    ("Remote worldwide", None, True), ("London", "SG", False), ("", None, True), ("CBD", None, False),
])
def test_headquarters_or_stale_country_is_not_singapore_location(location, country, remote):
    item = job(location=location, country=country, remote=remote,
               description="Our Singapore headquarters supports global product analytics.")
    assert not filter_job(item, SETTINGS, now=NOW)[0]


@pytest.mark.parametrize("text", [
    "Train models using LightGBM for pricing research.",
    "Build a research platform with distributed model training and GPU workloads.",
    "Research customer needs and build LLM-powered RAG applications.",
    "Fine tune language models for customer support and evaluate prompts.",
    "Experience pretraining large language models preferred; focus on product analytics.",
])
def test_ordinary_ml_and_genai_do_not_trigger_foundation_knockout(text):
    assert filter_job(job(description=text), SETTINGS, now=NOW)[0]


@pytest.mark.parametrize("salary", ["SGD 5000", "SGD 60,000", "USD 5000 per month", "Total compensation SGD 5000 per month", "Up to SGD 5000 per month"])
def test_salary_currency_and_period_are_not_guessed(salary):
    item = job(salary=salary)
    assert filter_job(item, SETTINGS, now=NOW)[0]
    assert FLAG_SALARY not in item.flags


def test_full_time_graduate_programme_and_veteran_eeo_pass():
    item = job(title="Data Scientist Graduate Program", description="Equal opportunity for military veterans. Previous internships are welcome.")
    assert filter_job(item, SETTINGS, now=NOW)[0]


def test_company_history_does_not_become_applicant_experience_requirement():
    item = job(title="Machine Learning Engineer", description="We have a 25+ year track record of innovation. Requires 2+ years of distributed systems experience.")
    assert filter_job(item, SETTINGS, now=NOW)[0]
    assert item.flags == ["secondary"]


def test_principle_seniority_typo_is_rejected():
    assert filter_job(job(title="[Data Scientist] Principle Data Scientist - SG"), SETTINGS, now=NOW) == (False, "seniority_title")


@pytest.mark.parametrize("text", ["This role is an internship.", "Develop military weapons analytics."])
def test_internship_and_defence_role_descriptions_rejected(text):
    assert not filter_job(job(description=text), SETTINGS, now=NOW)[0]


# --------------------------------------------------------------------------
# degree / citizenship / clearance / foundation models
# --------------------------------------------------------------------------


def test_masters_required_rejected():
    ok, reason = filter_job(
        job(description="Master's degree is required. Python preferred."),
        SETTINGS,
        now=NOW,
    )
    assert ok is False
    assert reason == "masters_phd_required"


def test_phd_mandatory_rejected():
    ok, reason = filter_job(
        job(description="PhD mandatory for this research role."),
        SETTINGS,
        now=NOW,
    )
    assert ok is False
    assert reason == "masters_phd_required"


def test_masters_preferred_passes():
    ok, _ = filter_job(
        job(description="Master's degree preferred. Bachelor's welcome."),
        SETTINGS,
        now=NOW,
    )
    assert ok is True


def test_citizens_only_rejected():
    ok, reason = filter_job(
        job(description="Open to Singapore citizens only."),
        SETTINGS,
        now=NOW,
    )
    assert ok is False
    assert reason == "citizens_pr_only"


def test_citizens_negation_passes():
    ok, _ = filter_job(
        job(description="Role is not limited to Singapore citizens or PRs."),
        SETTINGS,
        now=NOW,
    )
    assert ok is True


def test_security_clearance_required_rejected():
    ok, reason = filter_job(
        job(description="Security clearance is required before start date."),
        SETTINGS,
        now=NOW,
    )
    assert ok is False
    assert reason == "security_clearance_required"


def test_no_security_clearance_required_passes():
    ok, _ = filter_job(
        job(description="No security clearance required for this role."),
        SETTINGS,
        now=NOW,
    )
    assert ok is True


def test_training_large_models_rejected():
    ok, reason = filter_job(
        job(
            description=(
                "You will focus on training large language models and "
                "developing new DL architectures."
            )
        ),
        SETTINGS,
        now=NOW,
    )
    assert ok is False
    assert reason == "foundation_model_research"


def test_normal_genai_product_work_passes():
    ok, _ = filter_job(
        job(
            description=(
                "Apply generative AI and RAG to customer support copilots. "
                "Prompt engineering and evaluation."
            )
        ),
        SETTINGS,
        now=NOW,
    )
    assert ok is True


# --------------------------------------------------------------------------
# salary / EP thresholds (flag only)
# --------------------------------------------------------------------------


def test_monthly_salary_below_ep_flags_not_rejects():
    j = job(
        salary="SGD 4,500 / month",
        description="Fixed monthly salary SGD 4,500.",
        raw={"fs": False},
    )
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY in j.flags


def test_monthly_salary_above_ep_no_flag():
    j = job(
        salary="SGD 8,000 / month",
        description="Fixed monthly salary SGD 8,000.",
        raw={"fs": False},
    )
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY not in j.flags


def test_annual_salary_converted_for_ep_flag():
    # 60_000 / 12 = 5_000 < 6064
    j = job(
        salary="SGD 60,000 per annum",
        description="Compensation SGD 60,000 per annum.",
        raw={"fs": False},
    )
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY in j.flags


def test_fs_threshold_used_when_raw_fs():
    # 6500 is above non-fs 6064 but below fs 6709
    j = job(
        salary="SGD 6,500 / month",
        description="Fixed monthly salary SGD 6,500.",
        raw={"fs": True},
    )
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY in j.flags


def test_date_effective_threshold_switches_in_2027():
    later = datetime(2027, 2, 1, tzinfo=timezone.utc)
    j = job(
        salary="SGD 6,200 / month",
        description="Fixed monthly salary SGD 6,200.",
        raw={"fs": False},
    )
    # 6200 >= 6064 (2025 table) → no flag before 2027
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY not in j.flags
    # 6200 < 6500 (2027 table) → flag
    j2 = job(
        salary="SGD 6,200 / month",
        description="Fixed monthly salary SGD 6,200.",
        raw={"fs": False},
    )
    ok2, _ = filter_job(j2, SETTINGS, now=later)
    assert ok2 is True
    assert FLAG_SALARY in j2.flags


def test_unknown_salary_does_not_flag():
    j = job(salary=None, description="Competitive package.", raw={"fs": False})
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY not in j.flags


def test_salary_range_flags_when_upper_below_ep():
    j = job(
        description="Salary range SGD 4,000 - 5,000 / month.",
        raw={"fs": False},
    )
    ok, _ = filter_job(j, SETTINGS, now=NOW)
    assert ok is True
    assert FLAG_SALARY in j.flags


# --------------------------------------------------------------------------
# apply_filters aggregation
# --------------------------------------------------------------------------


def test_apply_filters_counts_and_keeps():
    jobs = [
        job(title="Data Scientist", url="https://example.com/a"),
        job(title="Senior Data Scientist", url="https://example.com/b"),
        job(location="London", country="GB", url="https://example.com/c"),
    ]
    result = apply_filters(jobs, SETTINGS, now=NOW)
    assert len(result.kept) == 1
    assert len(result.rejected) == 2
    assert result.counts.get("seniority_title") == 1
    assert result.counts.get("location_not_singapore") == 1
    assert result.total == 3
