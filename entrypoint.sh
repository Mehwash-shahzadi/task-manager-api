#!/bin/sh
set -e

# Apply migrations, then start the server
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
