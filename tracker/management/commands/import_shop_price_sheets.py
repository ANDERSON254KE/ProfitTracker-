"""Read the edited per-shop price sheets back into the database.

Counterpart to ``make_shop_price_sheets``: the owner edits the cost/selling
prices in ``price list - <shop>.xlsx`` and this command updates each shop's
:class:`~tracker.models.ShopProduct` rows from those files.

Rows are matched to the catalogue by product name + category. Products that do
not exist in the catalogue yet are reported and skipped -- add new products
with ``import_price_list`` first, so every shop shares the same catalogue.

Usage::

    python manage.py import_shop_price_sheets                 # all 'price list - *.xlsx'
    python manage.py import_shop_price_sheets "price list - Fig Tree.xlsx"
"""

import os
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tracker.models import MasterProduct, Shop, ShopProduct

#: Filename prefix written by make_shop_price_sheets.
FILE_PREFIX = "price list - "


class Command(BaseCommand):
    """Import the per-shop price workbooks."""

    help = "Update each shop's prices from its 'price list - <shop>.xlsx' file."

    def add_arguments(self, parser):
        """Declare the positional file list and the ``--dir`` option."""
        parser.add_argument(
            "files",
            nargs="*",
            help="Sheet(s) to import. Default: every 'price list - *.xlsx' in BASE_DIR.",
        )
        parser.add_argument(
            "--dir",
            dest="directory",
            default=None,
            help="Directory to search for sheets (default: BASE_DIR).",
        )

    @staticmethod
    def to_decimal(value, default=None):
        """Parse a spreadsheet cell into a :class:`~decimal.Decimal`."""
        if value is None:
            return default
        try:
            value = str(value).strip()
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return default
        if not value:
            return default
        try:
            return Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return default

    @staticmethod
    def clean(value):
        """Return a trimmed string for a spreadsheet cell, or '' when blank."""
        if value is None:
            return ""
        text = str(value).strip()
        if text.lower() == "nan":
            return ""
        return text

    def shop_for_file(self, path):
        """Map a workbook filename to a :class:`Shop`."""
        stem = os.path.splitext(os.path.basename(path))[0]
        if not stem.lower().startswith("price list"):
            return None
        shop_name = stem[len("price list"):].strip(" -_")
        if not shop_name:
            return None
        return Shop.objects.filter(name__iexact=shop_name).first()

    def handle(self, *args, **options):
        """Read every requested sheet and update the matching price cards."""
        directory = options["directory"] or settings.BASE_DIR
        files = options["files"]
        if not files:
            files = sorted(
                os.path.join(directory, name)
                for name in os.listdir(directory)
                if name.startswith(FILE_PREFIX) and name.endswith(".xlsx")
                and not name.startswith("~$")
            )
        if not files:
            raise CommandError(
                f"No '{FILE_PREFIX}*.xlsx' sheets found in {directory}. "
                "Run make_shop_price_sheets first."
            )

        grand_updated = grand_unchanged = grand_rows = grand_unknown = 0

        for path in files:
            if not os.path.exists(path):
                raise CommandError(f"File not found: {path}")

            shop = self.shop_for_file(path)
            if shop is None:
                raise CommandError(
                    f"Cannot match {os.path.basename(path)} to a shop. "
                    "The file must be named 'price list - <shop name>.xlsx'."
                )

            updated, unchanged, rows_read, unknown = self.import_file(path, shop)
            grand_updated += updated
            grand_unchanged += unchanged
            grand_rows += rows_read
            grand_unknown += unknown
            self.stdout.write(
                self.style.SUCCESS(
                    f"{shop.name}: {rows_read} row(s) read, {updated} price(s) updated, "
                    f"{unchanged} unchanged, {unknown} unknown product(s)."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {grand_rows} row(s) across {len(files)} sheet(s); "
                f"{grand_updated} price(s) updated, {grand_unchanged} unchanged, "
                f"{grand_unknown} unknown product(s) skipped."
            )
        )

    def import_file(self, path, shop):
        """Apply one workbook to one shop. Returns (updated, unchanged, rows, unknown)."""
        import pandas as pd

        try:
            df = pd.read_excel(path, sheet_name=0)
        except Exception as exc:  # pragma: no cover - depends on the file
            raise CommandError(f"Could not read {path}: {exc}")

        updated = unchanged = rows_read = unknown = 0

        with transaction.atomic():
            for _, row in df.iterrows():
                name = self.clean(row.get("Product"))
                category = self.clean(row.get("Category"))
                if not name:
                    continue
                rows_read += 1

                cost = self.to_decimal(row.get("Cost Price"))
                selling = self.to_decimal(row.get("Selling Price"))
                if selling is None:
                    unknown += 1
                    self.stdout.write(
                        f"  ! {name} ({category}): no selling price; skipped."
                    )
                    continue

                product = MasterProduct.objects.filter(
                    name=name, category=category
                ).first()
                if product is None:
                    # Fall back to a case-insensitive match on the same name.
                    product = MasterProduct.objects.filter(
                        name__iexact=name, category__iexact=category
                    ).first()
                if product is None:
                    unknown += 1
                    self.stdout.write(
                        f"  ! {name} ({category or 'no category'}): not in the "
                        "catalogue; add it with import_price_list first."
                    )
                    continue

                card = ShopProduct.objects.filter(shop=shop, product=product).first()
                new_cost = cost if cost is not None else Decimal("0.00")
                new_selling = selling.quantize(Decimal("0.01"))

                if card is None:
                    ShopProduct.objects.create(
                        shop=shop,
                        product=product,
                        cost_price=new_cost.quantize(Decimal("0.01")),
                        selling_price=new_selling,
                    )
                    updated += 1
                elif card.cost_price != new_cost or card.selling_price != new_selling:
                    card.cost_price = new_cost.quantize(Decimal("0.01"))
                    card.selling_price = new_selling
                    card.save(update_fields=["cost_price", "selling_price"])
                    updated += 1
                else:
                    unchanged += 1

        return updated, unchanged, rows_read, unknown
