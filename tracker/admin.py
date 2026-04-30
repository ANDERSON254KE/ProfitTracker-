from django.contrib import admin
from .models import Product, Transaction

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'shop', 'category', 'cost_price', 'selling_price')
    search_fields = ('product_name', 'shop', 'category')

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('product', 'type', 'quantity', 'profit', 'date')
    list_filter = ('type', 'date')
    search_fields = ('product__product_name',)
