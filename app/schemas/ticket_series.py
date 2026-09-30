"""Phase 3 ticket-series request and response contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.ticket_series import TicketSeriesStatus


def _strict_positive_int(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"{field} must be an integer between 1 and {maximum}")
    return value


class PrizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    title: str = Field(min_length=1, max_length=100)
    prize_paise: int

    @field_validator("rank", mode="before")
    @classmethod
    def validate_rank(cls, value: object) -> int:
        return _strict_positive_int(value, field="rank", maximum=100_000)

    @field_validator("prize_paise", mode="before")
    @classmethod
    def validate_prize(cls, value: object) -> int:
        return _strict_positive_int(value, field="prize_paise", maximum=9_000_000_000_000_000_000)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title cannot be blank")
        return value.strip()


class TicketSeriesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    price_paise: int
    ticket_limit: int
    sales_start_at: datetime
    sales_end_at: datetime
    draw_at: datetime
    prizes: list[PrizeInput] = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("price_paise", mode="before")
    @classmethod
    def validate_price(cls, value: object) -> int:
        return _strict_positive_int(value, field="price_paise", maximum=9_000_000_000_000_000_000)

    @field_validator("ticket_limit", mode="before")
    @classmethod
    def validate_limit(cls, value: object) -> int:
        return _strict_positive_int(value, field="ticket_limit", maximum=10_000_000)

    @model_validator(mode="after")
    def validate_dates(self) -> "TicketSeriesCreateRequest":
        values = (self.sales_start_at, self.sales_end_at, self.draw_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("series dates must include a timezone")
        if self.sales_end_at <= self.sales_start_at:
            raise ValueError("sales_end_at must be after sales_start_at")
        if self.draw_at <= self.sales_end_at:
            raise ValueError("draw_at must be after sales_end_at")
        if len({prize.rank for prize in self.prizes}) != len(self.prizes):
            raise ValueError("prize ranks must be unique")
        return self


class TicketSeriesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    price_paise: int | None = None
    ticket_limit: int | None = None
    sales_start_at: datetime | None = None
    sales_end_at: datetime | None = None
    draw_at: datetime | None = None
    prizes: list[PrizeInput] | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("price_paise", mode="before")
    @classmethod
    def validate_optional_price(cls, value: object) -> int | None:
        return None if value is None else _strict_positive_int(value, field="price_paise", maximum=9_000_000_000_000_000_000)

    @field_validator("ticket_limit", mode="before")
    @classmethod
    def validate_optional_limit(cls, value: object) -> int | None:
        return None if value is None else _strict_positive_int(value, field="ticket_limit", maximum=10_000_000)

    @model_validator(mode="after")
    def require_change(self) -> "TicketSeriesUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        for value in (self.sales_start_at, self.sales_end_at, self.draw_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("series dates must include a timezone")
        if self.prizes is not None and len({prize.rank for prize in self.prizes}) != len(self.prizes):
            raise ValueError("prize ranks must be unique")
        return self


class PrizeResponse(BaseModel):
    rank: int
    title: str
    prize_paise: int


class TicketSeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    price_paise: int
    ticket_limit: int
    sold_count: int
    reserved_count: int
    currency: str
    sales_start_at: datetime
    sales_end_at: datetime
    draw_at: datetime
    status: TicketSeriesStatus
    created_by_user_id: UUID
    prizes: list[PrizeResponse]
    draw_seed_commitment: str | None = None
    draw_committed_at: datetime | None = None
