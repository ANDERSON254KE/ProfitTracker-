"""Views for the multi-shop profit tracker.

Role rules applied throughout:

* OWNER  - sees every shop: the cross-shop dashboard and the price editor.
* MANAGER - sees only ``request.user.assigned_shop``: their dashboard, their
  recent sales, and the form used to record a new sale.

Profit is never summed from client-supplied numbers; every figure below is
aggregated from :class:`~tracker.models.DailySale` rows whose money columns are
computed by the model.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView as AuthLoginView
from django.contrib.auth.views import LogoutView as AuthLogoutView
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, RedirectView, TemplateView, View

from .forms import DailySaleForm, ShopPricingForm
from .mixins import (
    LoginRequiredMixin,
    ManagerRequiredMixin,
    ManagerShopAccessMixin,
    OwnerRequiredMixin,
)
from .models import CustomUser, DailySale, Shop, ShopProduct

#: Number of days shown in the owner's "recent days" table.
OWNER_TREND_DAYS = 7
#: Number of recent sale lines shown on a manager's dashboard.
MANAGER_HISTORY_ROWS = 15
#: Kenyan shillings symbol used by the ``ksh`` template filter.
CURRENCY = "KSh"


def _money(value) -> Decimal:
    """Coerce an aggregate (``Decimal``, ``None`` or ``int``) to a Decimal."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value))


def _shop_sales(shop):
    """Return the DailySale queryset for one shop (used by both dashboards)."""
    return DailySale.objects.filter(shop=shop).select_related("product", "recorded_by")


class LoginView(AuthLoginView):
    """Sign a user in and send them to the dashboard for their role."""

    template_name = "tracker/login.html"
    redirect_authenticated_user = True

    def get_success_url(self) -> str:
        """Route the user to their role's dashboard."""
        return self.get_redirect_url() or "/"

    def form_valid(self, form):
        """Log the user in, remembering an explicit ``next`` when present."""
        response = super().form_valid(form)
        messages.success(
            self.request,
            f"Welcome back, {self.request.user.get_full_name() or self.request.user.username}.",
        )
        return response


class LogoutView(AuthLogoutView):
    """Sign a user out and return them to the login page.

    Django 5+ only accepts POST for logout, so the nav bar posts a form here.
    """

    http_method_names = ["post", "options"]


class DashboardRedirectView(LoginRequiredMixin, RedirectView):
    """Send a signed-in user to the dashboard that matches their role."""

    permanent = False

    def get_redirect_url(self, *args, **kwargs) -> str:
        """Return the owner's or the manager's dashboard URL."""
        if self.request.user.is_owner():
            return reverse("owner_dashboard")
        return reverse("manager_dashboard")


class OwnerDashboardView(OwnerRequiredMixin, TemplateView):
    """Cross-shop performance for the owner: totals, per-shop profit, 7-day trend.

    :class:`~tracker.mixins.OwnerRequiredMixin` already applies
    ``LoginRequiredMixin``, so anonymous users are sent to the login page and
    managers get a 403.
    """

    template_name = "tracker/owner_dashboard.html"

    def get_context_data(self, **kwargs):
        """Aggregate profit and units for all shops and the last 7 days."""
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        window_start = today - timedelta(days=OWNER_TREND_DAYS - 1)

        sales = DailySale.objects.select_related("product", "shop")
        shops = list(Shop.objects.all())
        context["shops"] = shops

        # Overall totals across every shop.
        overall = sales.aggregate(
            profit=Sum("profit"),
            units=Sum("units_sold"),
            revenue=Sum("total_revenue"),
        )
        context["total_profit"] = _money(overall["profit"])
        context["total_units"] = _money(overall["units"])
        context["total_revenue"] = _money(overall["revenue"])

        # Per-shop profit and units, one row per shop (zero-sold shops included).
        per_shop = {
            row["shop"]: row
            for row in sales.values("shop")
            .annotate(profit=Sum("profit"), units=Sum("units_sold"))
        }
        shop_rows = []
        for shop in shops:
            row = per_shop.get(shop.pk, {})
            shop_rows.append(
                {
                    "shop": shop,
                    "profit": _money(row.get("profit")),
                    "units": _money(row.get("units")),
                }
            )
        shop_rows.sort(key=lambda r: r["profit"], reverse=True)
        context["shop_rows"] = shop_rows

        # Last 7 days as an explicit table (one row per day, one column per
        # shop). Cells are built in `shops` order so they line up with the
        # table header, which is rendered from the same list.
        days = [window_start + timedelta(days=i) for i in range(OWNER_TREND_DAYS)]
        trend = sales.filter(sale_date__gte=window_start).values("sale_date", "shop").annotate(
            profit=Sum("profit")
        )
        trend_map = {}
        for row in trend:
            trend_map.setdefault(row["sale_date"], {})[row["shop"]] = _money(row["profit"])

        trend_rows = []
        for day in days:
            cells = trend_map.get(day, {})
            values = [cells.get(shop.pk, Decimal("0.00")) for shop in shops]
            trend_rows.append(
                {
                    "date": day,
                    "cells": values,
                    "day_total": sum(values, Decimal("0.00")),
                    "is_today": day == today,
                }
            )
        context["trend_rows"] = trend_rows
        context["today"] = today
        return context


class ManagerDashboardView(ManagerRequiredMixin, TemplateView):
    """Shop-scoped performance for a manager."""

    template_name = "tracker/manager_dashboard.html"

    def get_context_data(self, **kwargs):
        """Build today's, lifetime and recent-sale figures for the user's shop."""
        context = super().get_context_data(**kwargs)
        user = self.request.user
        shop = user.assigned_shop
        today = timezone.localdate()

        if shop is None:
            context["shop"] = None
            context["has_shop"] = False
            return context

        context["shop"] = shop
        context["has_shop"] = True
        context["today"] = today

        sales = _shop_sales(shop)

        today_row = sales.filter(sale_date=today).aggregate(
            profit=Sum("profit"), units=Sum("units_sold")
        )
        context["today_profit"] = _money(today_row["profit"])
        context["today_units"] = _money(today_row["units"])

        lifetime = sales.aggregate(profit=Sum("profit"), units=Sum("units_sold"))
        context["total_profit"] = _money(lifetime["profit"])
        context["total_units"] = _money(lifetime["units"])

        context["recent_sales"] = sales.order_by("-sale_date", "-id")[:MANAGER_HISTORY_ROWS]
        context["sale_form_url"] = reverse("daily_sale_create")
        return context


class DailySaleCreateView(LoginRequiredMixin, ManagerShopAccessMixin, CreateView):
    """Record a new sale for the manager's shop.

    The shop is forced by :class:`ManagerShopAccessMixin`, the prices are
    locked in by ``DailySale.save``, and the user is stamped onto the row.
    """

    model = DailySale
    form_class = DailySaleForm
    template_name = "tracker/daily_sale_form.html"

    def get_form_kwargs(self):
        """Pass the resolved shop into the form so it can scope its choices."""
        kwargs = super().get_form_kwargs()
        kwargs["shop"] = self.shop
        return kwargs

    def form_valid(self, form):
        """Stamp the recorder, save, and confirm the server-calculated profit."""
        # Set before saving: the form only covers product and quantity, and
        # recorded_by is not a client-supplied field.
        form.instance.recorded_by = self.request.user
        response = super().form_valid(form)
        sale = self.object
        messages.success(
            self.request,
            f"Recorded {sale.units_sold} x {sale.product} at {self.shop.name}. "
            f"Profit: {CURRENCY} {sale.profit:,.2f}.",
        )
        return response

    def get_success_url(self) -> str:
        """Return to the manager's dashboard after saving."""
        return f"{reverse('manager_dashboard')}?recorded=1"


class ShopPricingView(OwnerRequiredMixin, View):
    """Owner-only inline editor for every shop's price card.

    ``GET`` renders all :class:`ShopProduct` rows grouped by shop. ``POST``
    accepts ``shop_product-<pk>-<field>`` inputs and updates each row, so the
    owner can correct many prices in a single submit.
    """

    template_name = "tracker/shop_pricing.html"

    def get(self, request, *args, **kwargs):
        """Render the price editor with all rows grouped by shop."""
        grouped = {}
        shop_products = ShopProduct.objects.select_related("shop", "product").order_by(
            "shop__name", "product__name"
        )
        for sp in shop_products:
            grouped.setdefault(sp.shop, []).append(sp)

        return render(
            request,
            self.template_name,
            {
                "grouped_products": grouped,
                "shops": Shop.objects.all(),
            },
        )

    def post(self, request, *args, **kwargs):
        """Apply the submitted prices, then redirect back with a summary."""
        updates = 0
        for sp in ShopProduct.objects.select_related("shop", "product").all():
            prefix = f"shop_product-{sp.pk}-"
            cost_raw = request.POST.get(prefix + "cost_price")
            sell_raw = request.POST.get(prefix + "selling_price")
            if cost_raw is None and sell_raw is None:
                continue

            cost = ShopPricingForm.to_decimal(cost_raw)
            sell = ShopPricingForm.to_decimal(sell_raw)
            active = request.POST.get(prefix + "is_active") == "on"

            problems = []
            if cost is None or cost < Decimal("0.00"):
                problems.append(f"{sp.product} @ {sp.shop.name}: invalid cost price.")
            if sell is None or sell < Decimal("0.00"):
                problems.append(f"{sp.product} @ {sp.shop.name}: invalid selling price.")
            if problems:
                for problem in problems:
                    messages.error(request, problem)
                continue

            sp.cost_price = cost.quantize(Decimal("0.01"))
            sp.selling_price = sell.quantize(Decimal("0.01"))
            sp.is_active = active
            sp.save(update_fields=["cost_price", "selling_price", "is_active"])
            updates += 1

        if updates:
            messages.success(request, f"Saved prices for {updates} product(s).")
        else:
            messages.warning(request, "No price changes were submitted.")
        return redirect(reverse("shop_pricing"))


class ShopProductPriceAPIView(LoginRequiredMixin, ManagerShopAccessMixin, View):
    """Return the cost/selling price of a product for the requesting shop.

    Used by ``daily_sale_form.html`` to live-preview profit in the browser.
    The lookup is scoped to the manager's own shop, so it can only ever
    disclose prices the manager is already entitled to see.
    """

    def get(self, request, product_id, *args, **kwargs):
        """Return ``{cost_price, selling_price}`` as JSON."""
        shop_product = (
            ShopProduct.objects.filter(shop=self.shop, product_id=product_id)
            .select_related("product")
            .first()
        )
        if shop_product is None or not shop_product.is_active:
            return JsonResponse(
                {"error": "Product is not available for this shop."}, status=404
            )

        return JsonResponse(
            {
                "product": str(shop_product.product),
                "cost_price": str(shop_product.cost_price),
                "selling_price": str(shop_product.selling_price),
            }
        )
