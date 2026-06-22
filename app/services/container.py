"""按请求构造服务依赖，避免模块导入阶段连接模型或创建全局客户端。"""

from app.core.config import get_settings
from app.llm.factory import build_model_registry
from app.services.background_runner import background_runner
from app.services.bidder_service import BidderService
from app.services.project_service import ProjectService


def build_project_service() -> ProjectService:
    settings = get_settings()
    return ProjectService(settings, build_model_registry(settings))


def build_bidder_service() -> BidderService:
    settings = get_settings()
    return BidderService(settings, build_model_registry(settings))


def build_audit_task_service():
    """延迟导入 Agent 和报告模块，保持应用健康检查不依赖模型 Key。"""

    from app.graph.audit_task_graph import LangGraphAuditExecutor
    from app.reports.service import ReportService
    from app.services.audit_task_service import AuditTaskService

    settings = get_settings()
    models = build_model_registry(settings)
    return AuditTaskService(
        settings,
        models.embedding,
        LangGraphAuditExecutor(settings, models),
        ReportService(),
        background_runner,
    )
