"""Tests for the tracker's money maths, price locking and access control.

Run with::

    python manage.py test tracker
"""

import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import openpyxl

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tracker.models import DailySale, MasterProduct, Shop, ShopProduct
from tracker.templatetags.money import ksh, signed_ksh, units

CustomUser = get_user_model()

# Password hashing dominates the runtime of these tests otherwise.
FAST_PASSWORD_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]


class MoneyFilterTests(TestCase):
    """Currency and quantity formatting."""

    def test_formats_as_ksh_with_separators(self):
        self.assertEqual(ksh(Decimal("1234.5")), "KSh 1,234.50")
        self.assertEqual(ksh(Decimal("0")), "KSh 0.00")
        self.assertEqual(ksh(None), "KSh 0.00")

    def test_negative_values_keep_the_minus_sign(self):
        self.assertEqual(ksh(Decimal("-250.00")), "KSh -250.00")

    def test_signed_shows_a_plus_for_gains(self):
        self.assertEqual(signed_ksh(Decimal("40.00")), "+KSh 40.00")

    def test_units_drops_trailing_zeroes(self):
        self.assertEqual(units(Decimal("12.00")), "12")
        self.assertEqual(units(Decimal("1234.50")), "1,234.5")
        self.assertEqual(units(None), "0")


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHER)
class BaseDataTestCase(TestCase):
    """Shared fixtures: two shops with different prices for the same product."""

    def setUp(self):
        self.fig_tree = Shop.objects.create(name="Fig Tree", location="Nairobi")
        self.empire = Shop.objects.create(name="Empire Shop", location="Nairobi")

        self.gilbeys = MasterProduct.objects.create(name="Gilbeys", category="250ml")
        self.blue_ice = MasterProduct.objects.create(name="Blue Ice", category="250ml")

        # The same product, priced differently in each shop.
        ShopProduct.objects.create(
            shop=self.fig_tree, product=self.gilbeys,
            cost_price=Decimal("450.00"), selling_price=Decimal("550.00"),
        )
        ShopProduct.objects.create(
            shop=self.empire, product=self.gilbeys,
            cost_price=Decimal("450.00"), selling_price=Decimal("600.00"),
        )
        ShopProduct.objects.create(
            shop=self.fig_tree, product=self.blue_ice,
            cost_price=Decimal("150.00"), selling_price=Decimal("200.00"),
        )
        # Empire does not stock Blue Ice.

        self.owner = CustomUser.objects.create_user(
            username="owner", password="pw", role=CustomUser.Role.OWNER
        )
        self.fig_manager = CustomUser.objects.create_user(
            username="figtree", password="pw",
            role=CustomUser.Role.MANAGER, assigned_shop=self.fig_tree,
        )
        self.empire_manager = CustomUser.objects.create_user(
            username="empire", password="pw",
            role=CustomUser.Role.MANAGER, assigned_shop=self.empire,
        )
        self.orphan = CustomUser.objects.create_user(
            username="orphan", password="pw", role=CustomUser.Role.MANAGER
        )


class DailySaleCalculationTests(BaseDataTestCase):
    """Profit is derived server-side and frozen at the time of sale."""

    def test_profit_uses_the_shops_own_price(self):
        empire_sale = DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("3"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("600.00"),
            recorded_by=self.empire_manager,
        )
        self.assertEqual(empire_sale.total_revenue, Decimal("1800.00"))
        self.assertEqual(empire_sale.total_cost, Decimal("1350.00"))
        self.assertEqual(empire_sale.profit, Decimal("450.00"))

    def test_client_supplied_money_is_overwritten(self):
        """A crafted save() cannot set the totals or profit by hand."""
        sale = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            total_revenue=Decimal("999999.00"), total_cost=Decimal("0.00"),
            profit=Decimal("999999.00"),
            recorded_by=self.fig_manager,
        )
        sale.refresh_from_db()
        self.assertEqual(sale.total_revenue, Decimal("1100.00"))
        self.assertEqual(sale.total_cost, Decimal("900.00"))
        self.assertEqual(sale.profit, Decimal("200.00"))

    def test_prices_are_locked_in_by_clean(self):
        sale = DailySale(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("1"),
            recorded_by=self.empire_manager,
        )
        sale.clean()
        self.assertEqual(sale.unit_price_at_sale, Decimal("600.00"))
        self.assertEqual(sale.unit_cost_at_sale, Decimal("450.00"))

    def test_clean_rejects_a_product_the_shop_does_not_stock(self):
        sale = DailySale(
            shop=self.empire, product=self.blue_ice, units_sold=Decimal("1"),
            recorded_by=self.empire_manager,
        )
        with self.assertRaises(ValidationError):
            sale.clean()

    def test_clean_rejects_an_inactive_product(self):
        ShopProduct.objects.filter(
            shop=self.fig_tree, product=self.blue_ice
        ).update(is_active=False)
        sale = DailySale(
            shop=self.fig_tree, product=self.blue_ice, units_sold=Decimal("1"),
            recorded_by=self.fig_manager,
        )
        with self.assertRaises(ValidationError):
            sale.clean()

    def test_later_price_edits_do_not_change_history(self):
        sale = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        original_profit = sale.profit

        card = ShopProduct.objects.get(shop=self.fig_tree, product=self.gilbeys)
        card.cost_price = Decimal("400.00")
        card.selling_price = Decimal("700.00")
        card.save()

        sale.refresh_from_db()
        self.assertEqual(sale.profit, original_profit)
        self.assertEqual(sale.unit_price_at_sale, Decimal("550.00"))


class RoleAccessTests(BaseDataTestCase):
    """Owners see everything; managers see only their own shop."""

    def test_manager_cannot_reach_the_owner_dashboard(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_owner_cannot_reach_the_manager_dashboard(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("manager_dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_reach_the_pricing_page(self):
        self.client.force_login(self.empire_manager)
        response = self.client.get(reverse("shop_pricing"))
        self.assertEqual(response.status_code, 403)

    def test_the_dashboard_redirect_also_refuses_the_wrong_role(self):
        """A manager hitting /manager/sale/add/ with no shop gets a 403."""
        self.client.force_login(self.orphan)
        response = self.client.get(reverse("daily_sale_create"))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_root_redirects_by_role(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/"), reverse("owner_dashboard"))

        self.client.force_login(self.fig_manager)
        self.assertRedirects(self.client.get("/"), reverse("manager_dashboard"))


class ManagerDashboardIsolationTests(BaseDataTestCase):
    """A manager's figures must only ever come from their own shop."""

    def setUp(self):
        super().setUp()
        DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("10"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("600.00"),
            recorded_by=self.empire_manager,
        )

    def test_fig_tree_manager_sees_only_fig_tree_profit(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("manager_dashboard"))
        self.assertEqual(response.context["shop"], self.fig_tree)
        self.assertEqual(response.context["total_profit"], Decimal("200.00"))
        self.assertEqual(response.context["total_units"], Decimal("2"))

    def test_empire_manager_sees_only_empire_profit(self):
        self.client.force_login(self.empire_manager)
        response = self.client.get(reverse("manager_dashboard"))
        self.assertEqual(response.context["shop"], self.empire)
        self.assertEqual(response.context["total_profit"], Decimal("1500.00"))

    def test_recent_history_excludes_other_shops(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("manager_dashboard"))
        shops = {sale.shop_id for sale in response.context["recent_sales"]}
        self.assertEqual(shops, {self.fig_tree.pk})

    def test_manager_without_a_shop_is_refused(self):
        self.client.force_login(self.orphan)
        response = self.client.get(reverse("daily_sale_create"))
        self.assertEqual(response.status_code, 403)


class DailySaleCreateTests(BaseDataTestCase):
    """Recording a sale through the form."""

    def test_creates_a_sale_for_the_managers_own_shop(self):
        self.client.force_login(self.fig_manager)
        response = self.client.post(
            reverse("daily_sale_create"),
            {"product": self.gilbeys.pk, "units_sold": "4"},
        )
        self.assertRedirects(response, reverse("manager_dashboard") + "?recorded=1")

        sale = DailySale.objects.get(product=self.gilbeys)
        self.assertEqual(sale.shop, self.fig_tree)
        self.assertEqual(sale.recorded_by, self.fig_manager)
        self.assertEqual(sale.units_sold, Decimal("4"))
        self.assertEqual(sale.profit, Decimal("400.00"))

    def test_a_shop_in_the_post_cannot_override_the_managers_shop(self):
        self.client.force_login(self.fig_manager)
        self.client.post(
            reverse("daily_sale_create"),
            {
                "product": self.gilbeys.pk,
                "units_sold": "1",
                "shop": self.empire.pk,          # ignored: the form sets the shop
            },
        )
        self.assertEqual(DailySale.objects.get(product=self.gilbeys).shop, self.fig_tree)

    def test_cannot_sell_a_product_the_shop_does_not_stock(self):
        self.client.force_login(self.empire_manager)
        response = self.client.post(
            reverse("daily_sale_create"),
            {"product": self.blue_ice.pk, "units_sold": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DailySale.objects.filter(product=self.blue_ice).exists())

    def test_zero_quantity_is_rejected(self):
        self.client.force_login(self.fig_manager)
        response = self.client.post(
            reverse("daily_sale_create"),
            {"product": self.gilbeys.pk, "units_sold": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DailySale.objects.filter(product=self.gilbeys).exists())

    def test_product_choices_are_scoped_to_the_shop(self):
        self.client.force_login(self.empire_manager)
        response = self.client.get(reverse("daily_sale_create"))
        offered = {str(pk) for pk in response.context["form"].fields["product"].queryset.values_list("pk", flat=True)}
        self.assertIn(str(self.gilbeys.pk), offered)
        self.assertNotIn(str(self.blue_ice.pk), offered)


class PriceApiTests(BaseDataTestCase):
    """The live-preview JSON endpoint."""

    def url_for(self, product):
        return reverse("shop_product_price", args=[product.pk])

    def test_returns_the_managers_own_shop_price(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(self.url_for(self.gilbeys))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cost_price"], "450.00")
        self.assertEqual(response.json()["selling_price"], "550.00")

    def test_the_same_product_prices_differ_per_shop(self):
        self.client.force_login(self.empire_manager)
        response = self.client.get(self.url_for(self.gilbeys))
        self.assertEqual(response.json()["selling_price"], "600.00")

    def test_product_not_stocked_is_a_404(self):
        self.client.force_login(self.empire_manager)
        response = self.client.get(self.url_for(self.blue_ice))
        self.assertEqual(response.status_code, 404)

    def test_inactive_product_is_a_404(self):
        ShopProduct.objects.filter(shop=self.fig_tree, product=self.gilbeys).update(
            is_active=False
        )
        self.client.force_login(self.fig_manager)
        self.assertEqual(self.client.get(self.url_for(self.gilbeys)).status_code, 404)

    def test_anonymous_access_is_redirected(self):
        response = self.client.get(self.url_for(self.gilbeys))
        self.assertEqual(response.status_code, 302)


class PageRenderTests(BaseDataTestCase):
    """Every page renders, including for a brand new database."""

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")

    def test_logout_requires_post(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)

    def test_owner_dashboard_renders_with_no_sales(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_profit"], Decimal("0.00"))
        self.assertContains(response, "KSh 0.00")

    def test_manager_dashboard_renders(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("manager_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fig Tree")

    def test_sale_form_renders(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("daily_sale_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id_units_sold")

    def test_pricing_page_renders(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_pricing"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gilbeys")

    def test_owner_sees_a_shop_with_no_sales_without_erroring(self):
        """Every shop gets a column in the 7-day table, even an empty one."""
        self.client.force_login(self.owner)
        response = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(len(response.context["trend_rows"]), 7)
        self.assertEqual(len(response.context["shop_rows"]), 2)


class DashboardAnalyticsTests(BaseDataTestCase):
    """The chart payloads that feed the dashboards' Chart.js analytics."""

    def setUp(self):
        super().setUp()
        DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("10"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("600.00"),
            recorded_by=self.empire_manager,
        )

    def test_owner_dashboard_carries_the_chart_payload(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("owner_dashboard"))
        analytics = response.context["analytics"]

        # Per-shop profit comparison, best shop first.
        self.assertEqual(analytics["shop_profits"]["labels"], ["Empire Shop", "Fig Tree"])
        self.assertEqual(analytics["shop_profits"]["values"], [1500.0, 200.0])

        # One trend dataset per shop, one label per chart day.
        self.assertEqual(len(analytics["trend"]["datasets"]), 2)
        self.assertEqual(len(analytics["trend"]["labels"]), 30)
        self.assertEqual(response.context["week_profit"], Decimal("1700.00"))

    def test_top_products_chart_lists_the_best_mover(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("owner_dashboard"))
        top = response.context["analytics"]["top_products"]
        self.assertEqual(top["labels"], ["Gilbeys · 250ml"])
        self.assertEqual(top["values"], [1700.0])

    def test_manager_dashboard_carries_a_14_day_trend(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("manager_dashboard"))
        trend = response.context["trend"]
        self.assertEqual(len(trend["labels"]), 14)
        self.assertEqual(len(trend["values"]), 14)
        self.assertEqual(sum(trend["values"]), 200.0)


class ProductReportTests(BaseDataTestCase):
    """The all-products cross-shop report page and its CSV export."""

    def setUp(self):
        super().setUp()
        DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("10"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("600.00"),
            recorded_by=self.empire_manager,
        )
        # Blue Ice is stocked but has never sold anywhere.

    def test_rows_split_each_product_by_shop(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("product_report"))
        rows = {row["product"].pk: row for row in response.context["rows"]}

        cells = {cell["shop"].pk: cell for cell in rows[self.gilbeys.pk]["shop_cells"]}
        self.assertEqual(cells[self.fig_tree.pk]["profit"], Decimal("200.00"))
        self.assertEqual(cells[self.empire.pk]["profit"], Decimal("1500.00"))
        self.assertEqual(rows[self.gilbeys.pk]["total_profit"], Decimal("1700.00"))

        # Best mover comes first.
        self.assertEqual(response.context["rows"][0]["product"], self.gilbeys)

    def test_products_with_no_sales_are_included_with_zero_cells(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("product_report"))
        row = next(
            row for row in response.context["rows"] if row["product"] == self.blue_ice
        )
        self.assertEqual(row["total_profit"], Decimal("0.00"))
        self.assertTrue(all(cell["profit"] == Decimal("0.00") for cell in row["shop_cells"]))

    def test_shop_totals_footer_matches_the_column_sums(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("product_report"))
        totals = {t["shop"].pk: t["profit"] for t in response.context["shop_totals"]}
        self.assertEqual(totals[self.fig_tree.pk], Decimal("200.00"))
        self.assertEqual(totals[self.empire.pk], Decimal("1500.00"))
        self.assertEqual(response.context["grand_total"], Decimal("1700.00"))

    def test_a_manager_is_refused(self):
        self.client.force_login(self.fig_manager)
        self.assertEqual(self.client.get(reverse("product_report")).status_code, 403)

    def test_csv_export_lists_every_product_and_shop(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("product_report_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

        lines = response.content.decode().splitlines()
        self.assertEqual(
            lines[0], "Product,Category,Empire Shop profit,Fig Tree profit,"
                      "Total Profit,Total Units,Total Revenue"
        )
        gilbeys_row = next(line for line in lines if line.startswith("Gilbeys"))
        self.assertIn("1500.00", gilbeys_row)   # Empire cell
        self.assertIn("200.00", gilbeys_row)    # Fig Tree cell
        self.assertIn("1700.00", gilbeys_row)   # total profit

    def test_csv_export_refuses_a_manager(self):
        self.client.force_login(self.fig_manager)
        self.assertEqual(self.client.get(reverse("product_report_csv")).status_code, 403)


class ShopPricingTests(BaseDataTestCase):
    """The owner's inline price editor."""

    def test_owner_can_update_prices_and_the_profit_column_follows(self):
        self.client.force_login(self.owner)
        card = ShopProduct.objects.get(shop=self.fig_tree, product=self.gilbeys)
        self.client.post(
            reverse("shop_pricing"),
            {
                f"shop_product-{card.pk}-cost_price": "400.00",
                f"shop_product-{card.pk}-selling_price": "700.00",
                f"shop_product-{card.pk}-is_active": "on",
            },
        )
        card.refresh_from_db()
        self.assertEqual(card.cost_price, Decimal("400.00"))
        self.assertEqual(card.selling_price, Decimal("700.00"))
        self.assertEqual(card.unit_profit, Decimal("300.00"))

    def test_prices_are_grouped_by_shop(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_pricing"))
        grouped = response.context["grouped_products"]
        self.assertEqual(set(grouped), {self.fig_tree, self.empire})
        self.assertEqual(len(grouped[self.fig_tree]), 2)

    def test_a_manager_cannot_save_prices(self):
        self.client.force_login(self.fig_manager)
        card = ShopProduct.objects.get(shop=self.fig_tree, product=self.gilbeys)
        response = self.client.post(
            reverse("shop_pricing"),
            {
                f"shop_product-{card.pk}-cost_price": "1.00",
                f"shop_product-{card.pk}-selling_price": "2.00",
            },
        )
        self.assertEqual(response.status_code, 403)
        card.refresh_from_db()
        self.assertEqual(card.cost_price, Decimal("450.00"))


class DailySalesSheetTests(BaseDataTestCase):
    """The one-submit daily stock sheet.

    Opening stock comes from the previous day's closing stock; the manager
    enters additional/total/closing stock and units sold, amount and profit
    are derived server side. Re-submitting the day updates it.
    """

    SHEET = "daily_sales_sheet"

    @staticmethod
    def stock(product, additional="", total="", closing=""):
        """Build the POST fields for one product's stock row."""
        return {
            f"add_{product.pk}": additional,
            f"total_{product.pk}": total,
            f"close_{product.pk}": closing,
        }

    def post_stock(self, *rows):
        """POST stock rows for ``(product, figures)`` pairs."""
        payload = {}
        for product, figures in rows:
            payload.update(self.stock(product, **figures))
        return self.client.post(reverse(self.SHEET), payload)

    def test_sheet_renders_every_active_product(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse(self.SHEET))
        self.assertEqual(response.status_code, 200)
        names = {row["product"].name for row in response.context["rows"]}
        self.assertIn("Gilbeys", names)
        self.assertIn("Blue Ice", names)
        self.assertNotIn("money in the form", response.content.decode())
        # The stock boxes are present for each product.
        self.assertContains(response, f'name="total_{self.gilbeys.pk}"')
        self.assertContains(response, f'name="close_{self.gilbeys.pk}"')

    def test_a_whole_day_saves_in_one_submit(self):
        self.client.force_login(self.fig_manager)
        response = self.post_stock(
            (self.gilbeys, {"additional": "10", "total": "10", "closing": "6"}),
            (self.blue_ice, {"additional": "10", "total": "10", "closing": "0"}),
        )
        self.assertRedirects(response, reverse(self.SHEET))

        gilbeys = DailySale.objects.get(product=self.gilbeys)
        self.assertEqual(gilbeys.shop, self.fig_tree)
        self.assertEqual(gilbeys.recorded_by, self.fig_manager)
        self.assertTrue(gilbeys.is_stock_entry)
        self.assertEqual(gilbeys.additional_stock, Decimal("10"))
        # Sold = total stock - closing stock = 10 - 6 = 4.
        self.assertEqual(gilbeys.units_sold, Decimal("4.00"))
        # Amount = 4 x 550 selling price, profit = 4 x (550 - 450).
        self.assertEqual(gilbeys.total_revenue, Decimal("2200.00"))
        self.assertEqual(gilbeys.profit, Decimal("400.00"))

        blue_ice = DailySale.objects.get(product=self.blue_ice)
        # 10 x (200 - 150) = 500
        self.assertEqual(blue_ice.units_sold, Decimal("10.00"))
        self.assertEqual(blue_ice.profit, Decimal("500.00"))

    def test_opening_stock_comes_from_yesterdays_closing(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        previous = DailySale.objects.create(
            shop=self.fig_tree,
            product=self.gilbeys,
            units_sold=Decimal("0"),
            opening_stock=Decimal("0.00"),
            additional_stock=Decimal("20.00"),
            total_stock=Decimal("20.00"),
            closing_stock=Decimal("9.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.filter(pk=previous.pk).update(sale_date=yesterday)

        # The sheet shows yesterday's closing as today's opening.
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse(self.SHEET))
        row = next(r for r in response.context["rows"] if r["product"] == self.gilbeys)
        self.assertEqual(row["opening"], Decimal("9.00"))
        self.assertEqual(response.context["openings"][self.gilbeys.pk], Decimal("9.00"))

        # And it is stored on the new entry: 9 + 1 added = 10 total.
        response = self.post_stock(
            (self.gilbeys, {"additional": "1", "total": "10", "closing": "3"})
        )
        self.assertRedirects(response, reverse(self.SHEET))
        today = DailySale.objects.get(
            product=self.gilbeys, sale_date=timezone.localdate()
        )
        self.assertEqual(today.opening_stock, Decimal("9.00"))
        self.assertEqual(today.units_sold, Decimal("7.00"))

    def test_second_submit_replaces_todays_row(self):
        self.client.force_login(self.fig_manager)
        self.post_stock(
            (self.gilbeys, {"additional": "10", "total": "10", "closing": "6"})
        )
        self.post_stock(
            (self.gilbeys, {"additional": "10", "total": "10", "closing": "8"})
        )
        # One row per product per day: the correction replaces, never doubles.
        rows = DailySale.objects.filter(product=self.gilbeys, shop=self.fig_tree)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.closing_stock, Decimal("8"))
        self.assertEqual(row.units_sold, Decimal("2.00"))

    def test_blank_row_means_no_activity(self):
        self.client.force_login(self.fig_manager)
        response = self.post_stock(
            (self.gilbeys, {"additional": "10", "total": "10", "closing": "6"}),
            (self.blue_ice, {}),
        )
        self.assertRedirects(response, reverse(self.SHEET))
        self.assertEqual(DailySale.objects.count(), 1)
        self.assertEqual(
            DailySale.objects.get().product_id, self.gilbeys.pk
        )

    def test_a_product_the_shop_does_not_stock_is_rejected(self):
        self.client.force_login(self.fig_manager)
        # A tampered product id the shop does not stock is refused wholesale.
        response = self.client.post(
            reverse(self.SHEET),
            {"add_999999": "1", "total_999999": "5", "close_999999": "0"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DailySale.objects.count(), 0)

        # Empire does not stock Blue Ice.
        self.client.force_login(self.empire_manager)
        response = self.post_stock(
            (self.blue_ice, {"additional": "10", "total": "10", "closing": "0"}),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            DailySale.objects.filter(product=self.blue_ice, shop=self.empire).exists()
        )

    def test_partial_rows_are_rejected(self):
        self.client.force_login(self.fig_manager)
        # Total without closing: neither half can be trusted alone.
        response = self.client.post(
            reverse(self.SHEET), {f"total_{self.gilbeys.pk}": "10"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DailySale.objects.count(), 0)

    def test_closing_greater_than_total_is_rejected(self):
        self.client.force_login(self.fig_manager)
        response = self.post_stock(
            (self.gilbeys, {"total": "5", "closing": "10"}),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DailySale.objects.count(), 0)

    def test_bad_numbers_are_rejected(self):
        self.client.force_login(self.fig_manager)
        for bad in ("abc", "-5"):
            response = self.post_stock(
                (self.gilbeys, {"additional": bad, "total": "10", "closing": "4"}),
            )
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(DailySale.objects.count(), 0)

    def test_mismatched_total_saves_with_a_warning(self):
        """opening 0 + additional 2 != total 10: warn, but still save."""
        self.client.force_login(self.fig_manager)
        response = self.post_stock(
            (self.gilbeys, {"additional": "2", "total": "10", "closing": "4"}),
        )
        self.assertEqual(response.status_code, 302)
        row = DailySale.objects.get(product=self.gilbeys)
        # The sale figure still comes from the count: 10 - 4 = 6.
        self.assertEqual(row.units_sold, Decimal("6.00"))
        self.assertEqual(row.profit, Decimal("600.00"))

        page = self.client.get(reverse(self.SHEET))
        warnings = [str(m) for m in page.context["messages"]]
        self.assertTrue(any("does not match" in w for w in warnings), warnings)

    def test_legacy_quantity_fields_are_ignored(self):
        """The old units-only payload no longer writes rows."""
        self.client.force_login(self.fig_manager)
        response = self.client.post(reverse(self.SHEET), {f"qty_{self.gilbeys.pk}": "4"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DailySale.objects.count(), 0)

    def test_no_filled_rows_does_not_save(self):
        self.client.force_login(self.fig_manager)
        response = self.post_stock((self.gilbeys, {}))
        self.assertRedirects(response, reverse(self.SHEET))
        self.assertEqual(DailySale.objects.count(), 0)

    def test_day_totals_show_after_a_save(self):
        self.client.force_login(self.fig_manager)
        self.post_stock(
            (self.gilbeys, {"additional": "10", "total": "10", "closing": "6"})
        )
        response = self.client.get(reverse(self.SHEET))
        # 4 x (550 - 450) = 400 profit, 4 units, 1 recorded product.
        self.assertEqual(response.context["day_profit"], Decimal("400.00"))
        self.assertEqual(response.context["day_units"], Decimal("4"))
        self.assertEqual(response.context["recorded_count"], 1)

    def test_profit_is_not_shown_on_the_sheet(self):
        """Profit is a dashboard figure -- the sheet never renders it."""
        self.client.force_login(self.fig_manager)
        self.post_stock(
            (self.gilbeys, {"additional": "10", "total": "10", "closing": "6"})
        )

        page = self.client.get(reverse(self.SHEET))
        flash = " ".join(str(m) for m in page.context["messages"])
        self.assertNotIn("profit", flash.lower())

        html = page.content.decode()
        self.assertNotContains(page, "row-profit")          # per-product column
        self.assertNotContains(page, "Profit so far today")  # header card
        self.assertNotContains(page, "new-profit")           # footer running total
        self.assertNotIn("data-cost", html)                  # cost stays on the server

        # It is still computed and stored, and the dashboard still shows it.
        self.assertEqual(DailySale.objects.get().profit, Decimal("400.00"))
        dash = self.client.get(reverse("manager_dashboard"))
        self.assertContains(dash, "Today's Profit")

    def test_products_without_a_cost_are_left_off_the_sheet(self):
        """No cost, no profit -- so the product is not offered at all."""
        ShopProduct.objects.filter(shop=self.fig_tree, product=self.blue_ice).update(
            cost_price=Decimal("0.00")
        )
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse(self.SHEET))
        self.assertEqual(response.status_code, 200)
        names = {r["product"].name for r in response.context["rows"]}
        self.assertIn("Gilbeys", names)
        self.assertNotIn("Blue Ice", names)
        # Not rendered anywhere on the page either.
        self.assertNotIn("Blue Ice", response.content.decode())
        self.assertContains(response, "until the owner prices them")

        # A crafted POST cannot force an unpriced product in either.
        response = self.post_stock(
            (self.blue_ice, {"additional": "10", "total": "10", "closing": "0"}),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DailySale.objects.filter(product=self.blue_ice).exists())

    def test_rows_follow_the_price_list_row_order(self):
        """250ml first ... exactly as the owner's spreadsheet reads."""
        with tempfile.TemporaryDirectory() as tmp:
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "all"
            sheet.append(["Product", "Cost Price", "Selling Price", "Category"])
            # Gilbeys deliberately listed first: alphabetically it would be last.
            sheet.append(["Gilbeys", 416, 550, "250ml"])
            sheet.append(["Blue Ice", 95, 200, "250ml"])
            workbook.save(os.path.join(tmp, "price list.xlsx"))

            with override_settings(BASE_DIR=tmp):
                self.client.force_login(self.fig_manager)
                response = self.client.get(reverse(self.SHEET))

        self.assertEqual(response.status_code, 200)
        names = [r["product"].name for r in response.context["rows"]]
        self.assertEqual(names, ["Gilbeys", "Blue Ice"])

    def test_products_missing_from_the_price_list_sort_last(self):
        """A product the file does not know still appears, after the file's rows."""
        extra = MasterProduct.objects.create(name="Zeta", category="350ml")
        ShopProduct.objects.create(
            shop=self.fig_tree,
            product=extra,
            cost_price=Decimal("100.00"),
            selling_price=Decimal("150.00"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "all"
            sheet.append(["Product", "Cost Price", "Selling Price", "Category"])
            sheet.append(["Gilbeys", 416, 550, "250ml"])
            sheet.append(["Blue Ice", 95, 200, "250ml"])
            workbook.save(os.path.join(tmp, "price list.xlsx"))

            with override_settings(BASE_DIR=tmp):
                self.client.force_login(self.fig_manager)
                response = self.client.get(reverse(self.SHEET))

        names = [r["product"].name for r in response.context["rows"]]
        self.assertEqual(names, ["Gilbeys", "Blue Ice", "Zeta"])

    def test_owner_is_refused_when_they_have_no_shop(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse(self.SHEET)).status_code, 403)


class ShopPriceSheetCommandTests(BaseDataTestCase):
    """Per-shop price workbooks: make copies, edit them, import them back."""

    @staticmethod
    def path_in(directory, shop_name):
        return os.path.join(directory, f"price list - {shop_name}.xlsx")

    @staticmethod
    def set_price(path, product_name, cost, selling):
        """Edit a generated workbook the way the owner would by hand."""
        workbook = openpyxl.load_workbook(path)
        sheet = workbook.active
        for row in sheet.iter_rows(min_row=2):
            if str(row[0].value or "").strip() == product_name:
                row[1].value = float(cost)
                row[2].value = float(selling)
        workbook.save(path)

    def test_make_writes_one_copy_per_shop_with_own_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            call_command("make_shop_price_sheets", "--dir", tmp)
            fig = self.path_in(tmp, "Fig Tree")
            empire = self.path_in(tmp, "Empire Shop")
            self.assertTrue(os.path.exists(fig))
            self.assertTrue(os.path.exists(empire))

            # Each copy carries that shop's selling price for the same product.
            sheet = openpyxl.load_workbook(fig).active
            prices = {
                str(r[0]).strip(): (r[1], r[2])
                for r in ((row[0].value, row[1].value, row[2].value)
                          for row in sheet.iter_rows(min_row=2))
            }
            self.assertEqual(prices["Gilbeys"][1], 550.0)   # Fig Tree sells at 550
            sheet = openpyxl.load_workbook(empire).active
            prices = {
                str(r[0]).strip(): (r[1], r[2])
                for r in ((row[0].value, row[1].value, row[2].value)
                          for row in sheet.iter_rows(min_row=2))
            }
            self.assertEqual(prices["Gilbeys"][1], 600.0)   # Empire sells at 600

    def test_existing_copies_are_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            call_command("make_shop_price_sheets", "--shop", "Fig Tree", "--dir", tmp)
            path = self.path_in(tmp, "Fig Tree")
            self.set_price(path, "Gilbeys", "111", "222")

            call_command("make_shop_price_sheets", "--shop", "Fig Tree", "--dir", tmp)
            sheet = openpyxl.load_workbook(path).active
            edited = [
                (row[1].value, row[2].value)
                for row in sheet.iter_rows(min_row=2)
                if str(row[0].value or "").strip() == "Gilbeys"
            ]
            self.assertEqual(edited, [(111.0, 222.0)])  # the hand edits survived

    def test_import_updates_only_the_matching_shop(self):
        with tempfile.TemporaryDirectory() as tmp:
            call_command("make_shop_price_sheets", "--dir", tmp)
            self.set_price(self.path_in(tmp, "Fig Tree"), "Gilbeys", "500", "700")
            call_command("import_shop_price_sheets", "--dir", tmp)

        fig = ShopProduct.objects.get(shop=self.fig_tree, product=self.gilbeys)
        self.assertEqual(fig.cost_price, Decimal("500.00"))
        self.assertEqual(fig.selling_price, Decimal("700.00"))

        empire = ShopProduct.objects.get(shop=self.empire, product=self.gilbeys)
        self.assertEqual(empire.cost_price, Decimal("450.00"))
        self.assertEqual(empire.selling_price, Decimal("600.00"))

    def test_unknown_products_are_reported_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            call_command("make_shop_price_sheets", "--shop", "Fig Tree", "--dir", tmp)
            path = self.path_in(tmp, "Fig Tree")
            workbook = openpyxl.load_workbook(path)
            workbook.active.append(["Mystery Item", 100, 150, "250ml"])
            workbook.save(path)

            out = StringIO()
            call_command("import_shop_price_sheets", path, stdout=out)

        self.assertIn("unknown product", out.getvalue())
        self.assertFalse(MasterProduct.objects.filter(name="Mystery Item").exists())
