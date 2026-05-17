"""
=============================================================
chat_service/ws_events.py
=============================================================
Event type constants for WebSocket messages.
Used by both backend (publisher) and frontend (consumer).
"""

class WSEvent:
    # Report lifecycle
    REPORT_START  = "report_start"
    PROGRESS      = "progress"
    REPORT_DONE   = "report_done"
    REPORT_ERROR  = "error"

    # Data changes
    DATA_CHANGED  = "data_changed"   # new rows in runtime DB

    # Metadata / sync
    META_CHANGED  = "metadata_changed"
    SYNC_TRIGGERED = "sync_triggered"
    SYNC_START    = "sync_start"
    SYNC_PROGRESS = "sync_progress"
    SYNC_DONE     = "sync_done"

    # Connection
    CONNECTED     = "connected"
    PING          = "ping"
