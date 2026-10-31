"""Smoke tests for the two new surfaces: the admin shop sheets
(add / delete / day totals) and the dashboard's analytics charts."""

import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from tracker.models import DailySale, DayTotals, Product


class AdminShopSheetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="sheet-user", password="pw", is_staff=True
        )
        cls.product = Product.objects.create(
            product_name="Gilbeys", category="250ml",
            cost_price=Decimal("416.00"), selling_price=Decimal("550.00"),
        )
        cls.url = reverse("admin_shop_sheet", args=["Fig Tree"])
        cls.today = date.today().isoformat()

    def setUp(self):
        self.client.force_login(self.staff)

    def test_anonymous_is_sent_to_admin_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_sheet_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fig Tree")
        self.assertContains(response, "Add row")

    def test_sheet_with_date_param_renders(self):
        response = self.client.get(self.url, {"date": self.today})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.today)

    def test_unknown_shop_is_404(self):
        response = self.client.get(reverse("admin_shop_sheet", args=["Nowhere"]))
        self.assertEqual(response.status_code, 404)

    def test_add_row_uses_shop_prices_when_blanked(self):
        response = self.client.post(self.url, {
            "action": "add", "date": self.today,
            "product": self.product.pk, "qty": "3",
        })
        self.assertEqual(response.status_code, 302)
        sale = DailySale.objects.get(shop="Fig Tree")
        self.assertEqual(sale.quantity_sold, Decimal("3"))
        self.assertEqual(sale.sell_price, Decimal("550.00"))
        self.assertEqual(sale.profit, (Decimal("550.00") - Decimal("416.00")) * 3)

    def test_add_row_without_quantity_creates_nothing(self):
        self.client.post(self.url, {
            "action": "add", "date": self.today,
            "product": self.product.pk, "qty": "",
        })
        self.assertFalse(DailySale.objects.exists())

    def test_delete_row_removes_only_that_shop(self):
        other = DailySale.objects.create(
            product=self.product, shop="Empire Shop",
            quantity_sold=Decimal("2"), sell_price=Decimal("550.00"),
            date=date.today(),
        )
        mine = DailySale.objects.create(
            product=self.product, shop="Fig Tree",
            quantity_sold=Decimal("2"), sell_price=Decimal("550.00"),
            date=date.today(),
        )
        response = self.client.post(self.url, {
            "action": "delete", "date": self.today, "sale": mine.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DailySale.objects.filter(shop="Fig Tree").exists())
        self.assertTrue(DailySale.objects.filter(pk=other.pk).exists())

    def test_day_totals_are_saved(self):
        self.client.post(self.url, {
            "action": "totals", "date": self.today,
            "total_sales": "1000", "total_profit": "300",
            "total_expenditure": "50",
        })
        totals = DayTotals.objects.get(shop="Fig Tree", date=date.today())
        self.assertEqual(totals.total_sales, Decimal("1000.00"))
        self.assertEqual(totals.total_expenditure, Decimal("50.00"))


class DailySalesSheetTests(TestCase):
    """The public daily-sales sheet: whole-sheet search + item deletion."""

    @classmethod
    def setUpTestData(cls):
        cls.product = Product.objects.create(
            product_name="Gilbeys", category="250ml",
            cost_price=Decimal("416.00"), selling_price=Decimal("550.00"),
        )

    def setUp(self):
        session = self.client.session
        session["daily_sales_shop"] = "Fig Tree"
        session.save()

    def test_sheet_ships_global_search_and_delete_controls(self):
        response = self.client.get(reverse("daily_sales"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="globalSearch"')
        self.assertContains(response, "delete-item")
        self.assertContains(response, 'data-name="Gilbeys 250ml"')

    def test_rows_ship_swipe_actions_and_the_inline_editor(self):
        response = self.client.get(reverse("daily_sales"))
        self.assertContains(response, 'class="sheet-row"')
        self.assertContains(response, "swipe-actions")
        self.assertContains(response, "edit-item")
        self.assertContains(response, 'id="editRowTemplate"')
        self.assertContains(response, 'data-category="250ml"')

    def test_update_item_edits_the_product_on_the_sheet(self):
        response = self.client.post(reverse("daily_sales"), {
            "entry_type": "update_item",
            "product_id": str(self.product.pk),
            "sale_date": date.today().isoformat(),
            "product_name": "Gilbeys Gold",
            "category": "750ml",
            "selling_price": "600",
            "cost_price": "430",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("edited=1", response.url)
        self.product.refresh_from_db()
        self.assertEqual(self.product.product_name, "Gilbeys Gold")
        self.assertEqual(self.product.category, "750ml")
        self.assertEqual(self.product.selling_price, Decimal("600.00"))
        self.assertEqual(self.product.cost_price, Decimal("430.00"))

    def test_update_item_blanks_leave_values_untouched(self):
        self.client.post(reverse("daily_sales"), {
            "entry_type": "update_item",
            "product_id": str(self.product.pk),
            "sale_date": date.today().isoformat(),
            "product_name": "   ",
            "category": "",
            "selling_price": "",
            "cost_price": "",
        })
        self.product.refresh_from_db()
        self.assertEqual(self.product.product_name, "Gilbeys")
        self.assertEqual(self.product.selling_price, Decimal("550.00"))

    def test_update_item_with_invalid_pk_is_a_safe_no_op(self):
        response = self.client.post(reverse("daily_sales"), {
            "entry_type": "update_item",
            "product_id": "nope",
            "sale_date": date.today().isoformat(),
            "product_name": "Renamed",
            "category": "soda",
        })
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.product_name, "Gilbeys")

    def test_edited_flag_renders_the_confirmation(self):
        response = self.client.get(reverse("daily_sales"), {"edited": "1"})
        self.assertContains(response, "Product updated")

    def test_delete_item_removes_product_and_its_sales(self):
        sale = DailySale.objects.create(
            product=self.product, shop="Fig Tree",
            quantity_sold=Decimal("2"), sell_price=Decimal("550.00"),
            date=date.today(),
        )
        response = self.client.post(reverse("daily_sales"), {
            "entry_type": "delete_item",
            "product_id": str(self.product.pk),
            "sale_date": date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("deleted=1", response.url)
        self.assertFalse(Product.objects.filter(pk=self.product.pk).exists())
        self.assertFalse(DailySale.objects.filter(pk=sale.pk).exists())

    def test_delete_item_with_an_invalid_pk_is_a_safe_no_op(self):
        response = self.client.post(reverse("daily_sales"), {
            "entry_type": "delete_item",
            "product_id": "not-a-number",
            "sale_date": date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_deleted_flag_renders_the_confirmation(self):
        response = self.client.get(reverse("daily_sales"), {"deleted": "1"})
        self.assertContains(response, "Item deleted")


class DashboardAnalyticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.product = Product.objects.create(
            product_name="Gilbeys", category="250ml",
            cost_price=Decimal("416.00"), selling_price=Decimal("550.00"),
        )
        DailySale.objects.create(
            product=cls.product, shop="Fig Tree",
            quantity_sold=Decimal("4"), sell_price=Decimal("550.00"),
            date=date.today(),
        )

    def test_dashboard_ships_analytics_payload_and_charts(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sd-analytics"')
        for canvas in ("chart-bar", "chart-share", "chart-line", "chart-shops",
                       "chart-weekday"):
            self.assertContains(response, canvas)

        payload = json.loads(response.context["analytics_json"])
        self.assertEqual(set(payload), {"day", "week", "month", "weekday"})
        self.assertEqual(payload["day"]["bar"]["labels"], ["Gilbeys"])
        self.assertEqual(payload["day"]["bar"]["values"], [4.0])
        self.assertEqual(payload["day"]["shops"]["labels"], ["Fig Tree"])
        self.assertEqual(payload["day"]["units"], 4.0)
        # "when" insights: busiest date in the range + day-of-week rhythm
        self.assertEqual(payload["day"]["peak"]["units"], 4.0)
        self.assertEqual(payload["day"]["peak"]["date"], date.today().isoformat())
        self.assertEqual(payload["weekday"]["labels"],
                         ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertEqual(len(payload["weekday"]["values"]), 7)
        self.assertEqual(payload["weekday"]["values"][date.today().weekday()], 4.0)

    def test_empty_ranges_still_render(self):
        DailySale.objects.all().delete()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.context["analytics_json"])
        self.assertEqual(payload["week"]["top"], [])
        self.assertEqual(payload["month"]["shops"], {"labels": [], "values": []})
        self.assertIsNone(payload["day"]["peak"])
        self.assertEqual(payload["weekday"]["values"], [0.0] * 7)
