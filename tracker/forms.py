"""Forms used by the tracker's views.

:class:`DailySaleForm` is the only place a client is allowed to name a product
and a quantity; the money is derived from the shop's price card by the model,
never posted by the browser.
"""

from decimal import Decimal, InvalidOperation

from django import forms

from .models import DailySale, MasterProduct, ShopProduct

#: Upper bound on products in one sheet submission, so a crafted POST cannot ask
#: the server to write an unbounded number of rows.
MAX_SHEET_ROWS = 500


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


class DailySalesSheetForm(forms.Form):
    """Record a whole trading day for one shop in a single submit.

    The manager types a quantity per product and leaves the rest blank. Only
    quantities cross the wire: every price, and therefore every money figure, is
    derived server-side by :meth:`tracker.models.DailySale.save`.

    This is deliberately append-only. A submitted day is never rewritten -- to
    add more sales later, submit again and the new quantities are inserted
    alongside what is already recorded, so the audit trail of what was entered
    when stays intact.
    """

    def __init__(self, *args, shop=None, **kwargs):
        """Scope the sheet to ``shop``'s active range.

        Args:
            shop: the :class:`~tracker.models.Shop` being recorded for.
        """
        self.shop = shop
        self.errors_by_product = {}
        super().__init__(*args, **kwargs)

    def clean(self):
        """Parse the posted quantities, ignoring blanks and non-numbers.

        Products the shop cannot sell are rejected rather than silently dropped,
        so a tampered POST cannot create a sale the shop is not stocked for.
        """
        cleaned = super().clean()
        if self.shop is None:
            return cleaned

        stocked = {
            (sp.product_id): sp
            for sp in ShopProduct.objects.filter(shop=self.shop, is_active=True)
        }
        quantities = {}
        for raw_key, raw_value in self.data.items():
            if not raw_key.startswith("qty_"):
                continue
            try:
                product_id = int(raw_key[4:])
            except ValueError:
                continue

            if raw_value is None or not str(raw_value).strip():
                continue  # blank means "not sold", which is the normal case

            if product_id not in stocked:
                self.add_error(None, "One of the products is not stocked by this shop.")
                continue

            try:
                units = Decimal(str(raw_value).strip())
            except (InvalidOperation, TypeError, ValueError):
                self.add_error(
                    None, f"{stocked[product_id].product}: enter a number, or leave it blank."
                )
                continue

            if units <= 0:
                self.add_error(
                    None,
                    f"{stocked[product_id].product}: quantity must be greater than zero.",
                )
                continue

            if len(quantities) >= MAX_SHEET_ROWS:
                self.add_error(None, "Too many products in one submission.")
                break

            quantities[product_id] = units

        cleaned["quantities"] = quantities
        return cleaned

    def save(self, recorder):
        """Create one :class:`~tracker.models.DailySale` per entered quantity.

        Prices are left unset on purpose so ``DailySale.save()`` locks them from
        the shop's price card and computes the money fields itself.

        Args:
            recorder: the :class:`~tracker.models.CustomUser` submitting the day.

        Returns:
            The list of created sales, in the order they were entered.
        """
        quantities = self.cleaned_data.get("quantities") or {}
        if not quantities:
            return []

        sales = [
            DailySale(
                shop=self.shop,
                product_id=product_id,
                units_sold=units,
                recorded_by=recorder,
            )
            for product_id, units in quantities.items()
        ]
        # save() per row (not bulk_create) so the model computes each sale's
        # revenue, cost and profit from the current price card.
        for sale in sales:
            sale.save()

        self.errors_by_product = {}
        return sales


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
