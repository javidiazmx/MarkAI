"""Deal math: known values, division safety, and the tool schemas Claude sees."""

from __future__ import annotations

from markai.advisor.calculators import (
    TOOL_DEFINITIONS,
    analyze_deal,
    break_even_occupancy,
    cap_rate,
    cash_on_cash,
    dispatch_tool,
    dscr,
    gross_rent_multiplier,
    monthly_cash_flow,
    mortgage_payment,
    one_percent_rule,
)


def test_mortgage_payment_matches_a_known_amortization():
    assert mortgage_payment(225000, 0.065, 30) == 1422.15


def test_mortgage_payment_handles_zero_interest_and_bad_input():
    assert mortgage_payment(120000, 0.0, 10) == 1000.0
    assert mortgage_payment(0, 0.065, 30) == 0.0
    assert mortgage_payment(100000, 0.065, 0) == 0.0


def test_monthly_cash_flow_applies_vacancy_before_expenses():
    flow = monthly_cash_flow(
        gross_rent=3000,
        vacancy_rate=0.05,
        other_income=100,
        operating_expenses_monthly=900,
        debt_service_monthly=1400,
    )
    assert flow["effective_gross_income"] == 2950.0
    assert flow["noi_monthly"] == 2050.0
    assert flow["cash_flow_monthly"] == 650.0
    assert flow["cash_flow_annual"] == 7800.0


def test_ratios_never_divide_by_zero():
    assert cap_rate(10000, 0) == 0.0
    assert cash_on_cash(5000, 0) == 0.0
    assert gross_rent_multiplier(300000, 0) == 0.0
    assert dscr(10000, 0) == 0.0
    assert break_even_occupancy(1000, 1000, 0) == 0.0
    result = one_percent_rule(0, 3000)
    assert result["passes"] is False and "note" in result


def test_one_percent_rule_boundary():
    assert one_percent_rule(300000, 3000) == {"ratio": 0.01, "passes": True}
    assert one_percent_rule(300000, 2999)["passes"] is False


def test_analyze_deal_is_internally_consistent():
    result = analyze_deal(
        price=400000,
        down_payment_pct=0.25,
        annual_rate=0.07,
        years=30,
        monthly_rent=4000,
        taxes_annual=7200,
        insurance_annual=2400,
    )
    assert result["down_payment"] == 100000.0
    assert result["loan_amount"] == 300000.0
    assert result["monthly_payment"] == mortgage_payment(300000, 0.07, 30)
    assert result["cash_flow_monthly"] == round(
        result["noi_monthly"] - result["monthly_payment"], 2
    )
    assert result["one_percent_rule"]["passes"] is True
    assert 0 < result["cap_rate"] < 1


def test_analyze_deal_rejects_a_free_building():
    assert "error" in analyze_deal(
        price=0, down_payment_pct=0.25, annual_rate=0.07, years=30, monthly_rent=1000
    )


def test_tool_definitions_are_strict_and_complete():
    assert [t["name"] for t in TOOL_DEFINITIONS] == ["analyze_deal", "mortgage_payment"]
    allowed_keys = {"type", "description", "enum", "required", "additionalProperties", "properties"}
    for tool in TOOL_DEFINITIONS:
        assert tool["strict"] is True
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert set(schema) <= allowed_keys
        for prop in schema["properties"].values():
            assert set(prop) <= allowed_keys, prop
        assert tool["description"].startswith("Call this")


def test_tool_descriptions_state_the_unit_for_every_rate():
    schema = TOOL_DEFINITIONS[0]["input_schema"]["properties"]
    for field in ("down_payment_pct", "annual_rate", "vacancy_rate", "maintenance_pct"):
        assert (
            "decimal" in schema[field]["description"] or "fraction" in schema[field]["description"]
        )


def test_dispatch_rejects_a_percent_where_a_fraction_belongs():
    result = dispatch_tool(
        "mortgage_payment", {"principal": 225000, "annual_rate": 6.5, "years": 30}
    )
    assert "error" in result and "0.065" in result["error"]


def test_dispatch_runs_the_tools_and_refuses_unknown_names():
    payment = dispatch_tool(
        "mortgage_payment", {"principal": 225000, "annual_rate": 0.065, "years": 30}
    )
    assert payment == {"monthly_payment": 1422.15}
    assert "error" in dispatch_tool("do_my_taxes", {})
    assert "error" in dispatch_tool("analyze_deal", {"price": -5})


def test_dispatch_ignores_unexpected_keys_rather_than_crashing():
    result = dispatch_tool(
        "analyze_deal",
        {
            "price": 300000,
            "down_payment_pct": 0.25,
            "annual_rate": 0.07,
            "years": 30,
            "monthly_rent": 3000,
            "not_a_field": 1,
        },
    )
    assert "cash_flow_monthly" in result
