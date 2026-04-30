import streamlit as st
import pandas as pd
import os
from datetime import datetime
import plotly.express as px
from calculator import InventoryCalculator

# --- Page Config ---
st.set_page_config(page_title="Profit Tracker Dashboard", layout="wide", page_icon="📈")

# --- Initialize Calculator ---
calc = InventoryCalculator()

# --- App Header ---
st.title("🚀 Inventory & Profit Tracker Pro")
st.markdown("---")

# --- Sidebar (Navigation & Settings) ---
st.sidebar.header("📊 Dashboard Menu")
menu = st.sidebar.radio("Navigation", ["Home Dashboard", "Track / Audit Product", "Full Sales History", "Product Price List"])

# --- Helper Functions ---
def format_money(val):
    return f"KSh {val:,.2f}"

# 1. HOME DASHBOARD (Summary Charts)
if menu == "Home Dashboard":
    st.subheader("Business Summary")
    
    if os.path.exists(calc.stock_transactions_file):
        df = pd.read_excel(calc.stock_transactions_file)
        audits = df[df['type'] == 'SALES_CHECK']
        
        if not audits.empty:
            # Metrics Row
            total_profit = audits['profit'].sum()
            total_used = audits['units_used'].sum()
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Net Profit", format_money(total_profit))
            col2.metric("Total Products Used", f"{total_used} units")
            col3.metric("Last Audit Date", audits['transaction_date'].iloc[-1])
            
            # Profit by Category / Product Chart
            fig = px.bar(audits, x='product_name', y='profit', color='shop', 
                         title="Profit per Product (Last Audits)",
                         labels={'product_name': 'Product', 'profit': 'Profit (KSh)'})
            st.plotly_chart(fig, use_container_width=True)
            
            # History Table
            st.markdown("### Latest Sales Audits")
            st.dataframe(audits[['transaction_date', 'product_name', 'shop', 'units_used', 'sell_price_used', 'profit']].tail(10), use_container_width=True)
        else:
            st.info("No sales audits recorded yet. Go to 'Track / Audit Product' to start.")
    else:
        st.info("Start tracking to see your business summary here!")

# 2. TRACK / AUDIT PRODUCT (The Core Tool)
elif menu == "Track / Audit Product":
    st.subheader("Manage Inventory & Sales")
    
    # Search with Autocomplete logic
    product_names = calc.prices_df['Product'].tolist()
    search = st.selectbox("Search for a Product (Start typing...)", product_names, index=None, placeholder="Type name (e.g. Ginger, Hunters)")
    
    if search:
        # Get all matches (to handle different shops)
        matches = calc.find_products(search)
        
        if len(matches) > 1:
            # Shop Selection if multiple matches found
            shops = [f"{p['Product']} (Shop: {p['Shop'] if not pd.isna(p['Shop']) else 'N/A'})" for p in matches]
            selected_idx = st.radio("Multiple versions found. Please select the correct one:", range(len(shops)), format_func=lambda i: shops[i])
            product = matches[selected_idx]
        else:
            product = matches[0]
            
        st.success(f"Selected: {product['Product']}")
        
        # Current State
        prev_rem, total_added, restocks = calc.get_stock_state(product['Product'])
        
        # Prices UI
        c1, c2, c3 = st.columns(3)
        c1.write(f"**Category:** {product['Category']}")
        c2.warning(f"**Buying Price:** {format_money(product['Cost Price'])}")
        c3.success(f"**Default Selling Price:** {format_money(product['Selling Price'])}")
        
        # Actions Layout
        st.markdown("---")
        action_col1, action_col2 = st.columns(2)
        
        with action_col1:
            st.markdown("#### 📥 Add New Stock")
            new_qty = st.number_input("Quantity Added Today", min_value=0, step=1, key="restock_qty")
            restock_date = st.date_input("Restock Date", datetime.now(), key="restock_date")
            if st.button("Save Restock", use_container_width=True):
                calc.save_restock(product, int(new_qty), str(restock_date))
                st.toast("✓ Restock Recorded!", icon='📥')
                st.rerun()

        with action_col2:
            st.markdown("#### 🧮 Record Sales Audit")
            manual_sell = st.number_input("Selling Price Used for This Period", value=float(product['Selling Price']), step=10.0, key="manual_sell")
            current_rem = st.number_input("CURRENT Count on Shelf (Remaining)", min_value=0, step=1, key="audit_rem")
            audit_date = st.date_input("Audit Date", datetime.now(), key="audit_date")
            
            # Show calculations preview
            total_available = prev_rem + total_added
            used = max(0, total_available - current_rem)
            
            st.info(f"Summary: Found {prev_rem} units + Added {total_added} = {total_available} total. {used} units were used.")
            
            if st.button("Finalize Audit & Calculate Profit", use_container_width=True, type="primary"):
                row, margin = calc.save_audit(product, int(current_rem), prev_rem, total_added, str(audit_date), manual_sell_price=manual_sell)
                st.success(f"✓ Profit Recorded: {format_money(row['profit'])} (Margin: {margin:.1f}%)")
                if current_rem < 10:
                    st.warning("⚠️ Low stock warning! Remember to restock.")
                st.balloons()
        
        # Show recent activity for this product
        if restocks:
            with st.expander("Show Recent Restock History"):
                st.table(pd.DataFrame(restocks))

# 3. FULL HISTORY
elif menu == "Full Sales History":
    st.subheader("Complete Sales Audit History")
    if os.path.exists(calc.stock_transactions_file):
        full_df = pd.read_excel(calc.stock_transactions_file)
        st.dataframe(full_df.sort_values('transaction_id', ascending=False), use_container_width=True)
        
        # Option to download
        csv = full_df.to_csv(index=False).encode('utf-8')
        st.download_button("Download History as CSV", csv, "sales_history.csv", "text/csv")
    else:
        st.info("No data found.")

# 4. PRICE LIST
elif menu == "Product Price List":
    st.subheader("Master Price List (from Excel)")
    st.dataframe(calc.prices_df, use_container_width=True)
