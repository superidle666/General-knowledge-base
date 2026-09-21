# -*- coding: utf-8 -*-
"""
后台认证接口。
@author: 项目维护者
@date: 2026-09-02
@desc: 提供登录、刷新令牌、退出登录与当前用户查询。
@business: 已停用、锁定或软删除的账号不得获取或续期管理会话。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.schemas import AuthUserOut, LoginRequest, LogoutRequest, RefreshTokenRequest, TokenPairOut
from backend.app.core.audit import log_operation, request_ip
from backend.app.core.auth import (
    AuthenticatedUser, TokenValidationError, decode_token, encode_token, get_current_user,
    load_active_user, to_authenticated_user, verify_password,
)
from backend.app.core.config import settings
from backend.app.db.models import AuthRefreshToken, Role, User
from backend.app.db.session import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


def _token_hash(token: str) -> str:
    """生成刷新令牌的 SHA-256 摘要。

    Note:
        数据库只保存摘要，不保存可被直接复用的原始令牌。
    """
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def _user_out(user: AuthenticatedUser) -> dict:
    """构造认证接口返回的最小用户信息。

    Note:
        仅暴露前端鉴权所需的角色和权限，不返回密码等内部字段。
    """
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "avatar_url": user.avatar_url, "roles": list(user.roles),
        "permissions": sorted(user.permissions),
    }


async def _issue_pair(session: AsyncSession, user: User) -> tuple[dict, AuthRefreshToken]:
    """签发访问令牌与刷新令牌，并登记刷新令牌摘要。

    Note:
        调用方必须在同一事务中提交令牌记录，避免返回成功但服务端无法续期。
    """
    now = datetime.now(timezone.utc)
    access_expiry = now + timedelta(minutes=settings.jwt_access_token_minutes)
    refresh_expiry = now + timedelta(days=settings.jwt_refresh_token_days)
    access_token = encode_token(user.id, "access", access_expiry, secrets.token_urlsafe(24))
    refresh_id = secrets.token_urlsafe(24)
    refresh_token = encode_token(user.id, "refresh", refresh_expiry, refresh_id)
    record = AuthRefreshToken(
        user_id=user.id, token_id=refresh_id, token_hash=_token_hash(refresh_token),
        expires_at=refresh_expiry.replace(tzinfo=None),
    )
    session.add(record)
    return {
        "access_token": access_token, "refresh_token": refresh_token,
        "token_type": "bearer", "expires_in": settings.jwt_access_token_minutes * 60,
        "user": _user_out(to_authenticated_user(user)),
    }, record



# 登录成功后在同一事务内更新最近登录时间并写审计日志，避免出现“已登录但无日志”的不一致记录。
@router.post("/login", response_model=TokenPairOut)
async def login(payload: LoginRequest, request: Request, session: AsyncSession = Depends(get_db)):
    """管理员登录接口。

    Args:
        payload: 用户名和密码。
        request: 当前请求，用于记录客户端地址。
        session: 数据库会话。
    Returns:
        TokenPairOut: 访问令牌、刷新令牌及用户权限。
    Raises:
        HTTPException: 账号密码错误返回 401；账号停用、锁定或删除返回 403。
    Note:
        最近登录时间、刷新令牌和登录审计日志必须在同一事务中写入。
    """
    result = await session.execute(
        select(User).options(selectinload(User.roles).selectinload(Role.permissions)).where(User.username == payload.username)
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号或密码错误")
    if user.deleted_at is not None or user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用、锁定或删除")
    user.last_login_at = datetime.now()
    result, _ = await _issue_pair(session, user)
    await log_operation(session, "login", "auth_session", user.id, {"username": user.username}, actor_id=user.id, client_ip=request_ip(request))
    await session.commit()
    return result



# NOTE: 刷新令牌采用轮换策略，旧令牌在签发新令牌后必须立即撤销，降低泄露后的可用窗口。
@router.post("/refresh", response_model=TokenPairOut)
async def refresh(payload: RefreshTokenRequest, session: AsyncSession = Depends(get_db)):
    """轮换刷新令牌并续期管理会话。

    Args:
        payload: 当前刷新令牌。
        session: 数据库会话。
    Returns:
        TokenPairOut: 新的访问令牌和刷新令牌。
    Raises:
        HTTPException: 令牌签名、摘要、有效期、撤销状态或账号状态不合法时返回 401。
    Note:
        旧刷新令牌在新令牌签发后立即撤销，降低泄露令牌的可用窗口。
    """
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except TokenValidationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "刷新令牌无效或已过期") from exc
    record = await session.scalar(select(AuthRefreshToken).where(
        AuthRefreshToken.token_id == claims["jti"], AuthRefreshToken.user_id == int(claims["sub"])
    ))
    expired = record is None or record.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    if expired or record.revoked_at is not None or record.token_hash != _token_hash(payload.refresh_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "刷新令牌已失效")
    user = await load_active_user(session, record.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已停用、锁定或删除")
    result, replacement = await _issue_pair(session, user)
    record.revoked_at = datetime.now()
    record.replaced_by_token_id = replacement.token_id
    await session.commit()
    return result


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """退出当前管理会话。

    Args:
        payload: 可选刷新令牌。
        request: 当前请求，用于记录客户端地址。
        current_user: 已认证的操作用户。
        session: 数据库会话。
    Returns:
        None: 成功或令牌不可用时均返回 204。
    Note:
        只能撤销属于当前用户的刷新令牌；无效令牌按幂等退出处理。
    """
    if not payload.refresh_token:
        return
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except TokenValidationError:
        return
    if int(claims["sub"]) != current_user.id:
        return
    record = await session.scalar(select(AuthRefreshToken).where(AuthRefreshToken.token_id == claims["jti"]))
    if record is not None:
        record.revoked_at = datetime.now()
        await log_operation(session, "logout", "auth_session", current_user.id, {"username": current_user.username}, actor_id=current_user.id, client_ip=request_ip(request))
        await session.commit()


@router.get("/me", response_model=AuthUserOut)
async def me(current_user: AuthenticatedUser = Depends(get_current_user)):
    """获取当前认证用户的授权上下文。

    Args:
        current_user: 已通过访问令牌校验的用户。
    Returns:
        AuthUserOut: 前端路由和功能权限控制所需的用户、角色与权限信息。
    """
    return _user_out(current_user)

