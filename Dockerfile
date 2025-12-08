FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for cryptography/requests-oauthlib if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Persistent data directory for sqlite
RUN mkdir -p /data

COPY . .

EXPOSE 5002

# Use gevent worker to support WebSockets and keep single worker for SQLite.
CMD ["gunicorn", "-b", "0.0.0.0:5002", "-w", "1", "-k", "gevent", "--access-logfile", "-", "--error-logfile", "-", "--capture-output", "--log-level", "info", "app:app"]
