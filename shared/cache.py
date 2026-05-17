"""
shared/cache.py
Redis wrapper — metadata cache + report cache + pubsub
"""
import redis, json, hashlib, os
from dotenv import load_dotenv
load_dotenv()

TTL_METADATA = 86400   # 24hr
TTL_SQL      = 86400   # 24hr
TTL_REPORT   = 900     # 15min
TTL_MODULES  = 86400   # 24hr

def get_redis():
    try:
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=0, decode_responses=True, socket_timeout=2
        )
        r.ping()
        return r
    except Exception as e:
        print(f"[cache] Redis unavailable: {e}")
        return None

class Cache:

    def get(self, key: str):
        try:
            r = get_redis()
            if not r: return None
            val = r.get(key)
            return json.loads(val) if val else None
        except Exception as e:
            print(f"[cache] GET error {key}: {e}")
            return None

    def set(self, key: str, value, ttl: int = TTL_METADATA):
        try:
            r = get_redis()
            if not r: return
            r.setex(key, ttl, json.dumps(value, default=str))
        except Exception as e:
            print(f"[cache] SET error {key}: {e}")

    def delete(self, key: str):
        try:
            r = get_redis()
            if not r: return
            r.delete(key)
        except: pass

    def delete_pattern(self, pattern: str):
        try:
            r = get_redis()
            if not r: return
            keys = r.keys(pattern)
            if keys:
                r.delete(*keys)
                print(f"[cache] Deleted {len(keys)} keys: {pattern}")
        except Exception as e:
            print(f"[cache] DELETE_PATTERN error {pattern}: {e}")

    # ── Key builders ──────────────────────────────────────────
    def meta_key(self, schema, transid):
        return f"meta:{schema}:{transid.lower()}"

    def sql_key(self, schema, transid):
        return f"sql:{schema}:{transid.lower()}"

    def report_key(self, schema, transid, filters):
        h = self.make_hash(filters)
        return f"report:{schema}:{transid.lower()}:{h}"

    def modules_key(self, schema):
        return f"modules:{schema}"

    def meta_hash_key(self, schema):
        return f"metahash:{schema}"

    def data_hash_key(self, schema, table):
        return f"datahash:{schema}:{table}"

    # ── Hash utils ────────────────────────────────────────────
    def make_hash(self, params: dict) -> str:
        s = json.dumps(params, sort_keys=True, default=str)
        return hashlib.md5(s.encode()).hexdigest()[:8]

    def make_content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()

    # ── Invalidation ──────────────────────────────────────────
    def invalidate_schema(self, schema: str):
        for pattern in [
            f"meta:{schema}:*",
            f"sql:{schema}:*",
            f"modules:{schema}",
            f"metahash:{schema}",
        ]:
            self.delete_pattern(pattern)
        print(f"[cache] Invalidated all cache: {schema}")

    def invalidate_transid(self, schema: str, transid: str):
        self.delete(self.meta_key(schema, transid))
        self.delete(self.sql_key(schema, transid))
        self.delete_pattern(f"report:{schema}:{transid.lower()}:*")

    def invalidate_reports(self, schema: str):
        self.delete_pattern(f"report:{schema}:*")
        print(f"[cache] Invalidated report cache: {schema}")

    # ── PubSub ────────────────────────────────────────────────
    def publish(self, channel: str, message: dict):
        try:
            r = get_redis()
            if not r: return
            r.publish(channel, json.dumps(message, default=str))
        except Exception as e:
            print(f"[cache] PUBLISH error {channel}: {e}")

    def get_pubsub(self):
        try:
            r = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=0,
                decode_responses=True
            )
            r.ping()
            return r.pubsub()
        except Exception as e:
            print(f"[cache] PubSub unavailable: {e}")
            return None

    def publish_sync_event(self, schema, event, data={}):
        self.publish(f"sync:{schema}", {"event": event, "schema": schema, **data})

    def publish_data_event(self, schema, table):
        self.publish(f"data:{schema}", {
            "event": "data_changed", "schema": schema, "table": table
        })

    def publish_report_event(self, client_id, event, data={}):
        self.publish(f"report:{client_id}", {"event": event, **data})

cache = Cache()
