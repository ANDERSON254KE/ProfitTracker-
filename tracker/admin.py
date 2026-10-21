from django.contrib import admin
from .models import Product, Transaction, DailySale, ProductShopPrice, DailyExpense, DayTotals


class ProductShopPriceInline(admin.TabularInline):
    model = ProductShopPrice
    extra = 0
    fk_name = 'product'
    fields = ('shop', 'selling_price', 'cost_price')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'shop', 'category', 'cost_price', 'selling_price')
    search_fields = ('product_name', 'shop', 'category')
    inlines = [ProductShopPriceInline]
    show_full_result_count = False

@admin.register(ProductShopPrice)
class ProductShopPriceAdmin(admin.ModelAdmin):
    """Edit each shop's price list in one place.

    Filter by shop to see that branch's overrides, then edit selling/cost
    prices straight from the list (Save row buttons at the bottom). A blank
    cell means the shop follows the product's default price; a value equal
    to the default is cleared automatically on save.
    """

    list_display = ('product', 'shop', 'selling_price', 'cost_price',
                    'effective_selling', 'effective_cost')
    list_editable = ('selling_price', 'cost_price')
    list_filter = ('shop',)
    search_fields = ('product__product_name', 'shop')
    autocomplete_fields = ('product',)
    list_select_related = ('product',)
    ordering = ('shop', 'product__order', 'product__product_name')
    show_full_result_count = False

    @admin.display(description='Effective Selling')
    def effective_selling(self, obj):
        return (obj.selling_price if obj.selling_price is not None
                else obj.product.selling_price)

    @admin.display(description='Effective Cost')
    def effective_cost(self, obj):
        return (obj.cost_price if obj.cost_price is not None
                else obj.product.cost_price)

    def save_model(self, request, obj, form, change):
        """A value equal to the product default clears the override."""
        if obj.selling_price is not None \
                and obj.selling_price == obj.product.selling_price:
            obj.selling_price = None
        if obj.cost_price is not None \
                and obj.cost_price == obj.product.cost_price:
            obj.cost_price = None
        super().save_model(request, obj, form, change)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('product', 'type', 'quantity', 'profit', 'date')
    list_filter = ('type', 'date')
    search_fields = ('product__product_name',)
    show_full_result_count = False

@admin.register(DailySale)
class DailySaleAdmin(admin.ModelAdmin):
    list_display = ('product', 'shop', 'quantity_sold', 'sell_price', 'cost_price', 'profit', 'date')
    list_filter = ('date', 'shop')
    search_fields = ('product__product_name',)
    show_full_result_count = False

@admin.register(DailyExpense)
class DailyExpenseAdmin(admin.ModelAdmin):
    list_display = ('shop', 'date', 'description', 'amount')
    list_filter = ('date', 'shop')
    search_fields = ('description',)
    show_full_result_count = False


@admin.register(DayTotals)
class DayTotalsAdmin(admin.ModelAdmin):
    list_display = ('shop', 'date', 'total_sales', 'total_profit', 'total_expenditure')
    list_filter = ('date', 'shop')
    show_full_result_count = False
