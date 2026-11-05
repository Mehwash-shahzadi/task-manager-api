#!/bin/sh
set -e

# Apply migrations, then start the server
alembic upgrade head
# --proxy-headers: trust X-Forwarded-For / X-Forwarded-Proto, but only from the proxies
# listed in FORWARDED_ALLOW_IPS (default: this machine). Never use "*" in production.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
