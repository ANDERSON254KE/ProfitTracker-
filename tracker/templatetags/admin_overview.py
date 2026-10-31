"""Template tags that power the shadcn-styled admin dashboard cards."""

from datetime import date

from django import template
from django.db.models import Count, Sum

from tracker.models import DailySale, DayTotals, Product, SHOP_CHOICES

register = template.Library()


@register.simple_tag
def admin_overview():
    """Aggregate KPIs + one card's worth of numbers per shop.

    Everything here is month-to-date so the dashboard reads the same no
    matter when it is opened; the day figures are reported separately.
    """
    today = date.today()
    month_start = today.replace(day=1)

    products = Product.objects.count()

    day = DailySale.objects.filter(date=today).aggregate(
        units=Sum('quantity_sold'), profit=Sum('profit'),
        rows=Count('id'),
    )
    day_exp = (
        DayTotals.objects.filter(date=today).aggregate(v=Sum('total_expenditure'))['v']
        or 0
    )

    month_sales = DailySale.objects.filter(date__gte=month_start, date__lte=today)
    month = month_sales.aggregate(units=Sum('quantity_sold'), profit=Sum('profit'))
    month_exp = (
        DayTotals.objects.filter(date__gte=month_start, date__lte=today)
        .aggregate(v=Sum('total_expenditure'))['v']
        or 0
    )
    month_profit = month['profit'] or 0
    month_units = month['units'] or 0
    month_sales_total = (
        DayTotals.objects.filter(date__gte=month_start, date__lte=today)
        .aggregate(v=Sum('total_sales'))['v']
        or 0
    )

    # Best-selling product this month (by units) - the "what moves most" KPI.
    top = (
        month_sales.values('product_id', 'product__product_name')
        .annotate(units=Sum('quantity_sold'))
        .order_by('-units')
        .first()
    )

    shops = []
    for name, _label in SHOP_CHOICES:
        qs = month_sales.filter(shop=name)
        agg = qs.aggregate(units=Sum('quantity_sold'), profit=Sum('profit'))
        exp = (
            DayTotals.objects.filter(shop=name, date__gte=month_start, date__lte=today)
            .aggregate(v=Sum('total_expenditure'))['v']
            or 0
        )
        sales = (
            DayTotals.objects.filter(shop=name, date__gte=month_start, date__lte=today)
            .aggregate(v=Sum('total_sales'))['v']
            or 0
        )
        last = qs.order_by('-date').values_list('date', flat=True).first()
        all_time_rows = DailySale.objects.filter(shop=name).count()
        shops.append({
            'name': name,
            'units': agg['units'] or 0,
            'profit': agg['profit'] or 0,
            'expenses': exp,
            'sales': sales,
            'net': (agg['profit'] or 0) - exp,
            'last_date': last,
            'rows': all_time_rows,
            'days': qs.values('date').distinct().count(),
        })

    return {
        'today': today,
        'month_start': month_start,
        'products': products,
        'day_units': day['units'] or 0,
        'day_profit': day['profit'] or 0,
        'day_rows': day['rows'] or 0,
        'day_expenses': day_exp,
        'month_units': month_units,
        'month_profit': month_profit,
        'month_expenses': month_exp,
        'month_sales': month_sales_total,
        'month_net': month_profit - month_exp,
        'top_product': top['product__product_name'] if top else None,
        'top_product_units': top['units'] if top else 0,
        'shops': shops,
    }
