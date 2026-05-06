# syntax=docker/dockerfile:1.7
#
# Cache-friendly layout. Every layer above the final ENV block is keyed only
# on file content, so as long as requirements.txt + the source files don't
# change, the whole image rebuilds in seconds. The version/SHA build-args go
# in the LAST ENV layer so a fresh commit only invalidates that final step
# instead of cascading through apt, pip, and every COPY.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STASH_DB=/data/stash.db \
    STASH_UPLOADS=/data/uploads

WORKDIR /app

# apt cache + lists are kept in BuildKit cache mounts so re-installs are fast
# even when this layer's content cache is invalidated. `rm` of /var/lib/apt/lists
# is dropped — the cache mount handles cleanliness without bloating the layer.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends curl

COPY requirements.txt ./
# pip wheel cache persists across builds — when requirements.txt changes, pip
# re-resolves but pulls already-downloaded wheels from the cache instead of
# re-fetching from PyPI.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Source files in roughly stable→volatile order so a frequent CHANGELOG bump
# doesn't invalidate the python file layer.
COPY app.py labels.py vision.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY CHANGELOG.md ./

RUN useradd --system --uid 1000 --home /app stash \
    && mkdir -p /data/uploads \
    && chown -R stash:stash /app /data
USER stash

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ >/dev/null || exit 1

# Version + SHA come last so a new commit only invalidates this layer (cheap
# to rebuild — no file copies, no installs). The OCI labels make the image
# self-describing for `docker inspect`.
ARG VERSION=dev
ARG GIT_SHA=unknown
ENV STASH_VERSION=${VERSION} \
    STASH_GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/Brennan-VanderLaan/stash"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
