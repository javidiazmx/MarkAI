"""Deterministic guardrails: fixed wording, topic flags, and answer post-processing.

The system prompt is the primary enforcement; these heuristics are the backstop that
guarantees the legal disclaimer and the fair-housing refusal actually appear.
"""

from __future__ import annotations

import re

LEGAL_DISCLAIMER = (
    "I'm not a lawyer, and this isn't legal advice. You should consult with an Illinois "
    "real estate attorney to confirm this applies to your situation."
)
NOT_COVERED_PHRASE = "That's not covered in my training materials."
IDENTITY_NOTICE = (
    "Mark is an AI assistant built in the style of Mark Ainley (Straight Up Chicago "
    "Investor). It is not Mark Ainley, it is not a lawyer, and its answers are not "
    "reviewed by him or by an attorney."
)
REFUSAL_TEXT = (
    "I can't help with that one. If it's a Chicagoland landlording question, try "
    "rephrasing it and I'll take another swing."
)
HIGH_RISK_RESPONSE = (
    "I can't help with that one. Screening criteria have to be applied the same way to "
    "every applicant and can't be based on a protected characteristic, and removing a "
    "tenant has to go through the court process.\n\n"
    "What I can help with: lawful, uniform screening criteria (income ratio, credit, "
    "rental history, applied identically to everyone) and the eviction steps my sources "
    "cover."
)

FLAG_LEGAL = "legal_topic"
FLAG_GEO_OUT = "geo_out_of_scope"
FLAG_GEO_IL_NON_CHICAGO = "geo_illinois_non_chicagoland"
FLAG_HIGH_RISK = "high_risk_request"
FLAG_FOLLOW_UP = "follow_up"

_LEGAL_TERMS = (
    "evict",
    "eviction",
    "lease",
    "sublease",
    "security deposit",
    "deposit interest",
    "rlto",
    "crlto",
    "rtlo",
    "fair housing",
    "discriminat",
    "protected class",
    "notice to quit",
    "5-day notice",
    "five-day notice",
    "30-day notice",
    "10-day notice",
    "court",
    "ordinance",
    "statute",
    "illegal",
    "lawsuit",
    "attorney",
    "lawyer",
    "code violation",
    "habitability",
    "late fee",
    "lockout",
    "lock out",
    "abandonment",
    "section 8",
    "housing voucher",
    "source of income",
    "tenant rights",
    "landlord tenant act",
    "just cause",
    "rent control",
    "sealed eviction",
    "small claims",
    "liability",
    "legally",
)

_CHICAGOLAND = (
    "chicago",
    "chicagoland",
    "cook county",
    "dupage",
    "lake county",
    "will county",
    "kane county",
    "mchenry",
    "kendall",
    "evanston",
    "oak park",
    "naperville",
    "cicero",
    "berwyn",
    "skokie",
    "aurora",
    "joliet",
    "elgin",
    "schaumburg",
    "arlington heights",
    "des plaines",
    "wheaton",
    "bolingbrook",
    "orland park",
    "tinley park",
    "palatine",
    "hoffman estates",
    "downers grove",
    "elmhurst",
    "lombard",
    "berwyn",
    "logan square",
    "pilsen",
    "bridgeport",
    "rogers park",
    "hyde park",
    "avondale",
    "humboldt park",
    "uptown",
    "lincoln park",
    "austin",
    "englewood",
    "bronzeville",
    "albany park",
    "portage park",
    "jefferson park",
    "south shore",
    "woodlawn",
    "garfield park",
    "little village",
)

# Illinois places that are outside Chicagoland.
_IL_NON_CHICAGOLAND = (
    "springfield",
    "peoria",
    "rockford",
    "champaign",
    "urbana",
    "bloomington-normal",
    "decatur",
    "carbondale",
    "quad cities",
    "moline",
    "rock island",
    "galesburg",
    "quincy",
    "effingham",
    "southern illinois",
    "central illinois",
    "downstate",
)

_STATES = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
)

_OUT_OF_STATE_CITIES = (
    "indianapolis",
    "milwaukee",
    "detroit",
    "cleveland",
    "st. louis",
    "saint louis",
    "st louis",
    "kansas city",
    "atlanta",
    "phoenix",
    "dallas",
    "houston",
    "austin, tx",
    "denver",
    "tampa",
    "miami",
    "nashville",
    "memphis",
    "columbus",
    "cincinnati",
    "louisville",
    "minneapolis",
    "gary, indiana",
    "hammond, indiana",
    "los angeles",
    "san francisco",
    "seattle",
    "portland",
    "philadelphia",
    "boston",
    "new york city",
    "brooklyn",
)

# "Indiana Ave" is a Chicago street; only a state *context* counts as out of scope.
_STATE_CONTEXT = re.compile(
    r"\b(?:in|from|to|out\s+in|over\s+in|move[d]?\s+to|my|our|a|another|buying\s+in|"
    r"property\s+in|rental\s+in|unit\s+in|invest(?:ing)?\s+in)\s+"
    r"(" + "|".join(re.escape(s) for s in _STATES) + r")\b",
    re.IGNORECASE,
)
_STATE_POSSESSIVE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in _STATES) + r")\s+"
    r"(?:rental|rentals|property|properties|landlord|landlords|law|laws|tenant|tenants|"
    r"market|duplex|triplex|house|home|apartment|building|lease|leases|eviction|deal|deals)\b",
    re.IGNORECASE,
)
_STREET_SUFFIX = re.compile(
    r"\b(?:ave|avenue|st|street|blvd|boulevard|rd|road|dr|drive|pkwy|parkway|ct|court|"
    r"pl|place|ln|lane|way|hwy|highway)\b\.?",
    re.IGNORECASE,
)

_PROTECTED = (
    "race",
    "racial",
    "black",
    "white tenants",
    "hispanic",
    "latino",
    "asian",
    "color",
    "religion",
    "muslim",
    "jewish",
    "christian",
    "national origin",
    "immigrant",
    "immigration status",
    "undocumented",
    "accent",
    "foreigner",
    "sex",
    "gender",
    "transgender",
    "gay",
    "lesbian",
    "sexual orientation",
    "gender identity",
    "familial status",
    "families with children",
    "family with kids",
    "with children",
    "with kids",
    "single mother",
    "single mom",
    "pregnant",
    "disability",
    "disabled",
    "handicap",
    "wheelchair",
    "service animal",
    "emotional support animal",
    "mental illness",
    "source of income",
    "section 8",
    "housing choice voucher",
    "housing voucher",
    "cha ",
    "subsidy",
    "welfare",
    "age",
    "elderly",
    "senior",
    "marital status",
    "military status",
    "veteran",
    "arrest record",
    "criminal record",
    "felon",
    "ex-offender",
)

_EXCLUSION_VERBS = (
    "screen out",
    "screen them out",
    "filter out",
    "avoid",
    "avoid renting",
    "reject",
    "deny",
    "denying",
    "keep out",
    "keep them out",
    "not rent to",
    "won't rent to",
    "wont rent to",
    "refuse",
    "refusing",
    "discourage",
    "steer",
    "steering",
    "only rent to",
    "prefer not to rent",
    "get rid of",
    "weed out",
    "exclude",
    "turn away",
    "turn down",
    "say no to",
    "stop renting to",
    "block",
)

_SELF_HELP = (
    "change the locks",
    "changing the locks",
    "change locks",
    "lock them out",
    "lock him out",
    "lock her out",
    "lockout the tenant",
    "shut off the utilities",
    "shut off utilities",
    "shut off the heat",
    "shut off heat",
    "turn off the water",
    "turn off the heat",
    "turn off utilities",
    "cut the power",
    "cut off the electricity",
    "remove the door",
    "take the door off",
    "throw out their belongings",
    "throw out his belongings",
    "throw out her belongings",
    "toss their stuff",
    "remove their stuff",
    "without a court order",
    "without going to court",
    "without an eviction",
    "force them out myself",
    "make them leave myself",
)

_FOLLOW_UP_CUES = (
    "what about",
    "how about",
    "and what",
    "and how",
    "does that",
    "is that",
    "can i still",
    "same question",
    "what if",
    "why not",
    "how so",
    "and in",
    "ok and",
    "okay and",
)
_PRONOUN_START = re.compile(r"^\s*(it|that|they|those|this|these|he|she|them)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9']+")


def normalize_quotes(text: str) -> str:
    """Fold curly quotes to ASCII so string comparisons survive model typography."""
    return text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_quotes(text)).strip().lower()


def is_legal_topic(text: str) -> bool:
    """True when the text touches a legal matter that needs the disclaimer."""
    low = _norm(text)
    return any(term in low for term in _LEGAL_TERMS)


def _mentions_chicagoland(low: str) -> bool:
    return any(place in low for place in _CHICAGOLAND)


def assess_geography(text: str) -> str | None:
    """Classify the geography of a question, or return ``None`` when it is in scope."""
    low = _norm(text)

    for city in _OUT_OF_STATE_CITIES:
        if city in low:
            return FLAG_GEO_OUT

    for match in list(_STATE_CONTEXT.finditer(low)) + list(_STATE_POSSESSIVE.finditer(low)):
        state = match.group(1).lower()
        if state == "illinois":
            continue
        tail = low[match.end() : match.end() + 12]
        if _STREET_SUFFIX.match(tail.strip()):
            continue  # "on Indiana Ave" is a Chicago street, not the state of Indiana
        return FLAG_GEO_OUT

    if any(place in low for place in _IL_NON_CHICAGOLAND) and not _mentions_chicagoland(low):
        return FLAG_GEO_IL_NON_CHICAGO
    return None


def is_high_risk_request(text: str) -> bool:
    """True for discriminatory screening or self-help eviction requests."""
    low = _norm(text)

    for phrase in _SELF_HELP:
        if phrase in low:
            return True

    words = _WORD_RE.findall(low)
    for term in _PROTECTED:
        term_words = _WORD_RE.findall(term)
        if not term_words:
            continue
        for i in range(len(words) - len(term_words) + 1):
            if words[i : i + len(term_words)] != term_words:
                continue
            window = " ".join(words[max(0, i - 12) : i + len(term_words) + 12])
            if any(_verb_in(window, verb) for verb in _EXCLUSION_VERBS):
                return True
            if _SPLIT_VERBS.search(window):
                return True
    return False


# Particle verbs split around their object: "keep Section 8 tenants out".
_SPLIT_VERBS = re.compile(
    r"\b(?:keep|screen|filter|weed|throw|push|price|leave)\s+(?:\w+\s+){0,5}?out\b"
    r"|\bturn\s+(?:\w+\s+){0,5}?(?:away|down)\b"
    r"|\brent\s+(?:\w+\s+){0,5}?only\s+to\b"
)


def _verb_in(window: str, verb: str) -> bool:
    return re.search(r"\b" + re.escape(verb) + r"\b", window) is not None


def is_follow_up(question: str) -> bool:
    """True when a question probably leans on the previous turn for its subject."""
    low = _norm(question)
    if not low:
        return False
    if any(low.startswith(cue) for cue in _FOLLOW_UP_CUES):
        return True
    if _PRONOUN_START.match(low):
        return True
    return len(_WORD_RE.findall(low)) < 10


def detect_flags(question: str) -> list[str]:
    """All flags that apply to a question, sorted for deterministic prompts."""
    flags: set[str] = set()
    if is_legal_topic(question):
        flags.add(FLAG_LEGAL)
    geo = assess_geography(question)
    if geo:
        flags.add(geo)
    if is_high_risk_request(question):
        flags.add(FLAG_HIGH_RISK)
        flags.add(FLAG_LEGAL)
    return sorted(flags)


def _contains_disclaimer(answer: str) -> bool:
    prefix = _norm(LEGAL_DISCLAIMER)[:60]
    return prefix in _norm(answer)


def ensure_disclaimer(answer: str, flags: list[str]) -> str:
    """Append the verbatim disclaimer once when the question or answer is legal."""
    legal = FLAG_LEGAL in flags or is_legal_topic(answer)
    if not legal or _contains_disclaimer(answer):
        return answer
    separator = "\n\n" if answer.strip() else ""
    return f"{answer.rstrip()}{separator}{LEGAL_DISCLAIMER}"


def ensure_high_risk_response(answer: str, flags: list[str]) -> str:
    """Replace an answer that failed to decline a discriminatory or self-help request."""
    if FLAG_HIGH_RISK not in flags:
        return answer
    low = _norm(answer)
    already_declined = "can't help" in low or "cannot help" in low or "won't help" in low
    if already_declined:
        return answer
    return f"{HIGH_RISK_RESPONSE}\n\n{LEGAL_DISCLAIMER}"


def is_not_covered_answer(answer: str) -> bool:
    """True when Mark said the topic is outside his materials."""
    return _norm(NOT_COVERED_PHRASE) in _norm(answer)
