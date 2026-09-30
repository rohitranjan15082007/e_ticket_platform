"""Operational dashboard read contract."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.p2p_match import P2PDisputeStatus


class AdminDashboardResponse(BaseModel):
    total_users: int
    open_series: int
    closed_series: int
    tickets_sold: int
    open_tickets_remaining: int
    pending_orders: int
    pending_manual_proofs: int
    active_p2p_matches: int
    open_disputes: int
    unresolved_withdrawals: int
    pending_referral_rewards: int
    pending_cashback_rewards: int
    pending_affiliate_commissions: int
    allocated_revenue_paise: int


class AdminDisputeResponse(BaseModel):
    id: UUID
    match_id: UUID
    opened_by_user_id: UUID | None
    status: P2PDisputeStatus
    reason_code: str
    evidence_reference: str | None
    created_at: datetime
    resolved_at: datetime | None
