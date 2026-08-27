FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src CTA_DB_PATH=/data/cta.db
WORKDIR /app
COPY src ./src
RUN mkdir -p /data && useradd --create-home --uid 10001 app && chown -R app:app /data
USER app
EXPOSE 8000
CMD ["python3", "-m", "cta_pipeline", "serve", "--host", "0.0.0.0", "--port", "8000"]
