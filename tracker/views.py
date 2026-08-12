from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum, Q
from django.http import HttpResponse
from django.urls import reverse
from io import BytesIO
from decimal import Decimal, InvalidOperation
import pandas as pd
from .models import Product, Transaction, DailySale
from datetime import date, datetime
from decimal import Decimal
from rapidfuzz import process, fuzz

SALES_CATEGORIES = ['250ml', '350ml', '750ml', 'soda', 'cans']
SHOPS = ['Fig Tree', 'Empire Shop', 'Emirates', 'Small City']

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

def _sale_products():
    return list(
        Product.objects.filter(category__in=SALES_CATEGORIES, cost_price__gt=0)
        .order_by('category', 'product_name')
    )

def daily_sales(request):
    products = _sale_products()
    entries = request.session.get('daily_sales', {})
    shop = request.session.get('daily_sales_shop', '')
    if entries:
        ids = [e['sale_id'] for e in entries.values() if e.get('sale_id')]
        alive = set(DailySale.objects.filter(pk__in=ids).values_list('pk', flat=True))
        entries = {k: e for k, e in entries.items() if not e.get('sale_id') or e['sale_id'] in alive}
        request.session['daily_sales'] = entries

    if request.GET.get('reset'):
        request.session['daily_sales'] = {}
        request.session['daily_sales_shop'] = ''
        return redirect(reverse('daily_sales'))

    if request.method == "POST":
        if request.POST.get('entry_type') == 'shop':
            selected = request.POST.get('shop', '').strip()
            if selected in SHOPS:
                request.session['daily_sales_shop'] = selected
            return redirect(reverse('daily_sales'))

        if request.POST.get('entry_type') == 'manual':
            name = request.POST.get('product_name', '').strip()
            sale_date = _parse_date(request.POST.get('sale_date'))
            qty = _parse_decimal(request.POST.get('quantity'))
            sell = _parse_decimal(request.POST.get('selling_price'))
            buy = _parse_decimal(request.POST.get('buying_price'))
            step = int(request.POST.get('step', 0))
            if name and qty and qty > 0 and sell and sell > 0:
                product = Product.objects.filter(product_name=name).first()
                if product is None:
                    product = Product.objects.create(
                        product_name=name,
                        cost_price=buy if buy is not None else Decimal('0.00'),
                        selling_price=sell,
                        category='MANUAL',
                    )
                key = str(product.pk)
                existing = entries.get(key)
                sale = existing.get('sale_id') if existing else None
                if sale:
                    sale = DailySale.objects.filter(pk=sale).first()
                if sale is not None:
                    sale.quantity_sold = qty
                    sale.sell_price = sell
                    sale.cost_price = buy
                    sale.shop = shop
                    sale.date = sale_date
                    sale.save()
                else:
                    sale = DailySale.objects.create(
                        product=product,
                        shop=shop,
                        quantity_sold=qty,
                        sell_price=sell,
                        cost_price=buy,
                        date=sale_date,
                    )
                entries[key] = {'quantity_sold': str(qty), 'sale_id': sale.pk, 'manual': True}
            request.session['daily_sales'] = entries
            return redirect(f"{reverse('daily_sales')}?step={step + 1}")

        pid = request.POST.get('product_id')
        product = get_object_or_404(Product, pk=pid)
        qty_raw = request.POST.get('quantity', '').strip()
        try:
            qty = Decimal(qty_raw)
        except (InvalidOperation, TypeError, ValueError):
            qty = Decimal('0.00')
        sale_date = _parse_date(request.POST.get('sale_date'))
        key = str(product.pk)

        if qty > 0:
            existing = entries.get(key)
            sale = existing.get('sale_id') if existing else None
            sale = DailySale.objects.filter(pk=sale).first() if sale else None
            if sale is not None:
                sale.quantity_sold = qty
                sale.sell_price = product.selling_price
                sale.shop = shop
                sale.date = sale_date
                sale.save()
            else:
                sale = DailySale.objects.create(
                    product=product,
                    shop=shop,
                    quantity_sold=qty,
                    sell_price=product.selling_price,
                    date=sale_date,
                )
            entries[key] = {'quantity_sold': str(qty), 'sale_id': sale.pk}
        else:
            existing = entries.pop(key, None)
            if existing and existing.get('sale_id'):
                DailySale.objects.filter(pk=existing['sale_id']).delete()
        request.session['daily_sales'] = entries

        step = int(request.POST.get('step', 0)) + 1
        return redirect(f"{reverse('daily_sales')}?step={step}")

    if shop not in SHOPS:
        return render(request, 'tracker/daily_sales.html', {
            'pick_shop': True,
            'shops': SHOPS,
            'done': False,
        })

    step = max(0, int(request.GET.get('step', 0)))
    if step < len(products):
        product = products[step]
        prior = entries.get(str(product.pk), {})
        return render(request, 'tracker/daily_sales.html', {
            'product': product,
            'shop': shop,
            'step': step,
            'total': len(products),
            'prior_qty': prior.get('quantity_sold', ''),
            'today_date': date.today().strftime('%Y-%m-%d'),
            'done': False,
        })

    if step == len(products):
        return render(request, 'tracker/daily_sales.html', {
            'manual': True,
            'shop': shop,
            'step': step,
            'total': len(products) + 1,
            'today_date': date.today().strftime('%Y-%m-%d'),
            'done': False,
        })

    ids = [e['sale_id'] for e in entries.values() if e.get('sale_id')]
    sales = DailySale.objects.filter(pk__in=ids)
    total = sales.aggregate(Sum('profit'))['profit__sum'] or 0
    total_units = sales.aggregate(Sum('quantity_sold'))['quantity_sold__sum'] or 0
    return render(request, 'tracker/daily_sales.html', {
        'sales': sales,
        'shop': shop,
        'total_profit': total,
        'total_units': total_units,
        'today_date': date.today().strftime('%Y-%m-%d'),
        'manual_step': len(products),
        'done': True,
    })

def daily_sales_export(request):
    entries = request.session.get('daily_sales', {})
    ids = [e['sale_id'] for e in entries.values() if e.get('sale_id')]
    sales = DailySale.objects.filter(pk__in=ids).select_related('product')
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
    response['Content-Disposition'] = f'attachment; filename="daily_sales_{date.today()}.xlsx"'
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
