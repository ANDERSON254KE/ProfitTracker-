from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('product/<int:pk>/', views.views_product_detail, name='product_detail'),
    path('daily-sales/', views.daily_sales, name='daily_sales'),
    path('daily-sales/product-lookup/', views.daily_sales_product_lookup, name='daily_sales_product_lookup'),
    path('daily-sales/export/', views.daily_sales_export, name='daily_sales_export'),
    path('daily-sales/day-export/', views.daily_sales_day_export, name='daily_sales_day_export'),
    path('daily-sales/report/', views.daily_sales_report, name='daily_sales_report'),
    path('daily-sales/report/export/', views.daily_sales_report_export, name='daily_sales_report_export'),
]
