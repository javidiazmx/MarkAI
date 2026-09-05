"""Rental deal math, exposed both as plain functions and as Claude tools.

UNITS: every ``*_rate`` and ``*_pct`` argument in this module is a DECIMAL FRACTION
(0.25 for 25%, 0.065 for 6.5%). The CLI divides its percent-style options by 100 before
calling in, and every tool schema description spells the unit out for the model.

Nothing here raises on bad input: division by zero yields 0.0 (with a ``note`` when the
return value is a dict) so a tool call can never crash a conversation.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "TOOL_DEFINITIONS",
    "analyze_deal",
    "break_even_occupancy",
    "cap_rate",
    "cash_on_cash",
    "dispatch_tool",
    "dscr",
    "gross_rent_multiplier",
    "monthly_cash_flow",
    "mortgage_payment",
    "one_percent_rule",
]


def _round(value: float, places: int = 2) -> float:
    return round(float(value) + 0.0, places)


def mortgage_payment(principal: float, annual_rate: float, years: int) -> float:
    """Level monthly principal-and-interest payment for a fully amortizing loan."""
    if principal <= 0 or years <= 0:
        return 0.0
    months = int(years) * 12
    monthly_rate = float(annual_rate) / 12.0
    if monthly_rate <= 0:
        return _round(principal / months)
    factor = (1.0 + monthly_rate) ** months
    return _round(principal * monthly_rate * factor / (factor - 1.0))


def monthly_cash_flow(
    gross_rent: float,
    vacancy_rate: float,
    other_income: float,
    operating_expenses_monthly: float,
    debt_service_monthly: float,
) -> dict[str, float]:
    """Effective gross income, NOI, and cash flow for one month."""
    egi = gross_rent * (1.0 - float(vacancy_rate)) + other_income
    noi = egi - operating_expenses_monthly
    cash_flow = noi - debt_service_monthly
    return {
        "effective_gross_income": _round(egi),
        "noi_monthly": _round(noi),
        "cash_flow_monthly": _round(cash_flow),
        "cash_flow_annual": _round(cash_flow * 12.0),
    }


def cap_rate(annual_noi: float, purchase_price: float) -> float:
    """NOI divided by price, as a decimal fraction."""
    if purchase_price <= 0:
        return 0.0
    return _round(annual_noi / purchase_price, 4)


def cash_on_cash(annual_cash_flow: float, total_cash_invested: float) -> float:
    """Annual cash flow divided by cash in the deal, as a decimal fraction."""
    if total_cash_invested <= 0:
        return 0.0
    return _round(annual_cash_flow / total_cash_invested, 4)


def gross_rent_multiplier(price: float, annual_gross_rent: float) -> float:
    """Price divided by annual gross rent."""
    if annual_gross_rent <= 0:
        return 0.0
    return _round(price / annual_gross_rent)


def one_percent_rule(price: float, monthly_rent: float) -> dict[str, Any]:
    """Monthly rent as a fraction of price, and whether it clears 1%."""
    if price <= 0:
        return {"ratio": 0.0, "passes": False, "note": "Purchase price must be greater than zero."}
    ratio = monthly_rent / price
    return {"ratio": _round(ratio, 4), "passes": ratio >= 0.01}


def dscr(annual_noi: float, annual_debt_service: float) -> float:
    """Debt service coverage ratio; lenders usually want 1.20 or better."""
    if annual_debt_service <= 0:
        return 0.0
    return _round(annual_noi / annual_debt_service)


def break_even_occupancy(
    operating_expenses_annual: float,
    debt_service_annual: float,
    gross_potential_rent_annual: float,
) -> float:
    """Occupancy needed to cover expenses and debt, as a decimal fraction."""
    if gross_potential_rent_annual <= 0:
        return 0.0
    return _round(
        (operating_expenses_annual + debt_service_annual) / gross_potential_rent_annual, 4
    )


def analyze_deal(
    price: float,
    down_payment_pct: float,
    annual_rate: float,
    years: int,
    monthly_rent: float,
    other_income_monthly: float = 0.0,
    vacancy_rate: float = 0.05,
    taxes_annual: float = 0.0,
    insurance_annual: float = 0.0,
    maintenance_pct: float = 0.05,
    capex_pct: float = 0.05,
    management_pct: float = 0.0,
    hoa_monthly: float = 0.0,
    utilities_monthly: float = 0.0,
    closing_costs: float = 0.0,
    rehab: float = 0.0,
) -> dict[str, Any]:
    """Full underwriting pass: payment, expenses, cash flow, and the usual ratios."""
    if price <= 0:
        return {"error": "Purchase price must be greater than zero."}

    down_payment = price * float(down_payment_pct)
    loan_amount = max(price - down_payment, 0.0)
    payment = mortgage_payment(loan_amount, annual_rate, years)

    gross_scheduled_monthly = monthly_rent + other_income_monthly
    maintenance = monthly_rent * float(maintenance_pct)
    capex = monthly_rent * float(capex_pct)
    management = monthly_rent * float(management_pct)
    fixed_monthly = (taxes_annual + insurance_annual) / 12.0 + hoa_monthly + utilities_monthly
    operating_expenses_monthly = maintenance + capex + management + fixed_monthly

    flow = monthly_cash_flow(
        gross_rent=monthly_rent,
        vacancy_rate=vacancy_rate,
        other_income=other_income_monthly,
        operating_expenses_monthly=operating_expenses_monthly,
        debt_service_monthly=payment,
    )
    noi_annual = flow["noi_monthly"] * 12.0
    cash_invested = down_payment + closing_costs + rehab

    return {
        "purchase_price": _round(price),
        "down_payment": _round(down_payment),
        "loan_amount": _round(loan_amount),
        "monthly_payment": payment,
        "gross_scheduled_income_monthly": _round(gross_scheduled_monthly),
        "operating_expenses_monthly": _round(operating_expenses_monthly),
        "expense_breakdown_monthly": {
            "maintenance": _round(maintenance),
            "capex": _round(capex),
            "management": _round(management),
            "taxes": _round(taxes_annual / 12.0),
            "insurance": _round(insurance_annual / 12.0),
            "hoa": _round(hoa_monthly),
            "utilities": _round(utilities_monthly),
        },
        "effective_gross_income_monthly": flow["effective_gross_income"],
        "noi_monthly": flow["noi_monthly"],
        "noi_annual": _round(noi_annual),
        "cash_flow_monthly": flow["cash_flow_monthly"],
        "cash_flow_annual": flow["cash_flow_annual"],
        "total_cash_invested": _round(cash_invested),
        "cap_rate": cap_rate(noi_annual, price),
        "cash_on_cash": cash_on_cash(flow["cash_flow_annual"], cash_invested),
        "dscr": dscr(noi_annual, payment * 12.0),
        "gross_rent_multiplier": gross_rent_multiplier(price, gross_scheduled_monthly * 12.0),
        "one_percent_rule": one_percent_rule(price, monthly_rent),
        "break_even_occupancy": break_even_occupancy(
            operating_expenses_monthly * 12.0, payment * 12.0, gross_scheduled_monthly * 12.0
        ),
    }


_ANALYZE_PROPERTIES = {
    "price": {"type": "number", "description": "Purchase price in dollars."},
    "down_payment_pct": {
        "type": "number",
        "description": (
            "Down payment as a decimal fraction, e.g. 0.25 for 25%. Use 0.25 if unknown."
        ),
    },
    "annual_rate": {
        "type": "number",
        "description": (
            "Annual interest rate as a decimal, e.g. 0.065 for 6.5%. Use 0.07 if unknown."
        ),
    },
    "years": {"type": "integer", "description": "Loan term in years, usually 30."},
    "monthly_rent": {"type": "number", "description": "Total monthly rent from all units."},
    "other_income_monthly": {
        "type": "number",
        "description": "Monthly income beyond rent (parking, laundry, storage). Use 0 if none.",
    },
    "vacancy_rate": {
        "type": "number",
        "description": "Vacancy as a decimal fraction, e.g. 0.05 for 5%. Use 0.05 if unknown.",
    },
    "taxes_annual": {
        "type": "number",
        "description": "Annual property taxes in dollars. Use 0 if unknown, and say so.",
    },
    "insurance_annual": {
        "type": "number",
        "description": "Annual insurance premium in dollars. Use 0 if unknown, and say so.",
    },
    "maintenance_pct": {
        "type": "number",
        "description": (
            "Maintenance reserve as a decimal fraction of rent, e.g. 0.05. Use 0.05 if unknown."
        ),
    },
    "capex_pct": {
        "type": "number",
        "description": (
            "Capital-expense reserve as a fraction of rent, e.g. 0.05. Use 0.05 if unknown."
        ),
    },
    "management_pct": {
        "type": "number",
        "description": ("Management fee as a fraction of rent, e.g. 0.08. Use 0 for self-managed."),
    },
    "hoa_monthly": {"type": "number", "description": "Monthly HOA or assessment. Use 0 if none."},
    "utilities_monthly": {
        "type": "number",
        "description": "Monthly utilities the owner pays. Use 0 if tenants pay everything.",
    },
    "closing_costs": {
        "type": "number",
        "description": "Estimated closing costs in dollars. Use 0 if unknown.",
    },
    "rehab": {
        "type": "number",
        "description": "Up-front rehab or make-ready budget in dollars. Use 0 if none.",
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "analyze_deal",
        "description": (
            "Call this whenever the user gives (or asks about) a purchase price and rent or "
            "expenses and wants cash flow, cap rate, cash-on-cash, DSCR, the 1% rule, or "
            "whether a deal works. Do not estimate these numbers yourself. Every rate and "
            "percentage argument is a decimal fraction (0.065 means 6.5%). Pass every field; "
            "use the stated default when the user did not give a number, and tell them which "
            "assumptions you used."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": _ANALYZE_PROPERTIES,
            "required": list(_ANALYZE_PROPERTIES),
            "additionalProperties": False,
        },
    },
    {
        "name": "mortgage_payment",
        "description": (
            "Call this when the user asks only for a loan payment: principal, rate, and term. "
            "Returns the monthly principal-and-interest payment. The rate is a decimal "
            "fraction (0.065 means 6.5%). For a full deal analysis use analyze_deal instead."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "principal": {
                    "type": "number",
                    "description": "Loan amount in dollars (price minus down payment).",
                },
                "annual_rate": {
                    "type": "number",
                    "description": "Annual interest rate as a decimal, e.g. 0.065 for 6.5%.",
                },
                "years": {"type": "integer", "description": "Loan term in years, usually 30."},
            },
            "required": ["principal", "annual_rate", "years"],
            "additionalProperties": False,
        },
    },
]

_RATE_FIELDS = (
    "down_payment_pct",
    "annual_rate",
    "vacancy_rate",
    "maintenance_pct",
    "capex_pct",
    "management_pct",
)


def _validate(name: str, data: dict[str, Any]) -> str | None:
    for key, value in data.items():
        if key == "years":
            if not isinstance(value, int | float) or not (1 <= float(value) <= 40):
                return f"{key} must be a number of years between 1 and 40."
            continue
        if not isinstance(value, int | float) or isinstance(value, bool):
            return f"{key} must be a number."
        if key in _RATE_FIELDS and not (0.0 <= float(value) <= 1.0):
            return (
                f"{key} must be a decimal fraction between 0 and 1 "
                f"(got {value}; 6.5% is 0.065, not 6.5)."
            )
        if key in ("price", "principal") and float(value) <= 0:
            return f"{key} must be greater than zero."
        if float(value) < 0:
            return f"{key} cannot be negative."
    if name == "analyze_deal" and "price" not in data:
        return "price is required."
    if name == "mortgage_payment" and "principal" not in data:
        return "principal is required."
    return None


def dispatch_tool(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Run a calculator tool by name. Always returns a JSON-serializable dict."""
    if name not in {"analyze_deal", "mortgage_payment"}:
        return {"error": f"Unknown tool {name!r}."}
    data = dict(tool_input or {})
    problem = _validate(name, data)
    if problem:
        return {"error": problem}
    try:
        if name == "mortgage_payment":
            return {
                "monthly_payment": mortgage_payment(
                    float(data["principal"]), float(data["annual_rate"]), int(data["years"])
                )
            }
        allowed = set(_ANALYZE_PROPERTIES)
        return analyze_deal(**{k: v for k, v in data.items() if k in allowed})
    except Exception as exc:  # never let a tool crash the conversation
        return {"error": f"Calculation failed: {exc}"}
