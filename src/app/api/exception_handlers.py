from __future__ import annotations

from starlette import status
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.app.api.schemas.result import ErrorModel
from src.common import MyBaseError


def get_error_status_code(exc: Exception) -> int:
    """Get HTTP status code for an exception."""
    match exc:
        case MyBaseError() as base_exc:
            return base_exc.code_status or status.HTTP_500_INTERNAL_SERVER_ERROR
        case _:
            return status.HTTP_500_INTERNAL_SERVER_ERROR


def exception_response(req: Request, err: Exception) -> JSONResponse:
    """Create a JSON response for an exception."""
    return JSONResponse(
        headers={"Access-Control-Allow-Origin": "*"},
        status_code=get_error_status_code(err),
        content=ErrorModel.make(err=err, req=req).model_dump(),
    )
