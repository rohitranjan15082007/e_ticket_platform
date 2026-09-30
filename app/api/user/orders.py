"""Authenticated order creation, viewing, and safe cancellation endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.schemas.order import CheckoutStateResponse, OrderCreateRequest, OrderResponse
from app.services.order_service import OrderSelection, OrderService


router = APIRouter(prefix="/orders", tags=["orders"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreateRequest,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> OrderResponse:
    result = await OrderService(session).create(
        buyer_user_id=current_user.id,
        selection=OrderSelection(
            payload.product_type, payload.product_id, payload.quantity, payload.coupon_code
        ),
        idempotency_key=idempotency_key,
        commit=True,
    )
    return OrderResponse.model_validate(result.response_payload)


@router.get("", response_model=list[OrderResponse])
async def list_orders(session: SessionDependency, current_user: CurrentUser) -> list[OrderResponse]:
    orders = await OrderService(session).list_for_buyer(buyer_user_id=current_user.id)
    return [OrderResponse.model_validate(OrderService.snapshot(order)) for order in orders]


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(order_id: UUID, session: SessionDependency, current_user: CurrentUser) -> OrderResponse:
    order = await OrderService(session).get_for_actor(order_id=order_id, actor_user_id=current_user.id)
    return OrderResponse.model_validate(OrderService.snapshot(order))


@router.get("/{order_id}/checkout-state", response_model=CheckoutStateResponse)
async def get_checkout_state(
    order_id: UUID, session: SessionDependency, current_user: CurrentUser,
) -> CheckoutStateResponse:
    state = await OrderService(session).checkout_state_for_buyer(
        order_id=order_id, buyer_user_id=current_user.id,
    )
    return CheckoutStateResponse.model_validate(state)


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: UUID,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> OrderResponse:
    result = await OrderService(session).cancel(
        order_id=order_id,
        actor_user_id=current_user.id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return OrderResponse.model_validate(result.response_payload)
