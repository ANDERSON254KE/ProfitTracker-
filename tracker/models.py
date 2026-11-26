"""Data models for the multi-shop profit tracker.

The domain is deliberately split in three:

* :class:`MasterProduct` is the catalogue entry ("Gilbeys", 250ml) that is
  identical for every branch.
* :class:`ShopProduct` is the per-branch price card. The same product is sold
  at a different cost/selling price in every shop, so prices live here and
  never on the master record.
* :class:`DailySale` is one product's figures for one day. It copies (locks
  in) the prices from the :class:`ShopProduct` row it was sold at, so that
  later price edits never rewrite history. Rows written by the daily sheet
  carry the stock trail (opening -> additional -> total -> closing) and their
  units sold, revenue, cost and profit are computed server side in
  :meth:`DailySale.save` -- they are never accepted from a client.
"""

from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

#: Every monetary amount is a non-negative two-decimal KSh value.
MONEY_VALIDATORS = [MinValueValidator(Decimal("0.00"))]
#: Additional stock may be negative: it records products given out that day.
STOCK_MOVE_VALIDATORS = [MinValueValidator(Decimal("-999999999999.99"))]


class LocalDateField(models.DateField):
    """DateField whose ``auto_now``/``auto_now_add`` use Django's timezone.

    Django's stock implementation stamps ``datetime.date.today()`` -- the
    server OS clock. On a UTC host serving Nairobi those disagree between
    00:00 and 03:00 local time, so a sale recorded just after midnight would
    be filed under the previous day and disappear from "today". Writing
    :func:`django.utils.timezone.localdate` keeps the column in step with
    every view, which filters on the same function.
    """

    def pre_save(self, model_instance, add):
        if self.auto_now or (self.auto_now_add and add):
            value = timezone.localdate()
            setattr(model_instance, self.attname, value)
            return value
        return super().pre_save(model_instance, add)


class CustomUser(AbstractUser):
    """Project user, either the owner (sees everything) or a shop manager.

    ``assigned_shop`` is the authoritative link used for data scoping: a
    manager may only ever read or write rows belonging to that shop. It is
    kept in sync with :attr:`Shop.manager` by the management commands, but the
    access mixins read this field because it is always available on
    ``request.user``.
    """

    class Role(models.TextChoices):
        """Role choices. OWNER sees every shop, MANAGER sees only their own."""

        OWNER = "OWNER", "Owner"
        MANAGER = "MANAGER", "Manager"

    role = models.CharField(
        "Role", max_length=10, choices=Role.choices, default=Role.MANAGER
    )
    assigned_shop = models.ForeignKey(
        "tracker.Shop",
        verbose_name="Assigned Shop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_users",
        help_text="Only used for MANAGER users. Scopes everything they can see.",
    )
    id_number = models.CharField(
        "ID Number", max_length=50, blank=True, default=""
    )
    residency = models.CharField(
        "Residency", max_length=255, blank=True, default=""
    )
    guardian_name = models.CharField(
        "Guardian / Emergency Contact", max_length=255, blank=True, default=""
    )
    guardian_phone = models.CharField(
        "Guardian / Emergency Contact Number", max_length=50, blank=True, default=""
    )
    guardian_id_number = models.CharField(
        "Guardian / Emergency Contact ID Number", max_length=50, blank=True, default=""
    )

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

    def is_owner(self) -> bool:
        """Return ``True`` when this user may see every shop."""
        return self.role == self.Role.OWNER

    def is_manager(self) -> bool:
        """Return ``True`` when this user is limited to their assigned shop."""
        return self.role == self.Role.MANAGER

    def __str__(self) -> str:
        shop = self.assigned_shop.name if self.assigned_shop_id else "All shops"
        return f"{self.get_full_name() or self.username} ({self.get_role_display()} - {shop})"


class Shop(models.Model):
    """A physical branch, e.g. Fig Tree."""

    name = models.CharField("Name", max_length=120, unique=True)
    location = models.CharField("Location", max_length=255, blank=True, default="")
    manager = models.OneToOneField(
        "tracker.CustomUser",
        verbose_name="Manager",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_shop",
        help_text="Convenience link for the owner; access checks use assigned_shop.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class MasterProduct(models.Model):
    """A catalogue product, independent of any shop.

    The same name can exist in several categories ("Gilbeys" 250ml and
    "Gilbeys" 350ml), so the pair is what identifies a product.
    """

    name = models.CharField("Product", max_length=255)
    category = models.CharField("Category", max_length=100, blank=True, default="")
    is_manual = models.BooleanField(
        "Added Manually",
        default=False,
        help_text="Added from the Common product sheet; price-list syncs keep it active.",
    )

    class Meta:
        ordering = ["name", "category"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "category"], name="unique_master_product_name_category"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.category})" if self.category else self.name


class ShopProduct(models.Model):
    """The price of one :class:`MasterProduct` in one :class:`Shop`."""

    shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name="shop_products"
    )
    product = models.ForeignKey(
        MasterProduct, on_delete=models.CASCADE, related_name="shop_prices"
    )
    cost_price = models.DecimalField(
        "Cost Price", max_digits=12, decimal_places=2, validators=MONEY_VALIDATORS
    )
    selling_price = models.DecimalField(
        "Selling Price", max_digits=12, decimal_places=2, validators=MONEY_VALIDATORS
    )
    is_active = models.BooleanField(
        "Active", default=True, help_text="Untick to hide the product from this shop."
    )

    class Meta:
        ordering = ["shop__name", "product__name", "product__category"]
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "product"], name="unique_product_per_shop"
            )
        ]

    @property
    def unit_profit(self) -> Decimal:
        """Profit per single unit sold at these prices."""
        return self.selling_price - self.cost_price

    def __str__(self) -> str:
        return f"{self.product} @ {self.shop}"


class DailySale(models.Model):
    """One product's stock movement (and therefore sales) for one day.

    ``unit_cost_at_sale`` and ``unit_price_at_sale`` are locked in at the
    moment of the sale so that editing a shop's price card later changes
    nothing that has already been reported.

    Two kinds of row live here:

    * **Stock entry** (``total_stock`` is set) -- produced by the daily sheet.
      The manager enters additional stock, total stock and closing stock;
      opening stock is copied from the previous day's closing stock and
      ``units_sold`` is derived as ``total_stock - closing_stock``. Revenue,
      cost and profit then follow from the locked-in prices as usual.
    * **Plain entry** (``total_stock`` is NULL) -- written by the single-product
      sale form, where ``units_sold`` is supplied directly.
        * **Profit-sheet entry** (``is_profit_sheet_entry`` is true) -- one
            consolidated manual quantity per shop, product and selected date.
    """

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="sales")
    product = models.ForeignKey(
        MasterProduct, on_delete=models.PROTECT, related_name="daily_sales"
    )
    sale_date = LocalDateField("Sale Date", default=timezone.localdate, db_index=True)
    is_profit_sheet_entry = models.BooleanField(
        "Profit Sheet Entry",
        default=False,
        help_text="Marks the consolidated manual units entry for a shop, product and day.",
    )
    units_sold = models.DecimalField(
        "Units Sold",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    #: Stock trail. ``opening_stock`` is a snapshot of the previous day's
    #: closing stock; ``units_sold`` for these rows is derived from the other
    #: three, never posted by the browser.
    opening_stock = models.DecimalField(
        "Opening Stock",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=MONEY_VALIDATORS,
    )
    additional_stock = models.DecimalField(
        "Additional Stock",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=STOCK_MOVE_VALIDATORS,
    )
    total_stock = models.DecimalField(
        "Total Stock",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=MONEY_VALIDATORS,
    )
    closing_stock = models.DecimalField(
        "Closing Stock",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=MONEY_VALIDATORS,
    )
    stock_note = models.CharField(
        "Stock Note", max_length=200, blank=True, default=""
    )
    unit_cost_at_sale = models.DecimalField(
        "Unit Cost at Sale", max_digits=12, decimal_places=2
    )
    unit_price_at_sale = models.DecimalField(
        "Unit Price at Sale", max_digits=12, decimal_places=2
    )
    total_revenue = models.DecimalField(
        "Total Revenue", max_digits=14, decimal_places=2, editable=False, default=0
    )
    total_cost = models.DecimalField(
        "Total Cost", max_digits=14, decimal_places=2, editable=False, default=0
    )
    profit = models.DecimalField(
        "Profit", max_digits=14, decimal_places=2, editable=False, default=0
    )
    recorded_by = models.ForeignKey(
        CustomUser,
        verbose_name="Recorded By",
        on_delete=models.PROTECT,
        related_name="recorded_sales",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sale_date", "-id"]
        indexes = [
            models.Index(fields=["shop", "-sale_date"], name="dailysale_shop_date_idx"),
            models.Index(
                fields=["sale_date", "shop", "product"],
                name="dailysale_date_shop_prod_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "product", "sale_date"],
                condition=models.Q(is_profit_sheet_entry=True),
                name="unique_profit_sheet_sale_per_shop_product_day",
            ),
            # A stock entry is the whole day for that product, so rewriting the
            # daily sheet replaces it: the database refuses a second row even
            # when two submits (or a double-tap) race each other.
            models.UniqueConstraint(
                fields=["shop", "product", "sale_date"],
                condition=models.Q(total_stock__isnull=False),
                name="unique_stock_entry_per_shop_product_day",
            ),
        ]

    @property
    def is_stock_entry(self) -> bool:
        """Return ``True`` when this row came from the stock-based sheet."""
        return self.total_stock is not None

    @property
    def units_from_stock(self) -> Decimal:
        """Units sold implied by the stock figures (total - closing)."""
        return (
            Decimal(self.total_stock or 0) - Decimal(self.closing_stock or 0)
        ).quantize(Decimal("0.01"))

    @property
    def given_out(self):
        """Positive units given out when additional stock was entered negative."""
        if self.additional_stock is not None and self.additional_stock < 0:
            return (-self.additional_stock).quantize(Decimal("0.01"))
        return None

    def clean(self) -> None:
        """Lock the sale's prices to the shop's current price card.

        Raises:
            ValidationError: if the shop does not stock the product, the product
                is inactive, or a stock entry's closing stock exceeds its
                total stock.
        """
        if not self.shop_id or not self.product_id:
            return

        shop_product = ShopProduct.objects.filter(
            shop_id=self.shop_id, product_id=self.product_id
        ).first()
        if shop_product is None:
            raise ValidationError(
                {"product": "This product is not stocked by the selected shop."}
            )
        if not shop_product.is_active:
            raise ValidationError(
                {"product": "This product is inactive for the selected shop."}
            )

        self.unit_cost_at_sale = shop_product.cost_price
        self.unit_price_at_sale = shop_product.selling_price

        if self.is_stock_entry:
            # Physical stock can never imply a negative number of sales.
            if self.closing_stock is None or self.total_stock is None:
                raise ValidationError(
                    "A stock entry needs both total stock and closing stock."
                )
            if self.closing_stock > self.total_stock:
                raise ValidationError(
                    "Closing stock cannot be greater than total stock."
                )

    def save(self, *args, **kwargs) -> None:
        """Lock in prices if missing, then compute revenue, cost and profit.

        The money fields are ``editable=False`` and are always recomputed here
        so a crafted POST can never set them.
        """
        if self.unit_cost_at_sale is None or self.unit_price_at_sale is None:
            shop_product = ShopProduct.objects.filter(
                shop_id=self.shop_id, product_id=self.product_id
            ).first()
            if shop_product is not None:
                self.unit_cost_at_sale = shop_product.cost_price
                self.unit_price_at_sale = shop_product.selling_price

        # A stock entry derives its sales figure from the physical count:
        # units sold = total stock - closing stock. Never taken from a client.
        if self.is_stock_entry:
            derived = self.units_from_stock
            if derived < 0:
                raise ValidationError(
                    "Closing stock cannot be greater than total stock."
                )
            self.units_sold = derived

        units = Decimal(self.units_sold or 0)
        cost = Decimal(self.unit_cost_at_sale or 0)
        price = Decimal(self.unit_price_at_sale or 0)

        self.total_revenue = (price * units).quantize(Decimal("0.01"))
        self.total_cost = (cost * units).quantize(Decimal("0.01"))
        self.profit = (self.total_revenue - self.total_cost).quantize(Decimal("0.01"))

        super().save(*args, **kwargs)

    @property
    def sale_day(self) -> str:
        """The sale date in ISO format, for JSON serialisation."""
        return self.sale_date.isoformat() if self.sale_date else ""

    def __str__(self) -> str:
        return f"{self.product} x{self.units_sold} at {self.shop} on {self.sale_date}"


class ShopExpense(models.Model):
    """An expense entered for one shop on a specific trading day."""

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="expenses")
    legacy_source_id = models.PositiveBigIntegerField(
        null=True, blank=True, unique=True, editable=False
    )
    expense_date = LocalDateField("Expense Date", default=timezone.localdate, db_index=True)
    description = models.CharField("Description", max_length=200)
    amount = models.DecimalField(
        "Amount", max_digits=14, decimal_places=2, validators=MONEY_VALIDATORS
    )
    recorded_by = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, related_name="recorded_expenses"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        indexes = [models.Index(fields=["shop", "-expense_date"], name="expense_shop_date_idx")]

    def __str__(self) -> str:
        return f"{self.description}: {self.amount} at {self.shop} on {self.expense_date}"


class ShopDailyCash(models.Model):
    """The cash side of one shop's trading day: M-Pesa and the bills.

    Product sales already give the day's total sales, so only the payment
    split and the bills are kept here:

    * ``cash`` -- ``total sales - mpesa`` (what was taken in notes and coins).
    * ``balance`` -- ``cash + bill_paid - expenses - bill_out``, the money the
      shop carries into the next day.

    Expenses are never duplicated: they are the :class:`ShopExpense` rows for
    the same shop and day.
    """

    shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name="daily_cash_records"
    )
    cash_date = LocalDateField("Date", default=timezone.localdate, db_index=True)
    mpesa = models.DecimalField(
        "M-Pesa", max_digits=14, decimal_places=2, default=0, validators=MONEY_VALIDATORS
    )
    bill_out = models.DecimalField(
        "Bill Out",
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=MONEY_VALIDATORS,
        help_text="Credit given to customers that day.",
    )
    bill_paid = models.DecimalField(
        "Bill Paid",
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=MONEY_VALIDATORS,
        help_text="Money collected on earlier bills that day.",
    )
    recorded_by = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, related_name="recorded_daily_cash"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-cash_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "cash_date"], name="unique_shop_daily_cash_date"
            )
        ]

    def __str__(self) -> str:
        return f"{self.shop} cash on {self.cash_date}: M-Pesa {self.mpesa}"


class ShopDailyProfit(models.Model):
    """Saved daily sales, expense and net-profit calculation for one shop."""

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="daily_profit_records")
    profit_date = LocalDateField("Profit Date", default=timezone.localdate, db_index=True)
    sales_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    product_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gross_profit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    expense_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_profit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    calculated_by = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, related_name="daily_profit_calculations"
    )
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-profit_date", "shop__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "profit_date"],
                name="unique_shop_daily_profit_date",
            )
        ]
        indexes = [models.Index(fields=["shop", "-profit_date"], name="shop_daily_profit_date_idx")]

    def save(self, *args, **kwargs):
        self.net_profit = (self.gross_profit - self.expense_total).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.shop} profit on {self.profit_date}: {self.net_profit}"


class StockAddition(models.Model):
    """Stock the owner records by hand instead of on a daily stock sheet.

    The stock sheet can only say how much stock a shop had in total on a day.
    When the owner knows a delivery arrived but nobody filled the sheet, they
    enter it here. The stock-batch report treats it exactly like a sheet
    addition: units sold and profit are read from :class:`DailySale` rows
    dated on or after :attr:`added_on`, using the prices locked in there.
    """

    shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name="stock_additions"
    )
    product = models.ForeignKey(
        MasterProduct, on_delete=models.PROTECT, related_name="stock_additions"
    )
    added_on = LocalDateField("Added On", default=timezone.localdate, db_index=True)
    quantity = models.DecimalField(
        "Units Added",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    recorded_by = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, related_name="recorded_stock_additions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_on", "-created_at"]
        indexes = [
            models.Index(fields=["shop", "-added_on"], name="stock_addition_shop_date_idx")
        ]

    def clean(self) -> None:
        """Reject a product the chosen shop does not stock."""
        if not self.shop_id or not self.product_id:
            return
        if not ShopProduct.objects.filter(
            shop_id=self.shop_id, product_id=self.product_id
        ).exists():
            raise ValidationError(
                {"product": "This product is not stocked by the selected shop."}
            )

    def __str__(self) -> str:
        return f"{self.quantity} x {self.product} at {self.shop} on {self.added_on}"
