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
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlencode
import csv

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView as AuthLoginView
from django.contrib.auth.views import LogoutView as AuthLogoutView
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.generic import CreateView, DetailView, RedirectView, TemplateView, View

from .forms import DailySaleForm, DailySalesSheetForm, ShopPricingForm
from .mixins import (
    LoginRequiredMixin,
    ManagerRequiredMixin,
    ManagerShopAccessMixin,
    OwnerRequiredMixin,
)
from .models import CustomUser, DailySale, MasterProduct, Shop, ShopExpense, ShopProduct
from .price_list import price_list_rank

#: Number of days shown in the owner's "recent days" table.
OWNER_TREND_DAYS = 7
#: Days spanned by the owner's dashboard profit-trend chart.
OWNER_CHART_DAYS = 30
#: Products listed in the owner's "top products" chart.
OWNER_TOP_PRODUCTS = 10
#: Days spanned by a manager's dashboard trend chart.
MANAGER_CHART_DAYS = 14
#: Number of recent sale lines shown on a manager's dashboard.
MANAGER_HISTORY_ROWS = 15
#: Rows per page in a shop report's dated profit table.
SHOP_DETAIL_PAGE_SIZE = 30
#: Products in a shop's "most sold" report.
SHOP_TOP_PRODUCTS = 10
#: Kenyan shillings symbol used by the ``ksh`` template filter.
CURRENCY = "KSh"


def _money(value) -> Decimal:
    """Coerce an aggregate (``Decimal``, ``None`` or ``int``) to a Decimal."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value))


def _parse_date(value):
    """Parse an ISO date query value, returning ``None`` for invalid input."""
    try:
        return parse_date(value or "")
    except (TypeError, ValueError):
        return None


def _shop_sales(shop):
    """Return the DailySale queryset for one shop (used by both dashboards)."""
    return DailySale.objects.filter(shop=shop).select_related("product", "recorded_by")


def _input_value(value) -> str:
    """Render a stock figure for an HTML input: '' when there is no figure yet."""
    if value is None:
        return ""
    return str(value)


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

        # Per-shop profit, units and amount sold, one row per shop (zero-sold shops included).
        per_shop = {
            row["shop"]: row
            for row in sales.values("shop")
            .annotate(
                profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue")
            )
        }
        today_per_shop = {
            row["shop"]: _money(row["profit"])
            for row in sales.filter(sale_date=today)
            .values("shop")
            .annotate(profit=Sum("profit"))
        }
        week_per_shop = {
            row["shop"]: _money(row["profit"])
            for row in sales.filter(sale_date__gte=window_start)
            .values("shop")
            .annotate(profit=Sum("profit"))
        }
        shop_rows = []
        for shop in shops:
            row = per_shop.get(shop.pk, {})
            shop_rows.append(
                {
                    "shop": shop,
                    "profit": _money(row.get("profit")),
                    "units": _money(row.get("units")),
                    "revenue": _money(row.get("revenue")),
                    "today_profit": today_per_shop.get(shop.pk, Decimal("0.00")),
                    "week_profit": week_per_shop.get(shop.pk, Decimal("0.00")),
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

        # --- Chart payloads, rendered by Chart.js in the template ---
        week = sales.filter(sale_date__gte=window_start).aggregate(profit=Sum("profit"))
        context["week_profit"] = _money(week["profit"])

        chart_start = today - timedelta(days=OWNER_CHART_DAYS - 1)
        chart_days = [chart_start + timedelta(days=i) for i in range(OWNER_CHART_DAYS)]
        shop_trend = sales.filter(sale_date__gte=chart_start).values(
            "sale_date", "shop"
        ).annotate(profit=Sum("profit"))
        trend_map = {}
        for row in shop_trend:
            trend_map.setdefault(row["sale_date"], {})[row["shop"]] = _money(
                row["profit"]
            )

        top_products = (
            sales.values("product__name", "product__category")
            .annotate(profit=Sum("profit"))
            .order_by("-profit")[:OWNER_TOP_PRODUCTS]
        )
        context["analytics"] = {
            "shop_profits": {
                "labels": [row["shop"].name for row in shop_rows],
                "values": [float(row["profit"]) for row in shop_rows],
            },
            "trend": {
                "labels": [day.strftime("%d %b") for day in chart_days],
                "datasets": [
                    {
                        "label": shop.name,
                        "data": [
                            float(trend_map.get(day, {}).get(shop.pk, Decimal("0.00")))
                            for day in chart_days
                        ],
                    }
                    for shop in shops
                ],
            },
            "top_products": {
                "labels": [
                    f"{row['product__name']} · {row['product__category']}"
                    if row["product__category"]
                    else row["product__name"]
                    for row in top_products
                ],
                "values": [float(_money(row["profit"])) for row in top_products],
            },
        }
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
            profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue")
        )
        context["today_profit"] = _money(today_row["profit"])
        context["today_units"] = _money(today_row["units"])
        context["today_revenue"] = _money(today_row["revenue"])

        lifetime = sales.aggregate(profit=Sum("profit"), units=Sum("units_sold"))
        context["total_profit"] = _money(lifetime["profit"])
        context["total_units"] = _money(lifetime["units"])

        # Daily profit chart for the manager's own shop, last 14 days.
        trend_start = today - timedelta(days=MANAGER_CHART_DAYS - 1)
        days = [trend_start + timedelta(days=i) for i in range(MANAGER_CHART_DAYS)]
        per_day = {
            row["sale_date"]: _money(row["profit"])
            for row in sales.filter(sale_date__gte=trend_start)
            .values("sale_date")
            .annotate(profit=Sum("profit"))
        }
        context["trend"] = {
            "labels": [day.strftime("%d %b") for day in days],
            "values": [float(per_day.get(day, Decimal("0.00"))) for day in days],
        }

        context["recent_sales"] = sales.order_by("-sale_date", "-id")[:MANAGER_HISTORY_ROWS]
        context["sale_form_url"] = reverse("daily_sales_sheet")
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


class DailySalesSheetView(ManagerRequiredMixin, ManagerShopAccessMixin, TemplateView):
    """Record a whole trading day for the manager's shop in one submit.

    ``GET`` lists the shop's products in the owner's price-list row order
    (250ml first, down to soda) with their opening stock -- the previous day's
    closing stock -- and boxes for additional stock, total stock and closing
    stock; sold units and the amount sold are derived server side. Products
    with no cost price are not shown: they cannot produce a profit, so the
    owner must price them first.

    ``POST`` upserts one stock row per filled-in product for today: submitting
    again replaces today's figures instead of double-counting them.
    """

    template_name = "tracker/daily_sales_sheet.html"

    def _build_context(self):
        """Gather the sheet rows, openings and the day's recorded totals."""
        shop = self.shop
        today = timezone.localdate()

        price_card = list(
            ShopProduct.objects.filter(
                shop=shop, is_active=True, cost_price__gt=0
            )
            .select_related("product")
            .order_by("product__name", "product__category")
        )

        # Follow the owner's price-list row order (250ml ... soda). Products
        # missing from the file sort alphabetically after the listed ones.
        rank = price_list_rank()
        price_card.sort(
            key=lambda card: (
                rank.get((card.product.name, card.product.category), len(rank)),
                card.product.name,
                card.product.category,
            )
        )

        # Today's saved stock rows, used to pre-fill the inputs.
        today_entries = {
            sale.product_id: sale
            for sale in DailySale.objects.filter(
                shop=shop, sale_date=today, total_stock__isnull=False
            )
        }

        # Opening stock = the latest closing stock before today, per product.
        openings = {}
        previous = (
            DailySale.objects.filter(
                shop=shop, sale_date__lt=today, closing_stock__isnull=False
            )
            .order_by("product_id", "-sale_date", "-id")
        )
        for sale in previous:
            openings.setdefault(sale.product_id, sale.closing_stock)

        rows = []
        for card in price_card:
            entry = today_entries.get(card.product_id)
            rows.append(
                {
                    "product": card.product,
                    "cost": card.cost_price,
                    "price": card.selling_price,
                    "opening": openings.get(card.product_id, Decimal("0.00")),
                    "entry": entry,
                    "additional_input": _input_value(
                        entry.additional_stock if entry else None
                    ),
                    "total_input": _input_value(entry.total_stock if entry else None),
                    "closing_input": _input_value(
                        entry.closing_stock if entry else None
                    ),
                    "sold": entry.units_sold if entry else None,
                    "amount": entry.total_revenue if entry else None,
                }
            )

        day = DailySale.objects.filter(shop=shop, sale_date=today).aggregate(
            revenue=Sum("total_revenue"),
            cost=Sum("total_cost"),
            profit=Sum("profit"),
            units=Sum("units_sold"),
        )

        return {
            "shop": shop,
            "today": today,
            "openings": openings,
            "rows": rows,
            "has_products": bool(rows),
            "day_revenue": _money(day["revenue"]),
            "day_cost": _money(day["cost"]),
            "day_profit": _money(day["profit"]),
            "day_units": _money(day["units"]),
            "recorded_count": DailySale.objects.filter(shop=shop, sale_date=today).count(),
        }

    def get(self, request, *args, **kwargs):
        """Render the sheet, pre-filled with what has already been recorded."""
        return self.render_to_response(self._build_context())

    def post(self, request, *args, **kwargs):
        """Save every filled-in stock row, then show the day's running totals."""
        context = self._build_context()
        form = DailySalesSheetForm(
            request.POST,
            shop=self.shop,
            openings=context["openings"],
            day=context["today"],
        )
        if not form.is_valid():
            # Keep whatever the user typed so a rejected row can be fixed in place.
            for row in context["rows"]:
                key = row["product"].pk
                for name, prefix in (
                    ("additional_input", DailySalesSheetForm.ADDITIONAL_PREFIX),
                    ("total_input", DailySalesSheetForm.TOTAL_PREFIX),
                    ("closing_input", DailySalesSheetForm.CLOSING_PREFIX),
                ):
                    raw = request.POST.get(f"{prefix}{key}")
                    if raw is not None and str(raw).strip() != "":
                        row[name] = raw
            context["form_errors"] = form.non_field_errors()
            context["form_warnings"] = form.warnings
            return self.render_to_response(context, status=400)

        sales = form.save(request.user)
        for warning in form.warnings:
            messages.warning(request, warning)

        if not sales:
            messages.warning(
                request,
                "Nothing was entered. Fill in additional stock, total stock and "
                "closing stock for the products that changed, or leave a row blank.",
            )
            return redirect("daily_sales_sheet")

        units = sum((s.units_sold for s in sales), Decimal("0.00"))
        amount = sum((s.total_revenue for s in sales), Decimal("0.00"))
        # Profit is deliberately left out of the sheet and this flash: it is a
        # dashboard figure, shown to the manager on their dashboard.
        messages.success(
            request,
            f"Saved {len(sales)} product(s): {units.normalize():f} unit(s) sold, "
            f"amount {CURRENCY} {amount:,.2f}.",
        )
        return redirect("daily_sales_sheet")


class ShopProfitSheetView(LoginRequiredMixin, ManagerShopAccessMixin, TemplateView):
    """Record one shop's daily product quantities and expenses."""

    template_name = "tracker/shop_profit_sheet.html"

    def _selected_date(self, raw):
        return _parse_date(raw) or timezone.localdate()

    def _build_context(self, day, posted=None):
        posted = posted or {}
        cards = ShopProduct.objects.filter(
            shop=self.shop, is_active=True, cost_price__gt=0
        ).select_related("product").order_by("product__name", "product__category")
        manual_sales = {
            sale.product_id: sale
            for sale in DailySale.objects.filter(
                shop=self.shop, sale_date=day, is_profit_sheet_entry=True
            )
        }
        stock_products = set(
            DailySale.objects.filter(
                shop=self.shop, sale_date=day, total_stock__isnull=False
            ).values_list("product_id", flat=True)
        )
        rows = []
        for card in cards:
            entry = manual_sales.get(card.product_id)
            rows.append({
                "card": card,
                "entry": entry,
                "units": posted.get(f"units_{card.product_id}", _input_value(entry.units_sold if entry else None)),
                "stock_entry": card.product_id in stock_products,
            })

        sales_totals = DailySale.objects.filter(shop=self.shop, sale_date=day).aggregate(
            revenue=Sum("total_revenue"), cost=Sum("total_cost"), profit=Sum("profit"), units=Sum("units_sold")
        )
        expenses = list(ShopExpense.objects.filter(shop=self.shop, expense_date=day))
        expense_total = sum((expense.amount for expense in expenses), Decimal("0.00"))
        gross_profit = _money(sales_totals["profit"])
        return {
            "shop": self.shop,
            "selected_date": day.isoformat(),
            "rows": rows,
            "expenses": expenses,
            "expense_description": posted.get("description", ""),
            "expense_amount": posted.get("amount", ""),
            "day_revenue": _money(sales_totals["revenue"]),
            "day_cost": _money(sales_totals["cost"]),
            "day_units": _money(sales_totals["units"]),
            "gross_profit": gross_profit,
            "expense_total": expense_total,
            "net_profit": gross_profit - expense_total,
            "has_products": bool(rows),
        }

    def get(self, request, *args, **kwargs):
        day = self._selected_date(request.GET.get("date"))
        return self.render_to_response(self._build_context(day))

    def post(self, request, *args, **kwargs):
        day = self._selected_date(request.POST.get("date"))
        action = request.POST.get("action")
        redirect_url = f"{reverse('shop_profit_sheet', args=[self.shop.pk])}?date={day.isoformat()}"

        if action == "add_expense":
            description = request.POST.get("description", "").strip()
            try:
                amount = Decimal(request.POST.get("amount", "").strip())
            except (InvalidOperation, AttributeError):
                amount = None
            errors = []
            if not description:
                errors.append("Enter an expense description.")
            if (
                amount is None
                or not amount.is_finite()
                or amount <= 0
                or amount > Decimal("999999999999.99")
                or amount.as_tuple().exponent < -2
            ):
                errors.append("Enter an expense amount greater than zero with up to two decimal places.")
            if errors:
                context = self._build_context(day, request.POST)
                context["errors"] = errors
                return self.render_to_response(context, status=400)
            ShopExpense.objects.create(
                shop=self.shop,
                expense_date=day,
                description=description,
                amount=amount,
                recorded_by=request.user,
            )
            messages.success(request, "Expense added to this shop's profit sheet.")
            return redirect(redirect_url)

        if action == "delete_expense":
            try:
                expense_id = int(request.POST.get("expense_id", ""))
            except ValueError:
                expense_id = None
            if expense_id is not None:
                ShopExpense.objects.filter(
                    pk=expense_id, shop=self.shop, expense_date=day
                ).delete()
            messages.success(request, "Expense removed from this shop's profit sheet.")
            return redirect(redirect_url)

        cards = list(ShopProduct.objects.filter(
            shop=self.shop, is_active=True, cost_price__gt=0
        ).select_related("product"))
        existing_sales = {
            sale.product_id: sale
            for sale in DailySale.objects.filter(
                shop=self.shop, sale_date=day, is_profit_sheet_entry=True
            )
        }
        stock_products = set(
            DailySale.objects.filter(
                shop=self.shop, sale_date=day, total_stock__isnull=False
            ).values_list("product_id", flat=True)
        )
        updates = []
        errors = []
        for card in cards:
            key = f"units_{card.product_id}"
            raw = request.POST.get(key)
            if raw is None:
                continue
            raw = raw.strip()
            previous = existing_sales.get(card.product_id)
            if raw == "":
                continue
            try:
                units = Decimal(raw)
            except InvalidOperation:
                errors.append(f"Enter a valid quantity for {card.product}.")
                continue
            if (
                not units.is_finite()
                or units < 0
                or units > Decimal("9999999999.99")
                or units.as_tuple().exponent < -2
            ):
                errors.append(f"Quantity for {card.product} must be non-negative with up to two decimal places.")
                continue
            if card.product_id in stock_products:
                errors.append(f"{card.product} already has a stock-sheet entry for this date.")
                continue
            if previous is not None or units > 0:
                updates.append((card, previous, units))

        if errors:
            context = self._build_context(day, request.POST)
            context["errors"] = errors
            return self.render_to_response(context, status=400)

        with transaction.atomic():
            for card, sale, units in updates:
                if sale is None:
                    sale = DailySale(
                        shop=self.shop,
                        product=card.product,
                        sale_date=day,
                        units_sold=units,
                        unit_cost_at_sale=card.cost_price,
                        unit_price_at_sale=card.selling_price,
                        is_profit_sheet_entry=True,
                        recorded_by=request.user,
                    )
                else:
                    sale.units_sold = units
                sale.save()

        messages.success(request, "Product quantities saved. Profit totals include this shop's recorded expenses.")
        return redirect(redirect_url)


class ShopPricingView(OwnerRequiredMixin, View):
    """Owner-only inline editor for every shop's price card.

    Each shop prices its products independently, so ``GET ?shop=<pk>``
    narrows the page to a single shop's card; without the parameter every
    shop is shown. ``POST`` accepts ``shop_product-<pk>-<field>`` inputs and
    updates each row, so the owner can correct many prices in one submit.
    """

    template_name = "tracker/shop_pricing.html"

    def _current_shop(self, request):
        """Return the shop the editor is scoped to, or ``None`` for all."""
        raw = request.GET.get("shop") or request.POST.get("shop")
        if not raw:
            return None
        return get_object_or_404(Shop, pk=raw)

    def get(self, request, *args, **kwargs):
        """Render the price editor, one card per shop (or a single shop)."""
        shop = self._current_shop(request)
        grouped = {}
        shop_products = ShopProduct.objects.select_related("shop", "product").order_by(
            "shop__name", "product__name"
        )
        if shop is not None:
            shop_products = shop_products.filter(shop=shop)
        for sp in shop_products:
            grouped.setdefault(sp.shop, []).append(sp)

        return render(
            request,
            self.template_name,
            {
                "grouped_products": grouped,
                "shops": Shop.objects.all(),
                "current_shop": shop,
            },
        )

    def post(self, request, *args, **kwargs):
        """Apply the submitted prices, then redirect back with a summary."""
        shop = self._current_shop(request)
        updates = 0
        rows = ShopProduct.objects.select_related("shop", "product").all()
        if shop is not None:
            rows = rows.filter(shop=shop)
        for sp in rows:
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
        if shop is not None:
            return redirect(f"{reverse('shop_pricing')}?shop={shop.pk}")
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



class ProductComparisonView(OwnerRequiredMixin, TemplateView):
    """Compare two products side by side across all shops or one shop.

    The owner can search for Product A and Product B, optionally scope the
    comparison to one shop, and view totals plus a daily profit chart and
    per-shop breakdown. The legacy ``product_ids`` list is still supported so
    older links and CSV exports keep working.
    """

    template_name = "tracker/product_comparison.html"

    def _product_ids_from_request(self):
        """Return the selected product ids, preferring A/B selectors."""
        raw_ids = []
        for name in ("product_a", "product_b"):
            value = self.request.GET.get(name)
            if not value:
                continue
            value = value.strip()
            try:
                raw_ids.append(int(value))
                continue
            except ValueError:
                pass

            match = MasterProduct.objects.filter(name__iexact=value).first()
            if match is None and " — " in value:
                product_name, category = value.rsplit(" — ", 1)
                match = MasterProduct.objects.filter(
                    name__iexact=product_name,
                    category__iexact=category,
                ).first()
                if match is None:
                    match = MasterProduct.objects.filter(name__iexact=product_name).first()
            if match is None:
                match = MasterProduct.objects.filter(name__icontains=value).order_by("name").first()
            if match is not None:
                raw_ids.append(match.pk)
        if raw_ids:
            unique = []
            seen = set()
            for pid in raw_ids:
                if pid not in seen:
                    unique.append(pid)
                    seen.add(pid)
            return unique[:2]

        product_ids = self.request.GET.getlist("product_ids")
        try:
            ids = [int(pid) for pid in product_ids]
        except ValueError:
            return []
        return ids[:5]

    def _selected_shop(self):
        shop_id = self.request.GET.get("shop")
        if not shop_id:
            return None
        try:
            return Shop.objects.get(pk=int(shop_id))
        except (Shop.DoesNotExist, ValueError):
            return None

    def _selected_date_range(self, today):
        end_date = _parse_date(self.request.GET.get("end_date")) or today
        start_date = _parse_date(self.request.GET.get("start_date"))
        if start_date is None:
            start_date = end_date - timedelta(days=OWNER_TREND_DAYS - 1)
        if start_date > end_date:
            start_date = end_date - timedelta(days=OWNER_TREND_DAYS - 1)
        return start_date, end_date

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        start_date, end_date = self._selected_date_range(today)

        all_shops = list(Shop.objects.all().order_by("name"))
        selected_shop = self._selected_shop()
        shops_in_scope = [selected_shop] if selected_shop else all_shops

        context["shops"] = all_shops
        context["products"] = MasterProduct.objects.all().order_by("name", "category")
        context["selected_product_ids"] = []
        context["selected_product_a"] = ""
        context["selected_product_b"] = ""
        context["selected_product_a_name"] = ""
        context["selected_product_b_name"] = ""
        context["selected_shop"] = selected_shop
        context["scope_name"] = selected_shop.name if selected_shop else "All shops"
        context["start_date"] = start_date.isoformat()
        context["end_date"] = end_date.isoformat()
        context["comparison_data"] = []
        context["chart_data"] = None
        context["today"] = today
        context["max_products"] = 2

        product_ids = self._product_ids_from_request()
        if product_ids:
            valid = [
                pid for pid in product_ids
                if MasterProduct.objects.filter(pk=pid).exists()
            ]
            product_ids = valid[:2]
            context["selected_product_ids"] = product_ids
            if len(product_ids) > 0:
                product_a = MasterProduct.objects.filter(pk=product_ids[0]).first()
                context["selected_product_a"] = product_ids[0]
                context["selected_product_a_name"] = product_a.name if product_a else ""
            if len(product_ids) > 1:
                product_b = MasterProduct.objects.filter(pk=product_ids[1]).first()
                context["selected_product_b"] = product_ids[1]
                context["selected_product_b_name"] = product_b.name if product_b else ""

        if not product_ids:
            context["csv_export_url"] = ""
            return context

        sales = DailySale.objects.filter(product_id__in=product_ids).select_related("product", "shop")
        sales = sales.filter(sale_date__range=(start_date, end_date))
        if selected_shop is not None:
            sales = sales.filter(shop=selected_shop)

        shop_profit_data = sales.values(
            "product_id",
            "product__name",
            "product__category",
            "shop_id",
            "shop__name",
        ).annotate(
            profit=Sum("profit"),
            units=Sum("units_sold"),
            revenue=Sum("total_revenue"),
        )

        products_data = {}
        for row in shop_profit_data:
            pid = row["product_id"]
            if pid not in products_data:
                products_data[pid] = {
                    "product_id": pid,
                    "product_name": row["product__name"],
                    "product_category": row["product__category"],
                    "shop_cells": [],
                    "total_profit": Decimal("0.00"),
                    "total_units": Decimal("0.00"),
                    "total_revenue": Decimal("0.00"),
                }
            products_data[pid]["shop_cells"].append(
                {
                    "shop": Shop.objects.get(pk=row["shop_id"]),
                    "profit": _money(row["profit"]),
                    "units": _money(row["units"]),
                    "revenue": _money(row["revenue"]),
                }
            )

        for pid in product_ids:
            product = products_data.get(
                pid,
                {
                    "product_id": pid,
                    "product_name": MasterProduct.objects.get(pk=pid).name,
                    "product_category": MasterProduct.objects.get(pk=pid).category,
                    "shop_cells": [],
                    "total_profit": Decimal("0.00"),
                    "total_units": Decimal("0.00"),
                    "total_revenue": Decimal("0.00"),
                },
            )
            if not product["shop_cells"]:
                product["shop_cells"] = [
                    {"shop": shop, "profit": Decimal("0.00"), "units": Decimal("0.00"), "revenue": Decimal("0.00")}
                    for shop in shops_in_scope
                ]
            else:
                by_shop = {cell["shop"].pk: cell for cell in product["shop_cells"]}
                product["shop_cells"] = [
                    {
                        "shop": shop,
                        "profit": by_shop.get(shop.pk, {}).get("profit", Decimal("0.00")),
                        "units": by_shop.get(shop.pk, {}).get("units", Decimal("0.00")),
                        "revenue": by_shop.get(shop.pk, {}).get("revenue", Decimal("0.00")),
                    }
                    for shop in shops_in_scope
                ]
            product["total_profit"] = sum((cell["profit"] for cell in product["shop_cells"]), Decimal("0.00"))
            product["total_units"] = sum((cell["units"] for cell in product["shop_cells"]), Decimal("0.00"))
            product["total_revenue"] = sum((cell["revenue"] for cell in product["shop_cells"]), Decimal("0.00"))
            product["avg_profit_per_shop"] = (
                product["total_profit"] / len(product["shop_cells"])
                if product["shop_cells"]
                else Decimal("0.00")
            )
            products_data[pid] = product

        ordered_products = [products_data[pid] for pid in product_ids if pid in products_data]
        context["comparison_data"] = ordered_products
        context["totals"] = {
            "profit": sum((p["total_profit"] for p in ordered_products), Decimal("0.00")),
            "units": sum((p["total_units"] for p in ordered_products), Decimal("0.00")),
            "revenue": sum((p["total_revenue"] for p in ordered_products), Decimal("0.00")),
        }

        date_count = (end_date - start_date).days + 1
        trend_days = [start_date + timedelta(days=i) for i in range(date_count)]
        daily_profit = {
            (row["product_id"], row["sale_date"]): _money(row["profit"])
            for row in sales.values("product_id", "sale_date").annotate(profit=Sum("profit"))
        }
        datasets = []
        for product in ordered_products:
            datasets.append(
                {
                    "label": f"{product['product_name']} ({product['product_category'] or 'No category'})",
                    "data": [
                        float(daily_profit.get((product["product_id"], day), Decimal("0.00")))
                        for day in trend_days
                    ],
                }
            )
        context["chart_data"] = {
            "labels": [day.strftime("%d %b %Y") for day in trend_days],
            "datasets": datasets,
        }

        export_url = reverse("product_comparison_csv")
        params = urlencode(
            {
                "product_a": context["selected_product_a"],
                "product_b": context["selected_product_b"],
                "shop": selected_shop.pk if selected_shop else "",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            doseq=True,
        )
        if params:
            context["csv_export_url"] = f"{export_url}?{params}"
        else:
            context["csv_export_url"] = export_url
        return context


class ProductComparisonCSVView(OwnerRequiredMixin, View):
    """Download the comparison table for the selected products as a CSV."""

    def get(self, request, *args, **kwargs):
        from django.http import HttpResponseBadRequest

        product_ids = []
        for name in ("product_a", "product_b"):
            raw = request.GET.get(name)
            if not raw:
                continue
            try:
                product_ids.append(int(raw))
            except ValueError:
                return HttpResponseBadRequest("Invalid product id")

        if not product_ids:
            try:
                product_ids = [int(pid) for pid in request.GET.getlist("product_ids")][:5]
            except ValueError:
                return HttpResponseBadRequest("Invalid product id")

        if not product_ids:
            return HttpResponseBadRequest("Select at least one product")

        selected_shop_id = request.GET.get("shop")
        if selected_shop_id:
            try:
                selected_shop = Shop.objects.get(pk=int(selected_shop_id))
            except (Shop.DoesNotExist, ValueError):
                selected_shop = None
        else:
            selected_shop = None

        if selected_shop is not None:
            product_ids = list(dict.fromkeys(product_ids))[:2]

        today = timezone.localdate()
        end_date = _parse_date(request.GET.get("end_date")) or today
        start_date = _parse_date(request.GET.get("start_date"))
        if start_date is None:
            start_date = end_date - timedelta(days=OWNER_TREND_DAYS - 1)
        if start_date > end_date:
            start_date = end_date - timedelta(days=OWNER_TREND_DAYS - 1)

        shops = [selected_shop] if selected_shop else list(Shop.objects.all().order_by("name"))
        totals = {
            (row["product_id"], row["shop_id"]): row
            for row in DailySale.objects.filter(
                product_id__in=product_ids,
                shop_id__in=[shop.pk for shop in shops],
                sale_date__range=(start_date, end_date),
            ).values("product_id", "shop_id").annotate(
                units=Sum("units_sold"),
                revenue=Sum("total_revenue"),
                profit=Sum("profit"),
            )
        }

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="product-comparison-{start_date:%Y%m%d}-{end_date:%Y%m%d}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(["Product", "Category", "Shop", "From", "To", "Units", "Revenue", "Profit"])
        products = {
            product.pk: product
            for product in MasterProduct.objects.filter(pk__in=product_ids)
        }
        for product_id in product_ids:
            product = products.get(product_id)
            if product is None:
                continue
            for shop in shops:
                row = totals.get((product_id, shop.pk), {})
                writer.writerow([
                    product.name,
                    product.category,
                    shop.name,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    f"{_money(row.get('units')):.2f}",
                    f"{_money(row.get('revenue')):.2f}",
                    f"{_money(row.get('profit')):.2f}",
                ])
        return response


def _product_report_rows():
    """Return ``(shops, rows)`` for the all-products cross-shop report.

    One row per product, sorted by total profit so the best movers are on
    top. Each row carries an all-time profit/units/revenue cell per shop, in
    ``shops`` order, plus product totals. Products with no sales anywhere are
    included with zero cells, so the report also answers "which products
    have never moved?".
    """
    shops = list(Shop.objects.all().order_by("name"))
    aggregated = {
        (row["product_id"], row["shop_id"]): row
        for row in DailySale.objects.values("product_id", "shop_id").annotate(
            profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue")
        )
    }

    rows = []
    for product in MasterProduct.objects.all().order_by("name", "category"):
        shop_cells = []
        for shop in shops:
            row = aggregated.get((product.pk, shop.pk))
            shop_cells.append(
                {
                    "shop": shop,
                    "profit": _money(row["profit"]) if row else Decimal("0.00"),
                    "units": _money(row["units"]) if row else Decimal("0.00"),
                    "revenue": _money(row["revenue"]) if row else Decimal("0.00"),
                }
            )
        rows.append(
            {
                "product": product,
                "shop_cells": shop_cells,
                "total_profit": sum(
                    (cell["profit"] for cell in shop_cells), Decimal("0.00")
                ),
                "total_units": sum(
                    (cell["units"] for cell in shop_cells), Decimal("0.00")
                ),
                "total_revenue": sum(
                    (cell["revenue"] for cell in shop_cells), Decimal("0.00")
                ),
            }
        )
    rows.sort(key=lambda row: row["total_profit"], reverse=True)
    return shops, rows


class ProductReportView(OwnerRequiredMixin, TemplateView):
    """All-time report: every product, one profit column per shop.

    Complements :class:`ProductComparisonView`, which compares up to five
    products the owner picks. This page shows the whole catalogue at once,
    with per-shop profit cells, product totals and a shop totals footer, and
    a CSV export for the spreadsheet workflow.
    """

    template_name = "tracker/product_report.html"

    def get(self, request, *args, **kwargs):
        shop_id = request.GET.get("open_sheet_shop")
        if shop_id:
            shop = get_object_or_404(Shop, pk=shop_id)
            return redirect("shop_profit_sheet", shop_pk=shop.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        shops, rows = _product_report_rows()
        context["shops"] = shops
        context["rows"] = rows

        # Column totals for the table footer, aligned with ``shops``.
        shop_totals = []
        grand_total = Decimal("0.00")
        for index, shop in enumerate(shops):
            profit = sum(
                (row["shop_cells"][index]["profit"] for row in rows), Decimal("0.00")
            )
            shop_totals.append({"shop": shop, "profit": profit})
            grand_total += profit
        context["shop_totals"] = shop_totals
        context["grand_total"] = grand_total
        context["csv_export_url"] = reverse("product_report_csv")
        return context


class ProductReportCSVView(OwnerRequiredMixin, View):
    """Download the all-products cross-shop report as a CSV."""

    def get(self, request, *args, **kwargs):
        shops, rows = _product_report_rows()
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["Product", "Category"]
            + [f"{shop.name} profit" for shop in shops]
            + ["Total Profit", "Total Units", "Total Revenue"]
        )
        for row in rows:
            writer.writerow(
                [row["product"].name, row["product"].category]
                + [f"{cell['profit']:.2f}" for cell in row["shop_cells"]]
                + [
                    f"{row['total_profit']:.2f}",
                    f"{row['total_units']:.2f}",
                    f"{row['total_revenue']:.2f}",
                ]
            )

        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="product_report.csv"'
        return response


def _shop_report_data(shop):
    """Return per-day and per-product aggregates for one shop's report.

    Returns ``(day_rows, top_products, totals)``:

    * ``day_rows`` -- one row per trading day, newest first: profit, units
      and revenue for that date.
    * ``top_products`` -- the shop's best sellers ordered by units sold.
    * ``totals`` -- the shop's all-time profit, units and revenue.
    """
    sales = DailySale.objects.filter(shop=shop)

    day_rows = list(
        sales.values("sale_date")
        .annotate(profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue"))
        .order_by("-sale_date")
    )
    for row in day_rows:
        row["profit"] = _money(row["profit"])
        row["units"] = _money(row["units"])
        row["revenue"] = _money(row["revenue"])

    top_products = list(
        sales.values("product_id", "product__name", "product__category")
        .annotate(
            units=Sum("units_sold"),
            revenue=Sum("total_revenue"),
            profit=Sum("profit"),
        )
        .order_by("-units")[:SHOP_TOP_PRODUCTS]
    )
    for row in top_products:
        row["units"] = _money(row["units"])
        row["revenue"] = _money(row["revenue"])
        row["profit"] = _money(row["profit"])

    totals = sales.aggregate(profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue"))
    return day_rows, top_products, {
        "profit": _money(totals["profit"]),
        "units": _money(totals["units"]),
        "revenue": _money(totals["revenue"]),
    }


class ShopDetailView(OwnerRequiredMixin, DetailView):
    """One shop's full report: KPIs, most sold products, dated profits.

    Reached from the owner dashboard's shop cards. The page also carries an
    in-shop comparison: ``?compare=<pk>&compare=<pk>`` (up to four products
    the owner picks) shows those products side by side -- all-time totals
    plus a daily profit chart -- within this shop only.
    """

    model = Shop
    template_name = "tracker/shop_detail.html"
    context_object_name = "shop"

    #: Upper bound on products the in-shop comparison accepts.
    MAX_COMPARE = 4

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        shop = self.object
        today = timezone.localdate()
        week_start = today - timedelta(days=OWNER_TREND_DAYS - 1)
        sales = _shop_sales(shop)

        context["today"] = today
        context["week_start"] = week_start

        today_row = sales.filter(sale_date=today).aggregate(
            profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue")
        )
        context["today_profit"] = _money(today_row["profit"])
        context["today_units"] = _money(today_row["units"])
        context["today_revenue"] = _money(today_row["revenue"])

        week_row = sales.filter(sale_date__gte=week_start).aggregate(
            profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue")
        )
        context["week_profit"] = _money(week_row["profit"])
        context["week_units"] = _money(week_row["units"])
        context["week_revenue"] = _money(week_row["revenue"])

        day_rows, top_products, totals = _shop_report_data(shop)
        context["total_profit"] = totals["profit"]
        context["total_units"] = totals["units"]
        context["total_revenue"] = totals["revenue"]
        context["top_products"] = top_products
        context["day_count"] = len(day_rows)

        # Dated profit table, paginated so years of history stay usable.
        paginator = Paginator(day_rows, SHOP_DETAIL_PAGE_SIZE)
        page_number = self.request.GET.get("page", 1)
        page = paginator.get_page(page_number)
        context["page_obj"] = page
        context["day_rows"] = page.object_list
        context["is_paginated"] = paginator.num_pages > 1

        # The shop's price card feeds the comparison picker.
        context["shop_products"] = list(
            ShopProduct.objects.filter(shop=shop, is_active=True)
            .select_related("product")
            .order_by("product__name", "product__category")
        )

        # --- In-shop product comparison (up to 4 products) ---
        compare_ids = []
        for raw in self.request.GET.getlist("compare"):
            try:
                compare_ids.append(int(raw))
            except ValueError:
                continue
        compare_ids = compare_ids[: self.MAX_COMPARE]
        context["max_compare"] = self.MAX_COMPARE
        context["compare_ids"] = compare_ids

        if compare_ids:
            available = {sp.product_id for sp in context["shop_products"]}
            compare_ids = [pk for pk in compare_ids if pk in available]
            context["compare_ids"] = compare_ids
        if compare_ids:
            compare_rows = list(
                sales.filter(product_id__in=compare_ids)
                .values("product_id", "product__name", "product__category")
                .annotate(
                    units=Sum("units_sold"),
                    revenue=Sum("total_revenue"),
                    profit=Sum("profit"),
                )
            )
            by_id = {row["product_id"]: row for row in compare_rows}

            chart_start = today - timedelta(days=OWNER_CHART_DAYS - 1)
            chart_days = [chart_start + timedelta(days=i) for i in range(OWNER_CHART_DAYS)]
            per_day = {
                (row["product_id"], row["sale_date"]): _money(row["profit"])
                for row in sales.filter(product_id__in=compare_ids, sale_date__gte=chart_start)
                .values("product_id", "sale_date")
                .annotate(profit=Sum("profit"))
            }

            products = []
            for pk in compare_ids:
                row = by_id.get(pk)
                if row is None:
                    continue
                products.append(
                    {
                        "product_id": pk,
                        "name": row["product__name"],
                        "category": row["product__category"],
                        "units": _money(row["units"]),
                        "revenue": _money(row["revenue"]),
                        "profit": _money(row["profit"]),
                    }
                )
            context["comparison_data"] = products
            context["comparison_chart"] = {
                "labels": [day.strftime("%d %b") for day in chart_days],
                "datasets": [
                    {
                        "label": (
                            f"{p['name']} · {p['category']}" if p["category"] else p["name"]
                        ),
                        "data": [
                            float(per_day.get((p["product_id"], day), Decimal("0.00")))
                            for day in chart_days
                        ],
                    }
                    for p in products
                ],
            }
        else:
            context["comparison_data"] = []
            context["comparison_chart"] = None

        context["csv_export_url"] = reverse("shop_report_csv", args=[shop.pk])
        context["pricing_url"] = f"{reverse('shop_pricing')}?shop={shop.pk}"
        return context


class ShopReportCSVView(OwnerRequiredMixin, View):
    """Download one shop's dated profit report and product totals as CSV."""

    def get(self, request, pk=None, *args, **kwargs):
        shop_pk = kwargs.get("shop_pk", pk)
        if shop_pk is None:
            raise Http404("Shop not found.")
        shop = get_object_or_404(Shop, pk=shop_pk)
        day_rows, top_products, totals = _shop_report_data(shop)

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([f"{shop.name} — profit report"])
        writer.writerow(["Section", "Product", "Category", "Date", "Units", "Revenue", "Profit"])
        for row in top_products:
            writer.writerow(
                [
                    "Most sold",
                    row["product__name"],
                    row["product__category"],
                    "",
                    f"{row['units']:.2f}",
                    f"{row['revenue']:.2f}",
                    f"{row['profit']:.2f}",
                ]
            )
        for row in day_rows:
            writer.writerow(
                [
                    "Daily totals",
                    "",
                    "",
                    row["sale_date"],
                    f"{row['units']:.2f}",
                    f"{row['revenue']:.2f}",
                    f"{row['profit']:.2f}",
                ]
            )
        writer.writerow(
            ["Shop totals", "", "", "", f"{totals['units']:.2f}",
             f"{totals['revenue']:.2f}", f"{totals['profit']:.2f}"]
        )

        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="shop_report_{shop.name.lower().replace(" ", "_")}.csv"'
        )
        return response
