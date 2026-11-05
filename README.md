# Task Manager API

A small REST API for tasks, built with FastAPI, SQLModel, PostgreSQL and Redis.

When you create a task, the API also publishes a `task.created` event. A separate worker reads those events. Celery runs the background jobs (sending events, cleanup, CSV export, overdue check).

## What it does

- CRUD for tasks, with an optional `due_date`
- Redis cache for `GET /v1/tasks` and `GET /v1/tasks/{id}`. If Redis is down, the API just reads from the database
- Every response, including errors, has the same format
- The event is saved in the same database transaction as the task (an outbox table), then sent to a Redis stream. If Redis is down, nothing is lost
- A stream worker handles each event once, retries failures, and moves broken events to a dead-letter stream
- CSV export runs as a background job. You start it, then poll for the result

## Tech stack

| Part | What I use |
|---|---|
| Web framework | FastAPI |
| Models and DB access | SQLModel |
| Database | PostgreSQL 16 |
| Cache, event stream | Redis 7 (two servers, see below) |
| Background jobs | Celery, Celery Beat, Flower |
| Migrations | Alembic |
| Run everything | Docker Compose |

## Quick start

```bash
cp .env.example .env
docker compose up -d --build
```

That starts Postgres, Redis, the API, the stream worker, the Celery worker, Celery Beat and Flower. Migrations run before the API starts.

| What | URL |
|---|---|
| API | http://localhost:8010 |
| Swagger docs | http://localhost:8010/docs |
| Cached keys | http://localhost:8010/v1/cache |
| Flower (job monitor) | http://localhost:5555 |

Check that everything is up with `docker compose ps`.

Useful commands:

```bash
docker compose logs -f app            # API logs
docker compose logs -f stream-worker  # event worker logs
docker compose logs -f celery-worker  # job logs
docker compose down                   # stop. Add -v to also delete the database, exports and Redis data
```

### If something doesn't work

- **Port already in use.** The API uses `APP_PORT` (8010), Postgres 5433, Redis 6379, the cache Redis 6380 and Flower 5555. Stop whatever else uses the port, or change it in `.env`. `docker ps` shows what is running.
- **App stops at start and names a setting.** That setting is missing from `.env`. There are no defaults in the code, so copy it from `.env.example`.
- **The cache is empty.** It only fills after you call `GET /v1/tasks` or `GET /v1/tasks/{id}`.

## Settings

Everything is read from `.env`. `.env.example` lists every setting with a short comment; this section lists them by the program that uses them. A test fails if this section, `.env.example` and `app/config.py` ever disagree.

### API

Used by: the web API.

| Variable | Meaning | Example |
|---|---|---|
| `API_KEY` | Secret that clients send in the `X-API-Key` header. Every route except `/health` needs it. Make one with `openssl rand -hex 24` | `changeme` |
| `DOCS_ENABLED` | `false` hides `/docs` and `/openapi.json` | `true` |
| `MAX_BODY_BYTES` | Bigger request bodies get `413` | `1000000` |
| `APP_PORT` | Port of the API on your machine (read by Docker Compose, not by `config.py`) | `8010` |
| `FORWARDED_ALLOW_IPS` | Reverse proxies uvicorn trusts for `X-Forwarded-*` headers. Never use `*` in production (read by `entrypoint.sh`) | `127.0.0.1` |

### Database

Used by: the API, the workers and Alembic.

| Variable | Meaning | Example |
|---|---|---|
| `POSTGRES_USER` | Postgres user. The database container uses it too | `postgres` |
| `POSTGRES_PASSWORD` | Postgres password. The database container uses it too | `postgres` |
| `POSTGRES_HOST` | `localhost` when the app runs on your machine, `db` inside Docker (Compose sets it) | `localhost` |
| `POSTGRES_PORT` | `5433` on your machine (the published port), `5432` inside Docker | `5433` |
| `POSTGRES_DB` | Database name. The database container uses it too | `tasks` |

### Redis and cache

Used by: the API and the workers.

| Variable | Meaning | Example |
|---|---|---|
| `REDIS_PASSWORD` | Password of the main Redis (streams, job queue, job ids). Use letters and digits only (read by Docker Compose and used inside the URLs below) | `changeme` |
| `CACHE_REDIS_PASSWORD` | A different password for the cache Redis, so reaching the cache gives no access to the stream or the queue (read by Docker Compose) | `cachesecret` |
| `REDIS_URL` | The Redis server for streams, job ids and health. It never evicts data | `redis://:changeme@localhost:6379/0` |
| `CACHE_REDIS_URL` | The second Redis server, only for the task cache. It may drop old keys when full | `redis://:cachesecret@localhost:6380/0` |
| `REDIS_TIMEOUT` | Seconds to wait for Redis before giving up | `1.0` |
| `REDIS_MAX_CONNECTIONS` | Most connections each Redis client opens | `20` |
| `KEY_PREFIX` | Starts every Redis key we write. Use a different one per environment | `taskapi:dev:` |
| `LIST_CACHE_TTL` | Seconds the cached task list lives | `30` |
| `TASK_CACHE_TTL` | Seconds one cached task lives | `120` |
| `NOT_FOUND_CACHE_TTL` | Seconds an unknown task id is remembered (a cached `404`) | `10` |

### Events: outbox relay

Used by: the Celery `relay_outbox` and `cleanup_outbox` jobs.

| Variable | Meaning | Example |
|---|---|---|
| `EVENT_STREAM` | Name of the Redis Stream that events go to | `events:tasks` |
| `EVENT_STREAM_MAXLEN` | Stream length Redis trims to. Unread events past this are lost, so keep it high | `100000` |
| `OUTBOX_POLL_SECONDS` | How often the relay job runs | `2.0` |
| `OUTBOX_BATCH_SIZE` | Outbox rows sent per relay run | `50` |
| `OUTBOX_MAX_BAD_ATTEMPTS` | Tries before an invalid outbox event is marked dead | `5` |
| `OUTBOX_RETENTION_DAYS` | Days sent outbox rows are kept | `7` |

### Events: stream worker

Used by: `python -m app.messaging.worker`.

| Variable | Meaning | Example |
|---|---|---|
| `CONSUMER_GROUP` | Consumer group of the stream worker | `task-workers` |
| `CONSUMER_NAME` | Name of this worker. Give each worker its own. Empty means hostname-pid | empty |
| `CONSUMER_BLOCK_MS` | How long to wait for a new event, in ms | `5000` |
| `CONSUMER_BATCH_SIZE` | Events read at once | `10` |
| `CONSUMER_SEEN_TTL_SECONDS` | How long a handled event id is remembered (repeats are ignored for this long) | `86400` |
| `CONSUMER_MIN_IDLE_MS` | Idle time before a stuck event is taken over, in ms | `60000` |
| `CONSUMER_MAX_DELIVERIES` | Tries before an event goes to the dead-letter stream | `3` |
| `DEAD_STREAM_MAXLEN` | Most events the dead-letter stream keeps | `1000` |

### Celery jobs and export

Used by: the Celery worker, Beat and Flower.

| Variable | Meaning | Example |
|---|---|---|
| `CELERY_BROKER_URL` | Redis database used as the job queue | `redis://:changeme@localhost:6379/1` |
| `CELERY_RESULT_BACKEND` | Redis database where job results are stored | `redis://:changeme@localhost:6379/2` |
| `JOB_RESULT_TTL_SECONDS` | How long a job id and result are kept | `3600` |
| `EXPORT_DIR` | Folder where the CSV export is written (a shared volume in Docker) | `exports` |
| `EXPORT_RETENTION_HOURS` | Export files older than this are deleted | `24` |
| `EXPORT_LOCK_SECONDS` | Only one export runs at a time. If the worker dies, the lock frees itself after this long | `900` |
| `FLOWER_BASIC_AUTH` | Login for the Flower page, as `user:password` | `admin:changeme` |

### Two Redis servers

They need opposite memory rules, so they are separate:

| Service | Holds | When memory is full |
|---|---|---|
| `redis` (512 MB, saved to disk) | event streams, "seen" markers, job ids, Celery queue and results | writes fail with an error. Nothing is deleted (`noeviction`) |
| `redis-cache` (128 MB, no disk) | cached tasks only | drops the least recently used keys (`allkeys-lru`) |

On `redis`, database `0` is the streams and job ids, `1` is the Celery queue and `2` the results. Every key we write starts with `KEY_PREFIX` (for example `taskapi:dev:`), so cached keys look like `taskapi:dev:tasks:v1:<id>`. Raise `CACHE_VERSION` in `app/cache.py` when the cached task JSON changes shape.

## API

Every route except `/health` lives under `/v1` and needs the header `X-API-Key: <your API_KEY>`. A missing or wrong key gets `401`.

| Method | Path | What it does |
|---|---|---|
| POST | `/v1/tasks` | Create a task and publish `task.created` |
| GET | `/v1/tasks` | List tasks, oldest first. `?limit=50&offset=0` (limit is 1 to 200) |
| GET | `/v1/tasks/{id}` | Get one task |
| PATCH | `/v1/tasks/{id}` | Change some fields of a task (send only what changes). `title` and `status` can't be `null`. `PUT` is not supported |
| DELETE | `/v1/tasks/{id}` | Delete a task. Returns `204` and no body, also if it is already gone |
| POST | `/v1/tasks/export` | Start a CSV export. Returns `202` and a `job_id` |
| GET | `/v1/jobs/{job_id}` | Job status, and the result when it's done |
| GET | `/v1/jobs/{job_id}/download` | Download the CSV of a finished export |
| GET | `/v1/cache` | Show cached keys |
| GET | `/health` | Database, both Redis servers, unsent outbox events, stream backlog and cache hits/misses |

`status` is `pending` (default), `in_progress` or `done`.

`due_date` is optional. Include a time zone, for example `2026-11-05T10:00:00Z`. `overdue_notified_at` is set by the overdue job. You can't set it yourself, and it is cleared when you change `due_date`.

Errors use the same format as everything else, with status `404`, `409`, `413`, `422`, `503` or `500`. Only the first page of `GET /v1/tasks` (the default `limit` and `offset`) is cached.

`POST /v1/tasks` accepts an optional `Idempotency-Key` header (1 to 100 characters). If the same key is sent again with the same body, no second task is created: you get `200` with the first task. If the body is different, you get `409`, because the key was already used for another request. Without the header, every request creates a new task.

```bash
curl -X POST localhost:8010/v1/tasks -H 'content-type: application/json' \
  -H 'X-API-Key: changeme' -H 'Idempotency-Key: write-docs-1' \
  -d '{"title": "Write docs", "status": "in_progress", "due_date": "2026-11-05T10:00:00Z"}'
```

## How events work

Only task creation publishes an event.

1. `POST /v1/tasks` saves the task and an `outbox_event` row in one transaction.
2. The Celery job `relay_outbox` sends pending rows to the `events:tasks` stream. If Redis is down it tries again later, waiting longer each time.
3. The stream worker (`python -m app.messaging.worker`) reads the stream, skips events it has already handled, and acknowledges them.
4. If handling fails, the event is retried. After `CONSUMER_MAX_DELIVERIES` tries it goes to `events:tasks:dead`. Invalid events (bad JSON, missing fields, unknown type) go there straight away.

Delivery is at-least-once, so the worker uses `event_id` to ignore repeats.

**How long repeats are caught:** after an event is handled, the worker remembers its `event_id` for `CONSUMER_SEEN_TTL_SECONDS` (24 hours by default). Inside that time a repeat is ignored. After it, the same event is handled again. For example, a dead-letter event replayed with `python -m app.messaging.replay` more than 24 hours after it was first handled will run the handler a second time, so handlers should be safe to run twice. Raise the setting if you replay later than that.

To look inside the streams, use RedisInsight on `localhost:6379`, or these Redis commands:

```
XREVRANGE events:tasks + - COUNT 5     # latest events
XRANGE events:tasks:dead - +           # failed events
XPENDING events:tasks task-workers     # read but not acknowledged yet
```

If the worker is stopped or too slow, unread events pile up. When the stream passes `EVENT_STREAM_MAXLEN`, Redis deletes the oldest ones. `GET /health` shows `stream_backlog`, and the `watch_stream` job logs an error when it reaches 80% of the limit. Start the worker before that happens.

Once you have fixed whatever made events fail, send the dead ones back with `python -m app.messaging.replay` (100 at a time). It handles both places an event can die:

```bash
python -m app.messaging.replay stream   # dead-letter stream: the worker could not handle them
python -m app.messaging.replay outbox   # dead outbox rows: the relay could not send them
python -m app.messaging.replay          # both (the default)
```

An event that fails `OUTBOX_MAX_BAD_ATTEMPTS` times in the relay is marked dead in the `outbox_event` table (its `last_error` says why). `GET /health` shows `outbox_dead`, the `watch_outbox` job logs an error every minute while any exist, and dead rows older than `OUTBOX_RETENTION_DAYS` are deleted.

Don't put a TTL on a stream key. When it expires, Redis deletes the whole stream.

## Background jobs

Celery Beat decides when a job runs. A Celery worker runs it. Beat only puts a message in the queue. The jobs are in `app/celery_jobs.py` and the schedule is in `app/celery_app.py`.

| Job | When | What it does |
|---|---|---|
| `relay_outbox` | every 2 seconds | Sends pending outbox rows to the stream |
| `cleanup_outbox` | every hour | Deletes sent outbox rows older than `OUTBOX_RETENTION_DAYS` |
| `mark_overdue_tasks` | every minute | Finds tasks past their `due_date` that aren't `done`, logs each once, sets `overdue_notified_at` |
| `export_tasks_csv` | `POST /v1/tasks/export` | Writes all tasks to `EXPORT_DIR/tasks-<job id>.csv` |
| `cleanup_exports` | every hour | Deletes export files older than `EXPORT_RETENTION_HOURS` |
| `watch_stream` | every minute | Logs an error if the event stream is 80% full of unhandled events |

Running a job twice is safe (it can happen after a worker crash). Cells in the CSV that start with `=`, `+`, `-` or `@` get a leading `'` so Excel doesn't run them.

Run exactly **one** `celery-beat`. With two, every job is scheduled twice.

### CSV export

```bash
curl -s -X POST localhost:8010/v1/tasks/export -H 'X-API-Key: changeme'     # 202, gives you a job_id
curl -s localhost:8010/v1/jobs/<job_id> -H 'X-API-Key: changeme'            # PENDING, STARTED, SUCCESS or FAILURE
curl -s -OJ localhost:8010/v1/jobs/<job_id>/download -H 'X-API-Key: changeme'   # when it says SUCCESS
```

Only one export runs at a time. If you ask for another while one runs, you get `202` with the id of the running one (`"Export already running"`), so a loop of requests cannot fill the disk. The lock frees itself when the job ends, or after `EXPORT_LOCK_SECONDS` if the worker dies. The files are in the `exports` volume, shared by the API and the Celery worker. A job id that isn't a UUID gets `422`. An id that was never started, or is older than `JOB_RESULT_TTL_SECONDS`, gets `404`. If Redis is down, starting an export gets `503`.

To run a job right now instead of waiting for Beat:

```bash
docker compose exec app python -c "from app.celery_jobs import cleanup_outbox; print(cleanup_outbox.delay().get(timeout=10))"
```

## Running without Docker for the app

Postgres and Redis still run in Docker.

```bash
docker compose up -d db redis redis-cache
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8010
python -m app.messaging.worker                                    # stream worker, second terminal
celery -A app.celery_app worker -l info --pool=solo     # Celery worker, third terminal
celery -A app.celery_app beat -l info                   # Celery Beat, fourth terminal
```

`--pool=solo` is needed on macOS with Python 3.14, because the default worker pool can't start its child processes there. The Docker image uses Python 3.12 and doesn't need it.

Look at the database:

```bash
docker compose exec db psql -U postgres -d tasks -c '\dt'
docker compose exec db psql -U postgres -d tasks -c 'SELECT * FROM task;'
```

## Migrations

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

Autogenerate doesn't see check constraints, so write those migrations by hand.

## Tests

The tests need their own database, and its name must end in `_test`. At the start of a run the test schema is wiped and rebuilt with `alembic upgrade head`, so the tests also check your migrations. Tables are emptied after every test, and your real data is never touched.

```bash
docker compose exec db psql -U postgres -c "create database tasks_test;"
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
mypy app
```

If you change a model without writing a migration, `tests/test_migrations.py` fails (it runs `alembic check`).

Test files are named by topic (`test_cache.py`, `test_consumer.py`, `test_outbox.py`, `test_auth.py`, ...), and the fake Redis, cache and job classes are all in `tests/fakes.py`. Check the pinned packages for known vulnerabilities with `pip-audit -r requirements.lock --no-deps`.

The Docker image installs `requirements.lock` (every package with an exact version, runtime only). When you change `requirements.txt`, regenerate that file from the versions you tested with.

## Project layout

```
app/
  main.py          app setup, middleware, routers
  config.py        settings (read from .env)
  database.py      engine and session
  cache.py         Redis cache helpers
  redis_client.py  builds the Redis clients (health check, connection limit)
  messaging/       everything about task events:
    publisher.py     saves events to the outbox (add_to_outbox)
    monitor.py       the numbers behind /health and the alert jobs
    outbox.py        sends outbox rows to the stream, cleans old ones
    consumer.py      reads the stream (no repeats, retry, dead-letter)
    replay.py        sends dead-letter events back
    worker.py        stream worker entry point
  celery_app.py    Celery app and Beat schedule
  celery_jobs.py   the Celery jobs
  job_registry.py  export job ids and the "one export at a time" lock
  overdue.py       finds and marks overdue tasks
  middleware.py    body size limit, request logging and timing
  exceptions.py    error handlers
  models/          Task, OutboxEvent
  schemas/         request and response shapes
  routers/         /v1/tasks, /v1/jobs, /v1/cache
alembic/           migrations
tests/             tests
Dockerfile, docker-compose.yml, entrypoint.sh
```
