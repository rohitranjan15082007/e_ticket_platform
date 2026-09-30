"""Bounded offset pagination for operational read endpoints."""

from dataclasses import dataclass

from sqlalchemy.sql import Select


@dataclass(frozen=True, slots=True)
class PageWindow:
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or not 0 <= self.offset <= 1_000_000:
            raise ValueError("offset must be an integer from 0 to 1000000")

    def apply(self, statement: Select) -> Select:
        """Apply a window after the caller chooses a stable sort order."""

        return statement.limit(self.limit).offset(self.offset)
