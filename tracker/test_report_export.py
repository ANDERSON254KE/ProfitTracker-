"""The report Excel download keeps Chambers Point separate.

Chambers Point has its own workbook (`?shop=chambers`); the default download
is every other shop, mirroring how the day export already splits the two.
"""

from datetime import date
from decimal import Decimal
from io import BytesIO

import pandas as pd
from django.test import TestCase
from django.urls import reverse

from tracker.models import DailySale, Product


class ReportExportSeparationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = date.today()
        cls.product = Product.objects.create(
            product_name="Gilbeys", category="250ml",
            cost_price=Decimal("416.00"), selling_price=Decimal("550.00"),
        )
        DailySale.objects.create(
            product=cls.product, shop="Fig Tree",
            quantity_sold=Decimal("3"), sell_price=Decimal("550.00"),
            date=cls.today,
        )
        DailySale.objects.create(
            product=cls.product, shop="Chambers Point",
            quantity_sold=Decimal("7"), sell_price=Decimal("550.00"),
            date=cls.today,
        )

    def _get(self, **params):
        response = self.client.get(
            reverse("daily_sales_report_export"),
            {"start": self.today.isoformat(), "end": self.today.isoformat(), **params},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def _shops_in(self, response):
        df = pd.read_excel(BytesIO(response.content))
        return set(df["Shop"].dropna().astype(str))

    def test_report_page_offers_both_downloads(self):
        response = self.client.get(
            reverse("daily_sales_report"),
            {"start": self.today.isoformat(), "end": self.today.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All Shops")
        self.assertContains(response, "shop=chambers")

    def test_default_download_leaves_chambers_out(self):
        response = self._get()
        self.assertIn('filename="report_',
                      response["Content-Disposition"])
        shops = self._shops_in(response)
        self.assertIn("Fig Tree", shops)
        self.assertNotIn("Chambers Point", shops)
        self.assertNotIn("CHAMBERS POINT", shops)

    def test_chambers_download_is_its_own_file(self):
        response = self._get(shop="chambers")
        self.assertIn('filename="chambers_report_',
                      response["Content-Disposition"])
        shops = self._shops_in(response)
        self.assertIn("Chambers Point", shops)
        self.assertNotIn("Fig Tree", shops)
