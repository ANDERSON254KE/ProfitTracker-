import os
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from tracker.models import SHOP_CHOICES, Product, ProductShopPrice


class Command(BaseCommand):
    help = (
        "Sync per-shop price overrides from 'price list - <Shop>.xlsx' into the "
        "database. A sheet value that matches the product's default clears the "
        "override for that field, so the shop follows the master price again."
    )

    def to_dec(self, value):
        try:
            d = Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None
        return d if d.is_finite() else None

    def read_sheet(self, path):
        """Yield (name, category, cost, selling) for rows that carry a price."""
        df = pd.read_excel(path, sheet_name="all")
        for _, row in df.iterrows():
            name = row.get("Product")
            name = str(name).strip() if not pd.isna(name) else ""
            selling = self.to_dec(row.get("Selling Price"))
            if not name or selling is None:
                continue
            category = row.get("Category")
            category = str(category).strip() if not pd.isna(category) else ""
            yield name, category, self.to_dec(row.get("Cost Price")), selling

    def handle(self, *args, **options):
        created = updated = cleared = unmatched = sheets = 0
        skipped_files = []
        unmatched_names = []

        for shop, _label in SHOP_CHOICES:
            path = os.path.join(settings.BASE_DIR, f"price list - {shop}.xlsx")
            if not os.path.exists(path):
                skipped_files.append(os.path.basename(path))
                continue
            sheets += 1
            for name, category, cost, selling in self.read_sheet(path):
                product = Product.objects.filter(
                    product_name__iexact=name, category__iexact=category
                ).first()
                if product is None:
                    unmatched += 1
                    unmatched_names.append(f"{name} ({category or 'no category'})")
                    continue

                # Only a difference from the default becomes an override; a
                # matching value clears it so the shop follows the master price.
                cost_override = (
                    cost if (cost is not None and cost != product.cost_price) else None
                )
                sell_override = (
                    selling if selling != product.selling_price else None
                )

                override = ProductShopPrice.objects.filter(
                    product=product, shop=shop
                ).first()

                if override is None:
                    if cost_override is None and sell_override is None:
                        continue  # shop follows the defaults, nothing to store
                    ProductShopPrice.objects.create(
                        product=product,
                        shop=shop,
                        cost_price=cost_override,
                        selling_price=sell_override,
                    )
                    created += 1
                    continue

                if (
                    override.cost_price == cost_override
                    and override.selling_price == sell_override
                ):
                    continue

                if cost_override is None and sell_override is None:
                    override.delete()
                    cleared += 1
                    continue

                override.cost_price = cost_override
                override.selling_price = sell_override
                override.save(update_fields=["cost_price", "selling_price"])
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Shop prices synced from {sheets} sheet(s): {created} created, "
                f"{updated} updated, {cleared} cleared, {unmatched} row(s) had no "
                f"matching product. Overrides now: {ProductShopPrice.objects.count()}."
            )
        )
        if skipped_files:
            self.stdout.write(
                self.style.WARNING(f"No sheet for: {', '.join(skipped_files)}")
            )
        if unmatched_names:
            listed = ", ".join(sorted(set(unmatched_names)))
            self.stdout.write(
                self.style.WARNING(
                    f"{len(set(unmatched_names))} product(s) not in the master price "
                    f"list: {listed}. Run sync_products first to add them."
                )
            )
