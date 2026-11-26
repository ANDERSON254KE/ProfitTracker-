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
from io import BytesIO, StringIO
from urllib.parse import urlencode
import csv

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView as AuthLoginView
from django.contrib.auth.views import LogoutView as AuthLogoutView
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, ProtectedError, Q, Sum
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.generic import CreateView, DetailView, RedirectView, TemplateView, View
from openpyxl import Workbook
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False
from .forms import (
    DailySaleForm,
    DailySalesSheetForm,
    ShopDailyCashForm,
    ShopPricingForm,
    StockAdditionForm,
    WorkerProfileForm,
)
from .mixins import (
    LoginRequiredMixin,
    ManagerRequiredMixin,
    ManagerShopAccessMixin,
    OwnerRequiredMixin,
)
from .models import (
    MAX_WORKERS_PER_SHOP,
    CustomUser,
    DailySale,
    MasterProduct,
    Shop,
    ShopDailyCash,
    ShopDailyProfit,
    ShopExpense,
    ShopProduct,
    StockAddition,
)
from .price_list import price_list_order, price_list_rank

#: Number of days shown in the owner's "recent days" table.
OWNER_TREND_DAYS = 7
#: Days spanned by the owner's dashboard profit-trend chart.
OWNER_CHART_DAYS = 30
#: Products listed in the owner's "top products" chart.
OWNER_TOP_PRODUCTS = 10
#: Number of recent sale lines shown on a manager's dashboard.
MANAGER_HISTORY_ROWS = 15
#: Rows per page in a shop report's dated profit table.
SHOP_DETAIL_PAGE_SIZE = 30
#: Products in a shop's "most sold" report.
SHOP_TOP_PRODUCTS = 10
#: Daily rows rendered in the product report preview before downloading.
REPORT_PREVIEW_ROWS = 20
#: Kenyan shillings symbol used by the ``ksh`` template filter.
CURRENCY = "KSh"


def _money(value) -> Decimal:
    """Coerce an aggregate (``Decimal``, ``None`` or ``int``) to a Decimal."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value))


def _sync_shop_manager(shop):
    """Keep the convenience ``Shop.manager`` link on an active worker."""
    if shop is None:
        return
    candidate = (
        CustomUser.objects.filter(
            role=CustomUser.Role.MANAGER, assigned_shop=shop, is_active=True
        )
        .order_by("id")
        .first()
    )
    candidate_id = candidate.pk if candidate else None
    if shop.manager_id != candidate_id:
        shop.manager = candidate
        shop.save(update_fields=["manager"])


def _worker_directory():
    """Group managers by shop for the owner's profile area."""
    workers = (
        CustomUser.objects.filter(role=CustomUser.Role.MANAGER)
        .select_related("assigned_shop")
        .order_by("assigned_shop__name", "username")
    )
    grouped = {}
    unassigned = []
    for worker in workers:
        if worker.assigned_shop_id:
            grouped.setdefault(worker.assigned_shop_id, []).append(worker)
        else:
            unassigned.append(worker)

    rows = []
    for shop in Shop.objects.all():
        shop_workers = grouped.get(shop.pk, [])
        active_count = sum(1 for worker in shop_workers if worker.is_active)
        rows.append(
            {
                "shop": shop,
                "workers": shop_workers,
                "active_count": active_count,
                "full": active_count >= MAX_WORKERS_PER_SHOP,
            }
        )
    return rows, unassigned


def _save_shop_daily_profit(shop, day, user, create=True):
    existing = ShopDailyProfit.objects.filter(shop=shop, profit_date=day).first()
    if existing is None and not create:
        return None

    sales = DailySale.objects.filter(shop=shop, sale_date=day).aggregate(
        revenue=Sum("total_revenue"), cost=Sum("total_cost"), profit=Sum("profit")
    )
    expenses = ShopExpense.objects.filter(shop=shop, expense_date=day).aggregate(
        amount=Sum("amount")
    )
    gross_profit = _money(sales["profit"])
    expense_total = _money(expenses["amount"])
    record, _ = ShopDailyProfit.objects.update_or_create(
        shop=shop,
        profit_date=day,
        defaults={
            "sales_revenue": _money(sales["revenue"]),
            "product_cost": _money(sales["cost"]),
            "gross_profit": gross_profit,
            "expense_total": expense_total,
            "net_profit": gross_profit - expense_total,
            "calculated_by": user,
        },
    )
    return record


def _parse_date(value):
    """Parse an ISO date query value, returning ``None`` for invalid input."""
    try:
        return parse_date(value or "")
    except (TypeError, ValueError):
        return None


def _parse_int_values(values, limit=None):
    """Return unique positive integer ids from repeated query values."""
    ids = []
    for raw in values:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids[:limit] if limit is not None else ids


def _csv_row(values):
    """Render one CSV line without holding the whole file in memory."""
    buffer = StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(values)
    return buffer.getvalue()


def _shop_sales(shop):
    """Return the DailySale queryset for one shop (used by both dashboards)."""
    return DailySale.objects.filter(shop=shop).select_related("product", "recorded_by")


def _display_name(first_name, last_name, username) -> str:
    """Return a person's name for display, falling back to the username."""
    full = f"{first_name or ''} {last_name or ''}".strip()
    return full or username or ""


def _input_value(value) -> str:
    """Render a stock figure for an HTML input: '' when there is no figure yet."""
    if value is None:
        return ""
    return str(value)


def _parse_money_input(raw, label):
    """Parse one non-negative money input, returning ``(value, error)``."""
    text = str(raw or "").strip()
    if not text:
        return Decimal("0.00"), None
    try:
        candidate = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        candidate = None
    if (
        candidate is None
        or not candidate.is_finite()
        or candidate < 0
        or candidate > Decimal("999999999999.99")
        or candidate.as_tuple().exponent < -2
    ):
        return None, (
            f"{label}: enter a non-negative amount with up to two decimal places."
        )
    return candidate, None


def _daily_sheet_openings(shop, day):
    """Return ``product_id -> last known closing stock`` before ``day``."""
    openings = {}
    history = (
        DailySale.objects.filter(
            shop=shop, sale_date__lt=day, closing_stock__isnull=False
        )
        .order_by("product_id", "-sale_date", "-id")
        .values_list("product_id", "closing_stock")
    )
    for product_id, closing in history:
        openings.setdefault(product_id, closing)
    return openings


def build_daily_sheet_context(shop):
    """Build the context for the daily stock sheet of one shop and today.

    Shared by the sheet page and the manager's dashboard, where the same form
    is shown first so the manager can fill the day in without navigating away.
    """
    today = timezone.localdate()

    price_card = list(
        ShopProduct.objects.filter(shop=shop, is_active=True)
        .select_related("product")
        .order_by("product__name", "product__category")
    )

    rank = price_list_rank()
    price_card.sort(
        key=lambda card: (
            rank.get((card.product.name, card.product.category), len(rank)),
            card.product.name,
            card.product.category,
        )
    )

    today_entries = {
        sale.product_id: sale
        for sale in DailySale.objects.filter(
            shop=shop, sale_date=today, total_stock__isnull=False
        )
    }
    openings = _daily_sheet_openings(shop, today)

    rows = []
    for card in price_card:
        entry = today_entries.get(card.product_id)
        rows.append(
            {
                "product": card.product,
                "section": card.product.category.strip() or "Uncategorised",
                "cost": card.cost_price,
                "price": card.selling_price,
                "opening": openings.get(card.product_id, Decimal("0.00")),
                "opening_input": _input_value(
                    entry.opening_stock
                    if entry and entry.opening_stock is not None
                    else openings.get(card.product_id, Decimal("0.00"))
                ),
                "entry": entry,
                "additional_input": _input_value(
                    entry.additional_stock if entry else None
                ),
                "total_input": _input_value(entry.total_stock if entry else None),
                "closing_input": _input_value(
                    entry.closing_stock if entry else None
                ),
                "note_input": entry.stock_note if entry else "",
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

    cash_record = ShopDailyCash.objects.filter(shop=shop, cash_date=today).first()
    mpesa = cash_record.mpesa if cash_record else Decimal("0.00")
    bill_out = cash_record.bill_out if cash_record else Decimal("0.00")
    bill_paid = cash_record.bill_paid if cash_record else Decimal("0.00")
    total_sales = _money(day["revenue"])

    expense_rows = [
        {"description": row["description"], "amount": _input_value(row["amount"])}
        for row in ShopExpense.objects.filter(shop=shop, expense_date=today)
        .order_by("pk")
        .values("description", "amount")
    ]
    expense_total = sum(
        (Decimal(str(row["amount"])) for row in expense_rows), Decimal("0.00")
    )
    cash = total_sales - mpesa
    balance = cash - expense_total - bill_out + bill_paid

    section_counts = {}
    for row in rows:
        section_counts[row["section"]] = section_counts.get(row["section"], 0) + 1

    return {
        "shop": shop,
        "today": today,
        "openings": openings,
        "rows": rows,
        "sections": [
            {"name": name, "count": count}
            for name, count in section_counts.items()
        ],
        "has_products": bool(rows),
        "day_revenue": _money(day["revenue"]),
        "day_cost": _money(day["cost"]),
        "day_profit": _money(day["profit"]),
        "day_units": _money(day["units"]),
        "recorded_count": DailySale.objects.filter(shop=shop, sale_date=today).count(),
        "cash_record": cash_record,
        "cash_mpesa": str(mpesa) if cash_record else "",
        "cash_bill_out": str(bill_out) if cash_record else "",
        "cash_bill_paid": str(bill_paid) if cash_record else "",
        "cash_expenses": (
            format(expense_total.normalize(), "f") if expense_total else ""
        ),
        "expense_rows": expense_rows,
        "cash_total_sales": total_sales,
        "cash_cash": cash,
        "cash_expense_total": expense_total,
        "cash_balance": balance.quantize(Decimal("0.01")),
    }


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
        worker_rows, unassigned_workers = _worker_directory()
        context["worker_rows"] = worker_rows
        context["unassigned_workers"] = unassigned_workers
        context["max_workers"] = MAX_WORKERS_PER_SHOP

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
        context["today_sales"] = list(
            sales.filter(sale_date=today)
            .select_related("shop")
            .order_by("shop__name", "product__name")
        )

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


class WorkerProfileView(OwnerRequiredMixin, TemplateView):
    """Owner-only directory for adding shop managers."""

    template_name = "tracker/worker_profiles.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        worker_rows, unassigned_workers = _worker_directory()
        context["worker_rows"] = worker_rows
        context["unassigned_workers"] = unassigned_workers
        context["max_workers"] = MAX_WORKERS_PER_SHOP
        context.setdefault("form", WorkerProfileForm())
        return context

    def post(self, request, *args, **kwargs):
        form = WorkerProfileForm(request.POST)
        if form.is_valid():
            worker = form.save()
            _sync_shop_manager(worker.assigned_shop)
            messages.success(
                request,
                f"Added {worker.get_full_name() or worker.username} "
                f"to {worker.assigned_shop.name}.",
            )
            return redirect("worker_profiles")
        return self.render_to_response(self.get_context_data(form=form))


class WorkerProfileUpdateView(OwnerRequiredMixin, TemplateView):
    """Owner-only editor for one existing shop manager."""

    template_name = "tracker/worker_profile_form.html"

    def get_worker(self):
        return get_object_or_404(
            CustomUser, pk=self.kwargs["pk"], role=CustomUser.Role.MANAGER
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        worker = kwargs.get("worker") or self.get_worker()
        context["worker"] = worker
        context.setdefault("form", WorkerProfileForm(instance=worker))
        return context

    def post(self, request, *args, **kwargs):
        worker = self.get_worker()
        old_shop = worker.assigned_shop
        form = WorkerProfileForm(request.POST, instance=worker)
        if form.is_valid():
            worker = form.save()
            _sync_shop_manager(old_shop)
            _sync_shop_manager(worker.assigned_shop)
            messages.success(
                request,
                f"Saved {worker.get_full_name() or worker.username}'s profile.",
            )
            return redirect("worker_profiles")
        return self.render_to_response(
            self.get_context_data(form=form, worker=worker)
        )


class WorkerProfileDeleteView(OwnerRequiredMixin, View):
    """Delete a shop manager when no recorded activity still references them."""

    def post(self, request, *args, **kwargs):
        worker = get_object_or_404(
            CustomUser, pk=kwargs["pk"], role=CustomUser.Role.MANAGER
        )
        label = worker.get_full_name() or worker.username
        old_shop = worker.assigned_shop
        try:
            worker.delete()
        except ProtectedError:
            messages.error(
                request,
                f"Could not delete {label}: sales, expenses or stock entries are "
                "linked to this account. Deactivate them instead.",
            )
        else:
            _sync_shop_manager(old_shop)
            messages.success(request, f"Deleted {label}'s profile.")
        return redirect("worker_profiles")


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
            units=Sum("units_sold"), revenue=Sum("total_revenue")
        )
        context["today_units"] = _money(today_row["units"])
        context["today_revenue"] = _money(today_row["revenue"])

        lifetime = sales.aggregate(units=Sum("units_sold"))
        context["total_units"] = _money(lifetime["units"])

        context["recent_sales"] = sales.order_by("-sale_date", "-id")[:MANAGER_HISTORY_ROWS]
        context["sale_form_url"] = reverse("daily_sales_sheet")
        context.update(build_daily_sheet_context(shop))
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
        """Stamp the recorder, save, and confirm the recorded sale."""
        form.instance.recorded_by = self.request.user
        response = super().form_valid(form)
        sale = self.object
        _save_shop_daily_profit(self.shop, sale.sale_date, self.request.user, create=False)
        messages.success(
            self.request,
            f"Recorded {sale.units_sold} x {sale.product} at {self.shop.name}.",
        )
        return response

    def get_success_url(self) -> str:
        """Return to the manager's dashboard after saving."""
        return f"{reverse('manager_dashboard')}?recorded=1"


class DailySalesSheetView(ManagerRequiredMixin, ManagerShopAccessMixin, TemplateView):
    """Record a whole trading day for the manager's shop in one submit.

    ``GET`` lists the shop's products in the owner's price-list row order
    (250ml first, down to soda) with their opening stock -- the previous day's
    closing stock --, the selling price and boxes for additional stock and
    closing stock; total stock, sold units and the amount sold are derived
    server side. Products with no cost price are not shown: they cannot produce
    a profit, so the owner must price them first. ``sections`` groups the rows
    by category (250ml, 500ml, soda ...) for the sheet's filter chips.

    ``POST`` upserts one stock row per filled-in product for today: submitting
    again replaces today's figures instead of double-counting them.
    """

    template_name = "tracker/daily_sales_sheet.html"

    def _build_context(self):
        """Gather the sheet rows, openings and the day's recorded totals."""
        return build_daily_sheet_context(self.shop)

    def get(self, request, *args, **kwargs):
        """Render the sheet, pre-filled with what has already been recorded."""
        return self.render_to_response(self._build_context())

    def post(self, request, *args, **kwargs):
        """Save every filled-in stock row, then show the day's running totals."""
        today = timezone.localdate()
        form = DailySalesSheetForm(
            request.POST,
            shop=self.shop,
            openings=_daily_sheet_openings(self.shop, today),
            day=today,
        )
        cash_form = ShopDailyCashForm(request.POST)

        raw_expenses = request.POST.get("expenses")
        descriptions = request.POST.getlist("expense_description")
        amounts = request.POST.getlist("expense_amount")
        expense_errors = []
        expense_items = []
        if descriptions or amounts:
            for index in range(max(len(descriptions), len(amounts))):
                description = (
                    descriptions[index] if index < len(descriptions) else ""
                ).strip()
                raw_amount = amounts[index] if index < len(amounts) else ""
                if not description and not str(raw_amount).strip():
                    continue
                amount, error = _parse_money_input(raw_amount, f"Expense {index + 1}")
                if error:
                    expense_errors.append(error)
                else:
                    expense_items.append((description or "Expense", amount))
            expense_amount = sum(
                (amount for _, amount in expense_items), Decimal("0.00")
            )
        else:
            expense_amount, error = _parse_money_input(raw_expenses, "Expenses")
            if error:
                expense_errors.append(error)

        if not form.is_valid() or not cash_form.is_valid() or expense_errors:
            # Re-render with what the user typed so nothing has to be retyped.
            context = build_daily_sheet_context(self.shop)
            for row in context["rows"]:
                key = row["product"].pk
                for name, prefix in (
                    ("opening_input", DailySalesSheetForm.OPENING_PREFIX),
                    ("additional_input", DailySalesSheetForm.ADDITIONAL_PREFIX),
                    ("total_input", DailySalesSheetForm.TOTAL_PREFIX),
                    ("closing_input", DailySalesSheetForm.CLOSING_PREFIX),
                ):
                    raw = request.POST.get(f"{prefix}{key}")
                    if raw is not None and str(raw).strip() != "":
                        row[name] = raw
                note_raw = request.POST.get(f"{DailySalesSheetForm.NOTE_PREFIX}{key}")
                if note_raw is not None:
                    row["note_input"] = note_raw
            errors = list(form.non_field_errors())
            for field, field_errors in cash_form.errors.items():
                label = field.replace("_", " ").capitalize()
                errors.extend(f"{label}: {error}" for error in field_errors)
            errors.extend(expense_errors)

            if expense_items:
                context["expense_rows"] = [
                    {"description": description, "amount": _input_value(amount)}
                    for description, amount in expense_items
                ]
                raw_total = str(expense_amount)
            else:
                raw_total = raw_expenses
            if raw_total is not None:
                context["cash_expenses"] = raw_total
                raw = str(raw_total).strip()
                if raw:
                    try:
                        parsed = Decimal(raw)
                        if parsed.is_finite():
                            context["cash_expense_total"] = parsed
                            cash_now = context["cash_cash"] - parsed - (
                                cash_form.cleaned_data.get("bill_out") or Decimal("0.00")
                            ) + (cash_form.cleaned_data.get("bill_paid") or Decimal("0.00"))
                            context["cash_balance"] = cash_now.quantize(Decimal("0.01"))
                    except (InvalidOperation, ValueError):
                        pass
            context["form_errors"] = errors
            context["form_warnings"] = form.warnings
            return self.render_to_response(context, status=400)

        # One transaction for the whole sheet: cash, every expense and every
        # product row, so a slow submit never leaves a half-written day behind.
        with transaction.atomic():
            ShopDailyCash.objects.update_or_create(
                shop=self.shop,
                cash_date=today,
                defaults={
                    "mpesa": cash_form.cleaned_data["mpesa"] or Decimal("0.00"),
                    "bill_out": cash_form.cleaned_data["bill_out"] or Decimal("0.00"),
                    "bill_paid": cash_form.cleaned_data["bill_paid"] or Decimal("0.00"),
                    "recorded_by": request.user,
                },
            )

            if expense_items:
                ShopExpense.objects.filter(shop=self.shop, expense_date=today).delete()
                ShopExpense.objects.bulk_create(
                    [
                        ShopExpense(
                            shop=self.shop,
                            expense_date=today,
                            description=description,
                            amount=amount,
                            recorded_by=request.user,
                        )
                        for description, amount in expense_items
                    ]
                )
            elif raw_expenses is not None:
                existing_expenses = list(
                    ShopExpense.objects.filter(
                        shop=self.shop, expense_date=today
                    ).order_by("pk")
                )
                if existing_expenses:
                    first = existing_expenses[0]
                    first.amount = expense_amount
                    first.description = "Daily expenses"
                    first.save(update_fields=["amount", "description"])
                    for extra in existing_expenses[1:]:
                        extra.amount = Decimal("0.00")
                        extra.save(update_fields=["amount"])
                elif expense_amount > 0:
                    ShopExpense.objects.create(
                        shop=self.shop,
                        expense_date=today,
                        description="Daily expenses",
                        amount=expense_amount,
                        recorded_by=request.user,
                    )

            sales = form.save(request.user)

        if sales or raw_expenses is not None or expense_items:
            _save_shop_daily_profit(self.shop, today, request.user, create=False)
        for warning in form.warnings:
            messages.warning(request, warning)

        if not sales:
            messages.warning(
                request,
                "Cash figures saved, but no product rows were entered. Fill in "
                "additional stock and closing stock for the products that changed, "
                "or leave a row blank.",
            )
            return redirect("daily_sales_sheet")

        units = sum((s.units_sold for s in sales), Decimal("0.00"))
        amount = sum((s.total_revenue for s in sales), Decimal("0.00"))
        messages.success(
            request,
            f"Saved {len(sales)} product(s): {units.normalize():f} unit(s) sold, "
            f"amount {CURRENCY} {amount:,.2f}.",
        )
        return redirect("daily_sales_sheet")


class ShopProfitSheetView(OwnerRequiredMixin, ManagerShopAccessMixin, TemplateView):
    """Record one shop's daily product quantities and expenses."""

    template_name = "tracker/shop_profit_sheet.html"

    def _selected_date(self, raw):
        return _parse_date(raw) or timezone.localdate()

    def _save_daily_profit(self, day, user, create=True):
        return _save_shop_daily_profit(self.shop, day, user, create=create)

    def _expense_range(self, start_raw, end_raw, fallback_day):
        start = _parse_date(start_raw) or fallback_day
        end = _parse_date(end_raw) or fallback_day
        if start > end:
            start, end = end, start
        if (end - start).days + 1 > self.MAX_EXPENSE_RANGE_DAYS:
            return start, end, []
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        return start, end, days

    def _build_context(self, day, posted=None):
        posted = posted or {}
        expense_start = expense_end = day
        expense_dates = [day]
        cards = ShopProduct.objects.filter(
            shop=self.shop, is_active=True
        ).select_related("product").order_by("product__name", "product__category")
        # Every non-stock row for the day is prefilled, not only this sheet's
        # own entries: a legacy single sale for the same product and day would
        # otherwise stay on the books while the units typed here are added as a
        # second row.
        manual_sales = {
            sale.product_id: sale
            for sale in DailySale.objects.filter(
                shop=self.shop, sale_date=day, total_stock__isnull=True
            ).order_by("pk")
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

        product_order = price_list_rank()
        category_order = {}
        for name, category in price_list_order():
            if category and category not in category_order:
                category_order[category] = len(category_order)
        rows.sort(key=lambda row: (
            category_order.get(row["card"].product.category, len(category_order)),
            product_order.get(
                (row["card"].product.name, row["card"].product.category),
                len(product_order),
            ),
            row["card"].product.name.casefold(),
        ))

        category_groups = []
        for row in rows:
            category = row["card"].product.category or "Other / uncategorized"
            if not category_groups or category_groups[-1]["category"] != category:
                category_groups.append({"category": category, "rows": []})
            category_groups[-1]["rows"].append(row)

        sales_totals = DailySale.objects.filter(shop=self.shop, sale_date=day).aggregate(
            revenue=Sum("total_revenue"), cost=Sum("total_cost"), profit=Sum("profit"), units=Sum("units_sold")
        )
        expenses = list(ShopExpense.objects.filter(shop=self.shop, expense_date=day))
        expense_total = sum((expense.amount for expense in expenses), Decimal("0.00"))
        range_totals = {
            row["expense_date"]: _money(row["amount"])
            for row in ShopExpense.objects.filter(
                shop=self.shop, expense_date__range=(expense_start, expense_end)
            ).values("expense_date").annotate(amount=Sum("amount"))
        }
        range_gross_profits = {
            row["sale_date"]: _money(row["profit"])
            for row in DailySale.objects.filter(
                shop=self.shop, sale_date__range=(expense_start, expense_end)
            ).values("sale_date").annotate(profit=Sum("profit"))
        }
        saved_records = {
            record.profit_date: record
            for record in ShopDailyProfit.objects.filter(
                shop=self.shop, profit_date__range=(expense_start, expense_end)
            )
        }
        expense_range_rows = [
            self._expense_range_row(
                range_day, range_totals, range_gross_profits, saved_records, posted
            )
            for range_day in expense_dates
        ]
        saved_day = ShopDailyProfit.objects.filter(shop=self.shop, profit_date=day).first()
        gross_profit = _money(sales_totals["profit"])
        if saved_day is not None:
            gross_profit = saved_day.gross_profit
            expense_total = saved_day.expense_total
        return {
            "shop": self.shop,
            "selected_date": day.isoformat(),
            "rows": rows,
            "category_groups": category_groups,
            "expenses": expenses,
            "expense_start": expense_start.isoformat(),
            "expense_end": expense_end.isoformat(),
            "expense_range_rows": expense_range_rows,
            "expense_range_too_long": not expense_dates,
            "saved_daily_profit": saved_day,
            "expense_description": posted.get("description", ""),
            "expense_amount": posted.get("amount", ""),
            "day_revenue": saved_day.sales_revenue if saved_day else _money(sales_totals["revenue"]),
            "day_cost": saved_day.product_cost if saved_day else _money(sales_totals["cost"]),
            "day_units": _money(sales_totals["units"]),
            "gross_profit": gross_profit,
            "expense_total": expense_total,
            "net_profit": saved_day.net_profit if saved_day else gross_profit - expense_total,
            "has_products": bool(rows),
        }

    @staticmethod
    def _expense_range_row(day, range_totals, range_gross_profits, saved_records, posted):
        saved = saved_records.get(day)
        gross = saved.gross_profit if saved else range_gross_profits.get(day, Decimal("0.00"))
        expense_total = saved.expense_total if saved else range_totals.get(day, Decimal("0.00"))
        return {
            "date": day,
            "existing_total": expense_total,
            "amount": posted.get(f"expense_total_{day.isoformat()}", _input_value(expense_total)),
            "gross_profit": gross,
            "net_profit": saved.net_profit if saved else gross - expense_total,
            "calculated": saved is not None,
            "calculated_at": saved.calculated_at if saved else None,
        }

    def get(self, request, *args, **kwargs):
        day = self._selected_date(request.GET.get("date"))
        return self.render_to_response(self._build_context(day))

    def post(self, request, *args, **kwargs):
        day = self._selected_date(request.POST.get("date"))
        action = request.POST.get("action")
        redirect_url = f"{reverse('shop_profit_sheet', args=[self.shop.pk])}?date={day.isoformat()}"

        if action == "calculate_daily_profit":
            start = end = day
            dates = [day]
            errors = []

            entries = []
            for expense_day in dates:
                day_key = expense_day.isoformat()
                raw_amount = request.POST.get(f"expense_total_{day_key}", "").strip()
                if not raw_amount:
                    continue
                try:
                    amount = Decimal(raw_amount)
                except (InvalidOperation, TypeError, ValueError):
                    amount = None
                if (
                    amount is None
                    or not amount.is_finite()
                    or amount < 0
                    or amount > Decimal("999999999999.99")
                    or amount.as_tuple().exponent < -2
                ):
                    errors.append(f"Enter a valid non-negative expense total for {day_key}.")
                    continue
                entries.append((expense_day, amount))

            cards = list(ShopProduct.objects.filter(
                shop=self.shop, is_active=True
            ).select_related("product"))
            # Any non-stock row for the day is updated in place, so re-entering
            # units replaces the day's figure instead of adding a second row.
            existing_sales = {
                sale.product_id: sale
                for sale in DailySale.objects.filter(
                    shop=self.shop, sale_date=day, total_stock__isnull=True
                ).order_by("pk")
            }
            stock_products = set(
                DailySale.objects.filter(
                    shop=self.shop, sale_date=day, total_stock__isnull=False
                ).values_list("product_id", flat=True)
            )
            sales_updates = []
            sales_deletes = []
            for card in cards:
                raw_units = request.POST.get(f"units_{card.product_id}")
                if raw_units is None or not raw_units.strip():
                    continue
                try:
                    units = Decimal(raw_units.strip())
                except (InvalidOperation, TypeError, ValueError):
                    units = None
                if (
                    units is None
                    or not units.is_finite()
                    or units < 0
                    or units > Decimal("9999999999.99")
                    or units.as_tuple().exponent < -2
                ):
                    errors.append(f"Enter a valid non-negative quantity for {card.product}.")
                    continue
                if card.product_id in stock_products:
                    errors.append(f"{card.product} already has a stock-sheet entry for this date.")
                    continue
                previous = existing_sales.get(card.product_id)
                if units == 0:
                    if previous is not None:
                        sales_deletes.append(previous)
                    continue
                sales_updates.append((card, previous, units))

            if errors:
                context = self._build_context(day, request.POST)
                context["errors"] = errors
                return self.render_to_response(context, status=400)

            with transaction.atomic():
                for sale in sales_deletes:
                    sale.delete()
                for card, sale, units in sales_updates:
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
                        sale.is_profit_sheet_entry = True
                        sale.recorded_by = request.user
                    sale.save()

                for expense_day, amount in entries:
                    existing = list(
                        ShopExpense.objects.select_for_update()
                        .filter(shop=self.shop, expense_date=expense_day)
                        .order_by("pk")
                    )
                    if existing:
                        existing[0].amount = amount
                        existing[0].description = "Daily expenses"
                        existing[0].save(update_fields=["amount", "description"])
                        for extra in existing[1:]:
                            extra.amount = Decimal("0.00")
                            extra.save(update_fields=["amount"])
                    elif amount > 0:
                        ShopExpense.objects.create(
                            shop=self.shop,
                            expense_date=expense_day,
                            description="Daily expenses",
                            amount=amount,
                            recorded_by=request.user,
                        )
                dates_to_calculate = set(dates)
                dates_to_calculate.add(day)
                for expense_day in dates_to_calculate:
                    self._save_daily_profit(expense_day, request.user)
            if dates:
                messages.success(request, f"Saved sales and calculated profit for {len(dates_to_calculate)} date(s).")
            else:
                messages.warning(request, "No valid dates were available to calculate profit.")
            return redirect(
                redirect_url
            )


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
            self._save_daily_profit(day, request.user)
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
                self._save_daily_profit(day, request.user)
            messages.success(request, "Expense removed from this shop's profit sheet.")
            return redirect(redirect_url)

        cards = list(ShopProduct.objects.filter(
            shop=self.shop, is_active=True
        ).select_related("product"))
        # Any non-stock row for the day is updated in place, so re-entering
        # units replaces the day's figure instead of adding a second row.
        existing_sales = {
            sale.product_id: sale
            for sale in DailySale.objects.filter(
                shop=self.shop, sale_date=day, total_stock__isnull=True
            ).order_by("pk")
        }
        stock_products = set(
            DailySale.objects.filter(
                shop=self.shop, sale_date=day, total_stock__isnull=False
            ).values_list("product_id", flat=True)
        )
        updates = []
        deletes = []
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
            if units == 0:
                if previous is not None:
                    deletes.append(previous)
                continue
            updates.append((card, previous, units))

        if errors:
            context = self._build_context(day, request.POST)
            context["errors"] = errors
            return self.render_to_response(context, status=400)

        with transaction.atomic():
            for sale in deletes:
                sale.delete()
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
                    sale.is_profit_sheet_entry = True
                    sale.recorded_by = request.user
                sale.save()
            self._save_daily_profit(day, request.user, create=False)

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

        products = MasterProduct.objects.all().order_by("name", "category")

        categories = []
        for _, category in price_list_order():
            if category and category not in categories:
                categories.append(category)
        for category in MasterProduct.objects.exclude(category="").values_list(
            "category", flat=True
        ).distinct():
            if category not in categories:
                categories.append(category)

        return render(
            request,
            self.template_name,
            {
                "grouped_products": grouped,
                "shops": Shop.objects.all(),
                "current_shop": shop,
                "products": products,
                "categories": categories,
            },
        )

    def post(self, request, *args, **kwargs):
        """Apply the submitted prices, then redirect back with a summary."""
        shop = self._current_shop(request)

        # --- Product catalog edits (common sheet): add / rename items ---
        product_updates = 0
        # Add a new item
        new_name = (request.POST.get("new_product_name") or "").strip()
        new_category = (request.POST.get("new_product_category") or "").strip()
        if new_category == "__new__":
            new_category = (request.POST.get("new_category_name") or "").strip()
            if new_name and not new_category:
                messages.error(request, "Enter a name for the new category.")
                new_name = ""
        if new_name:
            try:
                with transaction.atomic():
                    product = MasterProduct.objects.create(
                        name=new_name, category=new_category, is_manual=True
                    )
                    ShopProduct.objects.bulk_create([
                        ShopProduct(
                            shop=shop,
                            product=product,
                            cost_price=Decimal("0.00"),
                            selling_price=Decimal("0.00"),
                            is_active=True,
                        )
                        for shop in Shop.objects.all()
                    ])
                messages.success(
                    request,
                    f"Added product '{new_name}' to every shop's sheet. Set its prices on each shop card.",
                )
                product_updates += 1
            except Exception:
                messages.error(
                    request,
                    f"Could not add '{new_name}' (maybe a duplicate name+category).",
                )
        # Rename / change description of existing items
        for mp in MasterProduct.objects.all():
            prefix = f"master-{mp.pk}-"
            name_raw = request.POST.get(prefix + "name")
            if name_raw is None:
                continue
            name_raw = name_raw.strip()
            cat_raw = (request.POST.get(prefix + "category") or "").strip()
            if not name_raw:
                messages.error(request, f"Product #{mp.pk}: name cannot be empty.")
                continue
            if name_raw == mp.name and cat_raw == mp.category:
                continue
            try:
                mp.name = name_raw
                mp.category = cat_raw
                mp.save(update_fields=["name", "category"])
                product_updates += 1
            except Exception:
                messages.error(
                    request,
                    f"Could not update '{mp.name}' (maybe a duplicate name+category).",
                )
        if product_updates:
            messages.success(request, f"Saved {product_updates} product change(s).")

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
    """Return the selling price of a product for the requesting shop."""

    def get(self, request, product_id, *args, **kwargs):
        """Return ``{selling_price}`` as JSON."""
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


def _product_report_rows(
    start_date=None, end_date=None, shop_ids=None, product_query=None,
    product_ids=None, shops=None,
):
    """Return ``(shops, rows)`` for products-by-shop within an optional date range."""
    shops = list(Shop.objects.all().order_by("name")) if shops is None else list(shops)
    if shop_ids is not None:
        shops = [shop for shop in shops if shop.pk in shop_ids]
    sales = DailySale.objects.order_by()
    if shops:
        sales = sales.filter(shop_id__in=[shop.pk for shop in shops])
    elif shop_ids:
        sales = sales.none()
    if start_date is not None and end_date is not None:
        sales = sales.filter(sale_date__range=(start_date, end_date))
    if product_ids:
        sales = sales.filter(product_id__in=product_ids)
    aggregated = {
        (row["product_id"], row["shop_id"]): row
        for row in sales.values("product_id", "shop_id").annotate(
            profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue")
        )
    }

    products = MasterProduct.objects.all().order_by("name", "category")
    if product_ids:
        products = products.filter(pk__in=product_ids)
    if product_query:
        products = products.filter(
            Q(name__icontains=product_query) | Q(category__icontains=product_query)
        )

    rows = []
    for product in products:
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


def _stock_batch_profit_rows(start_date, end_date, shop_ids=None):
    """Profit made since each stock addition, per product and shop.

    A "batch" is either one day's ``additional_stock`` on a shop's daily sheet
    or a :class:`~tracker.models.StockAddition` the owner entered by hand. For
    every batch the row reports how much stock was added, how many units of
    that product the shop has sold from that day up to today, day by day, what
    is left, and the profit those sales produced.

    Units and profit are taken from the ``DailySale`` rows dated on or after
    the addition, so a batch spanning several days is measured against the
    same locked-in prices the reports use. Sales made from stock that was
    already on the shelf count towards the batch too -- the records do not say
    which physical units left the shelf -- so rows for the same shop and
    product overlap; totals across rows are therefore not additive.

    Args:
        start_date: first stock-addition date to include.
        end_date: last stock-addition date to include.
        shop_ids: optional set of shop pks to restrict the report to.

    Returns:
        A list of row dicts, newest addition first. Each row's ``daily`` entry
        lists the units and profit sold on each day from the addition to today.
    """
    today = timezone.localdate()
    sheet_additions = (
        DailySale.objects.filter(
            sale_date__range=(start_date, end_date), additional_stock__gt=0
        )
        .select_related("shop", "product")
    )
    if shop_ids:
        sheet_additions = sheet_additions.filter(shop_id__in=list(shop_ids))
    manual_additions = StockAddition.objects.filter(
        added_on__range=(start_date, end_date)
    ).select_related("shop", "product")
    if shop_ids:
        manual_additions = manual_additions.filter(shop_id__in=list(shop_ids))

    additions = [
        {
            "date": sale.sale_date,
            "shop": sale.shop,
            "product": sale.product,
            "added": _money(sale.additional_stock),
            "source": "sheet",
            "pk": None,
        }
        for sale in sheet_additions
    ] + [
        {
            "date": addition.added_on,
            "shop": addition.shop,
            "product": addition.product,
            "added": _money(addition.quantity),
            "source": "manual",
            "pk": addition.pk,
        }
        for addition in manual_additions
    ]
    if not additions:
        return []
    additions.sort(
        key=lambda item: (
            item["date"],
            item["shop"].name,
            item["product"].name,
            item["product"].category,
            item["source"],
            item["pk"] or 0,
        ),
        reverse=True,
    )

    # One query for every sale from the earliest addition to today, then a
    # running total per shop/product so each row's "since" figures are a
    # subtraction rather than another query.
    first_day = min(item["date"] for item in additions)
    relevant_pairs = {(item["shop"].pk, item["product"].pk) for item in additions}
    pair_filter = Q()
    for shop_pk, product_pk in relevant_pairs:
        pair_filter |= Q(shop_id=shop_pk, product_id=product_pk)
    per_day = {}
    for row in (
        DailySale.objects.order_by()
        .filter(sale_date__range=(first_day, today))
        .filter(pair_filter)
        .values("shop_id", "product_id", "sale_date")
        .annotate(
            units=Sum("units_sold"),
            revenue=Sum("total_revenue"),
            profit=Sum("profit"),
        )
    ):
        per_day.setdefault((row["shop_id"], row["product_id"]), []).append(row)

    cumulative = {}
    daily_totals = {}
    for key, rows in per_day.items():
        rows.sort(key=lambda row: row["sale_date"])
        totals = []
        units = revenue = profit = Decimal("0.00")
        for row in rows:
            units += _money(row["units"])
            revenue += _money(row["revenue"])
            profit += _money(row["profit"])
            totals.append((row["sale_date"], units, revenue, profit))
            daily_totals.setdefault(key, []).append(
                (row["sale_date"], _money(row["units"]), _money(row["profit"]))
            )
        cumulative[key] = totals

    unit_profits = {
        (card.shop_id, card.product_id): card.selling_price - card.cost_price
        for card in ShopProduct.objects.filter(
            shop_id__in={item["shop"].pk for item in additions},
            product_id__in={item["product"].pk for item in additions},
        )
    }

    rows = []
    for item in additions:
        added = item["added"]
        key = (item["shop"].pk, item["product"].pk)
        totals = cumulative.get(key, [])
        if totals:
            _, units_to_date, revenue_to_date, profit_to_date = totals[-1]
        else:
            units_to_date = revenue_to_date = profit_to_date = Decimal("0.00")
        # Subtract everything sold before the addition: what remains is the
        # product's sales (and profit) from that day up to today.
        before = (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        for day, units, revenue, profit in totals:
            if day >= item["date"]:
                break
            before = (units, revenue, profit)
        daily = [
            {"date": day, "units": day_units, "profit": day_profit}
            for day, day_units, day_profit in daily_totals.get(key, [])
            if day >= item["date"] and day_units
        ]
        sold = units_to_date - before[0]
        revenue_to_date -= before[1]
        profit_to_date -= before[2]
        rows.append(
            {
                "date": item["date"],
                "shop": item["shop"],
                "product": item["product"],
                "added": added,
                "source": item["source"],
                "pk": item["pk"],
                "daily": daily,
                "sold": sold,
                "remaining": max(added - sold, Decimal("0.00")),
                "revenue": revenue_to_date,
                "profit": profit_to_date,
                "unit_profit": _money(
                    unit_profits.get((item["shop"].pk, item["product"].pk), Decimal("0.00"))
                ),
                "days": (today - item["date"]).days,
            }
        )
    return rows


def _all_shops_profit_date_range(request):
    """Return a valid date range, defaulting to all recorded history through today."""
    today = timezone.localdate()
    start_date = _parse_date(request.GET.get("start_date"))
    end_date = _parse_date(request.GET.get("end_date"))
    if start_date and end_date:
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date
    first_sale = DailySale.objects.order_by("sale_date").values_list("sale_date", flat=True).first()
    first_expense = ShopExpense.objects.order_by("expense_date").values_list("expense_date", flat=True).first()
    earliest = min((day for day in (first_sale, first_expense) if day), default=today)
    start_date = start_date or earliest
    end_date = end_date or today
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _report_request_filters(request):
    """Resolve the shared filters used by the report page and its downloads."""
    start_date, end_date = _all_shops_profit_date_range(request)
    all_shops = list(Shop.objects.all().order_by("name"))
    selected_shop_ids = _parse_int_values(request.GET.getlist("report_shops"))
    if not selected_shop_ids:
        selected_shop_ids = _parse_int_values([request.GET.get("report_shop")])
    selected_shop_ids = set(selected_shop_ids)
    shops = [shop for shop in all_shops if shop.pk in selected_shop_ids]
    product_ids = _parse_int_values(request.GET.getlist("products"))
    product_query = (request.GET.get("product") or "").strip()
    return {
        "start_date": start_date,
        "end_date": end_date,
        "all_shops": all_shops,
        "shops": shops,
        "shop_ids": {shop.pk for shop in shops},
        "product_ids": product_ids,
        "product_query": product_query,
    }


def _daily_sales_queryset(filters):
    """Return the filtered DailySale rows behind the daily sales report."""
    sales = DailySale.objects.order_by()
    if filters["start_date"] and filters["end_date"]:
        sales = sales.filter(
            sale_date__range=(filters["start_date"], filters["end_date"])
        )
    if filters["shop_ids"]:
        sales = sales.filter(shop_id__in=filters["shop_ids"])
    if filters["product_ids"]:
        sales = sales.filter(product_id__in=filters["product_ids"])
    if filters["product_query"]:
        sales = sales.filter(
            Q(product__name__icontains=filters["product_query"])
            | Q(product__category__icontains=filters["product_query"])
        )
    return sales


def _daily_sales_rows(sales):
    """Group filtered sales into one row per day, shop and product."""
    return (
        sales.annotate(
            shop_name=F("shop__name"),
            product_name=F("product__name"),
            product_category=F("product__category"),
        )
        .values(
            "sale_date",
            "shop_id",
            "shop_name",
            "product_id",
            "product_name",
            "product_category",
        )
        .annotate(
            units=Sum("units_sold"),
            revenue=Sum("total_revenue"),
            profit=Sum("profit"),
        )
        .order_by("-sale_date", "shop_name", "product_name", "product_category")
    )


def _daily_sales_report(filters):
    """Build a bounded preview plus totals from the filtered daily rows."""
    sales = _daily_sales_queryset(filters)
    grouped = _daily_sales_rows(sales)
    totals = sales.aggregate(
        units=Sum("units_sold"),
        revenue=Sum("total_revenue"),
        profit=Sum("profit"),
    )
    return {
        "rows": list(grouped[:REPORT_PREVIEW_ROWS]),
        "count": grouped.count(),
        "totals": {
            "units": _money(totals["units"]),
            "revenue": _money(totals["revenue"]),
            "profit": _money(totals["profit"]),
        },
    }


def _daily_sales_csv_response(filters):
    """Stream the full filtered daily report as CSV."""
    rows = _daily_sales_rows(_daily_sales_queryset(filters))

    def lines():
        yield _csv_row([
            "From", filters["start_date"].isoformat(),
            "To", filters["end_date"].isoformat(),
        ])
        yield _csv_row([
            "Date", "Shop", "Product", "Category",
            "Units Sold", "Revenue (KSh)", "Profit (KSh)",
        ])
        for row in rows.iterator(chunk_size=2000):
            yield _csv_row([
                row["sale_date"].isoformat(),
                row["shop_name"],
                row["product_name"],
                row["product_category"],
                f"{_money(row['units']):.2f}",
                f"{_money(row['revenue']):.2f}",
                f"{_money(row['profit']):.2f}",
            ])

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="daily_sales_{filters["start_date"]:%Y%m%d}_{filters["end_date"]:%Y%m%d}.csv"'
    )
    return response


def _daily_sales_workbook_response(filters):
    """Build one worksheet per date in the filtered range."""
    rows_by_date = {}
    for row in _daily_sales_rows(_daily_sales_queryset(filters)):
        rows_by_date.setdefault(row["sale_date"], []).append(row)

    workbook = Workbook(write_only=True)
    day = filters["start_date"]
    while day <= filters["end_date"]:
        sheet = workbook.create_sheet(day.isoformat())
        day_rows = rows_by_date.get(day, [])
        sheet.append([f"Date {day.isoformat()}"])
        sheet.append([
            "Product", "Category", "Shop",
            "Units Sold", "Revenue (KSh)", "Profit (KSh)",
        ])
        for row in day_rows:
            sheet.append([
                row["product_name"],
                row["product_category"],
                row["shop_name"],
                _money(row["units"]),
                _money(row["revenue"]),
                _money(row["profit"]),
            ])
        if day_rows:
            sheet.append([
                "Total", "", "",
                sum((_money(row["units"]) for row in day_rows), Decimal("0.00")),
                sum((_money(row["revenue"]) for row in day_rows), Decimal("0.00")),
                sum((_money(row["profit"]) for row in day_rows), Decimal("0.00")),
            ])
        else:
            sheet.append(["No sales recorded.", "", "", "", "", ""])
        day += timedelta(days=1)

    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="daily_sales_{filters["start_date"]:%Y%m%d}_{filters["end_date"]:%Y%m%d}.xlsx"'
    )
    return response


class ProductReportView(OwnerRequiredMixin, TemplateView):
    """Date-filtered report: every product, one profit column per shop.

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

    def _stock_redirect_url(self, request):
        """Return to the stock section with the filters the form was shown under."""
        params = request.GET.copy()
        for key in ("stock_start_date", "stock_end_date", "stock_shop"):
            value = (request.POST.get(key) or "").strip()
            if value:
                params[key] = value
            else:
                params.pop(key, None)
        query = params.urlencode()
        url = reverse("product_report")
        return f"{url}?{query}" if query else url

    def post(self, request, *args, **kwargs):
        """Record or remove a hand-entered stock addition.

        The daily sheet only knows a shop's total stock for a day; this is the
        escape hatch for deliveries that never made it onto a sheet, so the
        stock-batch section can still measure the sales that followed them.
        """
        removal_id = request.POST.get("delete_stock_addition")
        if removal_id:
            addition = get_object_or_404(StockAddition, pk=removal_id)
            label = f"{addition.quantity} × {addition.product} at {addition.shop}"
            addition.delete()
            messages.success(request, f"Removed manual stock addition: {label}.")
            return redirect(self._stock_redirect_url(request))

        form = StockAdditionForm(request.POST)
        if form.is_valid():
            addition = form.save(commit=False)
            addition.recorded_by = request.user
            addition.save()
            messages.success(
                request,
                f"Added {addition.quantity} × {addition.product} at {addition.shop} "
                f"on {addition.added_on}. Units sold and profit from that day are "
                "counted in the stock section below.",
            )
            return redirect(self._stock_redirect_url(request))

        context = self.get_context_data(**kwargs)
        context["stock_form"] = form
        return self.render_to_response(context, status=400)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = _report_request_filters(self.request)
        start_date = filters["start_date"]
        end_date = filters["end_date"]
        shops, rows = _product_report_rows(
            start_date,
            end_date,
            product_query=filters["product_query"],
            product_ids=filters["product_ids"] or None,
            shops=filters["shops"] or None,
        )
        daily_report = _daily_sales_report(filters)
        all_products = list(MasterProduct.objects.all().order_by("name", "category"))
        selected_products = [
            product for product in all_products
            if product.pk in filters["product_ids"]
        ]
        context["all_shops"] = filters["all_shops"]
        context["product_catalog"] = [
            {
                "id": product.pk,
                "name": product.name,
                "category": product.category or "",
                "search": f"{product.name} {product.category or ''}".lower(),
            }
            for product in all_products
        ]
        context["selected_shop_ids"] = filters["shop_ids"]
        context["selected_report_shops"] = filters["shops"]
        context["selected_product_ids"] = filters["product_ids"]
        context["selected_products"] = selected_products
        context["shops"] = shops
        context["rows"] = rows
        context["product_query"] = filters["product_query"]

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
        context["profit_start_date"] = start_date.isoformat()
        context["profit_end_date"] = end_date.isoformat()
        context["daily_preview"] = daily_report["rows"]
        context["daily_count"] = daily_report["count"]
        context["daily_totals"] = daily_report["totals"]
        context["report_preview_limit"] = REPORT_PREVIEW_ROWS

        # --- Stock-batch profit: sales since each stock addition, to today ---
        today = timezone.localdate()
        stock_start = _parse_date(self.request.GET.get("stock_start_date")) or today.replace(day=1)
        stock_end = _parse_date(self.request.GET.get("stock_end_date")) or today
        if stock_start > stock_end:
            stock_start, stock_end = stock_end, stock_start
        stock_shop_id = self.request.GET.get("stock_shop") or ""
        stock_shop_ids = None
        selected_stock_shop = None
        if stock_shop_id:
            try:
                selected_stock_shop = Shop.objects.get(pk=int(stock_shop_id))
                stock_shop_ids = {selected_stock_shop.pk}
            except (Shop.DoesNotExist, ValueError, TypeError):
                stock_shop_id = ""
        stock_rows = _stock_batch_profit_rows(
            stock_start, stock_end, shop_ids=stock_shop_ids
        )
        context["stock_rows"] = stock_rows
        stock_totals = {
            "added": sum((row["added"] for row in stock_rows), Decimal("0.00")),
            "sold": sum((row["sold"] for row in stock_rows), Decimal("0.00")),
            "remaining": sum(
                (row["remaining"] for row in stock_rows), Decimal("0.00")
            ),
            "revenue": sum((row["revenue"] for row in stock_rows), Decimal("0.00")),
            "profit": sum((row["profit"] for row in stock_rows), Decimal("0.00")),
        }
        stock_totals["unit_profit"] = (
            stock_totals["profit"] / stock_totals["sold"]
            if stock_totals["sold"]
            else Decimal("0.00")
        )
        context["stock_totals"] = stock_totals
        context["stock_start_date"] = stock_start.isoformat()
        context["stock_end_date"] = stock_end.isoformat()
        context["stock_today"] = today.isoformat()
        context["selected_stock_shop"] = selected_stock_shop
        context["stock_shop_filter"] = stock_shop_id
        context.setdefault(
            "stock_form",
            StockAdditionForm(
                initial={"shop": selected_stock_shop, "added_on": today}
            ),
        )
        return context


class ShopProfitCSVView(OwnerRequiredMixin, View):
    """Download a slim profit sheet: one row per shop with its net profit.

    Covers the selected date range and only the shops chosen in the report.
    """

    def get(self, request, *args, **kwargs):
        filters = _report_request_filters(request)
        start_date = filters["start_date"]
        end_date = filters["end_date"]
        sales_by_shop = {
            row["shop_id"]: _money(row["profit"])
            for row in DailySale.objects.order_by()
            .filter(sale_date__range=(start_date, end_date))
            .values("shop_id")
            .annotate(profit=Sum("profit"))
        }
        expenses_by_shop = {
            row["shop_id"]: _money(row["amount"])
            for row in ShopExpense.objects.order_by()
            .filter(expense_date__range=(start_date, end_date))
            .values("shop_id")
            .annotate(amount=Sum("amount"))
        }

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["From", start_date.isoformat(), "To", end_date.isoformat()])
        writer.writerow(["Shop", "Profit"])
        total = Decimal("0.00")
        for shop in filters["shops"]:
            net = (
                sales_by_shop.get(shop.pk, Decimal("0.00"))
                - expenses_by_shop.get(shop.pk, Decimal("0.00"))
            )
            writer.writerow([shop.name, f"{net:.2f}"])
            total += net
        writer.writerow(["Shops", f"{total:.2f}"])

        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="profit_sheet_{start_date:%Y%m%d}_{end_date:%Y%m%d}.csv"'
        )
        return response


class ProductReportCSVView(OwnerRequiredMixin, View):
    """Stream the previewed daily sales and profits as CSV."""

    def get(self, request, *args, **kwargs):
        return _daily_sales_csv_response(_report_request_filters(request))


class AllShopsProfitWorkbookView(OwnerRequiredMixin, View):
    """Download the previewed daily sales and profits as an XLSX workbook."""

    def get(self, request, *args, **kwargs):
        return _daily_sales_workbook_response(_report_request_filters(request))


def _shop_report_data(shop):
    """Return per-day and per-product aggregates for one shop's report.

    Returns ``(day_rows, top_products, totals)``:

    * ``day_rows`` -- one row per trading day, newest first: profit, units,
      revenue, that day's expenses and net profit, plus the people who
      recorded something at the shop that day (``staff``).
    * ``top_products`` -- the shop's best sellers ordered by units sold.
    * ``totals`` -- the shop's all-time profit, units and revenue.
    """
    sales = DailySale.objects.filter(shop=shop).order_by()

    day_rows = list(
        sales.values("sale_date")
        .annotate(profit=Sum("profit"), units=Sum("units_sold"), revenue=Sum("total_revenue"))
        .order_by("-sale_date")
    )

    # Who was at work each day: everyone who recorded a sale at the shop.
    # There is no attendance log, so the recorder on each row is the proxy.
    staff_by_day = {}
    for person in sales.values(
        "sale_date",
        "recorded_by__first_name",
        "recorded_by__last_name",
        "recorded_by__username",
    ).distinct():
        staff_by_day.setdefault(person["sale_date"], set()).add(
            _display_name(
                person["recorded_by__first_name"],
                person["recorded_by__last_name"],
                person["recorded_by__username"],
            )
        )

    expenses_by_day = {
        row["expense_date"]: _money(row["total"])
        for row in ShopExpense.objects.filter(shop=shop)
        .order_by()
        .values("expense_date")
        .annotate(total=Sum("amount"))
    }
    for row in day_rows:
        row["profit"] = _money(row["profit"])
        row["units"] = _money(row["units"])
        row["revenue"] = _money(row["revenue"])
        row["expenses"] = expenses_by_day.get(row["sale_date"], Decimal("0.00"))
        row["net"] = (row["profit"] - row["expenses"]).quantize(Decimal("0.01"))
        row["staff"] = ", ".join(sorted(staff_by_day.get(row["sale_date"], ())))

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
    """One shop's full report: KPIs, a dated daily sheet, dated profits.

    Reached from the owner dashboard's shop cards. ``?date=YYYY-MM-DD`` picks
    the day whose sheet is shown (today by default); that day's products,
    staff, expenses and cash figures are all scoped to it. The page also
    carries an in-shop comparison: ``?compare=<pk>&compare=<pk>`` (up to four
    products the owner picks) shows those products side by side -- all-time
    totals plus a daily profit chart -- within this shop only.
    """

    model = Shop
    template_name = "tracker/shop_detail.html"
    context_object_name = "shop"

    #: Upper bound on products the in-shop comparison accepts.
    MAX_COMPARE = 5

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

        # --- The shop's daily sheet for one date (today unless ?date= given) ---
        selected_date = _parse_date(self.request.GET.get("date")) or today
        day_sales = list(sales.filter(sale_date=selected_date).select_related("product"))
        rank = price_list_rank()
        day_sales.sort(
            key=lambda sale: (
                rank.get((sale.product.name, sale.product.category), len(rank)),
                sale.product.name,
            )
        )
        day_totals = sales.filter(sale_date=selected_date).aggregate(
            units=Sum("units_sold"), revenue=Sum("total_revenue"), profit=Sum("profit")
        )
        context["selected_date"] = selected_date
        context["is_today"] = selected_date == today
        context["day_sales"] = day_sales
        context["day_totals"] = {
            "units": _money(day_totals["units"]),
            "revenue": _money(day_totals["revenue"]),
            "profit": _money(day_totals["profit"]),
        }
        context["prev_date"] = selected_date - timedelta(days=1)
        context["next_date"] = (
            selected_date + timedelta(days=1) if selected_date < today else None
        )

        # The shop's price card feeds the comparison picker.
        context["shop_products"] = list(
            ShopProduct.objects.filter(shop=shop, is_active=True)
            .select_related("product")
            .order_by("product__name", "product__category")
        )

        # --- In-shop product comparison (up to 5 products, on one chosen day) ---
        compare_date = _parse_date(self.request.GET.get("compare_date")) or today
        context["compare_date"] = compare_date
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
            # Figures are for the chosen day, not all time, so the owner can
            # compare products on today or any past date.
            compare_rows = list(
                sales.filter(product_id__in=compare_ids, sale_date=compare_date)
                .values("product_id", "product__name", "product__category")
                .annotate(
                    units=Sum("units_sold"),
                    revenue=Sum("total_revenue"),
                    profit=Sum("profit"),
                )
            )
            by_id = {row["product_id"]: row for row in compare_rows}

            chart_start = compare_date - timedelta(days=OWNER_CHART_DAYS - 1)
            chart_days = [chart_start + timedelta(days=i) for i in range(OWNER_CHART_DAYS)]
            per_day = {
                (row["product_id"], row["sale_date"]): _money(row["profit"])
                for row in sales.filter(
                    product_id__in=compare_ids,
                    sale_date__gte=chart_start,
                    sale_date__lte=compare_date,
                )
                .values("product_id", "sale_date")
                .annotate(profit=Sum("profit"))
            }

            # Look up the product's name/category from the shop's price card, so
            # a date with no sales still lists the product with zero figures.
            name_by_id = {
                sp.product_id: (sp.product.name, sp.product.category)
                for sp in context["shop_products"]
            }
            products = []
            for pk in compare_ids:
                row = by_id.get(pk, {})
                name, category = name_by_id.get(pk, ("", ""))
                products.append(
                    {
                        "product_id": pk,
                        "name": name,
                        "category": category,
                        "units": _money(row.get("units")),
                        "revenue": _money(row.get("revenue")),
                        "profit": _money(row.get("profit")),
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

        # --- The cash sheet for the selected day: M-Pesa, bills, balance ---
        cash_record = ShopDailyCash.objects.filter(
            shop=shop, cash_date=selected_date
        ).first()
        day_expenses = list(
            ShopExpense.objects.filter(shop=shop, expense_date=selected_date).order_by(
                "created_at"
            )
        )
        expense_today = sum(
            (expense.amount for expense in day_expenses), Decimal("0.00")
        )
        mpesa = cash_record.mpesa if cash_record else Decimal("0.00")
        bill_out = cash_record.bill_out if cash_record else Decimal("0.00")
        bill_paid = cash_record.bill_paid if cash_record else Decimal("0.00")

        # Total sales come from the products recorded on the sheet that day. The
        # M-Pesa figure splits off its share, the rest is cash, and the day's
        # expenses and bills come out of what is left.
        total_sales = context["day_totals"]["revenue"]
        cash = total_sales - mpesa
        context["cash_form"] = ShopDailyCashForm(instance=cash_record)
        context["today_expenses"] = day_expenses
        context["daily_cards"] = {
            "sales": total_sales,
            "expenses": expense_today,
            "profit": context["day_totals"]["profit"] - expense_today,
        }
        context["cash_summary"] = {
            "total_sales": total_sales,
            "mpesa": mpesa,
            "cash": cash,
            "expenses": expense_today,
            "bill_out": bill_out,
            "bill_paid": bill_paid,
            "balance": (cash + bill_paid - expense_today - bill_out).quantize(
                Decimal("0.01")
            ),
        }

        context["csv_export_url"] = reverse("shop_report_csv", args=[shop.pk])
        context["pricing_url"] = f"{reverse('shop_pricing')}?shop={shop.pk}"
        return context

    def post(self, request, *args, **kwargs):
        """Save the M-Pesa and bill figures for the named day (today if none)."""
        self.object = self.get_object()
        day = _parse_date(request.POST.get("date")) or timezone.localdate()
        form = ShopDailyCashForm(request.POST)
        if form.is_valid():
            ShopDailyCash.objects.update_or_create(
                shop=self.object,
                cash_date=day,
                defaults={
                    "mpesa": form.cleaned_data["mpesa"] or Decimal("0.00"),
                    "bill_out": form.cleaned_data["bill_out"] or Decimal("0.00"),
                    "bill_paid": form.cleaned_data["bill_paid"] or Decimal("0.00"),
                    "recorded_by": request.user,
                },
            )
            messages.success(
                request, f"Cash sheet saved for {self.object.name} on {day}."
            )
        else:
            errors = " ".join(
                f"{field}: {error}"
                for field, field_errors in form.errors.items()
                for error in field_errors
            )
            messages.error(request, f"Cash sheet not saved. {errors}")
        return redirect(
            f"{reverse('shop_detail', args=[self.object.pk])}?date={day.isoformat()}"
        )


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


# ---------------------------------------------------------------------------
# PWA: manifest, service worker, icons
# ---------------------------------------------------------------------------

def _icon_png(size):
    """Generate a simple square icon with a white 'E' on indigo background."""
    if not PIL_AVAILABLE:
        return None
    img = Image.new("RGB", (size, size), (79, 70, 229))  # indigo-600
    draw = ImageDraw.Draw(img)
    # Draw letter E
    try:
        font = ImageFont.truetype("arial.ttf", int(size * 0.6))
    except Exception:
        font = ImageFont.load_default()
    text = "E"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_ICON_CACHE = {}


import os as _os


def pwa_icon(request, size):
    size = int(size)
    if size not in (192, 512, 180, 32, 16):
        size = 192
    if size not in _ICON_CACHE:
        # Prefer committed icon file (works without Pillow in production).
        icon_path = _os.path.join(_os.path.dirname(__file__), "icons", f"icon-{size}.png")
        data = None
        if _os.path.exists(icon_path):
            with open(icon_path, "rb") as f:
                data = f.read()
        if data is None:
            data = _icon_png(size)
        if data is None:
            return HttpResponse(status=404)
        _ICON_CACHE[size] = data
    return HttpResponse(_ICON_CACHE[size], content_type="image/png")


def pwa_manifest(request):
    base = request.build_absolute_uri("/").rstrip("/")
    data = {
        "name": "Empire",
        "short_name": "Empire",
        "description": "Multi-shop profit tracker",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#4f46e5",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


def pwa_service_worker(request):
    js = """
const CACHE_NAME = 'empire-v2';
const SHELL = ['/'];
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  event.respondWith(
    fetch(req).then((res) => {
      const type = res.headers.get('content-type') || '';
      if (res.ok && req.mode !== 'navigate' && !type.includes('text/html')) {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    }).catch(() => {
      if (req.mode === 'navigate') {
        return new Response('Offline - reconnect to open this page.', {
          status: 503,
          headers: {'Content-Type': 'text/plain'}
        });
      }
      return caches.match(req);
    })
  );
});
"""
    return HttpResponse(js.strip(), content_type="application/javascript")
