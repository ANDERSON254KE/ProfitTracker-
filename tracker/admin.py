from django.contrib import admin
from .models import Product, Transaction, DailySale, ProductShopPrice, DailyExpense, DayTotals


class ProductShopPriceInline(admin.TabularInline):
    model = ProductShopPrice
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'shop', 'category', 'cost_price', 'selling_price')
    search_fields = ('product_name', 'shop', 'category')
    inlines = [ProductShopPriceInline]
    show_full_result_count = False

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
