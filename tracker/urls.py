"""URL routing for the tracker.

    /                                 -> redirect to the right dashboard
    /login/  /logout/
    /owner/dashboard/                 -> owner, all shops
    /owner/pricing/                   -> owner, inline price editor
    /manager/dashboard/               -> manager, their shop only
    /manager/sale/add/                -> manager, record a sale
    /api/shop-product-price/<pk>/     -> manager, price lookup for the preview JS
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
    # JSON helper for the live profit preview
    path(
        "api/shop-product-price/<int:product_id>/",
        views.ShopProductPriceAPIView.as_view(),
        name="shop_product_price",
    ),
]
