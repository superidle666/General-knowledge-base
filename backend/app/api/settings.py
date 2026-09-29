# -*- coding: utf-8 -*-
"""
系统配置管理接口。
仅允许系统管理员读取和更新已注册的系统配置。
敏感配置只写不读，接口响应不返回密钥明文。
"""
"""Administrator-only encrypted system-setting endpoints."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.schemas import SystemSettingOut, SystemSettingUpdate
from backend.app.core.audit import bind_audit_context, log_operation
from backend.app.core.auth import AuthenticatedUser, get_current_user
from backend.app.core.system_settings import SETTING_SPECS, decrypt_value, encrypt_value, mask_value, setting_spec, validate_value
from backend.app.db.models import SystemSetting
from backend.app.db.session import get_db
from utils.runtime_settings import publish_settings_snapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/v1/settings', tags=['system-settings'])


# NOTE: 系统设置独立于通用管理权限，仅 admin 角色可访问，避免普通管理员修改基础服务配置。
async def require_system_admin(request: Request, current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if not current_user.is_system_admin:
        raise HTTPException(403, '仅系统管理员可以管理系统设置')
    bind_audit_context(current_user, request)
    return current_user

def serialize(item: SystemSetting) -> dict:
    value = decrypt_value(item.encrypted_value)
    return {
        'key': item.setting_key, 'name': item.display_name, 'category': item.category,
        'description': item.description, 'is_sensitive': item.is_sensitive,
        'value': None if item.is_sensitive else value,
        'masked_value': mask_value(value) if item.is_sensitive else None,
        'has_value': bool(value), 'updated_at': item.updated_at, 'updated_by': item.updated_by,
    }

@router.get('', response_model=list[SystemSettingOut])
async def list_settings(session: AsyncSession = Depends(get_db), _: AuthenticatedUser = Depends(require_system_admin)):
    rows = (await session.execute(select(SystemSetting).order_by(SystemSetting.category, SystemSetting.id))).scalars()
    # 只展示注册表内的项：启动清理前的孤儿行（已下线配置）不该继续暴露一个 PATCH 必 404 的假入口。
    registered = {item['key'] for item in SETTING_SPECS}
    return [serialize(item) for item in rows if item.setting_key in registered]

@router.patch('/{setting_key}', response_model=SystemSettingOut)
async def update_setting(setting_key: str, payload: SystemSettingUpdate, session: AsyncSession = Depends(get_db), current_user: AuthenticatedUser = Depends(require_system_admin)):
    spec = setting_spec(setting_key)
    item = await session.scalar(select(SystemSetting).where(SystemSetting.setting_key == setting_key))
    if item is None:
        raise HTTPException(404, '系统配置尚未初始化')
    # NOTE: 敏感配置空提交代表保持原值，避免前端未回显明文时意外覆盖已生效的密钥。
    # Sensitive values are write-only. An empty value deliberately preserves the existing secret.
    if spec['sensitive'] and not (payload.value or '').strip():
        return serialize(item)
    if payload.value is None:
        raise HTTPException(422, '请填写配置值')
    item.encrypted_value = encrypt_value(validate_value(spec, payload.value))
    item.updated_by = current_user.id
    await log_operation(session, 'update', 'system_setting', setting_key, {'key': setting_key, 'sensitive': spec['sensitive']})
    await session.commit()
    await session.refresh(item)
    # 保存成功后刷新运行时快照（P0-1）：DB 是事实源已落库，发布失败只影响生效时点，
    # 下次启动或再次保存会自愈，不能让 Redis 故障反过来置已成功的保存于失败。
    try:
        await publish_settings_snapshot(session)
    except Exception:
        logger.error('系统设置已保存但运行时快照发布失败：%s', setting_key, exc_info=False)
    return serialize(item)
