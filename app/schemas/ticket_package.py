"""Phase 3 ticket-package request and response contracts."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.ticket_series import _strict_positive_int


class PackageItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: UUID
    quantity: int

    @field_validator("quantity", mode="before")
    @classmethod
    def validate_quantity(cls, value: object) -> int:
        return _strict_positive_int(value, field="quantity", maximum=10_000_000)


class TicketPackageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    price_paise: int
    inventory_limit: int | None = None
    items: list[PackageItemInput] = Field(min_length=1, max_length=100)

    @field_validator("price_paise", mode="before")
    @classmethod
    def validate_price(cls, value: object) -> int:
        return _strict_positive_int(value, field="price_paise", maximum=9_000_000_000_000_000_000)

    @field_validator("inventory_limit", mode="before")
    @classmethod
    def validate_limit(cls, value: object) -> int | None:
        return None if value is None else _strict_positive_int(value, field="inventory_limit", maximum=10_000_000)

    @model_validator(mode="after")
    def validate_items(self) -> "TicketPackageCreateRequest":
        if len({item.series_id for item in self.items}) != len(self.items):
            raise ValueError("each series may appear only once")
        return self


class TicketPackageUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    price_paise: int | None = None
    inventory_limit: int | None = None
    items: list[PackageItemInput] | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("price_paise", mode="before")
    @classmethod
    def validate_price(cls, value: object) -> int | None:
        return None if value is None else _strict_positive_int(value, field="price_paise", maximum=9_000_000_000_000_000_000)

    @field_validator("inventory_limit", mode="before")
    @classmethod
    def validate_limit(cls, value: object) -> int | None:
        return None if value is None else _strict_positive_int(value, field="inventory_limit", maximum=10_000_000)

    @model_validator(mode="after")
    def require_change(self) -> "TicketPackageUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        if self.items is not None and len({item.series_id for item in self.items}) != len(self.items):
            raise ValueError("each series may appear only once")
        return self


class TicketPackageItemResponse(BaseModel):
    series_id: UUID
    quantity: int


class TicketPackageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    price_paise: int
    inventory_limit: int | None
    sold_count: int
    reserved_count: int
    currency: str
    is_active: bool
    created_by_user_id: UUID
    items: list[TicketPackageItemResponse]
