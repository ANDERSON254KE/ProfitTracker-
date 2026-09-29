"""Load the master catalogue from ``price list.xlsx`` and price it for every shop.

The spreadsheet is the single source of truth for *which* products exist. Each
shop's cost/selling price comes from the existing :class:`ShopProduct` row when
one is already there, so re-running this command after a price change at the
branch never overwrites that shop's own pricing.

Usage::

    python manage.py import_price_list                     # every shop
    python manage.py import_price_list --shop "Fig Tree"   # one shop
    python manage.py import_price_list --file other.xlsx
"""

import os
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tracker.models import MasterProduct, Shop, ShopProduct


class Command(BaseCommand):
    """Import the price list into the catalogue and create per-shop price cards."""

    help = "Import products from price list.xlsx into MasterProduct and ShopProduct."

    def add_arguments(self, parser):
        """Declare the ``--file`` and ``--shop`` options."""
        parser.add_argument(
            "--file",
            dest="file",
            default=None,
            help="Path to the price list (defaults to BASE_DIR/price list.xlsx).",
        )
        parser.add_argument(
            "--shop",
            dest="shop",
            default=None,
            help="Only price this shop (by name). Default: every shop.",
        )
        parser.add_argument(
            "--sheet",
            dest="sheet",
            default="all",
            help="Worksheet name to read (default: 'all').",
        )

    def to_decimal(self, value, default=None):
        """Parse a spreadsheet cell into a :class:`~decimal.Decimal`."""
        if value is None:
            return default
        try:
            if pd.isna(value):
                return default
        except (TypeError, ValueError):
            pass
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return default

    def handle(self, *args, **options):
        """Read the spreadsheet and sync the catalogue plus price cards."""
        path = options["file"] or os.path.join(settings.BASE_DIR, "price list.xlsx")
        if not os.path.exists(path):
            raise CommandError(f"Price list not found: {path}")

        try:
            df = pd.read_excel(path, sheet_name=options["sheet"])
        except Exception as exc:  # pragma: no cover - depends on the file
            raise CommandError(f"Could not read {path}: {exc}")

        if options["shop"]:
            shops = list(Shop.objects.filter(name__iexact=options["shop"]))
            if not shops:
                raise CommandError(f"No shop named '{options['shop']}'.")
        else:
            shops = list(Shop.objects.all())
        if not shops:
            raise CommandError("No shops exist yet. Run setup_initial_data first.")

        created_products = created_prices = updated = skipped = deactivated = 0

        with transaction.atomic():
            seen_ids = set()

            for index, (_, row) in enumerate(df.iterrows()):
                name = self._clean(row.get("Product"))
                category = self._clean(row.get("Category"))
                cost = self.to_decimal(row.get("Cost Price"), Decimal("0.00"))
                selling = self.to_decimal(row.get("Selling Price"))

                if not name or selling is None:
                    skipped += 1
                    continue

                product, was_created = MasterProduct.objects.get_or_create(
                    name=name, category=category
                )
                if was_created:
                    created_products += 1

                seen_ids.add(product.pk)

                for shop in shops:
                    shop_product, made = ShopProduct.objects.get_or_create(
                        shop=shop,
                        product=product,
                        defaults={
                            "cost_price": cost,
                            "selling_price": selling,
                        },
                    )
                    if made:
                        created_prices += 1
                    else:
                        # Only nudge an existing price when the sheet changed it
                        # for a shop that had no bespoke price of its own.
                        updated += 1
                        if shop_product.cost_price != cost or shop_product.selling_price != selling:
                            shop_product.cost_price = cost
                            shop_product.selling_price = selling
                            shop_product.save(
                                update_fields=["cost_price", "selling_price"]
                            )
                        if not shop_product.is_active:
                            shop_product.is_active = True
                            shop_product.save(update_fields=["is_active"])
                            deactivated += 1

            # Anything that disappeared from the sheet is switched off rather
            # than deleted, so past sales keep their product reference.
            for shop in shops:
                stale = ShopProduct.objects.filter(shop=shop, is_active=True).exclude(
                    product_id__in=seen_ids
                )
                deactivated += stale.update(is_active=False)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {created_products} new product(s); {created_prices} price row(s) "
                f"created, {updated} existing row(s) checked, {skipped} row(s) skipped, "
                f"{deactivated} row(s) deactivated. Shops priced: {len(shops)}."
            )
        )

    @staticmethod
    def _clean(value):
        """Return a trimmed string for a spreadsheet cell, or '' when blank."""
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        return str(value).strip()
