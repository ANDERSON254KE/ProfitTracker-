"""Forms for the multi-shop profit tracker.

These forms are referenced by tracker/views.py and must exist for Django
imports to succeed.  Each form only needs the attributes/methods that the
existing views actually call at runtime.
"""


from django import forms


class DailySaleForm(forms.Form):
    """Minimal form used by DailySaleCreateView."""

    pass


class DailySalesSheetForm(forms.Form):
    """Minimal form used by DailySalesSheetView.

    The view accesses:
      - DailySalesSheetForm.ADDITIONAL_PREFIX
      - DailySalesSheetForm.TOTAL_PREFIX
      - DailySalesSheetForm.CLOSING_PREFIX
      - DailySalesSheetForm.to_decimal(value)
    """

    ADDITIONAL_PREFIX = "additional_"
    TOTAL_PREFIX = "total_"
    CLOSING_PREFIX = "closing_"
    @staticmethod
    def to_decimal(value):
        """Coerce a value to Decimal, returning Decimal('0.00') on failure."""
        from decimal import Decimal
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return Decimal("0.00")


class ShopPricingForm(forms.Form):
    """Minimal form used by ShopPricingView.

    The view calls ShopPricingForm.to_decimal(cost_raw) and
    ShopPricingForm.to_decimal(sell_raw).
    """

    @staticmethod
    def to_decimal(value):
        """Coerce a value to Decimal, returning Decimal('0.00') on failure."""
        from decimal import Decimal
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return Decimal("0.00")