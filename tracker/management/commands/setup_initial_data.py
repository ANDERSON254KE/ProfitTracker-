"""Create the initial owner, managers, shops, products and per-shop pricing.

The command is idempotent: running it twice updates nothing that already
exists and only fills in what is missing, so it is safe to run against a
production database (for example after adding a new shop).

Usage::

    python manage.py setup_initial_data
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from tracker.models import CustomUser, MasterProduct, Shop, ShopProduct

#: (name, location) for each branch.
SHOPS = [
    ("Fig Tree", "Fig Tree, Nairobi"),
    ("Empire Shop", "Empire Shopping Centre, Nairobi"),
    ("Emirates", "Emirates Mall, Nairobi"),
    ("Small City", "Small City, Nairobi"),
]

#: (username, shop name) for the branch managers created by this command.
MANAGERS = [
    ("figtree", "Fig Tree"),
    ("empire", "Empire Shop"),
]

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "SecurePassword123!"
MANAGER_PASSWORD = "SecurePassword123!"

#: Master catalogue: (product name, category).
MASTER_PRODUCTS = [
    ("Gilbeys", "250ml"),
    ("Blue Ice", "250ml"),
    ("V&A", "250ml"),
    ("Viceroy", "250ml"),
]

#: Per-shop overrides: shop name -> (product name) -> (cost, selling).
#: Anything not listed here falls back to the base pricing below.
BASE_PRICING = {
    "Gilbeys": (Decimal("450.00"), Decimal("550.00")),
    "Blue Ice": (Decimal("150.00"), Decimal("200.00")),
    "V&A": (Decimal("200.00"), Decimal("280.00")),
    "Viceroy": (Decimal("180.00"), Decimal("250.00")),
}

SHOP_PRICING = {
    "Fig Tree": {
        "Gilbeys": (Decimal("450.00"), Decimal("550.00")),
        "Blue Ice": (Decimal("150.00"), Decimal("200.00")),
    },
    "Empire Shop": {
        "Gilbeys": (Decimal("450.00"), Decimal("600.00")),
        "Blue Ice": (Decimal("160.00"), Decimal("200.00")),
    },
    "Emirates": {
        "Gilbeys": (Decimal("460.00"), Decimal("550.00")),
    },
    "Small City": {
        "Gilbeys": (Decimal("470.00"), Decimal("550.00")),
    },
}


class Command(BaseCommand):
    """Populate the database with the four shops, an owner, managers and pricing."""

    help = "Create the initial owner, shop managers, shops, products and pricing."

    def add_arguments(self, parser):
        """Add the ``--skip-products`` flag for a shops/users-only bootstrap."""
        parser.add_argument(
            "--skip-products",
            action="store_true",
            help="Only create the users and shops, not the catalogue or pricing.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        """Create every record, reporting what was created versus reused."""
        self.stdout.write(self.style.MIGRATE_HEADING("Setting up initial tracker data..."))

        shops = self._create_shops()
        owner = self._create_owner()
        self._create_managers(shops)

        if not options["skip_products"]:
            self._create_products(shops)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Owner '{OWNER_USERNAME}' and {len(MANAGERS)} manager(s) "
                f"across {len(shops)} shop(s)."
            )
        )
        self.stdout.write(
            self.style.WARNING(
                "Default passwords are 'SecurePassword123!' — change them before "
                "putting this on a public server."
            )
        )

    def _create_shops(self):
        """Create (or fetch) the four branches and return them keyed by name."""
        shops = {}
        for name, location in SHOPS:
            shop, created = Shop.objects.get_or_create(
                name=name, defaults={"location": location}
            )
            if not created and shop.location != location and location:
                shop.location = location
                shop.save(update_fields=["location"])
            shops[name] = shop
            self.stdout.write(
                f"  shop     {name}" + ("" if created else "  (already existed)")
            )
        return shops

    def _create_owner(self):
        """Create the single OWNER user, who is not tied to a shop."""
        owner, created = CustomUser.objects.get_or_create(
            username=OWNER_USERNAME,
            defaults={
                "role": CustomUser.Role.OWNER,
                "first_name": "Mufasa",
                "is_staff": True,
                "is_superuser": True,
                "email": "owner@example.com",
            },
        )
        if created:
            owner.set_password(OWNER_PASSWORD)
            owner.save()
            self.stdout.write(f"  user     {OWNER_USERNAME} (owner)  created")
        else:
            self.stdout.write(f"  user     {OWNER_USERNAME} (owner)  (already existed)")
        return owner

    def _create_managers(self, shops):
        """Create one MANAGER per shop listed in :data:`MANAGERS`.

        ``assigned_shop`` and ``Shop.manager`` are both set so the access
        mixins and the admin agree.
        """
        for username, shop_name in MANAGERS:
            shop = shops.get(shop_name)
            if shop is None:
                continue

            user, created = CustomUser.objects.get_or_create(
                username=username,
                defaults={
                    "role": CustomUser.Role.MANAGER,
                    "assigned_shop": shop,
                    "email": f"{username}@example.com",
                },
            )
            if created:
                user.set_password(MANAGER_PASSWORD)
            elif user.assigned_shop_id != shop.pk:
                user.assigned_shop = shop
            if user.role != CustomUser.Role.MANAGER:
                user.role = CustomUser.Role.MANAGER
            user.save()

            if shop.manager_id != user.pk:
                shop.manager = user
                shop.save(update_fields=["manager"])

            self.stdout.write(
                f"  user     {username} (manager of {shop_name})"
                + ("" if created else "  (already existed)")
            )

    def _create_products(self, shops):
        """Create the master catalogue and every shop's price card."""
        catalogue = {}
        for name, category in MASTER_PRODUCTS:
            product, _ = MasterProduct.objects.get_or_create(
                name=name, category=category
            )
            catalogue[name] = product

        created = updated = 0
        for shop_name, shop in shops.items():
            overrides = SHOP_PRICING.get(shop_name, {})
            for product_name, product in catalogue.items():
                if product_name in overrides:
                    cost, sell = overrides[product_name]
                else:
                    base = BASE_PRICING.get(product_name)
                    if base is None:
                        continue
                    cost, sell = base

                shop_product, was_created = ShopProduct.objects.get_or_create(
                    shop=shop,
                    product=product,
                    defaults={"cost_price": cost, "selling_price": sell},
                )
                if not was_created:
                    if (shop_product.cost_price, shop_product.selling_price) != (cost, sell):
                        shop_product.cost_price = cost
                        shop_product.selling_price = sell
                        shop_product.save(update_fields=["cost_price", "selling_price"])
                        updated += 1
                else:
                    created += 1

        self.stdout.write(
            f"  pricing  {created} shop product(s) created, {updated} updated "
            f"across {len(shops)} shop(s)"
        )
