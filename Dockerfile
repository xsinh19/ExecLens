# syntax=docker/dockerfile:1
# ExecLens runtime image: API by default; the dashboard uses the same image
# (see docker-compose.yml). Dependencies are installed from uv.lock.
# The base image is pinned by digest for reproducible builds; Dependabot
# proposes updates (.github/dependabot.yml) so security fixes still land.

# --- builder: resolve the locked environment into /opt/venv -----------------
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv==0.12.20
WORKDIR /app

# Third-party dependencies first, so this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- runtime: just the environment, the sample data and config --------------
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    EXECLENS_DATA_DIR=data/sample \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

RUN useradd --create-home --uid 10001 execlens
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY data/sample ./data/sample
COPY benchmarks/results.json ./benchmarks/results.json
COPY .streamlit ./.streamlit
RUN chown -R execlens /app

USER execlens
EXPOSE 8000 8501

CMD ["execlens", "serve", "--host", "0.0.0.0", "--port", "8000"]
