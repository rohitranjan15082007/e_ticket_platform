"""Database models imported here for Alembic metadata discovery."""

from app.models.audit_log import AuditLog
from app.models.affiliate import Affiliate, AffiliateClick, AffiliateCommission, AffiliateConversion
from app.models.base import Base
from app.models.cashback import CashbackCampaign, CashbackReward
from app.models.coupon import Coupon, CouponRedemption
from app.models.ledger import JournalGroup, JournalPosting, LedgerAccount
from app.models.order import Order, OrderItem, TicketReservation
from app.models.payment import (
    ManualPaymentDestination,
    ManualPaymentProof,
    PaymentAttempt,
    PaymentOutboxEvent,
    PaymentProviderEvent,
    PaymentReconciliationRun,
    PaymentSettlement,
)
from app.models.p2p_match import (
    P2PAdminResolution,
    P2PDispute,
    P2PMatch,
    P2PNotification,
    P2POutboxEvent,
    P2PPaymentSubmission,
    P2PProviderEvent,
    P2PReceiverConfirmation,
    P2PRefund,
    P2PSettlement,
    P2PVerifiedPaymentReference,
)
from app.models.revenue_allocation import RevenueAllocation
from app.models.referral import Referral, ReferralProfile, ReferralProgram, ReferralReward
from app.models.ticket import Ticket
from app.models.ticket_package import TicketPackage, TicketPackageItem
from app.models.ticket_series import TicketSeries, TicketSeriesPrize
from app.models.user import IdempotencyRecord, Role, User
from app.models.wallet import Wallet, WalletHold
from app.models.withdrawal import PaymentDestination, WithdrawalRequest
from app.models.winner import Draw, DrawEntry, DrawNotification, PrizeAward, Winner

__all__ = [
    "AuditLog",
    "Affiliate",
    "AffiliateClick",
    "AffiliateCommission",
    "AffiliateConversion",
    "Base",
    "CashbackCampaign",
    "CashbackReward",
    "Coupon",
    "CouponRedemption",
    "Draw",
    "DrawEntry",
    "DrawNotification",
    "IdempotencyRecord",
    "JournalGroup",
    "JournalPosting",
    "LedgerAccount",
    "ManualPaymentDestination",
    "ManualPaymentProof",
    "Order",
    "OrderItem",
    "PaymentAttempt",
    "PaymentOutboxEvent",
    "PaymentProviderEvent",
    "PaymentReconciliationRun",
    "PaymentSettlement",
    "P2PAdminResolution",
    "P2PDispute",
    "P2PMatch",
    "P2PNotification",
    "P2POutboxEvent",
    "P2PPaymentSubmission",
    "P2PProviderEvent",
    "P2PReceiverConfirmation",
    "P2PRefund",
    "P2PSettlement",
    "P2PVerifiedPaymentReference",
    "PaymentDestination",
    "PrizeAward",
    "Role",
    "RevenueAllocation",
    "Referral",
    "ReferralProfile",
    "ReferralProgram",
    "ReferralReward",
    "Ticket",
    "TicketPackage",
    "TicketPackageItem",
    "TicketReservation",
    "TicketSeries",
    "TicketSeriesPrize",
    "User",
    "Wallet",
    "WalletHold",
    "Winner",
    "WithdrawalRequest",
]
