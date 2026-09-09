# The base image is provided by build.yaml (build_from). Since Supervisor
# 2026.04.0 the BUILD_FROM arg is no longer injected automatically, so we
# declare it explicitly and fall back to a sane default for plain
# `docker build` outside of the Supervisor builder.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.23
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    APP_HOME=/opt/app \
    DATA_DIR=/data

WORKDIR /opt/app

# tzdata so scheduling / "America/Los_Angeles" quota day math is correct.
RUN apk add --no-cache tzdata

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Labels populated by the Supervisor builder.
ARG BUILD_ARCH
ARG BUILD_VERSION
LABEL \
  io.hass.name="Spotify → YouTube Playlist Sync" \
  io.hass.description="One-way authoritative mirror of a Spotify playlist to a YouTube playlist" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.type="addon" \
  io.hass.version="${BUILD_VERSION}"

# The Supervisor watchdog reads this container health status (replaces the
# obsolete `watchdog:` add-on option). busybox `wget` ships in the base image.
HEALTHCHECK --interval=120s --timeout=10s --start-period=45s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:8099/healthz || exit 1

CMD [ "/run.sh" ]
