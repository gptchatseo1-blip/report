FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-crosextra-carlito poppler-utils \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[dev]"
COPY . .

# Build the immutable WhiteNoise manifest into the image. The key is intentionally
# build-only; no production secret is embedded in a layer or required for static collection.
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=collectstatic-build-only python manage.py collectstatic --noinput

RUN mkdir -p /app/media \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --timeout ${GUNICORN_TIMEOUT_SECONDS:-300}"]
