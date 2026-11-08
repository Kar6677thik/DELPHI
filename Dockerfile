# DELPHI image (docs/23 §2): one image, run as `api`, `worker` or a Job (`migrate`, `seed`).
# The repo's layout is kept under /app, so the code finds migrations, the KB and the built
# frontend at the same relative paths as in a checkout. Base images are pinned by digest.

# ---------------------------------------------------------------------------- frontend build
FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS frontend
WORKDIR /src/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
# The build reads the API contract for its generated types.
COPY backend/openapi.json /src/backend/openapi.json
RUN pnpm build

# ---------------------------------------------------------------------------- python runtime
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv

# libgomp1: LightGBM's OpenMP runtime (sensing, M3).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 delphi \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin delphi

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/backend/.venv/bin:$PATH

WORKDIR /app/backend
# Dependencies first, so code changes reuse this layer.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev \
    && rm /usr/local/bin/uv
COPY kb /app/kb
COPY --from=frontend /src/frontend/dist /app/frontend/dist

ARG DELPHI_VERSION=dev
ENV DELPHI_VERSION=${DELPHI_VERSION}
LABEL org.opencontainers.image.source="https://github.com/Kar6677thik/DELPHI" \
      org.opencontainers.image.revision="${DELPHI_VERSION}" \
      org.opencontainers.image.title="delphi" \
      org.opencontainers.image.licenses="NONE"

# Read-only root filesystem at runtime: nothing below is written; /tmp is an emptyDir.
USER 10001:10001
EXPOSE 8000 9000
ENTRYPOINT ["delphi"]
CMD ["api"]
