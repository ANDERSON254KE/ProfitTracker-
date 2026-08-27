from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum, Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from io import BytesIO
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta
import json
import re
from rapidfuzz import process, fuzz
import pandas as pd
from .models import Product, Transaction, DailySale, ProductShopPrice, DayTotals

SALES_CATEGORIES = ['250ml', '350ml', '750ml', 'soda', 'cans']
SHOPS = ['Fig Tree', 'Empire Shop', 'Emirates', 'Small City']

STOCK_SHEET_ORDER = [
    'Gilbeys', 'Best Whiskey', 'Blue Ice', 'V&A', 'Bond 7', 'viceroy', 'Smirnoff',
    'Kane', 'Napoleon', 'Konyagi', 'Richot', 'Kenya King', 'Muckpit', 'Captain Morgan',
    'Origin', 'Chrome Vodka', 'Tripple Ace', 'Kibao', 'Hunters', 'Best Gin',
    'William Grayson', 'Dallas', 'General Meakings', 'People Vodka', 'Smart Vodka',
    'Best Vodka', 'Chrome Gin', 'County', 'carribian', 'Trace', 'KC. Pineapple',
    'KC. smooth', 'KC .Ginger',
    'Jameson', 'Richot', 'Black&white', 'All seasons', 'Gilbeys', 'Smirnoff',
    'Kibao', 'CrazyCork', 'Hunters', 'Viceroy', 'Amarula', 'VAT 69',
    'Black label', 'Red label',
    'Kc smooth', 'KC. Pineapple', 'Kc.Ginger', 'Kane Extra', 'V&A', 'Hunters',
    'All seasons', 'Kenya king', 'Chrome Vodka', 'Chrome Gin', 'Origin',
    '4th street', 'Jameson 750ml', 'William Lawsons', 'viceroy', 'Black&white',
    'Gilbeys', 'Captain Morgan', 'Cointy', 'Smirnoff', 'Kibao', 'General Meakings',
    'Amarula 1ltr', 'Amarula 750', 'Best Gin', 'Best Whisky', 'Red label 750',
    'Red Label 1 ltr', 'Black Label 750ml', 'Black Label 1ltr', 'Crazy Cock',
    'Richot', 'VAT 69',
    'Guarana', 'Guarana Black', 'Guiness', 'Tusker', 'Tusker Malt', 'Tusker Lite',
    'Tusker cider', 'Snapp', 'pineapple Punch', 'Faxe', 'Redbull', 'Delmonte',
    'Balozi', 'Whitecap', 'Caprice', 'Pilsner', 'Heineken',
    'Tusker', 'Guiness', 'Tusker cider', 'Pilsner', 'White Cap', 'Kingfisher',
    'Soda 500ml', 'Soda 350ml', 'Soda 300ml', 'Soda 200ml', 'Soda 1.25ltrs',
    'Soda 2ltrs', 'mInutemaid Small', 'mInutemaid Big', 'Dasani Small',
    'Dasani Big', 'Lemonade', 'predator', 'Kiss', 'Glacier Small', 'Glacier Big',
]

STOCK_SHEET_POSITION = {name.lower().strip(): idx for idx, name in enumerate(STOCK_SHEET_ORDER)}


def _shop_price_overrides(shop):
    """Return {product_id: selling_price} for shop-specific price overrides."""
    return {sp.product_id: sp.selling_price for sp in ProductShopPrice.objects.filter(shop=shop)}


def dashboard(request):
    query = request.GET.get('q', '').strip()
    products = Product.objects.all()
    
    # 1. Search Logic (Fuzzy Match Support)
    if query:
        # Exact/Partial
        products = Product.objects.filter(Q(product_name__icontains=query) | Q(shop__icontains=query))
        # Fuzzy (Optional improvement via JavaScript or this backend loop)
        if not products.exists():
            choices = {p.id: p.product_name for p in Product.objects.all()}
            fuzzy_ids = [res[2] for res in process.extract(query, choices, scorer=fuzz.WRatio, limit=5) if res[1] > 60]
            products = Product.objects.filter(id__in=fuzzy_ids)
    else:
        products = products[:4]

    # 2. Total Business Metrics
    audit_profit = Transaction.objects.filter(type='SALES_CHECK').aggregate(Sum('profit'))['profit__sum'] or 0
    audit_used = Transaction.objects.filter(type='SALES_CHECK').aggregate(Sum('units_used'))['units_used__sum'] or 0
    daily_profit = DailySale.objects.aggregate(Sum('profit'))['profit__sum'] or 0
    daily_used = DailySale.objects.aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or 0
    total_profit = audit_profit + daily_profit
    total_used = audit_used + daily_used

    today = date.today()
    today_profit = (
        DailySale.objects.filter(date=today).aggregate(Sum('profit'))['profit__sum'] or 0
    ) + (
        Transaction.objects.filter(type='SALES_CHECK', date=today).aggregate(Sum('profit'))['profit__sum'] or 0
    )
    today_units = (
        DailySale.objects.filter(date=today).aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or 0
    )
    
    # 3. Recent History
    history = Transaction.objects.filter(type='SALES_CHECK').order_by('-date')[:10]

    return render(request, 'tracker/dashboard.html', {
        'products': products,
        'total_profit': total_profit,
        'total_used': total_used,
        'today_profit': today_profit,
        'today_units': today_units,
        'history': history,
        'query': query
    })

def views_product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    
    # Calculate state since last audit
    last_audit = Transaction.objects.filter(product=product, type='SALES_CHECK').first()
    prev_rem = last_audit.current_remaining if last_audit else 0
    
    restocks = Transaction.objects.filter(product=product, type='RESTOCK')
    if last_audit:
        restocks = restocks.filter(created_at__gt=last_audit.created_at)
        
    total_added = restocks.aggregate(Sum('quantity'))['quantity__sum'] or 0
    
    if request.method == "POST":
        action = request.POST.get('action')
        manual_date = _parse_date(request.POST.get('transaction_date'))
        
        if action == "RESTOCK":
            qty = int(request.POST.get('quantity', 0))
            Transaction.objects.create(product=product, type='RESTOCK', quantity=qty, date=manual_date)
            return redirect('product_detail', pk=pk)
            
        elif action == "AUDIT":
            current = int(request.POST.get('current_remaining', 0))
            sell_price_val = request.POST.get('sell_price')
            manual_sell = Decimal(sell_price_val) if sell_price_val else product.selling_price
            transaction = Transaction.objects.create(
                product=product, 
                type='SALES_CHECK', 
                prev_remaining=prev_rem,
                quantity=total_added,
                current_remaining=current,
                sell_price_used=manual_sell,
                date=manual_date
            )
            return render(request, 'tracker/audit_results.html', {'transaction': transaction})

    return render(request, 'tracker/product_detail.html', {
        'product': product,
        'prev_rem': prev_rem,
        'total_added': total_added,
        'restocks': restocks.order_by('-date'),
        'today_date': date.today().strftime('%Y-%m-%d')
    })

def _parse_date(value):
    if not value:
        return date.today()
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return date.today()

def _parse_decimal(value):
    if value is None:
        return None
    try:
        value = Decimal(str(value).strip())
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None

def _stock_sheet_position(product):
    key = product.product_name.lower().strip()
    return STOCK_SHEET_POSITION.get(key, 9999)


def _sale_products():
    products = list(
        Product.objects.filter(
            Q(category__in=SALES_CATEGORIES) | Q(category='MANUAL'),
            cost_price__gt=0,
        )
    )
    products.sort(key=_stock_sheet_position)
    return products

def daily_sales_product_lookup(request):
    name = request.GET.get('name', '').strip()
    shop = request.GET.get('shop', '').strip()
    product = Product.objects.filter(product_name__iexact=name).first()
    if product is not None:
        selling_price = product.selling_price
        if shop in SHOPS:
            override = ProductShopPrice.objects.filter(product=product, shop=shop).first()
            if override:
                selling_price = override.selling_price
        return JsonResponse({
            'found': True,
            'cost_price': str(product.cost_price),
            'selling_price': str(selling_price),
            'category': product.category or '',
        })
    return JsonResponse({'found': False})


def daily_sales(request):
    shop = request.session.get('daily_sales_shop', '')

    # Persist the chosen date in the session so it survives shop switches.
    date_param = request.GET.get('date')
    if date_param:
        request.session['daily_sales_date'] = date_param

    if request.GET.get('reset'):
        request.session['daily_sales_shop'] = ''
        return redirect(reverse('daily_sales'))
    if request.GET.get('reset_date'):
        request.session['daily_sales_date'] = ''
        return redirect(reverse('daily_sales'))

    if request.method == "POST":
        if request.POST.get('entry_type') == 'shop':
            selected = request.POST.get('shop', '').strip()
            if selected in SHOPS:
                request.session['daily_sales_shop'] = selected
            return redirect(reverse('daily_sales'))

        if request.POST.get('entry_type') == 'bulk':
            shop = request.session.get('daily_sales_shop', '')
            if shop not in SHOPS:
                return redirect(reverse('daily_sales'))
            sale_date = _parse_date(request.POST.get('sale_date')) or date.today()

            existing = {
                s.product_id: s for s in
                DailySale.objects.filter(shop=shop, date=sale_date)
            }
            overrides = _shop_price_overrides(shop)

            for product in _sale_products():
                qty = _parse_decimal(request.POST.get('qty_%d' % product.pk))
                sell_price = _parse_decimal(request.POST.get('sell_%d' % product.pk))
                cost_price = _parse_decimal(request.POST.get('cost_%d' % product.pk))
                sale = existing.get(product.pk)
                if qty and qty > 0:
                    sell = sell_price if sell_price is not None else overrides.get(product.pk, product.selling_price)
                    cost = cost_price if cost_price is not None else product.cost_price
                    if sale:
                        sale.quantity_sold = qty
                        sale.sell_price = sell
                        sale.cost_price = cost
                        sale.save()
                    else:
                        DailySale.objects.create(
                            product=product,
                            shop=shop,
                            quantity_sold=qty,
                            sell_price=sell,
                            cost_price=cost,
                            date=sale_date,
                        )
                elif sale:
                    sale.delete()

            names = request.POST.getlist('manual_name')
            buys = request.POST.getlist('manual_buy')
            sells = request.POST.getlist('manual_sell')
            qtys = request.POST.getlist('manual_qty')
            cats = request.POST.getlist('manual_category')
            for i in range(len(names)):
                name = names[i].strip()
                qty = _parse_decimal(qtys[i]) if i < len(qtys) else None
                sell = _parse_decimal(sells[i]) if i < len(sells) else None
                buy = _parse_decimal(buys[i]) if i < len(buys) else None
                cat = cats[i].strip() if i < len(cats) else ''
                if name and qty and qty > 0 and sell and sell > 0:
                    product = Product.objects.filter(product_name__iexact=name).first()
                    if product is None:
                        product = Product.objects.create(
                            product_name=name,
                            cost_price=buy if buy is not None else Decimal('0.00'),
                            selling_price=sell,
                            category=cat or 'MANUAL',
                        )
                    else:
                        if buy is not None:
                            product.cost_price = buy
                        product.selling_price = sell
                        if cat:
                            product.category = cat
                        product.save()
                    sale, created = DailySale.objects.get_or_create(
                        product=product,
                        shop=shop,
                        date=sale_date,
                        defaults={
                            'quantity_sold': qty,
                            'sell_price': sell,
                            'cost_price': product.cost_price,
                        },
                    )
                    if not created:
                        sale.quantity_sold = qty
                        sale.sell_price = sell
                        sale.cost_price = product.cost_price
                        sale.save()

            # Manually-typed day totals (Total Sales / Profit / Expenditure) for
            # the copyable all-branches Day Summary.
            DayTotals.objects.update_or_create(
                shop=shop,
                date=sale_date,
                defaults={
                    'total_sales': _parse_decimal(request.POST.get('total_sales')) or Decimal('0.00'),
                    'total_profit': _parse_decimal(request.POST.get('total_profit')) or Decimal('0.00'),
                    'total_expenditure': _parse_decimal(request.POST.get('total_expenditure')) or Decimal('0.00'),
                },
            )

            return redirect(f"{reverse('daily_sales')}?saved=1&date={sale_date}")

    if shop not in SHOPS:
        return render(request, 'tracker/daily_sales.html', {
            'pick_shop': True,
            'shops': SHOPS,
        })

    sale_date = (
        _parse_date(request.GET.get('date'))
        or _parse_date(request.session.get('daily_sales_date'))
        or date.today()
    )
    products = _sale_products()

    existing = {
        s.product_id: s for s in
        DailySale.objects.filter(shop=shop, date=sale_date)
    }
    overrides = _shop_price_overrides(shop)

    # Expenditure is the single value the user types (DayTotals), not itemized.
    day_totals = DayTotals.objects.filter(shop=shop, date=sale_date).first()
    total_expenses = day_totals.total_expenditure if day_totals else Decimal('0.00')

    saved = request.GET.get('saved') == '1'
    sales_profit = net_profit = Decimal('0.00')
    if saved:
        sales_profit = (
            DailySale.objects.filter(shop=shop, date=sale_date)
            .aggregate(Sum('profit'))['profit__sum'] or Decimal('0.00')
        )
        net_profit = sales_profit - total_expenses

    categories_data = []
    for cat in SALES_CATEGORIES:
        cat_products = [p for p in products if p.category == cat]
        if cat_products:
            rows = []
            for p in cat_products:
                sale = existing.get(p.pk)
                rows.append((
                    p,
                    sale.quantity_sold if sale else '',
                    overrides.get(p.pk, p.selling_price),
                    sale.cost_price if sale else p.cost_price,
                ))
            categories_data.append({'name': cat, 'rows': rows})

    custom_products = [p for p in products if p.category == 'MANUAL']
    if custom_products:
        rows = []
        for p in custom_products:
            sale = existing.get(p.pk)
            rows.append((
                p,
                sale.quantity_sold if sale else '',
                overrides.get(p.pk, p.selling_price),
                sale.cost_price if sale else p.cost_price,
            ))
        categories_data.append({'name': 'Custom', 'rows': rows})

    manual_categories = SALES_CATEGORIES + sorted(
        c for c in Product.objects.values_list('category', flat=True).distinct()
        if c and c not in SALES_CATEGORIES
    )

    # Manually-typed day totals: this branch's saved values (to pre-fill the
    # inputs) and every branch's values for the same date (for the popup).
    def _prefill(v):
        return ('%0.2f' % v) if v else ''

    saved_totals = {dt.shop: dt for dt in DayTotals.objects.filter(date=sale_date)}
    branch_totals = {}
    for name in SHOPS:
        dt = saved_totals.get(name)
        branch_totals[name] = {
            'sales': float(dt.total_sales) if dt else 0.0,
            'profit': float(dt.total_profit) if dt else 0.0,
            'exp': float(dt.total_expenditure) if dt else 0.0,
        }

    return render(request, 'tracker/daily_sales.html', {
        'shop': shop,
        'sale_date': sale_date,
        'date_is_today': sale_date == date.today(),
        'today_iso': date.today().strftime('%Y-%m-%d'),
        'categories_data': categories_data,
        'saved': saved,
        'sales_categories': SALES_CATEGORIES,
        'manual_categories': manual_categories,
        'total_expenses': total_expenses,
        'sales_profit': sales_profit,
        'net_profit': net_profit,
        'day_sales_val': _prefill(day_totals.total_sales) if day_totals else '',
        'day_profit_val': _prefill(day_totals.total_profit) if day_totals else '',
        'day_exp_val': _prefill(day_totals.total_expenditure) if day_totals else '',
        'branch_totals': branch_totals,
    })

def daily_sales_export(request):
    shop = request.session.get('daily_sales_shop', '')
    sale_date = _parse_date(request.GET.get('date')) or date.today()
    sales = DailySale.objects.filter(shop=shop, date=sale_date).select_related('product')
    rows = [{
        'Shop': s.shop or 'Unassigned',
        'Product': s.product.product_name,
        'Category': s.product.category,
        'Selling Price': float(s.sell_price),
        'Amount Sold': s.quantity_sold,
        'Profit': float(s.profit),
        'Date': s.date,
    } for s in sales]
    if rows:
        total_units = sum((r['Amount Sold'] for r in rows), Decimal('0.00'))
        total_profit = sum((r['Profit'] for r in rows), 0.0)
        dt = DayTotals.objects.filter(shop=shop, date=sale_date).first()
        expenses = dt.total_expenditure if dt else Decimal('0.00')
        net_profit = total_profit - float(expenses)
        rows.append({
            'Shop': '',
            'Product': 'TOTAL',
            'Category': '',
            'Selling Price': None,
            'Amount Sold': total_units,
            'Profit': total_profit,
            'Date': '',
        })
        rows.append({
            'Shop': '',
            'Product': 'EXPENSES',
            'Category': '',
            'Selling Price': None,
            'Amount Sold': None,
            'Profit': -float(expenses),
            'Date': '',
        })
        rows.append({
            'Shop': '',
            'Product': 'NET PROFIT',
            'Category': '',
            'Selling Price': None,
            'Amount Sold': None,
            'Profit': net_profit,
            'Date': '',
        })
    else:
        rows = [{'Shop': None, 'Product': None, 'Category': None, 'Selling Price': None, 'Amount Sold': None, 'Profit': None, 'Date': None}]
    df = pd.DataFrame(rows)
    buffer = BytesIO()
    df.to_excel(buffer, index=False, sheet_name='Daily Sales')
    buffer.seek(0)
    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    safe_shop = (shop or 'Unassigned').replace(' ', '_')
    response['Content-Disposition'] = f'attachment; filename="daily_sales_{safe_shop}_{sale_date}.xlsx"'
    return response

def _safe_sheet_name(name):
    """Excel tab names: max 31 chars, none of : \\ / ? * [ ]."""
    cleaned = re.sub(r'[:\\/?*\[\]]', ' ', str(name)).strip()
    return (cleaned or 'Sheet')[:31]


def daily_sales_day_export(request):
    """Export every shop's sales for a single day into one workbook: one tab
    per shop (its products + TOTAL/EXPENSES/NET) plus an 'All Shops' summary tab.
    """
    sale_date = _parse_date(request.GET.get('date')) or date.today()
    all_sales = DailySale.objects.filter(date=sale_date).select_related('product')
    columns = ['Product', 'Category', 'Selling Price', 'Amount Sold', 'Profit']

    # (tab label, sales queryset, expenses queryset) — the four shops, then any
    # sales/expenses whose shop is unknown (only if such rows exist).
    day_exp = {dt.shop: dt.total_expenditure for dt in DayTotals.objects.filter(date=sale_date)}
    segments = [
        (name, all_sales.filter(shop=name), day_exp.get(name, Decimal('0.00')))
        for name in SHOPS
    ]
    unassigned = all_sales.exclude(shop__in=SHOPS)
    if unassigned.exists():
        unassigned_exp = sum(
            (v for k, v in day_exp.items() if k not in SHOPS), Decimal('0.00')
        )
        segments.append(('Unassigned', unassigned, unassigned_exp))

    summary_rows = []
    grand_units = Decimal('0.00')
    grand_profit = 0.0
    grand_expenses = 0.0

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        for name, qs, exp_val in segments:
            rows = [{
                'Product': s.product.product_name,
                'Category': s.product.category,
                'Selling Price': float(s.sell_price),
                'Amount Sold': s.quantity_sold,
                'Profit': float(s.profit),
            } for s in qs.order_by('product__product_name')]

            units = sum((r['Amount Sold'] for r in rows), Decimal('0.00'))
            profit = sum((r['Profit'] for r in rows), 0.0)
            expenses = float(exp_val)
            net = profit - expenses

            grand_units += units
            grand_profit += profit
            grand_expenses += expenses
            summary_rows.append({
                'Shop': name,
                'Amount Sold': units,
                'Profit': profit,
                'Expenses': expenses,
                'Net Profit': net,
            })

            rows.append({'Product': 'TOTAL', 'Category': '', 'Selling Price': None, 'Amount Sold': units, 'Profit': profit})
            rows.append({'Product': 'EXPENSES', 'Category': '', 'Selling Price': None, 'Amount Sold': None, 'Profit': -expenses})
            rows.append({'Product': 'NET PROFIT', 'Category': '', 'Selling Price': None, 'Amount Sold': None, 'Profit': net})

            df = pd.DataFrame(rows, columns=columns)
            df.to_excel(writer, index=False, sheet_name=_safe_sheet_name(name))

        summary_rows.append({
            'Shop': 'GRAND TOTAL',
            'Amount Sold': grand_units,
            'Profit': grand_profit,
            'Expenses': grand_expenses,
            'Net Profit': grand_profit - grand_expenses,
        })
        summary_df = pd.DataFrame(
            summary_rows,
            columns=['Shop', 'Amount Sold', 'Profit', 'Expenses', 'Net Profit'],
        )
        summary_df.to_excel(writer, index=False, sheet_name='All Shops')

    buffer.seek(0)
    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="all_shops_{sale_date}.xlsx"'
    return response

def _strict_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _iso_month(value):
    """Return a (year, month) tuple from 'YYYY-MM' or None."""
    try:
        return int(str(value)[:4]), int(str(value)[5:7])
    except (ValueError, IndexError):
        return None


def _month_bounds(value):
    ym = _iso_month(value)
    if not ym or not (1 <= ym[1] <= 12):
        return None
    y, m = ym
    first = date(y, m, 1)
    if m == 12:
        last = date(y + 1, 1, 1)
    else:
        last = date(y, m + 1, 1)
    return first, last - timedelta(days=1)


def _report_period(request):
    """Return (start, end, label) for the report based on request params.

    Priority: explicit start+end range > month > single date > today.
    """
    today = date.today()
    start = _strict_date(request.GET.get('start'))
    end = _strict_date(request.GET.get('end'))
    if start and end:
        if end >= start:
            return start, end, f"{start} → {end}"

    bounds = _month_bounds(request.GET.get('month'))
    if bounds:
        return bounds[0], bounds[1], bounds[0].strftime('%B %Y')

    day = _strict_date(request.GET.get('date'))
    if day:
        return day, day, str(day)

    return today, today, str(today)


def _report_series(sale_by_date, exp_by_date, start, end):
    """Build chart series (labels, profits, expenses, nets).

    Daily for ranges up to 62 days, otherwise bucketed weekly.
    """
    labels, profits, expenses = [], [], []
    if (end - start).days <= 62:
        cur = start
        while cur <= end:
            sp = float(sale_by_date.get(cur, 0))
            ex = float(exp_by_date.get(cur, 0))
            labels.append(cur.strftime('%d %b'))
            profits.append(sp)
            expenses.append(ex)
            cur += timedelta(days=1)
    else:
        buckets = {}
        order = []
        cur = start
        while cur <= end:
            wk = cur.isocalendar()[:2]
            if wk not in buckets:
                buckets[wk] = {'sp': 0.0, 'ex': 0.0, 'label': cur.strftime('%d %b')}
                order.append(wk)
            buckets[wk]['sp'] += float(sale_by_date.get(cur, 0))
            buckets[wk]['ex'] += float(exp_by_date.get(cur, 0))
            cur += timedelta(days=1)
        for wk in order:
            b = buckets[wk]
            labels.append(b['label'])
            profits.append(b['sp'])
            expenses.append(b['ex'])
    nets = [p - e for p, e in zip(profits, expenses)]
    return labels, profits, expenses, nets


def daily_sales_report(request):
    start, end, period_label = _report_period(request)
    sales = (
        DailySale.objects.filter(date__gte=start, date__lte=end)
        .select_related('product').order_by('shop', 'product__product_name')
    )
    expense_qs = DayTotals.objects.filter(date__gte=start, date__lte=end)

    shops_data = []
    grand_profit = grand_units = grand_expenses = grand_net = Decimal('0.00')
    for name in SHOPS:
        qs = sales.filter(shop=name)
        profit = qs.aggregate(Sum('profit'))['profit__sum'] or Decimal('0.00')
        units = qs.aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or Decimal('0.00')
        expenses = (
            expense_qs.filter(shop=name).aggregate(Sum('total_expenditure'))['total_expenditure__sum']
            or Decimal('0.00')
        )
        net = profit - expenses
        product_rows = list(
            qs.values('product__product_name', 'product__category')
            .annotate(qty=Sum('quantity_sold'), profit=Sum('profit'))
            .order_by('product__product_name')
        )
        grand_profit += profit
        grand_units += units
        grand_expenses += expenses
        grand_net += net
        shops_data.append({
            'name': name,
            'product_rows': product_rows,
            'profit': profit,
            'expenses': expenses,
            'net_profit': net,
            'units': units,
        })

    unassigned = sales.exclude(shop__in=SHOPS)
    if unassigned.exists():
        up = unassigned.aggregate(Sum('profit'))['profit__sum'] or Decimal('0.00')
        uu = unassigned.aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or Decimal('0.00')
        ue = (
            expense_qs.exclude(shop__in=SHOPS).aggregate(Sum('total_expenditure'))['total_expenditure__sum']
            or Decimal('0.00')
        )
        product_rows = list(
            unassigned.values('product__product_name', 'product__category')
            .annotate(qty=Sum('quantity_sold'), profit=Sum('profit'))
            .order_by('product__product_name')
        )
        grand_profit += up
        grand_units += uu
        grand_expenses += ue
        grand_net += (up - ue)
        shops_data.append({
            'name': 'Unassigned',
            'product_rows': product_rows,
            'profit': up,
            'expenses': ue,
            'net_profit': up - ue,
            'units': uu,
        })

    sale_by_date = {
        r['date']: r['subtotal']
        for r in sales.values('date').annotate(subtotal=Sum('profit')).order_by('date')
    }
    exp_by_date = {
        r['date']: r['subtotal']
        for r in expense_qs.values('date').annotate(subtotal=Sum('total_expenditure')).order_by('date')
    }
    labels, profits, expenses, nets = _report_series(sale_by_date, exp_by_date, start, end)
    chart = {
        'labels': labels,
        'profits': profits,
        'expenses': expenses,
        'nets': nets,
        'shops': [s['name'] for s in shops_data],
        'shop_nets': [float(s['net_profit']) for s in shops_data],
    }

    return render(request, 'tracker/daily_sales_report.html', {
        'shops_data': shops_data,
        'grand_profit': grand_profit,
        'grand_units': grand_units,
        'grand_expenses': grand_expenses,
        'grand_net': grand_net,
        'period_label': period_label,
        'start': start,
        'end': end,
        'month_value': end.strftime('%Y-%m'),
        'today_iso': date.today().strftime('%Y-%m-%d'),
        'month_first': date.today().replace(day=1).strftime('%Y-%m-%d'),
        'current_month': date.today().strftime('%Y-%m'),
        'chart': chart,
    })

def daily_sales_report_export(request):
    start, end, period_label = _report_period(request)
    sales = (
        DailySale.objects.filter(date__gte=start, date__lte=end)
        .select_related('product').order_by('shop', 'product__product_name')
    )
    columns = ['Shop', 'Product', 'Category', 'Selling Price', 'Amount Sold', 'Profit', 'Date']
    rows = [{
        'Shop': s.shop or 'Unassigned',
        'Product': s.product.product_name,
        'Category': s.product.category,
        'Selling Price': float(s.sell_price),
        'Amount Sold': s.quantity_sold,
        'Profit': float(s.profit),
        'Date': s.date,
    } for s in sales]
    if rows:
        grand_units = Decimal('0.00')
        grand_profit = 0.0
        grand_expenses = 0.0
        expense_rows = (
            DayTotals.objects.filter(date__gte=start, date__lte=end)
            .values('shop').annotate(total=Sum('total_expenditure'))
        )
        expenses_by_shop = {r['shop'] or 'Unassigned': r['total'] for r in expense_rows}
        grouped = {}
        for r in rows:
            grouped.setdefault(r['Shop'] or 'Unassigned', []).append(r)
        for name in list(SHOPS) + [k for k in grouped if k not in SHOPS]:
            sub = grouped.get(name)
            if not sub:
                continue
            units = sum((r['Amount Sold'] for r in sub), Decimal('0.00'))
            profit = sum((r['Profit'] for r in sub), 0.0)
            expenses = float(expenses_by_shop.get(name, Decimal('0.00')))
            net = profit - expenses
            grand_units += units
            grand_profit += profit
            grand_expenses += expenses
            rows.append({
                'Shop': name,
                'Product': 'TOTAL - ' + name,
                'Category': '',
                'Selling Price': None,
                'Amount Sold': units,
                'Profit': profit,
                'Date': '',
            })
            rows.append({
                'Shop': name,
                'Product': 'EXPENSES - ' + name,
                'Category': '',
                'Selling Price': None,
                'Amount Sold': None,
                'Profit': -expenses,
                'Date': '',
            })
            rows.append({
                'Shop': name,
                'Product': 'NET PROFIT - ' + name,
                'Category': '',
                'Selling Price': None,
                'Amount Sold': None,
                'Profit': net,
                'Date': '',
            })
        rows.append({
            'Shop': 'ALL SHOPS',
            'Product': 'GRAND NET PROFIT',
            'Category': '',
            'Selling Price': None,
            'Amount Sold': grand_units,
            'Profit': grand_profit - grand_expenses,
            'Date': '',
        })
    else:
        rows = [{k: None for k in columns}]
    df = pd.DataFrame(rows, columns=columns)
    buffer = BytesIO()
    df.to_excel(buffer, index=False, sheet_name=f'Report {period_label}')
    buffer.seek(0)
    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    safe = period_label.replace('/', '-').replace(' ', '_').replace('→', 'to')
    response['Content-Disposition'] = f'attachment; filename="report_{safe}.xlsx"'
    return response
