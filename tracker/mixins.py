"""Reusable access-control mixins for the tracker's views.

Two rules are enforced here rather than being repeated in every view:

* a MANAGER may only ever touch rows belonging to ``request.user.assigned_shop``
* an OWNER may look at any shop

Views that need a concrete shop for the request mix in
:class:`ManagerShopAccessMixin` and then simply use ``self.shop``.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from .models import CustomUser, Shop


class RoleRequiredMixin(LoginRequiredMixin):
    """Base mixin restricting a view to a single role.

    Subclasses set :attr:`allowed_roles`. Anonymous users are redirected to
    the login page by ``LoginRequiredMixin``; a logged-in user with the wrong
    role gets a 403 rather than a confusing "not found".
    """

    #: Sequence of :class:`~tracker.models.CustomUser.Role` values allowed in.
    #: Resolved lazily so the user model can live in this same app.
    allowed_roles: tuple[str, ...] = ()

    def get_allowed_roles(self):
        """Return the role values permitted to use this view."""
        return self.allowed_roles

    def dispatch(self, request, *args, **kwargs):
        """Reject users whose role is not allowed."""
        user = request.user
        if not user.is_authenticated:
            return self.handle_no_permission()
        if user.role not in self.get_allowed_roles():
            raise PermissionDenied(
                "You do not have permission to view this page."
            )
        return super().dispatch(request, *args, **kwargs)


class OwnerRequiredMixin(RoleRequiredMixin):
    """Restrict a view to users with the OWNER role."""

    allowed_roles = (CustomUser.Role.OWNER,)


class ManagerRequiredMixin(RoleRequiredMixin):
    """Restrict a view to users with the MANAGER role."""

    allowed_roles = (CustomUser.Role.MANAGER,)


class ManagerShopAccessMixin:
    """Resolve the shop a request is allowed to act on and store it on ``self``.

    * A MANAGER is *forced* onto ``request.user.assigned_shop``. Any shop in
      the URL or query string is ignored, so a manager cannot widen their own
      access by editing the request.
    * An OWNER may target any shop via the ``shop_pk`` URL kwarg or a
      ``shop`` query parameter, falling back to their assigned shop.

    Raises:
        PermissionDenied: if a manager has no assigned shop, or an owner
            targets a shop that does not exist.
    """

    #: URL kwarg holding a primary key, if the route uses one.
    shop_kwarg = "shop_pk"

    def dispatch(self, request, *args, **kwargs):
        """Determine ``self.shop`` before the view body runs."""
        user = request.user
        if not user.is_authenticated:
            raise PermissionDenied("Authentication is required.")

        if user.is_manager():
            # Managers never get a say: the shop comes from their profile.
            shop = user.assigned_shop
            if shop is None:
                raise PermissionDenied(
                    "Your account has no shop assigned. Please ask the owner "
                    "to assign you to a shop."
                )
        else:
            shop = self._resolve_owner_shop(request, kwargs)

        self.shop = shop
        return super().dispatch(request, *args, **kwargs)

    def _resolve_owner_shop(self, request, kwargs):
        """Return the shop an owner is targeting, defaulting to their own."""
        raw_shop = kwargs.get(self.shop_kwarg) or request.GET.get("shop")
        if not raw_shop:
            return request.user.assigned_shop
        try:
            return get_object_or_404(Shop, pk=int(raw_shop))
        except (TypeError, ValueError):
            raise PermissionDenied("That shop reference is not valid.")
