# Backend container for Data Pipeline Copilot.
# Works on any container host (Hugging Face Spaces, Render, Fly.io, Railway,
# Cloud Run). Binds the platform-provided $PORT, defaulting to 8000 locally.
FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code (see .dockerignore for what's excluded).
COPY . .

ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} is expanded at runtime by the host platform.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
