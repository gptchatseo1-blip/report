import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key")
if not DEBUG and SECRET_KEY in {"", "unsafe-development-key", "replace-with-a-long-random-value"}:
    raise ImproperlyConfigured("Set a strong DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")


def _csv_setting(name, default=""):
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


ALLOWED_HOSTS = _csv_setting("DJANGO_ALLOWED_HOSTS", "localhost")
CSRF_TRUSTED_ORIGINS = _csv_setting("DJANGO_CSRF_TRUSTED_ORIGINS")

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
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.getenv("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))


def positive_int_setting(name, default):
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


REPORT_PDF_TIMEOUT_SECONDS = positive_int_setting("REPORT_PDF_TIMEOUT_SECONDS", 180)
REPORT_ARTIFACT_STALE_SECONDS = positive_int_setting("REPORT_ARTIFACT_STALE_SECONDS", 360)
GUNICORN_TIMEOUT_SECONDS = positive_int_setting("GUNICORN_TIMEOUT_SECONDS", 300)
if GUNICORN_TIMEOUT_SECONDS < REPORT_PDF_TIMEOUT_SECONDS + 60:
    raise ValueError(
        "GUNICORN_TIMEOUT_SECONDS must allow REPORT_PDF_TIMEOUT_SECONDS plus at least "
        "60 seconds for DOCX generation and artifact storage"
    )
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
TOPVISOR_PROJECTS_CACHE_SECONDS = int(os.getenv("TOPVISOR_PROJECTS_CACHE_SECONDS", "300"))

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

# TLS is normally terminated by the reverse proxy. Production deployments must enable
# these switches explicitly after HTTPS and the forwarded-proto header are configured.
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "0") == "1"
SESSION_COOKIE_SECURE = os.getenv("DJANGO_SESSION_COOKIE_SECURE", "0") == "1"
CSRF_COOKIE_SECURE = os.getenv("DJANGO_CSRF_COOKIE_SECURE", "0") == "1"
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", "0") == "1"
SECURE_HSTS_PRELOAD = os.getenv("DJANGO_SECURE_HSTS_PRELOAD", "0") == "1"
if os.getenv("DJANGO_TRUST_X_FORWARDED_PROTO", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
