FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/frontend

COPY frontend/index.html frontend/pipeline.html frontend/pipeline_summary.js ./

USER 10001:10001

EXPOSE 8080

CMD ["python", "-m", "http.server", "8080", "--bind", "0.0.0.0", "--directory", "/app/frontend"]
