import os
import django
import pandas as pd
from decimal import Decimal, InvalidOperation

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'profit_tracker_pro.settings')
django.setup()

from tracker.models import Product

def clean_decimal(value):
    """Helper to convert various inputs to a valid Decimal or 0."""
    if pd.isna(value):
        return Decimal('0.00')
    
    # Remove currency symbols and commas if any, then strip whitespace
    str_val = str(value).replace('KSh', '').replace(',', '').strip()
    
    if not str_val or str_val == '-':
        return Decimal('0.00')
    
    try:
        return Decimal(str_val)
    except InvalidOperation:
        print(f"⚠️ Warning: Could not convert '{value}' to decimal. Using 0.00 instead.")
        return Decimal('0.00')

def import_products():
    file_path = 'price list.xlsx'
    if not os.path.exists(file_path):
        print(f"Error: {file_path} not found.")
        return

    print(f"Importing products from {file_path}...")
    df = pd.read_excel(file_path)
    df.columns = df.columns.str.strip()

    count = 0
    for _, row in df.iterrows():
        product_name = str(row.get('Product', '')).strip()
        if not product_name or product_name == 'nan':
            continue

        shop = str(row.get('Shop', '')).strip()
        if pd.isna(row.get('Shop')) or shop == 'nan': shop = ''
        
        category = str(row.get('Category', '')).strip()
        if pd.isna(row.get('Category')) or category == 'nan': category = ''
        
        cost = clean_decimal(row.get('Cost Price'))
        sell = clean_decimal(row.get('Selling Price'))

        Product.objects.update_or_create(
            product_name=product_name,
            shop=shop,
            defaults={
                'category': category,
                'cost_price': cost,
                'selling_price': sell,
            }
        )
        count += 1
    
    print(f"Successfully imported {count} products into the database!")

if __name__ == "__main__":
    import_products()
