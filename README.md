# Task Manager API

A task management REST API built with FastAPI, SQLModel, PostgreSQL and Redis. Creating a task also publishes a `task.created` event, which a separate worker consumes.

## Features

- CRUD endpoints for tasks
- Redis caching for `GET /tasks` and `GET /tasks/{id}`. If Redis is down, the API falls back to the database
- Same response format for every endpoint, including errors
- `task.created` events saved to an outbox table in the same transaction as the task, then sent to a Redis Stream, so no event is lost if Redis is down
- Worker process that reads the stream through a consumer group, skips duplicates, retries failures and moves broken events to a dead-letter stream
- Request logging and an `X-Process-Time` header
- Alembic migrations, applied automatically in Docker

## Setup

```bash
cp .env.example .env
docker compose up -d --build
```

This starts Postgres, Redis, the API and the worker. Migrations run before the API starts.

| Service | URL |
|---|---|
| API | http://localhost:8010 |
| Swagger docs | http://localhost:8010/docs |
| Cached keys | http://localhost:8010/cache |

```bash
docker compose logs -f app      # API logs
docker compose logs -f worker   # worker logs
docker compose down             # stop (add -v to delete the database)
```

If a port is already in use, stop the other process or change `APP_PORT` in `.env`.

## Configuration

Settings come from `.env`. The main ones:

| Variable | Description | Default |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Postgres credentials | see `.env.example` |
| `DATABASE_URL`, `REDIS_URL` | Connection strings for running the app locally (Docker overrides them) | localhost |
| `CACHE_TTL` | Cache expiry in seconds | `60` |
| `APP_PORT` | API port on the host | `8010` |
| `EVENT_STREAM` | Stream that events go to | `events:tasks` |
| `CONSUMER_GROUP` | Consumer group used by the worker | `task-workers` |
| `CONSUMER_MAX_DELIVERIES` | Tries before an event goes to the dead-letter stream | `3` |

The outbox and consumer have more optional settings (poll interval, batch size, retention and so on). They are all in `app/config.py` with defaults.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/tasks` | Create a task (publishes `task.created`) |
| GET | `/tasks` | List tasks |
| GET | `/tasks/{id}` | Get one task |
| PUT | `/tasks/{id}` | Update a task |
| DELETE | `/tasks/{id}` | Delete a task |
| GET | `/cache` | Show cached keys |

`status` is `pending` (default), `in_progress` or `done`. Errors return `404`, `422` or `500`.

```json
{ "success": true, "message": "Task created", "data": { "id": "..." }, "errors": null }
```

```bash
curl -X POST localhost:8010/tasks -H 'content-type: application/json' \
  -d '{"title": "Write docs", "status": "in_progress"}'
```

## Events

Only task creation publishes an event.

1. `POST /tasks` saves the task and an `outbox_event` row in one transaction. The request never talks to Redis for events.
2. A background relay (`app/outbox.py`) sends pending rows to the `events:tasks` stream. If Redis is down, it keeps retrying with a growing delay (2s up to 60s).
3. If an event itself is broken, it is marked `dead` after a few tries and does not block the others.

Delivery is at-least-once, so the same event can arrive twice. The worker handles this with the `event_id`.

Each stream entry has two fields: `event_type` and `payload` (a JSON string with the task details).

## Worker

The worker runs as its own service (`python -m app.worker`). For each event it:

1. Reads it through the consumer group `task-workers`.
2. Skips it if the `event_id` was already handled.
3. Handles it (currently it only logs the event) and acknowledges it.
4. Retries it if handling fails. After `CONSUMER_MAX_DELIVERIES` tries it moves to the dead-letter stream `events:tasks:dead`.

Invalid events (bad JSON, missing `event_id` or `event_type`, unknown type) skip the retries and go straight to the dead-letter stream. Each dead-letter entry has the original fields plus `error` and `original_id`.

## Checking events with RedisInsight

Install [RedisInsight](https://redis.io/insight/) (desktop app) and add a database with host `localhost` and port `6379`. Redis is already exposed by `docker-compose.yml`, so nothing else is needed.

- **Browser** tab: open `events:tasks` to see each event with its `event_type` and `payload`. Open `events:tasks:dead` for failed events (they also have `error` and `original_id`).
- **Consumer Groups** tab on `events:tasks`: shows the group `task-workers`, its consumers and pending events.
- **Workbench** tab runs Redis commands:

```
XREVRANGE events:tasks + - COUNT 5      # latest events
XRANGE events:tasks:dead - +            # failed events
XINFO GROUPS events:tasks               # consumer group status
XPENDING events:tasks task-workers      # read but not yet acknowledged
```

Events the relay could not send are marked `dead` in the database:

```bash
docker compose exec db psql -U postgres -d tasks -c "select event_type, status, last_error from outbox_event where status='dead';"
```

Do not set a TTL on a stream key in RedisInsight. Redis deletes the whole stream when it expires.

## Running locally

Start Postgres and Redis (for example `docker compose up -d db redis`), then:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8010
python -m app.worker        # in a second terminal
```

## Migrations

After changing a model:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Review the generated file before committing it.

## Tests

Tests use a separate database whose name must end in `_test`:

```bash
docker compose exec db psql -U postgres -c "create database tasks_test;"
pip install -r requirements-dev.txt
pytest
```

## Project structure

```
app/
  main.py         app setup, middleware, routers, logging
  config.py       settings
  cache.py        Redis cache helpers
  events.py       saves events to the outbox
  outbox.py       relay that sends outbox events to the stream
  consumer.py     stream consumer (dedupe, retry, dead-letter)
  worker.py       worker entrypoint
  exceptions.py   error handlers
  middleware.py   request logging and timing
  models/         database models
  schemas/        request and response schemas
  routers/        tasks and cache endpoints
alembic/          migrations
tests/            tests
```
