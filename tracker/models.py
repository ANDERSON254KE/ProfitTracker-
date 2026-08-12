from django.db import models
from django.utils import timezone
from decimal import Decimal

class Product(models.Model):
    product_name = models.CharField("Product", max_length=255)
    shop = models.CharField("Shop/Branch", max_length=255, blank=True, null=True)
    category = models.CharField("Category", max_length=100, blank=True, null=True)
    cost_price = models.DecimalField("Cost Price", max_digits=10, decimal_places=2)
    selling_price = models.DecimalField("Default Selling Price", max_digits=10, decimal_places=2)
    
    def __str__(self):
        return f"{self.product_name} ({self.shop if self.shop else 'N/A'})"

class Transaction(models.Model):
    TYPE_CHOICES = [
        ('RESTOCK', 'Restock (Add Stock)'),
        ('SALES_CHECK', 'Audit (Record Sales & Profit)'),
    ]
    
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='transactions')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    quantity = models.IntegerField("Quantity Added", default=0)
    prev_remaining = models.IntegerField("Previous Count", default=0)
    current_remaining = models.IntegerField("Current Count", default=0)
    sell_price_used = models.DecimalField("Sell Price Used", max_digits=10, decimal_places=2, null=True, blank=True)
    units_used = models.IntegerField("Units Used", default=0)
    profit = models.DecimalField("Profit Earned", max_digits=10, decimal_places=2, default=0)
    date = models.DateField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        """Logic to calculate profit on save (Audit only)."""
        if self.type == 'SALES_CHECK':
            # Units used = (Prev + Added) - Current
            self.units_used = (self.prev_remaining + self.quantity) - self.current_remaining
            
            # Ensure we are working with Decimal for currency calculations
            sell_price = Decimal(str(self.sell_price_used)) if self.sell_price_used else Decimal('0.00')
            cost_price = Decimal(str(self.product.cost_price))
            
            # Profit = (Sell Price Used * Used) - (Cost Price * Used)
            self.profit = (sell_price * self.units_used) - (cost_price * self.units_used)
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-date', '-created_at']

SHOP_CHOICES = [
    ('Fig Tree', 'Fig Tree'),
    ('Empire Shop', 'Empire Shop'),
    ('Emirates', 'Emirates'),
    ('Small City', 'Small City'),
]

class DailySale(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='daily_sales')
    shop = models.CharField("Shop", max_length=50, choices=SHOP_CHOICES, blank=True, default='')
    quantity_sold = models.DecimalField("Amount Sold", max_digits=10, decimal_places=2, default=0)
    sell_price = models.DecimalField("Selling Price Used", max_digits=10, decimal_places=2)
    cost_price = models.DecimalField("Buying Price Used", max_digits=10, decimal_places=2, null=True, blank=True)
    profit = models.DecimalField("Profit Earned", max_digits=10, decimal_places=2, default=0)
    date = models.DateField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        cost = self.cost_price if self.cost_price is not None else self.product.cost_price
        self.profit = (self.sell_price - cost) * self.quantity_sold
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-date', '-created_at']
