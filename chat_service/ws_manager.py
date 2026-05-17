"""
=============================================================
chat_service/ws_manager.py
=============================================================
"""
import asyncio, json, threading
from fastapi import WebSocket
from shared.cache import cache


class WSManager:
    """
    Manages WebSocket connections.
    Each client connects with a client_id.
    Listens to Redis pubsub and forwards to browser.
    """

    def __init__(self):
        self.connections: dict[str, WebSocket] = {}
        self._started = False

    async def connect(self, ws: WebSocket, client_id: str):
        await ws.accept()
        self.connections[client_id] = ws
        print(f"[ws] Connected: {client_id} "
              f"(total: {len(self.connections)})")

    def disconnect(self, client_id: str):
        self.connections.pop(client_id, None)
        print(f"[ws] Disconnected: {client_id}")

    async def send(self, client_id: str, data: dict):
        ws = self.connections.get(client_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception as e:
                print(f"[ws] Send error {client_id}: {e}")
                self.disconnect(client_id)

    async def broadcast(self, schema: str, data: dict):
        """Send to all clients watching a schema."""
        for cid, ws in list(self.connections.items()):
            if cid.startswith(schema):
                await self.send(cid, data)

    def start_redis_listener(self, schemas: list):
            """
            Subscribe to Redis pubsub channels.
            Forwards events to connected WebSocket clients.
            Runs in background thread with auto-reconnect.
            """
            if self._started:
                return
            self._started = True

            def _listen():
                import time
                while True:
                    try:
                        ps = cache.get_pubsub()
                        if not ps:
                            print("[ws] Redis pubsub unavailable — retrying in 5s")
                            time.sleep(5)
                            continue

                        channels = []
                        for s in schemas:
                            channels.append(f"sync:{s}")
                            channels.append(f"data:{s}")

                        ps.subscribe(*channels)
                        ps.psubscribe("report:*")
                        print(f"[ws] Subscribed to Redis channels: {channels}")

                        for msg in ps.listen():
                            if msg['type'] not in ('message', 'pmessage'):
                                continue
                            try:
                                channel = msg['channel']
                                data    = json.loads(msg['data'])
                                schema  = data.get('schema', '')

                                loop = asyncio.new_event_loop()
                                if channel.startswith('report:'):
                                    target = channel.replace('report:', '')
                                    loop.run_until_complete(self.send(target, data))
                                elif schema:
                                    loop.run_until_complete(self.broadcast(schema, data))
                                loop.close()
                            except Exception as e:
                                print(f"[ws] Redis listener error: {e}")

                    except Exception as e:
                        print(f"[ws] Redis listener crashed: {e} — reconnecting in 5s")
                        time.sleep(5)
                        continue

            t = threading.Thread(target=_listen, daemon=True)
            t.start()
            print("[ws] Redis listener started")

ws_manager = WSManager()

