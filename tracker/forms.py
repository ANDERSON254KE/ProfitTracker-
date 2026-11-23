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
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    DailySale,
    MasterProduct,
    Shop,
    ShopDailyCash,
    ShopProduct,
    StockAddition,
)

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


class ShopDailyCashForm(forms.ModelForm):
    """M-Pesa and bill figures for one shop's trading day.

    Total sales, cash and the balance carried forward are never posted: they
    are derived by the view from the day's product sales and expenses.
    """

    class Meta:
        model = ShopDailyCash
        fields = ["mpesa", "bill_out", "bill_paid"]
        widgets = {
            name: forms.NumberInput(
                attrs={
                    "inputmode": "decimal",
                    "step": "0.01",
                    "min": "0",
                    "class": (
                        "block w-full rounded-lg border border-slate-300 bg-white "
                        "px-3 py-2 text-right text-sm tabular-nums shadow-sm "
                        "focus:border-indigo-500 focus:ring-indigo-500"
                    ),
                }
            )
            for name in ("mpesa", "bill_out", "bill_paid")
        }

    def __init__(self, *args, **kwargs):
        """Let any of the three boxes be left empty, which means zero."""
        super().__init__(*args, **kwargs)
        for name in ("mpesa", "bill_out", "bill_paid"):
            self.fields[name].required = False


class DailySalesSheetForm(forms.Form):
    """Record a whole trading day for one shop in a single submit.

    The manager fills three boxes per product -- opening stock, additional
    stock and closing stock -- and leaves untouched products blank. Opening
    stock defaults to ``openings`` (the previous day's closing stock) and is
    only taken from the browser when the manager overrides it. Total stock is
    ``opening + additional``; a ``total_`` value is still accepted for older
    clients but is not required. Additional stock may be negative, meaning the
    manager gave products out; a ``note_`` value records who received them.
    Only stock figures cross the wire; units sold (``total - closing``) and
    every money figure are derived server side by
    :meth:`tracker.models.DailySale.save`.

    Only products the shop stocks appear on the sheet; a row for an unstocked
    product is rejected outright. A product with opening stock greater than
    zero must have a closing count before the sheet can be saved.

    One row exists per product per day, whatever created it: submitting again
    rewrites that day's row rather than appending, so a corrected count -- or a
    manual entry already made for the same product and day -- is replaced
    instead of double-counted.
    """

    #: Input prefixes used by the sheet template.
    OPENING_PREFIX = "open_"
    ADDITIONAL_PREFIX = "add_"
    TOTAL_PREFIX = "total_"
    CLOSING_PREFIX = "close_"
    NOTE_PREFIX = "note_"

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
    def _parse(raw, allow_negative=False):
        """Return a Decimal, ``None`` for blank, or ``'bad'``.

        Additional stock allows negatives: a negative value records products
        given out that day. Counts and totals stay non-negative.
        """
        if raw is None or not str(raw).strip():
            return None
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, TypeError, ValueError):
            return "bad"
        if value < 0 and not allow_negative:
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
            for sp in ShopProduct.objects.filter(shop=self.shop, is_active=True)
            .select_related("product")
        }
        entries = {}
        entered = 0

        product_ids = set()
        for raw_key in self.data:
            for prefix in (
                self.OPENING_PREFIX,
                self.ADDITIONAL_PREFIX,
                self.TOTAL_PREFIX,
                self.CLOSING_PREFIX,
            ):
                if raw_key.startswith(prefix):
                    try:
                        product_ids.add(int(raw_key[len(prefix):]))
                    except ValueError:
                        pass

        for product_id in sorted(product_ids):
            opening = self._parse(self.data.get(self.OPENING_PREFIX + str(product_id)))
            additional = self._parse(
                self.data.get(self.ADDITIONAL_PREFIX + str(product_id)),
                allow_negative=True,
            )
            total = self._parse(self.data.get(self.TOTAL_PREFIX + str(product_id)))
            closing = self._parse(self.data.get(self.CLOSING_PREFIX + str(product_id)))

            if product_id not in stocked:
                self.add_error(None, "One of the products is not stocked by this shop.")
                continue
            label = str(stocked[product_id].product)

            # A product that carried stock into today must be counted out today.
            # The opening box is pre-filled, so it cannot be ignored just because
            # the manager left the other boxes untouched.
            effective_opening = opening
            if effective_opening in (None, "bad"):
                effective_opening = Decimal(
                    str(self.openings.get(product_id, Decimal("0.00")))
                )
            if isinstance(effective_opening, Decimal) and effective_opening > 0 and closing is None:
                self.add_error(
                    None,
                    f"{label}: closing stock is required because opening stock was "
                    f"{effective_opening:f}.",
                )
                continue

            # Only the three stock boxes the manager types into decide whether a
            # row is entered; an opening of zero with no activity is fine.
            if additional is None and total is None and closing is None:
                continue

            entered += 1
            if entered > MAX_SHEET_ROWS:
                self.add_error(None, "Too many products in one submission.")
                break

            bad = [
                name
                for name, value in (
                    ("opening stock", opening),
                    ("additional stock", additional),
                    ("total stock", total),
                    ("closing stock", closing),
                )
                if value == "bad"
            ]
            if bad:
                self.add_error(
                    None,
                    f"{label}: enter a valid number for {', '.join(bad)} "
                    "(only additional stock may be negative, for goods given out).",
                )
                continue

            if closing is None:
                self.add_error(
                    None,
                    f"{label}: closing stock is required "
                    "(leave the whole row blank if the product had no activity).",
                )
                continue

            if additional is None:
                additional = Decimal("0.00")

            if opening is None:
                opening = Decimal(self.openings.get(product_id, Decimal("0.00")))

            # Total stock is what the shelf should hold: opening + additions.
            # Nothing was typed for it, so derive it from the two figures.
            if total is None:
                total = opening + additional

            if total < 0:
                self.add_error(
                    None,
                    f"{label}: total stock cannot be negative "
                    f"({opening:f} + {additional:f} = {total:f}).",
                )
                continue

            if closing > total:
                self.add_error(
                    None,
                    f"{label}: closing stock ({closing:f}) cannot be greater than "
                    f"total stock ({total:f}).",
                )
                continue

            if self.data.get(self.TOTAL_PREFIX + str(product_id)) is not None:
                typed_total = self._parse(
                    self.data.get(self.TOTAL_PREFIX + str(product_id))
                )
                if typed_total is not None and typed_total != opening + additional:
                    self.warnings.append(
                        f"{label}: total stock {typed_total:f} does not match opening "
                        f"{opening:f} + additional {additional:f} = "
                        f"{opening + additional:f}. Saved anyway -- double-check the count."
                    )

            note = (self.data.get(self.NOTE_PREFIX + str(product_id)) or "").strip()
            entries[product_id] = {
                "opening": opening,
                "additional": additional,
                "total": total,
                "closing": closing,
                "note": note[:200],
                "cost": stocked[product_id].cost_price,
                "price": stocked[product_id].selling_price,
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

        # Match every row for the day, not just stock entries: a product that
        # was already recorded by hand (profit sheet or a legacy single sale)
        # is rewritten into today's stock row instead of gaining a second row
        # whose units would be added to the day's sales twice.
        existing = {}
        extras = {}
        for sale in (
            DailySale.objects.filter(shop=self.shop, sale_date=self.day)
            .order_by("pk")
        ):
            if sale.product_id in existing:
                extras.setdefault(sale.product_id, []).append(sale)
            else:
                existing[sale.product_id] = sale

        def fill(sale, figures):
            """Put one product's submitted figures on ``sale``."""
            sale.recorded_by = recorder
            sale.is_profit_sheet_entry = False
            sale.opening_stock = figures["opening"]
            sale.additional_stock = figures["additional"]
            sale.total_stock = figures["total"]
            sale.closing_stock = figures["closing"]
            sale.stock_note = figures["note"]
            sale.unit_cost_at_sale = figures["cost"]
            sale.unit_price_at_sale = figures["price"]
            return sale

        saved = []
        for product_id, figures in entries.items():
            sale = existing.get(product_id)
            # A repeated bulk import can leave identical rows beside the one we
            # rewrite; drop them so this product is not counted twice.
            for extra in extras.get(product_id, ()):
                extra.delete()
            if sale is None:
                sale = fill(
                    DailySale(shop=self.shop, product_id=product_id, sale_date=self.day),
                    figures,
                )
            else:
                fill(sale, figures)
            try:
                with transaction.atomic():
                    sale.save()
            except IntegrityError:
                # A parallel submit (a double-tap on Save) wrote this product's
                # row first. Rewrite that row instead of adding a second one.
                sale = DailySale.objects.get(
                    shop=self.shop,
                    product_id=product_id,
                    sale_date=self.day,
                    total_stock__isnull=False,
                )
                fill(sale, figures).save()
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


class StockAdditionForm(forms.ModelForm):
    """Record stock that arrived but never went through a daily stock sheet.

    The shop and product are both chosen freely here because the owner is
    correcting history for any branch; :meth:`StockAddition.clean` rejects a
    product the chosen shop does not stock.
    """

    class Meta:
        model = StockAddition
        fields = ["shop", "product", "added_on", "quantity"]
        widgets = {
            "shop": forms.Select(
                attrs={
                    "class": (
                        "rounded-lg border border-slate-300 bg-white px-3 py-2.5 "
                        "text-sm shadow-sm focus:border-indigo-500 focus:ring-indigo-500"
                    )
                }
            ),
            "product": forms.Select(
                attrs={
                    "class": (
                        "min-w-[16rem] rounded-lg border border-slate-300 bg-white px-3 py-2.5 "
                        "text-sm shadow-sm focus:border-indigo-500 focus:ring-indigo-500"
                    )
                }
            ),
            "added_on": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": (
                        "rounded-lg border border-slate-300 bg-white px-3 py-2.5 "
                        "text-sm shadow-sm focus:border-indigo-500 focus:ring-indigo-500"
                    ),
                }
            ),
            "quantity": forms.NumberInput(
                attrs={
                    "step": "1",
                    "min": "0.01",
                    "class": (
                        "w-28 rounded-lg border border-slate-300 bg-white px-3 py-2.5 "
                        "text-sm shadow-sm focus:border-indigo-500 focus:ring-indigo-500"
                    ),
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shop"].queryset = Shop.objects.all()
        self.fields["product"].queryset = MasterProduct.objects.all()
        self.fields["product"].label_from_instance = lambda product: (
            f"{product.name} ({product.category})" if product.category else product.name
        )
