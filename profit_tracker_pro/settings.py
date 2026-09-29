import os
from pathlib import Path

from django.contrib.messages import constants as messages
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

SECRET_KEY = 'django-insecure-your-secret-key-here'
ALLOWED_HOSTS = ['*']
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_bootstrap5',
    'tracker',
]

JAZZMIN_SETTINGS = {
    "site_title": "Profit Tracker Admin",
    "site_header": "Profit Tracker",
    "site_brand": "Mufasa's Inventory",
    "welcome_sign": "Welcome back, Mufasa!",
    "search_model": ["tracker.MasterProduct", "tracker.DailySale"],
    "show_sidebar": True,
    "navigation_expanded": True,
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.Group": "fas fa-users",
        "tracker.Shop": "fas fa-store",
        "tracker.MasterProduct": "fas fa-box",
        "tracker.ShopProduct": "fas fa-tags",
        "tracker.DailySale": "fas fa-file-invoice-dollar",
    },
    "order_with_respect_to": ["tracker", "auth"],
}

JAZZMIN_UI_CONFIG = {
    "navbar_small_text": False,
    "footer_small_text": False,
    "body_small_text": False,
    "brand_small_text": False,
    "brand_colour": "navbar-dark",
    "accent": "accent-primary",
    "navbar": "navbar-dark",
    "no_navbar_border": False,
    "navbar_fixed": True,
    "layout_boxed": False,
    "footer_fixed": False,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-primary",
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": False,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style": False,
    "theme": "flatly",
    "dark_mode_theme": None,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success"
    }
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'profit_tracker_pro.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'profit_tracker_pro.wsgi.application'

import dj_database_url

# Only talk to the hosted Postgres database when we are actually deployed.
# A DATABASE_URL sitting in .env must not silently redirect local commands
# (migrate, shell, tests) at production data. Set USE_DATABASE_URL=True to
# opt in for a one-off, e.g.
#   $env:USE_DATABASE_URL='True'; python manage.py migrate
# Vercel sets DATABASE_URL itself, so deployments are unaffected.
_use_hosted_db = bool(os.environ.get('VERCEL')) or (
    os.environ.get('USE_DATABASE_URL') == 'True'
)
_database_url = os.environ.get('DATABASE_URL', '') if _use_hosted_db else ''

# On Vercel (serverless) each invocation should NOT keep a persistent
# connection, otherwise idle connections accumulate across containers and
# exhaust Aiven's connection slots. A PgBouncer pool (pgbouncer=true in the
# URL) also requires CONN_MAX_AGE=0, since the pool reuses one physical
# connection across many logical ones and a long-lived Django connection
# would defeat the pooling.
conn_max_age = 0 if (os.environ.get('VERCEL') or 'pgbouncer' in _database_url) else 600

if _database_url:
    # Hosted deployment: parse the URL explicitly rather than letting
    # dj_database_url read DATABASE_URL back out of the environment.
    DATABASES = {
        'default': dj_database_url.parse(_database_url, conn_max_age=conn_max_age)
    }
    DATABASES['default']['ENGINE'] = 'django.db.backends.postgresql'
    DATABASES['default']['CONN_MAX_AGE'] = conn_max_age
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(BASE_DIR / 'db.sqlite3'),
            'OPTIONS': {},
            'AUTOCOMMIT': True,
            'ATOMIC_REQUESTS': False,
            'CONN_MAX_AGE': 0,
            'CONN_HEALTH_CHECKS': False,
            'TIME_ZONE': None,
            'USER': '',
            'PASSWORD': '',
            'HOST': '',
            'PORT': '',
            'TEST': {
                'CHARSET': None,
                'COLLATION': None,
                'MIGRATE': True,
                'MIRROR': None,
                'NAME': None,
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Authentication -------------------------------------------------------
# The tracker ships its own user model so a user carries a role (OWNER or
# MANAGER) and an assigned shop. See tracker.models.CustomUser.
AUTH_USER_MODEL = 'tracker.CustomUser'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# Messages shown after a sale/pricing change are styled by MESSAGE_TAGS so
# the same markup works in base.html regardless of the message level.
MESSAGE_TAGS = {
    messages.DEBUG: 'bg-sky-100 text-sky-800',
    messages.INFO: 'bg-sky-100 text-sky-800',
    messages.SUCCESS: 'bg-emerald-100 text-emerald-800',
    messages.WARNING: 'bg-amber-100 text-amber-800',
    messages.ERROR: 'bg-rose-100 text-rose-800',
}
