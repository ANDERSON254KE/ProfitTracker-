import os
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count

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
        for idx, (_, row) in enumerate(df.iterrows()):
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
                defaults={"cost_price": cost, "selling_price": selling, "order": idx},
            )
            if was:
                created += 1
            else:
                obj.cost_price = cost
                obj.selling_price = selling
                obj.order = idx
                obj.save()
                updated += 1

        # Remove products that no longer exist in the price list. Products
        # with recorded sales or transactions are kept instead: deleting them
        # cascades to that history, and a stale/edited price list must never be
        # able to erase past results. They are reported so the owner can either
        # restore the row in the sheet or retire them deliberately.
        removed = 0
        kept_with_history = []
        if price_keys:
            candidates = Product.objects.annotate(
                n_sales=Count("daily_sales", distinct=True),
                n_transactions=Count("transactions", distinct=True),
            )
            for product in candidates:
                pkey = (product.product_name.strip().lower(), (product.category or "").strip().lower())
                if pkey in price_keys:
                    continue
                history = product.n_sales + product.n_transactions
                if history:
                    kept_with_history.append((product, history))
                    continue
                product.delete()
                removed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Sync complete: {created} created, {updated} updated, "
                f"{skipped} skipped (no price), {removed} removed (not in price list). "
                f"Total products: {Product.objects.count()}"
            )
        )
        if kept_with_history:
            listed = ", ".join(
                f"{product.product_name} ({product.category or 'no category'}: {history})"
                for product, history in kept_with_history
            )
            self.stdout.write(
                self.style.WARNING(
                    f"Kept {len(kept_with_history)} product(s) missing from the price list "
                    f"because they have recorded history: {listed}. "
                    "Add them back to price list.xlsx to manage their prices."
                )
            )
