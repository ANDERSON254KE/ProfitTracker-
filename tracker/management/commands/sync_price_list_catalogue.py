"""Add missing products from the master workbook without changing shop prices.

New products with no workbook prices are created with zero-valued shop price
cards. They remain unavailable for sales until each shop's prices are entered.

Usage::

    python manage.py sync_price_list_catalogue
    python manage.py sync_price_list_catalogue --file other.xlsx
"""

import os
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from openpyxl import load_workbook

from tracker.models import MasterProduct, Shop, ShopProduct


class Command(BaseCommand):
    """Safely add new catalogue entries and missing per-shop price cards."""

    help = "Add missing products from price list.xlsx without overwriting existing shop prices."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=None,
            help="Workbook path (defaults to BASE_DIR/price list.xlsx).",
        )

    @staticmethod
    def _text(value):
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _amount(value):
        if value is None or not str(value).strip():
            return Decimal("0.00")
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0.00")
        if not amount.is_finite() or amount < 0:
            return Decimal("0.00")
        return amount.quantize(Decimal("0.01"))

    @staticmethod
    def _is_section_heading(name):
        return name.casefold() == "cans" or bool(
            re.fullmatch(r"brand\s+\d+\s*ml", name, flags=re.IGNORECASE)
        )

    def handle(self, *args, **options):
        path = options["file"] or os.path.join(settings.BASE_DIR, "price list.xlsx")
        if not os.path.exists(path):
            raise CommandError(f"Price list not found: {path}")

        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
            sheet = workbook.active
            rows = list(sheet.iter_rows(min_row=2, values_only=True))
        except Exception as exc:
            raise CommandError(f"Could not read {path}: {exc}") from exc
        finally:
            if "workbook" in locals():
                workbook.close()

        shops = list(Shop.objects.all())
        if not shops:
            raise CommandError("No shops exist yet.")

        products_to_import = {}
        skipped_headings = 0
        for row in rows:
            if not row:
                continue
            name = self._text(row[0] if len(row) > 0 else None)
            category = self._text(row[3] if len(row) > 3 else None)
            if not name:
                continue
            if self._is_section_heading(name):
                skipped_headings += 1
                continue
            products_to_import.setdefault(
                (name, category),
                (
                    self._amount(row[1] if len(row) > 1 else None),
                    self._amount(row[2] if len(row) > 2 else None),
                ),
            )

        names = {name for name, _ in products_to_import}
        existing_products = {
            (product.name, product.category): product
            for product in MasterProduct.objects.filter(name__in=names)
        }
        new_products = [
            MasterProduct(name=name, category=category)
            for name, category in products_to_import
            if (name, category) not in existing_products
        ]
        created_products = len(new_products)
        created_cards = 0
        with transaction.atomic():
            if new_products:
                MasterProduct.objects.bulk_create(new_products, ignore_conflicts=True)

            products = {
                (product.name, product.category): product
                for product in MasterProduct.objects.filter(name__in=names)
            }
            product_ids = [product.pk for product in products.values()]
            existing_cards = set(
                ShopProduct.objects.filter(
                    shop__in=shops,
                    product_id__in=product_ids,
                ).values_list("shop_id", "product_id")
            )
            new_cards = []
            for key, (cost, selling) in products_to_import.items():
                product = products[key]
                for shop in shops:
                    if (shop.pk, product.pk) not in existing_cards:
                        new_cards.append(ShopProduct(
                            shop=shop,
                            product=product,
                            cost_price=cost,
                            selling_price=selling,
                        ))
            if new_cards:
                ShopProduct.objects.bulk_create(new_cards, ignore_conflicts=True)
                created_cards = len(new_cards)

        self.stdout.write(self.style.SUCCESS(
            f"Added {created_products} new product(s) and {created_cards} missing shop price card(s); "
            f"skipped {skipped_headings} section heading(s). Existing prices were left unchanged."
        ))
