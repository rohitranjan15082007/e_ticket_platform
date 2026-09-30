"""Admin-controlled ticket-series catalog lifecycle."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency, require_paise
from app.core.permissions import RoleName
from app.core.state_machine import SERIES_TRANSITIONS, require_transition
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.ticket_series import TicketSeries, TicketSeriesPrize, TicketSeriesStatus
from app.models.user import IdempotencyRecord, User
from app.models.winner import Draw, DrawStatus
from app.repositories.ticket_repository import get_ticket_series


@dataclass(frozen=True, slots=True)
class PrizeDraft:
    rank: int
    title: str
    prize_paise: int


@dataclass(slots=True)
class TicketSeriesMutationResult:
    series: TicketSeries
    response_payload: dict[str, object]
    replayed: bool


class TicketSeriesService:
    """Mutates only admin-owned catalog state; routes never choose transitions."""

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
        ticket_limit: int,
        sales_start_at: datetime,
        sales_end_at: datetime,
        draw_at: datetime,
        prizes: list[PrizeDraft],
        idempotency_key: str,
        commit: bool,
    ) -> TicketSeriesMutationResult:
        """Create a DRAFT with server-validated INR price, dates, and prizes."""

        await self._require_admin(actor_user_id)
        validated = self._validate_definition(
            name=name,
            description=description,
            price_paise=price_paise,
            ticket_limit=ticket_limit,
            sales_start_at=sales_start_at,
            sales_end_at=sales_end_at,
            draw_at=draw_at,
            prizes=prizes,
        )
        request = {"action": "create", **validated}
        scope = f"admin:{actor_user_id}:ticket-series.create"
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        series = TicketSeries(
            id=uuid4(),
            name=validated["name"],
            description=validated["description"],
            price_paise=validated["price_paise"],
            ticket_limit=validated["ticket_limit"],
            currency="INR",
            sales_start_at=validated["sales_start_at"],
            sales_end_at=validated["sales_end_at"],
            draw_at=validated["draw_at"],
            status=TicketSeriesStatus.DRAFT,
            created_by_user_id=actor_user_id,
        )
        self.session.add(series)
        for prize in validated["prizes"]:
            self.session.add(
                TicketSeriesPrize(
                    id=uuid4(),
                    series_id=series.id,
                    rank=prize["rank"],
                    title=prize["title"],
                    prize_paise=prize["prize_paise"],
                )
            )
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="ticket_series",
            resource_id=series.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="ticket_series",
            entity_id=series.id,
            action="TICKET_SERIES_CREATED",
            before_state=None,
            after_state=self._state(series, prize_values=validated["prizes"]),
        )
        await self.session.flush()
        await self.session.refresh(series, attribute_names=["prizes"])
        response = self.snapshot(series)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketSeriesMutationResult(series, response, False)

    async def update(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID,
        name: str | None = None,
        description: str | None | object = ...,  # ``...`` means unchanged; None clears the description.
        price_paise: int | None = None,
        ticket_limit: int | None = None,
        sales_start_at: datetime | None = None,
        sales_end_at: datetime | None = None,
        draw_at: datetime | None = None,
        prizes: list[PrizeDraft] | None = None,
        idempotency_key: str,
        commit: bool,
    ) -> TicketSeriesMutationResult:
        """Edit only unsold DRAFT/PUBLISHED catalog data; opened inventory is immutable."""

        await self._require_admin(actor_user_id)
        requested_name = self._text(name, field="name", maximum=200) if name is not None else None
        description_provided = description is not ...
        requested_description = (
            self._optional_text(description, field="description", maximum=10_000)
            if description_provided else None
        )
        requested_price = require_paise(price_paise) if price_paise is not None else None
        requested_limit = (
            self._positive_int(ticket_limit, field="ticket_limit", maximum=10_000_000)
            if ticket_limit is not None else None
        )
        requested_start = (
            self._aware_datetime(sales_start_at, field="sales_start_at")
            if sales_start_at is not None else None
        )
        requested_end = (
            self._aware_datetime(sales_end_at, field="sales_end_at")
            if sales_end_at is not None else None
        )
        requested_draw = self._aware_datetime(draw_at, field="draw_at") if draw_at is not None else None
        requested_prizes = self._validate_prizes(prizes) if prizes is not None else None
        request = {
            "action": "update",
            "series_id": series_id,
            "name": requested_name,
            "description_provided": description_provided,
            "description": requested_description,
            "price_paise": requested_price,
            "ticket_limit": requested_limit,
            "sales_start_at": requested_start,
            "sales_end_at": requested_end,
            "draw_at": requested_draw,
            "prizes": requested_prizes,
        }
        scope = f"admin:{actor_user_id}:ticket-series.update"
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        series = await self._locked_series(series_id)
        if series.status not in {TicketSeriesStatus.DRAFT, TicketSeriesStatus.PUBLISHED}:
            raise ConflictError("SERIES_EDIT_LOCKED", "Only DRAFT or PUBLISHED series can be edited")
        if series.sold_count or series.reserved_count:
            raise ConflictError("SERIES_EDIT_LOCKED", "Series inventory cannot change after reservations or sales")
        if series.status == TicketSeriesStatus.PUBLISHED:
            committed_draw = await self.session.scalar(
                select(Draw).where(Draw.series_id == series.id).with_for_update()
            )
            if committed_draw is not None:
                raise ConflictError(
                    "SERIES_EDIT_LOCKED",
                    "A ticket series cannot change after its draw commitment is recorded",
                )
        target_prizes = prizes if prizes is not None else [
            PrizeDraft(prize.rank, prize.title, prize.prize_paise) for prize in series.prizes
        ]
        validated = self._validate_definition(
            name=series.name if requested_name is None else requested_name,
            description=series.description if not description_provided else requested_description,
            price_paise=series.price_paise if requested_price is None else requested_price,
            ticket_limit=series.ticket_limit if requested_limit is None else requested_limit,
            sales_start_at=series.sales_start_at if requested_start is None else requested_start,
            sales_end_at=series.sales_end_at if requested_end is None else requested_end,
            draw_at=series.draw_at if requested_draw is None else requested_draw,
            prizes=target_prizes,
        )
        before = self.snapshot(series)
        series.name = validated["name"]
        series.description = validated["description"]
        series.price_paise = validated["price_paise"]
        series.ticket_limit = validated["ticket_limit"]
        series.sales_start_at = validated["sales_start_at"]
        series.sales_end_at = validated["sales_end_at"]
        series.draw_at = validated["draw_at"]
        if prizes is not None:
            for prize in list(series.prizes):
                await self.session.delete(prize)
            for prize in validated["prizes"]:
                self.session.add(
                    TicketSeriesPrize(
                        id=uuid4(), series_id=series.id, rank=prize["rank"],
                        title=prize["title"], prize_paise=prize["prize_paise"],
                    )
                )
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="ticket_series", resource_id=series.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="ticket_series", entity_id=series.id,
            action="TICKET_SERIES_UPDATED", before_state=before,
            after_state=self._state(series, prize_values=validated["prizes"]),
        )
        await self.session.flush()
        await self.session.refresh(series, attribute_names=["prizes"])
        response = self.snapshot(series)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketSeriesMutationResult(series, response, False)

    async def publish(
        self, *, series_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> TicketSeriesMutationResult:
        return await self._transition(
            series_id=series_id,
            actor_user_id=actor_user_id,
            target=TicketSeriesStatus.PUBLISHED,
            action="publish",
            idempotency_key=idempotency_key,
            commit=commit,
        )

    async def open(
        self, *, series_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> TicketSeriesMutationResult:
        return await self._transition(
            series_id=series_id,
            actor_user_id=actor_user_id,
            target=TicketSeriesStatus.OPEN,
            action="open",
            idempotency_key=idempotency_key,
            commit=commit,
        )

    async def close(
        self, *, series_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> TicketSeriesMutationResult:
        """Prevent a legacy catalog caller from bypassing the draw snapshot.

        Phase 6 closes sales through ``WinnerService.close_sales`` because a
        CLOSED series must atomically freeze candidates and prize ranks under
        the same locks as the status transition.
        """

        raise ConflictError(
            "DRAW_CLOSE_WORKFLOW_REQUIRED",
            "Close ticket sales through the audited draw workflow",
        )

    async def cancel(
        self, *, series_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> TicketSeriesMutationResult:
        return await self._transition(
            series_id=series_id,
            actor_user_id=actor_user_id,
            target=TicketSeriesStatus.CANCELLED,
            action="cancel",
            idempotency_key=idempotency_key,
            commit=commit,
        )

    async def _transition(
        self,
        *,
        series_id: UUID,
        actor_user_id: UUID,
        target: TicketSeriesStatus,
        action: str,
        idempotency_key: str,
        commit: bool,
    ) -> TicketSeriesMutationResult:
        await self._require_admin(actor_user_id)
        series = await self._locked_series(series_id)
        request = {"action": action, "series_id": series_id, "target": target}
        scope = f"admin:{actor_user_id}:ticket-series.{action}"
        request_fingerprint = fingerprint(request)
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        if target == TicketSeriesStatus.PUBLISHED and not series.prizes:
            raise ValidationError("SERIES_PRIZES_REQUIRED", "A series needs at least one configured prize")
        if target == TicketSeriesStatus.OPEN:
            now = datetime.now(timezone.utc)
            if now < self._utc(series.sales_start_at) or now >= self._utc(series.sales_end_at):
                raise ConflictError("SERIES_SALES_WINDOW_CLOSED", "The configured sales window is not open")
            committed_draw = await self.session.scalar(
                select(Draw).where(Draw.series_id == series.id).with_for_update()
            )
            if committed_draw is None or committed_draw.status != DrawStatus.COMMITTED:
                raise ConflictError(
                    "DRAW_COMMITMENT_REQUIRED",
                    "Sales cannot open until a pre-sale draw commitment is recorded",
                )
        if target == TicketSeriesStatus.CANCELLED and (series.sold_count or series.reserved_count):
            raise ConflictError("SERIES_CANCEL_UNSAFE", "A series with sold or reserved tickets cannot be cancelled")
        require_transition(
            current=series.status, target=target, transitions=SERIES_TRANSITIONS, resource="Ticket series"
        )
        before = self.snapshot(series)
        series.status = target
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint,
            resource_type="ticket_series", resource_id=series.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="ticket_series", entity_id=series.id,
            action=f"TICKET_SERIES_{target.value}", before_state=before, after_state=self.snapshot(series),
        )
        await self.session.flush()
        response = self.snapshot(series)
        record.response_payload = response
        await self._finish(commit=commit)
        return TicketSeriesMutationResult(series, response, False)

    async def _locked_series(self, series_id: UUID) -> TicketSeries:
        series = await get_ticket_series(self.session, series_id, for_update=True)
        if series is None:
            raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series does not exist")
        return series

    async def _replay(self, record: IdempotencyRecord) -> TicketSeriesMutationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket-series mutation has no resource")
        series = await get_ticket_series(self.session, record.resource_id)
        if series is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior ticket-series mutation cannot be recovered")
        return TicketSeriesMutationResult(series, dict(record.response_payload), True)

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
        ticket_limit: object,
        sales_start_at: object,
        sales_end_at: object,
        draw_at: object,
        prizes: object,
    ) -> dict[str, object]:
        valid_name = cls._text(name, field="name", maximum=200)
        valid_description = cls._optional_text(description, field="description", maximum=10_000)
        valid_price = require_paise(price_paise)
        valid_limit = cls._positive_int(ticket_limit, field="ticket_limit", maximum=10_000_000)
        start = cls._aware_datetime(sales_start_at, field="sales_start_at")
        end = cls._aware_datetime(sales_end_at, field="sales_end_at")
        draw = cls._aware_datetime(draw_at, field="draw_at")
        if not start < end:
            raise ValidationError("INVALID_SALES_WINDOW", "sales_end_at must be after sales_start_at")
        if not end < draw:
            raise ValidationError("INVALID_DRAW_TIME", "draw_at must be after sales_end_at")
        valid_prizes = cls._validate_prizes(prizes)
        return {
            "name": valid_name,
            "description": valid_description,
            "price_paise": valid_price,
            "ticket_limit": valid_limit,
            "sales_start_at": start,
            "sales_end_at": end,
            "draw_at": draw,
            "prizes": valid_prizes,
        }

    @classmethod
    def _validate_prizes(cls, values: object) -> list[dict[str, object]]:
        if not isinstance(values, list) or not values:
            raise ValidationError("SERIES_PRIZES_REQUIRED", "At least one prize must be configured")
        if len(values) > 100:
            raise ValidationError("INVALID_PRIZE_CONFIGURATION", "At most 100 prizes may be configured")
        result: list[dict[str, object]] = []
        ranks: set[int] = set()
        for value in values:
            if not isinstance(value, PrizeDraft):
                raise ValidationError("INVALID_PRIZE_CONFIGURATION", "Prize data has an invalid shape")
            rank = cls._positive_int(value.rank, field="prize.rank", maximum=100_000)
            if rank in ranks:
                raise ValidationError("INVALID_PRIZE_CONFIGURATION", "Prize ranks must be unique")
            ranks.add(rank)
            result.append(
                {
                    "rank": rank,
                    "title": cls._text(value.title, field="prize.title", maximum=100),
                    "prize_paise": require_paise(value.prize_paise),
                }
            )
        return sorted(result, key=lambda prize: int(prize["rank"]))

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
        if value is None:
            return None
        return cls._text(value, field=field, maximum=maximum)

    @staticmethod
    def _positive_int(value: object, *, field: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
            raise ValidationError("INVALID_CATALOG_FIELD", f"{field} must be an integer between 1 and {maximum}")
        return value

    @staticmethod
    def _aware_datetime(value: object, *, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("INVALID_CATALOG_FIELD", f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @classmethod
    def _state(
        cls, series: TicketSeries, *, prize_values: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        prizes = prize_values if prize_values is not None else [
            {"rank": prize.rank, "title": prize.title, "prize_paise": prize.prize_paise}
            for prize in sorted(series.prizes, key=lambda value: value.rank)
        ]
        return {
            "id": series.id,
            "name": series.name,
            "description": series.description,
            "price_paise": series.price_paise,
            "ticket_limit": series.ticket_limit,
            "sold_count": series.sold_count,
            "reserved_count": series.reserved_count,
            "currency": require_inr_currency(series.currency),
            "sales_start_at": cls._utc(series.sales_start_at).isoformat(),
            "sales_end_at": cls._utc(series.sales_end_at).isoformat(),
            "draw_at": cls._utc(series.draw_at).isoformat(),
            "status": series.status,
            "created_by_user_id": series.created_by_user_id,
            "prizes": prizes,
        }

    @classmethod
    def snapshot(cls, series: TicketSeries) -> dict[str, object]:
        return canonical_payload(cls._state(series))
