"""Operations dashboard package for the Coordinator engine.

Provides attach_dashboard(bus, pack, *, host, port) — a coroutine that:
  - subscribes to ALL bus channels
  - broadcasts every Envelope as JSON to connected WebSocket clients
  - serves the static single-page dashboard at "/"
  - provides a WebSocket endpoint at "/ws"

Pure bus subscriber — never publishes; never drives behaviour.
"""
from engine.dashboard.app import attach_dashboard  # noqa: F401

__all__ = ["attach_dashboard"]
