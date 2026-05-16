FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000

# Production server. --proxy-headers makes Uvicorn trust X-Forwarded-Proto
# from a front-end reverse proxy (Traefik), so request.url.scheme reads as
# "https" and Starlette's SessionMiddleware sets the Secure cookie flag
# correctly once SESSION_HTTPS_ONLY is true.
CMD ["uvicorn", "app.web:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
