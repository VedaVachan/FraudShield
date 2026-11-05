FROM fraudshield-api:local

COPY backend/model_client.py ./backend/model_client.py
COPY backend/canary.py ./backend/canary.py
COPY backend/gateway.py ./backend/gateway.py
COPY backend/gateway_server.py ./backend/gateway_server.py

EXPOSE 8010

CMD ["python", "-m", "uvicorn", "backend.gateway_server:create_gateway", "--factory", "--host", "0.0.0.0", "--port", "8010", "--workers", "1", "--no-access-log"]
