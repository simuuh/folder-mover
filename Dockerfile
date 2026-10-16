FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FOLDER_MOVER_CONFIG=/config/config.yaml \
    FOLDER_MOVER_HISTORY=/config/history.jsonl \
    FOLDER_MOVER_DOWNLOAD_DIR=/downloads \
    FOLDER_MOVER_MOVIES_DIR=/movies \
    FOLDER_MOVER_SERIES_DIR=/series

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py config.py scanner.py mover.py history.py gunicorn.conf.py ./
COPY templates templates
COPY static static

# Runs as 1000:1000 by default; override with `user:` in compose to match the
# owner of your download/media folders. /config must be writable by that user.
RUN mkdir -p /config /downloads /movies /series && chown 1000:1000 /config
USER 1000:1000

VOLUME /config
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
