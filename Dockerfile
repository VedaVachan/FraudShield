FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src:/app \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-api.lock.txt ./

RUN python -m pip install --no-cache-dir --only-binary=:all: \
        --no-deps -r requirements-api.lock.txt \
    && python -m pip check

COPY backend/app.py ./backend/app.py
COPY backend/metrics.py ./backend/metrics.py
COPY backend/drift.py ./backend/drift.py
COPY reports/drift_reference.json ./reports/drift_reference.json
COPY src/fraudshield/features.py ./src/fraudshield/features.py
COPY src/fraudshield/explanations.py ./src/fraudshield/explanations.py

RUN mkdir -p /app/artifacts/xgboost_v1 /app/reports

USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

