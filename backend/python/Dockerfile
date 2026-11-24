FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SYRINX_DISABLE_LOCAL_FS=1 \
    PORT=8080

RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY syrinx_backend ./syrinx_backend

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

EXPOSE 8080

CMD ["sh", "-c", "uvicorn syrinx_backend.api.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
