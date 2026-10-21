# Task Manager API

A RESTful API for managing tasks, built with **FastAPI**, **SQLModel**, **PostgreSQL**, **Redis** and **Alembic**.

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Configuration](#configuration)
- [Getting Started with Docker](#getting-started-with-docker)
- [Running Locally](#running-locally)
- [API Reference](#api-reference)
- [Database Migrations](#database-migrations)
- [Project Structure](#project-structure)

## Features

- Full CRUD operations for tasks
- Reusable `BaseTable` model (`id`, `created_at`, `updated_at`) and a single `TaskStatus` enum
- Consistent `ApiResponse` format for every response, including errors
- Redis caching for `GET /tasks` and `GET /tasks/{id}`, invalidated on create, update and delete
- `GET /cache` endpoint to inspect cached keys (uses `SCAN`, limited to the `tasks:` prefix)
- Alembic migrations generated from the models and applied automatically in Docker

## Tech Stack

| Component | Technology |
|---|---|
| Web framework | FastAPI |
| ORM / models | SQLModel |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| Migrations | Alembic |
| Containerization | Docker, Docker Compose |

## Configuration

Settings are read from environment variables (`.env`). Create the file from the template:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Credentials for the Postgres container |
| `DATABASE_URL` | Database connection string when running the app locally (default: `localhost:5433`) |
| `REDIS_URL` | Redis connection string when running the app locally (default: `localhost:6379`) |
| `CACHE_TTL` | Cache expiry in seconds (default: `60`) |
| `APP_PORT` | Host port for the API in Docker (default: `8010`) |

Inside Docker Compose, `DATABASE_URL` and `REDIS_URL` are overridden to use the service names `db` and `redis`.

## Getting Started with Docker

Run all commands from the project root.

```bash
cp .env.example .env
docker compose up -d --build
```

This starts Postgres, Redis and the API. Database migrations run automatically before the server starts.

| Resource | URL |
|---|---|
| API | http://localhost:8010 |
| Interactive docs (Swagger UI) | http://localhost:8010/docs |
| Cache entries | http://localhost:8010/cache |

The cache is empty until you call `GET /tasks` or `GET /tasks/{id}`.

Useful commands:

```bash
docker compose ps              # check service status
docker compose logs -f app     # follow API logs
docker compose down            # stop services
docker compose down -v         # stop services and delete the database volume
```

### Troubleshooting

- **Port conflicts:** the API is published on `APP_PORT` (default `8010`), Postgres on `5433` and Redis on `6379`. If another process or Docker stack already uses these ports, stop it or change the values in `.env`. Use `docker ps` to see running containers.
- **Standalone containers:** if you started the local-development containers described below, stop them first: `docker stop task-manager-db task-manager-redis`.

## Running Locally

Run the app on your machine with Postgres and Redis in Docker. Execute everything from the project root so `.env` is found.

**1. Create `.env`**

```bash
cp .env.example .env
```

**2. Create a virtual environment and install dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Start Postgres** (host port `5433`, to avoid clashing with a local Postgres on `5432`)

```bash
docker run -d --name task-manager-db \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tasks \
  -p 5433:5432 -v task_manager_pgdata:/var/lib/postgresql/data \
  postgres:16-alpine
```

Verify it is ready:

```bash
docker exec task-manager-db pg_isready -U postgres -d tasks
```

The matching URL in `.env`:

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/tasks
```

On later runs, start it with `docker start task-manager-db`.

**4. Apply the migration** (creates the `task` table)

```bash
alembic upgrade head
```

**5. Start Redis** (required for caching)

```bash
docker run -d --name task-manager-redis -p 6379:6379 redis:7-alpine
```

**6. Start the API**

```bash
uvicorn app.main:app --reload --port 8010
```

### Inspecting the database

```bash
docker exec task-manager-db psql -U postgres -d tasks -c '\dt'
docker exec task-manager-db psql -U postgres -d tasks -c '\d task'
docker exec task-manager-db psql -U postgres -d tasks -c 'SELECT * FROM task;'
```

### Stopping and resetting

```bash
docker stop task-manager-db task-manager-redis
docker rm -f task-manager-db && docker volume rm task_manager_pgdata   # wipe the database
```

To use your own Postgres or Redis instead of Docker, change `DATABASE_URL` / `REDIS_URL` in `.env`.

## API Reference

| Method | Path | Description | Success code |
|---|---|---|---|
| POST | `/tasks` | Create a task | 201 |
| GET | `/tasks` | List tasks (cached) | 200 |
| GET | `/tasks/{id}` | Get a single task (cached) | 200 |
| PUT | `/tasks/{id}` | Update a task | 200 |
| DELETE | `/tasks/{id}` | Delete a task | 200 |
| GET | `/cache` | View cached entries | 200 |

Errors return the appropriate HTTP status: `404` (not found), `422` (validation error) and `500` (unexpected error).

### Response format

```json
{ "success": true, "message": "Task created", "data": { "id": "..." }, "errors": null }
{ "success": false, "message": "Task not found", "data": null, "errors": ["Task not found"] }
```

### Task status

`status` is one of `pending` (default), `in_progress` or `done`.

### Example

```bash
curl -X POST localhost:8010/tasks -H 'content-type: application/json' \
  -d '{"title": "Write docs", "status": "in_progress"}'
```

## Database Migrations

The initial migration in `alembic/versions/` was generated from the models. After changing a model:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Always review the generated migration file before committing it.

## Project Structure

```
app/
  main.py          application, routers and exception handlers
  config.py        settings loaded from the environment
  database.py      engine and session dependency
  cache.py         Redis helpers (get / set / invalidate)
  enums.py         TaskStatus enum
  exceptions.py    global error handlers
  models/          BaseTable, Task
  schemas/         ApiResponse, TaskCreate/Update/Read, CacheEntry
  routers/         tasks, cache
alembic/           database migrations
```
