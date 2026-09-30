"""Authenticated ticket-entitlement read endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.repositories.ticket_repository import list_tickets_for_owner
from app.schemas.order import TicketResponse


router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketResponse])
async def list_owned_tickets(
    session: SessionDependency,
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[TicketResponse]:
    tickets = await list_tickets_for_owner(session, current_user.id)
    return [TicketResponse.model_validate(ticket) for ticket in tickets]
