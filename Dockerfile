FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install requests beautifulsoup4

# Copy scripts
COPY over25_predictor.py /app/
COPY daily_runner.py /app/

# Run daily at 8 AM (using cron inside container)
RUN apt-get update && apt-get install -y cron
RUN echo "0 8 * * * cd /app && python3 daily_runner.py >> /app/cron.log 2>&1" | crontab -

# Start cron
CMD ["cron", "-f"]
