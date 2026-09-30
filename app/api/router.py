"""Top-level API v1 router."""

from fastapi import APIRouter

from app.api.admin.audit import router as admin_audit_router
from app.api.admin.disputes import router as admin_disputes_router
from app.api.admin.dashboard import router as admin_dashboard_router
from app.api.admin.marketing import router as admin_marketing_router
from app.api.admin.packages import router as admin_packages_router
from app.api.admin.payments import router as admin_payments_router
from app.api.admin.revenue import router as admin_revenue_router
from app.api.admin.refunds import router as admin_refunds_router
from app.api.admin.ticket_series import router as admin_ticket_series_router
from app.api.admin.users import router as admin_users_router
from app.api.admin.withdrawals import router as admin_withdrawals_router
from app.api.admin.winners import router as admin_winners_router
from app.api.webhooks.p2p_provider import router as p2p_provider_webhook_router
from app.api.webhooks.telegram import router as telegram_webhook_router
from app.api.webhooks.white_label import router as white_label_webhook_router
from app.api.user.auth import router as auth_router
from app.api.user.catalog import router as catalog_router
from app.api.user.notifications import router as notifications_router
from app.api.user.orders import router as orders_router
from app.api.user.profile import router as profile_router
from app.api.user.payments import router as payments_router
from app.api.user.referrals import router as referrals_router
from app.api.user.results import router as results_router
from app.api.user.tickets import router as tickets_router
from app.api.user.wallet import router as wallet_router
from app.api.user.withdrawals import router as withdrawals_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(profile_router)
api_router.include_router(catalog_router)
api_router.include_router(orders_router)
api_router.include_router(payments_router)
api_router.include_router(referrals_router)
api_router.include_router(results_router)
api_router.include_router(tickets_router)
api_router.include_router(wallet_router)
api_router.include_router(withdrawals_router)
api_router.include_router(notifications_router)
api_router.include_router(admin_ticket_series_router)
api_router.include_router(admin_withdrawals_router)
api_router.include_router(admin_winners_router)
api_router.include_router(admin_packages_router)
api_router.include_router(admin_marketing_router)
api_router.include_router(admin_revenue_router)
api_router.include_router(admin_payments_router)
api_router.include_router(admin_disputes_router)
api_router.include_router(admin_dashboard_router)
api_router.include_router(admin_refunds_router)
api_router.include_router(admin_audit_router)
api_router.include_router(admin_users_router)
api_router.include_router(p2p_provider_webhook_router)
api_router.include_router(white_label_webhook_router)
api_router.include_router(telegram_webhook_router)
