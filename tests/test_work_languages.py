"""`filters.work_languages`: Luis works in English and Spanish only, so a
posting that demands any other language — or is written only in one — is
dropped before scoring. Optional mentions, alternatives and "the German
market" are not demands."""

from __future__ import annotations

import pytest

from src.filters import _check_language
from src.requirements import posting_language
from tests.conftest import make_job

WORK = {"filters": {"work_languages": ["en", "es"],
                    "language_levels": {"en": "C1", "es": "native", "de": "A2", "pl": "beginner"}}}

ENGLISH_BODY = (
    " You will build forecasting models with Python and SQL, work with the product "
    "team on experiments, and present what you find to stakeholders. We are a team "
    "of twelve and we value clear writing. This is a hybrid role in our office."
)


@pytest.mark.parametrize("description", [
    "Fluent French required.",
    "Polish required.",
    "German language skills are essential.",
    "Basic German required.",
    "Fluent Mandarin is required for client meetings.",
    "Dutch C1 is mandatory.",
    "Profil : maîtrise du français et de Python.",
    "Wymagania: biegła znajomość języka polskiego.",
    "Sehr gute Deutschkenntnisse in Wort und Schrift.",
    "Vloeiend Nederlands is een must.",
    "Ottima conoscenza della lingua italiana.",
])
def test_a_demanded_language_outside_english_and_spanish_rejects(description):
    check = _check_language(make_job(description=description + ENGLISH_BODY), WORK)
    assert not check.ok and check.category == "language_requirement"


@pytest.mark.parametrize("description", [
    "Fluent English required. Spanish is a plus.",
    "Fluent Spanish required.",
    "Polish is a plus.",
    "Mile widziana znajomość języka polskiego.",
    "German or English fluency required.",
    "Le français ou l'anglais courant.",
    "Experience in the German market is required.",
    "We offer professional German courses.",
    "You will work closely with native German colleagues.",
])
def test_optional_alternative_and_non_language_mentions_pass(description):
    assert _check_language(make_job(description=description + ENGLISH_BODY), WORK).ok


def test_without_work_languages_the_old_level_rule_is_unchanged():
    old = {"filters": {"language_levels": {"de": "A2"}}}
    assert _check_language(make_job(description="Fluent French required." + ENGLISH_BODY), old).ok


FRENCH_AD = (
    "Nous recherchons un Data Scientist pour rejoindre notre équipe à Lyon. Vous "
    "serez en charge de la conception des modèles de prévision et de leur mise en "
    "production. Vous travaillerez avec les équipes produit et data sur des sujets "
    "variés. Votre profil : diplômé d'une école d'ingénieur, vous avez une première "
    "expérience en Python et SQL. Nous offrons un environnement stimulant, des "
    "locaux au cœur de la ville et une équipe qui partage ses connaissances."
)
POLISH_AD = (
    "Poszukujemy osoby na stanowisko Data Scientist, która dołączy do naszego zespołu "
    "w Krakowie. Twoje zadania będą obejmować budowę modeli prognostycznych oraz ich "
    "wdrożenie. Oferujemy pracę w zespole, który dzieli się wiedzą, oraz elastyczne "
    "godziny pracy. Wymagania: doświadczenie w Python i SQL, znajomość statystyki, "
    "umiejętność pracy w zespole. To jest praca dla osoby, która lubi wyzwania i nie "
    "boi się odpowiedzialności przez cały czas trwania projektu."
)


def test_an_ad_written_only_in_french_or_polish_rejects():
    assert posting_language(FRENCH_AD) == "fr" and posting_language(POLISH_AD) == "pl"
    for ad in (FRENCH_AD, POLISH_AD):
        check = _check_language(make_job(description=ad), WORK)
        assert not check.ok and "written in" in check.reason


def test_a_bilingual_ad_with_a_full_english_version_passes():
    bilingual = POLISH_AD + "\n\nEnglish version:" + ENGLISH_BODY * 3
    assert posting_language(bilingual) is None
    assert _check_language(make_job(description=bilingual), WORK).ok


def test_a_spanish_ad_and_a_short_snippet_are_not_rejected_for_their_language():
    spanish = ("Buscamos un Data Scientist para nuestro equipo en Madrid. Ofrecemos "
               "trabajo híbrido y formación. Requisitos: experiencia con Python, "
               "conocimientos de SQL y estadística, también valoramos años de "
               "experiencia en modelos de previsión para el sector retail. ") * 2
    assert _check_language(make_job(description=spanish), WORK).ok
    assert posting_language("Prognosemodelle. SQL.") is None


def test_a_chinese_ad_is_read_by_its_script():
    chinese = "我們正在尋找一位資料科學家，負責建立預測模型並與產品團隊合作。" * 12
    assert posting_language(chinese) == "zh"
