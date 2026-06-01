FROM python:3.11-slim

# System deps for aiosqlite
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/       ./app/
COPY data/      ./data/
COPY sample_events/ ./sample_events/

# Ensure data dir exists for the DB
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV DATABASE_URL=sqlite+aiosqlite:///./data/store_intelligence.db
ENV PORT=8000

EXPOSE 8000

# Use shell form so $PORT env variable from Railway is respected
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
