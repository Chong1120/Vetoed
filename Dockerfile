# Read-only dashboard image.
#
# This deploys the JOURNAL VIEWER only - never the trading agent. The agent
# needs live Alpaca credentials and must run on a trusted machine; this
# container holds no keys, opens no broker connection, and has no route that
# can place, cancel, or modify an order.
#
# dashboard/api.py imports `agent.journal`, which is pure standard library
# (sqlite3, json, os, datetime). So the image needs no Alpaca SDK, no
# anthropic, and no MCP client - just FastAPI and uvicorn.
#
#   docker build -t vetoed-dashboard .
#   docker run -p 7860:7860 vetoed-dashboard

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Pinned to what the project was built against. Kept as an explicit list
# rather than `pip install .[dashboard]` so the image never pulls the trading
# dependencies.
RUN pip install --no-cache-dir \
        "fastapi>=0.141.1" \
        "uvicorn[standard]>=0.52.4"

# Only what the viewer actually reads. Note the absence of .env, agent/data.py,
# agent/executor.py and agent/brain.py - nothing that could reach a broker.
COPY agent/__init__.py   agent/__init__.py
COPY agent/journal.py    agent/journal.py
COPY dashboard/          dashboard/
COPY journal/trades.db   journal/trades.db

# Run unprivileged.
RUN useradd --create-home --uid 1000 viewer && chown -R viewer:viewer /app
USER viewer

# Hugging Face Spaces expects 7860; Render and Railway inject $PORT.
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn dashboard.api:app --host 0.0.0.0 --port ${PORT:-7860}"]
