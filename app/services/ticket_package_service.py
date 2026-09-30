"""Admin-controlled multi-series package catalog lifecycle."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency, require_paise
from app.core.permissions import RoleName
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.ticket_package import TicketPackage, TicketPackageItem
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import IdempotencyRecord, User
from app.repositories.ticket_repository import get_ticket_package, get_ticket_series_locked_many


@dataclass(frozen=True, slots=True)
class PackageItemDraft:
    series_id: UUID
    quantity: int


@dataclass(slots=True)
class TicketPackageMutationResult:
    package: TicketPackage
    response_payload: dict[str, object]
    replayed: bool


class TicketPackageService:
    """Creates safe package definitions; purchase-time availability is rechecked elsewhere."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def create(
        self,
        *,
        actor_user_id: UUID,
        name: str,
        description: str | None,
        price_paise: int,
        inventory_limit: int | None,
        items: list[PackageItemDraft],
        idempotency_key: str,
        commit: bool,
    ) -> TicketPackageMutationResult:
        await self._require_admin(actor_user_id)
        values = self._validate_definition(
            name=name, description=description, price_paise=price_paise,
            inventory_limit=inventory_limit, items=items,
        )
        scope = f"admin:{actor_user_id}:ticket-package.create"
        request_fingerprint = fingerprint({"action": "create", **values})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        await self._validate_series_contents(values["items"])
        package = TicketPackage(
            id=uuid4(), name=values["name"], description=values["description"],
            price_paise=values["price_paise"], inventory_limit=values["inventory_limit"],
            currency="INR", is_active=True, created_by_user_id=actor_user_id,
        )
        self.session.add(package)
        for item in values["items"]:
            self.session.add(
                TicketPackageItem(
                    id=uuid4(), package_id=package.id, series_id=item["series_id"], quantity=item["quantity"]
                )
            )
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="ticket_package", resource_id=package.id, status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="ticket_package", entity_id=package.id,
            action="TICKET_PACKAGE_CREATED", before_state=None,
            after_state=self._state(package, item_values=values["items"]),
        )
        await self.session.flush()
        await self.session.refresh(package, attribute_names=["items"])
        response = self.snapshot(package)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketPackageMutationResult(package, response, False)

    async def update(
        self,
        *,
        package_id: UUID,
        actor_user_id: UUID,
        name: str | None = None,
        description: str | None | object = ...,  # ``...`` means unchanged; None clears the description.
        price_paise: int | None = None,
        inventory_limit: int | None | object = ...,  # ``...`` means unchanged; None means unlimited.
        items: list[PackageItemDraft] | None = None,
        idempotency_key: str,
        commit: bool,
    ) -> TicketPackageMutationResult:
        await self._require_admin(actor_user_id)
        requested_name = self._text(name, field="name", maximum=200) if name is not None else None
        description_provided = description is not ...
        requested_description = (
            self._optional_text(description, field="description", maximum=10_000)
            if description_provided else None
        )
        requested_price = require_paise(price_paise) if price_paise is not None else None
        inventory_limit_provided = inventory_limit is not ...
        requested_inventory_limit = (
            self._optional_positive_int(inventory_limit, field="inventory_limit", maximum=10_000_000)
            if inventory_limit_provided else None
        )
        requested_items = self._validate_items(items) if items is not None else None
        request = {
            "action": "update",
            "package_id": package_id,
            "name": requested_name,
            "description_provided": description_provided,
            "description": requested_description,
            "price_paise": requested_price,
            "inventory_limit_provided": inventory_limit_provided,
            "inventory_limit": requested_inventory_limit,
            "items": requested_items,
        }
        scope = f"admin:{actor_user_id}:ticket-package.update"
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        package = await self._locked_package(package_id)
        if package.sold_count or package.reserved_count:
            raise ConflictError("PACKAGE_EDIT_LOCKED", "Package cannot change after reservations or sales")
        original_items = [PackageItemDraft(item.series_id, item.quantity) for item in package.items]
        values = self._validate_definition(
            name=package.name if requested_name is None else requested_name,
            description=package.description if not description_provided else requested_description,
            price_paise=package.price_paise if requested_price is None else requested_price,
            inventory_limit=(
                package.inventory_limit if not inventory_limit_provided else requested_inventory_limit
            ),
            items=original_items if items is None else items,
        )
        await self._validate_series_contents(values["items"])
        before = self.snapshot(package)
        package.name = values["name"]
        package.description = values["description"]
        package.price_paise = values["price_paise"]
        package.inventory_limit = values["inventory_limit"]
        if items is not None:
            for item in list(package.items):
                await self.session.delete(item)
            for item in values["items"]:
                self.session.add(
                    TicketPackageItem(
                        id=uuid4(), package_id=package.id, series_id=item["series_id"], quantity=item["quantity"]
                    )
                )
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="ticket_package", resource_id=package.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="ticket_package", entity_id=package.id,
            action="TICKET_PACKAGE_UPDATED", before_state=before,
            after_state=self._state(package, item_values=values["items"]),
        )
        await self.session.flush()
        await self.session.refresh(package, attribute_names=["items"])
        response = self.snapshot(package)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketPackageMutationResult(package, response, False)

    async def deactivate(
        self, *, package_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> TicketPackageMutationResult:
        """Stop future sales without invalidating reservations already made."""

        await self._require_admin(actor_user_id)
        package = await self._locked_package(package_id)
        scope = f"admin:{actor_user_id}:ticket-package.deactivate"
        request_fingerprint = fingerprint({"action": "deactivate", "package_id": package_id})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        if not package.is_active:
            raise ConflictError("PACKAGE_ALREADY_INACTIVE", "Ticket package is already inactive")
        before = self.snapshot(package)
        package.is_active = False
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="ticket_package", resource_id=package.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="ticket_package", entity_id=package.id,
            action="TICKET_PACKAGE_DEACTIVATED", before_state=before, after_state=self.snapshot(package),
        )
        await self.session.flush()
        response = self.snapshot(package)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketPackageMutationResult(package, response, False)

    async def _validate_series_contents(self, item_values: list[dict[str, object]]) -> None:
        series_ids = [item["series_id"] for item in item_values]
        if not all(isinstance(series_id, UUID) for series_id in series_ids):
            raise ValidationError("INVALID_PACKAGE_ITEM", "Package series IDs must be UUIDs")
        series = await get_ticket_series_locked_many(self.session, series_ids)
        by_id = {value.id: value for value in series}
        if len(by_id) != len(series_ids):
            raise ValidationError("UNKNOWN_TICKET_SERIES", "A package references an unknown ticket series")
        for item in item_values:
            target = by_id[item["series_id"]]
            if target.status not in {TicketSeriesStatus.PUBLISHED, TicketSeriesStatus.OPEN}:
                raise ConflictError(
                    "PACKAGE_SERIES_UNAVAILABLE", "Packages can include only PUBLISHED or OPEN ticket series"
                )
            remaining = target.ticket_limit - target.sold_count - target.reserved_count
            if remaining < item["quantity"]:
                raise ConflictError("INSUFFICIENT_TICKET_INVENTORY", "A package item exceeds series availability")

    async def _locked_package(self, package_id: UUID) -> TicketPackage:
        package = await get_ticket_package(self.session, package_id, for_update=True)
        if package is None:
            raise ValidationError("UNKNOWN_TICKET_PACKAGE", "Ticket package does not exist")
        return package

    async def _replay(self, record: IdempotencyRecord) -> TicketPackageMutationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket-package mutation has no resource")
        package = await get_ticket_package(self.session, record.resource_id)
        if package is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket-package mutation cannot be recovered")
        return TicketPackageMutationResult(package, dict(record.response_payload), True)

    async def _require_admin(self, actor_user_id: UUID) -> User:
        actor = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == actor_user_id).with_for_update()
        )
        if actor is None or not actor.is_active or RoleName.ADMIN.value not in {role.name for role in actor.roles}:
            raise AuthorizationError("Ticket catalog changes require an administrator")
        return actor

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @classmethod
    def _validate_definition(
        cls,
        *,
        name: object,
        description: object,
        price_paise: object,
        inventory_limit: object,
        items: object,
    ) -> dict[str, object]:
        return {
            "name": cls._text(name, field="name", maximum=200),
            "description": cls._optional_text(description, field="description", maximum=10_000),
            "price_paise": require_paise(price_paise),
            "inventory_limit": cls._optional_positive_int(inventory_limit, field="inventory_limit", maximum=10_000_000),
            "items": cls._validate_items(items),
        }

    @classmethod
    def _validate_items(cls, values: object) -> list[dict[str, object]]:
        if not isinstance(values, list) or not values:
            raise ValidationError("INVALID_PACKAGE_ITEM", "A package needs at least one included series")
        if len(values) > 100:
            raise ValidationError("INVALID_PACKAGE_ITEM", "A package may include at most 100 series")
        result: list[dict[str, object]] = []
        seen: set[UUID] = set()
        for value in values:
            if not isinstance(value, PackageItemDraft) or not isinstance(value.series_id, UUID):
                raise ValidationError("INVALID_PACKAGE_ITEM", "Package items need a UUID series_id")
            if value.series_id in seen:
                raise ValidationError("INVALID_PACKAGE_ITEM", "A series may appear only once in a package")
            seen.add(value.series_id)
            result.append(
                {
                    "series_id": value.series_id,
                    "quantity": cls._positive_int(value.quantity, field="item.quantity", maximum=10_000_000),
                }
            )
        return sorted(result, key=lambda item: str(item["series_id"]))

    @staticmethod
    def _text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("INVALID_CATALOG_FIELD", f"{field} must be a non-empty string")
        value = value.strip()
        if len(value) > maximum:
            raise ValidationError("INVALID_CATALOG_FIELD", f"{field} must be at most {maximum} characters")
        return value

    @classmethod
    def _optional_text(cls, value: object, *, field: str, maximum: int) -> str | None:
        return None if value is None else cls._text(value, field=field, maximum=maximum)

    @staticmethod
    def _positive_int(value: object, *, field: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
            raise ValidationError("INVALID_CATALOG_FIELD", f"{field} must be an integer between 1 and {maximum}")
        return value

    @classmethod
    def _optional_positive_int(cls, value: object, *, field: str, maximum: int) -> int | None:
        return None if value is None else cls._positive_int(value, field=field, maximum=maximum)

    @classmethod
    def _state(
        cls, package: TicketPackage, *, item_values: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        items = item_values if item_values is not None else [
            {"series_id": item.series_id, "quantity": item.quantity}
            for item in sorted(package.items, key=lambda value: str(value.series_id))
        ]
        return {
            "id": package.id,
            "name": package.name,
            "description": package.description,
            "price_paise": package.price_paise,
            "inventory_limit": package.inventory_limit,
            "sold_count": package.sold_count,
            "reserved_count": package.reserved_count,
            "currency": require_inr_currency(package.currency),
            "is_active": package.is_active,
            "created_by_user_id": package.created_by_user_id,
            "items": items,
        }

    @classmethod
    def snapshot(cls, package: TicketPackage) -> dict[str, object]:
        return canonical_payload(cls._state(package))
