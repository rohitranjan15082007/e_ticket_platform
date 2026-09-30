"""Strict API contracts for the auditable draw and published-result flow."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.winner import DrawStatus


class DrawCommitmentRequest(BaseModel):
    """Commit the SHA-256 hex digest of a secret seed before sales close."""

    model_config = ConfigDict(extra="forbid")

    seed_commitment: str = Field(min_length=64, max_length=64)

    @field_validator("seed_commitment")
    @classmethod
    def validate_seed_commitment(cls, value: str) -> str:
        normalized = value.lower()
        if value != normalized or any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("seed_commitment must be a lowercase SHA-256 hexadecimal digest")
        return normalized


class DrawRevealRequest(BaseModel):
    """Reveal exactly the seed that produced the pre-sale commitment."""

    model_config = ConfigDict(extra="forbid")

    seed_reveal: str = Field(min_length=16, max_length=512)

    @field_validator("seed_reveal")
    @classmethod
    def validate_seed_reveal(cls, value: str) -> str:
        if any(not 0x21 <= ord(character) <= 0x7E for character in value):
            raise ValueError("seed_reveal must contain only visible ASCII characters")
        return value


class ResultPrizeResponse(BaseModel):
    rank: int
    title: str
    prize_paise: int


class PublishedWinnerResponse(BaseModel):
    """Public winner proof excludes the ticket owner's identity."""

    rank: int
    title: str
    prize_paise: int
    ticket_serial_number: int
    selection_counter: int
    selection_digest: str


class AdminWinnerResponse(PublishedWinnerResponse):
    ticket_id: UUID
    owner_user_id: UUID
    prize_awarded: bool
    prize_award_id: UUID | None = None
    journal_group_id: UUID | None = None
    credited_at: datetime | None = None


class DrawAdminResponse(BaseModel):
    """Administrative state without exposing a seed before publication."""

    id: UUID
    series_id: UUID
    series_name: str
    series_status: str
    status: DrawStatus
    algorithm_version: str
    seed_commitment: str
    seed_revealed: bool
    eligible_ticket_count: int
    eligible_tickets_digest: str | None = None
    prize_snapshot: list[ResultPrizeResponse] = Field(default_factory=list)
    result_digest: str | None = None
    committed_at: datetime
    closed_at: datetime | None = None
    drawn_at: datetime | None = None
    prizes_posted_at: datetime | None = None
    published_at: datetime | None = None
    winners: list[AdminWinnerResponse] = Field(default_factory=list)


class PublishedResultResponse(BaseModel):
    """Publicly reproducible result; owner identities and ledger IDs stay private."""

    id: UUID
    series_id: UUID
    series_name: str
    algorithm_version: str
    seed_commitment: str
    seed_reveal: str
    eligible_ticket_count: int
    eligible_tickets_digest: str
    prize_snapshot: list[ResultPrizeResponse]
    result_digest: str
    drawn_at: datetime
    published_at: datetime
    winners: list[PublishedWinnerResponse]


class PublishedCandidatePageResponse(BaseModel):
    """One bounded page of the public candidate manifest."""

    series_id: UUID
    eligible_ticket_count: int
    eligible_tickets_digest: str
    ticket_serial_numbers: list[int]
    next_after_serial_number: int | None = None
