FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12.15-slim-bookworm
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY sci ./sci
COPY server ./server
RUN uv sync --frozen --extra server --no-dev
COPY deploy ./deploy
COPY web ./web
RUN useradd --uid 10001 --create-home publisher
USER publisher
EXPOSE 8000
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
