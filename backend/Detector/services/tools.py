"""Deterministic checks the reconciliation agent calls instead of doing arithmetic itself.

Date arithmetic and tolerance maths are exactly the parts of documentary
compliance a language model gets subtly wrong, and exactly the parts where a
wrong answer costs the beneficiary a refusal. Each tool here is pure: same
inputs, same verdict, no model involved.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


class ToolVerdict(BaseModel):
    """Shared shape for every deterministic check."""

    model_config = ConfigDict(extra='forbid')

    passed: bool
    """True when the check is satisfied."""

    detail: str
    """One line stating the computed numbers, for the agent to quote in its finding."""


class AmountVerdict(ToolVerdict):
    """Result of an amount-against-limit comparison."""

    difference: Decimal
    """presented minus permitted; positive means the presentation is over."""

    permitted_maximum: Decimal
    """The ceiling the presented amount was compared against."""


class DateVerdict(ToolVerdict):
    """Result of a date-ordering comparison."""

    days_between: int
    """later minus earlier, in days; negative means the dates are out of order."""


def check_amount_tolerance(
    credit_amount: Decimal,
    presented_amount: Decimal,
    tolerance_percent: Decimal = _ZERO,
) -> AmountVerdict:
    """Check a presented amount against a credit amount plus its stated tolerance.

    Use for invoice-against-LC and draft-against-LC amount checks. UCP 600 Art 30(a)
    allows a tolerance only when the credit states one ('about', '+/- 5 pct'); with no
    stated tolerance pass 0 and the credit amount is a hard ceiling.

    Args:
        credit_amount: The amount available under the credit.
        presented_amount: The amount actually drawn or invoiced.
        tolerance_percent: Tolerance the credit permits, as a percentage (5 means 5%).
    """
    permitted = credit_amount * (_HUNDRED + tolerance_percent) / _HUNDRED
    difference = presented_amount - permitted
    passed = difference <= _ZERO
    detail = (
        f'presented {presented_amount} against credit {credit_amount} '
        f'with {tolerance_percent}% tolerance (ceiling {permitted}): '
        f'{"within" if passed else f"over by {difference}"}'
    )
    return AmountVerdict(
        passed=passed,
        detail=detail,
        difference=difference,
        permitted_maximum=permitted,
    )


def check_insurance_coverage(
    goods_value: Decimal,
    insured_amount: Decimal,
    required_percent: Decimal = Decimal(110),
) -> AmountVerdict:
    """Check that an insurance document covers at least the required percentage of value.

    UCP 600 Art 28(f)(ii) sets a minimum of 110% of the CIF or CIP value where the
    credit is silent. Here the check is inverted relative to `check_amount_tolerance`:
    the computed amount is a floor, not a ceiling.

    Args:
        goods_value: The CIF/CIP value of the goods, normally the invoice total.
        insured_amount: The sum insured on the certificate or policy.
        required_percent: Minimum coverage the credit requires, as a percentage.
    """
    required = goods_value * required_percent / _HUNDRED
    difference = insured_amount - required
    passed = difference >= _ZERO
    detail = (
        f'insured {insured_amount} against required {required_percent}% of {goods_value} '
        f'(floor {required}): {"adequate" if passed else f"short by {-difference}"}'
    )
    return AmountVerdict(
        passed=passed,
        detail=detail,
        difference=difference,
        permitted_maximum=required,
    )


def check_date_order(
    earlier_label: str,
    earlier: date,
    later_label: str,
    later: date,
) -> DateVerdict:
    """Check that one date falls on or before another, and report the gap in days.

    Use for shipment date against latest shipment date, insurance effective date
    against shipment date, and any other ordering the credit imposes.

    Args:
        earlier_label: Name of the date that must come first, e.g. 'shipment date'.
        earlier: The date that must come first.
        later_label: Name of the date that must come second, e.g. 'latest shipment date'.
        later: The date that must come second.
    """
    days = (later - earlier).days
    passed = days >= 0
    detail = (
        f'{earlier_label} {earlier.isoformat()} vs {later_label} {later.isoformat()}: '
        f'{"within by" if passed else "late by"} {abs(days)} day(s)'
    )
    return DateVerdict(passed=passed, detail=detail, days_between=days)


class PresentationVerdict(ToolVerdict):
    """Result of the presentation-period check."""

    deadline: date
    """The last day a compliant presentation could be made."""

    days_late: int = Field(default=0)
    """Days past the deadline; 0 when the presentation was in time."""


def check_presentation_period(
    shipment_date: date,
    expiry_date: date,
    presented_on: date,
    presentation_period_days: int = 21,
) -> PresentationVerdict:
    """Check that documents were presented in time.

    Under UCP 600 Art 14(c) a presentation including a transport document must be
    made no later than 21 calendar days after shipment, and in any case no later
    than the credit's expiry date. The effective deadline is the earlier of the two.

    Args:
        shipment_date: The on-board date from the transport document.
        expiry_date: The expiry date of the credit.
        presented_on: The date the documents reached the bank.
        presentation_period_days: The period the credit allows, defaulting to the
            21 days UCP 600 applies when the credit is silent.
    """
    period_deadline = shipment_date + timedelta(days=presentation_period_days)
    deadline = min(period_deadline, expiry_date)
    days_late = max((presented_on - deadline).days, 0)
    passed = days_late == 0
    binding = 'expiry date' if deadline == expiry_date else f'{presentation_period_days}-day period'
    detail = (
        f'presented {presented_on.isoformat()} against deadline {deadline.isoformat()} '
        f'(set by the {binding}): {"in time" if passed else f"late by {days_late} day(s)"}'
    )
    return PresentationVerdict(
        passed=passed,
        detail=detail,
        deadline=deadline,
        days_late=days_late,
    )


RECONCILIATION_TOOLS = [
    check_amount_tolerance,
    check_insurance_coverage,
    check_date_order,
    check_presentation_period,
]
"""Registered on the reconciliation agent; see `Detector.services.agents`."""
