# Task Manager API

A task REST API built with FastAPI, SQLModel, PostgreSQL and Redis. Creating a task also publishes a `task.created` event that a separate worker consumes.

## Features

- CRUD endpoints for tasks
- Redis cache for `GET /tasks` and `GET /tasks/{id}`. If Redis is down, the API reads from the database
- Cache invalidation: create, update and delete clear the cached list and that task's key. Items also expire after `CACHE_TTL` seconds
- Same response format for every endpoint, including errors
- Events go to an outbox table in the same transaction as the task, then to a Redis Stream, so none are lost if Redis is down
- Worker with a consumer group that skips duplicates, retries failures and moves broken events to a dead-letter stream

## Setup

```bash
cp .env.example .env
docker compose up -d --build
```

This starts Postgres, Redis, the API and the worker. Migrations run before the API starts.

- API: http://localhost:8010
- Swagger docs: http://localhost:8010/docs
- Cached keys: http://localhost:8010/cache

```bash
docker compose logs -f app      # API logs
docker compose logs -f worker   # worker logs
docker compose down             # stop (add -v to delete the database)
```

## Configuration

Settings are read from `.env`. The main ones:

| Variable | Description | Default |
|---|---|---|
| `CACHE_TTL` | Cache expiry in seconds | `60` |
| `APP_PORT` | API port on the host | `8010` |
| `EVENT_STREAM` | Stream that events go to | `events:tasks` |
| `CONSUMER_GROUP` | Worker consumer group | `task-workers` |
| `CONSUMER_MAX_DELIVERIES` | Tries before an event goes to the dead-letter stream | `3` |

The rest (database, Redis, outbox settings) are in `.env.example` and `app/config.py`.

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

```bash
curl -X POST localhost:8010/tasks -H 'content-type: application/json' \
  -d '{"title": "Write docs", "status": "in_progress"}'
```

## Events and worker

Only task creation publishes an event.

1. `POST /tasks` saves the task and an `outbox_event` row in one transaction.
2. A relay (`app/outbox.py`) sends pending rows to the `events:tasks` stream. If Redis is down it retries with a growing delay.
3. The worker (`python -m app.worker`) reads the stream, skips events it already handled, and acknowledges them.
4. A failed event is retried. After `CONSUMER_MAX_DELIVERIES` tries it goes to `events:tasks:dead`. Invalid events (bad JSON, missing fields, unknown type) go there straight away.

Delivery is at-least-once, so the worker uses `event_id` to ignore duplicates.

To look at the streams, open RedisInsight on `localhost:6379`, or run:

```
XREVRANGE events:tasks + - COUNT 5      # latest events
XRANGE events:tasks:dead - +            # failed events
XPENDING events:tasks task-workers      # read but not acknowledged
```

Don't set a TTL on a stream key. Redis deletes the whole stream when it expires.

## Running locally

```bash
docker compose up -d db redis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8010
python -m app.worker        # in a second terminal
```

## Migrations

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

## Tests

Tests need a separate database whose name ends in `_test`:

```bash
docker compose exec db psql -U postgres -c "create database tasks_test;"
pip install -r requirements-dev.txt
pytest
```

## Project structure

```
app/
  main.py         app setup, middleware, routers
  config.py       settings
  database.py     database engine and session
  cache.py        Redis cache helpers
  events.py       saves events to the outbox
  outbox.py       relay: outbox table -> Redis Stream
  consumer.py     stream consumer (dedupe, retry, dead-letter)
  worker.py       worker entrypoint
  middleware.py   request logging and timing
  exceptions.py   error handlers
  enums.py        task status values
  models/         Task, OutboxEvent
  schemas/        request and response schemas
  routers/        /tasks and /cache endpoints
alembic/          migrations
tests/            consumer, outbox and worker tests
Dockerfile, docker-compose.yml, entrypoint.sh
```
