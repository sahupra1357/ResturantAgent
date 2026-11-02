# Restaurant Ops Agents: one image with the built UI and the Python backend.
#   docker compose up -d            # app + Redis, data in the `ro-data` volume
# The first start seeds the demo database and trains the forecast models (a minute or two).

# ---- 1. build the React UI
FROM node:20.18-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Python runtime
FROM python:3.12-slim-bookworm AS app
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    LIVE_HOST=0.0.0.0 LIVE_PORT=8000 LOG_JSON=true
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY restaurant_ops/ restaurant_ops/
COPY --from=ui /ui/dist frontend/dist
RUN useradd --create-home --uid 10001 ro && mkdir -p data && chown -R ro /app/data
USER ro
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"
CMD ["/app/.venv/bin/python", "-m", "restaurant_ops.live.server"]
