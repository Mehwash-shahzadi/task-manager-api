"""Fake stand-ins for Redis, the cache and Celery jobs. Every test file imports them from here."""

from redis.exceptions import ResponseError

from app.cache import LIST_KEY, task_key


class FakeRedis:
    """Stands in for redis_client. Raises `error` (if set) instead of accepting events."""

    def __init__(self):
        self.error = None
        self.calls = []

    def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, fields))
        if self.error is not None:
            raise self.error


class FakePipeline:
    """Queues commands and runs them together on execute(), like a Redis MULTI/EXEC transaction."""

    def __init__(self, redis):
        self.redis = redis
        self.queued = []

    def __getattr__(self, command):
        def queue(*args, **kwargs):
            self.queued.append((command, args, kwargs))
            return self

        return queue

    def execute(self):
        queued, self.queued = self.queued, []
        return [
            getattr(self.redis, command)(*args, **kwargs)
            for command, args, kwargs in queued
        ]


class FakeStreamRedis:
    """Stands in for consumer_redis. Keeps streams, consumer groups, pending entries and keys in memory."""

    def __init__(self):
        self.streams = {}  # stream name -> list of (id, fields)
        self.groups = {}  # (stream, group) -> {"last": index of last delivered entry, "pending": {id: info}}
        self.keys = {}  # key -> (value, ttl)
        self.now_ms = 0
        self._counter = 0

    def advance(self, ms):
        """Move the fake clock forward, so pending entries become idle."""
        self.now_ms += ms

    def entries(self, stream):
        return list(self.streams.get(stream, []))

    def pending_ids(self, stream, group):
        return list(self.groups[(stream, group)]["pending"])

    def xgroup_create(self, name, groupname, id="$", mkstream=False):
        if name not in self.streams:
            if not mkstream:
                raise ResponseError(
                    "ERR The XGROUP subcommand requires the key to exist"
                )
            self.streams[name] = []
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        last = -1 if id == "0" else len(self.streams[name]) - 1
        self.groups[(name, groupname)] = {"last": last, "pending": {}}

    def xadd(self, name, fields, **kwargs):
        self._counter += 1
        entry_id = f"{self._counter}-0"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        return entry_id

    def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        result = []
        for name in streams:
            group = self.groups[(name, groupname)]
            new = self.streams[name][group["last"] + 1 :]
            if count is not None:
                new = new[:count]
            if not new:
                continue
            group["last"] += len(new)
            for entry_id, _ in new:
                group["pending"][entry_id] = {
                    "consumer": consumername,
                    "delivered_at": self.now_ms,
                    "times": 1,
                }
            result.append(
                [name, [(entry_id, dict(fields)) for entry_id, fields in new]]
            )
        return result

    def xack(self, name, groupname, *ids):
        pending = self.groups[(name, groupname)]["pending"]
        return sum(1 for entry_id in ids if pending.pop(entry_id, None) is not None)

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def xpending_range(self, name, groupname, min, max, count):
        pending = self.groups[(name, groupname)]["pending"]
        return [
            {
                "message_id": entry_id,
                "consumer": info["consumer"],
                "time_since_delivered": self.now_ms - info["delivered_at"],
                "times_delivered": info["times"],
            }
            for entry_id, info in pending.items()
            if min <= entry_id <= max
        ][:count]

    def xautoclaim(
        self, name, groupname, consumername, min_idle_time, start_id="0-0", count=None
    ):
        pending = self.groups[(name, groupname)]["pending"]
        claimed = []
        for entry_id, info in pending.items():
            if count is not None and len(claimed) >= count:
                break
            if self.now_ms - info["delivered_at"] >= min_idle_time:
                info["consumer"] = consumername
                info["delivered_at"] = self.now_ms
                info["times"] += 1
                fields = dict(next(f for i, f in self.streams[name] if i == entry_id))
                claimed.append((entry_id, fields))
        return ("0-0", claimed, [])

    def xrange(self, name, min="-", max="+", count=None):
        entries = [(i, dict(f)) for i, f in self.streams.get(name, [])]
        return entries[:count]

    def xdel(self, name, *ids):
        before = len(self.streams.get(name, []))
        self.streams[name] = [e for e in self.streams.get(name, []) if e[0] not in ids]
        return before - len(self.streams[name])

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True

    def get(self, key):
        entry = self.keys.get(key)
        return entry[0] if entry else None

    def exists(self, key):
        return int(key in self.keys)

    def delete(self, *keys):
        return sum(1 for key in keys if self.keys.pop(key, None) is not None)

    def expire(self, key, seconds):
        if key not in self.keys:
            return False
        self.keys[key] = (self.keys[key][0], seconds)
        return True

    def ttl(self, key):
        return self.keys[key][1]


class FakeCache:
    """Stands in for TaskCache in the routes. Keeps values in a dict instead of Redis."""

    def __init__(self):
        self.store = {}
        self.generation = 0

    def get(self, key):
        return self.store.get(key)

    def token(self):
        return self.generation

    def set(self, key, value, ttl, token=None):
        if token is not None and token != self.generation:
            return  # Something was cleared since the reader started.
        self.store[key] = value

    def invalidate_tasks(self, *task_ids):
        self.generation += 1
        for key in (LIST_KEY, *[task_key(i) for i in task_ids]):
            self.store.pop(key, None)


class FakeCacheRedis:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def scan_iter(self, match=None):
        self.calls.append("scan")
        return iter(list(self.data))

    def mget(self, keys):
        self.calls.append("mget")
        return [self.data[k] for k in keys]

    def get(self, key):
        self.calls.append("get")
        return self.data[key]

    def pipeline(self):
        outer = self

        class Pipe:
            def __init__(self):
                self.keys = []

            def ttl(self, key):
                self.keys.append(key)

            def execute(self):
                outer.calls.append("pipeline")
                return [30 for _ in self.keys]

        return Pipe()


class GroupsRedis:
    def __init__(self, groups=None, error=None):
        self.groups = groups or []
        self.error = error

    def xinfo_groups(self, name):
        if self.error:
            raise self.error
        return self.groups


class FakeJob:
    """Stands in for the export_tasks_csv job."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []  # The task ids the route asked for.

    def apply_async(self, args=None, kwargs=None, task_id=None, **options):
        self.calls.append(task_id)
        if self.error is not None:
            raise self.error

        class Started:
            id = task_id

        return Started()
