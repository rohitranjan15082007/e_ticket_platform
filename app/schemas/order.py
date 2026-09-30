"""Phase 3 order and ticket read/write contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.order import DeliveryStatus, OrderStatus, TicketProductType, TicketReservationStatus
from app.models.ticket import TicketStatus
from app.schemas.ticket_series import _strict_positive_int


class OrderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_type: TicketProductType
    product_id: UUID
    quantity: int
    coupon_code: str | None = Field(default=None, min_length=3, max_length=64)

    @field_validator("quantity", mode="before")
    @classmethod
    def validate_quantity(cls, value: object) -> int:
        return _strict_positive_int(value, field="quantity", maximum=10_000)

    @field_validator("coupon_code")
    @classmethod
    def normalize_coupon_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not value:
            raise ValueError("coupon_code cannot be blank")
        return value


class OrderItemResponse(BaseModel):
    id: UUID
    product_type: TicketProductType
    product_id: UUID
    product_name_snapshot: str
    unit_price_paise: int
    quantity: int
    line_total_paise: int
    currency: str
    package_contents_snapshot: dict[str, object] | None


class TicketReservationResponse(BaseModel):
    id: UUID
    series_id: UUID
    quantity: int
    status: TicketReservationStatus
    expires_at: datetime
    allocated_at: datetime | None
    released_at: datetime | None


class OrderResponse(BaseModel):
    id: UUID
    status: OrderStatus
    delivery_status: DeliveryStatus
    subtotal_paise: int
    discount_paise: int
    total_paise: int
    currency: str
    coupon_code_snapshot: str | None
    expires_at: datetime
    items: list[OrderItemResponse]
    reservations: list[TicketReservationResponse]


class CheckoutStateResponse(BaseModel):
    order_id: UUID
    order_status: OrderStatus
    payment_attempt_id: UUID | None = None
    p2p_match_id: UUID | None = None


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    series_id: UUID
    serial_number: int
    order_item_id: UUID
    owner_user_id: UUID
    status: TicketStatus
    is_winner: bool
    prize_paise: int
