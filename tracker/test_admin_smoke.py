"""Smoke test: every admin page renders without errors.

Covers the admin index, each registered model's changelist, the add forms and a
change form (so the ProductShopPrice inline is exercised). The DailySale
changelist is seeded with more rows than one page so ``pagination_required`` is
true - that is the path that raised ``TypeError: args or kwargs must be
provided`` in production (django-jazzmin 3.0.1 calling ``format_html(html_str)``
on Django 6.0).

The test client re-raises view exceptions, so a broken admin page fails the
test instead of returning a pretty error page.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from tracker.models import DailySale, Product

TRACKER_MODELS = ("product", "transaction", "dailysale", "dailyexpense", "daytotals")
ADD_FORM_MODELS = TRACKER_MODELS


class AdminPagesRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="admin-smoke", email="smoke@example.com", password="pw"
        )
        cls.product = Product.objects.create(
            product_name="Gilbeys", category="250ml",
            cost_price=Decimal("416.00"), selling_price=Decimal("550.00"),
        )
        # More rows than the admin's list_per_page (100) so the changelist
        # paginates and the jazzmin pagination tag is actually rendered.
        DailySale.objects.bulk_create(
            DailySale(
                product=cls.product,
                shop="Fig Tree",
                quantity_sold=Decimal("1"),
                sell_price=Decimal("550.00"),
                cost_price=Decimal("416.00"),
                profit=Decimal("134.00"),
            )
            for _ in range(120)
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def test_admin_index_renders(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)

    def test_app_index_renders(self):
        response = self.client.get(reverse("admin:app_list", args=["tracker"]))
        self.assertEqual(response.status_code, 200)

    def test_changelists_render(self):
        for model in TRACKER_MODELS:
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:tracker_{model}_changelist"))
                self.assertEqual(response.status_code, 200)

    def test_auth_changelists_render(self):
        for model in ("user", "group"):
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:auth_{model}_changelist"))
                self.assertEqual(response.status_code, 200)

    def test_dailysale_changelist_paginates(self):
        """The reported /admin/tracker/dailysale/ page, on page 1 and 2."""
        for params in ({}, {"p": "2"}):
            with self.subTest(params=params):
                response = self.client.get(
                    reverse("admin:tracker_dailysale_changelist"), params
                )
                self.assertEqual(response.status_code, 200)
        self.assertContains(
            self.client.get(reverse("admin:tracker_dailysale_changelist")),
            "p=2",
            msg_prefix="expected the paginated changelist to render page links",
        )

    def test_add_forms_render(self):
        for model in ADD_FORM_MODELS:
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:tracker_{model}_add"))
                self.assertEqual(response.status_code, 200)

    def test_product_change_form_with_inline_renders(self):
        response = self.client.get(
            reverse("admin:tracker_product_change", args=[self.product.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shop Price Override")
