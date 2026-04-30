import os
import sys
from datetime import datetime
from colorama import init, Fore, Style
from calculator import InventoryCalculator

init(autoreset=True)

class Dashboard:
    def __init__(self):
        self.calc = InventoryCalculator()

    def clear(self):
        os.system('c' \
        'ls' if os.name == 'nt' else 'clear')

    def draw_line(self, char="─", length=55):
        print(char * length)

    def print_header(self, title):
        print(Fore.CYAN + Style.BRIGHT + "=" * 55)
        print(Fore.CYAN + Style.BRIGHT + f"   {title.upper()}")
        print(Fore.CYAN + Style.BRIGHT + "=" * 55)

    def format_money(self, val):
        try:
            val = float(val) if not isinstance(val, (int, float)) and val else val
            import pandas as pd
            if pd.isna(val): return "KSh 0.00"
            return f"KSh {val:,.2f}"
        except:
            return "KSh 0.00"

    def get_int_input(self, prompt):
        while True:
            try:
                val = input(prompt).strip()
                if not val: return 0
                return int(val)
            except ValueError:
                print(Fore.RED + "❌ Please enter a valid number.")

    def get_float_input(self, prompt, default=None):
        while True:
            try:
                val = input(prompt).strip()
                if not val: return default
                return float(val)
            except ValueError:
                print(Fore.RED + "❌ Please enter a valid price (e.g. 550.50).")

    def run(self):
        while True:
            self.clear()
            self.print_header("Inventory & Profit Dashboard")
            print("[1] Search & Update Product")
            print("[2] View Full Sales History")
            print("[3] Exit")
            
            cmd = input("\nSelect Option: ").strip()
            if cmd == '1': self.process_product()
            elif cmd == '2': self.show_history()
            elif cmd == '3': break

    def process_product(self):
        self.clear()
        self.print_header("Product Search")
        search = input("\nEnter Product Name (e.g. Ginger, Hunters): ").strip()
        if not search: return

        matches = self.calc.find_products(search)
        if not matches:
            print(Fore.RED + f"❌ Product '{search}' not found in Excel.")
            input("\nPress Enter to try again...")
            return

        if len(matches) > 1:
            print(Fore.YELLOW + f"\nMultiple matches found for '{search}':")
            for i, p in enumerate(matches, 1):
                import pandas as pd
                shop_info = f" | Shop: {p['Shop']}" if 'Shop' in p and not pd.isna(p['Shop']) else ""
                print(f"[{i}] {p['Product']} ({p['Category']}{shop_info})")
            
            choice = input("\nSelect correct version (number): ").strip()
            if not choice.isdigit(): return
            idx = int(choice) - 1
            if 0 <= idx < len(matches):
                product = matches[idx]
            else: return
        else:
            product = matches[0]

        self.product_menu(product)

    def product_menu(self, product):
        while True:
            self.clear()
            import pandas as pd
            shop_label = f" ({product['Shop']})" if 'Shop' in product and not pd.isna(product['Shop']) else ""
            self.print_header(f"Product: {product['Product']}{shop_label}")
            
            prev_rem, total_added, restocks = self.calc.get_stock_state(product['Product'])
            
            print(f"Category: {product['Category']}")
            print(f"Cost Price   : {Fore.YELLOW}{self.format_money(product['Cost Price'])}")
            print(f"Selling Price: {Fore.GREEN}{self.format_money(product['Selling Price'])}")
            self.draw_line()
            
            print(Fore.BLUE + f"Last Count Left      : {prev_rem} units")
            print(Fore.BLUE + f"Total Added Since    : {total_added} units")
            
            if restocks:
                print("\nRecent Restocks:")
                for r in restocks[-3:]:
                    print(f" • {r['transaction_date']}: +{r['quantity']} units")
            
            self.draw_line()
            print("[1] Add New Stock (RESTOCK)")
            print("[2] Record Sales & Profit (AUDIT)")
            print("[3] Back to Main Menu")
            
            choice = input("\nAction: ").strip()
            
            if choice == '1':
                qty = self.get_int_input("Enter quantity added: ")
                date = input("Date (YYYY-MM-DD) [Enter for today]: ") or datetime.now().strftime("%Y-%m-%d")
                self.calc.save_restock(product, qty, date)
                print(Fore.BLUE + "✓ Restock recorded!")
                input("Press Enter...")
            
            elif choice == '2':
                self.draw_line("═")
                print(Fore.CYAN + "Finalizing Sales & Profit Analysis")
                
                # REQUIREMENT: Manually edit selling price
                default_sell = product['Selling Price']
                sell_price = self.get_float_input(f"Enter Selling Price used for this period [Default {self.format_money(default_sell)}]: ", default=default_sell)
                
                current = self.get_int_input("Enter CURRENT stock count on shelf: ")
                date = input("Audit Date [Enter for today]: ") or datetime.now().strftime("%Y-%m-%d")
                
                # Pass the manual sell price to the calculator
                row, margin = self.calc.save_audit(product, current, prev_rem, total_added, date, manual_sell_price=sell_price)
                
                print(Fore.YELLOW + f"\nRESULTS for this period (at {self.format_money(sell_price)}):")
                print(f"Total Available : {prev_rem + total_added} units")
                print(f"Units Sold      : {Fore.WHITE}{row['units_used']}")
                print(f"TOTAL PROFIT    : {Fore.GREEN}{Style.BRIGHT}{self.format_money(row['profit'])}")
                print(f"Profit Margin   : {margin:.2f}%")
                
                if current < 10:
                    print(Fore.RED + "\n⚠️ LOW STOCK ALERT!")
                
                input("\nPress Enter to continue...")
                break
            
            elif choice == '3':
                break

    def show_history(self):
        self.clear()
        self.print_header("Sales History")
        if os.path.exists(self.calc.stock_transactions_file):
            import pandas as pd
            df = pd.read_excel(self.calc.stock_transactions_file)
            audits = df[df['type'] == 'SALES_CHECK']
            if audits.empty:
                print("No sales audits recorded yet.")
            else:
                # Show the sell price used in the history table
                print(audits[['transaction_date', 'product_name', 'sell_price_used', 'units_used', 'profit']].tail(15))
        else:
            print("No history file found.")
        input("\nPress Enter...")

if __name__ == "__main__":
    Dashboard().run()
