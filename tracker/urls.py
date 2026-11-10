"""URL routing for the tracker.

    /                                 -> redirect to the right dashboard
    /login/  /logout/
    /owner/dashboard/                 -> owner, all shops
    /owner/pricing/                   -> owner, inline price editor
    /manager/dashboard/               -> manager, their shop only
    /manager/sale/add/                -> manager, record a sale
    /api/shop-product-price/<pk>/     -> manager, price lookup for the preview JS
    /product-comparison/              -> owner, compare up to 5 products
    /products/report/                 -> owner, all-products cross-shop report
    /shops/<pk>/                      -> owner, one shop's report (most sold, dated profits)
    /shops/<pk>/export/               -> owner, that shop's report as CSV
"""

from django.urls import path

from . import views

# No app_name/namespace: route names stay flat ("login", "owner_dashboard")
# so they can be used directly in settings.LOGIN_URL and template {% url %}.
urlpatterns = [
    path("", views.DashboardRedirectView.as_view(), name="dashboard"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    # Owner area
    path("owner/dashboard/", views.OwnerDashboardView.as_view(), name="owner_dashboard"),
    path("owner/pricing/", views.ShopPricingView.as_view(), name="shop_pricing"),
    # Manager area
    path(
        "manager/dashboard/",
        views.ManagerDashboardView.as_view(),
        name="manager_dashboard",
    ),
    path(
        "manager/sale/add/",
        views.DailySaleCreateView.as_view(),
        name="daily_sale_create",
    ),
    path(
        "manager/sheet/",
        views.DailySalesSheetView.as_view(),
        name="daily_sales_sheet",
    ),
    path(
        "shops/<int:shop_pk>/profit-sheet/",
        views.ShopProfitSheetView.as_view(),
        name="shop_profit_sheet",
    ),
    # JSON helper for the live profit preview
    path(
        "api/shop-product-price/<int:product_id>/",
        views.ShopProductPriceAPIView.as_view(),
        name="shop_product_price",
    ),
    # Product comparison page - owner can compare up to 5 products across all shops
    path(
        "product-comparison/",
        views.ProductComparisonView.as_view(),
        name="product_comparison",
    ),
    path(
        "product-comparison/export/",
        views.ProductComparisonCSVView.as_view(),
        name="product_comparison_csv",
    ),
    # All-products cross-shop report with CSV export
    path(
        "products/report/",
        views.ProductReportView.as_view(),
        name="product_report",
    ),
    path(
        "products/report/export/",
        views.ProductReportCSVView.as_view(),
        name="product_report_csv",
    ),
    path(
        "products/report/profit-sheet.xlsx",
        views.AllShopsProfitWorkbookView.as_view(),
        name="all_shops_profit_workbook",
    ),
    # Per-shop report pages (owner dashboard shop cards link here)
    path(
        "shops/<int:pk>/",
        views.ShopDetailView.as_view(),
        name="shop_detail",
    ),
    path(
        "shops/<int:pk>/export/",
        views.ShopReportCSVView.as_view(),
        name="shop_report_csv",
    ),
]
