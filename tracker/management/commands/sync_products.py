import os
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from tracker.models import Product


class Command(BaseCommand):
    help = "Sync Product items and prices from price list.xlsx into the database."

    def to_dec(self, value, default=None):
        try:
            d = Decimal(str(value))
            return d if d.is_finite() else default
        except (InvalidOperation, TypeError, ValueError):
            return default

    def handle(self, *args, **options):
        path = os.path.join(settings.BASE_DIR, "price list.xlsx")
        df = pd.read_excel(path, sheet_name="all")
        created = updated = skipped = 0
        price_keys = set()
        for _, row in df.iterrows():
            name = row.get("Product")
            name = str(name).strip() if not pd.isna(name) else ""
            selling = self.to_dec(row.get("Selling Price"))
            if not name or selling is None:
                skipped += 1
                continue
            category = row.get("Category")
            category = str(category).strip() if not pd.isna(category) else ""
            cost = self.to_dec(row.get("Cost Price"), Decimal("0.00"))
            key = (name.lower(), category.lower())
            price_keys.add(key)
            obj, was = Product.objects.get_or_create(
                product_name=name,
                category=category,
                defaults={"cost_price": cost, "selling_price": selling},
            )
            if was:
                created += 1
            else:
                obj.cost_price = cost
                obj.selling_price = selling
                obj.save()
                updated += 1

        # Remove products that no longer exist in the price list.
        removed = 0
        if price_keys:
            for product in Product.objects.all():
                pkey = (product.product_name.strip().lower(), (product.category or "").strip().lower())
                if pkey not in price_keys:
                    product.delete()
                    removed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Sync complete: {created} created, {updated} updated, "
                f"{skipped} skipped (no price), {removed} removed (not in price list). "
                f"Total products: {Product.objects.count()}"
            )
        )
