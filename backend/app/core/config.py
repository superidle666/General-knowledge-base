"""统一 API 服务配置。

当前仅集中管理 API 外壳所需配置，旧业务服务迁移完成前仍保留其专属配置。
"""

from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


def _csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """解析逗号分隔的环境变量，并过滤空白项。"""

    if not value:
        return default
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    return values or default


@dataclass(frozen=True)
class Settings:
    """统一 FastAPI 外壳使用的运行参数。"""

    app_name: str = os.getenv(
        "APP_NAME", "设备技术文档智能知识库平台"
    )
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    environment: str = os.getenv("APP_ENV", "development")
    api_v1_prefix: str = os.getenv("API_V1_PREFIX", "/api/v1")
    cors_allow_origins: tuple[str, ...] = _csv(
        os.getenv("CORS_ALLOW_ORIGINS"), ("*",)
    )


    mysql_url: str = os.getenv("MYSQL_URL", "mysql+asyncmy://knowledge:knowledge@127.0.0.1:3306/knowledge_base?charset=utf8mb4")
    sql_echo: bool = os.getenv("SQL_ECHO", "0").lower() in {"1", "true", "yes"}
    # JWT 密钥只允许通过环境变量注入，应用启动时会校验其存在。
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_access_token_minutes: int = int(os.getenv("JWT_ACCESS_TOKEN_MINUTES", "30"))
    jwt_refresh_token_days: int = int(os.getenv("JWT_REFRESH_TOKEN_DAYS", "7"))
    # Dedicated environment-only encryption key for persisted sensitive settings.
    settings_encryption_key: str = os.getenv("SETTINGS_ENCRYPTION_KEY", "")
settings = Settings()

