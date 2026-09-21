"""异步 SQLAlchemy 引擎、会话工厂和请求级依赖。"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import settings


class Base(DeclarativeBase):
    """Base class for all MySQL ORM models."""


engine = create_async_engine(
    settings.mysql_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.sql_echo,
)
# NOTE: pool_recycle 配合 MySQL 空闲连接超时，避免长期运行时复用失效连接。

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """为每个请求提供一个异步数据库会话。"""

    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """为本地开发创建缺失表。

    生产环境应使用 ``alembic upgrade head`` 管理结构变更。
    """

    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
