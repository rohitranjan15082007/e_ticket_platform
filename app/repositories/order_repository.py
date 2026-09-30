"""Order retrieval helpers; no lifecycle decisions belong here."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order


_ORDER_LOADS = (
    selectinload(Order.items),
    selectinload(Order.reservations),
)


async def get_order(
    session: AsyncSession, order_id: UUID, *, for_update: bool = False
) -> Order | None:
    statement = select(Order).options(*_ORDER_LOADS).where(Order.id == order_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_order_for_buyer(
    session: AsyncSession, *, order_id: UUID, buyer_user_id: UUID, for_update: bool = False
) -> Order | None:
    statement = (
        select(Order)
        .options(*_ORDER_LOADS)
        .where(Order.id == order_id, Order.buyer_user_id == buyer_user_id)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_orders_for_buyer(session: AsyncSession, buyer_user_id: UUID) -> list[Order]:
    return (
        await session.scalars(
            select(Order)
            .options(*_ORDER_LOADS)
            .where(Order.buyer_user_id == buyer_user_id)
            .order_by(Order.created_at.desc(), Order.id)
        )
    ).all()
