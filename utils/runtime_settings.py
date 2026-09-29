# -*- coding: utf-8 -*-
"""
系统设置运行时快照（P0-1）。
utils/runtime_settings.py

@Description: 把管理端「系统设置」发布到 Redis，供 API 与 Worker 两个进程在同步上下文里读取。
@BusinessRule: 库配置优先、.env 兜底。get_runtime_setting 返回 None 表示「未配置/不可用」，
    调用方必须回落到 config/* 的 env 值；配置读取失败绝不允许拖垮问答与导入主链路。
    快照含密钥明文，落 Redis 前用 SETTINGS_ENCRYPTION_KEY 做 Fernet 加密，Redis 里不存明文。
@WhyRedis: get_llm_client 等消费点是同步函数，而项目 MySQL 只有 asyncmy 异步驱动；
    引入 pymysql 只为读 4 个配置键不划算，Redis 是两个进程都已依赖的核心组件，同步客户端零新依赖。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import redis
from sqlalchemy import select

from backend.app.core.system_settings import decrypt_value, encrypt_value
from backend.app.db.models import SystemSetting
from config.redis_config import redis_config

logger = logging.getLogger(__name__)

# 快照 key：全表打包成一个 Fernet token，读一次 GET 即得全部配置。
SNAPSHOT_REDIS_KEY = "kb:system_settings:snapshot"
# 进程内缓存 TTL：多进程部署下不引入广播失效，接受最长 30 秒的生效延迟。
SNAPSHOT_TTL_SECONDS = 30.0

_redis_lock = threading.Lock()
_redis_client: redis.Redis | None = None

_snapshot: dict[str, str] | None = None
_snapshot_at: float = 0.0
_snapshot_lock = threading.Lock()


def _get_redis_client() -> redis.Redis:
    """同步 Redis 客户端懒加载单例：模块级锁 + 快路径判空 + 锁内复查，构造成功才绑定。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is None:
            _redis_client = redis.Redis.from_url(
                redis_config.url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
    return _redis_client


async def publish_settings_snapshot(session) -> None:
    """把 system_settings 全表打包成加密快照写入 Redis；由 API 在启动与保存后调用。

    DB 才是事实源，快照只是分发缓存：发布失败会抛异常由调用方降级（记日志即可），
    下次启动或下一次任意配置保存都会自愈；API 进程内发布成功后顺带失效本地缓存。
    """
    rows = (await session.execute(select(SystemSetting))).scalars().all()
    snapshot = {row.setting_key: (decrypt_value(row.encrypted_value) or "") for row in rows}
    token = encrypt_value(json.dumps(snapshot, ensure_ascii=False))
    _get_redis_client().set(SNAPSHOT_REDIS_KEY, token)
    invalidate_runtime_settings_cache()


def _fetch_snapshot() -> dict[str, str] | None:
    """读并解密快照。返回 {} 表示 Redis 可达但尚无快照；None 表示不可用（连接/解密失败）。"""
    try:
        token = _get_redis_client().get(SNAPSHOT_REDIS_KEY)
        if not token:
            return {}
        loaded = json.loads(decrypt_value(token))
        return {str(key): str(value) for key, value in loaded.items()}
    except Exception:
        # 这里吞异常是故意的：快照是加速层不是事实源，失败语义 = 沿用旧值或 env 兜底。
        logger.warning("读取系统设置快照失败，沿用旧配置或环境变量兜底", exc_info=False)
        return None


def get_runtime_setting(key: str) -> str | None:
    """取管理端在线配置值；None 表示未配置，调用方应回落 env。TTL 内命中缓存零网络。"""
    global _snapshot, _snapshot_at
    now = time.monotonic()
    if _snapshot is not None and now - _snapshot_at < SNAPSHOT_TTL_SECONDS:
        return _snapshot.get(key) or None
    with _snapshot_lock:
        now = time.monotonic()
        if _snapshot is not None and now - _snapshot_at < SNAPSHOT_TTL_SECONDS:
            return _snapshot.get(key) or None
        fresh = _fetch_snapshot()
        if fresh is not None:
            _snapshot = fresh
        elif _snapshot is None:
            # 从未成功读过：缓存空快照，避免 Redis 故障期间每次建客户端都打一次网络。
            _snapshot = {}
        # 读取失败但已有旧快照：stale-while-error，继续沿用旧值。
        _snapshot_at = time.monotonic()
        return (_snapshot or {}).get(key) or None


def invalidate_runtime_settings_cache() -> None:
    """强制下次读取重新拉快照（本进程发布新快照后调用，做到保存即生效）。"""
    global _snapshot_at
    _snapshot_at = 0.0
