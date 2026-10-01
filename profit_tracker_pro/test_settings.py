"""Test settings: run the suite against in-memory SQLite.

Importing ``profit_tracker_pro.settings`` reads ``DATABASE_URL`` from the
environment (or ``.env``), which points at the hosted PostgreSQL database.
Django's test runner would then create its scratch ``test_*`` database there,
so this module swaps in a local SQLite database instead:

    DJANGO_SETTINGS_MODULE=profit_tracker_pro.test_settings python manage.py test
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
