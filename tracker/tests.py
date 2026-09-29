"""Tests for the tracker's money maths, price locking and access control.

Run with::

    python manage.py test tracker
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

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
