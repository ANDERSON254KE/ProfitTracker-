from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum, Q
from .models import Product, Transaction
from datetime import date
from decimal import Decimal
from rapidfuzz import process, fuzz

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
    total_profit = Transaction.objects.filter(type='SALES_CHECK').aggregate(Sum('profit'))['profit__sum'] or 0
    total_used = Transaction.objects.filter(type='SALES_CHECK').aggregate(Sum('units_used'))['units_used__sum'] or 0
    
    # 3. Recent History
    history = Transaction.objects.filter(type='SALES_CHECK').order_by('-date')[:10]

    return render(request, 'tracker/dashboard.html', {
        'products': products,
        'total_profit': total_profit,
        'total_used': total_used,
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
        manual_date = request.POST.get('transaction_date', date.today())
        
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
