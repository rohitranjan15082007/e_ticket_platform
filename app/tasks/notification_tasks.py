"""Notification-worker compatibility entry point.

P2P notifications share the transactional outbox with settlement delivery, so
they are dispatched by ``ticket.dispatch_p2p_outbox`` rather than a separate
unsafe queue. Re-exporting the registered task keeps callers on one durable
worker path.
"""

from app.tasks.payment_tasks import dispatch_p2p_outbox

__all__ = ["dispatch_p2p_outbox"]
