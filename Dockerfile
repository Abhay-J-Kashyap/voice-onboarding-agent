FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies are installed first so application edits do not invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY evals ./evals
# Migrations ship with the image so a deploy can bring the schema forward.
COPY alembic ./alembic
COPY alembic.ini .

# Run unprivileged. The container writes only to /srv/data.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/data \
    && chown -R appuser:appuser /srv
USER appuser

ENV DATABASE_URL=sqlite:////srv/data/kyc_agent.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8000/healthz', timeout=2).status_code==200 else 1)"

# Seed before serving so a fresh volume has reference data, and so an existing
# one gets new columns backfilled. This is standing in for migrations; see
# docs/runbook.md.
CMD ["sh", "-c", "python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
