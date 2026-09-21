"""认证与管理端权限依赖。

@desc: 提供 JWT 签发校验、密码哈希验证、当前用户解析和管理接口权限映射。
@business: 每次请求均依据数据库中的有效角色和权限重新鉴权；停用、锁定或软删除账号不得访问管理接口。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.audit import bind_audit_context
from backend.app.core.config import settings
from backend.app.core.permissions import expand_permissions, is_registered_permission
from backend.app.db.models import Role, User
from backend.app.db.session import get_db

_bearer = HTTPBearer(auto_error=False)


class TokenValidationError(Exception):
    """令牌格式错误、已过期或类型不匹配时抛出的认证异常。"""


@dataclass(frozen=True)
class AuthenticatedUser:
    """请求鉴权使用的最小用户身份快照。

角色和权限在认证阶段完成加载，避免业务接口重复查询关联数据。
"""

    id: int
    username: str
    display_name: str
    avatar_url: str | None
    roles: tuple[str, ...]
    permissions: frozenset[str]

    @property
    def is_system_admin(self) -> bool:
        return "admin" in self.roles


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(value: str) -> str:
    return _b64encode(hmac.new(settings.jwt_secret.encode(), value.encode(), hashlib.sha256).digest())


def encode_token(subject: int, token_type: str, expires_at: datetime, token_id: str) -> str:
    """使用 HMAC-SHA256 签发紧凑 JWT。

NOTE: 保持当前项目无额外 JWT 运行时依赖；令牌载荷只保存用户标识、令牌类型、唯一标识与过期时间。
"""

    now = datetime.now(timezone.utc)
    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64encode(json.dumps({
        "sub": str(subject), "type": token_type, "jti": token_id,
        "iat": int(now.timestamp()), "exp": int(expires_at.timestamp()),
    }, separators=(",", ":")).encode())
    unsigned = f"{header}.{payload}"
    return f"{unsigned}.{_sign(unsigned)}"


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """校验令牌签名、有效期和类型后返回载荷。

Args:
    token: 客户端提交的 JWT。
    expected_type: 当前场景允许的令牌类型，例如 access 或 refresh。
Returns:
    dict[str, Any]: 已通过校验的令牌载荷。
Raises:
    TokenValidationError: 令牌格式、签名、类型、主体或过期时间不合法时抛出。
"""

    try:
        header, encoded_payload, signature = token.split(".")
        unsigned = f"{header}.{encoded_payload}"
        if not hmac.compare_digest(signature, _sign(unsigned)):
            raise TokenValidationError
        payload = json.loads(_b64decode(encoded_payload))
        if payload.get("type") != expected_type or not payload.get("sub") or not payload.get("jti"):
            raise TokenValidationError
        if int(payload["exp"]) <= int(datetime.now(timezone.utc).timestamp()):
            raise TokenValidationError
        int(payload["sub"])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TokenValidationError) as exc:
        raise TokenValidationError("令牌无效或已过期") from exc
    return payload


def hash_password(password: str) -> str:
    """为新写入密码生成 PBKDF2 哈希。

NOTE: 每次生成独立盐值，避免相同密码产生相同的持久化摘要。
"""

    salt = secrets.token_hex(16)
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return "pbkdf2_sha256$" + str(iterations) + "$" + salt + "$" + digest


def verify_password(password: str, stored_hash: str) -> bool:
    """校验当前 PBKDF2 密码摘要并兼容历史 SHA-256 摘要。

NOTE: 保留历史格式兼容仅用于已存在账号；后续密码写入统一使用 PBKDF2。
"""

    try:
        algorithm, iteration_text, salt, digest = stored_hash.split("$", 3)
        if algorithm == "pbkdf2_sha256":
            calculated = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iteration_text)).hex()
            return hmac.compare_digest(calculated, digest)
    except ValueError:
        pass
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored_hash)


async def load_active_user(session: AsyncSession, user_id: int) -> User | None:
    result = await session.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == user_id, User.deleted_at.is_(None), User.status == "active")
    )
    return result.scalar_one_or_none()


def to_authenticated_user(user: User) -> AuthenticatedUser:
    active_roles = [role for role in user.roles if role.status == "active" and role.deleted_at is None]
    permissions = {
        permission.code
        for role in active_roles
        for permission in role.permissions
        if permission.status == "active" and permission.deleted_at is None and is_registered_permission(permission.code)
    }
    return AuthenticatedUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        roles=tuple(role.code for role in active_roles),
        permissions=expand_permissions(permissions),
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """解析访问令牌并加载用户当前有效的角色与权限。

NOTE: 不直接信任令牌中的权限信息；每次请求重新读取有效角色，确保停用和权限变更立即生效。
"""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "未登录或访问令牌无效", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_token(credentials.credentials, "access")
    except TokenValidationError as exc:
        raise HTTPException(401, "访问令牌无效或已过期", headers={"WWW-Authenticate": "Bearer"}) from exc
    user = await load_active_user(session, int(claims["sub"]))
    if user is None:
        raise HTTPException(401, "账号已停用、锁定或删除")
    return to_authenticated_user(user)


async def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> AuthenticatedUser | None:
    """在可匿名访问场景中按需解析当前用户。

未携带令牌时返回 None；携带但无效的令牌仍返回 401，避免错误令牌被当作匿名请求忽略。
"""

    if credentials is None:
        return None
    try:
        claims = decode_token(credentials.credentials, "access")
    except TokenValidationError as exc:
        raise HTTPException(401, "访问令牌无效或已过期", headers={"WWW-Authenticate": "Bearer"}) from exc
    user = await load_active_user(session, int(claims["sub"]))
    if user is None:
        raise HTTPException(401, "账号已停用、锁定或删除")
    return to_authenticated_user(user)


def _management_permission(request: Request) -> str:
    """将管理端接口映射为代码注册的权限编码。

NOTE: 新增管理接口必须在此处补充映射，否则默认拒绝访问，避免出现未配置授权规则的接口。
"""

    path = request.url.path.removeprefix(settings.api_v1_prefix)
    method = request.method
    if path.startswith("/users"):
        return {"GET": "user:view", "POST": "user:create", "PATCH": "user:modify", "DELETE": "user:delete"}[method]
    if path.startswith("/roles"):
        return {"GET": "role:view", "POST": "role:create", "PATCH": "role:modify", "DELETE": "role:delete"}[method]
    if path.startswith("/permissions"):
        return "permission:view" if method == "GET" else "permission:toggle"
    if path.startswith("/libraries"):
        if "/members" in path:
            return "library:read" if method == "GET" else "library:modify"
        return {"GET": "library:read", "POST": "library:create", "PATCH": "library:modify", "DELETE": "library:delete"}[method]
    if path.startswith("/import-tasks"):
        return "document:view" if method == "GET" else "document:modify"
    if path.startswith("/documents"):
        if path.endswith("/chunks"):
            return "document:view"
        if path.endswith("/tags"):
            return "document:modify"
        if path.endswith("/upload"):
            return "document:upload"
        return {"GET": "document:view", "POST": "document:upload", "PATCH": "document:modify", "DELETE": "document:delete"}[method]
    if path.startswith("/tags"):
        return "document:view" if method == "GET" else "document:modify"
    if path.startswith("/operation-logs"):
        return "session:view_all"
    # NOTE: 仪表盘只汇总本月问答会话统计与最近操作日志，数据范围与 /operation-logs 一致，因此复用会话查看权限，不额外注册权限编码。
    if path.startswith("/dashboard"):
        return "session:view_all"
    raise HTTPException(status.HTTP_403_FORBIDDEN, "当前接口没有授权规则")


async def require_management_access(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """校验管理接口访问权限并绑定审计上下文。

NOTE: 系统管理员拥有管理端全量权限；其他用户必须具备当前路由和请求方法映射的权限编码。
"""

    required_permission = _management_permission(request)
    if current_user.is_system_admin or required_permission in current_user.permissions:
        bind_audit_context(current_user, request)
        return current_user
    raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账号没有执行此操作的权限")

