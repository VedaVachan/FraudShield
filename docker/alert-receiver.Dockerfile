FROM fraudshield-api:local

COPY backend/alert_receiver.py ./backend/alert_receiver.py

EXPOSE 8030

CMD ["python", "-m", "uvicorn", "backend.alert_receiver:create_app", "--factory", "--host", "0.0.0.0", "--port", "8030", "--workers", "1", "--no-access-log"]
