import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost").split(",")]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.projects",
    "apps.imports",
    "apps.metrics",
    "apps.worklog",
    "apps.topvisor",
    "apps.yandex",
    "apps.reports",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

if os.getenv("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "report"),
            "USER": os.getenv("POSTGRES_USER", "report"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "report"),
            "HOST": os.getenv("POSTGRES_HOST", "db"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/projects/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TOPVISOR_USER_ID = os.getenv("TOPVISOR_USER_ID", "")
TOPVISOR_API_KEY = os.getenv("TOPVISOR_API_KEY", "")
TOPVISOR_API_BASE_URL = os.getenv("TOPVISOR_API_BASE_URL", "https://api.topvisor.com/v2/json")
TOPVISOR_REQUEST_TIMEOUT_SECONDS = float(os.getenv("TOPVISOR_REQUEST_TIMEOUT_SECONDS", "15"))
TOPVISOR_MAX_RETRIES = int(os.getenv("TOPVISOR_MAX_RETRIES", "3"))

YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")
YANDEX_REDIRECT_URI = os.getenv("YANDEX_REDIRECT_URI", "")
YANDEX_OAUTH_AUTHORIZE_URL = os.getenv(
    "YANDEX_OAUTH_AUTHORIZE_URL", "https://oauth.yandex.ru/authorize"
)
YANDEX_OAUTH_TOKEN_URL = os.getenv("YANDEX_OAUTH_TOKEN_URL", "https://oauth.yandex.ru/token")
YANDEX_METRIKA_API_BASE_URL = os.getenv(
    "YANDEX_METRIKA_API_BASE_URL", "https://api-metrika.yandex.net"
)
YANDEX_WEBMASTER_API_BASE_URL = os.getenv(
    "YANDEX_WEBMASTER_API_BASE_URL", "https://api.webmaster.yandex.net/v4"
)
YANDEX_REQUEST_TIMEOUT_SECONDS = float(os.getenv("YANDEX_REQUEST_TIMEOUT_SECONDS", "15"))
YANDEX_MAX_RETRIES = int(os.getenv("YANDEX_MAX_RETRIES", "3"))
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
