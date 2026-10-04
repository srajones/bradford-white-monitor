# bwwatch - Bradford White Wave fault watcher.
# Python standard library only: there is nothing to pip-install, so nothing to go stale or be hijacked.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data

# The service runs as this user. The entrypoint starts as root only long enough to hand the (possibly
# freshly created, root-owned) ./data folder to it, then drops privileges for good.
RUN groupadd --system --gid 10001 bwwatch \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin bwwatch \
 && install -d -o 10001 -g 10001 -m 0700 /data

WORKDIR /app
COPY bwwatch/ /app/bwwatch/
RUN python -m compileall -q /app/bwwatch \
 && printf '#!/bin/sh\nexec python -m bwwatch "$@"\n' > /usr/local/bin/bwwatch \
 && chmod 0755 /usr/local/bin/bwwatch

ENTRYPOINT ["bwwatch"]
CMD ["run"]
