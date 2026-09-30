"""Forms used by the tracker's views.

:class:`DailySaleForm` is the only place a client is allowed to name a product
and a quantity; the money is derived from the shop's price card by the model,
never posted by the browser.

:class:`DailySalesSheetForm` is the daily stock sheet: the manager posts
additional/total/closing stock per product, and the units sold -- and therefore
every money figure -- are derived server side.
"""

from decimal import Decimal, InvalidOperation

from django import forms
from django.utils import timezone

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

    The manager fills three boxes per product -- additional stock, total stock
    and closing stock -- and leaves untouched products blank. Opening stock is
    *not* posted: it comes from ``openings`` (the previous day's closing
    stock). Only stock figures cross the wire; units sold
    (``total - closing``) and every money figure are derived server side by
    :meth:`tracker.models.DailySale.save`.

    Only products the shop stocks at a cost above zero appear on the sheet;
    a row for a no-cost or unstocked product is rejected outright.

    One stock row exists per product per day: submitting again updates today's
    row rather than appending, so re-entering a corrected count never
    double-counts the day.
    """

    #: Input prefixes used by the sheet template.
    ADDITIONAL_PREFIX = "add_"
    TOTAL_PREFIX = "total_"
    CLOSING_PREFIX = "close_"

    def __init__(self, *args, shop=None, openings=None, day=None, **kwargs):
        """Scope the sheet to ``shop``'s active range.

        Args:
            shop: the :class:`~tracker.models.Shop` being recorded for.
            openings: mapping of ``product_id -> previous closing stock``.
            day: the ``datetime.date`` being recorded (defaults to today).
        """
        self.shop = shop
        self.openings = openings or {}
        self.day = day or timezone.localdate()
        self.warnings = []
        super().__init__(*args, **kwargs)

    @staticmethod
    def _parse(raw):
        """Return a non-negative Decimal, ``None`` for blank, or ``'bad'``."""
        if raw is None or not str(raw).strip():
            return None
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, TypeError, ValueError):
            return "bad"
        if value < 0:
            return "bad"
        return value

    def clean(self):
        """Parse and validate the per-product stock inputs.

        Products the shop cannot sell are rejected rather than silently dropped.
        A total that does not equal opening + additional only raises a warning:
        physical counts sometimes differ from the book, and the sale figure is
        derived from total minus closing regardless.
        """
        cleaned = super().clean()
        if self.shop is None:
            return cleaned

        stocked = {
            sp.product_id: sp
            for sp in ShopProduct.objects.filter(
                shop=self.shop, is_active=True, cost_price__gt=0
            )
        }
        entries = {}
        entered = 0

        product_ids = set()
        for raw_key in self.data:
            for prefix in (self.ADDITIONAL_PREFIX, self.TOTAL_PREFIX, self.CLOSING_PREFIX):
                if raw_key.startswith(prefix):
                    try:
                        product_ids.add(int(raw_key[len(prefix):]))
                    except ValueError:
                        pass

        for product_id in sorted(product_ids):
            additional = self._parse(self.data.get(self.ADDITIONAL_PREFIX + str(product_id)))
            total = self._parse(self.data.get(self.TOTAL_PREFIX + str(product_id)))
            closing = self._parse(self.data.get(self.CLOSING_PREFIX + str(product_id)))

            if additional is None and total is None and closing is None:
                continue  # untouched row: nothing to record

            entered += 1
            if entered > MAX_SHEET_ROWS:
                self.add_error(None, "Too many products in one submission.")
                break

            if product_id not in stocked:
                self.add_error(None, "One of the products is not stocked by this shop.")
                continue
            label = str(stocked[product_id].product)

            bad = [
                name
                for name, value in (
                    ("additional stock", additional),
                    ("total stock", total),
                    ("closing stock", closing),
                )
                if value == "bad"
            ]
            if bad:
                self.add_error(None, f"{label}: enter a positive number for {', '.join(bad)}.")
                continue

            if total is None or closing is None:
                self.add_error(
                    None,
                    f"{label}: total stock and closing stock are required "
                    "(leave the whole row blank if the product had no activity).",
                )
                continue

            if additional is None:
                additional = Decimal("0.00")

            if closing > total:
                self.add_error(
                    None,
                    f"{label}: closing stock ({closing:f}) cannot be greater than "
                    f"total stock ({total:f}).",
                )
                continue

            opening = Decimal(self.openings.get(product_id, Decimal("0.00")))
            if total != opening + additional:
                self.warnings.append(
                    f"{label}: total stock {total:f} does not match opening "
                    f"{opening:f} + additional {additional:f} = "
                    f"{opening + additional:f}. Saved anyway -- double-check the count."
                )

            entries[product_id] = {
                "opening": opening,
                "additional": additional,
                "total": total,
                "closing": closing,
            }

        cleaned["entries"] = entries
        return cleaned

    def save(self, recorder):
        """Upsert one stock row per entered product for ``self.day``.

        An existing row for the same shop/product/day is rewritten; otherwise a
        new one is created. Prices are left unset so ``DailySale.save()`` locks
        them from the shop's price card, derives units sold from the stock
        figures and computes the money fields itself.

        Args:
            recorder: the :class:`~tracker.models.CustomUser` submitting the day.

        Returns:
            The list of saved rows, in the order they were entered.
        """
        entries = self.cleaned_data.get("entries") or {}
        if not entries:
            return []

        existing = {
            sale.product_id: sale
            for sale in DailySale.objects.filter(
                shop=self.shop, sale_date=self.day, total_stock__isnull=False
            )
        }

        saved = []
        for product_id, figures in entries.items():
            sale = existing.get(product_id)
            if sale is None:
                sale = DailySale(
                    shop=self.shop,
                    product_id=product_id,
                    recorded_by=recorder,
                )
            else:
                sale.recorded_by = recorder
            sale.opening_stock = figures["opening"]
            sale.additional_stock = figures["additional"]
            sale.total_stock = figures["total"]
            sale.closing_stock = figures["closing"]
            sale.save()
            saved.append(sale)

        return saved


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
