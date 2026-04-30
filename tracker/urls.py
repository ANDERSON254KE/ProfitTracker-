from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('product/<int:pk>/', views.views_product_detail, name='product_detail'),
]
