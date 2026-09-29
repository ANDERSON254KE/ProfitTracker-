"""Currency template filters for the tracker's templates.

All money in this project is Kenyan shillings, so a single ``ksh`` filter is
enough to keep every figure rendered consistently as ``KSh 1,234.00``.
"""

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter(name="ksh")
def ksh(value) -> str:
    """Format ``value`` as ``KSh 1,234.00``.

    Args:
        value: a Decimal, int, float or numeric string. ``None`` and empty
            strings render as ``KSh 0.00`` so an empty aggregate still shows a
            number rather than "None".
    """
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError):
        return f"KSh {0:,.2f}"
    return f"KSh {amount:,.2f}"


@register.filter(name="units")
def units(value) -> str:
    """Format a quantity as ``1,234`` (no currency symbol, no trailing .00)."""
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    amount = amount.normalize()
    if amount == amount.to_integral_value():
        return f"{int(amount):,}"
    return f"{amount:,f}"


@register.filter(name="signed_ksh")
def signed_ksh(value) -> str:
    """Like :func:`ksh` but always shows ``+``/``-`` for gains and losses."""
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError):
        return f"KSh {0:,.2f}"
    sign = "+" if amount > 0 else ""
    return f"{sign}KSh {amount:,.2f}"
