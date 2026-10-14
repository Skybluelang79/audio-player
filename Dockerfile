# Production image for the Auralis Django backend.
#
# Koyeb builds this with its Dockerfile builder. Python and system deps are
# pinned here rather than left to a buildpack, so the runtime matches what the
# project was developed against. Postgres lives on Neon and media on Cloudflare
# R2 (see .env.example), so the image itself is stateless.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is cached across source-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs with DEBUG unset (False), so this builds the hashed WhiteNoise manifest
# into STATIC_ROOT. It reads no env vars and never touches the database.
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Koyeb provides $PORT (8000 here); migrate on boot because the external
# database is only reachable at runtime, then hand off to gunicorn.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py ensure_superuser && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120"]
