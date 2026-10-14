FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
RUN pip install --no-cache-dir uv

# зависимости отдельным слоем — пересобираются только при смене uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# база SQLite и загруженные медиа — в томе ./data
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; p='/'+os.getenv('WEB_PREFIX','').strip().strip('/'); urllib.request.urlopen('http://127.0.0.1:%s%s/api/health' % (os.getenv('WEB_PORT','8080'), p.rstrip('/')), timeout=4)" || exit 1

CMD [".venv/bin/python", "-m", "app"]
