"""Initial schema for the multi-shop profit tracker, with a legacy data bridge.

This app used to store everything in one wide ``tracker_product`` table plus a
``tracker_dailysale`` ledger with a free-text shop name. It now uses a
normalised ``Shop`` / ``MasterProduct`` / ``ShopProduct`` / ``DailySale``
schema with per-shop prices and server-computed profit.

Because ``AUTH_USER_MODEL`` is swapped to ``tracker.CustomUser``, Django
requires the custom user to be created in the app's *first* migration (the
``admin.LogEntry`` table references it before anything else has run). That
rules out appending the swap to the old migration chain, so the history is
rebased into this single initial migration.

The upgrade itself is data-preserving and runs in three guarded steps around
the table creation:

1. :func:`rename_legacy_tables` -- moves the old tables aside (only
   ``tracker_dailysale`` genuinely collides with the new schema, but all of
   them are renamed so the leftovers are obvious). No-op on a fresh install.
2. :func:`import_legacy_data` -- copies products, per-shop pricing and the
   sales ledger across, preserving the original sale dates.
3. :func:`drop_legacy_tables` -- removes the leftovers once the copy succeeded.

.. note::
   Deploying this over an existing database requires the ``tracker`` app's
   rows in ``django_migrations`` to be cleared first, so the new initial
   migration actually runs. ``manage.py prepare_multishop_upgrade`` does this
   for you, and backs the database up first.
"""

import django.contrib.auth.models
import django.contrib.auth.validators
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal

from django.conf import settings
from django.db import connection, migrations, models

#: Old table name -> new "aside" name used during the upgrade.
LEGACY_TABLES = {
    "tracker_dailysale": "legacy_dailysale",
    "tracker_product": "legacy_product",
    "tracker_productshopprice": "legacy_productshopprice",
    "tracker_transaction": "legacy_transaction",
    "tracker_dailyexpense": "legacy_dailyexpense",
    "tracker_daytotals": "legacy_daytotals",
}

#: Branches created by the upgrade so copied prices have a home.
BOOTSTRAP_SHOPS = [
    ("Fig Tree", "Fig Tree, Nairobi"),
    ("Empire Shop", "Empire Shopping Centre, Nairobi"),
    ("Emirates", "Emirates Mall, Nairobi"),
    ("Small City", "Small City, Nairobi"),
]

#: Where legacy sales with no recognisable shop are filed.
FALLBACK_SHOP = "Fig Tree"

#: Owner of rows migrated from the legacy ledger (inactive, never signed in).
LEGACY_IMPORT_USER = "legacy-import"


def _quote(name):
    """Return ``name`` quoted for the active database backend."""
    return connection.ops.quote_name(name)


def _table_exists(name):
    """Return whether ``name`` is a real table on the default connection."""
    return name in connection.introspection.table_names()


def _rename_sequences_for(old_table, new_table):
    """Move a renamed table's sequence aside on PostgreSQL.

    A no-op elsewhere: SQLite has no sequences and MySQL names them per
    table, so neither collides.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = current_schema()"
        )
        for (sequence,) in cursor.fetchall():
            if sequence == f"{old_table}_id_seq":
                cursor.execute(
                    f"ALTER SEQUENCE {_quote(sequence)} RENAME TO "
                    f"{_quote(f'{new_table}_id_seq')}"
                )


def rename_legacy_tables(apps, schema_editor):
    """Move the pre-upgrade tables aside so the new ones can be created.

    Skips any table that is already missing (fresh install) and any that has
    already been renamed (a retried migration).
    """
    for old_name, new_name in LEGACY_TABLES.items():
        if not _table_exists(old_name):
            continue
        if _table_exists(new_name):
            # Already moved aside by an earlier attempt; leave it alone.
            continue
        with connection.cursor() as cursor:
            cursor.execute(
                f"ALTER TABLE {_quote(old_name)} RENAME TO {_quote(new_name)}"
            )
        _rename_sequences_for(old_name, new_name)


def import_legacy_data(apps, schema_editor):
    """Copy legacy products, pricing and sales into the new schema.

    Safe to run against a database with no legacy tables: every step checks
    for the table it needs and returns early.
    """
    if not _table_exists("legacy_product"):
        return

    Shop = apps.get_model("tracker", "Shop")
    MasterProduct = apps.get_model("tracker", "MasterProduct")
    ShopProduct = apps.get_model("tracker", "ShopProduct")
    CustomUser = apps.get_model("tracker", "CustomUser")
    DailySale = apps.get_model("tracker", "DailySale")

    shops = {}
    for name, location in BOOTSTRAP_SHOPS:
        shop, _ = Shop.objects.get_or_create(
            name=name, defaults={"location": location}
        )
        shops[name] = shop
    fallback = shops.get(FALLBACK_SHOP) or next(iter(shops.values()))

    # Rows migrated from the old ledger need an owner; this account is inert.
    system_user, _ = CustomUser.objects.get_or_create(
        username=LEGACY_IMPORT_USER,
        defaults={"role": "MANAGER", "is_active": False},
    )

    master_by_legacy_id = {}
    for row in _fetch("legacy_product", "id, product_name, category, cost_price, selling_price"):
        name = (row["product_name"] or "").strip()
        if not name:
            continue
        category = (row["category"] or "").strip()
        master, _ = MasterProduct.objects.get_or_create(name=name, category=category)
        master_by_legacy_id[row["id"]] = master.pk

    # Per-shop selling-price overrides, keyed by (legacy product id, shop).
    overrides = {}
    if _table_exists("legacy_productshopprice"):
        for row in _fetch("legacy_productshopprice", "product_id, shop, selling_price"):
            overrides[(row["product_id"], (row["shop"] or "").strip())] = row["selling_price"]

    # Price every product in every shop, using the override where the old app
    # had one so branch-specific prices survive.
    legacy_prices = {
        row["id"]: (row["cost_price"], row["selling_price"])
        for row in _fetch("legacy_product", "id, cost_price, selling_price")
    }
    for legacy_id, master_id in master_by_legacy_id.items():
        base_cost, base_sell = legacy_prices.get(legacy_id, (Decimal("0.00"), Decimal("0.00")))
        for shop_name, shop in shops.items():
            selling = overrides.get((legacy_id, shop_name), base_sell)
            ShopProduct.objects.update_or_create(
                shop_id=shop.pk,
                product_id=master_id,
                defaults={
                    "cost_price": base_cost,
                    "selling_price": selling,
                    "is_active": True,
                },
            )

    if not _table_exists("legacy_dailysale"):
        return

    # Current price card, so migrated rows carry the shop's real prices.
    card = {
        (sp.shop_id, sp.product_id): (sp.cost_price, sp.selling_price)
        for sp in ShopProduct.objects.all()
    }

    pending = []
    for row in _fetch(
        "legacy_dailysale",
        "id, product_id, shop, quantity_sold, sell_price, cost_price, date",
    ):
        master_id = master_by_legacy_id.get(row["product_id"])
        if master_id is None:
            continue

        shop = shops.get((row["shop"] or "").strip()) or fallback
        prices = card.get((shop.pk, master_id))
        if prices is not None:
            cost, price = prices
        else:
            cost = row["cost_price"] if row["cost_price"] is not None else Decimal("0.00")
            price = row["sell_price"] or Decimal("0.00")

        units = Decimal(str(row["quantity_sold"] or 0))
        revenue = (price * units).quantize(Decimal("0.01"))
        total_cost = (cost * units).quantize(Decimal("0.01"))

        sale = DailySale(
            shop_id=shop.pk,
            product_id=master_id,
            units_sold=units,
            unit_cost_at_sale=cost,
            unit_price_at_sale=price,
            total_revenue=revenue,
            total_cost=total_cost,
            profit=revenue - total_cost,
            recorded_by_id=system_user.pk,
        )
        # sale_date uses auto_now_add, which bulk_create overwrites with today,
        # so hold on to the original date and restore it afterwards.
        sale._legacy_date = row["date"]
        pending.append(sale)

    if not pending:
        return

    created = DailySale.objects.bulk_create(pending, batch_size=500)

    # Restore the original dates, one query per distinct day.
    by_date = {}
    for sale in created:
        by_date.setdefault(sale._legacy_date, []).append(sale.pk)
    for day, pks in by_date.items():
        if day is None:
            continue
        DailySale.objects.filter(pk__in=pks).update(sale_date=day)


def drop_legacy_tables(apps, schema_editor):
    """Remove the moved-aside tables now that their contents have been copied."""
    for new_name in LEGACY_TABLES.values():
        if not _table_exists(new_name):
            continue
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {_quote(new_name)}")


def _fetch(table, columns):
    """Yield the rows of ``table`` as dicts, or nothing if it does not exist."""
    if not _table_exists(table):
        return []
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {columns} FROM {_quote(table)}")
        names = [c.strip() for c in columns.split(",")]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


class Migration(migrations.Migration):
    """Create the multi-shop schema, then carry legacy data across."""

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        # Runs first: the new tracker_dailysale table cannot be created while
        # the legacy one still owns that name.
        migrations.RunPython(rename_legacy_tables, migrations.RunPython.noop),
        migrations.CreateModel(
            name="CustomUser",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                ("is_superuser", models.BooleanField(default=False, help_text="Designates that this user has all permissions without explicitly assigning them.", verbose_name="superuser status")),
                ("username", models.CharField(error_messages={"unique": "A user with that username already exists."}, help_text="Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.", max_length=150, unique=True, validators=[django.contrib.auth.validators.UnicodeUsernameValidator()], verbose_name="username")),
                ("first_name", models.CharField(blank=True, max_length=150, verbose_name="first name")),
                ("last_name", models.CharField(blank=True, max_length=150, verbose_name="last name")),
                ("email", models.EmailField(blank=True, max_length=254, verbose_name="email address")),
                ("is_staff", models.BooleanField(default=False, help_text="Designates whether the user can log into this admin site.", verbose_name="staff status")),
                ("is_active", models.BooleanField(default=True, help_text="Designates whether this user should be treated as active. Unselect this instead of deleting accounts.", verbose_name="active")),
                ("date_joined", models.DateTimeField(default=django.utils.timezone.now, verbose_name="date joined")),
                ("role", models.CharField(choices=[("OWNER", "Owner"), ("MANAGER", "Manager")], default="MANAGER", max_length=10, verbose_name="Role")),
                ("groups", models.ManyToManyField(blank=True, help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.", related_name="user_set", related_query_name="user", to="auth.group", verbose_name="groups")),
                ("user_permissions", models.ManyToManyField(blank=True, help_text="Specific permissions for this user.", related_name="user_set", related_query_name="user", to="auth.permission", verbose_name="user permissions")),
            ],
            options={
                "verbose_name": "User",
                "verbose_name_plural": "Users",
            },
            managers=[
                ("objects", django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name="MasterProduct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, verbose_name="Product")),
                ("category", models.CharField(blank=True, default="", max_length=100, verbose_name="Category")),
            ],
            options={
                "ordering": ["name", "category"],
                "constraints": [models.UniqueConstraint(fields=("name", "category"), name="unique_master_product_name_category")],
            },
        ),
        migrations.CreateModel(
            name="Shop",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True, verbose_name="Name")),
                ("location", models.CharField(blank=True, default="", max_length=255, verbose_name="Location")),
                ("manager", models.OneToOneField(blank=True, help_text="Convenience link for the owner; access checks use assigned_shop.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_shop", to=settings.AUTH_USER_MODEL, verbose_name="Manager")),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="customuser",
            name="assigned_shop",
            field=models.ForeignKey(blank=True, help_text="Only used for MANAGER users. Scopes everything they can see.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_users", to="tracker.shop", verbose_name="Assigned Shop"),
        ),
        migrations.CreateModel(
            name="DailySale",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sale_date", models.DateField(auto_now_add=True, db_index=True, verbose_name="Sale Date")),
                ("units_sold", models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.01"))], verbose_name="Units Sold")),
                ("unit_cost_at_sale", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Unit Cost at Sale")),
                ("unit_price_at_sale", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Unit Price at Sale")),
                ("total_revenue", models.DecimalField(decimal_places=2, default=0, editable=False, max_digits=14, verbose_name="Total Revenue")),
                ("total_cost", models.DecimalField(decimal_places=2, default=0, editable=False, max_digits=14, verbose_name="Total Cost")),
                ("profit", models.DecimalField(decimal_places=2, default=0, editable=False, max_digits=14, verbose_name="Profit")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("recorded_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recorded_sales", to=settings.AUTH_USER_MODEL, verbose_name="Recorded By")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="daily_sales", to="tracker.masterproduct")),
                ("shop", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales", to="tracker.shop")),
            ],
            options={
                "ordering": ["-sale_date", "-id"],
                "indexes": [models.Index(fields=["shop", "-sale_date"], name="dailysale_shop_date_idx")],
            },
        ),
        migrations.CreateModel(
            name="ShopProduct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cost_price", models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))], verbose_name="Cost Price")),
                ("selling_price", models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))], verbose_name="Selling Price")),
                ("is_active", models.BooleanField(default=True, help_text="Untick to hide the product from this shop.", verbose_name="Active")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shop_prices", to="tracker.masterproduct")),
                ("shop", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shop_products", to="tracker.shop")),
            ],
            options={
                "ordering": ["shop__name", "product__name", "product__category"],
                "constraints": [models.UniqueConstraint(fields=("shop", "product"), name="unique_product_per_shop")],
            },
        ),
        migrations.RunPython(import_legacy_data, migrations.RunPython.noop),
        migrations.RunPython(drop_legacy_tables, migrations.RunPython.noop),
    ]
