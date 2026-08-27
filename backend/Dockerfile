FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
RUN pip install --no-cache-dir .

# Release identity: passed at build time (see docs/deploy.md), surfaced by /health.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

EXPOSE 8000

# Honor $PORT so the same image serves docker-compose (defaults to 8000) and managed
# hosts like Railway (which inject $PORT). Shell form + healthcheck both read it (AL-26).
#
# `app.serve` rather than the `uvicorn` CLI, so logging is configured before uvicorn's first
# line. The CLI logs "Started server process" before the app's lifespan runs, which under
# LOG_JSON=true put two plain-text lines on a JSON stream — and they are the two an operator
# reads when a box will not boot (GRPH-33). It reads $PORT and $HOST itself.
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{p}/health').status==200 else 1)"

CMD ["python", "-m", "app.serve"]
