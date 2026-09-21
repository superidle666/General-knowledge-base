"""服务存活、数据库探针和 API 能力发现接口。"""

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import settings
from backend.app.db.session import SessionLocal

router = APIRouter(tags=["system"])


@router.get("/health", summary="服务健康检查")
async def health() -> dict:
    """返回 API 进程的基础运行状态。"""
    return {"code": 0, "message": "ok", "data": {"status": "ok", "service": "knowledge-base-api", "version": settings.app_version, "environment": settings.environment}}


@router.get("/health/db", summary="MySQL 数据库健康检查")
async def database_health() -> dict:
    """检查 MySQL 连通性。

    数据库异常转换为业务状态响应，避免健康检查本身产生 500。
    """
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"code": 0, "message": "ok", "data": {"status": "ok", "database": "mysql"}}
    except SQLAlchemyError:
        return {"code": 1, "message": "database unavailable", "data": {"status": "error", "database": "mysql"}}


@router.get("/api", summary="API 能力列表")
async def api_index() -> dict:
    """列出统一入口下的主要 API 分组。"""
    return {"code": 0, "message": "ok", "data": {"name": settings.app_name, "version": settings.app_version, "groups": {"query": f"{settings.api_v1_prefix}/query", "management": settings.api_v1_prefix}}}
