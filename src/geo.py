"""EU / EEA / UK location resolution.

Job boards hand us free-text locations: ``"Berlin, Germany"``, ``"Munich, DE"``,
``"Remote (EMEA)"``, ``"Hybrid - Berlin"``, ``"Boston, MA or Berlin"``. This
module turns that soup into an ISO-3166 alpha-2 code, a remote flag and an
"is Europe mentioned anywhere?" hint, so `filters.py` can make one clean
decision per job.

Three rules drive every design choice here:

1. **Never a naive substring test.** ``"IN" in "Berlin"`` is True and would map
   half of Germany to Indiana. Everything below matches whole words (or whole
   word *sequences*) over `models.normalize_text` output, which also folds
   accents so ``"Zürich"`` and ``"Zurich"`` are the same key.
2. **The United States wins ties.** Europe and the US share an enormous number
   of city names (Berlin CT, Birmingham AL, Dublin OH, Athens GA, Naples FL,
   Vienna VA, Paris TX...). Whenever a location segment carries a US signal —
   a state code, a state name, ``US``/``USA``, a US-only city — that segment is
   discarded before any city lookup runs.
3. **Segment by segment.** ``"San Francisco, CA; Berlin, Germany"`` is a real
   posting. The US segment is dropped and the German one still resolves.

Pure stdlib, no network, no config: everything is a lookup table.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import normalize_text

# --------------------------------------------------------------------------
# countries
# --------------------------------------------------------------------------

EU_COUNTRIES: dict[str, str] = {
    # EU-27
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
    # EEA / EFTA / UK — outside the EU proper but inside the same job market.
    "IS": "Iceland",
    "LI": "Liechtenstein",
    "NO": "Norway",
    "CH": "Switzerland",
    "GB": "United Kingdom",
}

# European but *not* EU/EEA/UK. Resolved so a posting in Belgrade or Kyiv is
# reported honestly as RS/UA and then dropped by `filters.countries`, rather
# than silently looking like "no country found".
OTHER_EUROPE: dict[str, str] = {
    "AL": "Albania",
    "BA": "Bosnia and Herzegovina",
    "BY": "Belarus",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MK": "North Macedonia",
    "RS": "Serbia",
    "TR": "Türkiye",
    "UA": "Ukraine",
}

ALL_COUNTRIES: dict[str, str] = {**EU_COUNTRIES, **OTHER_EUROPE}


def is_eu(code: str | None) -> bool:
    """True when `code` is one of the EU/EEA/UK countries we hunt in."""
    return bool(code) and str(code).upper() in EU_COUNTRIES


def country_name(code: str | None) -> str:
    """English name for an ISO alpha-2 code, or the code itself if unknown."""
    if not code:
        return ""
    upper = str(code).upper()
    return ALL_COUNTRIES.get(upper, upper)


# --------------------------------------------------------------------------
# country aliases: names, endonyms, adjectives, ISO-2 / ISO-3 codes
# --------------------------------------------------------------------------

# NOTE on 3-letter codes: "nor", "fin", "est", "lie" are ordinary words in
# English/French and are deliberately absent.
_COUNTRY_ALIAS_SOURCE: dict[str, tuple[str, ...]] = {
    "AT": ("Austria", "Österreich", "Oesterreich", "Autriche", "Austrian", "AT", "AUT"),
    "BE": ("Belgium", "België", "Belgie", "Belgique", "Belgien", "Belgian", "BE", "BEL"),
    "BG": ("Bulgaria", "Bulgarien", "Bulgarian", "BG", "BGR"),
    "HR": ("Croatia", "Hrvatska", "Croatian", "HR", "HRV"),
    "CY": ("Cyprus", "Cypriot", "Kypros", "CY", "CYP"),
    "CZ": ("Czechia", "Czech Republic", "Czech", "Cesko", "Česko", "CZ", "CZE"),
    "DK": ("Denmark", "Danmark", "Danish", "Dänemark", "DK", "DNK"),
    "EE": ("Estonia", "Eesti", "Estonian", "EE"),
    "FI": ("Finland", "Suomi", "Finnish", "Finnland", "FI"),
    "FR": ("France", "Frankreich", "Francia", "French", "FR", "FRA"),
    "DE": (
        "Germany", "Deutschland", "Allemagne", "Alemania", "Germania",
        "German", "DE", "DEU", "Ger",
    ),
    "GR": ("Greece", "Hellas", "Ellada", "Greek", "Griechenland", "GR", "GRC"),
    "HU": ("Hungary", "Magyarorszag", "Magyarország", "Hungarian", "HU", "HUN"),
    "IE": ("Ireland", "Eire", "Éire", "Irish", "Republic of Ireland", "IE", "IRL"),
    "IT": ("Italy", "Italia", "Italien", "Italian", "IT", "ITA"),
    "LV": ("Latvia", "Latvija", "Latvian", "LV", "LVA"),
    "LT": ("Lithuania", "Lietuva", "Lithuanian", "LT", "LTU"),
    "LU": ("Luxembourg", "Luxemburg", "Letzebuerg", "Lëtzebuerg", "Luxembourgish", "LU"),
    "MT": ("Malta", "Maltese", "MLT"),
    "NL": (
        "Netherlands", "The Netherlands", "Nederland", "Holland", "Niederlande",
        "Pays-Bas", "Dutch", "NL", "NLD",
    ),
    "PL": ("Poland", "Polska", "Polish", "Polen", "Pologne", "PL", "POL"),
    "PT": ("Portugal", "Portuguese", "PT", "PRT"),
    "RO": ("Romania", "Rumänien", "Rumanien", "Romanian", "RO", "ROU"),
    "SK": ("Slovakia", "Slovensko", "Slovak", "SK", "SVK"),
    "SI": ("Slovenia", "Slovenija", "Slovenian", "SI", "SVN"),
    "ES": ("Spain", "España", "Espana", "Espagne", "Spanish", "Spanien", "ES", "ESP"),
    "SE": ("Sweden", "Sverige", "Swedish", "Schweden", "SE", "SWE"),
    "IS": ("Iceland", "Island", "Icelandic", "ISL"),
    "LI": ("Liechtenstein",),
    "NO": ("Norway", "Norge", "Noreg", "Norwegian", "Norwegen", "NO"),
    "CH": ("Switzerland", "Schweiz", "Suisse", "Svizzera", "Swiss", "CH", "CHE"),
    "GB": (
        "United Kingdom", "UK", "U.K.", "Great Britain", "Britain", "British",
        "England", "Scotland", "Wales", "Northern Ireland", "English",
        "Scottish", "Welsh", "GB", "GBR",
    ),
    # non-EU Europe
    "AL": ("Albania", "Shqiperi", "Albanian"),
    "BA": ("Bosnia and Herzegovina", "Bosnia", "Herzegovina", "BiH"),
    "BY": ("Belarus", "Belarusian"),
    "MD": ("Moldova", "Moldovan"),
    "ME": ("Montenegro", "Crna Gora"),
    "MK": ("North Macedonia", "Macedonia", "Macedonian"),
    "RS": ("Serbia", "Srbija", "Serbian"),
    "TR": ("Türkiye", "Turkiye", "Turkey", "Turkish"),
    "UA": ("Ukraine", "Ukraina", "Ukrainian"),
}

COUNTRY_ALIASES: dict[str, str] = {}
for _iso, _aliases in _COUNTRY_ALIAS_SOURCE.items():
    for _alias in _aliases:
        _key = normalize_text(_alias)
        if _key:
            COUNTRY_ALIASES.setdefault(_key, _iso)

_ALIAS_MAX_NGRAM = max(len(k.split()) for k in COUNTRY_ALIASES)


# --------------------------------------------------------------------------
# city -> country
# --------------------------------------------------------------------------

# Accented spellings are listed alongside the English ones because
# `normalize_text` only folds *combining* accents: "ø" and "ł" survive NFKD
# decomposition, so "København" and "Łódź" need their own keys.
_CITY_SOURCE: dict[str, tuple[str, ...]] = {
    "DE": (
        "Berlin", "Munich", "München", "Muenchen", "Hamburg", "Cologne", "Köln",
        "Koeln", "Frankfurt", "Frankfurt am Main", "Stuttgart", "Düsseldorf",
        "Dusseldorf", "Duesseldorf", "Leipzig", "Dresden", "Nuremberg",
        "Nürnberg", "Hannover", "Hanover", "Bremen", "Karlsruhe", "Mannheim",
        "Essen", "Dortmund", "Bonn", "Aachen", "Heidelberg", "Freiburg",
        "Münster", "Wiesbaden", "Augsburg", "Potsdam", "Darmstadt", "Kiel",
    ),
    "NL": (
        "Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "The Hague",
        "Den Haag", "Hague", "Groningen", "Delft", "Haarlem", "Leiden",
        "Tilburg", "Almere", "Arnhem", "Nijmegen", "Breda", "Enschede",
        "Maastricht", "Amersfoort", "Hilversum", "Zwolle", "Schiphol",
    ),
    "FR": (
        "Paris", "Lyon", "Toulouse", "Bordeaux", "Nantes", "Lille", "Marseille",
        "Nice", "Strasbourg", "Montpellier", "Rennes", "Grenoble",
        "Sophia Antipolis", "Aix-en-Provence", "Toulon", "Saint-Etienne",
        "Saint-Étienne", "Reims", "Angers", "Dijon", "Nancy", "Metz",
    ),
    "ES": (
        "Madrid", "Barcelona", "Valencia", "Seville", "Sevilla", "Malaga",
        "Málaga", "Bilbao", "Zaragoza", "Murcia", "Palma", "Alicante",
        "Granada", "Valladolid", "Vigo", "A Coruña", "La Coruna",
        "San Sebastian", "Donostia", "Santander", "Gijon", "Cordoba",
        "Córdoba", "Pamplona", "Las Palmas", "Santa Cruz de Tenerife",
        "Toledo", "Salamanca",
    ),
    "PT": (
        "Lisbon", "Lisboa", "Porto", "Oporto", "Braga", "Coimbra", "Aveiro",
        "Faro", "Funchal", "Cascais", "Sintra", "Guimaraes", "Guimarães",
    ),
    "IE": ("Dublin", "Cork", "Galway", "Limerick", "Waterford", "Sligo"),
    "GB": (
        "London", "Manchester", "Edinburgh", "Glasgow", "Bristol", "Cambridge",
        "Oxford", "Leeds", "Birmingham", "Sheffield", "Liverpool",
        "Newcastle", "Newcastle upon Tyne", "Nottingham", "Reading",
        "Brighton", "Cardiff", "Belfast", "Aberdeen", "Coventry", "Leicester",
        "Southampton", "Milton Keynes", "York", "Bath", "Exeter", "Norwich",
        "Derby", "Portsmouth", "Swansea", "Dundee", "St Albans", "Croydon",
        "Slough", "Basingstoke", "Bournemouth", "Warwick",
    ),
    "SE": (
        "Stockholm", "Gothenburg", "Göteborg", "Goteborg", "Malmö", "Malmo",
        "Uppsala", "Linköping", "Linkoping", "Lund", "Västerås", "Vasteras",
        "Örebro", "Orebro", "Helsingborg", "Umeå", "Umea", "Jönköping",
        "Jonkoping", "Solna", "Kista", "Sundbyberg",
    ),
    "DK": (
        "Copenhagen", "København", "Kobenhavn", "Koebenhavn", "Aarhus",
        "Århus", "Arhus", "Odense", "Aalborg", "Ålborg", "Esbjerg",
        "Roskilde", "Lyngby", "Horsens",
    ),
    "NO": (
        "Oslo", "Bergen", "Trondheim", "Stavanger", "Tromsø", "Tromso",
        "Drammen", "Fornebu", "Lysaker", "Kristiansand",
    ),
    "FI": (
        "Helsinki", "Espoo", "Tampere", "Oulu", "Turku", "Vantaa",
        "Jyväskylä", "Jyvaskyla", "Lahti", "Kuopio",
    ),
    "EE": ("Tallinn", "Tartu"),
    "LV": ("Riga", "Rīga"),
    "LT": ("Vilnius", "Kaunas", "Klaipeda", "Klaipėda"),
    "PL": (
        "Warsaw", "Warszawa", "Krakow", "Kraków", "Cracow", "Wroclaw",
        "Wrocław", "Poznan", "Poznań", "Gdansk", "Gdańsk", "Katowice",
        "Lodz", "Łódź", "Lódz", "Szczecin", "Lublin", "Bydgoszcz", "Gdynia",
        "Rzeszow", "Rzeszów", "Bialystok", "Białystok", "Torun", "Toruń",
        "Sopot", "Gliwice",
    ),
    "CZ": ("Prague", "Praha", "Brno", "Ostrava", "Plzen", "Plzeň", "Olomouc",
           "Liberec", "Hradec Kralove"),
    "SK": ("Bratislava", "Kosice", "Košice", "Zilina", "Žilina", "Nitra"),
    "HU": ("Budapest", "Debrecen", "Szeged", "Pecs", "Pécs", "Gyor", "Győr",
           "Miskolc"),
    "RO": (
        "Bucharest", "Bucuresti", "București", "Cluj", "Cluj-Napoca", "Iasi",
        "Iași", "Timisoara", "Timișoara", "Brasov", "Brașov", "Sibiu",
        "Constanta", "Constanța", "Craiova", "Oradea",
    ),
    "BG": ("Sofia", "Plovdiv", "Varna", "Burgas", "Ruse"),
    "GR": ("Athens", "Athina", "Thessaloniki", "Patras", "Heraklion", "Larissa"),
    "HR": ("Zagreb", "Split", "Rijeka", "Osijek", "Zadar", "Dubrovnik"),
    "SI": ("Ljubljana", "Maribor", "Celje"),
    "AT": ("Vienna", "Wien", "Graz", "Linz", "Salzburg", "Innsbruck",
           "Klagenfurt", "Villach"),
    "BE": (
        "Brussels", "Bruxelles", "Brussel", "Antwerp", "Antwerpen", "Anvers",
        "Ghent", "Gent", "Leuven", "Louvain", "Liege", "Liège", "Bruges",
        "Brugge", "Mechelen", "Hasselt", "Namur", "Charleroi", "Diegem",
        "Zaventem", "Louvain-la-Neuve",
    ),
    "LU": ("Luxembourg City", "Esch-sur-Alzette", "Kirchberg", "Belval"),
    "CH": (
        "Zurich", "Zürich", "Zuerich", "Geneva", "Genève", "Geneve", "Genf",
        "Basel", "Basle", "Bern", "Berne", "Lausanne", "Lugano", "Zug",
        "Winterthur", "St Gallen", "St. Gallen", "Lucerne", "Luzern",
        "Neuchatel", "Neuchâtel",
    ),
    "IT": (
        "Milan", "Milano", "Rome", "Roma", "Turin", "Torino", "Bologna",
        "Florence", "Firenze", "Naples", "Napoli", "Venice", "Venezia",
        "Genoa", "Genova", "Palermo", "Bari", "Catania", "Verona", "Padua",
        "Padova", "Trento", "Pisa", "Modena", "Parma", "Cagliari", "Trieste",
    ),
    "IS": ("Reykjavik", "Reykjavík", "Kopavogur"),
    "MT": ("Valletta", "Sliema", "St Julians", "Birkirkara", "Gzira"),
    "CY": ("Nicosia", "Limassol", "Larnaca", "Paphos"),
    "LI": ("Vaduz", "Schaan"),
    # Mapped but *not* EU — `filters.countries` never contains these, so they
    # get rejected with an honest "country RS is not in your list" reason.
    "RS": ("Belgrade", "Beograd", "Novi Sad", "Nis", "Niš"),
    "UA": ("Kyiv", "Kiev", "Lviv", "Kharkiv", "Odesa"),
    "TR": ("Istanbul", "Ankara", "Izmir"),
    "BA": ("Sarajevo", "Banja Luka"),
    "MK": ("Skopje",),
    "AL": ("Tirana",),
    "ME": ("Podgorica",),
    "MD": ("Chisinau", "Chișinău"),
    "BY": ("Minsk",),
}

CITY_TO_COUNTRY: dict[str, str] = {}
for _iso, _cities in _CITY_SOURCE.items():
    for _city in _cities:
        _key = normalize_text(_city)
        if _key:
            CITY_TO_COUNTRY.setdefault(_key, _iso)
# "Luxembourg" is both a city and a country; the country alias already covers
# it and resolves to the same code, so no separate city entry is needed.

_CITY_MAX_NGRAM = max(len(k.split()) for k in CITY_TO_COUNTRY)

# City names that are also ordinary English words. Safe inside a location
# field ("Split, Croatia"), far too noisy to treat as a Europe hint when they
# turn up in a job description ("we split the work", "nice to have").
_WEAK_CITY_HINTS: frozenset[str] = frozenset(
    {"split", "bath", "nice", "cork", "reading", "york", "derby", "essen",
     "island", "lund", "brno", "graz", "linz", "bari", "pisa", "zug", "nancy",
     "metz", "faro", "vigo", "nis", "palma", "parma"}
)


# --------------------------------------------------------------------------
# United States detection
# --------------------------------------------------------------------------

US_STATE_CODES: frozenset[str] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
        "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
        "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
        "WI", "WY", "DC", "PR",
    }
)

# Codes that are simultaneously a US state and an EU country: Delaware/Germany
# and Montana/Malta. Which one is meant is decided by the city in front of it
# (see `_ambiguous_code_is_us`) rather than by an ever-growing list of US
# cities — enumerating every town in Delaware is a race you lose, and losing
# it means a US posting passes an EU-only filter.
AMBIGUOUS_STATE_CODES: frozenset[str] = frozenset({"DE", "MT"})

#: Delaware and Montana are small states with a knowable town list. Germany
#: and Malta are not — Germany alone has thousands of towns, and the city
#: table here holds ~34 of them.
#:
#: So the enumeration goes on the SMALL side. Deciding "is this Delaware?" by
#: asking "is the head a German city I happen to know?" got the asymmetry
#: exactly backwards: it bought four Delaware towns and lost every German city
#: off the list — Duisburg, Bochum, Wuppertal, Bielefeld, Mainz, Rostock and
#: a dozen more, all silently, all invisible forever.
_DELAWARE_CITIES: frozenset[str] = frozenset(
    normalize_text(name)
    for name in (
        "Wilmington", "Dover", "Newark", "Middletown", "Smyrna", "Milford",
        "Seaford", "Georgetown", "Elsmere", "New Castle", "Bear", "Glasgow",
        "Brookside", "Hockessin", "Pike Creek", "Claymont", "Lewes",
        "Rehoboth Beach", "Newport", "Camden", "Clayton", "Delaware City",
        "Harrington", "Laurel", "Milton", "Selbyville", "Townsend", "Wyoming",
        "Bridgeville", "Millsboro", "Ocean View", "Greenville", "Christiana",
    )
)

_MONTANA_CITIES: frozenset[str] = frozenset(
    normalize_text(name)
    for name in (
        "Billings", "Missoula", "Great Falls", "Bozeman", "Butte", "Helena",
        "Kalispell", "Havre", "Anaconda", "Miles City", "Belgrade",
        "Livingston", "Laurel", "Whitefish", "Sidney", "Lewistown",
        "Glendive", "Dillon", "Hamilton", "Polson", "Columbia Falls",
        "Hardin", "Deer Lodge", "Cut Bank", "Libby", "Conrad", "Baker",
        "Wolf Point", "Red Lodge", "Bigfork", "Big Sky",
    )
)

_AMBIGUOUS_STATE_CITIES: dict[str, frozenset[str]] = {
    "DE": _DELAWARE_CITIES,
    "MT": _MONTANA_CITIES,
}

#: Location heads that name no city at all, so an ambiguous trailing code has
#: to be read as the country: "Remote, DE" is Germany.
_PLACELESS_HEADS: frozenset[str] = frozenset({
    "", "remote", "remote work", "hybrid", "on site", "onsite", "office",
    "anywhere", "flexible", "multiple", "multiple locations", "various",
    "eu", "europe", "emea", "home", "home office", "distributed",
})

_US_STATE_NAMES: frozenset[str] = frozenset(
    normalize_text(name)
    for name in (
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
        "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
        "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
        "Puerto Rico",
    )
)

# US-only city names — deliberately excludes every name Europe also uses
# (Cambridge, Birmingham, Athens, Naples, Dublin, Berlin, Manchester, ...),
# which are handled by the state-code rule instead.
_US_CITY_HINTS: frozenset[str] = frozenset(
    normalize_text(name)
    for name in (
        "San Francisco", "SF Bay Area", "Bay Area", "Silicon Valley",
        "New York City", "NYC", "Brooklyn", "Manhattan", "Los Angeles",
        "San Jose", "San Diego", "Seattle", "Austin", "Boston", "Chicago",
        "Denver", "Atlanta", "Dallas", "Houston", "Philadelphia", "Phoenix",
        "Detroit", "Minneapolis", "Nashville", "Charlotte", "Columbus",
        "Palo Alto", "Mountain View", "Sunnyvale", "Cupertino", "Menlo Park",
        "Redmond", "Bellevue", "Ann Arbor", "Boulder", "Salt Lake City",
        "Las Vegas", "San Antonio", "Jersey City", "Hoboken", "Pittsburgh",
        "Baltimore", "Raleigh", "Durham", "Tampa", "Orlando", "Kansas City",
        "St Louis", "Saint Louis", "Cincinnati", "Cleveland", "Milwaukee",
        "Sacramento", "Oakland", "Santa Monica", "Santa Clara", "Irvine",
        "Plano", "Wilmington", "Bozeman", "Missoula", "Billings",
        "Washington DC", "Portland Oregon", "Chapel Hill", "Redwood City",
    )
)

_US_MARKER_PHRASES: frozenset[str] = frozenset(
    normalize_text(name)
    for name in (
        "USA", "United States", "United States of America", "U.S.", "U.S.A.",
        "US only", "US based", "US remote", "Remote US", "US citizens",
        "North America", "Americas", "stateside", "anywhere in the US",
        "continental US", "lower 48",
    )
)

# Case-SENSITIVE on purpose: the pronoun "us" is everywhere, the country code
# "US" is not. Matches US, USA, U.S., U.S.A.
_US_ABBREV_RE = re.compile(r"\bU\.?S\.?A?\.?\b")

_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


# --------------------------------------------------------------------------
# tokenising helpers
# --------------------------------------------------------------------------

# Location strings glue several places together. Split on the separators that
# actually appear in the wild; ` or ` / ` and ` are matched lowercase-only so
# "Portland, OR" keeps its state code.
_PHRASE_SPLIT_RE = re.compile(r"[;|/\n·•]|[()\[\]]|\s+-\s+|\s+or\s+|\s+and\s+|\s+&\s+")

# "EMEA (excluding UK)" names the United Kingdom precisely because the job is
# NOT there, and reading it as a UK posting is the wrong answer twice over: it
# files a pan-European role under the one country it excludes, and on the
# common post-Brexit config (GB off the list) it drops the job outright with a
# reason that reads like a parser bug.
#
# The word list is deliberately short. "outside" and "minus" are ambiguous in
# a location field, and "no" is the ISO code for Norway — a wrong entry here
# deletes the country a job really is in, which is worse than the bug.
_EXCLUSION_RE = re.compile(
    r"\b(?:excluding|excl\.?|except(?:\s+for)?|other\s+than|apart\s+from|"
    r"not\s+including)\b[^,;)\]|/]*",
    re.IGNORECASE,
)


def _phrases(location: str) -> list[str]:
    """Break a multi-location string into independently resolvable chunks.

    Exclusion clauses are dropped first: a country named only to be ruled out
    must not become the country the job is filed under.
    """
    text = _EXCLUSION_RE.sub(" ", str(location))
    return [p.strip() for p in _PHRASE_SPLIT_RE.split(text) if p and p.strip()]


def _ngram_hit(tokens: list[str], table: Iterable[str] | Mapping[str, str],
               max_n: int) -> str | None:
    """Longest whole-word n-gram of `tokens` present in `table`.

    Returns the mapped value for a mapping, the matched gram for a set, or
    None. This is the only matching primitive used for names — it can never
    fire on a fragment of a word.
    """
    if not tokens:
        return None
    is_map = isinstance(table, Mapping)
    for size in range(min(max_n, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            gram = " ".join(tokens[start:start + size])
            if is_map:
                value = table.get(gram)  # type: ignore[union-attr]
                if value:
                    return value
            elif gram in table:
                return gram
    return None


def _has_upper_token(raw: str, token: str) -> bool:
    """True when `token` appears SHOUTED in the raw text ("Munich, DE")."""
    return re.search(rf"\b{re.escape(token.upper())}\b", raw) is not None


# --------------------------------------------------------------------------
# US detection
# --------------------------------------------------------------------------


def _part_looks_us(raw_part: str, *, index: int) -> bool:
    """US signal inside one comma-separated part. `index` is its position."""
    norm = normalize_text(raw_part)
    if not norm:
        return False
    tokens = norm.split()
    if _ngram_hit(tokens, _US_MARKER_PHRASES, 5):
        return True
    if _ngram_hit(tokens, _US_STATE_NAMES, 3):
        return True
    if _ngram_hit(tokens, _US_CITY_HINTS, 3):
        return True

    # "Austin, TX" / "Berlin, CT 06037" / a bare "TX". Only the *trailing*
    # token counts, otherwise an all-caps "REMOTE IN GERMANY" would read as
    # Indiana.
    words = [t for t in tokens if not _ZIP_RE.match(t)]
    if words:
        tail = words[-1].upper()
        if len(tail) == 2 and tail in US_STATE_CODES and (index > 0 or len(words) == 1):
            if tail not in AMBIGUOUS_STATE_CODES:
                return True
            # A bare ambiguous code is resolved at phrase level, where the
            # city that disambiguates it is visible.
    return False


def _phrase_looks_us(phrase: str) -> bool:
    if _US_ABBREV_RE.search(phrase):
        return True
    parts = phrase.split(",")
    if any(_part_looks_us(part, index=i) for i, part in enumerate(parts)):
        return True
    # "Newark, DE" — the ambiguous code and the city that disambiguates it sit
    # in different comma parts, so this decision cannot be made part by part.
    return _trailing_ambiguous_code_is_us(parts)


def _trailing_ambiguous_code_is_us(parts: list[str]) -> bool:
    """Is a trailing DE/MT Delaware/Montana rather than Germany/Malta?

    Decided by the city in front of it: "Munich, DE" is Germany because Munich
    is a European city we know, "Newark, DE" and "Dover, DE" are Delaware
    because they are not. Enumerating every town in Delaware is a race you
    lose, and losing it means a US posting passes an EU-only filter.

    An unknown head is read as the *country*, deliberately. The cost of that
    is one US posting reaching the scorer, which reads the location and marks
    it down. The opposite default would silently delete real German jobs from
    any board whose location strings we do not recognise, and a deleted job is
    invisible forever.
    """
    cleaned = [normalize_text(part) for part in parts]
    cleaned = [part for part in cleaned if part]
    if len(cleaned) < 2:
        return False

    tail = cleaned[-1].upper()
    if tail not in AMBIGUOUS_STATE_CODES:
        return False

    head_tokens = " ".join(cleaned[:-1]).split()
    if not head_tokens:
        return False
    if _ngram_hit(head_tokens, CITY_TO_COUNTRY, _CITY_MAX_NGRAM):
        return False                      # a European city -> the country
    if _ngram_hit(head_tokens, COUNTRY_ALIASES, _ALIAS_MAX_NGRAM):
        return False                      # "Germany, DE"
    # Only a town of that actual US state makes this the state. Anything else
    # — including a European city we simply do not know — reads as the
    # country, because the list that can be complete is the small one.
    return _ngram_hit(head_tokens, _AMBIGUOUS_STATE_CITIES[tail], 3)


def looks_like_us(location: str | None) -> bool:
    """True when any part of `location` points at the United States.

    Used to stop the Europe/US city-name collision (Berlin CT, Dublin OH,
    Vienna VA) from leaking American postings into an EU job hunt.
    """
    if not location:
        return False
    return any(_phrase_looks_us(phrase) for phrase in _phrases(str(location)))


# --------------------------------------------------------------------------
# country resolution
# --------------------------------------------------------------------------


def _alias_in(raw: str) -> str | None:
    """Country code named anywhere in `raw`, whole-word."""
    norm = normalize_text(raw)
    if not norm:
        return None
    direct = COUNTRY_ALIASES.get(norm)
    if direct:
        return direct
    tokens = norm.split()
    for size in range(min(_ALIAS_MAX_NGRAM, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            gram = " ".join(tokens[start:start + size])
            iso = COUNTRY_ALIASES.get(gram)
            if not iso:
                continue
            # "it"/"at"/"is"/"no"/"be" are English words. A bare two-letter
            # code buried in a longer phrase is only believable when the
            # source wrote it in caps.
            if size == 1 and len(gram) <= 2 and not _has_upper_token(raw, gram):
                continue
            return iso
    return None


def _phrase_country(phrase: str) -> str | None:
    """Resolve one already-US-cleared location chunk to an ISO code."""
    parts = [p.strip() for p in phrase.split(",") if p.strip()]

    # "Munich, DE" / "remote, de" — the tail of a comma list is the country
    # slot, so a lowercase two-letter code is trustworthy there.
    if len(parts) >= 2:
        tail = COUNTRY_ALIASES.get(normalize_text(parts[-1]))
        if tail:
            return tail

    # A named country beats a city guess ("Toledo, Spain" is not Ohio).
    iso = _alias_in(phrase)
    if iso:
        return iso
    return _ngram_hit(normalize_text(phrase).split(), CITY_TO_COUNTRY, _CITY_MAX_NGRAM)


def country_of(location: str | None) -> str | None:
    """ISO-3166 alpha-2 country for a free-text location, or None.

    Returns None for anything that reads as American ("Berlin, CT",
    "Remote (US)") and for pure abstractions ("Remote", "EMEA", "Europe") —
    `mentions_eu` is the right question for those.

        >>> country_of("Berlin, Germany"), country_of("Munich, DE")
        ('DE', 'DE')
        >>> country_of("Zürich"), country_of("London, UK")
        ('CH', 'GB')
        >>> country_of("Berlin, CT") is None
        True
    """
    if not location:
        return None
    text = str(location)
    for phrase in _phrases(text):
        if _phrase_looks_us(phrase):
            continue  # segment is American — try the next one
        iso = _phrase_country(phrase)
        if iso:
            return iso
    return None


def countries_of(location: str | None) -> list[str]:
    """*Every* country a location names, best guess first, US segments dropped.

    "Remote (Portugal, Spain, Poland)" is one job you may take from any of
    three countries, and `country_of` can only ever answer with one of them —
    so an applicant in Lisbon whose `filters.countries` is [PT, ES] would
    never see it. This is the list `filters.passes_location` gates on; the
    first element is always exactly what `country_of` returns, so nothing that
    reads the primary country changes behaviour.

        >>> countries_of("Remote (Portugal, Spain, Poland)")
        ['PL', 'PT', 'ES']
        >>> countries_of("Berlin, Germany")
        ['DE']
    """
    if not location:
        return []
    found: list[str] = []
    primary = country_of(location)
    if primary:
        found.append(primary)
    for phrase in _phrases(str(location)):
        if _phrase_looks_us(phrase):
            continue
        # Per comma-part, so every country in a list is seen and not just the
        # one in the country slot at the end.
        for part in phrase.split(","):
            iso = _phrase_country(part)
            if iso and iso not in found:
                found.append(iso)
    return found


# --------------------------------------------------------------------------
# remote detection
# --------------------------------------------------------------------------

_SEP = "\x00"  # token that can never appear in normalised text

# Single tokens that mean "work arrangement: remote".
_REMOTE_TOKENS: frozenset[str] = frozenset(
    {"remote", "remotely", "wfh", "telecommute", "telecommuting", "telework",
     "teleworking", "anywhere", "homeoffice"}
)

# "Remote sensing engineer" is an on-site job about satellites.
_NOT_A_WORKPLACE: frozenset[str] = frozenset(
    {"sensing", "sensor", "sensors", "monitoring", "control", "controls",
     "controlled", "desktop", "access", "imaging", "telemetry", "diagnostics",
     "surgery", "surgical", "patient", "procedure", "detonation"}
)

# "distributed systems" is a skill, "distributed team" is a workplace.
_DISTRIBUTED_TECH: frozenset[str] = frozenset(
    {"system", "systems", "computing", "computation", "database", "databases",
     "ledger", "cache", "caching", "tracing", "architecture", "architectures",
     "transaction", "transactions", "consensus", "storage", "sql", "data",
     "services", "training", "locking", "queue", "queues"}
)

_STRONG_REMOTE_PHRASES: tuple[tuple[str, ...], ...] = tuple(
    tuple(normalize_text(p).split())
    for p in (
        "fully remote", "100% remote", "remote first", "remote only",
        "work from home", "work from anywhere", "working from home",
        "home based", "home office", "remote position", "remote role",
        "remote job", "remote opportunity", "remotely", "telecommute",
        "telecommuting", "wfh", "distributed team", "fully distributed",
        "remote friendly", "remote within",
    )
)

_HYBRID_PHRASES: tuple[tuple[str, ...], ...] = tuple(
    tuple(normalize_text(p).split()) for p in ("hybrid", "part remote", "partially remote")
)


def _contains_phrase(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    if not n or n > len(tokens):
        return False
    return any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1))


def _any_phrase(tokens: list[str], phrases: Iterable[tuple[str, ...]]) -> bool:
    return any(_contains_phrase(tokens, p) for p in phrases)


def _remote_word_hit(tokens: list[str]) -> bool:
    """A bare remote-ish word that is not part of a technical phrase."""
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in _REMOTE_TOKENS and following not in _NOT_A_WORKPLACE:
            return True
        if token == "distributed" and following not in _DISTRIBUTED_TECH:
            return True
    return False


def is_remote(location: str | None, title: str = "", description: str = "") -> bool:
    """True when the posting is a remote work arrangement.

    Location and title are read in full; the description only counts when it
    states the arrangement explicitly ("fully remote", "work from home"),
    because half of all engineering descriptions mention "distributed
    systems" or "remote monitoring" without offering remote work at all.

    "Hybrid" is not remote — it needs you in the office.
    """
    loc_tokens = normalize_text(location).split()
    title_tokens = normalize_text(title).split()
    desc_tokens = normalize_text(description).split()
    surface = loc_tokens + [_SEP] + title_tokens

    if _any_phrase(surface, _STRONG_REMOTE_PHRASES):
        return True
    # A hybrid label on the location/title wins over anything the description
    # says: the structured field is the authoritative statement of the
    # arrangement, the prose is usually boilerplate about the company.
    if _any_phrase(surface, _HYBRID_PHRASES):
        return False
    if _any_phrase(desc_tokens, _STRONG_REMOTE_PHRASES):
        return True
    return _remote_word_hit(surface)


# --------------------------------------------------------------------------
# "is Europe mentioned at all?"
# --------------------------------------------------------------------------

_SHORT_EU_HINTS: frozenset[str] = frozenset(
    {"eu", "eea", "emea", "uk", "cet", "cest", "eet", "eest", "efta", "schengen"}
)

# Country names/demonyms usable as a hint inside prose. Bare ISO codes are
# excluded (too noisy) and so are the two words that are far more often
# English than geography.
_HINT_EXCLUDED: frozenset[str] = frozenset({"english", "polish", "island", "britain"})

_EU_HINT_TERMS: dict[str, str] = {
    alias: iso
    for alias, iso in COUNTRY_ALIASES.items()
    if iso in EU_COUNTRIES and len(alias) >= 4 and alias not in _HINT_EXCLUDED
}
for _extra in ("europe", "european", "european union", "europewide", "eurozone",
               "central europe", "western europe", "northern europe",
               "southern europe", "eastern europe", "eu timezone",
               "eu timezones", "eu time zone", "european timezone",
               "european time zone", "cet timezone", "eu remote",
               "remote europe", "europe remote", "eu wide", "benelux",
               "nordics", "dach", "iberia"):
    _EU_HINT_TERMS[normalize_text(_extra)] = "EU"

_HINT_MAX_NGRAM = max(len(k.split()) for k in _EU_HINT_TERMS)

# "UTC+1" / "GMT +2" / "UTC+01:00" — European offsets only. Run against the
# raw text because normalisation eats the sign.
_TZ_OFFSET_RE = re.compile(r"\b(?:UTC|GMT)\s*\+\s*0?([0-3])\b", re.IGNORECASE)


def mentions_eu(text: str | None) -> bool:
    """True when `text` hints at Europe: a country, a city, EU/EEA/EMEA,
    a European timezone, or a CET/UTC+1-style offset.

    Deliberately permissive: this gates `filters.remote_requires_eu_hint`,
    where a false positive costs one scoring call and a false negative loses
    a real job.
    """
    if not text:
        return False
    raw = str(text)
    if _TZ_OFFSET_RE.search(raw):
        return True

    tokens = normalize_text(raw).split()
    if not tokens:
        return False
    if _SHORT_EU_HINTS.intersection(tokens):
        return True
    if _ngram_hit(tokens, _EU_HINT_TERMS, _HINT_MAX_NGRAM):
        return True

    # A named European city ("our Berlin office") is a hint too, minus the
    # city names that double as ordinary English words.
    for size in range(min(_CITY_MAX_NGRAM, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            gram = " ".join(tokens[start:start + size])
            iso = CITY_TO_COUNTRY.get(gram)
            if iso and iso in EU_COUNTRIES and gram not in _WEAK_CITY_HINTS:
                return True
    return False


# --------------------------------------------------------------------------
# one-shot resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GeoResult:
    """Everything `filters.passes_location` needs, computed once."""

    country: str | None = None
    remote: bool = False
    eu_hint: bool = False
    us: bool = False
    #: Every country the posting names, best guess first. `country` is the
    #: first entry; a multi-country remote role is the reason both exist.
    countries: tuple[str, ...] = ()
    #: Europe named in the *structured* fields — location or title — rather
    #: than anywhere in the prose. `eu_hint` includes the description, which
    #: is far weaker evidence: half of all US company descriptions mention
    #: their European offices.
    eu_stated: bool = False

    @property
    def in_eu(self) -> bool:
        return is_eu(self.country)


def _field(job_like: Any, name: str) -> str:
    """Read `name` off a Job, a dict, or anything with attributes."""
    if isinstance(job_like, Mapping):
        return str(job_like.get(name) or "")
    return str(getattr(job_like, name, "") or "")


def resolve(job_like: Any) -> GeoResult:
    """Resolve a job-like object (Job, dict or namespace) in one pass.

    Keeps `filters.py` free of geo plumbing: country, remote flag, EU hint
    and the US marker all come back together.
    """
    if isinstance(job_like, str):
        location, title, description, declared = job_like, "", "", None
        stated = None
    else:
        location = _field(job_like, "location")
        title = _field(job_like, "title")
        description = _field(job_like, "description")
        declared = (
            job_like.get("remote") if isinstance(job_like, Mapping)
            else getattr(job_like, "remote", None)
        )
        stated = _field(job_like, "country").strip().upper()
        # Only an ISO-3166 alpha-2 shape is believed. Sources write this
        # field, so a stray "Europe" or "n/a" would otherwise be reported to
        # the user as the country the job is in.
        stated = stated if len(stated) == 2 and stated.isalpha() else None

    # The veto reads the title too. Reading only the location while the EU
    # rescue reads location + title + description is half a check: a posting
    # with location "Remote" and title "Backend Engineer (Remote - US)" was
    # rescued by any European city mentioned in its description.
    us = looks_like_us(location) or looks_like_us(title)
    countries = countries_of(location)
    if not countries and not us:
        # Some boards leave `location` empty and only say "Remote - Germany"
        # in the title. Cheap second look; never overrides an explicit US.
        countries = countries_of(title)
    if not countries and not us and stated:
        # The source already knows. Adzuna indexes by country, so a result
        # from `.../jobs/de/...` is a German posting even when its `location`
        # node is missing entirely — and re-deriving the country from free
        # text throws that away and rejects the job as "could not be
        # resolved". Only consulted when the text says nothing at all: a
        # declared country must never override a location that reads US, or a
        # mis-stamped source would walk a US posting past an EU-only filter.
        countries = [stated]
    country = countries[0] if countries else None

    # A source that already decided (`Job.remote`) is trusted over guessing.
    remote = declared if isinstance(declared, bool) else is_remote(
        location, title, description
    )

    eu_stated = (any(c in EU_COUNTRIES for c in countries)
                 or mentions_eu(f"{location}\n{title}"))
    eu_hint = eu_stated or mentions_eu(description)

    return GeoResult(country=country, remote=remote, eu_hint=eu_hint, us=us,
                     countries=tuple(countries), eu_stated=eu_stated)
