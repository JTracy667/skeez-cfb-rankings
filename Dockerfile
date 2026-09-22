# Skeez CFB Rankings — Cloudflare Containers image (startup-optimized)
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8003

# Dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Application + data
# NOTE: every top-level module the app imports must be copied here. A missing
# module = ImportError at container boot = every page 500s with
# "Failed to start container". Glob the root *.py so a new module can never be
# forgotten again (app.py, cfbd_shared.py, d1_store.py, d1_write_path.py).
COPY *.py ./
COPY index.html analytics.html schedule.html win_totals.html ./
COPY data/ ./data/
COPY scripts/ ./scripts/

EXPOSE 8003
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003", "--log-level", "warning"]
