"""The deterministic guardrails: legal disclaimer, geography, fair housing, follow-ups."""

from __future__ import annotations

import pytest

from markai.advisor.guardrails import (
    FLAG_GEO_IL_NON_CHICAGO,
    FLAG_GEO_OUT,
    FLAG_HIGH_RISK,
    FLAG_LEGAL,
    HIGH_RISK_RESPONSE,
    IDENTITY_NOTICE,
    LEGAL_DISCLAIMER,
    NOT_COVERED_PHRASE,
    assess_geography,
    detect_flags,
    ensure_disclaimer,
    ensure_high_risk_response,
    is_follow_up,
    is_high_risk_request,
    is_legal_topic,
    is_not_covered_answer,
)


def test_fixed_wording_is_exact():
    assert LEGAL_DISCLAIMER == (
        "I'm not a lawyer, and this isn't legal advice. You should consult with an Illinois "
        "real estate attorney to confirm this applies to your situation."
    )
    assert NOT_COVERED_PHRASE == "That's not covered in my training materials."
    assert "not Mark Ainley" in IDENTITY_NOTICE


@pytest.mark.parametrize(
    "text",
    [
        "How much can I keep from a security deposit?",
        "What is the eviction process in Cook County?",
        "Does the RLTO apply to my two-flat?",
        "Can I charge a late fee after five days?",
    ],
)
def test_legal_topics_detected(text):
    assert is_legal_topic(text)


def test_non_legal_topic_not_flagged():
    assert not is_legal_topic("What paint holds up best in a rental hallway?")


def test_disclaimer_appended_once_and_is_idempotent():
    answer = "You owe interest on the deposit every year."
    once = ensure_disclaimer(answer, [FLAG_LEGAL])
    assert once.endswith(LEGAL_DISCLAIMER)
    assert ensure_disclaimer(once, [FLAG_LEGAL]) == once
    assert once.count("I'm not a lawyer") == 1


def test_disclaimer_recognises_curly_apostrophes():
    curly = LEGAL_DISCLAIMER.replace("'", "’")
    answer = f"Here is the rule.\n\n{curly}"
    assert ensure_disclaimer(answer, [FLAG_LEGAL]) == answer


def test_disclaimer_triggered_by_the_answer_even_when_the_question_looked_harmless():
    answer = "That depends on your lease and the eviction timeline in Cook County."
    assert ensure_disclaimer(answer, []).endswith(LEGAL_DISCLAIMER)


def test_no_disclaimer_on_a_non_legal_answer():
    answer = "Budget five percent of rent for maintenance."
    assert ensure_disclaimer(answer, []) == answer


@pytest.mark.parametrize(
    "text",
    [
        "I have a rental in Texas, what are the rules?",
        "My Wisconsin property needs a new lease",
        "How do evictions work in Indianapolis?",
        "Buying in Florida instead, any advice?",
    ],
)
def test_out_of_state_flagged(text):
    assert assess_geography(text) == FLAG_GEO_OUT


@pytest.mark.parametrize(
    "text",
    [
        "My building on Indiana Ave in Chicago needs a boiler",
        "The unit is near Michigan Ave",
        "A property on California Ave in Humboldt Park",
        "Parking on Ohio St downtown",
    ],
)
def test_chicago_street_names_are_not_out_of_state(text):
    assert assess_geography(text) is None


def test_illinois_outside_chicagoland_flagged():
    assert assess_geography("What about a duplex in Peoria?") == FLAG_GEO_IL_NON_CHICAGO
    assert assess_geography("Rules for Springfield rentals") == FLAG_GEO_IL_NON_CHICAGO


@pytest.mark.parametrize(
    "text",
    [
        "How do I keep Section 8 tenants out of my building?",
        "Can I change the locks on a non-paying tenant?",
        "I want to avoid renting to families with children",
        "How do I screen out applicants with a criminal record",
        "Can I turn away someone with a service animal",
        "I only rent to Christian tenants, is that fine",
    ],
)
def test_high_risk_requests_detected(text):
    assert is_high_risk_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "My tenant has kids, what lease clause covers occupancy limits?",
        "What are lawful screening criteria in Chicago?",
        "How much can I charge for a security deposit?",
        "What is the eviction process in Cook County?",
        "Do I have to accept a housing voucher in Chicago?",
        "How do I keep my vacancy rate down?",
    ],
)
def test_ordinary_questions_are_not_high_risk(text):
    assert not is_high_risk_request(text)


def test_high_risk_flag_implies_legal_flag():
    flags = detect_flags("How do I keep Section 8 tenants out?")
    assert FLAG_HIGH_RISK in flags
    assert FLAG_LEGAL in flags
    assert flags == sorted(flags)


def test_high_risk_response_replaces_a_compliant_answer():
    bad = "Sure, here is how to filter those applicants out."
    fixed = ensure_high_risk_response(bad, [FLAG_HIGH_RISK])
    assert fixed.startswith(HIGH_RISK_RESPONSE)
    assert fixed.endswith(LEGAL_DISCLAIMER)


def test_high_risk_response_keeps_an_answer_that_already_declined():
    good = "I can't help with that one. Apply the same criteria to everyone."
    assert ensure_high_risk_response(good, [FLAG_HIGH_RISK]) == good


def test_high_risk_response_is_a_noop_without_the_flag():
    answer = "Screen everyone the same way."
    assert ensure_high_risk_response(answer, []) == answer


@pytest.mark.parametrize(
    "text",
    ["What about in Naperville?", "And the deposit?", "Does that apply to a 3-flat?", "Why not?"],
)
def test_follow_ups_detected(text):
    assert is_follow_up(text)


def test_a_full_question_is_not_a_follow_up():
    question = (
        "What are the specific security deposit interest requirements that Chicago landlords "
        "must follow under the ordinance each year?"
    )
    assert not is_follow_up(question)


def test_not_covered_answer_detection():
    assert is_not_covered_answer(f"{NOT_COVERED_PHRASE} Try episode 12.")
    assert not is_not_covered_answer("Here is what the sources say.")


# --- Spanish -------------------------------------------------------------------------------
#
# The owner and a large share of Chicagoland landlords ask in Spanish. Every check below
# returned the safe-looking answer (no disclaimer, no refusal, no geography note) until the
# term lists learned Spanish, which is the worst possible way for a guardrail to fail.


@pytest.mark.parametrize(
    "text",
    [
        "Como desalojo a un inquilino que no paga?",
        "Cuanto tiempo tengo para devolver el deposito de seguridad?",
        "¿Cuál es el interés del depósito de seguridad en Chicago?",
        "Puedo cobrar una multa por pago tardio segun la ordenanza?",
        "Necesito un abogado para el desahucio?",
        "Que dice el contrato de arrendamiento sobre reparaciones?",
        "Cuales son los derechos del inquilino en Cook County?",
        "Tengo que ir a la corte para sacar a alguien?",
    ],
)
def test_spanish_legal_questions_get_the_disclaimer(text):
    assert is_legal_topic(text) is True
    assert FLAG_LEGAL in detect_flags(text)
    assert LEGAL_DISCLAIMER in ensure_disclaimer("Respuesta.", detect_flags(text))


@pytest.mark.parametrize(
    "text",
    [
        "Cuanto debo cobrar de renta por un dos flat en Logan Square?",
        "Como calculo el cash flow de una propiedad?",
        "Que seguro necesito para mi edificio?",
        "Cual es el mejor mes para rentar en Chicago?",
    ],
)
def test_ordinary_spanish_questions_are_not_treated_as_legal(text):
    assert is_legal_topic(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Como evito rentarle a personas con vales de la seccion 8?",
        "Prefiero no rentarle a inmigrantes, como lo hago?",
        "No quiero familias con ninos en el edificio, como los rechazo?",
        "Puedo negarme a rentarle a madres solteras?",
        "Quiero evitar personas con discapacidades",
        "Como filtro a los solicitantes con antecedentes penales?",
        "Puedo cambiar la cerradura si el inquilino no paga?",
        "Como saco al inquilino sin ir a la corte?",
        "Puedo cortar la luz para que se vaya?",
    ],
)
def test_spanish_high_risk_requests_are_refused(text):
    assert is_high_risk_request(text) is True
    assert ensure_high_risk_response("Claro, aqui va.", detect_flags(text)) != "Claro, aqui va."


@pytest.mark.parametrize(
    "text",
    [
        "Acepto mascotas o no?",
        "Que documentos le pido a un solicitante?",
        "Como hago un screening justo y uniforme para todos?",
        "Cuanto cuesta pintar una unidad antes de rentarla?",
        "Cuanto vale una propiedad en Cicero?",
    ],
)
def test_ordinary_spanish_questions_are_not_refused(text):
    assert is_high_risk_request(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Soy landlord en Texas", FLAG_GEO_OUT),
        ("Tengo una propiedad en Florida", FLAG_GEO_OUT),
        ("Las leyes de California aplican?", FLAG_GEO_OUT),
        ("Compre una casa en Indiana", FLAG_GEO_OUT),
        ("Rento en Springfield, Illinois", FLAG_GEO_IL_NON_CHICAGO),
        ("Tengo un edificio en Chicago", None),
        ("Rento en el condado de DuPage", None),
        ("Mi propiedad esta en La Villita", None),
    ],
)
def test_geography_reads_spanish(text, expected):
    assert assess_geography(text) == expected


def test_accents_do_not_hide_a_protected_class():
    """The word regex is ASCII, so an unfolded accent used to split the word in two."""
    assert is_high_risk_request("¿Puedo rechazar a los que tienen discapacidad?") is True
    assert is_legal_topic("¿Qué dice la sección 5-12-080 de la ordenanza?") is True


def test_a_spanish_answer_still_carries_the_owner_approved_english_wording():
    """The disclaimer is fixed wording approved by the owner. It is appended as-is."""
    answer = "La RLTO exige devolver el deposito dentro de 45 dias."
    assert ensure_disclaimer(answer, [FLAG_LEGAL]).endswith(LEGAL_DISCLAIMER)
