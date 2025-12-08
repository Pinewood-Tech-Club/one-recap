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

COPY . .

EXPOSE 5002

CMD ["gunicorn", "-b", "0.0.0.0:5002", "-w", "4", "--access-logfile", "-", "--error-logfile", "-", "--capture-output", "--log-level", "info", "app:app"]
