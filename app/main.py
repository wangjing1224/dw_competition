"""FastAPI 应用入口、生命周期和健康检查。"""

from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI

from app.api.audit_tasks import router as audit_task_router
from app.api.bidders import router as bidder_router
from app.api.projects import router as project_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.storage.database import initialize_database
from app.storage.repositories import AuditTaskRepository


def _app_version() -> str:
    try:
        return version("dw-competition")
    except PackageNotFoundError:
        return "0.1.0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.debug)
    settings.ensure_runtime_directories()
    initialize_database(settings.storage.database_path)
    AuditTaskRepository().mark_interrupted_tasks_failed()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=_app_version(),
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    application.include_router(project_router)
    application.include_router(bidder_router)
    application.include_router(audit_task_router)

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": _app_version(),
        }

    return application


app = create_app()
