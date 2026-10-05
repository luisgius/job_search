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
