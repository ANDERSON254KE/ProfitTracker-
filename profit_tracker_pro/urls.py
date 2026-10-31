from django.contrib import admin
from django.urls import path, include

from tracker import views as tracker_views

urlpatterns = [
    # Custom admin pages must come first: AdminSite installs a catch-all
    # route under admin/ that would otherwise swallow them.
    path('admin/shops/<str:shop>/', tracker_views.admin_shop_sheet, name='admin_shop_sheet'),
    path('admin/', admin.site.urls),
    path('', include('tracker.urls')),
]
