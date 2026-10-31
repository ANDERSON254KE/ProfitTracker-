import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

SECRET_KEY = 'django-insecure-your-secret-key-here'
ALLOWED_HOSTS = ['*']
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_bootstrap5',
    'tracker',
]

# Site branding shown across the shadcn-styled admin.
ADMIN_SITE_HEADER = "Profit Tracker Admin"
ADMIN_SITE_TITLE = "Profit Tracker Admin"
ADMIN_INDEX_TITLE = "Mufasa's Inventory"

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
        # Project-level overrides win over app templates, so the shadcn
        # admin skin (templates/admin/*) replaces Django's stock admin pages.
        'DIRS': [BASE_DIR / 'templates'],
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

# On Vercel (serverless) each invocation should NOT keep a persistent
# connection, otherwise idle connections accumulate across containers and
# exhaust Aiven's connection slots. A PgBouncer pool (pgbouncer=true in the
# URL) also requires CONN_MAX_AGE=0, since the pool reuses one physical
# connection across many logical ones and a long-lived Django connection
# would defeat the pooling.
_database_url = os.environ.get('DATABASE_URL', '')
conn_max_age = 0 if (os.environ.get('VERCEL') or 'pgbouncer' in _database_url) else 600

DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=conn_max_age,
    )
}

# When on Vercel, ensure the engine is explicitly PostgreSQL if we have a URL
if os.environ.get('DATABASE_URL'):
    DATABASES['default']['ENGINE'] = 'django.db.backends.postgresql'

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
