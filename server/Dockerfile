# SCI publishing service. Build from the repository root:
#   docker build -f server/Dockerfile -t sci-publisher .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY sci ./sci
RUN pip install ".[server]"
COPY server ./server

RUN useradd --system --uid 10001 --no-create-home sci
USER sci

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "server.app", "serve"]
