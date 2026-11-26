import os
from decimal import Decimal

from django.conf import settings
from django.db import migrations


def flag_missing_products(apps, schema_editor):
    MasterProduct = apps.get_model("tracker", "MasterProduct")
    ShopProduct = apps.get_model("tracker", "ShopProduct")
    path = os.path.join(str(settings.BASE_DIR), "price list.xlsx")
    if not os.path.exists(path):
        return
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        rows = list(workbook.active.iter_rows(min_row=2, values_only=True))
        workbook.close()
    except Exception:
        return

    pairs = set()
    for row in rows:
        if not row or row[0] is None:
            continue
        name = str(row[0]).strip()
        category = str(row[3]).strip() if len(row) > 3 and row[3] is not None else ""
        pairs.add((name, category))

    for product in MasterProduct.objects.all().iterator():
        if (product.name, product.category) not in pairs:
            MasterProduct.objects.filter(pk=product.pk).update(is_manual=True)
            ShopProduct.objects.filter(
                product_id=product.pk,
                is_active=False,
                selling_price=Decimal("0.00"),
                cost_price=Decimal("0.00"),
            ).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0012_masterproduct_is_manual'),
    ]

    operations = [
        migrations.RunPython(flag_missing_products, migrations.RunPython.noop),
    ]
