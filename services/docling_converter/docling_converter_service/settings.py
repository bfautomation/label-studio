import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DOCLING_CONVERTER_SECRET_KEY', 'docling-converter-dev-secret')
DEBUG = os.environ.get('DOCLING_CONVERTER_DEBUG', '0') == '1'
ALLOWED_HOSTS = ['*']
ROOT_URLCONF = 'docling_converter_service.urls'
WSGI_APPLICATION = 'docling_converter_service.wsgi.application'
USE_TZ = True
TIME_ZONE = 'UTC'

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'rest_framework',
    'converter_api',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [],
}

DOCLING_CONVERTER_API_TOKEN = os.environ.get('DOCLING_CONVERTER_API_TOKEN', '').strip()
DOCLING_CONVERTER_AUTH_HEADER_NAME = (
    os.environ.get('DOCLING_CONVERTER_AUTH_HEADER_NAME')
    or os.environ.get('LABEL_STUDIO_DOCUMENT_CONVERTER_AUTH_HEADER_NAME')
    or 'Authorization'
).strip()
DOCLING_CONVERTER_AUTH_HEADER_VALUE = (
    os.environ.get('DOCLING_CONVERTER_AUTH_HEADER_VALUE')
    or os.environ.get('LABEL_STUDIO_DOCUMENT_CONVERTER_AUTH_HEADER_VALUE')
    or ''
).strip()

if not DOCLING_CONVERTER_AUTH_HEADER_VALUE and DOCLING_CONVERTER_API_TOKEN:
    DOCLING_CONVERTER_AUTH_HEADER_VALUE = f'Token {DOCLING_CONVERTER_API_TOKEN}'

DOCLING_CONVERTER_MAX_FILE_SIZE = int(os.environ.get('DOCLING_CONVERTER_MAX_FILE_SIZE', 262144000))
