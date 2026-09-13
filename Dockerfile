FROM python:3.11-slim

WORKDIR /app

# Install system deps (cron + ca-certs for requests SSL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (use lockfile if present, fall back to requirements.txt)
COPY requirements.txt requirements.lock* ./
RUN pip install --no-cache-dir -r requirements.lock 2>/dev/null || \
    pip install --no-cache-dir -r requirements.txt

# Copy all source + data files (keep this list in sync with pipeline files)
COPY *.py ./
COPY *.json *.csv *.txt ./ 2>/dev/null || true
COPY .github/workflows/run_daily.yml ./.github/workflows/ 2>/dev/null || true

# Run daily at 8:00 AM UTC — falls back to daily_runner.py
RUN echo "0 8 * * * cd /app && python3 daily_runner.py >> /app/cron.log 2>&1" | crontab -

# Start cron foreground
CMD ["cron", "-f"]
