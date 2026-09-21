# -*- coding: utf-8 -*-
"""
后台操作审计工具。
@author: 项目维护者
@date: 2026-09-02
@desc: 绑定请求操作者上下文并在业务事务中记录管理操作。
@business: 审计详情必须过滤密码、令牌和密钥等敏感字段。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from backend.app.core.auth import AuthenticatedUser
from backend.app.db.models import OperationLog

_audit_actor: ContextVar[Any | None] = ContextVar("audit_actor", default=None)
_audit_request_ip: ContextVar[str | None] = ContextVar("audit_request_ip", default=None)

_SENSITIVE_DETAIL_KEYS = {
    "password", "password_hash", "token", "access_token", "refresh_token",
    "authorization", "api_key", "secret", "key",
}


def request_ip(request: Request) -> str | None:
    """Return the direct client address for audit storage."""

    return request.client.host if request.client else None


def bind_audit_context(user: 'AuthenticatedUser', request: Request) -> None:
    """Attach authenticated request identity to subsequent write logs."""

    _audit_actor.set(user)
    _audit_request_ip.set(request_ip(request))



# NOTE: 审计日志用于追踪行为而非存储认证材料，敏感字段必须在写库前统一剔除。
def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    if not detail:
        return None
    return {
        key: value
        for key, value in detail.items()
        if key.lower() not in _SENSITIVE_DETAIL_KEYS
    } or None


async def log_operation(
    session: AsyncSession,
    operation: str,
    resource_type: str,
    resource_id: int | str | None,
    detail: dict[str, Any] | None = None,
    *,
    actor_id: int | None = None,
    client_ip: str | None = None,
) -> None:
    """Queue a concise audit entry in the caller's transaction."""

    actor = _audit_actor.get()
    session.add(OperationLog(
        user_id=actor_id if actor_id is not None else (actor.id if actor else None),
        operation=operation,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        request_ip=client_ip if client_ip is not None else _audit_request_ip.get(),
        detail=_safe_detail(detail),
    ))
