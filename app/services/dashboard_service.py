"""Read-only operational counts for the administrator dashboard."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate import AffiliateCommission, AffiliateCommissionStatus
from app.models.cashback import CashbackReward, CashbackRewardStatus
from app.models.order import Order, OrderStatus
from app.models.p2p_match import P2PDispute, P2PDisputeStatus, P2PMatch, P2PMatchStatus
from app.models.payment import ManualPaymentProof, ManualPaymentProofStatus
from app.models.referral import ReferralReward, ReferralRewardStatus
from app.models.revenue_allocation import RevenueAllocation
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import User
from app.models.withdrawal import WithdrawalRequest, WithdrawalStatus


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def summary(self) -> dict[str, int]:
        """Return counts and posted revenue only; no financial state changes."""

        async def count(model, *predicates) -> int:
            return int(await self.session.scalar(select(func.count()).select_from(model).where(*predicates)) or 0)

        sold = await self.session.scalar(select(func.coalesce(func.sum(TicketSeries.sold_count), 0)))
        remaining = await self.session.scalar(
            select(func.coalesce(func.sum(
                TicketSeries.ticket_limit - TicketSeries.sold_count - TicketSeries.reserved_count
            ), 0)).where(TicketSeries.status == TicketSeriesStatus.OPEN)
        )
        revenue = await self.session.scalar(
            select(func.coalesce(func.sum(RevenueAllocation.allocation_base_paise), 0))
        )
        return {
            "total_users": await count(User),
            "open_series": await count(TicketSeries, TicketSeries.status == TicketSeriesStatus.OPEN),
            "closed_series": await count(
                TicketSeries, TicketSeries.status.in_([
                    TicketSeriesStatus.CLOSED, TicketSeriesStatus.DRAWN,
                    TicketSeriesStatus.RESULT_PUBLISHED,
                ]),
            ),
            "tickets_sold": int(sold or 0),
            "open_tickets_remaining": int(remaining or 0),
            "pending_orders": await count(
                Order, Order.status.in_([
                    OrderStatus.PENDING_PAYMENT, OrderStatus.WAITING_FOR_MATCH,
                    OrderStatus.AWAITING_PAYMENT, OrderStatus.PAYMENT_REVIEW,
                ]),
            ),
            "pending_manual_proofs": await count(
                ManualPaymentProof, ManualPaymentProof.status.in_([
                    ManualPaymentProofStatus.SUBMITTED, ManualPaymentProofStatus.UNDER_REVIEW,
                ]),
            ),
            "active_p2p_matches": await count(
                P2PMatch, P2PMatch.status.notin_([
                    P2PMatchStatus.SETTLED, P2PMatchStatus.CLOSED_UNPAID,
                    P2PMatchStatus.REFUNDED,
                ]),
            ),
            "open_disputes": await count(
                P2PDispute, P2PDispute.status != P2PDisputeStatus.RESOLVED,
            ),
            "unresolved_withdrawals": await count(
                WithdrawalRequest, WithdrawalRequest.status.in_([
                    WithdrawalStatus.WAITING_FOR_BUYER, WithdrawalStatus.MATCHED,
                    WithdrawalStatus.UNDER_REVIEW,
                ]),
            ),
            "pending_referral_rewards": await count(
                ReferralReward, ReferralReward.status == ReferralRewardStatus.PENDING_REVIEW,
            ),
            "pending_cashback_rewards": await count(
                CashbackReward, CashbackReward.status == CashbackRewardStatus.PENDING_REVIEW,
            ),
            "pending_affiliate_commissions": await count(
                AffiliateCommission, AffiliateCommission.status == AffiliateCommissionStatus.PENDING_REVIEW,
            ),
            "allocated_revenue_paise": int(revenue or 0),
        }
