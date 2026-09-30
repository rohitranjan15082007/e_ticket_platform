"""Small, explicit lifecycle transition guards used by domain services."""

from collections.abc import Mapping
from enum import Enum

from app.exceptions import ConflictError
from app.models.order import OrderStatus
from app.models.p2p_match import P2PDisputeStatus, P2PMatchStatus
from app.models.ticket_series import TicketSeriesStatus
from app.models.withdrawal import WithdrawalStatus


SERIES_TRANSITIONS: Mapping[TicketSeriesStatus, set[TicketSeriesStatus]] = {
    TicketSeriesStatus.DRAFT: {TicketSeriesStatus.PUBLISHED},
    TicketSeriesStatus.PUBLISHED: {TicketSeriesStatus.OPEN, TicketSeriesStatus.CANCELLED},
    TicketSeriesStatus.OPEN: {TicketSeriesStatus.CLOSED, TicketSeriesStatus.CANCELLED},
    TicketSeriesStatus.CLOSED: {TicketSeriesStatus.DRAWN},
    TicketSeriesStatus.DRAWN: {TicketSeriesStatus.RESULT_PUBLISHED},
    TicketSeriesStatus.RESULT_PUBLISHED: set(),
    TicketSeriesStatus.CANCELLED: set(),
}

ORDER_TRANSITIONS: Mapping[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: {
        OrderStatus.WAITING_FOR_MATCH,
        # A non-P2P method has exposed a frozen, order-linked payment
        # instruction.  It is intentionally no longer cancellable by the
        # ordinary unpaid-order path, because a delayed external receipt may
        # still arrive and must be reconciled rather than silently released.
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.PAYMENT_REVIEW,
        OrderStatus.PAID,
        OrderStatus.CANCELLED,
    },
    OrderStatus.WAITING_FOR_MATCH: {OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED},
    OrderStatus.AWAITING_PAYMENT: {
        OrderStatus.WAITING_FOR_MATCH,
        # An externally exposed payment may be delayed, malformed, or need
        # an explicit manual decision. Preserve the reservation for
        # reconciliation instead of releasing it through an unpaid path.
        OrderStatus.PAYMENT_REVIEW,
        OrderStatus.PAID,
        OrderStatus.CANCELLED,
        OrderStatus.REFUND_PENDING,
    },
    OrderStatus.PAYMENT_REVIEW: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.FULFILLED, OrderStatus.REFUND_PENDING},
    OrderStatus.FULFILLED: {OrderStatus.REFUND_PENDING},
    OrderStatus.CANCELLED: set(),
    OrderStatus.REFUND_PENDING: {OrderStatus.REFUNDED},
    OrderStatus.REFUNDED: set(),
}

WITHDRAWAL_TRANSITIONS: Mapping[WithdrawalStatus, set[WithdrawalStatus]] = {
    WithdrawalStatus.WAITING_FOR_BUYER: {WithdrawalStatus.MATCHED, WithdrawalStatus.CANCELLED},
    WithdrawalStatus.MATCHED: {
        WithdrawalStatus.WAITING_FOR_BUYER,
        WithdrawalStatus.UNDER_REVIEW,
        WithdrawalStatus.COMPLETED,
        WithdrawalStatus.CANCELLED,
    },
    WithdrawalStatus.UNDER_REVIEW: {
        WithdrawalStatus.WAITING_FOR_BUYER,
        WithdrawalStatus.COMPLETED,
        WithdrawalStatus.CANCELLED,
    },
    WithdrawalStatus.COMPLETED: set(),
    WithdrawalStatus.CANCELLED: set(),
}

P2P_MATCH_TRANSITIONS: Mapping[P2PMatchStatus, set[P2PMatchStatus]] = {
    P2PMatchStatus.WAITING_FOR_PAYMENT: {
        P2PMatchStatus.PAYMENT_SUBMITTED,
        P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION,
        P2PMatchStatus.CLOSED_UNPAID,
        P2PMatchStatus.ADMIN_REVIEW,
    },
    P2PMatchStatus.PAYMENT_SUBMITTED: {
        P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
        P2PMatchStatus.UNDER_VERIFICATION,
        P2PMatchStatus.ADMIN_REVIEW,
        P2PMatchStatus.CLOSED_UNPAID,
    },
    P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION: {
        P2PMatchStatus.UNDER_VERIFICATION,
        P2PMatchStatus.DISPUTED,
        P2PMatchStatus.ADMIN_REVIEW,
        P2PMatchStatus.CLOSED_UNPAID,
    },
    P2PMatchStatus.UNDER_VERIFICATION: {
        P2PMatchStatus.SETTLED,
        P2PMatchStatus.DISPUTED,
        P2PMatchStatus.ADMIN_REVIEW,
        P2PMatchStatus.CLOSED_UNPAID,
    },
    P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION: {
        P2PMatchStatus.UNDER_VERIFICATION,
        P2PMatchStatus.ADMIN_REVIEW,
        P2PMatchStatus.CLOSED_UNPAID,
    },
    P2PMatchStatus.DISPUTED: {
        P2PMatchStatus.ADMIN_REVIEW,
        P2PMatchStatus.SETTLED,
        P2PMatchStatus.CLOSED_UNPAID,
    },
    P2PMatchStatus.ADMIN_REVIEW: {
        P2PMatchStatus.SETTLED,
        P2PMatchStatus.CLOSED_UNPAID,
        P2PMatchStatus.REFUND_PENDING,
    },
    P2PMatchStatus.SETTLED: {P2PMatchStatus.REFUND_PENDING},
    P2PMatchStatus.CLOSED_UNPAID: set(),
    P2PMatchStatus.REFUND_PENDING: {P2PMatchStatus.REFUNDED},
    P2PMatchStatus.REFUNDED: set(),
}


# Keep the dispute record independent from the payment-match lifecycle.  A
# match can be under review while its dispute is still open, but no caller may
# skip the server-owned dispute lifecycle when recording an administrator's
# decision.
P2P_DISPUTE_TRANSITIONS: Mapping[P2PDisputeStatus, set[P2PDisputeStatus]] = {
    P2PDisputeStatus.OPEN: {P2PDisputeStatus.UNDER_REVIEW, P2PDisputeStatus.RESOLVED},
    P2PDisputeStatus.UNDER_REVIEW: {P2PDisputeStatus.RESOLVED},
    P2PDisputeStatus.RESOLVED: set(),
}


def require_transition(
    *, current: Enum, target: Enum, transitions: Mapping[Enum, set[Enum]], resource: str
) -> None:
    """Reject a state change absent from the server-owned lifecycle graph."""

    if target not in transitions.get(current, set()):
        raise ConflictError(
            "INVALID_TRANSITION",
            f"{resource} cannot transition from {current.value} to {target.value}",
        )
