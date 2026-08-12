from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand
from rapidfuzz import fuzz, process

from tracker.models import Product, Transaction

BASE_DIR = Path(__file__).resolve().parents[3]

def _to_decimal(value, default=None):
    if value is None:
        return default
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default

def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _to_str(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None

def _to_date(value):
    if pd.isna(value):
        return None
    return pd.to_datetime(value).date()

class Command(BaseCommand):
    help = "Seed Product and Transaction data from the Excel source files."

    def handle(self, *args, **options):
        self.import_products()
        self.import_transactions()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Products={Product.objects.count()}, Transactions={Transaction.objects.count()}"
        ))

    def import_products(self):
        path = BASE_DIR / 'price list.xlsx'
        df = pd.read_excel(path, sheet_name='all')
        created = 0
        for _, row in df.iterrows():
            name = _to_str(row.get('Product'))
            selling = _to_decimal(row.get('Selling Price'))
            if not name or selling is None:
                continue
            category = _to_str(row.get('Category'))
            cost = _to_decimal(row.get('Cost Price'), Decimal('0.00'))
            _, was_created = Product.objects.get_or_create(
                product_name=name,
                category=category,
                defaults={'cost_price': cost, 'selling_price': selling},
            )
            if was_created:
                created += 1
        self.stdout.write(f"Products imported from price list: {created} new, {Product.objects.count()} total")

    def import_transactions(self):
        path = BASE_DIR / 'stock_transactions.xlsx'
        df = pd.read_excel(path, sheet_name='Sheet1')
        products = list(Product.objects.all())
        choices = {p.id: f"{p.product_name} {p.category}".strip() for p in products}
        skipped = 0
        for _, row in df.iterrows():
            product_name = _to_str(row.get('product_name'))
            if not product_name:
                self.stderr.write(
                    f"Skipped {row.get('transaction_id')}: no product_name "
                    f"(only product_id={row.get('product_id')})"
                )
                skipped += 1
                continue

            match = process.extractOne(product_name, choices, scorer=fuzz.WRatio)
            if not match or match[1] < 60:
                self.stderr.write(
                    f"Skipped {row.get('transaction_id')}: no fuzzy match for '{product_name}' "
                    f"(best: {match[0] if match else None}, score: {match[1] if match else 0})"
                )
                skipped += 1
                continue

            product = Product.objects.get(pk=match[2])
            self.stdout.write(f"Matched '{product_name}' -> '{match[0]}' (score {match[1]})")

            quantity = _to_int(row.get('quantity'))
            date_value = _to_date(row.get('transaction_date')) or None
            ttype = (_to_str(row.get('type')) or _to_str(row.get('transaction_type')) or '').upper()

            if ttype == 'SALES_CHECK':
                Transaction.objects.create(
                    product=product,
                    type='SALES_CHECK',
                    prev_remaining=_to_int(row.get('prev_remaining')),
                    quantity=quantity,
                    current_remaining=_to_int(row.get('current_remaining')),
                    sell_price_used=product.selling_price,
                    date=date_value,
                )
            elif ttype in ('RESTOCK', 'OPENING'):
                Transaction.objects.create(
                    product=product,
                    type='RESTOCK',
                    quantity=quantity,
                    date=date_value,
                )
            else:
                self.stderr.write(f"Skipped {row.get('transaction_id')}: unknown type '{ttype}'")
                skipped += 1
        self.stdout.write(f"Transactions imported. Skipped {skipped} unmappable row(s)")
