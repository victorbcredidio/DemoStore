# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG DEMO_RELEASE_ID=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEMO_RELEASE_ID=${DEMO_RELEASE_ID} \
    DEMO_APP_DIR=/opt/demostore/app \
    DEMO_DATA_DIR=/var/lib/demostore \
    DEMO_DATABASE=/var/lib/demostore/lab.sqlite3 \
    DEMO_PORT=8000 \
    DEMO_BASE_URL=http://127.0.0.1:8000 \
    DEMO_TRAFFIC_BACKEND=process

WORKDIR /opt/demostore

# Exact, tested dependency versions.
COPY requirements.lock.txt .
RUN pip install -r requirements.lock.txt

# Unprivileged runtime account; the data directory holds SQLite state and the session key.
RUN groupadd --system --gid 10001 demostore \
 && useradd --system --uid 10001 --gid demostore --home-dir /var/lib/demostore \
      --no-create-home --shell /usr/sbin/nologin demostore \
 && install -d -o demostore -g demostore -m 700 /var/lib/demostore

COPY app/ app/
COPY docker/entrypoint.py /usr/local/bin/demostore-entrypoint
RUN printf '#!/bin/sh\nexec python /opt/demostore/app/demostore_cli.py "$@"\n' > /usr/local/bin/demostore \
 && chmod 755 /usr/local/bin/demostore /usr/local/bin/demostore-entrypoint

USER demostore
WORKDIR /opt/demostore/app
EXPOSE 8000
VOLUME ["/var/lib/demostore"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('DEMO_PORT', '8000'), timeout=4)"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/usr/local/bin/demostore-entrypoint"]
