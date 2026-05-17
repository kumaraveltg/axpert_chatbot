"""
sync_service/change_detector.py

Polls for changes every N seconds:
  1. Metadata hash (vw_tstructs) → trigger resync if changed
  2. max(modifiedon) per runtime table → notify if new data

Publishes Redis events → WebSocket → Frontend
"""
import asyncio, os, threading
from dotenv import load_dotenv
load_dotenv()

from sync_service.extractor import (
    get_metadata_hash,
    get_table_max_modified,
    get_watched_tables
)
from shared.cache import cache

METADATA_POLL  = 300   # 5 min — metadata rarely changes
DATA_POLL      = 60    # 1 min  — catch new runtime data


async def check_metadata(schema: str):
    """
    Compare vw_tstructs hash with stored hash.
    If different → invalidate cache + trigger resync.
    """
    current = get_metadata_hash(schema)
    if not current:
        return

    stored = cache.get(cache.meta_hash_key(schema))

    if stored and stored == current:
        return  # no change

    print(f"[detector] Metadata changed: {schema}")

    # Notify frontend
    cache.publish_sync_event(schema, "metadata_changed", {
        "message": "Metadata updated — resyncing..."
    })

    # Trigger resync via sync_service
    await _trigger_resync(schema)

    # Store new hash
    cache.set(cache.meta_hash_key(schema), current)


async def _trigger_resync(schema: str):
    """POST to sync_service to trigger resync."""
    import aiohttp
    url = os.getenv("SYNC_URL", "http://127.0.0.1:8005")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/sync/{schema}") as r:
                if r.status == 200:
                    print(f"[detector] Resync triggered: {schema}")
    except Exception as e:
        print(f"[detector] Resync error: {e}")


async def check_data_changes(schema: str, tables: list):
    """
    Compare max(modifiedon) per table with stored value.
    If changed → invalidate report cache + notify frontend.
    """
    for t in tables:
        tname   = t["tablename"]
        transid = t["transid"]

        current = get_table_max_modified(schema, tname)
        if not current:
            continue

        key    = cache.data_hash_key(schema, tname)
        stored = cache.get(key)

        if stored == current:
            continue  # no change

        print(f"[detector] New data: {schema}.{tname}")

        # Invalidate only report results — keep metadata cache
        cache.invalidate_reports(schema)

        # Notify frontend
        cache.publish_data_event(schema, tname)

        # Store new max
        cache.set(key, current)


async def run_detector(schemas: list):
    """
    Main polling loop for all schemas.
    schemas = ["hcaspay", "clientb", ...]
    """
    print(f"[detector] Starting for: {schemas}")

    # Pre-load watched tables per schema
    watched = {s: get_watched_tables(s) for s in schemas}
    for s, tables in watched.items():
        print(f"[detector] Watching {len(tables)} tables in {s}")

    meta_tick = 0
    data_tick = 0

    while True:
        await asyncio.sleep(10)
        meta_tick += 10
        data_tick += 10

        if data_tick >= DATA_POLL:
            for s in schemas:
                await check_data_changes(s, watched[s])
            data_tick = 0

        if meta_tick >= METADATA_POLL:
            for s in schemas:
                await check_metadata(s)
            # Refresh watched tables — new DCs may have been added
            watched = {s: get_watched_tables(s) for s in schemas}
            meta_tick = 0


def start_detector(schemas: list):
    """
    Start detector as FastAPI background task.
    FastAPI already has a running event loop —
    so we schedule the coroutine directly into it.

    Call from chat_service/main.py startup:
      from sync_service.change_detector import start_detector
      start_detector(["hcaspay"])
    """
    import asyncio

    async def _start():
        asyncio.create_task(run_detector(schemas))

    # Schedule into the already-running FastAPI event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # FastAPI is running — schedule as task
            loop.create_task(run_detector(schemas))
            print(f"[detector] Task scheduled in FastAPI event loop")
        else:
            loop.run_until_complete(run_detector(schemas))
    except RuntimeError:
        # No event loop yet — create one
        asyncio.run(run_detector(schemas))
