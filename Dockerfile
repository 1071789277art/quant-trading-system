FROM python:3.11-slim

WORKDIR /app

# Install curl (needed for live_fetcher subprocess calls) + sqlite3
RUN apt-get update && apt-get install -y --no-install-recommends curl sqlite3 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directories (Render Disk will mount over /app/data)
RUN mkdir -p /app/data/cache /app/data/daily_state /app/data/paper_state

EXPOSE 8050

# Single worker + threads: keeps in-memory trading state shared across requests
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8050} --workers 1 --threads 4 --timeout 120 'dashboard.app:create_app()'"]
