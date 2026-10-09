"""
Django settings for the Audio Player backend.

Configuration is environment driven so the same image runs against SQLite
(dev) and PostgreSQL (staging/production) without code changes.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    SECRET_KEY=(str, "django-insecure-change-me-in-production"),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1", "[::1]"]),
    # "sqlite" or "postgres"
    DB_ENGINE=(str, "sqlite"),
    JWT_SIGN_IN_LIFETIME_MINUTES=(int, 60),
    JWT_REFRESH_LIFETIME_DAYS=(int, 7),
    CORS_ALLOWED_ORIGINS=(list, []),
    MAX_UPLOAD_SIZE_MB=(int, 50),
    # Streaming is public by default (the site player needs no account), but a
    # deployment can gate it behind a signed token. Downloads default to
    # authenticated-only because they take a permanent copy.
    STREAM_REQUIRE_AUTH=(bool, False),
    DOWNLOADS_REQUIRE_AUTH=(bool, True),
    STREAM_URL_TTL_SECONDS=(int, 3600),
    # Email defaults to the console backend so local development and CI never
    # need an SMTP server; set EMAIL_BACKEND to the SMTP backend in production.
    EMAIL_BACKEND=(str, "django.core.mail.backends.console.EmailBackend"),
    DEFAULT_FROM_EMAIL=(str, "no-reply@auralis.local"),
)

_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(_env_file)

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS", default=env("CORS_ALLOWED_ORIGINS"))

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
APPEND_SLASH = True

AUTH_USER_MODEL = "accounts.User"

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    "rest_framework_simplejwt.token_blacklist",
]

LOCAL_APPS = [
    "accounts.apps.AccountsConfig",
    "music.apps.MusicConfig",
    "playlists.apps.PlaylistsConfig",
    "analytics.apps.AnalyticsConfig",
    "dashboard.apps.DashboardConfig",
    "notifications.apps.NotificationsConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Must sit directly below SecurityMiddleware and above everything else:
    # WhiteNoise sets the HSTS/caching headers that static assets rely on.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

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
                "config.context_processors.site_context",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database - PostgreSQL ready
# ---------------------------------------------------------------------------

if env("DB_ENGINE") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", default="audioplayer"),
            "USER": env("POSTGRES_USER", default="audioplayer"),
            "PASSWORD": env("POSTGRES_PASSWORD", default="audioplayer"),
            "HOST": env("POSTGRES_HOST", default="127.0.0.1"),
            "PORT": env("POSTGRES_PORT", default="5432"),
            "CONN_MAX_AGE": env("POSTGRES_CONN_MAX_AGE", default=60),
            "OPTIONS": {
                "connect_timeout": 10,
            },
            "TEST": {"NAME": env("POSTGRES_TEST_DB", default="test_audioplayer")},
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
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": env("PASSWORD_MIN_LENGTH", default=8)}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static and media files
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

# Outside DEBUG, django.contrib.staticfiles stops serving /static/ itself, so
# WhiteNoise takes over from STATIC_ROOT.
#
# The hashed/compressed backend is only enabled when DEBUG is off. With DEBUG on,
# Django serves assets straight from STATICFILES_DIRS and never touches this
# backend, so hashing 600+ files on every template render would be pure
# overhead (it made the suite ~2x slower and cost minutes across the run).
STORAGES = {
    "default": {
        "BACKEND": env(
            "DEFAULT_FILE_STORAGE", default="django.core.files.storage.FileSystemStorage"
        )
    },
    "staticfiles": {
        "BACKEND": env(
            "STATICFILES_STORAGE",
            default=(
                "django.contrib.staticfiles.storage.StaticFilesStorage"
                if DEBUG
                else "whitenoise.storage.CompressedManifestStaticFilesStorage"
            ),
        )
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))

# Hard cap on a single upload. Guards memory exhaustion on large audio files.
MAX_UPLOAD_SIZE_MB = env("MAX_UPLOAD_SIZE_MB")
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# Signed-URL lifetime for stream links, and the two gates that decide whether
# streaming/downloading needs a signed token or a login.
STREAM_REQUIRE_AUTH = env("STREAM_REQUIRE_AUTH")
DOWNLOADS_REQUIRE_AUTH = env("DOWNLOADS_REQUIRE_AUTH")
STREAM_URL_TTL_SECONDS = env("STREAM_URL_TTL_SECONDS")

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

EMAIL_BACKEND = env("EMAIL_BACKEND")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
# Where a password-reset link points. Defaults to the server-rendered page; set
# it to a SPA route when the front end owns the reset form.
PASSWORD_RESET_URL = env("PASSWORD_RESET_URL", default="")

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "login"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ),
    "DEFAULT_PAGINATION_CLASS": "config.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "config.exceptions.api_exception_handler",
    "DATETIME_FORMAT": "iso-8601",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

if DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ]

SPECTACULAR_SETTINGS = {
    "TITLE": "Audio Player API",
    "DESCRIPTION": (
        "REST API for streaming audio: tracks, artists, albums, genres, "
        "playlists, favourites, play history and play-count analytics."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    "ENUM_NAME_OVERRIDES": {
        # Several models expose a field called "status"/"repeat" with different
        # choice sets. Naming them explicitly keeps generated clients from
        # getting an opaque auto-derived name like "Status374Enum".
        # Paths must point at module-level attributes, not nested TextChoices.
        "TrackStatusEnum": "music.models.TRACK_STATUS_CHOICES",
        "TrackCommentStatusEnum": "music.models.TRACK_COMMENT_STATUS_CHOICES",
        "PlaybackQueueRepeatEnum": "playlists.models.PLAYBACK_QUEUE_REPEAT_CHOICES",
    },
    "TAGS": [
        {"name": "auth", "description": "Registration, JWT issue/refresh and profile."},
        {"name": "tracks", "description": "Track catalogue, uploads and play counting."},
        {"name": "artists", "description": "Artist profiles."},
        {"name": "albums", "description": "Album catalogue."},
        {"name": "genres", "description": "Genre taxonomy."},
        {"name": "playlists", "description": "User playlists and playlist membership."},
        {"name": "favorites", "description": "Favourite tracks per user."},
        {"name": "history", "description": "Per-user listening history."},
        {"name": "queue", "description": "Saved playback queue, restored across sessions."},
        {"name": "recommendations", "description": "Similar and personalised track feeds."},
        {"name": "analytics", "description": "Aggregated play statistics."},
        {"name": "search", "description": "Cross-entity search."},
        {"name": "dashboard", "description": "Staff dashboard metrics."},
    ],
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": True, "displayRequestDuration": True},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env("JWT_SIGN_IN_LIFETIME_MINUTES")),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env("JWT_REFRESH_LIFETIME_DAYS")),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True
CSRF_COOKIE_HTTPONLY = False

# ---------------------------------------------------------------------------
# Cache - local memory by default, Redis in production
# ---------------------------------------------------------------------------

if env("REDIS_URL", default="") or env("CACHE_BACKEND", default="") == "redis":
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": env("REDIS_URL", default="redis://127.0.0.1:6379/1"),
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "audioplayer",
        }
    }

# ---------------------------------------------------------------------------
# Security hardening (activated outside DEBUG)
# ---------------------------------------------------------------------------

SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG

if not DEBUG:
    # Off by default: TLS is usually terminated by a reverse proxy, which sets
    # X-Forwarded-Proto and is handled by SECURE_PROXY_SSL_HEADER below. Turn
    # this on only when Django itself terminates TLS.
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
    SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

MESSAGE_STORAGE = "django.contrib.messages.storage.fallback.CookieStorage"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[{levelname}] {asctime} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}
