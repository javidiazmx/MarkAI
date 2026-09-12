"""Deterministic guardrails: fixed wording, topic flags, and answer post-processing.

The system prompt is the primary enforcement; these heuristics are the backstop that
guarantees the legal disclaimer and the fair-housing refusal actually appear.
"""

from __future__ import annotations

import re
import unicodedata

LEGAL_DISCLAIMER = (
    "I'm not a lawyer, and this isn't legal advice. You should consult with an Illinois "
    "real estate attorney to confirm this applies to your situation."
)
NOT_COVERED_PHRASE = "That's not covered in my training materials."
IDENTITY_NOTICE = (
    "Jay is an AI assistant built in the style of Mark Ainley (Straight Up Chicago "
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
FLAG_PM_INTEREST = "pm_interest"
FLAG_TENANT_TROUBLE = "tenant_trouble"
FLAG_SELF_MANAGE_BURNOUT = "self_manage_burnout"
FLAG_VACANCY_HELP = "vacancy_help"
FLAG_BUYING_INTEREST = "buying_interest"

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
    # Spanish. The owner and many Chicagoland landlords ask in Spanish, and a question
    # that never matched a term here never got the disclaimer. Stems, because Spanish
    # conjugates: "desaloj" covers desalojo, desalojar, desalojando, desalojarlo.
    # _norm folds accents, so these are written unaccented on purpose.
    "desaloj",
    "desahuci",
    "deshauci",
    "evicci",
    "arrendamiento",
    "arrendatario",
    "arrendador",
    "subarr",
    "contrato de renta",
    "contrato de alquiler",
    "deposito de seguridad",
    "deposito de garantia",
    "interes del deposito",
    "intereses del deposito",
    "fianza",
    "ordenanza",
    "estatuto",
    "leyes",
    "ley de",
    "la ley",
    "legal",
    "abogad",
    "corte",
    "tribunal",
    "juzgado",
    "demanda",
    "orden judicial",
    "proceso judicial",
    "notificacion de desalojo",
    "aviso de desalojo",
    "aviso de 5 dias",
    "aviso de cinco dias",
    "notificacion de 5 dias",
    "derechos del inquilin",
    "derechos de los inquilin",
    "vivienda justa",
    "clase protegida",
    "discrimin",
    "seccion 8",
    "vale de vivienda",
    "vales de vivienda",
    "fuente de ingreso",
    "habitabilidad",
    "codigo de construccion",
    "violacion de codigo",
    "cargo por retraso",
    "multa por retraso",
    "recargo por mora",
    "pago tardio",
    "renta atrasada",
    "renta impagada",
    "control de renta",
    "reclamos menores",
    "cerradura",
    "responsabilidad legal",
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
    # Spanish names for the same places.
    "la villita",
    "condado de cook",
    "condado de dupage",
    "condado de lake",
    "condado de will",
    "condado de kane",
    "condado de mchenry",
    "condado de kendall",
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
    r"property\s+in|rental\s+in|unit\s+in|invest(?:ing)?\s+in|"
    # Spanish. Without these, "Soy landlord en Texas" read as in-scope.
    r"en|de|desde|hacia|mi|mis|otro|otra|"
    r"propiedad\s+en|rento\s+en|renta\s+en|unidad\s+en|edificio\s+en|casa\s+en|"
    r"invertir\s+en|invirtiendo\s+en|comprar\s+en|comprando\s+en|mudarme\s+a)\s+"
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
    # Spanish equivalents of everything above.
    "raza",
    "negro",
    "negra",
    "hispano",
    "hispana",
    "latina",
    "mexicano",
    "asiatico",
    "religion",
    "musulman",
    "judio",
    "cristiano",
    "origen nacional",
    "inmigrante",
    "indocumentado",
    "extranjero",
    "acento",
    "sexo",
    "genero",
    "transgenero",
    "homosexual",
    "orientacion sexual",
    "identidad de genero",
    "estado familiar",
    "familias con ninos",
    "con ninos",
    "con hijos",
    "madre soltera",
    "embarazada",
    "discapacidad",
    "discapacitado",
    "minusvalido",
    "silla de ruedas",
    "animal de servicio",
    "animal de apoyo emocional",
    "enfermedad mental",
    "seccion 8",
    "vale de vivienda",
    "vales de vivienda",
    "vales",
    "cupon de vivienda",
    "subsidio",
    "fuente de ingreso",
    "edad",
    "anciano",
    "adulto mayor",
    "estado civil",
    "veterano",
    "antecedentes penales",
    "antecedentes criminales",
    "expediente criminal",
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
    # Spanish. Self-help eviction is the same crime in either language.
    "cambiar la cerradura",
    "cambiar las cerraduras",
    "cambiar cerradura",
    "cambiarle la cerradura",
    "cambio la cerradura",
    "cambiar la chapa",
    "cortar la luz",
    "cortar el agua",
    "cortar la calefaccion",
    "cortar los servicios",
    "quitar la luz",
    "quitar el agua",
    "apagar la calefaccion",
    "suspender los servicios",
    "cortarle los servicios",
    "quitar la puerta",
    "sacar la puerta",
    "sacar sus cosas",
    "tirar sus cosas",
    "botar sus cosas",
    "sacar sus pertenencias",
    "tirar sus pertenencias",
    "sin orden judicial",
    "sin ir a la corte",
    "sin ir al tribunal",
    "sin proceso de desalojo",
    "sin desalojo formal",
    "sin pasar por la corte",
    "sacarlo yo mismo",
    "sacarla yo mismo",
    "sacarlos yo mismo",
    "obligarlo a irse",
    "obligarla a irse",
    "obligarlos a irse",
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
    """Lowercase, collapse whitespace, and fold accents.

    Folding matters more than it looks: the word regex below is ASCII, so an unfolded
    "seccion" written "secci\u00f3n" tokenises as ["secci", "n"] and every phrase match
    around it silently fails. Owners and tenants type Spanish both ways.
    """
    flat = unicodedata.normalize("NFD", normalize_quotes(text))
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", flat).strip().lower()


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
            if not _same_words(words[i : i + len(term_words)], term_words):
                continue
            window = " ".join(words[max(0, i - 12) : i + len(term_words) + 12])
            if any(_verb_in(window, verb) for verb in _EXCLUSION_VERBS):
                return True
            if _SPLIT_VERBS.search(window):
                return True
            if _EXCLUSION_STEMS.search(window):
                return True
    return False


# Spanish conjugates, so whole-word matching against an infinitive list misses almost
# everything a real question contains: "evito", "evitaria", "rechazo", "no le rento".
# Stems with \w* catch the whole paradigm.
_EXCLUSION_STEMS = re.compile(
    r"\b(?:evit|rechaz|descart|excluy|exclui|filtr|impid|impedir|prohib|"
    r"nieg|negarl|negar|desanim|discrimin|vet)\w*"
    r"|\bno\s+(?:\w+\s+){0,4}?(?:rent|alquil|arrend|acept)\w*"
    r"|\b(?:solo|unicamente)\s+(?:\w+\s+){0,3}?(?:rent|alquil|arrend)\w*"
    r"|\b(?:rent|alquil|arrend)\w*\s+(?:\w+\s+){0,3}?(?:solo|unicamente)\s+a\b"
    r"|\bdeshacer(?:me|nos|se)\b|\blibrar(?:me|nos|se)\b|\bsacar(?:me|los|las|lo|la)?\b"
    r"|\bmantener\s+(?:\w+\s+){0,4}?fuera\b"
)

# Particle verbs split around their object: "keep Section 8 tenants out".
_SPLIT_VERBS = re.compile(
    r"\b(?:keep|screen|filter|weed|throw|push|price|leave)\s+(?:\w+\s+){0,5}?out\b"
    r"|\bturn\s+(?:\w+\s+){0,5}?(?:away|down)\b"
    r"|\brent\s+(?:\w+\s+){0,5}?only\s+to\b"
)


def _same_words(found: list[str], term: list[str]) -> bool:
    """Word-for-word match, tolerating a plural. "inmigrantes" has to match "inmigrante"
    or every protected class written in the plural walks straight past the check."""
    if len(found) != len(term):
        return False
    return all(
        word == want or word in (want + "s", want + "es")
        for word, want in zip(found, term, strict=True)
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


# Hello, who are you, what is this, thanks. Real questions, none of them about a building.
_SMALL_TALK = re.compile(
    r"^\s*(hi|hey|hello|yo|thanks|thank you|ok|okay|test|testing|"
    r"hola|buenas|gracias|adios|prueba|probando)\b[\s!.?]*$",
    re.IGNORECASE,
)
_ABOUT_JAY = re.compile(
    r"\b(who are you|what are you|what is this|what can you (do|help)|who is mark|"
    r"qu[eé] eres|qui[eé]n eres|qu[eé] es esto|en qu[eé] (me )?puedes ayudar|"
    r"qui[eé]n es mark|para qu[eé] sirves)\b",
    re.IGNORECASE,
)
SMALL_TALK_MAX_WORDS = 8


def is_small_talk(question: str) -> bool:
    """A greeting, a thank you, or a question about Jay himself rather than a building.

    Only used to keep these out of `mark gaps`. The sources do not cover "who are you", and
    they are not supposed to: a list of content the owner should go write is worth nothing
    if half of it is people saying hello.
    """
    text = (question or "").strip()
    if not text:
        return True
    if _ABOUT_JAY.search(text):
        return True
    return bool(_SMALL_TALK.match(text)) and len(text.split()) <= SMALL_TALK_MAX_WORDS


_PM_INTEREST = re.compile(
    r"\b(hire|hiring|get|need|looking for|find|considering|switch(?:ing)? to|use|"
    r"outsource(?:ing)? to)\b[^.?!]{0,40}\bproperty managers?\b"
    r"|\bproperty managers?\b[^.?!]{0,40}\b(cost|charge|fee|worth it|percent|% )\b"
    r"|\bhow much (?:does|do|would|will)\b[^.?!]{0,40}\bproperty manag(?:er|ement)\b"
    r"|\b(?:do|does|can|could) (?:you|gc realty|mark ainley)\b[^.?!]{0,40}\bmanage\b[^.?!]"
    r"{0,20}\b(propert|building|unit)\w*\b"
    r"|\bmanagement (?:fee|cost|company|companies)\b",
    re.IGNORECASE,
)


def is_pm_interest_question(question: str) -> bool:
    """True for a landlord asking, in some form, whether to hire a property manager.

    Deliberately narrow: general education about what a property manager does ("what does
    a property manager do") should not fire this, only a question that is actually about
    hiring one, its cost, or whether GC Realty itself manages properties - the signal a
    lead-generation alert is built on, not a topic tag.
    """
    return bool(_PM_INTEREST.search(_norm(question)))


# First-person framing on purpose: "my tenant hasn't paid" is a landlord in a situation
# worth a call, "what's the eviction process in Cook County" is research and fires
# FLAG_LEGAL instead - the two should not collapse into the same alert.
_TENANT_TROUBLE = re.compile(
    r"\bmy tenants?\b[^.?!]{0,40}\b(hasn'?t|has not|isn'?t|is not|won'?t|refuses? to|"
    r"stopped)\b[^.?!]{0,20}\b(pay\w*|leave|leaving|left|moved? out)\b"
    r"|\b(i need to|how do i|can i|want to|trying to)\b[^.?!]{0,30}\bevict\b[^.?!]{0,10}"
    r"\bmy\b"
    r"|\b(non-?paying|problem|nightmare) tenants?\b"
    r"|\bmy tenant\b[^.?!]{0,40}\b(damag(?:e|ing)|trashed|won'?t leave|squatt)\w*\b",
    re.IGNORECASE,
)


def is_tenant_trouble_question(question: str) -> bool:
    """True for a landlord describing their own tenant situation, not asking in the abstract.

    A real pain point, not a topic: "my tenant hasn't paid in two months" is someone who
    might want GC Realty to take this off their plate. "What's the eviction process" is
    someone doing homework, and stays a plain answer with no alert attached.
    """
    return bool(_TENANT_TROUBLE.search(_norm(question)))


_SELF_MANAGE_BURNOUT = re.compile(
    r"\b(tired of|sick of|done with|burned? out( on)?|overwhelmed by|can'?t keep up with|"
    r"don'?t have time (for|to)|no time (for|to) manage|too much (work|hassle|to handle))\b"
    r"[^.?!]{0,40}\b(manag(?:e|ing)|landlord(?:ing)?|propert(?:y|ies)|rental)\b",
    re.IGNORECASE,
)


def is_self_manage_burnout_question(question: str) -> bool:
    """True when someone describes being worn out by managing their own rental(s).

    The clearest "ready to hand it off" signal there is, and one that never uses the
    word "manager" at all - which is exactly why FLAG_PM_INTEREST's own wording match
    would miss it.
    """
    return bool(_SELF_MANAGE_BURNOUT.search(_norm(question)))


_VACANCY_HELP = re.compile(
    r"\b(can'?t find|trouble (renting|leasing|finding|filling)|struggling to (rent|lease|fill)|"
    r"no one('?s| is| has been) (applying|responding|interested)|nobody('?s| is) applying)\b"
    r"[^.?!]{0,40}\b(tenant|renter|applicant|unit|apartment|vacan\w*)\b"
    r"|\b(unit|apartment|rental)\b[^.?!]{0,30}\bvacant\b[^.?!]{0,20}"
    r"\b(month|months|weeks|while)\b",
    re.IGNORECASE,
)


def is_vacancy_help_question(question: str) -> bool:
    """True for a landlord with a unit sitting empty longer than they expected.

    Marketing and leasing is a service line on its own, separate from the day-to-day
    management FLAG_PM_INTEREST and FLAG_SELF_MANAGE_BURNOUT point at.
    """
    return bool(_VACANCY_HELP.search(_norm(question)))


_BUYING_INTEREST = re.compile(
    r"\b(looking to buy|want to buy|planning to (buy|purchase)|in the market for|"
    r"shopping for)\b[^.?!]{0,40}\b(rental|investment|propert(?:y|ies)|building|unit|duplex|"
    r"multi-?family|multifamily)\b"
    r"|\blooking for\b[^.?!]{0,20}\b(my first|another|my next)\b[^.?!]{0,20}"
    r"\b(rental|investment propert)\w*\b",
    re.IGNORECASE,
)


def is_buying_interest_question(question: str) -> bool:
    """True for someone actively shopping for a rental to buy, not just curious about deals.

    Distinct from ``portfolio_growth`` in app.py, which fires reactively once a second
    building is actually added - this catches the intent before any purchase happens.
    """
    return bool(_BUYING_INTEREST.search(_norm(question)))


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
    if is_pm_interest_question(question):
        flags.add(FLAG_PM_INTEREST)
    if is_tenant_trouble_question(question):
        flags.add(FLAG_TENANT_TROUBLE)
    if is_self_manage_burnout_question(question):
        flags.add(FLAG_SELF_MANAGE_BURNOUT)
    if is_vacancy_help_question(question):
        flags.add(FLAG_VACANCY_HELP)
    if is_buying_interest_question(question):
        flags.add(FLAG_BUYING_INTEREST)
    return sorted(flags)


def _contains_disclaimer(answer: str) -> bool:
    prefix = _norm(LEGAL_DISCLAIMER)[:60]
    return prefix in _norm(answer)


# The exact owner-approved sentence, matched with whatever whitespace it arrives in.
_EXACT_DISCLAIMER = re.compile(r"\s*".join(re.escape(w) for w in LEGAL_DISCLAIMER.split()))

# And a paraphrase of it. Narrow on purpose: "worth an hour with a real estate attorney"
# is a real answer about one case and has to survive, while "I'm not a lawyer" is furniture.
_STANDING_NOTICE = re.compile(
    r"(not a lawyer|n't a lawyer|n't legal advice|not legal advice|"
    r"should consult (with )?an? .{0,40}(attorney|lawyer)|"
    r"no soy abogad|no es asesor[ií]a legal|esto no constituye asesor)",
    re.IGNORECASE,
)


def strip_disclaimer(answer: str) -> str:
    """Remove a standing disclaimer the model appended anyway.

    The prompt asks it not to, but the habit is strong and a prompt is not a guarantee. The
    exact sentence goes wherever it appears; a paraphrase goes only from the closing
    paragraph, and only the sentences of it that read as the notice, so an answer that
    genuinely says to call an attorney about this one thing keeps its point. An answer that
    is nothing but the notice is left alone: something beats nothing.
    """
    text = _EXACT_DISCLAIMER.sub("", answer or "").rstrip()
    if not text:
        return answer
    paragraphs = text.split("\n\n")
    if not _STANDING_NOTICE.search(paragraphs[-1]):
        return text
    sentences = re.split(r"(?<=[.!?])\s+", paragraphs[-1])
    kept = [s for s in sentences if not _STANDING_NOTICE.search(s)]
    if kept:
        paragraphs[-1] = " ".join(kept).strip()
    elif len(paragraphs) > 1:
        paragraphs.pop()
    else:
        return answer
    return "\n\n".join(paragraphs).rstrip()


def plain_punctuation(answer: str) -> str:
    """Turn em and en dashes into plain punctuation, per the owner's house style.

    The prompt asks for it; this is the backstop, since the habit is strong enough that
    asking alone does not settle it.
    """
    text = re.sub(r"\s*[\u2014\u2013]\s*", " - ", answer)
    return re.sub(r"[ \t]{2,}", " ", text)


def ensure_disclaimer(answer: str, flags: list[str], already_given: bool = False) -> str:
    """Append the verbatim disclaimer when the question or answer is legal.

    ``already_given`` suppresses it for the rest of a conversation. Repeated under every
    answer in a session it reads as furniture and stops being read, which is the opposite
    of what it is for; said once, it lands. A new conversation says it again.
    """
    legal = FLAG_LEGAL in flags or is_legal_topic(answer)
    if not legal or already_given or _contains_disclaimer(answer):
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
