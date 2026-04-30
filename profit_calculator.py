import pandas as pd
import os
from datetime import datetime

class ProfitTracker:
    def __init__(self, prices_file='price list.xlsx', stock_file='stock_transactions.xlsx', sales_file='sales_log.xlsx'):
        self.prices_file = prices_file
        self.stock_file = stock_file
        self.sales_file = sales_file
        self.prices_df = None
        self.stock_df = None
        self.sales_df = None
        self.report_df = None

    def load_data(self):
        """Loads data from Excel files and validates structure."""
        try:
            if not all(os.path.exists(f) for f in [self.prices_file, self.stock_file, self.sales_file]):
                missing = [f for f in [self.prices_file, self.stock_file, self.sales_file] if not os.path.exists(f)]
                raise FileNotFoundError(f"Missing files: {', '.join(missing)}")

            self.prices_df = pd.read_excel(self.prices_file)
            self.stock_df = pd.read_excel(self.stock_file)
            self.sales_df = pd.read_excel(self.sales_file)

            # Standardize date columns
            self.stock_df['transaction_date'] = pd.to_datetime(self.stock_df['transaction_date'])
            self.sales_df['sale_date'] = pd.to_datetime(self.sales_df['sale_date'])

            return True
        except Exception as e:
            print(f"Error loading data: {e}")
            return False

    def calculate_profits(self, start_date=None, end_date=None):
        """Performs core calculations for inventory and profits."""
        if self.prices_df is None:
            return None

        # Filter by date if provided
        stock_filtered = self.stock_df.copy()
        sales_filtered = self.sales_df.copy()

        if start_date:
            stock_filtered = stock_filtered[stock_filtered['transaction_date'] >= start_date]
            sales_filtered = sales_filtered[sales_filtered['sale_date'] >= start_date]
        if end_date:
            stock_filtered = stock_filtered[stock_filtered['transaction_date'] <= end_date]
            sales_filtered = sales_filtered[sales_filtered['sale_date'] <= end_date]

        # 1. Total Stock Available (OPENING + RESTOCK)
        stock_summary = stock_filtered.groupby('product_id')['quantity'].sum().reset_index()
        stock_summary.rename(columns={'quantity': 'total_stock_available'}, inplace=True)

        # 2. Total Units Sold
        sales_summary = sales_filtered.groupby('product_id')['quantity_sold'].sum().reset_index()
        sales_summary.rename(columns={'quantity_sold': 'total_units_sold'}, inplace=True)

        # Merge all into a master report dataframe
        report = self.prices_df.merge(stock_summary, on='product_id', how='left').fillna(0)
        report = report.merge(sales_summary, on='product_id', how='left').fillna(0)

        # 3. Closing Stock
        report['closing_stock'] = report['total_stock_available'] - report['total_units_sold']

        # 4. Revenue = Units Sold * Selling Price
        report['revenue'] = report['total_units_sold'] * report['selling_price']

        # 5. COGS = Units Sold * Cost Price
        report['cogs'] = report['total_units_sold'] * report['cost_price']

        # 6. Gross Profit = Revenue - COGS
        report['gross_profit'] = report['revenue'] - report['cogs']

        # 7. Profit Margin %
        report['profit_margin_pct'] = (report['gross_profit'] / report['revenue'].replace(0, 1)) * 100
        report.loc[report['revenue'] == 0, 'profit_margin_pct'] = 0

        self.report_df = report
        return report

    def get_low_stock(self, threshold=10):
        """Identifies products below a certain stock level."""
        if self.report_df is None:
            return None
        return self.report_df[self.report_df['closing_stock'] < threshold]

    def export_report(self, filename='profit_report.xlsx', format='excel'):
        """Exports the report to Excel or CSV."""
        if self.report_df is None:
            print("No report data to export.")
            return

        try:
            if format == 'excel':
                self.report_df.to_excel(filename, index=False)
            else:
                self.report_df.to_csv(filename.replace('.xlsx', '.csv'), index=False)
            print(f"Report exported successfully to {filename}")
        except Exception as e:
            print(f"Export failed: {e}")

    def lookup_product(self, product_id):
        """Looks up profit details for a single product."""
        if self.report_df is None:
            self.calculate_profits()
        
        product = self.report_df[self.report_df['product_id'] == product_id]
        if product.empty:
            return f"Product ID {product_id} not found."
        return product.to_dict('records')[0]

def main_menu():
    tracker = ProfitTracker()
    if not tracker.load_data():
        return

    while True:
        print("\n=== INVENTORY & PROFIT TRACKER ===")
        print("1. View All Products Summary")
        print("2. Generate Monthly Report (Date Filter)")
        print("3. Product Lookup by ID")
        print("4. Check Low Stock Alerts")
        print("5. Export Report to Excel/CSV")
        print("6. Exit")
        
        choice = input("\nEnter choice (1-6): ")

        if choice == '1':
            report = tracker.calculate_profits()
            print("\n--- PROFIT SUMMARY ---")
            print(report[['product_id', 'product_name', 'closing_stock', 'gross_profit', 'profit_margin_pct']])
        
        elif choice == '2':
            year = input("Enter Year (YYYY): ")
            month = input("Enter Month (MM): ")
            try:
                start = datetime(int(year), int(month), 1)
                # Simple month-end logic (1st of next month)
                if int(month) == 12:
                    end = datetime(int(year) + 1, 1, 1)
                else:
                    end = datetime(int(year), int(month) + 1, 1)
                
                report = tracker.calculate_profits(start_date=start, end_date=end)
                print(f"\n--- REPORT FOR {year}-{month} ---")
                print(report[['product_id', 'product_name', 'total_units_sold', 'gross_profit']])
            except ValueError:
                print("Invalid date format.")

        elif choice == '3':
            pid = input("Enter Product ID: ")
            data = tracker.lookup_product(pid)
            if isinstance(data, dict):
                print("\n--- PRODUCT DETAILS ---")
                for k, v in data.items():
                    print(f"{k.replace('_', ' ').title()}: {v}")
            else:
                print(data)

        elif choice == '4':
            threshold = int(input("Enter low stock threshold (default 10): ") or 10)
            tracker.calculate_profits()
            low_stock = tracker.get_low_stock(threshold)
            if low_stock.empty:
                print("No low stock alerts.")
            else:
                print("\n--- LOW STOCK ALERTS ---")
                print(low_stock[['product_id', 'product_name', 'closing_stock']])

        elif choice == '5':
            fmt = input("Export format (excel/csv): ").lower()
            tracker.calculate_profits()
            if fmt == 'csv':
                tracker.export_report(format='csv')
            else:
                tracker.export_report(format='excel')

        elif choice == '6':
            print("Exiting...")
            break
        else:
            print("Invalid choice. Try again.")

if __name__ == "__main__":
    main_menu()
