"""Duplicate ``price list.xlsx`` into one editable workbook per shop.

Each shop sells the same catalogue at different prices, so the owner needs a
sheet per branch to edit. This command writes ``price list - <shop>.xlsx`` for
every shop, pre-filled with that shop's current cost and selling prices from
the database. Rows follow the order of the master ``price list.xlsx`` so the
copies line up with the sheet the owner already knows.

The files are meant to be edited by hand and then read back with
``import_shop_price_sheets``.

Usage::

    python manage.py make_shop_price_sheets               # every shop
    python manage.py make_shop_price_sheets --shop "Fig Tree"
    python manage.py make_shop_price_sheets --force        # overwrite copies
"""

import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tracker.models import Shop, ShopProduct
from tracker.price_list import price_list_rank

#: Header row written to every generated sheet.
HEADERS = ("Product", "Cost Price", "Selling Price", "Category")


class Command(BaseCommand):
    """Write one editable price workbook per shop."""

    help = "Create 'price list - <shop>.xlsx' for each shop with its own prices."

    def add_arguments(self, parser):
        """Declare the ``--shop``, ``--file`` and ``--force`` options."""
        parser.add_argument(
            "--shop",
            dest="shop",
            default=None,
            help="Only write the sheet for this shop (by name).",
        )
        parser.add_argument(
            "--file",
            dest="file",
            default=None,
            help="Master price list used for row order (default: BASE_DIR/price list.xlsx).",
        )
        parser.add_argument(
            "--dir",
            dest="directory",
            default=None,
            help="Directory to write the sheets into (default: BASE_DIR).",
        )
        parser.add_argument(
            "--force",
            dest="force",
            action="store_true",
            help="Overwrite copies that already exist (their edits are lost).",
        )

    @staticmethod
    def shop_filename(shop_name):
        """Return the workbook filename for a shop name."""
        safe = re.sub(r'[\\/:*?"<>|]+', "", shop_name).strip()
        return f"price list - {safe}.xlsx"

    def handle(self, *args, **options):
        """Generate a workbook for each requested shop."""
        if options["shop"]:
            shops = list(Shop.objects.filter(name__iexact=options["shop"]))
            if not shops:
                raise CommandError(f"No shop named '{options['shop']}'.")
        else:
            shops = list(Shop.objects.all())
        if not shops:
            raise CommandError("No shops exist yet. Run setup_initial_data first.")

        base_dir = options["directory"] or settings.BASE_DIR
        # Row order comes from the master price list so every copy lines up
        # with the sheet the owner already knows (and with the daily sheet).
        rank = price_list_rank(options["file"])

        from openpyxl import Workbook

        written = skipped = rows_total = 0
        for shop in shops:
            path = os.path.join(base_dir, self.shop_filename(shop.name))
            if os.path.exists(path) and not options["force"]:
                skipped += 1
                self.stdout.write(
                    f"Skipped {os.path.basename(path)} (already exists; use --force to overwrite)."
                )
                continue

            cards = list(
                ShopProduct.objects.filter(shop=shop, is_active=True).select_related(
                    "product"
                )
            )
            # Follow the master price-list order when known, then name order.
            cards.sort(
                key=lambda card: (
                    rank.get((card.product.name, card.product.category), len(rank)),
                    card.product.name,
                    card.product.category,
                )
            )

            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "all"
            sheet.append(list(HEADERS))
            for card in cards:
                sheet.append(
                    [
                        card.product.name,
                        float(card.cost_price),
                        float(card.selling_price),
                        card.product.category,
                    ]
                )
            workbook.save(path)
            written += 1
            rows_total += len(cards)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Wrote {os.path.basename(path)} with {len(cards)} product(s)."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {written} sheet(s) written ({rows_total} rows), {skipped} skipped. "
                "Edit the files, then run import_shop_price_sheets."
            )
        )
