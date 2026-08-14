from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum, Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from io import BytesIO
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from rapidfuzz import process, fuzz
import pandas as pd
from .models import Product, Transaction, DailySale, ProductShopPrice

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
        Product.objects.filter(category__in=SALES_CATEGORIES, cost_price__gt=0)
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
        })
    return JsonResponse({'found': False})


def daily_sales(request):
    shop = request.session.get('daily_sales_shop', '')

    if request.GET.get('reset'):
        request.session['daily_sales_shop'] = ''
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
            for i in range(len(names)):
                name = names[i].strip()
                qty = _parse_decimal(qtys[i]) if i < len(qtys) else None
                sell = _parse_decimal(sells[i]) if i < len(sells) else None
                buy = _parse_decimal(buys[i]) if i < len(buys) else None
                if name and qty and qty > 0 and sell and sell > 0:
                    product = Product.objects.filter(product_name__iexact=name).first()
                    if product is None:
                        product = Product.objects.create(
                            product_name=name,
                            cost_price=buy if buy is not None else Decimal('0.00'),
                            selling_price=sell,
                            category='MANUAL',
                        )
                    else:
                        if buy is not None:
                            product.cost_price = buy
                        product.selling_price = sell
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

            return redirect(f"{reverse('daily_sales')}?saved=1&date={sale_date}")

    if shop not in SHOPS:
        return render(request, 'tracker/daily_sales.html', {
            'pick_shop': True,
            'shops': SHOPS,
        })

    sale_date = _parse_date(request.GET.get('date')) or date.today()
    products = _sale_products()

    existing = {
        s.product_id: s for s in
        DailySale.objects.filter(shop=shop, date=sale_date)
    }
    overrides = _shop_price_overrides(shop)

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

    saved = request.GET.get('saved') == '1'

    return render(request, 'tracker/daily_sales.html', {
        'shop': shop,
        'sale_date': sale_date,
        'categories_data': categories_data,
        'saved': saved,
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
        rows.append({
            'Shop': '',
            'Product': 'TOTAL',
            'Category': '',
            'Selling Price': None,
            'Amount Sold': total_units,
            'Profit': total_profit,
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

def daily_sales_report(request):
    report_date = _parse_date(request.GET.get('date'))
    sales = DailySale.objects.filter(date=report_date).select_related('product').order_by('shop', 'product__product_name')
    shops_data = []
    grand_profit = grand_units = Decimal('0.00')
    for name in SHOPS:
        qs = sales.filter(shop=name)
        profit = qs.aggregate(Sum('profit'))['profit__sum'] or Decimal('0.00')
        units = qs.aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or Decimal('0.00')
        grand_profit += profit
        grand_units += units
        shops_data.append({'name': name, 'sales': qs, 'profit': profit, 'units': units})
    unassigned = sales.exclude(shop__in=SHOPS)
    if unassigned.exists():
        up = unassigned.aggregate(Sum('profit'))['profit__sum'] or Decimal('0.00')
        uu = unassigned.aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or Decimal('0.00')
        grand_profit += up
        grand_units += uu
        shops_data.append({'name': 'Unassigned', 'sales': unassigned, 'profit': up, 'units': uu})
    return render(request, 'tracker/daily_sales_report.html', {
        'shops_data': shops_data,
        'grand_profit': grand_profit,
        'grand_units': grand_units,
        'report_date': report_date,
    })

def daily_sales_report_export(request):
    report_date = _parse_date(request.GET.get('date'))
    sales = DailySale.objects.filter(date=report_date).select_related('product').order_by('shop', 'product__product_name')
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
        grouped = {}
        for r in rows:
            grouped.setdefault(r['Shop'] or 'Unassigned', []).append(r)
        for name in list(SHOPS) + [k for k in grouped if k not in SHOPS]:
            sub = grouped.get(name)
            if not sub:
                continue
            units = sum((r['Amount Sold'] for r in sub), Decimal('0.00'))
            profit = sum((r['Profit'] for r in sub), 0.0)
            grand_units += units
            grand_profit += profit
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
            'Shop': 'ALL SHOPS',
            'Product': 'GRAND TOTAL',
            'Category': '',
            'Selling Price': None,
            'Amount Sold': grand_units,
            'Profit': grand_profit,
            'Date': '',
        })
    else:
        rows = [{k: None for k in columns}]
    df = pd.DataFrame(rows, columns=columns)
    buffer = BytesIO()
    df.to_excel(buffer, index=False, sheet_name=f'Daily Report {report_date}')
    buffer.seek(0)
    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="daily_report_{report_date}.xlsx"'
    return response
