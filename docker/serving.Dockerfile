FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    MLFLOW_TRACKING_URI=file:///tmp/mlruns

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-serving.lock.txt ./

RUN python -m pip install --no-cache-dir --no-deps \
        -r requirements-serving.lock.txt \
    && python -m pip check

USER 10001:10001

EXPOSE 5001

CMD ["python", "-m", "mlflow", "models", "serve", "--model-uri", "/model", "--host", "0.0.0.0", "--port", "5001", "--env-manager", "local", "--workers", "1"]
