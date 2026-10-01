"""Tests for the ``sync_products`` management command.

The important behaviour here is that a stale or hand-edited price list can
never destroy recorded history: products that have sales or stock transactions
are kept even when they no longer appear in the sheet.
"""

from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pandas as pd
from django.core.management import call_command
from django.test import TestCase

from tracker.models import DailySale, Product, Transaction

COLUMNS = ["Product", "Cost Price", "Selling Price", "Category"]


def sample_rows():
    """A minimal price list containing only Gilbeys."""
    return [["Gilbeys", Decimal("100.00"), Decimal("150.00"), "250ml"]]


class SyncProductsCommandTests(TestCase):
    def run_sync(self, rows=None):
        """Run the command against a fake price list; return its output."""
        frame = pd.DataFrame(sample_rows() if rows is None else rows, columns=COLUMNS)
        out = StringIO()
        with patch(
            "tracker.management.commands.sync_products.pd.read_excel",
            return_value=frame,
        ):
            call_command("sync_products", stdout=out)
        return out.getvalue()

    def make_product(self, name="Guinness", category="cans"):
        return Product.objects.create(
            product_name=name,
            category=category,
            cost_price=Decimal("40.00"),
            selling_price=Decimal("50.00"),
        )

    def test_product_absent_from_price_list_is_removed(self):
        self.make_product(name="Old Cola", category="soda")

        out = self.run_sync()

        self.assertFalse(Product.objects.filter(product_name="Old Cola").exists())
        self.assertIn("1 removed", out)
        self.assertNotIn("Kept", out)

    def test_product_with_sales_is_kept_and_reported(self):
        product = self.make_product()
        DailySale.objects.create(
            product=product,
            shop="Fig Tree",
            quantity_sold=Decimal("3"),
            sell_price=Decimal("50.00"),
            cost_price=Decimal("40.00"),
        )

        out = self.run_sync()

        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertEqual(product.daily_sales.count(), 1)
        self.assertIn("Kept 1 product", out)
        self.assertIn("Guinness", out)
        self.assertIn("0 removed", out)

    def test_product_with_stock_transactions_is_kept(self):
        product = self.make_product()
        Transaction.objects.create(
            product=product,
            type="RESTOCK",
            quantity=10,
            prev_remaining=0,
            current_remaining=10,
        )

        out = self.run_sync()

        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertEqual(product.transactions.count(), 1)
        self.assertIn("Kept 1 product", out)

    def test_prices_are_updated_from_the_file(self):
        product = Product.objects.create(
            product_name="Gilbeys",
            category="250ml",
            cost_price=Decimal("10.00"),
            selling_price=Decimal("10.00"),
        )

        self.run_sync()

        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("100.00"))
        self.assertEqual(product.selling_price, Decimal("150.00"))

    def test_new_products_in_the_file_are_created(self):
        self.run_sync([["Jameson", Decimal("900.00"), Decimal("1200.00"), "350ml"]])

        self.assertTrue(
            Product.objects.filter(product_name="Jameson", category="350ml").exists()
        )
