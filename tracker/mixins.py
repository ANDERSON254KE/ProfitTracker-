"""Mixins for the multi-shop profit tracker.

These mixins are imported by tracker/views.py and must exist for Django
imports to succeed.  Each mixin only needs the methods that the existing
views actually call at runtime.
"""


from django.contrib.auth.mixins import LoginRequiredMixin


class ManagerRequiredMixin(LoginRequiredMixin):
    """Mixin to restrict access to managers only."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_manager():
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("You do not have permission to perform this action.")
        return super().dispatch(request, *args, **kwargs)


class ManagerShopAccessMixin:
    """Mixin to ensure the manager has an assigned shop."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not getattr(request.user, "assigned_shop", None):
            from django.shortcuts import redirect
            return redirect("login")
        self.shop = request.user.assigned_shop
        return super().dispatch(request, *args, **kwargs)


class OwnerRequiredMixin(LoginRequiredMixin):
    """Mixin to restrict access to owners only."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not getattr(request.user, "is_owner", lambda: False)():
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("You do not have permission to perform this action.")
        return super().dispatch(request, *args, **kwargs)