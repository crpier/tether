"""Authenticated browser routes for inspecting Product observations."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response

from tether import product_observation_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.product_observation_capabilities import (
    PRODUCT_OBSERVATION_ERRORS,
    ProductObservationRead,
)
from tether.product_observation_errors import ProductObservationNotFoundError


class ResolveProductObservationRequest(BaseModel):
    """Observed version required to resolve Product feedback."""

    version: PositiveInt


def _path_observation_id(raw_id: str) -> UUID:
    """Parse a Product-observation path identity as not-found on failure."""
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise ProductObservationNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(PRODUCT_OBSERVATION_ERRORS)
router = APIRouter()


@router.get(
    "/api/product-observations",
    response_model=list[ProductObservationRead],
)
async def list_product_observations(request: Request) -> Response:
    """List unresolved Product observations newest first."""
    return rest_response(await product_observation_capabilities.list_open(request))


@router.post(
    "/api/product-observations/{observation_id}/resolve",
    response_model=ProductObservationRead,
)
@_translate_domain_errors
async def resolve_product_observation(
    request: Request,
    body: ResolveProductObservationRequest,
    observation_id: str,
) -> Response:
    """Resolve one Product observation at its observed version."""
    return rest_response(
        await product_observation_capabilities.resolve(
            request,
            _path_observation_id(observation_id),
            body.version,
        )
    )
