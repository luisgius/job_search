"""Tests for src/geo.py — free-text location -> ISO country.

Two failure modes matter, and they are not symmetric:

  * a **false positive** (a US job resolved to an EU country) puts a job you
    cannot take into "Needs your click", and in the worst case auto-applies
    to it;
  * a **false negative** (an EU job resolved to nothing) silently drops a job
    you wanted.

Europe and the US share a startling number of city names, so most of this
file is about the tie-break. The rule under test: when a segment carries any
US signal, that segment is discarded before any city lookup runs.
"""

from __future__ import annotations

import pytest

from src.geo import (
    ALL_COUNTRIES,
    CITY_TO_COUNTRY,
    COUNTRY_ALIASES,
    EU_COUNTRIES,
    US_STATE_CODES,
    country_name,
    country_of,
    is_eu,
    is_remote,
    looks_like_us,
    mentions_eu,
    resolve,
)


# ==========================================================================
# tables
# ==========================================================================


def test_eu_countries_covers_the_eu27_plus_eea_and_uk():
    assert len(EU_COUNTRIES) == 32
    for code in ("DE", "NL", "ES", "FR", "IE", "PT", "SE", "DK", "FI", "PL",
                 "AT", "BE", "IT", "CZ", "GR", "RO", "HU", "BG", "HR", "SK",
                 "SI", "EE", "LV", "LT", "LU", "MT", "CY"):
        assert code in EU_COUNTRIES, code
    for code in ("NO", "CH", "IS", "LI", "GB"):
        assert code in EU_COUNTRIES, code


def test_non_eu_europe_is_resolved_then_filtered_not_ignored():
    """Resolving Belgrade to RS and letting `filters.countries` drop it beats
    reporting "no country found", which looks like a parser bug."""
    assert country_of("Belgrade, Serbia") == "RS"
    assert is_eu("RS") is False
    assert "RS" in ALL_COUNTRIES


def test_city_table_is_large_enough_to_be_useful():
    assert len(CITY_TO_COUNTRY) >= 80


def test_country_name_and_is_eu():
    assert country_name("DE") == "Germany"
    assert country_name("de") == "Germany"
    assert country_name(None) == ""
    assert country_name("ZZ") == "ZZ"
    assert is_eu("DE") is True
    assert is_eu(None) is False


# ==========================================================================
# country_of — the EU cases that must resolve
# ==========================================================================


@pytest.mark.parametrize(
    "location,expected",
    [
        # country named outright
        ("Berlin, Germany", "DE"),
        ("Amsterdam, Netherlands", "NL"),
        ("Amsterdam, The Netherlands", "NL"),
        ("London, UK", "GB"),
        ("London, United Kingdom", "GB"),
        ("Edinburgh, Scotland", "GB"),
        ("Dublin, Ireland", "IE"),
        ("Kraków, Poland", "PL"),
        ("Lisbon, Portugal", "PT"),
        ("Prague, Czech Republic", "CZ"),
        ("Prague, Czechia", "CZ"),
        # ISO code as the tail
        ("Munich, DE", "DE"),
        ("Barcelona, ES", "ES"),
        ("Stockholm, SE", "SE"),
        # endonyms and accents
        ("Zürich", "CH"),
        ("Zurich", "CH"),
        ("München", "DE"),
        ("Köln", "DE"),
        ("Wien", "AT"),
        ("Warszawa", "PL"),
        ("København", "DK"),
        ("Göteborg", "SE"),
        ("Milano", "IT"),
        ("Lisboa", "PT"),
        ("Genève", "CH"),
        # bare cities
        ("Berlin", "DE"),
        ("Amsterdam", "NL"),
        ("Barcelona", "ES"),
        ("Helsinki", "FI"),
        ("Tallinn", "EE"),
        ("Bucharest", "RO"),
        ("Ljubljana", "SI"),
        ("Luxembourg", "LU"),
        # decorated
        ("Hybrid - Berlin", "DE"),
        ("Berlin / Remote", "DE"),
        ("Remote, Germany", "DE"),
        ("Berlin, Germany (Hybrid)", "DE"),
        ("Amsterdam, North Holland, Netherlands", "NL"),
        ("Madrid, Community of Madrid, Spain", "ES"),
    ],
)
def test_country_of_resolves_european_locations(location, expected):
    assert country_of(location) == expected


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Paris, France; Remote", "FR"),
        ("Berlin, Germany | Munich, Germany", "DE"),
        ("Remote (Europe) or Berlin", "DE"),
    ],
)
def test_country_of_handles_multi_location_strings(location, expected):
    assert country_of(location) == expected


def test_a_us_segment_does_not_poison_a_european_one():
    """"Boston, MA or Berlin, Germany" is a real posting shape. Dropping the
    whole string because it mentions the US would lose the job."""
    assert country_of("Boston, MA or Berlin, Germany") == "DE"
    assert country_of("San Francisco, CA; Berlin, Germany") == "DE"


# ==========================================================================
# country_of — the US cases that must NOT resolve
# ==========================================================================


@pytest.mark.parametrize(
    "location",
    [
        "Berlin, CT",
        "Birmingham, AL",
        "Dublin, OH",
        "Athens, GA",
        "Naples, FL",
        "Vienna, VA",
        "Paris, TX",
        "Cambridge, MA",
        "Frankfort, KY",
        "Hamburg, NY",
        "Moscow, ID",
        "San Francisco, CA",
        "New York, NY",
        "Austin, TX",
        "Seattle, Washington",
        "Remote (US)",
        "Remote - United States",
        "Remote, USA",
        "Chicago, Illinois",
    ],
)
def test_country_of_refuses_us_locations(location):
    """The expensive mistake: an EU-only filter must not let these through."""
    assert country_of(location) not in EU_COUNTRIES


@pytest.mark.parametrize(
    "location",
    ["Berlin, CT", "San Francisco, CA", "Remote (US)", "Austin, TX",
     "Boston, Massachusetts", "New York, NY 10001"],
)
def test_looks_like_us(location):
    assert looks_like_us(location) is True


@pytest.mark.parametrize(
    "location",
    ["Berlin, Germany", "Amsterdam", "Remote - Europe", "Zürich, Switzerland"],
)
def test_looks_like_us_is_false_for_europe(location):
    assert looks_like_us(location) is False


@pytest.mark.parametrize(
    "location",
    ["Munich, DE", "Berlin, DE", "Hamburg, DE", "Köln, DE", "Frankfurt, DE",
     "Remote, DE", "Germany, DE"],
)
def test_a_trailing_de_after_a_european_city_is_germany(location):
    assert country_of(location) == "DE"


@pytest.mark.parametrize(
    "location",
    ["Wilmington, DE", "Newark, DE", "Dover, DE", "Smyrna, DE", "Middletown, DE"],
)
def test_a_trailing_de_after_an_unknown_city_is_delaware(location):
    """`DE` is both Germany and Delaware — the nastiest collision in the table,
    and the one my first attempt got wrong. It was decided by a hand-written
    list of Delaware towns, which meant every town not on the list (Newark,
    Dover) resolved to Germany and walked straight through an EU-only filter.
    It is now decided by whether the city in front of it is a European one we
    recognise, which does not need the list to be complete."""
    assert country_of(location) not in EU_COUNTRIES


@pytest.mark.parametrize(
    "location,expected",
    [("Valletta, MT", "MT"), ("Malta, MT", "MT"),
     ("Bozeman, MT", None), ("Billings, MT", None), ("Missoula, MT", None)],
)
def test_malta_and_montana_are_told_apart_the_same_way(location, expected):
    assert country_of(location) == expected


def test_an_unrecognised_city_before_de_still_reads_as_germany_when_placeless():
    """The deliberate default. An unknown *named* place next to DE reads as
    Delaware; a placeless head ("Remote, DE") reads as Germany, because that
    is how those postings are actually written."""
    assert country_of("Remote, DE") == "DE"
    assert country_of("Hybrid, DE") == "DE"


def test_substring_matching_is_never_used():
    """"IN" appears inside "Berlin"; a naive `in` test maps half of Germany to
    Indiana. Whole-word matching is the invariant."""
    assert country_of("Berlin") == "DE"
    assert country_of("Indianapolis, IN") not in EU_COUNTRIES


# ==========================================================================
# country_of — nothing to go on
# ==========================================================================


@pytest.mark.parametrize(
    "location", [None, "", "   ", "Remote", "Anywhere", "Global", "Worldwide",
                 "Multiple Locations", "Somewhereville"],
)
def test_country_of_returns_none_when_it_cannot_tell(location):
    assert country_of(location) is None


def test_country_of_does_not_crash_on_pathological_input():
    assert country_of("," * 500) is None
    assert country_of("Berlin " * 300) == "DE"
    assert country_of("<script>alert(1)</script>") is None


# ==========================================================================
# is_remote
# ==========================================================================


@pytest.mark.parametrize(
    "location",
    ["Remote", "Remote - Europe", "Fully Remote", "remote", "REMOTE",
     "Work From Home", "WFH", "Telecommute", "Remote (EMEA)", "Anywhere"],
)
def test_is_remote_true(location):
    assert is_remote(location) is True


@pytest.mark.parametrize(
    "location",
    ["Berlin, Germany", "Amsterdam", "", "Hybrid - Berlin", "Hybrid",
     "Berlin (Hybrid)", "On-site, Munich"],
)
def test_is_remote_false(location):
    # Hybrid is deliberately not remote: it carries a commute.
    assert is_remote(location) is False


def test_is_remote_ignores_remote_as_a_technical_term():
    """"Remote Sensing Engineer" in Toulouse is an onsite job about satellites."""
    assert is_remote("Toulouse, France", title="Remote Sensing Engineer") is False
    assert is_remote("Berlin", title="Remote Monitoring Specialist") is False


def test_is_remote_reads_the_title_and_description_too():
    assert is_remote("", title="Backend Engineer (Remote)") is True
    assert is_remote("", description="This is a fully remote position.") is True


# ==========================================================================
# mentions_eu
# ==========================================================================


@pytest.mark.parametrize(
    "text",
    ["Remote - Europe", "EMEA", "European Union", "Remote (EU)", "EEA",
     "anywhere in Europe", "CET timezone", "CEST", "EU timezones",
     "must overlap with UTC+1", "GMT+1", "Berlin, Germany", "based in Spain"],
)
def test_mentions_eu_true(text):
    assert mentions_eu(text) is True


@pytest.mark.parametrize(
    "text",
    ["Remote (US)", "United States", "anywhere in the Americas", "APAC",
     "San Francisco", "", None, "PST timezone"],
)
def test_mentions_eu_false(text):
    assert mentions_eu(text) is False


def test_mentions_eu_does_not_fire_on_lookalike_words():
    """"English" contains "eng", "Polish" contains "pol", "island" contains
    "islan" — none of them are location evidence."""
    assert mentions_eu("Fluent English required") is False
    assert mentions_eu("polish the API surface") is False
    assert mentions_eu("Rhode Island") is False


# ==========================================================================
# resolve
# ==========================================================================


def test_resolve_reads_a_job_like_object():
    from tests.conftest import make_job

    result = resolve(make_job(location="Berlin, Germany", title="Backend Engineer"))
    assert result.country == "DE"
    assert result.in_eu is True
    assert result.remote is False


def test_resolve_on_a_remote_eu_posting():
    from tests.conftest import make_job

    result = resolve(make_job(location="Remote - Europe", title="Senior Engineer"))
    assert result.remote is True
    assert result.eu_hint is True


def test_resolve_on_a_us_remote_posting():
    from tests.conftest import make_job

    result = resolve(make_job(location="Remote", title="Engineer",
                              description="Work from anywhere in the United States."))
    assert result.remote is True
    assert result.in_eu is False
    assert result.eu_hint is False


def test_country_alias_table_has_no_dangerously_short_entries():
    """A one- or two-letter alias that is also an English word turns every
    posting containing that word into a location match."""
    for alias in COUNTRY_ALIASES:
        assert len(alias) >= 2, alias
    for word in ("no", "is", "in", "it", "at", "be", "as", "so", "an", "or", "on"):
        # These are all real ISO-2 codes AND common words; if any is matched
        # case-insensitively the tables are unsafe.
        assert country_of(f"we {word} hiring engineers") is None, word


def test_us_state_codes_table_is_complete():
    assert len(US_STATE_CODES) >= 50
    for code in ("CA", "NY", "TX", "MA", "WA", "IL", "FL", "GA", "OH", "CT"):
        assert code in US_STATE_CODES


# ==========================================================================
# non-European collisions — the Spanish-language case
# ==========================================================================


@pytest.mark.parametrize(
    "location",
    ["Valencia, Spain", "Valencia", "València", "Valencia, España",
     "Valencia, ES", "Valencia, Comunidad Valenciana, Spain",
     "Paterna, Valencia", "Remote - Valencia", "Hybrid - Valencia, Spain",
     "Alicante, Spain", "Castellón, Spain", "Elche, Spain"],
)
def test_valencia_and_its_region_resolve_to_spain(location):
    assert country_of(location) == "ES"


@pytest.mark.parametrize(
    "location",
    ["Valencia, Venezuela", "Barcelona, Venezuela", "Mérida, Venezuela",
     "Córdoba, Argentina", "Santa Fe, Argentina", "León, Mexico",
     "Mérida, Mexico", "Valencia, Philippines", "London, Ontario",
     "London, Canada", "Newcastle, Australia", "Valparaíso, Chile",
     "Cartagena, Colombia", "Santiago, Chile"],
)
def test_a_named_non_european_country_vetoes_the_city(location):
    """The same class of bug as Delaware, and it bites hardest in Spanish:
    Valencia, Barcelona and Mérida are Venezuelan cities too, Córdoba and
    Santa Fe are Argentinian, León and Mérida Mexican, London is in Ontario.
    Without the veto every one of them was filed as a European job, scored,
    tailored and shown as something to apply for."""
    assert country_of(location) not in EU_COUNTRIES


def test_a_bare_city_name_is_still_european():
    """The veto is on *named countries* only. A Spanish posting says
    "Valencia", not "Valencia, Spain", and it must keep working."""
    assert country_of("Valencia") == "ES"
    assert country_of("Barcelona") == "ES"
    assert country_of("London") == "GB"
