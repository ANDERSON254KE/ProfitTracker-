"""Tests for the tracker's money maths, price locking and access control.

Run with::

    python manage.py test tracker
"""

import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO

import openpyxl

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tracker.models import (
    DailySale,
    MasterProduct,
    Shop,
    ShopDailyCash,
    ShopDailyProfit,
    ShopExpense,
    ShopProduct,
    StockAddition,
)
from tracker.templatetags.money import ksh, signed_ksh, units
from tracker.views import _stock_batch_profit_rows

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

    def test_pricing_page_uses_responsive_table_wrappers(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_pricing"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overflow-x-auto")
        self.assertContains(response, "max-w-full")
        self.assertContains(response, "min-h-0 min-w-0 flex-1 overflow-y-auto")

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


class ShopDetailViewTests(BaseDataTestCase):
    """The per-shop report page behind the owner dashboard's shop cards."""

    def setUp(self):
        super().setUp()
        # A dated sale three days ago and one today, both at Fig Tree.
        old = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("1"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.filter(pk=old.pk).update(
            sale_date=timezone.localdate() - timedelta(days=3)
        )
        self.today_sale = DailySale.objects.create(
            shop=self.fig_tree, product=self.blue_ice, units_sold=Decimal("4"),
            unit_cost_at_sale=Decimal("150.00"), unit_price_at_sale=Decimal("200.00"),
            recorded_by=self.fig_manager,
        )
        # Empire sells a lot of Gilbeys.
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("10"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("600.00"),
            recorded_by=self.empire_manager,
        )

    def test_owner_dashboard_cards_carry_today_and_week_profit(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("owner_dashboard"))
        cards = {row["shop"].pk: row for row in response.context["shop_rows"]}
        fig = cards[self.fig_tree.pk]
        self.assertEqual(fig["today_profit"], Decimal("200.00"))   # 4 x Blue Ice
        self.assertEqual(fig["week_profit"], Decimal("300.00"))    # 200 today + 100 three days ago
        self.assertEqual(cards[self.empire.pk]["today_profit"], Decimal("1500.00"))

    def test_cards_link_to_the_shop_report(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("owner_dashboard"))
        self.assertContains(response, reverse("shop_detail", args=[self.fig_tree.pk]))

    def test_shop_report_shows_kpis_and_dated_rows(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        self.assertEqual(response.context["shop"], self.fig_tree)
        self.assertEqual(response.context["today_profit"], Decimal("200.00"))
        self.assertEqual(response.context["week_profit"], Decimal("300.00"))
        self.assertEqual(response.context["total_profit"], Decimal("300.00"))
        # One row per trading day, newest first.
        dates = [row["sale_date"] for row in response.context["day_rows"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(len(dates), 2)

    def test_each_day_names_the_staff_and_subtracts_expenses(self):
        """The daily rows carry who recorded sales that day and the net profit."""
        ShopExpense.objects.create(
            shop=self.fig_tree, expense_date=timezone.localdate(),
            description="Water", amount=Decimal("50.00"), recorded_by=self.fig_manager,
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        today_row = response.context["day_rows"][0]
        self.assertEqual(today_row["sale_date"], timezone.localdate())
        self.assertEqual(today_row["staff"], "figtree")
        self.assertEqual(today_row["expenses"], Decimal("50.00"))
        self.assertEqual(today_row["net"], Decimal("150.00"))   # 200 profit - 50
        self.assertContains(response, "On Duty")

    def test_today_cash_sheet_saves_and_derives_cash_and_balance(self):
        """M-Pesa is typed; cash and the carried balance are worked out."""
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("shop_detail", args=[self.fig_tree.pk]),
            {"mpesa": "120", "bill_out": "30", "bill_paid": "10"},
        )
        self.assertEqual(response.status_code, 302)
        record = ShopDailyCash.objects.get(
            shop=self.fig_tree, cash_date=timezone.localdate()
        )
        self.assertEqual(record.mpesa, Decimal("120.00"))

        page = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        summary = page.context["cash_summary"]
        self.assertEqual(summary["total_sales"], Decimal("800.00"))  # 4 x Blue Ice
        self.assertEqual(summary["cash"], Decimal("680.00"))         # 800 - 120
        self.assertEqual(summary["balance"], Decimal("660.00"))      # 680 + 10 - 30

        # Posting again replaces the day instead of adding a second record.
        self.client.post(
            reverse("shop_detail", args=[self.fig_tree.pk]),
            {"mpesa": "100", "bill_out": "0", "bill_paid": "0"},
        )
        self.assertEqual(
            ShopDailyCash.objects.filter(shop=self.fig_tree).count(), 1
        )
        page = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        self.assertEqual(page.context["cash_summary"]["cash"], Decimal("700.00"))

    def test_today_cash_sheet_rejects_a_negative_figure(self):
        self.client.force_login(self.owner)
        self.client.post(
            reverse("shop_detail", args=[self.fig_tree.pk]),
            {"mpesa": "-5", "bill_out": "0", "bill_paid": "0"},
        )
        self.assertFalse(ShopDailyCash.objects.exists())

    def test_daily_sheet_shows_the_selected_date_only(self):
        """?date= picks the day whose product lines and totals are shown."""
        old_day = timezone.localdate() - timedelta(days=3)
        self.client.force_login(self.owner)
        page = self.client.get(
            reverse("shop_detail", args=[self.fig_tree.pk]),
            {"date": old_day.isoformat()},
        )
        self.assertEqual(page.context["selected_date"], old_day)
        self.assertFalse(page.context["is_today"])
        lines = page.context["day_sales"]
        self.assertEqual([line.product_id for line in lines], [self.gilbeys.pk])
        self.assertEqual(page.context["day_totals"]["revenue"], Decimal("550.00"))

        today_page = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        self.assertEqual(
            [line.product_id for line in today_page.context["day_sales"]],
            [self.blue_ice.pk],
        )
        self.assertEqual(today_page.context["day_totals"]["revenue"], Decimal("800.00"))

    def test_most_sold_products_are_ranked_by_units(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        top = response.context["top_products"]
        self.assertEqual(top[0]["product_id"], self.blue_ice.pk)   # 4 units
        self.assertEqual(top[1]["product_id"], self.gilbeys.pk)   # 1 unit

    def test_in_shop_comparison_limits_and_scopes_products(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("shop_detail", args=[self.fig_tree.pk]),
            {"compare": [self.gilbeys.pk, self.blue_ice.pk]},
        )
        compared = [p["product_id"] for p in response.context["comparison_data"]]
        self.assertEqual(compared, [self.gilbeys.pk, self.blue_ice.pk])
        # Blue Ice (second dataset) sold today; Gilbeys sold three days ago.
        self.assertEqual(response.context["comparison_chart"]["datasets"][1]["data"][-1], 200.0)

    def test_comparison_only_offers_the_shop_own_products(self):
        """A product the shop does not stock is quietly dropped."""
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("shop_detail", args=[self.empire.pk]),
            {"compare": [self.gilbeys.pk, self.blue_ice.pk]},  # Blue Ice not stocked
        )
        compared = [p["product_id"] for p in response.context["comparison_data"]]
        self.assertEqual(compared, [self.gilbeys.pk])

    def test_dated_table_is_paginated(self):
        for day in range(2, 45):
            sale = DailySale.objects.create(
                shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("1"),
                unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
                recorded_by=self.fig_manager,
            )
            DailySale.objects.filter(pk=sale.pk).update(
                sale_date=timezone.localdate() - timedelta(days=day)
            )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        self.assertTrue(response.context["is_paginated"])
        self.assertLessEqual(len(response.context["day_rows"]), 30)

    def test_a_manager_is_refused(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("shop_detail", args=[self.empire.pk]))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_redirected(self):
        response = self.client.get(reverse("shop_detail", args=[self.fig_tree.pk]))
        self.assertEqual(response.status_code, 302)

    def test_csv_export_lists_days_and_products(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_report_csv", args=[self.fig_tree.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

        content = response.content.decode()
        self.assertIn("Fig Tree — profit report", content)
        self.assertIn("Most sold", content)
        self.assertIn("Daily totals", content)
        self.assertIn("Shop totals", content)
        self.assertIn("200.00", content)   # today's Blue Ice profit


class ShopPricingFilterTests(BaseDataTestCase):
    """The per-shop view of the price editor."""

    def test_without_a_filter_every_shop_is_shown(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_pricing"))
        self.assertEqual(
            set(response.context["grouped_products"]), {self.fig_tree, self.empire}
        )
        self.assertIsNone(response.context["current_shop"])

    def test_with_a_shop_only_that_card_is_shown(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("shop_pricing"), {"shop": self.fig_tree.pk})
        self.assertEqual(set(response.context["grouped_products"]), {self.fig_tree})
        self.assertEqual(response.context["current_shop"], self.fig_tree)

    def test_a_manager_is_refused(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("shop_pricing"), {"shop": self.fig_tree.pk})
        self.assertEqual(response.status_code, 403)


class ProductComparisonFeatureTests(BaseDataTestCase):
    """Search-driven product A vs product B comparisons."""

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
        DailySale.objects.create(
            shop=self.fig_tree, product=self.blue_ice, units_sold=Decimal("4"),
            unit_cost_at_sale=Decimal("150.00"), unit_price_at_sale=Decimal("200.00"),
            recorded_by=self.fig_manager,
        )

    def test_product_a_and_b_search_fields_compare_two_products(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_comparison"),
            {"product_a": self.gilbeys.pk, "product_b": self.blue_ice.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["product_id"] for item in response.context["comparison_data"]],
            [self.gilbeys.pk, self.blue_ice.pk],
        )
        self.assertEqual(response.context["scope_name"], "All shops")

    def test_single_shop_scope_filters_product_pair_comparison(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_comparison"),
            {"product_a": self.gilbeys.pk, "product_b": self.blue_ice.pk, "shop": self.fig_tree.pk},
        )
        self.assertEqual(response.context["selected_shop"], self.fig_tree)
        self.assertEqual(response.context["scope_name"], self.fig_tree.name)
        self.assertEqual(
            [cell["shop"].pk for cell in response.context["comparison_data"][0]["shop_cells"]],
            [self.fig_tree.pk],
        )

    def test_category_label_from_product_suggestions_resolves_in_single_shop_scope(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_comparison"),
            {
                "product_a": "Gilbeys — 250ml",
                "product_b": "Blue Ice — 250ml",
                "shop": self.empire.pk,
            },
        )
        self.assertEqual(
            [item["product_id"] for item in response.context["comparison_data"]],
            [self.gilbeys.pk, self.blue_ice.pk],
        )
        self.assertEqual(response.context["selected_shop"], self.empire)
        self.assertEqual(response.context["comparison_data"][0]["total_profit"], Decimal("1500.00"))

    def test_selected_date_range_filters_comparison_totals_and_trend(self):
        today = timezone.localdate()
        old_sale = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, units_sold=Decimal("1"),
            unit_cost_at_sale=Decimal("450.00"), unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.filter(pk=old_sale.pk).update(sale_date=today - timedelta(days=10))

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_comparison"),
            {
                "product_a": self.gilbeys.pk,
                "product_b": self.blue_ice.pk,
                "start_date": (today - timedelta(days=1)).isoformat(),
                "end_date": today.isoformat(),
            },
        )

        gilbeys = response.context["comparison_data"][0]
        self.assertEqual(gilbeys["total_profit"], Decimal("1700.00"))
        self.assertEqual(response.context["chart_data"]["labels"], [
            (today - timedelta(days=1)).strftime("%d %b %Y"),
            today.strftime("%d %b %Y"),
        ])
        self.assertEqual(response.context["chart_data"]["datasets"][0]["data"], [0.0, 1700.0])

    def test_comparison_csv_uses_the_selected_shop_and_dates(self):
        today = timezone.localdate()
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_comparison_csv"),
            {
                "product_a": self.gilbeys.pk,
                "shop": self.fig_tree.pk,
                "start_date": today.isoformat(),
                "end_date": today.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode()
        self.assertIn("Product,Category,Shop,From,To,Units,Revenue,Profit", csv_text)
        self.assertIn(f"Gilbeys,250ml,Fig Tree,{today.isoformat()},{today.isoformat()},2.00,1100.00,200.00", csv_text)
        self.assertNotIn("Empire Shop", csv_text)


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

    def test_reports_page_lists_shops_in_daily_profit_sheet_selector(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("product_report"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="open_sheet_shop"')
        self.assertContains(response, reverse("all_shops_profit_workbook"))
        for shop in (self.fig_tree, self.empire):
            self.assertContains(response, f'value="{shop.pk}">{shop.name}</option>')

    def test_reports_page_opens_the_selected_shop_profit_sheet(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_report"), {"open_sheet_shop": self.empire.pk}
        )
        self.assertRedirects(
            response,
            reverse("shop_profit_sheet", args=[self.empire.pk]),
            fetch_redirect_response=False,
        )

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

    def test_selected_period_filters_per_product_profit_table(self):
        today = timezone.localdate()
        earlier = today - timedelta(days=4)
        previous_sale = DailySale.objects.create(
            shop=self.fig_tree,
            product=self.gilbeys,
            units_sold=Decimal("1"),
            unit_cost_at_sale=Decimal("450.00"),
            unit_price_at_sale=Decimal("550.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.filter(pk=previous_sale.pk).update(sale_date=earlier)

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_report"),
            {"start_date": today.isoformat(), "end_date": today.isoformat()},
        )
        row = next(item for item in response.context["rows"] if item["product"] == self.gilbeys)
        cells = {cell["shop"].pk: cell for cell in row["shop_cells"]}
        self.assertEqual(cells[self.fig_tree.pk]["profit"], Decimal("200.00"))
        self.assertEqual(cells[self.empire.pk]["profit"], Decimal("1500.00"))
        self.assertEqual(row["total_profit"], Decimal("1700.00"))
        self.assertContains(response, f"{today.isoformat()} to {today.isoformat()}")

        csv_response = self.client.get(
            reverse("product_report_csv"),
            {"start_date": today.isoformat(), "end_date": today.isoformat()},
        )
        csv_rows = csv_response.content.decode().splitlines()
        self.assertEqual(csv_rows[0], f"From,{today.isoformat()},To,{today.isoformat()}")
        self.assertIn("1700.00", next(line for line in csv_rows if line.startswith("Gilbeys")))

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
            lines[1], "Product,Category,Empire Shop Units,Empire Shop Profit,"
                      "Fig Tree Units,Fig Tree Profit,"
                      "Total Units,Total Profit,Total Revenue"
        )
        gilbeys_row = next(line for line in lines if line.startswith("Gilbeys"))
        self.assertIn("1500.00", gilbeys_row)   # Empire cell
        self.assertIn("200.00", gilbeys_row)    # Fig Tree cell
        self.assertIn("1700.00", gilbeys_row)   # total profit

    def test_csv_export_refuses_a_manager(self):
        self.client.force_login(self.fig_manager)
        self.assertEqual(self.client.get(reverse("product_report_csv")).status_code, 403)

    def test_all_shops_profit_workbook_includes_sales_and_expense_net_profit(self):
        expense_day = timezone.localdate() - timedelta(days=1)
        ShopExpense.objects.create(
            shop=self.fig_tree,
            expense_date=expense_day,
            description="Transport",
            amount=Decimal("50.00"),
            recorded_by=self.fig_manager,
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("all_shops_profit_workbook"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        workbook = openpyxl.load_workbook(BytesIO(response.content), data_only=True)
        self.assertEqual(
            workbook.sheetnames,
            ["Shop Summary", "Product Sales", "Expenses", "Daily Net Profit"],
        )
        summary = list(workbook["Shop Summary"].values)
        fig_tree_row = next(row for row in summary if row[0] == "Fig Tree")
        self.assertEqual(fig_tree_row[4], Decimal("200.00"))
        self.assertEqual(fig_tree_row[5], Decimal("50.00"))
        self.assertEqual(fig_tree_row[6], Decimal("150.00"))
        expense_row = next(row for row in list(workbook["Expenses"].values)[1:] if row[2] == "Transport")
        self.assertEqual(expense_row[0].date(), expense_day)
        self.assertEqual(expense_row[1], "Fig Tree")
        self.assertEqual(expense_row[3], 50)
        self.assertEqual(expense_row[4], "figtree")

        daily_row = next(
            row for row in list(workbook["Daily Net Profit"].values)[2:]
            if row[0].date() == expense_day and row[1] == "Fig Tree"
        )
        self.assertEqual(daily_row[6], 50)
        self.assertEqual(daily_row[7], -50)

    def test_all_shops_profit_workbook_refuses_a_manager(self):
        self.client.force_login(self.fig_manager)
        self.assertEqual(
            self.client.get(reverse("all_shops_profit_workbook")).status_code, 403
        )

    def test_selected_date_range_is_visible_in_preview_and_filters_workbook(self):
        today = timezone.localdate()
        older_day = today - timedelta(days=3)
        old_sale = DailySale.objects.create(
            shop=self.fig_tree, product=self.blue_ice, units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("150.00"), unit_price_at_sale=Decimal("200.00"),
            recorded_by=self.fig_manager,
        )
        DailySale.objects.filter(pk=old_sale.pk).update(sale_date=older_day)
        self.client.force_login(self.owner)
        params = {"start_date": today.isoformat(), "end_date": today.isoformat()}

        preview = self.client.get(reverse("product_report"), params)
        self.assertEqual(preview.context["profit_start_date"], today.isoformat())
        self.assertEqual(preview.context["profit_end_date"], today.isoformat())
        self.assertEqual(preview.context["product_sales_count"], 2)
        self.assertContains(preview, f"{today.isoformat()} to {today.isoformat()}")
        self.assertContains(preview, "Workbook preview")

        response = self.client.get(reverse("all_shops_profit_workbook"), params)
        self.assertIn(today.strftime("%Y%m%d"), response["Content-Disposition"])
        workbook = openpyxl.load_workbook(BytesIO(response.content), data_only=True)
        sales_rows = list(workbook["Product Sales"].values)[2:]
        self.assertEqual(len(sales_rows), 2)
        self.assertTrue(all(row[0].date() == today for row in sales_rows))


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


class ShopProfitSheetTests(BaseDataTestCase):
    """Manual daily quantities and expense deductions stay shop-scoped."""

    def test_manager_records_units_and_expense_for_own_shop_and_date(self):
        self.client.force_login(self.fig_manager)
        day = timezone.localdate() - timedelta(days=2)
        url = reverse("shop_profit_sheet", args=[self.fig_tree.pk])
        response = self.client.post(
            url,
            {"date": day.isoformat(), "action": "save_sales", f"units_{self.gilbeys.pk}": "4"},
        )
        self.assertRedirects(response, f"{url}?date={day.isoformat()}")

        sale = DailySale.objects.get(
            shop=self.fig_tree, product=self.gilbeys, sale_date=day,
            is_profit_sheet_entry=True,
        )
        self.assertEqual(sale.units_sold, Decimal("4"))
        self.assertEqual(sale.total_revenue, Decimal("2200.00"))
        self.assertEqual(sale.profit, Decimal("400.00"))

        self.client.post(
            url,
            {
                "date": day.isoformat(), "action": "add_expense",
                "description": "Transport", "amount": "75.00",
            },
        )
        response = self.client.get(url, {"date": day.isoformat()})
        self.assertEqual(response.context["gross_profit"], Decimal("400.00"))
        self.assertEqual(response.context["expense_total"], Decimal("75.00"))
        self.assertEqual(response.context["net_profit"], Decimal("325.00"))

    def test_manual_quantities_update_instead_of_double_counting(self):
        self.client.force_login(self.fig_manager)
        day = timezone.localdate()
        url = reverse("shop_profit_sheet", args=[self.fig_tree.pk])
        for quantity in ("4", "2"):
            self.client.post(
                url,
                {"date": day.isoformat(), "action": "save_sales", f"units_{self.gilbeys.pk}": quantity},
            )
        self.assertEqual(
            DailySale.objects.filter(shop=self.fig_tree, product=self.gilbeys, sale_date=day,
                                     is_profit_sheet_entry=True).count(),
            1,
        )
        self.assertEqual(
            DailySale.objects.get(shop=self.fig_tree, product=self.gilbeys, sale_date=day,
                                  is_profit_sheet_entry=True).profit,
            Decimal("200.00"),
        )

    def test_manager_cannot_write_to_another_shop(self):
        self.client.force_login(self.fig_manager)
        day = timezone.localdate()
        other_shop_url = reverse("shop_profit_sheet", args=[self.empire.pk])
        self.client.post(
            other_shop_url,
            {"date": day.isoformat(), "action": "save_sales", f"units_{self.gilbeys.pk}": "3"},
        )
        self.assertTrue(
            DailySale.objects.filter(shop=self.fig_tree, product=self.gilbeys,
                                     is_profit_sheet_entry=True).exists()
        )
        self.assertFalse(
            DailySale.objects.filter(shop=self.empire, product=self.gilbeys,
                                     is_profit_sheet_entry=True).exists()
        )

    def test_owner_can_open_and_record_for_the_selected_shop(self):
        self.client.force_login(self.owner)
        day = timezone.localdate()
        url = reverse("shop_profit_sheet", args=[self.empire.pk])
        response = self.client.get(url, {"date": day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["shop"], self.empire)
        self.client.post(
            url,
            {"date": day.isoformat(), "action": "save_sales", f"units_{self.gilbeys.pk}": "2"},
        )
        self.assertTrue(
            DailySale.objects.filter(shop=self.empire, product=self.gilbeys,
                                     is_profit_sheet_entry=True).exists()
        )

    def test_daily_expense_totals_replace_existing_amount_and_calculate_net_profit(self):
        start = timezone.localdate() - timedelta(days=2)
        middle = start + timedelta(days=1)
        end = start + timedelta(days=2)
        ShopExpense.objects.create(
            shop=self.fig_tree,
            expense_date=middle,
            description="Previously recorded",
            amount=Decimal("20.00"),
            recorded_by=self.fig_manager,
        )
        legacy_expense = ShopExpense.objects.create(
            shop=self.fig_tree,
            expense_date=middle,
            description="Legacy expense",
            amount=Decimal("5.00"),
            recorded_by=self.fig_manager,
            legacy_source_id=91001,
        )
        self.client.force_login(self.fig_manager)
        url = reverse("shop_profit_sheet", args=[self.fig_tree.pk])
        response = self.client.get(
            url,
            {"date": end.isoformat()},
        )
        self.assertEqual(len(response.context["expense_range_rows"]), 1)
        self.assertEqual(response.context["expense_range_rows"][0]["existing_total"], Decimal("0.00"))

        response = self.client.post(
            url,
            {
                "date": end.isoformat(),
                "action": "calculate_daily_profit",
                f"expense_total_{end.isoformat()}": "250.00",
                f"units_{self.gilbeys.pk}": "2",
            },
        )
        self.assertRedirects(
            response,
            f"{url}?date={end.isoformat()}",
        )
        totals = dict(
            ShopExpense.objects.filter(shop=self.fig_tree, expense_date__range=(start, end))
            .values_list("expense_date")
            .annotate(total=Sum("amount"))
        )
        self.assertEqual(totals[end], Decimal("250.00"))
        self.assertEqual(totals[middle], Decimal("25.00"))
        self.assertNotIn(start, totals)
        legacy_expense.refresh_from_db()
        self.assertEqual(legacy_expense.legacy_source_id, 91001)

        saved = ShopDailyProfit.objects.get(shop=self.fig_tree, profit_date=end)
        self.assertEqual(saved.sales_revenue, Decimal("1100.00"))
        self.assertEqual(saved.product_cost, Decimal("900.00"))
        self.assertEqual(saved.gross_profit, Decimal("200.00"))
        self.assertEqual(saved.expense_total, Decimal("250.00"))
        self.assertEqual(saved.net_profit, Decimal("-50.00"))
        self.assertEqual(saved.calculated_by, self.fig_manager)

        end_sale = DailySale.objects.get(
            shop=self.fig_tree,
            product=self.gilbeys,
            sale_date=end,
            is_profit_sheet_entry=True,
        )
        self.assertEqual(end_sale.units_sold, Decimal("2"))
        end_summary = ShopDailyProfit.objects.get(shop=self.fig_tree, profit_date=end)
        self.assertEqual(end_summary.sales_revenue, Decimal("1100.00"))
        self.assertEqual(end_summary.expense_total, Decimal("250.00"))
        self.assertEqual(end_summary.net_profit, Decimal("-50.00"))

        refreshed = self.client.get(url, {"date": end.isoformat()})
        self.assertEqual(refreshed.context["net_profit"], Decimal("-50.00"))
        self.assertEqual(refreshed.context["saved_daily_profit"], saved)

    def test_saved_daily_profit_includes_sales_and_persists_historical_snapshot(self):
        day = timezone.localdate() - timedelta(days=4)
        DailySale.objects.create(
            shop=self.fig_tree,
            product=self.gilbeys,
            sale_date=day,
            units_sold=Decimal("2"),
            unit_cost_at_sale=Decimal("450.00"),
            unit_price_at_sale=Decimal("550.00"),
            is_profit_sheet_entry=True,
            recorded_by=self.fig_manager,
        )
        self.client.force_login(self.fig_manager)
        url = reverse("shop_profit_sheet", args=[self.fig_tree.pk])
        response = self.client.post(
            url,
            {
                "date": day.isoformat(),
                "action": "calculate_daily_profit",
                "expense_start": day.isoformat(),
                "expense_end": day.isoformat(),
                f"expense_total_{day.isoformat()}": "50.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        record = ShopDailyProfit.objects.get(shop=self.fig_tree, profit_date=day)
        self.assertEqual(record.sales_revenue, Decimal("1100.00"))
        self.assertEqual(record.product_cost, Decimal("900.00"))
        self.assertEqual(record.gross_profit, Decimal("200.00"))
        self.assertEqual(record.expense_total, Decimal("50.00"))
        self.assertEqual(record.net_profit, Decimal("150.00"))

        history = self.client.get(url, {"date": day.isoformat()})
        self.assertEqual(history.context["saved_daily_profit"], record)
        self.assertEqual(history.context["net_profit"], Decimal("150.00"))
        self.assertContains(history, "Saved for")

        self.client.post(
            url,
            {
                "date": day.isoformat(),
                "action": "save_sales",
                f"units_{self.gilbeys.pk}": "3",
            },
        )
        record.refresh_from_db()
        self.assertEqual(record.sales_revenue, Decimal("1650.00"))
        self.assertEqual(record.product_cost, Decimal("1350.00"))
        self.assertEqual(record.gross_profit, Decimal("300.00"))
        self.assertEqual(record.net_profit, Decimal("250.00"))

    def test_expense_range_batch_cannot_write_to_another_shop(self):
        self.client.force_login(self.fig_manager)
        day = timezone.localdate()
        self.client.post(
            reverse("shop_profit_sheet", args=[self.empire.pk]),
            {
                "date": day.isoformat(),
                "action": "calculate_daily_profit",
                "expense_start": day.isoformat(),
                "expense_end": day.isoformat(),
                f"expense_total_{day.isoformat()}": "75.00",
            },
        )
        self.assertTrue(ShopExpense.objects.filter(shop=self.fig_tree, amount=Decimal("75.00")).exists())
        self.assertFalse(ShopExpense.objects.filter(shop=self.empire, amount=Decimal("75.00")).exists())

    def test_profit_sheet_expenses_use_the_selected_trading_date_only(self):
        self.client.force_login(self.fig_manager)
        today = timezone.localdate()
        response = self.client.get(
            reverse("shop_profit_sheet", args=[self.fig_tree.pk]),
            {
                "date": today.isoformat(),
                "expense_start": (today - timedelta(days=10)).isoformat(),
                "expense_end": today.isoformat(),
            },
        )
        self.assertEqual(len(response.context["expense_range_rows"]), 1)
        self.assertEqual(response.context["expense_range_rows"][0]["date"], today)
        self.assertNotContains(response, "Expenses from")
        self.assertNotContains(response, "Load dates")

    def test_manual_entry_is_rejected_if_stock_sheet_already_has_product(self):
        stock_sale = DailySale.objects.create(
            shop=self.fig_tree,
            product=self.gilbeys,
            units_sold=Decimal("1"),
            total_stock=Decimal("10"),
            closing_stock=Decimal("9"),
            recorded_by=self.fig_manager,
        )
        self.client.force_login(self.fig_manager)
        response = self.client.post(
            reverse("shop_profit_sheet", args=[self.fig_tree.pk]),
            {
                "date": stock_sale.sale_date.isoformat(),
                "action": "save_sales",
                f"units_{self.gilbeys.pk}": "3",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            DailySale.objects.filter(shop=self.fig_tree, product=self.gilbeys,
                                     is_profit_sheet_entry=True).exists()
        )

    def test_profit_sheet_groups_products_by_price_list_category(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("shop_profit_sheet", args=[self.fig_tree.pk]))
        groups = response.context["category_groups"]
        self.assertEqual(groups[0]["category"], "250ml")
        self.assertEqual(
            {row["card"].product.category for row in groups[0]["rows"]},
            {"250ml"},
        )
        self.assertContains(response, ">250ml</h3>")
        self.assertNotContains(response, "Profit / unit")
        self.assertNotContains(response, "Recorded By")
        self.assertContains(response, "sheet-responsive-table")
        self.assertContains(response, "grid-template-columns: repeat(2, minmax(0, 1fr))")
        self.assertContains(response, "border border-slate-400")
        self.assertContains(response, 'value="calculate_daily_profit"')
        self.assertContains(response, "Calculate profits")
        self.assertNotContains(response, "Save quantities")
        self.assertNotContains(response, "Products awaiting shop pricing")

    def test_profit_sheet_has_global_and_per_category_search(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("shop_profit_sheet", args=[self.fig_tree.pk]))
        self.assertContains(response, 'id="allProductSearch"')
        self.assertContains(response, "data-category-search")
        self.assertContains(response, "data-product-search=")

    def test_profit_sheet_link_is_available_in_mobile_navigation(self):
        self.client.force_login(self.fig_manager)
        response = self.client.get(reverse("shop_profit_sheet", args=[self.fig_tree.pk]))
        self.assertContains(response, 'aria-label="Mobile navigation"')
        self.assertContains(response, reverse("shop_profit_sheet", args=[self.fig_tree.pk]))

    def test_owner_has_profit_sheets_navigation_link(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("product_report"))
        self.assertContains(response, "Profit Sheets")
        self.assertContains(response, 'aria-label="Mobile navigation"')


class PriceListCatalogueSyncTests(BaseDataTestCase):
    """New workbook products are added without changing shop price overrides."""

    def test_sync_adds_new_products_and_leaves_existing_shop_prices_untouched(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Product", "Cost Price", "Selling Price", "Category"])
        sheet.append(["Brand 350ml", None, None, None])
        sheet.append(["Jameson", None, None, "350ml"])
        sheet.append(["pilsner bottle", None, None, None])
        sheet.append(["Blue Ice", "999", "1200", "250ml"])

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "price list.xlsx")
            workbook.save(path)
            workbook.close()
            call_command("sync_price_list_catalogue", file=path, verbosity=0)

        self.assertTrue(MasterProduct.objects.filter(name="Jameson", category="350ml").exists())
        self.assertTrue(MasterProduct.objects.filter(name="pilsner bottle", category="").exists())
        self.assertFalse(MasterProduct.objects.filter(name="Brand 350ml").exists())
        blue_ice_card = ShopProduct.objects.get(shop=self.fig_tree, product=self.blue_ice)
        self.assertEqual(blue_ice_card.cost_price, Decimal("150.00"))
        self.assertEqual(blue_ice_card.selling_price, Decimal("200.00"))
        for shop in (self.fig_tree, self.empire):
            jameson = MasterProduct.objects.get(name="Jameson", category="350ml")
            self.assertTrue(ShopProduct.objects.filter(
                shop=shop, product=jameson, cost_price=Decimal("0.00"),
                selling_price=Decimal("0.00"),
            ).exists())


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
        # The stock boxes are present for each product. Total stock is derived
        # (opening + additional), so it is not one of the boxes.
        self.assertContains(response, f'name="open_{self.gilbeys.pk}"')
        self.assertContains(response, f'name="add_{self.gilbeys.pk}"')
        self.assertContains(response, f'name="close_{self.gilbeys.pk}"')
        self.assertNotContains(response, f'name="total_{self.gilbeys.pk}"')

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

    def test_total_stock_is_derived_when_it_is_not_typed(self):
        """The sheet only asks for opening, additional and closing stock."""
        self.client.force_login(self.fig_manager)
        response = self.post_stock(
            (self.gilbeys, {"additional": "10", "closing": "6"}),
        )
        self.assertRedirects(response, reverse(self.SHEET))

        gilbeys = DailySale.objects.get(product=self.gilbeys)
        # Total = opening (0) + additional (10); sold = 10 - closing (6).
        self.assertEqual(gilbeys.total_stock, Decimal("10.00"))
        self.assertEqual(gilbeys.units_sold, Decimal("4.00"))
        self.assertEqual(gilbeys.total_revenue, Decimal("2200.00"))

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


class StockBatchProfitTests(BaseDataTestCase):
    """Profit from a stock addition date to today, per product and shop."""

    def test_profit_counts_sales_from_the_addition_date_to_today(self):
        today = timezone.localdate()
        added_day = today - timedelta(days=5)

        # 20 units added, none sold on the day.
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=added_day,
            additional_stock=Decimal("20"),
            total_stock=Decimal("20"),
            closing_stock=Decimal("20"),
            recorded_by=self.empire_manager,
        )
        # Later, 10 of them sold at Empire's 450/600 price (150 profit each).
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=today - timedelta(days=1),
            total_stock=Decimal("20"),
            closing_stock=Decimal("10"),
            recorded_by=self.empire_manager,
        )

        rows = _stock_batch_profit_rows(added_day, added_day)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["added"], Decimal("20.00"))
        self.assertEqual(row["sold"], Decimal("10.00"))
        self.assertEqual(row["remaining"], Decimal("10.00"))
        self.assertEqual(row["profit"], Decimal("1500.00"))
        self.assertEqual(row["revenue"], Decimal("6000.00"))
        self.assertEqual(row["unit_profit"], Decimal("150.00"))

    def test_sales_before_the_addition_are_excluded(self):
        today = timezone.localdate()
        added_day = today - timedelta(days=2)

        # Sold out of existing stock the day before the addition.
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=added_day - timedelta(days=1),
            units_sold=Decimal("4"),
            recorded_by=self.empire_manager,
        )
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=added_day,
            additional_stock=Decimal("12"),
            total_stock=Decimal("12"),
            closing_stock=Decimal("9"),
            recorded_by=self.empire_manager,
        )

        row = _stock_batch_profit_rows(added_day, added_day)[0]

        self.assertEqual(row["sold"], Decimal("3.00"))
        self.assertEqual(row["profit"], Decimal("450.00"))

    def test_shops_and_date_range_filter_the_batches(self):
        today = timezone.localdate()
        old_day = today - timedelta(days=30)
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=old_day,
            additional_stock=Decimal("5"),
            total_stock=Decimal("5"),
            closing_stock=Decimal("5"),
            recorded_by=self.empire_manager,
        )

        self.assertEqual(
            _stock_batch_profit_rows(today - timedelta(days=7), today), []
        )
        self.assertEqual(
            _stock_batch_profit_rows(old_day, old_day, shop_ids={self.fig_tree.pk}), []
        )
        self.assertEqual(len(_stock_batch_profit_rows(old_day, old_day)), 1)

    def test_report_page_shows_the_stock_batch_section(self):
        today = timezone.localdate()
        added_day = today - timedelta(days=5)
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=added_day,
            additional_stock=Decimal("20"),
            total_stock=Decimal("20"),
            closing_stock=Decimal("20"),
            recorded_by=self.empire_manager,
        )
        DailySale.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            sale_date=today - timedelta(days=1),
            total_stock=Decimal("20"),
            closing_stock=Decimal("10"),
            recorded_by=self.empire_manager,
        )

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("product_report"),
            {
                "stock_start_date": added_day.isoformat(),
                "stock_end_date": added_day.isoformat(),
                "stock_shop": self.empire.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["stock_rows"]), 1)
        self.assertContains(response, "Stock added → profit to date")
        self.assertContains(response, "KSh 1,500.00")


class ProfitSheetResubmitTests(BaseDataTestCase):
    """Re-submitting a day replaces that day's entries instead of leaving stale ones."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.fig_manager)
        self.day = timezone.localdate()
        self.url = reverse("shop_profit_sheet", args=[self.fig_tree.pk])

    def _submit(self, value):
        return self.client.post(
            self.url,
            {"date": self.day.isoformat(), "action": "save_sales",
             f"units_{self.gilbeys.pk}": value},
        )

    def _entry(self):
        return DailySale.objects.filter(
            shop=self.fig_tree, product=self.gilbeys, sale_date=self.day,
            is_profit_sheet_entry=True,
        ).first()

    def test_zero_removes_the_entry(self):
        self._submit("4")
        self.assertIsNotNone(self._entry())
        # Snapshot the day's profit, then zero the quantity.
        self.client.post(
            self.url,
            {"date": self.day.isoformat(), "action": "calculate_daily_profit"},
        )
        self.assertEqual(
            ShopDailyProfit.objects.get(shop=self.fig_tree, profit_date=self.day).gross_profit,
            Decimal("400.00"),
        )

        self._submit("0")

        self.assertIsNone(self._entry())
        saved = ShopDailyProfit.objects.get(shop=self.fig_tree, profit_date=self.day)
        self.assertEqual(saved.gross_profit, Decimal("0.00"))

    def test_positive_value_updates_the_same_entry(self):
        self._submit("4")
        self._submit("6")

        self.assertEqual(
            DailySale.objects.filter(
                shop=self.fig_tree, product=self.gilbeys, sale_date=self.day,
                is_profit_sheet_entry=True,
            ).count(),
            1,
        )
        self.assertEqual(self._entry().units_sold, Decimal("6"))

    def test_blank_leaves_the_entry_unchanged(self):
        self._submit("4")
        self._submit("")

        self.assertEqual(self._entry().units_sold, Decimal("4"))


class ProductReportFilterTests(BaseDataTestCase):
    """The reports page and its CSV honour the shop and product filters."""

    def setUp(self):
        super().setUp()
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, units_sold=Decimal("2"),
            recorded_by=self.empire_manager,
        )
        self.client.force_login(self.owner)

    def test_product_search_narrows_the_table(self):
        response = self.client.get(reverse("product_report"), {"product": "Gilbeys"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["product"].name for row in response.context["rows"]], ["Gilbeys"]
        )

    def test_filters_reach_the_csv_export(self):
        response = self.client.get(
            reverse("product_report_csv"),
            {"product": "Gilbeys", "report_shop": self.empire.pk},
        )

        self.assertEqual(response.status_code, 200)
        lines = response.content.decode().splitlines()
        self.assertEqual(
            lines[1],
            "Product,Category,Empire Shop Units,Empire Shop Profit,"
            "Total Units,Total Profit,Total Revenue",
        )
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[2].startswith("Gilbeys"))
        self.assertNotIn("Fig Tree", response.content.decode())


class ManualStockAdditionTests(BaseDataTestCase):
    """Stock the owner records by hand feeds the stock-batch report."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.added_day = self.today - timedelta(days=4)

    def _add(self, quantity="20", added_on=None):
        return StockAddition.objects.create(
            shop=self.empire,
            product=self.gilbeys,
            added_on=added_on or self.added_day,
            quantity=Decimal(quantity),
            recorded_by=self.owner,
        )

    def test_manual_addition_reports_sales_and_per_day_breakdown(self):
        self._add()
        # 6 sold two days after the delivery, 4 more the day after that.
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys,
            sale_date=self.added_day + timedelta(days=2), units_sold=Decimal("6"),
            recorded_by=self.empire_manager,
        )
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys,
            sale_date=self.added_day + timedelta(days=3), units_sold=Decimal("4"),
            recorded_by=self.empire_manager,
        )

        rows = _stock_batch_profit_rows(self.added_day, self.added_day)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["added"], Decimal("20.00"))
        self.assertEqual(row["sold"], Decimal("10.00"))
        self.assertEqual(row["remaining"], Decimal("10.00"))
        self.assertEqual(row["profit"], Decimal("1500.00"))
        self.assertEqual(
            [(day["date"], day["units"], day["profit"]) for day in row["daily"]],
            [
                (self.added_day + timedelta(days=2), Decimal("6.00"), Decimal("900.00")),
                (self.added_day + timedelta(days=3), Decimal("4.00"), Decimal("600.00")),
            ],
        )

    def test_manual_addition_respects_the_shop_filter(self):
        self._add()

        self.assertEqual(
            _stock_batch_profit_rows(
                self.added_day, self.added_day, shop_ids={self.fig_tree.pk}
            ),
            [],
        )

    def test_owner_can_add_stock_from_the_report_page(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("product_report"),
            {
                "shop": self.empire.pk,
                "product": self.gilbeys.pk,
                "added_on": self.added_day.isoformat(),
                "quantity": "20",
                "stock_start_date": self.added_day.isoformat(),
                "stock_end_date": self.added_day.isoformat(),
                "stock_shop": self.empire.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        addition = StockAddition.objects.get()
        self.assertEqual(addition.quantity, Decimal("20.00"))
        self.assertEqual(addition.recorded_by, self.owner)

        page = self.client.get(
            reverse("product_report"),
            {
                "stock_start_date": self.added_day.isoformat(),
                "stock_end_date": self.added_day.isoformat(),
            },
        )
        self.assertEqual(len(page.context["stock_rows"]), 1)
        self.assertContains(page, "Manual")

    def test_stock_for_a_product_the_shop_does_not_carry_is_rejected(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("product_report"),
            {
                "shop": self.empire.pk,
                "product": self.blue_ice.pk,
                "added_on": self.added_day.isoformat(),
                "quantity": "5",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StockAddition.objects.exists())
        self.assertContains(
            response, "This product is not stocked by the selected shop.", status_code=400
        )

    def test_owner_can_remove_a_manual_addition(self):
        addition = self._add()
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("product_report"), {"delete_stock_addition": addition.pk}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(StockAddition.objects.exists())

    def test_totals_sum_the_rows_shown(self):
        self._add()
        DailySale.objects.create(
            shop=self.empire, product=self.gilbeys, sale_date=self.added_day,
            units_sold=Decimal("5"), recorded_by=self.empire_manager,
        )
        self.client.force_login(self.owner)

        page = self.client.get(
            reverse("product_report"),
            {
                "stock_start_date": self.added_day.isoformat(),
                "stock_end_date": self.added_day.isoformat(),
            },
        )

        totals = page.context["stock_totals"]
        self.assertEqual(totals["added"], Decimal("20.00"))
        self.assertEqual(totals["sold"], Decimal("5.00"))
        self.assertEqual(totals["remaining"], Decimal("15.00"))
        self.assertEqual(totals["revenue"], Decimal("3000.00"))
        self.assertEqual(totals["profit"], Decimal("750.00"))
        self.assertEqual(totals["unit_profit"], Decimal("150.00"))
        self.assertContains(page, "Totals")


class SingleRowPerDayTests(BaseDataTestCase):
    """One sale row per shop, product and day, whatever recorded it."""

    def test_opening_stock_can_be_corrected_by_hand(self):
        self.client.force_login(self.fig_manager)

        self.client.post(
            reverse("daily_sales_sheet"),
            {
                f"open_{self.gilbeys.pk}": "5",
                f"add_{self.gilbeys.pk}": "10",
                f"total_{self.gilbeys.pk}": "15",
                f"close_{self.gilbeys.pk}": "3",
            },
        )

        sale = DailySale.objects.get(
            shop=self.fig_tree, product=self.gilbeys, sale_date=timezone.localdate()
        )
        self.assertEqual(sale.opening_stock, Decimal("5.00"))
        self.assertEqual(sale.units_sold, Decimal("12.00"))

    def test_stock_sheet_rewrites_a_manual_entry_for_the_same_day(self):
        day = timezone.localdate()
        DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, sale_date=day,
            units_sold=Decimal("4"), is_profit_sheet_entry=True,
            recorded_by=self.fig_manager,
        )
        self.client.force_login(self.fig_manager)

        self.client.post(
            reverse("daily_sales_sheet"),
            {
                f"add_{self.gilbeys.pk}": "10",
                f"total_{self.gilbeys.pk}": "10",
                f"close_{self.gilbeys.pk}": "3",
            },
        )

        rows = DailySale.objects.filter(
            shop=self.fig_tree, product=self.gilbeys, sale_date=day
        )
        self.assertEqual(rows.count(), 1)
        sale = rows.get()
        self.assertTrue(sale.is_stock_entry)
        self.assertFalse(sale.is_profit_sheet_entry)
        # Sold comes from the count (10 - 3), not from the replaced manual 4.
        self.assertEqual(sale.units_sold, Decimal("7.00"))

    def test_profit_sheet_units_entered_twice_keep_the_latest(self):
        day = timezone.localdate()
        url = reverse("shop_profit_sheet", args=[self.fig_tree.pk])
        self.client.force_login(self.fig_manager)

        for units in ("5", "7"):
            self.client.post(
                url,
                {
                    "action": "calculate_daily_profit",
                    "date": day.isoformat(),
                    f"units_{self.gilbeys.pk}": units,
                },
            )

        rows = DailySale.objects.filter(
            shop=self.fig_tree, product=self.gilbeys, sale_date=day
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().units_sold, Decimal("7.00"))

    def test_profit_sheet_entry_replaces_an_older_sale_for_the_same_day(self):
        day = timezone.localdate()
        legacy = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, sale_date=day,
            units_sold=Decimal("3"), recorded_by=self.fig_manager,
        )
        self.client.force_login(self.fig_manager)

        self.client.post(
            reverse("shop_profit_sheet", args=[self.fig_tree.pk]),
            {
                "action": "calculate_daily_profit",
                "date": day.isoformat(),
                f"units_{self.gilbeys.pk}": "8",
            },
        )

        rows = DailySale.objects.filter(
            shop=self.fig_tree, product=self.gilbeys, sale_date=day
        )
        self.assertEqual(rows.count(), 1)
        sale = rows.get()
        self.assertEqual(sale.pk, legacy.pk)
        self.assertEqual(sale.units_sold, Decimal("8.00"))
        self.assertTrue(sale.is_profit_sheet_entry)


class MergeDuplicateSalesCommandTests(BaseDataTestCase):
    """The cleanup command folds an older sale into the day's manual figure."""

    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        self.older = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, sale_date=self.day,
            units_sold=Decimal("3"), recorded_by=self.fig_manager,
        )
        self.manual = DailySale.objects.create(
            shop=self.fig_tree, product=self.gilbeys, sale_date=self.day,
            units_sold=Decimal("8"), is_profit_sheet_entry=True,
            recorded_by=self.fig_manager,
        )
        ShopDailyProfit.objects.create(
            shop=self.fig_tree, profit_date=self.day,
            sales_revenue=Decimal("5500.00"), product_cost=Decimal("3600.00"),
            gross_profit=Decimal("1350.00"), expense_total=Decimal("0.00"),
            net_profit=Decimal("1350.00"), calculated_by=self.fig_manager,
        )

    def test_dry_run_deletes_nothing(self):
        call_command("merge_duplicate_sales", stdout=StringIO())

        self.assertEqual(
            DailySale.objects.filter(
                shop=self.fig_tree, product=self.gilbeys, sale_date=self.day
            ).count(),
            2,
        )

    def test_apply_keeps_the_manual_entry_and_recalculates_the_day(self):
        call_command("merge_duplicate_sales", "--apply", stdout=StringIO())

        rows = DailySale.objects.filter(
            shop=self.fig_tree, product=self.gilbeys, sale_date=self.day
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().pk, self.manual.pk)
        self.assertEqual(rows.get().units_sold, Decimal("8.00"))

        saved = ShopDailyProfit.objects.get(shop=self.fig_tree, profit_date=self.day)
        # 8 units x (550 - 450) = 800, no longer 1350 from 11 units.
        self.assertEqual(saved.gross_profit, Decimal("800.00"))
        self.assertEqual(saved.sales_revenue, Decimal("4400.00"))
