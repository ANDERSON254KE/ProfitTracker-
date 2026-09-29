"""Data models for the multi-shop profit tracker.

The domain is deliberately split in three:

* :class:`MasterProduct` is the catalogue entry ("Gilbeys", 250ml) that is
  identical for every branch.
* :class:`ShopProduct` is the per-branch price card. The same product is sold
  at a different cost/selling price in every shop, so prices live here and
  never on the master record.
* :class:`DailySale` is a single sale line. It copies (locks in) the prices
  from the :class:`ShopProduct` row it was sold at, so that later price edits
  never rewrite history. Revenue, cost and profit are computed server side in
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
    """One product sold on one day at one shop.

    ``unit_cost_at_sale`` and ``unit_price_at_sale`` are locked in at the
    moment of the sale so that editing a shop's price card later never changes
    a profit figure that has already been reported.
    """

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="sales")
    product = models.ForeignKey(
        MasterProduct, on_delete=models.PROTECT, related_name="daily_sales"
    )
    sale_date = models.DateField("Sale Date", auto_now_add=True, db_index=True)
    units_sold = models.DecimalField(
        "Units Sold",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
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
        indexes = [models.Index(fields=["shop", "-sale_date"], name="dailysale_shop_date_idx")]

    def clean(self) -> None:
        """Lock the sale's prices to the shop's current price card.

        Raises:
            ValidationError: if the shop does not stock the product, or has it
                marked inactive.
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
