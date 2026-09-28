"""FastAPI 应用入口、生命周期和健康检查。"""

from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.audit_tasks import router as audit_task_router
from app.api.bidders import router as bidder_router
from app.api.projects import router as project_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.llm.bailian_retrieval import close_retrieval_clients
from app.storage.bidder_management import mark_interrupted_bidders
from app.storage.database import initialize_database
from app.storage.project_management import mark_interrupted_projects
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
    mark_interrupted_bidders()
    mark_interrupted_projects()
    try:
        yield
    finally:
        await close_retrieval_clients()


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

    # 演示前端使用原生静态文件，不引入 Node 构建链。StaticFiles 必须最后挂载，
    # 避免 /web 的通配路径影响前面已经注册的 API。
    web_root = Path(__file__).resolve().parent / "web"
    application.mount(
        "/web",
        StaticFiles(directory=web_root, html=True),
        name="web",
    )

    @application.get("/", include_in_schema=False)
    async def web_home() -> RedirectResponse:
        return RedirectResponse(url="/web/")

    return application


app = create_app()
