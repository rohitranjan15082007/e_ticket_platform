"""Balanced journal validation tests."""

from uuid import uuid4

import pytest

from app.exceptions import InvariantViolationError
from app.models.ledger import PostingDirection
from app.services.ledger_service import LedgerService, PostingDraft


def test_ledger_rejects_an_unbalanced_group() -> None:
    postings = [
        PostingDraft(uuid4(), PostingDirection.DEBIT, 100, "INR"),
        PostingDraft(uuid4(), PostingDirection.CREDIT, 99, "INR"),
    ]

    with pytest.raises(InvariantViolationError, match="unbalanced"):
        LedgerService.validate_postings(postings, "INR")


def test_ledger_rejects_mixed_currency_group() -> None:
    postings = [
        PostingDraft(uuid4(), PostingDirection.DEBIT, 100, "INR"),
        PostingDraft(uuid4(), PostingDirection.CREDIT, 100, "USD"),
    ]

    with pytest.raises(InvariantViolationError, match="mix currencies"):
        LedgerService.validate_postings(postings, "INR")
