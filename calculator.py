import pandas as pd
import os
from datetime import datetime
from rapidfuzz import process, fuzz

class InventoryCalculator:
    def __init__(self, master_prices_file='price list.xlsx', stock_transactions_file='stock_transactions.xlsx'):
        self.master_prices_file = master_prices_file
        self.stock_transactions_file = stock_transactions_file
        self.prices_df = self._load_prices()
        self._ensure_stock_file()

    def _load_prices(self):
        if not os.path.exists(self.master_prices_file):
            raise FileNotFoundError(f"Missing {self.master_prices_file}")
        df = pd.read_excel(self.master_prices_file)
        df.columns = df.columns.str.strip()
        return df

    def _ensure_stock_file(self):
        if not os.path.exists(self.stock_transactions_file):
            cols = ['transaction_id', 'product_name', 'shop', 'type', 'quantity', 
                    'prev_remaining', 'current_remaining', 'units_used', 'sell_price_used', 'profit', 'transaction_date']
            pd.DataFrame(columns=cols).to_excel(self.stock_transactions_file, index=False)

    def find_products(self, search_term):
        if not search_term: return []
        search_term = str(search_term).lower().strip()
        choices = self.prices_df['Product'].astype(str).tolist()
        
        matches = self.prices_df[self.prices_df['Product'].astype(str).str.lower().str.contains(search_term, na=False)]
        fuzzy_results = process.extract(search_term, choices, scorer=fuzz.WRatio, limit=10)
        fuzzy_choices = [res[0] for res in fuzzy_results if res[1] > 60]
        fuzzy_matches = self.prices_df[self.prices_df['Product'].astype(str).isin(fuzzy_choices)]
        
        combined = pd.concat([matches, fuzzy_matches]).drop_duplicates(subset=['Product', 'Shop'] if 'Shop' in self.prices_df.columns else ['Product'])
        return combined.to_dict('records')

    def get_stock_state(self, product_name):
        if not os.path.exists(self.stock_transactions_file):
            return 0, 0, []
        
        df = pd.read_excel(self.stock_transactions_file)
        if 'product_name' not in df.columns:
            return 0, 0, []
            
        history = df[df['product_name'] == product_name].copy()
        
        if history.empty:
            return 0, 0, []

        last_audit = history[history['type'] == 'SALES_CHECK']
        
        if last_audit.empty:
            restocks = history[history['type'] == 'RESTOCK']
            prev_rem = 0
        else:
            last_audit_idx = last_audit.index[-1]
            prev_rem = last_audit.iloc[-1]['current_remaining']
            restocks = history.loc[last_audit_idx + 1:]
            restocks = restocks[restocks['type'] == 'RESTOCK']

        total_added = restocks['quantity'].sum()
        restock_list = []
        if not restocks.empty and 'transaction_date' in restocks.columns and 'quantity' in restocks.columns:
            restock_list = restocks[['transaction_date', 'quantity']].to_dict('records')
        
        return prev_rem, total_added, restock_list

    def save_restock(self, product, qty, date):
        df = pd.read_excel(self.stock_transactions_file)
        shop_val = product.get('Shop', '') if not pd.isna(product.get('Shop')) else ''
        new_row = {
            'transaction_id': f"T{len(df)+1:03d}",
            'product_name': product['Product'],
            'shop': shop_val,
            'type': 'RESTOCK',
            'quantity': qty,
            'transaction_date': date
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(self.stock_transactions_file, index=False)

    def save_audit(self, product, current_rem, prev_rem, total_added, date, manual_sell_price=None):
        total_available = prev_rem + total_added
        units_used = max(0, total_available - current_rem)
        
        cost = product.get('Cost Price', 0)
        cost = 0 if pd.isna(cost) else cost
        
        # Use manual price if provided, otherwise use Excel default
        sell = manual_sell_price if manual_sell_price is not None else product.get('Selling Price', 0)
        sell = 0 if pd.isna(sell) else sell
        
        profit = (units_used * sell) - (units_used * cost)
        
        df = pd.read_excel(self.stock_transactions_file)
        shop_val = product.get('Shop', '') if not pd.isna(product.get('Shop')) else ''
        new_row = {
            'transaction_id': f"T{len(df)+1:03d}",
            'product_name': product['Product'],
            'shop': shop_val,
            'type': 'SALES_CHECK',
            'quantity': total_added,
            'prev_remaining': prev_rem,
            'current_remaining': current_rem,
            'units_used': units_used,
            'sell_price_used': sell,
            'profit': profit,
            'transaction_date': date
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(self.stock_transactions_file, index=False)
        revenue = (units_used * sell)
        margin = (profit / revenue * 100) if revenue > 0 else 0
        return new_row, margin
