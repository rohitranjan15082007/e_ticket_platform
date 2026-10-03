"""Same-origin Phase 8 HTML pages and static assets."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


router = APIRouter(include_in_schema=False)
WEB_ROOT = Path(__file__).resolve().parents[1] / "templates_and_static"
TEMPLATES = WEB_ROOT / "templates"

PAGES = {
    "/": "public/home.html",
    "/series": "public/series.html",
    "/packages": "public/packages.html",
    "/results": "public/results.html",
    "/payment-demo": "public/payment_demo.html",
    "/login": "public/login.html",
    "/register": "public/register.html",
    "/how-it-works": "public/content.html",
    "/payment-methods": "public/content.html",
    "/terms": "public/content.html",
    "/privacy": "public/content.html",
    "/refund-policy": "public/content.html",
    "/responsible-use": "public/content.html",
    "/checkout": "user/checkout.html",
    "/dashboard": "user/dashboard.html",
    "/orders": "user/orders.html",
    "/payments": "user/payments.html",
    "/wallet": "user/wallet.html",
    "/withdrawals": "user/withdrawals.html",
    "/tickets": "user/tickets.html",
    "/referrals": "user/utility.html",
    "/account": "user/utility.html",
    "/notifications": "user/utility.html",
    "/admin": "admin/workspace.html",
    "/admin/users": "admin/workspace.html",
    "/admin/series": "admin/workspace.html",
    "/admin/packages": "admin/workspace.html",
    "/admin/draws": "admin/workspace.html",
    "/admin/payments": "admin/workspace.html",
    "/admin/withdrawals": "admin/workspace.html",
    "/admin/disputes": "admin/workspace.html",
    "/admin/revenue": "admin/workspace.html",
    "/admin/marketing": "admin/workspace.html",
    "/admin/audit": "admin/workspace.html",
    "/admin/actions": "admin/workspace.html",
}


def _page_response(relative_path: str) -> FileResponse:
    return FileResponse(
        TEMPLATES / relative_path,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )


def _endpoint(template_path: str):
    def page() -> FileResponse:
        return _page_response(template_path)

    return page


for page_path, template_path in PAGES.items():
    router.add_api_route(page_path, _endpoint(template_path), methods=["GET"], include_in_schema=False)


@router.get("/orders/{order_id}", include_in_schema=False)
def order_detail_page(order_id: UUID) -> FileResponse:
    """Serve an owner-scoped order view; the API enforces data ownership."""
    return _page_response("user/order_detail.html")


@router.get("/series/{series_id}", include_in_schema=False)
def series_detail_page(series_id: UUID) -> FileResponse:
    return _page_response("public/content.html")


@router.get("/packages/{package_id}", include_in_schema=False)
def package_detail_page(package_id: UUID) -> FileResponse:
    return _page_response("public/content.html")


@router.get("/results/{series_id}", include_in_schema=False)
def result_detail_page(series_id: UUID) -> FileResponse:
    return _page_response("public/content.html")


@router.get("/tickets/{ticket_id}", include_in_schema=False)
def ticket_detail_page(ticket_id: UUID) -> FileResponse:
    return _page_response("user/utility.html")


@router.get("/withdrawals/{withdrawal_id}", include_in_schema=False)
def withdrawal_detail_page(withdrawal_id: UUID) -> FileResponse:
    return _page_response("user/utility.html")


@router.get("/admin/users/{user_id}", include_in_schema=False)
def admin_user_detail_page(user_id: UUID) -> FileResponse:
    return _page_response("admin/workspace.html")


@router.get("/admin/catalog/{kind}/{record_id}", include_in_schema=False)
def admin_catalog_detail_page(kind: str, record_id: UUID) -> FileResponse:
    if kind not in {"series", "packages"}:
        raise HTTPException(status_code=404)
    return _page_response("admin/workspace.html")


@router.get("/admin/draws/{series_id}", include_in_schema=False)
def admin_draw_detail_page(series_id: UUID) -> FileResponse:
    return _page_response("admin/workspace.html")


@router.get("/admin/payments/{proof_id}", include_in_schema=False)
def admin_payment_detail_page(proof_id: UUID) -> FileResponse:
    return _page_response("admin/workspace.html")


@router.get("/admin/disputes/{dispute_id}", include_in_schema=False)
def admin_dispute_detail_page(dispute_id: UUID) -> FileResponse:
    return _page_response("admin/workspace.html")
