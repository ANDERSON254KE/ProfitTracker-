"""Forms used by the tracker's views.

:class:`DailySaleForm` is the only place a client is allowed to name a product
and a quantity; the money is derived from the shop's price card by the model,
never posted by the browser.
"""

from decimal import Decimal, InvalidOperation

from django import forms

from .models import DailySale, MasterProduct, ShopProduct


class DailySaleForm(forms.ModelForm):
    """Record one product sale for a single shop.

    The ``shop`` keyword argument is required. It scopes the product dropdown to
    what the shop actually stocks and is written onto the instance on save, so
    a crafted POST cannot move a sale into another shop.
    """

    class Meta:
        model = DailySale
        fields = ["product", "units_sold"]
        widgets = {
            "product": forms.Select(
                attrs={
                    "class": (
                        "mt-1 block w-full rounded-lg border-slate-300 "
                        "shadow-sm focus:border-indigo-500 focus:ring-indigo-500"
                    ),
                    "id": "id_product",
                }
            ),
            "units_sold": forms.NumberInput(
                attrs={
                    "class": (
                        "mt-1 block w-full rounded-lg border-slate-300 "
                        "shadow-sm focus:border-indigo-500 focus:ring-indigo-500"
                    ),
                    "step": "1",
                    "min": "0.01",
                    "id": "id_units_sold",
                }
            ),
        }

    def __init__(self, *args, shop=None, **kwargs):
        """Limit the product choices to the active range of ``shop``.

        Args:
            shop: the :class:`~tracker.models.Shop` being sold at.
        """
        self.shop = shop
        super().__init__(*args, **kwargs)

        if shop is None:
            self.fields["product"].queryset = MasterProduct.objects.none()
            return

        self.fields["product"].queryset = (
            MasterProduct.objects.filter(
                shop_prices__shop=shop, shop_prices__is_active=True
            )
            .select_related()
            .distinct()
            .order_by("name", "category")
        )
        if not self.fields["product"].queryset.exists():
            self.fields["product"].help_text = (
                "This shop has no active products yet. Ask the owner to add "
                "pricing for this branch first."
            )

    def clean_units_sold(self):
        """Return the quantity as a positive :class:`~decimal.Decimal`."""
        units = self.cleaned_data.get("units_sold")
        if units is None:
            return units
        if units <= Decimal("0"):
            raise forms.ValidationError("Enter a quantity greater than zero.")
        return units

    def clean(self):
        """Lock the sale's prices via the model's ``clean()``.

        ``ModelForm`` calls ``instance.full_clean()`` for us; this only makes
        sure the form fails cleanly when the shop cannot sell the product.
        """
        cleaned = super().clean()
        product = cleaned.get("product")
        if product is not None and self.shop is not None:
            stocked = ShopProduct.objects.filter(
                shop=self.shop, product=product
            ).first()
            if stocked is None:
                self.add_error("product", "This shop does not stock that product.")
            elif not stocked.is_active:
                self.add_error("product", "That product is inactive for this shop.")
        return cleaned

    def save(self, commit=True):
        """Attach the sale to the form's shop before saving."""
        instance = super().save(commit=False)
        instance.shop = self.shop
        if commit:
            instance.save()
        return instance


class ShopPricingForm(forms.Form):
    """One row of the inline price editor on the owner's pricing page.

    Built per :class:`~tracker.models.ShopProduct` and keyed by
    ``shop_product-<pk>-<field>`` so a single POST can update many rows.
    """

    cost_price = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.00"),
        widget=forms.NumberInput(
            attrs={"step": "0.01", "min": "0", "class": "price-input w-28 rounded-lg border-slate-300 text-sm shadow-sm"}
        ),
    )
    selling_price = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.00"),
        widget=forms.NumberInput(
            attrs={"step": "0.01", "min": "0", "class": "price-input w-28 rounded-lg border-slate-300 text-sm shadow-sm"}
        ),
    )
    is_active = forms.BooleanField(required=False)

    @staticmethod
    def to_decimal(raw):
        """Parse a posted value, returning ``None`` when it is not a number."""
        if raw is None:
            return None
        try:
            return Decimal(str(raw).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None
