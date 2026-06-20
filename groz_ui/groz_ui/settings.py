from pathlib import Path
import sys, os
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent   # croz_bot/
load_dotenv(PROJECT_ROOT / '.env')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'groz-django-secret-key-change-in-production')
MODEL_CONFIG_ENCRYPTION_KEY = os.getenv('MODEL_CONFIG_ENCRYPTION_KEY', SECRET_KEY)
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'catalog',
    'chatbot_app',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'groz_ui.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
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

WSGI_APPLICATION = 'groz_ui.wsgi.application'
postgres_url = os.getenv('POSTGRES_URL')
if not postgres_url:
    raise RuntimeError('POSTGRES_URL must be set in .env')

parsed_db_url = urlparse(postgres_url)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': parsed_db_url.path.lstrip('/'),
        'USER': unquote(parsed_db_url.username or ''),
        'PASSWORD': unquote(parsed_db_url.password or ''),
        'HOST': parsed_db_url.hostname or 'localhost',
        'PORT': parsed_db_url.port or 5432,
    }
}

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Add croz_bot root to path so rag_pipeline & vision_pipeline are importable
sys.path.insert(0, str(PROJECT_ROOT))

# Upload dir for PDFs
MEDIA_ROOT = PROJECT_ROOT / 'input'
MEDIA_URL  = '/media/'

LOGIN_URL          = '/admin-panel/login/'
LOGIN_REDIRECT_URL = '/admin-panel/'
LOGOUT_REDIRECT_URL = '/'
