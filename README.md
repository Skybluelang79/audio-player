# Auralis (Audio Player)

A full-featured Django + DRF music streaming platform with a vanilla JS web player, HLS streaming, playlists, analytics, and an artist studio/dashboard.

## Quick Start

### Prerequisites
- Python 3.13+
- ffmpeg (optional, for HLS packaging)
- Redis (optional, for caching)

### Setup
`ash
cd "Audio Player"
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
`

### Tests
`ash
pytest
`

## API
- Swagger: /api/docs/
- Redoc: /api/redoc/
- Schema: /api/schema/

## Deployment

This is a Django WSGI app requiring: PostgreSQL/SQLite, persistent media storage (or S3/R2), and env vars (see .env.example).

### Recommended Platforms
- **Koyeb**: Uses included Dockerfile (collectstatic + migrate + gunicorn). Set env vars; PORT provided.
- **Render**: Can use Docker or buildpack. Original blueprint in .kilo/worktrees/little-carob/render.yaml.
- **Railway/Fly.io**: Deploy from Dockerfile.

### About Netlify
Netlify is for static sites/serverless functions. Running full Django directly on Netlify is not straightforward as-is.

## Tech Stack
- Django 5.2, DRF 3.18, drf-spectacular, SimpleJWT
- django-filter, django-cors-headers, whitenoise, gunicorn
- Vanilla JS frontend + Django templates
- HLS (ffmpeg), Pillow, django-storages/boto3
