# Task Manager API

A RESTful API for managing tasks, built with **FastAPI**, **SQLModel**, **PostgreSQL**, **Redis** and **Alembic**.

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Configuration](#configuration)
- [Getting Started with Docker](#getting-started-with-docker)
- [Running Locally](#running-locally)
- [API Reference](#api-reference)
- [Events](#events)
- [Error Handling and Logging](#error-handling-and-logging)
- [Database Migrations](#database-migrations)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)

## Features

- Full CRUD operations for tasks
- Reusable `BaseTable` model (`id`, `created_at`, `updated_at`) and a single `TaskStatus` enum
- Consistent `ApiResponse` format for every response, including errors, built with the `ok()` and `fail()` helpers
- Redis caching for `GET /tasks` and `GET /tasks/{id}`, invalidated on create, update and delete
- Graceful degradation: if Redis is unavailable the API falls back to the database and logs a warning
- `GET /cache` endpoint to inspect cached keys (uses `SCAN`, limited to the `tasks:` prefix)
- `task.created` event written to an outbox table in the same transaction as the task, then sent to a Redis Stream by a background relay with retry, so events are not lost when Redis is down
- Request logging and timing middleware (`X-Process-Time` response header)
- Centralized exception handlers: unexpected errors are logged once with a full traceback
- Alembic migrations generated from the models and applied automatically in Docker

## Tech Stack

| Component | Technology |
|---|---|
| Web framework | FastAPI |
| ORM / models | SQLModel |
| Database | PostgreSQL 16 |
| Cache and event stream | Redis 7 |
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
| `EVENT_STREAM` | Redis Stream that events are published to (optional, default: `events:tasks`) |
| `EVENT_STREAM_MAXLEN` | Approximate max entries kept in the stream (optional, default: `10000`) |
| `REDIS_TIMEOUT` | Redis connect and read timeout in seconds (optional, default: `1.0`) |
| `OUTBOX_POLL_SECONDS` | How often the relay looks for waiting events (optional, default: `2.0`) |
| `OUTBOX_BATCH_SIZE` | Max events the relay handles per pass (optional, default: `50`) |
| `OUTBOX_MAX_BAD_ATTEMPTS` | Failed tries before a broken event is marked `dead`. Redis outages are never counted (optional, default: `5`) |
| `OUTBOX_RETENTION_DAYS` | Days to keep `sent` events before they are deleted (optional, default: `7`) |
| `TEST_DATABASE_URL` | Database used by the tests. Must end in `_test` (optional, default: `DATABASE_URL` with the database name `tasks_test`) |
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
| POST | `/tasks` | Create a task (publishes `task.created`) | 201 |
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

Every response includes an `X-Process-Time` header with the request duration in seconds.

### Task status

`status` is one of `pending` (default), `in_progress` or `done`.

### Example

```bash
curl -X POST localhost:8010/tasks -H 'content-type: application/json' \
  -d '{"title": "Write docs", "status": "in_progress"}'
```

## Events

Creating a task publishes a `task.created` event to the Redis Stream `events:tasks`. Only the producer exists so far; there are no consumers yet. Update and delete do not publish events.

The event goes through an **outbox**, so it is not lost when Redis is down:

1. `POST /tasks` saves the task and an `outbox_event` row in the same database transaction. The request never talks to Redis for events, so it returns `201` even when Redis is down.
2. A background relay (a thread started by `lifespan` in `app/main.py`, code in `app/outbox.py`) checks the table every `OUTBOX_POLL_SECONDS` and sends due `pending` rows to the stream, then marks them `sent`.
3. If Redis is down (connection error or timeout), the row stays `pending` and is retried with a growing wait of 2, 4, 8, 16, 32, then 60 seconds. This never gives up, however long the outage lasts.
4. If the event itself is broken (any other error), the failure is counted. After `OUTBOX_MAX_BAD_ATTEMPTS` tries the row becomes `dead` and an error is logged. Other events are not blocked.
5. `sent` rows older than `OUTBOX_RETENTION_DAYS` are deleted once an hour.

Delivery is **at-least-once**: after a crash an event can be sent twice, so consumers should ignore an `event_id` they have already seen. Events can arrive up to `OUTBOX_POLL_SECONDS` late and strict ordering is not guaranteed. Several app workers are safe: the relay locks rows with `FOR UPDATE SKIP LOCKED`.

Check the outbox, and retry a `dead` event after fixing the cause:

```bash
docker compose exec db psql -U postgres -d tasks -c "select event_type, status, attempts, bad_attempts, last_error from outbox_event order by created_at desc limit 10;"
docker compose exec db psql -U postgres -d tasks -c "update outbox_event set status='pending', bad_attempts=0, next_attempt_at=now() where status='dead';"
```

Each stream entry has two fields: `event_type` and `payload`, a JSON envelope:

```json
{
  "event_id": "93b0e962-400e-4ea1-a182-381b9471ee3c",
  "event_type": "task.created",
  "occurred_at": "2026-10-26T12:16:54.270643+00:00",
  "source": "task-manager-api",
  "data": {
    "id": "041c3841-8b7e-4c5a-a46d-6209f3261f8b",
    "title": "event test",
    "description": null,
    "status": "pending",
    "created_at": "2026-10-26T12:16:54.223493Z",
    "updated_at": "2026-10-26T12:16:54.223515Z"
  }
}
```

The stream is trimmed to roughly `EVENT_STREAM_MAXLEN` entries. Redis calls time out after `REDIS_TIMEOUT` seconds, so a stalled Redis slows a request by about that long per call instead of hanging it.

Inspect the stream:

```bash
docker compose exec redis redis-cli XRANGE events:tasks - +
docker compose exec redis redis-cli XLEN events:tasks
```

## Error Handling and Logging

Logging is configured once in `app/main.py` (`logging.basicConfig`); every module uses `logging.getLogger(__name__)` and shares that format.

| Layer | File | Responsibility |
|---|---|---|
| Middleware | `app/middleware.py` | Adds `X-Process-Time` and logs one line per request: method, path, status, duration |
| Exception handlers | `app/exceptions.py` | Convert errors into the `ApiResponse` format and log unexpected errors |

```
Request
  -> log_requests / add_process_time_header   (middleware.py)
  -> router endpoint
       |- success ................ 200/201 response, logged as INFO
       |- HTTPException (404) .... http_exception_handler        -> JSON 404
       |- validation error ....... validation_exception_handler  -> JSON 422
       `- unexpected error ....... general_exception_handler     -> logged with traceback (ERROR) -> JSON 500
```

Unexpected errors are logged in one place only, `general_exception_handler`, so each failure produces a single traceback in the logs. Clients receive a generic `Internal server error` message and never see internal details.

## Database Migrations

The initial migration in `alembic/versions/` was generated from the models. After changing a model:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Always review the generated migration file before committing it.

## Running Tests

Tests use a separate database so your development data is never touched. Create it once, then run pytest:

```bash
docker compose exec db psql -U postgres -c "create database tasks_test;"
pip install -r requirements-dev.txt
pytest
```

pytest stops with an error if the test database name does not end in `_test`.

## Project Structure

```
app/
  main.py          application, logging config, middleware, routers and exception handlers
  config.py        settings loaded from the environment
  database.py      engine and session dependency
  cache.py         Redis helpers (get / set / invalidate / list), Redis-failure safe
  events.py        add_to_outbox() saves an event in the caller's transaction
  outbox.py        relay_once() sends outbox events to the Redis Stream with retry, run_relay() loop, cleanup_sent()
  enums.py         TaskStatus enum
  exceptions.py    global error handlers
  middleware.py    request timing and request logging
  models/          BaseTable, Task, OutboxEvent
  schemas/         ApiResponse with ok() / fail() helpers, TaskCreate/Update/Read, CacheEntry
  routers/         tasks, cache
alembic/           database migrations
tests/             pytest tests for the outbox and relay (use the tasks_test database)
```
