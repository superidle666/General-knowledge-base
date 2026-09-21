"""
MySQL 数据管理接口的数据校验模型。

这些 Pydantic 模型同时用于请求参数校验和 FastAPI OpenAPI 文档生成，
状态字段的取值范围必须与数据库业务状态保持一致。
"""

from datetime import datetime
from math import ceil, isfinite
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_file_size_limit(value: object) -> object:
    """按整数 MB 保存文件大小限制，小数统一向上取整。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("单文件大小上限必须是数字")
    try:
        size = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("单文件大小上限必须是数字") from exc
    if not isfinite(size):
        raise ValueError("单文件大小上限必须是有效数字")
    rounded_size = ceil(size)
    if rounded_size < 1:
        raise ValueError("单文件大小上限至少为 1 MB")
    if rounded_size > 10240:
        raise ValueError("单文件大小上限不能超过 10240 MB")
    return rounded_size


class UserCreate(BaseModel):
    """创建用户请求模型；初始密码由服务端按账号和手机号生成。"""
    username: str = Field(
        min_length=3,
        max_length=20,
        pattern="^[A-Za-z]{3,20}$",
        description="3 至 20 位英文字母登录账号",
    )
    display_name: str = Field(min_length=1, max_length=100, description="用户显示名称")
    email: str | None = Field(default=None, max_length=255, description="邮箱，可选且必须唯一")
    phone: str = Field(min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$", description="中国大陆 11 位手机号，必填且必须唯一")
    role_codes: list[str] = Field(default_factory=lambda: ["user"], description="角色编码列表")

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: object) -> object:
        """去除手机号首尾空格，使正则校验处理规范化后的值。"""
        if not isinstance(value, str):
            return value
        phone = value.strip()
        if not phone:
            raise ValueError("手机号不能为空")
        return phone

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        email = value.strip()
        if not email:
            return None
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("请输入正确的邮箱地址")
        return email


class UserUpdate(BaseModel):
    """更新用户请求模型；未提交的字段不会覆盖原值。"""
    username: str | None = Field(
        default=None,
        min_length=3,
        max_length=20,
        pattern="^[A-Za-z]{3,20}$",
        description="3 至 20 位英文字母登录账号",
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$", description="中国大陆 11 位手机号")
    status: str | None = Field(default=None, pattern="^(active|disabled|locked)$")
    password: str | None = Field(default=None, min_length=6, max_length=128)
    role_codes: list[str] | None = None

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: object) -> object:
        """去除编辑手机号的首尾空格，使正则校验使用规范化后的值。"""
        if not isinstance(value, str):
            return value
        phone = value.strip()
        return phone or None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        email = value.strip()
        if not email:
            return None
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("请输入正确的邮箱地址")
        return email


class UserOut(BaseModel):
    """用户返回模型；密码哈希等敏感字段不会对外返回。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    email: str | None
    phone: str | None
    display_name: str
    avatar_url: str | None
    status: str
    last_login_at: datetime | None
    roles: list[str] = Field(default_factory=list)


class RoleCreate(BaseModel):
    """创建角色请求模型。"""
    code: str = Field(min_length=2, max_length=64, pattern="^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    permission_codes: list[str] = Field(default_factory=list)


class PermissionUpdate(BaseModel):
    """权限由注册表维护，后台仅允许切换启停状态。"""

    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class PermissionOut(BaseModel):
    """权限返回模型。"""
    id: int
    code: str
    name: str
    description: str | None
    model_config = ConfigDict(from_attributes=True)
    status: str
    deleted_at: datetime | None
    deleted_by: int | None
    parent_id: int | None


class RoleUpdate(BaseModel):
    """更新角色基本信息和权限绑定的请求模型。"""
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    permission_codes: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class RoleOut(BaseModel):
    """角色返回模型，permissions 返回权限编码列表。"""
    id: int
    code: str
    name: str
    description: str | None
    status: str
    deleted_at: datetime | None
    deleted_by: int | None
    permissions: list[str] = Field(default_factory=list)


class LibraryCreate(BaseModel):
    """创建知识库请求模型。"""
    name: str = Field(min_length=1, max_length=150)
    code: str = Field(min_length=2, max_length=64, pattern="^[a-z][a-z0-9-]*$")
    description: str | None = None
    cover_color: str | None = Field(default=None, max_length=20)


    entity_recognition_mode: str = Field(default="disabled", pattern="^(disabled|optional|required)$")
    allowed_file_types: list[str] | None = None
    max_file_size_mb: int | None = Field(default=None, ge=1, le=10240)
    max_document_count: int | None = Field(default=None, ge=1)
    max_upload_file_count: int | None = Field(default=None, ge=1)
    chunking_config: dict[str, Any] | None = None
    visibility: str = Field(default="private", pattern="^(private|shared)$")

    @field_validator("max_file_size_mb", mode="before")
    @classmethod
    def normalize_max_file_size_mb(cls, value: object) -> object:
        return normalize_file_size_limit(value)

class LibraryUpdate(BaseModel):
    """更新知识库信息或启用状态的请求模型。"""
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = None
    cover_color: str | None = Field(default=None, max_length=20)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


    entity_recognition_mode: str | None = Field(default=None, pattern="^(disabled|optional|required)$")
    allowed_file_types: list[str] | None = None
    max_file_size_mb: int | None = Field(default=None, ge=1, le=10240)
    max_document_count: int | None = Field(default=None, ge=1)
    max_upload_file_count: int | None = Field(default=None, ge=1)
    chunking_config: dict[str, Any] | None = None
    visibility: str | None = Field(default=None, pattern="^(private|shared)$")

    @field_validator("max_file_size_mb", mode="before")
    @classmethod
    def normalize_max_file_size_mb(cls, value: object) -> object:
        return normalize_file_size_limit(value)

class LibraryOut(BaseModel):
    """知识库返回模型，document_count 为当前有效文档数量。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: str
    description: str | None
    cover_color: str | None
    status: str
    entity_recognition_mode: str
    allowed_file_types: list[str] | None
    max_file_size_mb: int | None
    max_document_count: int | None
    max_upload_file_count: int | None
    chunking_config: dict[str, Any] | None
    visibility: str
    document_count: int
    created_by: int | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class MemberCreate(BaseModel):
    """添加知识库成员请求模型。"""
    user_id: int
    access_level: str = Field(default="read", pattern="^(read|write|admin)$")


class MemberOut(BaseModel):
    """知识库成员关系返回模型。"""
    model_config = ConfigDict(from_attributes=True)
    library_id: int
    user_id: int
    access_level: str
    created_at: datetime


class DocumentCreate(BaseModel):
    """创建文档元数据请求模型；文件内容由存储服务单独处理。"""
    library_id: int
    title: str = Field(min_length=1, max_length=255)
    original_filename: str = Field(min_length=1, max_length=255)
    storage_key: str = Field(min_length=1, max_length=500)
    mime_type: str | None = None
    file_size: int | None = Field(default=None, ge=0)
    file_hash: str | None = Field(default=None, max_length=128)
    version: int = Field(default=1, ge=1)
    status: str = Field(default="uploaded", pattern="^(uploaded|processing|ready|failed|deleted)$")
    uploaded_by: int | None = None


class DocumentUpdate(BaseModel):
    """更新文档标题、状态或解析错误的请求模型。"""
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, pattern="^(uploaded|processing|ready|failed|deleted)$")
    parse_error: str | None = None


class DocumentOut(BaseModel):
    """文档元数据返回模型，不直接返回文件内容。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    library_id: int
    title: str
    original_filename: str
    storage_key: str
    mime_type: str | None
    file_size: int | None
    file_hash: str | None
    version: int
    status: str
    parse_error: str | None
    uploaded_by: int | None
    published_at: datetime | None
    deleted_by: int | None
    created_at: datetime
    updated_at: datetime


class DocumentListOut(DocumentOut):
    """文档列表模型，补充 Chunk 数量和标签名称。"""
    chunk_count: int = 0
    tags: list[str] = Field(default_factory=list)
    library_name: str | None = None


class DocumentDetailOut(DocumentListOut):
    """文档详情模型，与列表模型保持一致以便前端复用。"""


class DocumentUploadOut(DocumentListOut):
    """本地上传成功后的文档元数据。"""
    task_id: int | None = None


class QueryRequest(BaseModel):
    """用户端知识库查询请求。"""

    library_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=128)
    top_k: int = Field(default=5, ge=1, le=10)
    use_hyde: bool = False
    use_web_search: bool = False

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("查询内容不能为空")
        return normalized


class QuerySourceOut(BaseModel):
    """用户端展示的单条知识库来源。"""

    chunk_id: str | int | None = None
    document_id: int | None = None
    document_title: str | None = None
    chunk_index: int | None = None
    page_number: int | None = None
    section_title: str | None = None
    parent_title: str | None = None
    content: str = ""
    score: float | None = None
    source: str = "local"


class QueryLibraryOut(BaseModel):
    """用户端可选择的知识库摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    cover_color: str | None
    status: str
    document_count: int
    updated_at: datetime


class QueryResponse(BaseModel):
    """知识库问答结果，包含答案和可追溯来源。"""

    library_id: int
    session_id: str | None = None
    query: str
    answer: str
    sources: list[QuerySourceOut] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)


class QuerySessionOut(BaseModel):
    """登录用户的已保存会话摘要。"""

    id: str
    library_id: int
    title: str
    created_at: float
    updated_at: float


class QuerySessionCreate(BaseModel):
    """创建登录用户的新会话。"""

    library_id: int
    title: str = Field(default="新建技术咨询", min_length=1, max_length=64)

class QuerySessionUpdate(BaseModel):
    """更新历史会话标题。"""

    title: str = Field(min_length=1, max_length=64)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("会话名称不能为空")
        return title


class QueryChatMessageOut(BaseModel):
    """用于恢复用户端历史会话的单条消息。"""

    id: str
    role: str
    content: str
    created_at: float
    sources: list[QuerySourceOut] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)


class QuerySessionMessagesOut(BaseModel):
    """单个会话的完整消息记录。"""

    session_id: str
    library_id: int
    messages: list[QueryChatMessageOut] = Field(default_factory=list)



class ImportTaskOut(BaseModel):
    """文档导入任务状态返回模型。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    library_id: int
    document_id: int
    status: str
    current_step: str | None
    progress: int
    config_snapshot: dict[str, Any] | None
    entity_result: dict[str, Any] | None
    chunk_count: int | None
    error_message: str | None
    created_by: int | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    document_title: str | None = None
    library_name: str | None = None
class ChunkCreate(BaseModel):
    """创建文档分段请求模型；vector_id 用于关联向量数据库记录。"""
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1)
    token_count: int | None = Field(default=None, ge=0)
    page_number: int | None = Field(default=None, ge=1)
    section_title: str | None = None
    vector_id: str | None = None
    metadata: dict[str, Any] | None = None


class ChunkOut(ChunkCreate):
    """文档分段返回模型。"""
    model_config = ConfigDict(from_attributes=True)
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="extra_metadata", serialization_alias="metadata")
    id: int
    document_id: int
    created_at: datetime


class TagCreate(BaseModel):
    """创建文档标签请求模型。"""
    name: str = Field(min_length=1, max_length=100)


class TagUpdate(BaseModel):
    """更新文档标签名称请求模型。"""
    name: str = Field(min_length=1, max_length=100)


class TagOut(BaseModel):
    """文档标签返回模型。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_at: datetime
    updated_at: datetime


class DocumentTagsUpdate(BaseModel):
    """覆盖文档标签关联；标签不存在时自动创建。"""
    tag_names: list[str] = Field(default_factory=list, max_length=30)


class OperationLogOut(BaseModel):
    """管理操作日志返回模型。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int | None
    user_display_name: str | None = None
    operation: str
    resource_type: str | None
    resource_id: str | None
    resource_display_name: str | None = None
    request_ip: str | None
    detail: dict[str, Any] | None
    created_at: datetime

class OperationLogPageOut(BaseModel):
    """Paginated operation-log result."""
    items: list[OperationLogOut] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class LoginRequest(BaseModel):
    """账号密码登录请求。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RefreshTokenRequest(BaseModel):
    """刷新 access token 的请求。"""

    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    """退出时提交当前设备的刷新令牌以撤销会话。"""

    refresh_token: str | None = None


class AuthUserOut(BaseModel):
    """当前登录用户的安全公开信息。"""

    id: int
    username: str
    display_name: str
    avatar_url: str | None
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class TokenPairOut(BaseModel):
    """短期访问令牌与可轮换刷新令牌。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserOut


class SystemSettingUpdate(BaseModel):
    """Update one administrator-managed setting."""

    value: str | None = Field(default=None, max_length=4096)


class SystemSettingOut(BaseModel):
    """Safe setting representation; sensitive values are never returned."""

    key: str
    name: str
    category: str
    description: str | None
    is_sensitive: bool
    value: str | None
    masked_value: str | None
    has_value: bool
    updated_at: datetime
    updated_by: int | None
