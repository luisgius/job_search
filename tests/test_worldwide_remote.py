"""Opt-in search scope is evidence of a workplace, not work authorization."""

import pytest

from src import geo
from src.filters import passes_location
from tests.conftest import make_job


def config(**overrides):
    return {"filters": {
        "countries": ["PL", "DE", "ES"],
        "countries_if_sponsorship": ["GB"],
        "allow_remote": True,
        "remote_requires_eu_hint": True,
        "allow_remote_worldwide": True,
        **overrides,
    }}


@pytest.mark.parametrize("location,description", [
    ("Remote - Worldwide", "Build data products."),
    ("Worldwide (Remote)", "Build data products."),
    ("Global Remote", "Build data products."),
    ("Home based - Worldwide", "Build data products."),
    ("Remote, Global", "Build data products."),
    ("Remote - Global", "Build data products."),
    ("Remote (Global)", "Build data products."),
    ("Anywhere in the World", "Build data products."),
    ("Anywhere", "Build data products."),
    ("Remote", "This role is fully remote worldwide."),
    ("Remote", "You can work from anywhere in the world."),
    ("", "Work from anywhere."),
    ("", "You can work remotely from any country."),
    ("Remote", "Work from anywhere. Our headquarters are in the United States."),
])
def test_explicit_worldwide_work_is_opt_in_and_preserves_unknown_country(location, description):
    job = make_job(location=location, description=description)
    assert not passes_location(job, config(allow_remote_worldwide=False))[0]
    assert passes_location(job, config())[0]
    assert job.country is None
    assert job.remote is True
    assert job.raw["remote_worldwide_evidence"]
    assert not any("authoriz" in key or "eligib" in key for key in job.raw)


def test_missing_setting_defaults_to_disabled():
    conf = config()
    del conf["filters"]["allow_remote_worldwide"]
    assert not passes_location(make_job(location="Remote worldwide"), conf)[0]


@pytest.mark.parametrize("description", [
    "Build data products remotely.",
    "We are a global company with worldwide customers and remote benefits.",
    "Join our global remote team.",
    "Worldwide travel is a benefit of this remote role.",
    "Our customers can work from anywhere with our software.",
    "Work from anywhere for 30 days per year.",
    "A work-from-anywhere policy is one of our benefits.",
    "This role is not remote worldwide.",
    "We never allow staff to work from anywhere.",
])
def test_generic_remote_and_global_boilerplate_do_not_supply_a_hint(description):
    job = make_job(location="Remote", remote=True, description=description)
    ok, reason = passes_location(job, config())
    assert not ok
    assert "remote_requires_eu_hint" in reason
    assert "remote_worldwide_evidence" not in job.raw


@pytest.mark.parametrize("location", [
    "Remote (US)", "Remote (Canada only)", "Remote US/Canada only",
    "San Francisco, CA", "Toronto, Canada", "Bangalore", "Remote - India",
])
def test_authoritative_location_beats_worldwide_prose(location):
    job = make_job(location=location, remote=True,
                   description="This role is remote worldwide. Work from anywhere.")
    assert not passes_location(job, config())[0]


@pytest.mark.parametrize("restriction", [
    "Candidates must be based in Canada.",
    "Applicants must reside in the United States.",
    "US residents only.",
    "Canada only.",
    "You need authorization to work in the US.",
    "Candidates must have UK citizenship.",
    "Only hiring in Australia.",
    "Remote in Canada.",
    "Work from anywhere in the United States.",
    "Remote within approved countries.",
    "This role is hybrid.",
    "This role requires onsite work.",
    "We are hiring US-based candidates for this role.",
    "Candidates must be authorized to work in the United States.",
    "Candidates need the right to work in Canada.",
    "Candidates are located in Canada.",
    "This opening is limited to the United States.",
    "This role is only available in Canada.",
])
def test_conflicting_restrictions_do_not_become_worldwide(restriction):
    job = make_job(location="Remote", remote=True,
                   description=f"Work from anywhere. {restriction}")
    assert geo.worldwide_remote_evidence(job.location, job.title, job.description) is None
    # The existing Europe hint path remains separately permissive; do not
    # use a European restriction to assert a change to that established path.
    if not geo.mentions_eu(restriction):
        assert not passes_location(job, config())[0]


@pytest.mark.parametrize("location", ["Hybrid", "On-site", "Fully remote / hybrid"])
def test_arrangement_label_beats_description_even_with_source_remote_true(location):
    job = make_job(location=location, remote=True, description="Work from anywhere.")
    assert not passes_location(job, config())[0]


def test_source_remote_false_is_not_overridden():
    job = make_job(location="Remote worldwide", remote=False)
    assert not passes_location(job, config())[0]
    assert job.remote is False


def test_existing_country_and_sponsorship_gates_are_preserved():
    for enabled in (False, True):
        conf = config(allow_remote_worldwide=enabled)
        assert passes_location(make_job(location="Berlin, Germany"), conf)[0]
        assert passes_location(make_job(location="Remote Europe"), conf)[0]
        assert not passes_location(make_job(location="Paris, France",
                                            description="Work from anywhere."), conf)[0]
        uk = make_job(location="London, UK", description="Work from anywhere.")
        assert not passes_location(uk, conf)[0]
        uk.description += " We offer visa sponsorship."
        assert passes_location(uk, conf)[0]


def test_remote_disable_and_empty_allowlist_keep_their_existing_meanings():
    job = make_job(location="Remote worldwide")
    assert not passes_location(job, config(allow_remote=False))[0]
    assert passes_location(make_job(location="Berlin, Germany"),
                           config(allow_remote=False))[0]
    assert passes_location(make_job(location="Unknown"), config(countries=[]))[0]


def test_us_title_and_source_country_are_not_overridden():
    assert not passes_location(make_job(location="Remote worldwide",
        title="Engineer (US only)"), config())[0]
    assert not passes_location(make_job(location="Remote worldwide", country="US"),
                               config())[0]


@pytest.mark.parametrize("description", [
    "Responsibilities include, but are not limited to, forecasting models.",
    "You must have 2+ years of Python and SQL.",
    "You must be comfortable presenting to stakeholders.",
    "Experience with hybrid search is a plus.",
    "Implement OAuth authorization for our API.",
    "Our headquarters are located in the United States.",
    "No travel restrictions apply.",
    "Responsibilities include, but are not limited to, forecasting models. "
    "You must have 2+ years of Python and SQL. You must be comfortable presenting "
    "to stakeholders. Experience with hybrid search is a plus.",
])
def test_ordinary_description_wording_does_not_veto_worldwide_location(description):
    job = make_job(location="Remote - Worldwide", description=description)
    assert not geo.resolve(job).eu_hint
    assert passes_location(job, config())[0]
    assert job.country is None
    assert job.remote is True
    assert job.raw["remote_worldwide_evidence"]


@pytest.mark.parametrize("location", ["Worldwide", "Global"])
def test_bare_global_label_requires_source_remote_flag(location):
    assert not passes_location(make_job(location=location, description="Build models."), config())[0]
    job = make_job(location=location, remote=True, description="Build models.")
    assert passes_location(job, config())[0]
    assert job.country is None


def test_mixed_global_and_us_locations_keep_existing_conservative_veto():
    # Deliberate limitation: worldwide evidence does not override the earlier
    # US/country gate, even when Global is one entry in a multi-location list.
    job = make_job(location="Remote, Global; Remote, San Francisco, CA; Remote, AMER",
                   remote=True, description="Work from anywhere in the world.")
    assert not passes_location(job, config())[0]


@pytest.mark.xfail(reason="existing Europe hint reads an excluded region; outside this change",
                   strict=True)
@pytest.mark.parametrize("location", [
    "Remote - Worldwide (excluding Europe)", "Remote (Global, except EU)",
])
def test_excluded_europe_is_not_an_eligibility_hint_existing_limitation(location):
    assert not passes_location(make_job(location=location, description="Build models."), config())[0]
