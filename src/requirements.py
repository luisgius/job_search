"""Conservative, quoted job evidence; missing requirements remain unknown."""
from __future__ import annotations

import re
from .util import html_to_text

LANGUAGES = {
    "en": r"english|englisch(?:kenntnisse)?",
    "es": r"spanish|español|castellano",
    "de": r"german|deutsch(?:kenntnisse)?",
    "pl": r"polish|polski|polskiego",
    "fr": r"french|français",
    # Added 2026-10-23 with `filters.work_languages`: every language a posting
    # in the search area (Europe, Singapore, Taiwan) commonly asks for.
    "nl": r"dutch|nederlands",
    "it": r"italian|italiano",
    "pt": r"portuguese|português",
    "da": r"danish|dansk",
    "sv": r"swedish|svenska",
    "no": r"norwegian|norsk",
    "fi": r"finnish|suomi",
    "cs": r"czech|čeština",
    "sk": r"slovak|slovenčina",
    "hu": r"hungarian|magyar",
    "ro": r"romanian|română",
    "el": r"greek",
    "lt": r"lithuanian",
    "lv": r"latvian",
    "et": r"estonian",
    "uk": r"ukrainian",
    "ru": r"russian",
    "zh": r"mandarin|chinese|cantonese",
    "ja": r"japanese",
    "ko": r"korean",
    "ms": r"malay|bahasa melayu",
}

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "de": "German", "pl": "Polish", "fr": "French",
    "nl": "Dutch", "it": "Italian", "pt": "Portuguese", "da": "Danish",
    "sv": "Swedish", "no": "Norwegian", "fi": "Finnish", "cs": "Czech",
    "sk": "Slovak", "hu": "Hungarian", "ro": "Romanian", "el": "Greek",
    "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian", "uk": "Ukrainian",
    "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ms": "Malay",
}
_LANGUAGE = r"\b(?:" + "|".join(LANGUAGES.values()) + r")\b"
_GROUP = rf"{_LANGUAGE}(?:\s+(?:and|&)\s+{_LANGUAGE})*"
_PROFICIENCY = (r"fluent|fluency|fließend\w*|native(?:[- ]level)?|business[- ]level|"
                r"professional(?:[- ]level)?|excellent|proficient|proficiency|sehr gute|b2|c1|c2|bilingual")
_LEVEL = rf"(?:{_PROFICIENCY}|a1|a2|b1|basic|beginner|intermediate)"
_OPTIONAL = re.compile(r"\b(?:optional|preferred|bonus|advantage|plus|nice.to.have|helpful|desirable)\b", re.I)
_NEGATED = re.compile(r"\b(?:no|not|without|kein\w*|nicht)\b", re.I)
_MANDATORY = re.compile(r"\b(?:required|mandatory|essential|non.negotiable|must|require|erforderlich)\b", re.I)


def language_requirements(description: str) -> list[dict[str, str]]:
    """Bind proficiency to a language phrase, never to nearby unrelated words.

    Ambiguous, optional, negated and alternative-language claims stay unknown.
    Coordinated bare language names share a directly attached proficiency.
    """
    found = []
    text = html_to_text(description)
    # A trailing modifier belongs to the preceding claim ("German, optional").
    clauses = re.split(r"[\n.;!?]|,(?!\s*(?:optional|preferred|not|no)\b)|\bbut\b|\bwhereas\b", text, flags=re.I)
    for clause in clauses:
        # An alternative between languages does not require either individually.
        if re.search(rf"{_LANGUAGE}\s+or\s+{_LANGUAGE}", clause, re.I):
            continue
        # Split independent conjunctions, but retain bare language coordination.
        coordinated = re.sub(rf"({_LANGUAGE})\s+and\s+(?={_LANGUAGE})", r"\1 & ", clause, flags=re.I)
        parts = re.split(r"\s+and\s+", coordinated, flags=re.I)
        for part in parts:
            if _OPTIONAL.search(part) or _NEGATED.search(part):
                continue
            # A benefit or a colleague's proficiency is not an applicant
            # requirement. Keep this local so independent requirements survive.
            learning_benefit = (
                re.search(r"\b(?:courses?|classes|lessons?|training)\b", part, re.I)
                and re.search(r"\b(?:offer\w*|provid\w*|available|free|paid)\b", part, re.I)
            )
            colleague_context = (
                re.search(r"\b(?:collaborat\w*|work\w*|interact\w*)\s+(?:closely\s+)?with\b", part, re.I)
                and re.search(r"\b(?:speakers?|colleagues?|coworkers?|team|partners?)\b", part, re.I)
            )
            if learning_benefit or colleague_context:
                continue
            for group in re.finditer(_GROUP, part, re.I):
                before, after = part[:group.start()], part[group.end():]
                prefix = re.search(rf"\b({_LEVEL})(?:\s+(?:or|and)\s+{_LEVEL})*(?:\s+(?:speaker|speakers))?(?:\s+(?:in|of))?\s*$", before, re.I)
                suffix = re.match(rf"\s*(?:(?:language\s+)?(?:skills|knowledge|proficiency)\s*)?(?:(?:is|are|at|at least|at level|level)\s+)?({_LEVEL})\b", after, re.I)
                level = (prefix or suffix)
                professional = bool(level and re.fullmatch(_PROFICIENCY, level.group(1), re.I))
                # "Excellent benefits" is not a proficiency descriptor, even
                # when directly after a language mentioned in company prose.
                if suffix and re.match(r"\s*(?:is\s+)?excellent\s+(?!command\b|knowledge\b|skills\b|proficiency\b)\w+", after, re.I):
                    professional = False
                    level = None
                if not level and not _MANDATORY.search(part):
                    continue
                for code, names in LANGUAGES.items():
                    if re.search(rf"\b(?:{names})\b", group.group(), re.I):
                        member_professional = professional
                        # A level directly following the final language owns
                        # that language, rather than inheriting the prefix of
                        # the first: "Fluent English and German A2".
                        if level and prefix and suffix and re.search(rf"\b(?:{names})$", group.group(), re.I):
                            member_professional = bool(re.fullmatch(_PROFICIENCY, suffix.group(1), re.I))
                        found.append({"language": code,
                                      "requirement": "professional" if member_professional else "level_unknown",
                                      "evidence": clause.strip()[:400]})
    return found


#: A "level_unknown" claim is a language requirement — not "experience in
#: the German market is required" — when the clause talks about language, or
#: the language name sits right against the mandatory word.
_LANGUAGE_CONTEXT = re.compile(
    r"\b(?:language|languages|speak\w*|spoken|written|writing|fluen\w*|"
    r"proficien\w*|skills?|knowledge|communicat\w*|level|native|verbal|"
    r"kenntnisse|sprach\w*)\b",
    re.I,
)
_ATTACHED_MANDATORY = re.compile(
    rf"{_LANGUAGE}\s*(?:is\s+|are\s+)?(?:required|mandatory|essential|a\s+must|must)\b"
    rf"|\b(?:required|mandatory|must\s+have|must\s+speak)\s*:?\s*{_LANGUAGE}",
    re.I,
)


def is_required(requirement: dict[str, str]) -> bool:
    """Does this extracted claim demand the language, rather than mention it?

    A proficiency word bound to the language ("fluent French") always does.
    A bare mandatory claim ("German required", "German language skills are
    essential") does when it reads as language; "experience in the German
    market is required" does not.
    """
    if requirement.get("requirement") == "professional":
        return True
    evidence = str(requirement.get("evidence") or "")
    return bool(_LANGUAGE_CONTEXT.search(evidence) or _ATTACHED_MANDATORY.search(evidence))


#: Requirements phrased in the language itself — the way a French, German,
#: Polish, Dutch, Italian or Portuguese ad states them. Each is a demand by
#: construction (a proficiency word or a mandatory word bound to the name).
_NATIVE_PHRASES: dict[str, re.Pattern[str]] = {
    "fr": re.compile(
        r"\b(?:ma[iî]trise|courant|bilingue|parfait\w*|excellent\w*|bon(?:ne)?\s+niveau)"
        r"\b[^.\n;:]{0,30}?\bfran[cç]ais\w*"
        r"|\bfran[cç]ais\w*\s+(?:courant|bilingue|exig[ée]e?|requise?|obligatoire|"
        r"indispensable|imp[ée]ratif|\(?(?:c1|c2|b2)\)?)", re.I),
    "de": re.compile(
        r"\b(?:sehr\s+gute|gute|flie(?:ß|ss)end\w*|verhandlungssicher\w*|ausgezeichnet\w*|"
        r"exzellent\w*)\s+deutsch\w*"
        r"|\bdeutsch\w*\s+(?:in\s+wort\s+und\s+schrift|flie(?:ß|ss)end|verhandlungssicher|"
        r"(?:auf\s+)?\(?(?:c1|c2|b2)\)?|(?:ist\s+|sind\s+)?(?:erforderlich|zwingend|vorausgesetzt))",
        re.I),
    "pl": re.compile(
        r"\b(?:biegł\w*|płynn\w*|bardzo\s+dobr\w*|dobr\w*|komunikatywn\w*)\s+"
        r"(?:znajomoś\w+\s+)?(?:język\w*\s+)?polsk\w*"
        r"|\bznajomoś\w+\s+język\w*\s+polsk\w*"
        r"|\bjęzyk\w*\s+polsk\w*\s+(?:na\s+poziomie|w\s+stopniu|\(?(?:c1|c2|b2)\)?|ojczyst\w*)",
        re.I),
    "nl": re.compile(
        r"\b(?:vloeiend\w*|uitstekend\w*|goed\w*|perfect\w*)\s+"
        r"(?:beheersing\s+van\s+(?:de\s+|het\s+)?)?nederlands\w*"
        r"|\bnederlands\w*\s+(?:vloeiend|is\s+een\s+must|vereist|verplicht|\(?(?:c1|c2|b2)\)?)",
        re.I),
    "it": re.compile(
        r"\b(?:ottim\w*|buon\w*|fluente|madrelingua|perfett\w*)\s+"
        r"(?:conoscenza\s+(?:della\s+lingua\s+)?)?italian\w*"
        r"|\bitalian\w*\s+(?:fluente|madrelingua|obbligatori\w*|richiest\w*)", re.I),
    "pt": re.compile(
        r"\b(?:fluente|flu[eê]ncia|dom[ií]nio|nativo)\s+(?:em\s+|de\s+|do\s+)?portugu[eê]s"
        r"|\bportugu[eê]s\s+(?:fluente|nativo|obrigat[oó]rio)", re.I),
}
#: Optional, in the languages the native phrases are written in.
_OPTIONAL_NATIVE = re.compile(
    r"\b(?:mile\s+widzian\w*|atut\w*|plusem|un\s+plus|appr[ée]ci[ée]e?|souhait[ée]e?|"
    r"von\s+vorteil|w[üu]nschenswert|pluspunt|een\s+pr[ée]|gradit[aoe]|preferibil\w*|"
    r"desej[áa]vel|valoriza\w*|optional|preferred|plus|bonus|advantage)\b", re.I)
#: "... or English" in the same clause: an alternative, not a demand.
_ALTERNATIVE_NATIVE = re.compile(
    r"\b(?:ou|or|oder|lub|albo|of|o|ou\s+bien)\s+(?:en\s+|in\s+)?(?:anglais|english|englisch|"
    r"angielski\w*|engels|inglese|ingl[eê]s)\b"
    r"|\b(?:anglais|english|englisch|angielski\w*|engels|inglese|ingl[eê]s)\s+"
    r"(?:ou|or|oder|lub|albo|of|o)\b", re.I)


def native_language_requirements(description: str) -> list[dict[str, str]]:
    """Requirements written in the language they demand ("maîtrise du
    français", "biegła znajomość języka polskiego"), which the English-phrased
    extractor above cannot read. Same shape as `language_requirements`."""
    text = html_to_text(description)
    found = []
    for clause in re.split(r"[\n.;!?]", text):
        if _OPTIONAL_NATIVE.search(clause) or _ALTERNATIVE_NATIVE.search(clause):
            continue
        for code, pattern in _NATIVE_PHRASES.items():
            if pattern.search(clause):
                found.append({"language": code, "requirement": "professional",
                              "evidence": clause.strip()[:400]})
    return found


#: Function words distinctive enough to tell an ad's language. Words shared
#: between languages ("de", "en", "a", "con", "para") are left out on purpose.
_STOPWORDS: dict[str, frozenset[str]] = {
    code: frozenset(words.split()) for code, words in {
        "en": "the and with you will our your are this that have from we be is of for "
              "an as on at about who what experience team work",
        "es": "el los las del para por como más nuestro nuestra equipo buscamos "
              "conocimientos también sobre ofrecemos años trabajo experiencia",
        "fr": "le les des du et est pour avec dans vous nous sur votre notre au aux "
              "sont être expérience équipe poste cette qui",
        "de": "der die das und ist mit für wir sie ein eine den dem auf bei zu von "
              "nicht oder ihre unser unsere erfahrung kenntnisse sowie",
        "pl": "się oraz jest dla nie od jak lub przez które który która doświadczenie "
              "znajomość pracy będzie oferujemy wymagania zespół",
        "nl": "het een van voor met zijn wij je jij onze ons bij naar ook niet werk "
              "ervaring kennis wat hoe als dat",
        "it": "il lo gli della delle dei degli per che sono nel nella alla esperienza "
              "lavoro nostro nostra siamo anche cerchiamo conoscenza offriamo",
        "pt": "não você nossa nosso uma com experiência trabalho equipe ao aos das "
              "dos pelo pela são está conhecimento oferecemos vaga",
        "sv": "och att är för med som på av vi du det inte har kommer erfarenhet "
              "arbete din våra vår",
        "da": "og er med som på af vi du det ikke har vil erfaring arbejde din vores til",
        "fi": "ja on että tai kanssa sinä meidän työ kokemus osaaminen olet olemme",
        "cs": "je se na pro jako nebo který která které zkušenosti práce znalost nabízíme",
    }.items()
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_SCRIPTS = (("zh", re.compile(r"[一-鿿]")),
            ("ja", re.compile(r"[぀-ヿ]")),
            ("ko", re.compile(r"[가-힯]")))


def posting_language(description: str) -> str | None:
    """The language an ad is written in, when it is unmistakably one that is
    not English and the ad carries no English version; else None.

    Conservative on purpose — this can reject a job: the winner needs 20
    function words and twice the runner-up, or (a short ad) 6 and three times
    the runner-up; English must stay under a quarter of the winner (a Polish
    ad with a full English section is bilingual, not Polish-only). A script
    language wins on 30% of letters, unless English is substantial too.
    """
    text = html_to_text(description)
    letters = sum(ch.isalpha() for ch in text)
    if letters < 300:
        return None
    words = [w.lower() for w in _WORD_RE.findall(text)]
    hits = {code: sum(w in stop for w in words) for code, stop in _STOPWORDS.items()}
    for code, script in _SCRIPTS:
        if len(script.findall(text)) >= 0.3 * letters and hits["en"] < 20:
            return code
    ranked = sorted(hits.items(), key=lambda item: -item[1])
    (best, top), (_second, runner) = ranked[0], ranked[1]
    clear = (top >= 20 and top >= 2 * runner) or (top >= 6 and top >= 3 * max(runner, 1))
    if best == "en" or not clear or hits["en"] >= 0.25 * top:
        return None
    return best


def adjacent_role_evidence(description: str) -> dict[str, object]:
    """Evidence for product/decision/experimentation analytics, not any analyst."""
    text = html_to_text(description).lower()
    methods = bool(re.search(r"\ba[/ -]b\b|\bexperimenta?\w*|\bcausal\b", text))
    product = bool(re.search(r"\b(product metrics|product analytics|funnels?|retention|conversion)\b", text))
    quantitative = bool(re.search(r"\b(sql|python|statistics|statistical)\b", text))
    # Only a clear minimum, never a range such as 2–5 years or a preferred skill.
    high_minimum = any(re.search(r"\b(?<![.\d–-])(?:[5-9]|[1-9]\d+)\+?\s+years?\s+(?:of\s+)?(?:(?:relevant|professional|data|analytics|analysis|product|industry)\s+){0,3}experience", line)
                       for line in re.split(r"[\n.;,]|\band\b", text)
                       if not _NEGATED.search(line)
                       and not re.search(r"preferred|bonus|plus|nice.to.have|\d\s*[-–]\s*\d", line))
    return {"compatible_functions": quantitative and (methods or product),
            "high_experience_minimum": high_minimum,
            "experience_requirement": "5+ years" if high_minimum else "unknown_or_not_a_high_minimum"}
