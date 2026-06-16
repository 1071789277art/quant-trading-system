FROM python:3.11-slim

WORKDIR /app

# Install curl (needed for live_fetcher subprocess calls)
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directories
RUN mkdir -p data/cache data/daily_state data/paper_state

EXPOSE 8050

# Railway/Render inject PORT env var; fallback to 8050
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8050} --workers 2 --timeout 120 'dashboard.app:create_app()'"]
