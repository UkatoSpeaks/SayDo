import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import SessionDep
from app.schemas.common import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

DB_CHECK_TIMEOUT_SECONDS = 3.0


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
)
async def health(request: Request, session: SessionDep) -> JSONResponse:
    try:
        async with asyncio.timeout(DB_CHECK_TIMEOUT_SECONDS):
            await session.execute(text("SELECT 1"))
        database, status_code = "ok", 200
    except Exception:
        logger.exception("Health check: database unreachable")
        database, status_code = "unreachable", 503

    body = HealthResponse(
        status="ok" if status_code == 200 else "degraded",
        app=request.app.title,
        version=request.app.version,
        database=database,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())
