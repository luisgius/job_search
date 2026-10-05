"""Synthetic evidence boundaries: these exercise deterministic gates, not model fit."""
import pytest

from src.filters import _check_language
from src.requirements import adjacent_role_evidence, language_requirements
from tests.conftest import make_job

LEVELS = {"filters": {"language_levels": {"en": "C1", "es": "native", "de": "A2", "pl": "beginner"}}}


@pytest.mark.parametrize("description", [
    "Fluent English and basic German required.",
    "Fluent English and German A2 required.",
    "English C1 and basic German required.",
    "Basic German and English C1 required.",
    "English C1 and German A2 required.",
    "German basic and French fluent required.",
    "German optional and English fluent required.",
    "Fluent English and German preferred.",
    "German fluency is not a requirement.",
    "Fluent German, optional.",
    "German C1, not required.",
    "Fluent German is not necessary.",
    "No German required. English is our working language.",
    "We offer German classes and excellent benefits.",
    "We offer professional German courses.",
    "German C1 training is provided.",
    "You will collaborate with fluent German speakers.",
    "You will work closely with native German colleagues.",
    "German or English fluency required.",
    "German required; level not specified.",
    "Basic German required.",
    "German B1 required.",  # explicit unsupported level remains unknown
    "Polish A2 required.",
])
def test_ambiguous_optional_or_compatible_language_does_not_reject(description):
    assert _check_language(make_job(description=description), LEVELS).ok


@pytest.mark.parametrize("description", [
    "Fluent German required, French optional.",
    "Fluent German required for written or spoken communication.",
    "German C1 is mandatory; English preferred.",
    "Fluent German and English required.",
    "Fluent English and German required.",
    "German and English fluency required.",
    "Native German and fluent English required.",
    "Native or professional German required.",
    "Professional German required.",
    "Bilingual in German and English required.",
    "Sehr gute Deutschkenntnisse sind erforderlich.",
    "Fluent Polish required.",
    "We offer professional German courses. Fluent German is required.",
    "German C1 training is provided; German C1 is mandatory.",
    "You will collaborate with fluent German speakers, but fluent German is required.",
    "We offer professional German courses and fluent German is required.",
])
def test_direct_mandatory_professional_language_rejects(description):
    assert not _check_language(make_job(description=description), LEVELS).ok


def test_proficiency_does_not_bleed_between_languages():
    requirements = language_requirements("English C1 and German A2 required.")
    assert {r['language']: r['requirement'] for r in requirements} == {
        'en': 'professional', 'de': 'level_unknown'}


def test_basic_language_does_not_borrow_french_proficiency():
    requirements = language_requirements("German basic and French fluent required.")
    assert {r['language']: r['requirement'] for r in requirements} == {
        'de': 'level_unknown', 'fr': 'professional'}


@pytest.mark.parametrize("description", [
    "At least 5 years of data analytics experience required.",
    "Minimum 5 years experience and Python is a plus.",
    "5+ years relevant experience required, SQL preferred.",
])
def test_explicit_high_minimum_is_not_hidden_by_unrelated_optional_skills(description):
    assert adjacent_role_evidence('SQL and product analytics. ' + description)['high_experience_minimum']


@pytest.mark.parametrize("description", [
    "2–5 years experience.",
    "2-5 years of data analytics experience.",
    "5 years experience preferred.",
    "5 years experience is not required.",
    "5 years of data analytics experience is a bonus.",
])
def test_ranges_and_optional_experience_are_not_high_minimums(description):
    assert not adjacent_role_evidence('SQL and product analytics. ' + description)['high_experience_minimum']


def test_direct_trailing_level_overrides_shared_prefix_only_for_its_language():
    requirements = language_requirements("Fluent English and German A2 required.")
    assert {r['language']: r['requirement'] for r in requirements} == {
        'en': 'professional', 'de': 'level_unknown'}
