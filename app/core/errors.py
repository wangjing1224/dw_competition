"""统一业务异常和 FastAPI 错误响应。"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details


class NotFoundError(BusinessError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, http_status=404)


class StateConflict(BusinessError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "state_conflict",
            message,
            http_status=409,
            details=details,
        )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BusinessError)
    async def handle_business_error(
        _: Request, exc: BusinessError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把 multipart、路径参数和请求体校验错误统一为项目错误结构。"""

        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": "请求参数不符合接口要求",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        """记录完整异常，但响应中不泄露路径、Key 或堆栈。"""

        logger.exception("未处理的后端异常", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": getattr(exc, "code", "internal_server_error"),
                    "message": "后端处理失败，请查看服务日志",
                    "details": None,
                }
            },
        )
