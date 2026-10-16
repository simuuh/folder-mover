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

COPY app.py config.py scanner.py mover.py history.py i18n.py gunicorn.conf.py ./
COPY templates templates
COPY static static
COPY translations translations

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /config /downloads /movies /series

# The entrypoint fixes /config ownership and then runs the app as PUID:PGID
# (set them to the owner of your download/media folders).
ENV PUID=1000 PGID=1000

VOLUME /config
EXPOSE 8080

# Follows FOLDER_MOVER_PORT, like gunicorn.conf.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (os.environ.get('FOLDER_MOVER_PORT') or '8080'), timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
