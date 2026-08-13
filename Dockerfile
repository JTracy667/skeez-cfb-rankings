# Skeez CFB Rankings — Cloudflare Containers image (startup-optimized)
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8003

# Dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application + data
COPY app.py .
COPY index.html analytics.html schedule.html ./
COPY data/ ./data/
COPY scripts/ ./scripts/

EXPOSE 8003
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003", "--log-level", "warning"]
