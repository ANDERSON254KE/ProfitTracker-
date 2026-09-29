"""Django admin registrations for the tracker.

Prices and computed money columns are read-only in the admin so that the
single source of truth for profit stays :meth:`tracker.models.DailySale.save`.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import CustomUser, DailySale, MasterProduct, Shop, ShopProduct


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """Adds role and assigned shop to the built-in user admin."""

    fieldsets = UserAdmin.fieldsets + (
        ("Tracker", {"fields": ("role", "assigned_shop")}),
    )
    list_display = ("username", "role", "assigned_shop", "is_active")
    list_filter = ("role", "is_active", "assigned_shop")


class ShopProductInline(admin.TabularInline):
    """Per-shop price card shown on the master product page."""

    model = ShopProduct
    extra = 0
    fields = ("shop", "cost_price", "selling_price", "is_active")


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    """Shop list with its manager and product count."""

    list_display = ("name", "location", "manager", "product_count")
    search_fields = ("name", "location")

    @admin.display(description="Products")
    def product_count(self, obj):
        """Return how many products this shop stocks."""
        return obj.shop_products.count()


@admin.register(MasterProduct)
class MasterProductAdmin(admin.ModelAdmin):
    """Catalogue of products with each shop's price inline."""

    list_display = ("name", "category", "shop_count")
    list_filter = ("category",)
    search_fields = ("name", "category")
    inlines = [ShopProductInline]

    @admin.display(description="Shops stocking this")
    def shop_count(self, obj):
        """Return how many shops carry this product."""
        return obj.shop_prices.count()


@admin.register(ShopProduct)
class ShopProductAdmin(admin.ModelAdmin):
    """One row per (shop, product) price card."""

    list_display = ("product", "shop", "cost_price", "selling_price", "unit_profit", "is_active")
    list_filter = ("shop", "is_active", "product__category")
    search_fields = ("product__name",)
    list_editable = ("cost_price", "selling_price", "is_active")


@admin.register(DailySale)
class DailySaleAdmin(admin.ModelAdmin):
    """Sales ledger; every money column is computed by the model."""

    list_display = (
        "sale_date", "shop", "product", "units_sold",
        "unit_price_at_sale", "unit_cost_at_sale",
        "total_revenue", "total_cost", "profit", "recorded_by",
    )
    list_filter = ("shop", "sale_date", "product__category")
    search_fields = ("product__name", "recorded_by__username")
    date_hierarchy = "sale_date"
    readonly_fields = ("total_revenue", "total_cost", "profit", "sale_date", "created_at")
    autocomplete_fields = ("product",)
