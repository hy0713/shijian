FROM python:3.11.15-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
# CPU-only torch is resolved via the explicit uv index in pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra embedding --no-install-project
COPY shijian ./shijian
COPY migrations ./migrations
COPY alembic.ini ./
COPY data/demo ./data/demo
COPY eval ./eval
RUN uv sync --frozen --no-dev --extra embedding --no-editable --reinstall-package shijian
ENV PATH="/opt/venv/bin:$PATH"
RUN useradd -m app && mkdir -p /app/data/cache /app/data/evaluations && chown -R app:app /app/data
USER app
CMD ["uvicorn", "shijian.api:app", "--host", "0.0.0.0", "--port", "8000"]
