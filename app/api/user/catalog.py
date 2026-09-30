"""Read-only public catalog endpoints."""

from uuid import UUID

from fastapi import APIRouter

from app.dependencies import SessionDependency
from app.schemas.ticket_package import TicketPackageResponse
from app.schemas.ticket_series import TicketSeriesResponse
from app.services.catalog_service import CatalogService
from app.services.ticket_package_service import TicketPackageService


router = APIRouter(prefix="/catalog", tags=["ticket catalog"])


@router.get("/series", response_model=list[TicketSeriesResponse])
async def list_series(session: SessionDependency) -> list[TicketSeriesResponse]:
    catalog = CatalogService(session)
    series = await catalog.list_available_series()
    return [TicketSeriesResponse.model_validate(await catalog.public_series_snapshot(value)) for value in series]


@router.get("/series/{series_id}", response_model=TicketSeriesResponse)
async def get_series(series_id: UUID, session: SessionDependency) -> TicketSeriesResponse:
    catalog = CatalogService(session)
    series = await catalog.get_available_series(series_id)
    return TicketSeriesResponse.model_validate(await catalog.public_series_snapshot(series))


@router.get("/packages", response_model=list[TicketPackageResponse])
async def list_packages(session: SessionDependency) -> list[TicketPackageResponse]:
    packages = await CatalogService(session).list_available_packages()
    return [TicketPackageResponse.model_validate(TicketPackageService.snapshot(value)) for value in packages]


@router.get("/packages/{package_id}", response_model=TicketPackageResponse)
async def get_package(package_id: UUID, session: SessionDependency) -> TicketPackageResponse:
    package = await CatalogService(session).get_available_package(package_id)
    return TicketPackageResponse.model_validate(TicketPackageService.snapshot(package))
