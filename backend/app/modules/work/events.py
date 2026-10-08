"""Best-effort live delivery of durable work events.

The durable event log (``event_log``) records notices in the writer's
transaction; the transaction's after-commit hook hands them here for live
delivery through the process's bound publisher:

- an in-process deployment fans out to its websockets directly;
- a split deployment's worker hands them to PostgreSQL ``pg_notify`` and the
  API process delivers what it hears.

Delivery may drop a notice (a dead socket, a restarted listener); the durable
rows and the client cursor are what guarantee recovery, so delivery is allowed
to fail and the write that produced the notice is never failed by it.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)


class Publisher(Protocol):
    def publish_threadsafe(self, channel: str, payload: dict[str, Any]) -> None: ...


_publisher: Publisher | None = None


def bind(publisher: Publisher | None) -> None:
    global _publisher
    _publisher = publisher


def relay(channel: str, payload: dict[str, Any]) -> None:
    """Deliver one committed notice; dropped when nothing is bound or it fails."""
    publisher = _publisher
    if publisher is None:
        return
    try:
        publisher.publish_threadsafe(channel, payload)
    except Exception:  # noqa: BLE001 - realtime delivery is best effort
        logger.warning("work event publication failed", extra={"channel": channel})
