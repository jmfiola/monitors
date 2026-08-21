# One image recipe for every app in the workspace.
#
#   docker build --platform linux/amd64 --build-arg APP=melanzana -t <tag> .
#
# Deliberately NOT one Dockerfile per app: the uid-1000 user below is required by
# the host's volume ownership, and a per-app copy makes omitting it easy and its
# absence quiet.
FROM python:3.13-slim AS build
ARG APP

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

# COMPILE_BYTECODE so the container does not pay import compilation on every
# restart; LINK_MODE=copy because the cache and the venv are on different layers.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, then sources: a code change must not re-resolve dependencies.
# Every member's manifest is copied because the workspace root declares them all
# and uv resolves the whole graph even when installing one package.
COPY pyproject.toml uv.lock ./
COPY lib/monitor/pyproject.toml lib/monitor/
COPY apps/ apps/
RUN find apps -mindepth 2 -not -name pyproject.toml -not -type d -delete || true
RUN uv sync --frozen --no-dev --package "$APP" --no-install-workspace

COPY lib/monitor lib/monitor
COPY apps/ apps/
RUN uv sync --frozen --no-dev --package "$APP"


FROM python:3.13-slim
ARG APP

# uid 1000 to match the host volume. The startup script chowns
# /var/lib/<app>-data to 1000:1000 — it was written for the node image's built-in
# uid-1000 user, and python:3.13-slim has no such user. Without this, /data is
# unwritable: the monitor still polls and alerts, but every tick logs a state-write
# failure and a restart silently re-baselines everything currently open.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown 1000:1000 /data

COPY --from=build --chown=1000:1000 /app /app

# PYTHONUNBUFFERED so stdout reaches `docker logs` and the Cloud Logging agent as
# it happens. Without it a crashed container can lose its last words to a buffer.
# APP is promoted to ENV so the CMD below can expand it at runtime.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP=${APP}

USER 1000
VOLUME ["/data"]

# `exec` so python replaces the shell as PID 1 and receives SIGTERM directly —
# without it systemd's stop signal goes to /bin/sh and the app is killed instead of
# shut down. Shell form is required because $APP must expand at runtime.
# No EXPOSE and no health port: the health server is not ported.
CMD ["sh", "-c", "exec python -m $APP.main"]
