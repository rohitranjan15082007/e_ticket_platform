"""Public, published-only result checking endpoints."""

from uuid import UUID

from fastapi import APIRouter, Query

from app.dependencies import SessionDependency
from app.schemas.result import PublishedCandidatePageResponse, PublishedResultResponse
from app.services.result_service import ResultService


router = APIRouter(prefix="/results", tags=["results"])


@router.get("", response_model=list[PublishedResultResponse])
async def list_results(
    session: SessionDependency, limit: int = Query(default=100, ge=1, le=100)
) -> list[PublishedResultResponse]:
    results = await ResultService(session).list_published(limit=limit)
    return [PublishedResultResponse.model_validate(result) for result in results]


@router.get("/{series_id}/candidates", response_model=PublishedCandidatePageResponse)
async def get_result_candidates(
    series_id: UUID,
    session: SessionDependency,
    after_serial_number: int = Query(default=0, ge=0),
    limit: int = Query(default=1_000, ge=1, le=1_000),
) -> PublishedCandidatePageResponse:
    """Expose the published candidate manifest without unbounded result payloads."""

    result = await ResultService(session).get_published_candidates(
        series_id=series_id,
        after_serial_number=after_serial_number,
        limit=limit,
    )
    return PublishedCandidatePageResponse.model_validate(result)


@router.get("/{series_id}", response_model=PublishedResultResponse)
async def get_result(series_id: UUID, session: SessionDependency) -> PublishedResultResponse:
    result = await ResultService(session).get_published(series_id=series_id)
    return PublishedResultResponse.model_validate(result)
